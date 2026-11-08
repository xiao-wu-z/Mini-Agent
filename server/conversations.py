"""会话编排服务：隔离窗口、装配上下文、管理当前任务与对话历史。

链路：
    校验用户归属 → 获取会话锁 → 读取当前任务 → 创建、继续或取消
    → 执行 Loop 并保存检查点 → 任务完成后原子提交完整消息。

存储：
    MySQL 保存完整历史与当前任务检查点，Redis 仅缓存已提交历史。
    暂停时未配对的工具块留在检查点，不写入公开历史。

边界：
    单进程内同窗口串行执行，不提供分布式调度。
    结果未知的写操作仅在工具声明可安全重试，或调用方核实未执行后允许重试。
"""

import asyncio
import logging
import time
from uuid import uuid4

from pydantic import BaseModel

from server.agent_loop import AgentLoop, TaskState, ToolTrace
from server.cache import SessionCache
from server.context import ContextState
from server.llm import Message
from server.store.sessions import ConversationCode, ConversationError, SessionStore
from skills._shared.context import ToolContext

logger = logging.getLogger(__name__)

# ============================================================================
# 对外响应与会话服务
# ============================================================================


class ChatResponse(BaseModel):
    """公开聊天响应；只返回文本、执行摘要和用量，不暴露原始任务检查点。"""

    session_id: str
    turn_id: str
    text: str
    stop_reason: str
    rounds: int
    tool_traces: list[ToolTrace]
    task_status: str = "completed"
    request_tokens: int = 0
    total_tokens: int = 0
    usage_estimated: bool = False


class ConversationService:
    """协调存储、缓存与无会话状态的 Loop，保证当前窗口的任务不会被新请求覆盖。"""

    def __init__(
        self,
        store: SessionStore,
        cache: SessionCache,
        loop: AgentLoop,
        *,
        timeout_seconds: float,
    ):
        self.store = store
        self.cache = cache
        self.loop = loop
        self.timeout_seconds = timeout_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    async def history(self, user_id: str, session_id: str) -> list[Message]:
        """校验归属后按消息水位读缓存，缓存未命中时从 MySQL 重建。"""
        info = await self.store.get(user_id, session_id)
        cached = await self.cache.get_history(user_id, session_id, info.version)
        if cached is not None:
            return cached
        records = await self.store.load_messages(user_id, session_id, through_version=info.version)
        messages = [Message(role=row.role, content=row.content) for row in records]
        await self.cache.set_history(user_id, session_id, info.version, messages)
        return messages

    async def chat(
        self,
        user_id: str,
        session_id: str,
        text: str,
        *,
        action: str = "new",
        confirmed_not_executed: list[str] | None = None,
    ) -> ChatResponse:
        """串行处理一个窗口的任务请求，并在退出时释放进程内会话锁。

        Args:
            user_id: 可信调用方提供的用户身份。
            session_id: 当前窗口的会话标识，必须属于该用户。
            text: 新任务目标或继续任务时的补充信息。
            action: new 创建任务，continue 恢复任务，cancel 取消后续执行。
            confirmed_not_executed: 经调用方核实未执行的未知操作 ID；仅用于继续当前任务。

        Returns:
            最终答案或暂停提示，以及本次请求用量与任务状态。

        Raises:
            ConversationError: 归属不符、会话忙碌、任务状态冲突或 Loop 执行失败。
            asyncio.CancelledError: 继续传播取消信号；检查点由 Loop 尽力保存。
        """
        await self.store.get(user_id, session_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise ConversationError(ConversationCode.SESSION_BUSY, "会话正在处理请求。")
        async with lock:
            try:
                return await self._chat(
                    user_id,
                    session_id,
                    text,
                    action=action,
                    confirmed_not_executed=confirmed_not_executed or [],
                )
            finally:
                self._locks.pop(session_id, None)

    async def _chat(
        self,
        user_id: str,
        session_id: str,
        text: str,
        *,
        action: str,
        confirmed_not_executed: list[str],
    ) -> ChatResponse:
        """在持有当前会话锁时处理任务生命周期与历史提交。"""
        # ---- 1. 恢复完成后尚未提交的历史，避免重复执行原任务 ----
        info = await self.store.get(user_id, session_id)
        raw = await self.store.load_task(user_id, session_id)
        task = TaskState.model_validate(raw) if raw is not None else None
        if task is not None and task.status == "completed" and not task.history_saved:
            await self._commit(user_id, session_id, task, info.version)
            info = await self.store.get(user_id, session_id)
        # ---- 2. 处理取消与继续；未完成任务不能被新目标静默覆盖 ----
        if action == "cancel":
            if task is None:
                raise ConversationError(ConversationCode.TASK_NOT_FOUND, "没有当前任务。")
            task.status, task.reason = "cancelled", "user_cancelled"
            task.checkpoint.phase = "cancelled"
            task.checkpoint.id = uuid4().hex
            task.checkpoint.version += 1
            task.checkpoint.updated_at = time.time()
            await self.store.save_task(
                user_id, session_id, task.model_dump(mode="json"), expected_version=info.version
            )
            return ChatResponse(
                session_id=session_id,
                turn_id=task.task_id,
                text="当前任务已取消。",
                stop_reason="user_cancelled",
                rounds=0,
                tool_traces=[],
                task_status="cancelled",
            )
        continuing = action == "continue"
        active = task is not None and task.status in {"running", "paused", "waiting_user"}
        if continuing and task is not None and task.status == "completed":
            last = task.messages[-1].content
            answer = (
                last
                if isinstance(last, str)
                else "".join(block.get("text", "") for block in last if block.get("type") == "text")
            )
            return ChatResponse(
                session_id=session_id,
                turn_id=task.task_id,
                text=answer,
                stop_reason=task.reason or "end_turn",
                rounds=0,
                tool_traces=task.traces,
                task_status="completed",
                request_tokens=0,
                total_tokens=task.total_tokens,
                usage_estimated=task.usage_estimated,
            )
        if continuing and not active:
            raise ConversationError(ConversationCode.TASK_NOT_FOUND, "没有可以继续的任务。")
        if not continuing and active:
            raise ConversationError(
                ConversationCode.TASK_PENDING, "当前任务未结束，请显式继续或取消，不能覆盖进度。"
            )
        # ---- 3. 核对人工确认的操作归属，只有当前任务的 unknown 操作可以放行 ----
        if confirmed_not_executed:
            unknown = (
                {
                    operation.id: operation
                    for operation in task.operations
                    if operation.status == "unknown"
                }
                if continuing and task is not None
                else {}
            )
            if any(operation_id not in unknown for operation_id in confirmed_not_executed):
                raise ConversationError(
                    ConversationCode.TASK_PENDING, "只能确认当前任务中结果未知的操作。"
                )
            for operation_id in confirmed_not_executed:
                unknown[operation_id].status = "pending"
        # ---- 4. 原文完整加载；模型工作视图由 Loop 按持久化摘要边界生成 ----
        history = [] if continuing else await self.history(user_id, session_id)
        raw_context = await self.store.load_context(user_id, session_id)
        context_state = ContextState.model_validate(raw_context or {})

        async def checkpoint(state: TaskState) -> None:
            """独立短事务保存进度；不在模型和工具执行期间持有数据库事务。"""
            await self.store.save_task(
                user_id, session_id, state.model_dump(mode="json"), expected_version=info.version
            )

        # ---- 5. 在请求总超时内执行；暂停进度保留在检查点，完成后提交完整历史 ----
        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await self.loop.run(
                    text,
                    history=history,
                    context=ToolContext(user_id=user_id, session_id=session_id),
                    state=task if continuing else None,
                    checkpoint=checkpoint,
                    context_state=context_state,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("conversation_failed error_type=%s", type(exc).__name__)
            code = (
                ConversationCode.RUN_TIMEOUT
                if isinstance(exc, TimeoutError)
                else ConversationCode.RUN_FAILED
            )
            raise ConversationError(
                code, "任务已中断；请查询当前任务检查点，再使用 continue 继续。"
            ) from exc
        if result.state.status == "completed":
            await self._commit(user_id, session_id, result.state, info.version)
        return ChatResponse(
            session_id=session_id,
            turn_id=result.run_id,
            text=result.text,
            stop_reason=result.stop_reason,
            rounds=result.rounds,
            tool_traces=result.tool_traces,
            task_status=result.task_status,
            request_tokens=result.request_tokens,
            total_tokens=result.total_tokens,
            usage_estimated=result.usage_estimated,
        )

    async def _commit(self, user_id: str, session_id: str, task: TaskState, version: int) -> None:
        """原子保存本任务完整消息与已提交标记，不重复保存上下文中的既有历史。"""
        task.history_saved = True
        await self.store.save_turn(
            user_id,
            session_id,
            task.task_id,
            task.task_messages,
            expected_version=version,
            task_state=task.model_dump(mode="json"),
            context_state=task.context_state.model_dump(mode="json"),
        )

    async def task(self, user_id: str, session_id: str) -> dict:
        """返回经归属校验的任务摘要，不暴露协议消息和模型内部推理。

        检查点仍为 running、但本实例没有持有会话锁时，按中断后的 paused 状态展示。
        此判断适用于单 worker 部署，不代表跨进程运行状态探测。

        Raises:
            ConversationError: 会话不属于该用户，或尚未创建当前任务。
        """
        raw = await self.store.load_task(user_id, session_id)
        if raw is None:
            raise ConversationError(ConversationCode.TASK_NOT_FOUND, "没有当前任务。")
        task = TaskState.model_validate(raw)
        interrupted = task.status == "running" and not (
            self._locks.get(session_id) and self._locks[session_id].locked()
        )
        return {
            "task_id": task.task_id,
            "goal": task.goal,
            "status": "paused" if interrupted else task.status,
            "reason": "interrupted" if interrupted else task.reason,
            "total_tokens": task.total_tokens,
            "request_tokens": task.request_tokens,
            "usage_estimated": task.usage_estimated,
            "checkpoint": task.checkpoint.model_dump(),
            "context": {
                "summary_version": task.context_state.version,
                "summary_start_sequence": task.context_state.start + 1
                if task.context_state.summary
                else None,
                "summary_until_sequence": task.context_state.end,
                "large_result_count": len(task.context_state.result_views),
            },
            "operations": [
                {"operation_id": op.id, "tool": op.call.name, "status": op.status}
                for op in task.operations
            ],
            "failures": [
                {
                    "tool": item.tool,
                    "code": item.code,
                    "count": item.count,
                    "reflected": item.reflected,
                    "closed": item.closed,
                }
                for item in task.failures.values()
            ],
            "completed_tool_count": sum(not trace.is_error for trace in task.traces),
        }
