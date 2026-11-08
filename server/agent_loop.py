"""Anthropic tool-use 循环：模型决策、工具回灌、请求预算与任务恢复。

职责：
    - 校验整轮工具调用，保留完整 assistant 内容块并配对回灌 tool_result。
    - 统一限制模型调用次数和 Token 消耗，触发同错提示、复盘与熔断。
    - 返回任务状态，通过调用方提供的回调持久化检查点。

链路：
    创建或恢复任务 → 处理待执行工具 → 回灌整批结果 → 按需复盘
    → 预算检查与模型调用 → 保存工具调用或返回最终文本。

边界：
    实例不保存会话消息，数据库与 Redis 由会话层管理。
    工具执行前保存 unknown，结果保存后标记 done；未知结果仅允许安全重试，
    其他写操作由上层核实后显式放行。预算耗尽和熔断均暂停任务，不视为任务完成。
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from server.agent_recover import ABORT_AT, REFLECT_AT, FailureRecord, Reflection, observe
from server.config import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_STEPS,
    DEFAULT_REQUEST_MAX_TOKENS,
    DEFAULT_TOKEN_MARGIN,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
)
from server.llm import LLMTurn, Message, ToolUse
from server.skill_runner import SkillRunner
from server.tools import ToolRegistry
from skills._shared.context import ToolContext

logger = logging.getLogger(__name__)

# ============================================================================
# 模型接口与执行状态
# ============================================================================


class TurnClient(Protocol):
    """Loop 依赖的单轮模型接口；客户端不执行工具或维护会话状态。"""

    async def run_turn(
        self,
        messages: list[Message],
        *,
        tools: list[dict],
        system: str,
        max_tokens: int | None = None,
    ) -> LLMTurn: ...


class ToolTrace(BaseModel):
    """工具执行摘要，不包含业务参数、完整结果或模型内部推理。"""

    round: int
    tool_use_id: str
    name: str
    is_error: bool
    duration_ms: float


class Operation(BaseModel):
    """一条工具调用的检查点；id 跨请求保持稳定，用于副作用去重。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    call: ToolUse

    # unknown 表示调用可能已产生副作用，不能等同于尚未执行的 pending。
    status: Literal["pending", "unknown", "done"] = "pending"
    result: dict[str, Any] | None = None


class TaskState(BaseModel):
    """可序列化的当前任务状态；恢复时复制使用，不修改调用方持有的对象。

    messages 保存模型回灌所需的完整协议，history_size 划分已有历史与本任务消息。
    request_* 每次继续时重新计数，total_tokens 与失败记录跨请求保留。
    history_saved 仅随完整历史原子提交，用于恢复完成检查点与历史提交之间的中断。
    """

    task_id: str = Field(default_factory=lambda: uuid4().hex)
    goal: str
    status: Literal["running", "paused", "waiting_user", "completed", "cancelled"] = "running"
    reason: str | None = None
    messages: list[Message] = Field(default_factory=list)
    history_size: int = 0
    operations: list[Operation] = Field(default_factory=list)
    failures: dict[str, FailureRecord] = Field(default_factory=dict)
    reflection_pending: list[str] = Field(default_factory=list)
    failure_call_ids: dict[str, str] = Field(default_factory=dict)
    resume_note: str | None = None
    traces: list[ToolTrace] = Field(default_factory=list)
    total_tokens: int = 0
    request_tokens: int = 0
    request_steps: int = 0
    usage_estimated: bool = False
    history_saved: bool = False


class AgentResult(BaseModel):
    """本次请求的返回结果；state 交给会话层保存，不直接作为公开 HTTP 响应。"""

    run_id: str
    text: str
    stop_reason: str
    rounds: int
    messages: list[Message]
    tool_traces: list[ToolTrace] = Field(default_factory=list)
    request_tokens: int = 0
    total_tokens: int = 0
    task_status: str = "completed"
    usage_estimated: bool = False
    state: TaskState | None = None


class BudgetStop(Exception):
    """内部预算停止信号，由 Loop 转为可继续的暂停结果。"""

    def __init__(self, reason: str):
        self.reason = reason


# ============================================================================
# 请求预算与工具调用主循环
# ============================================================================


class AgentLoop:
    """编排模型与工具；请求预算、错误记录和待执行操作均归属于任务状态。"""

    def __init__(
        self,
        llm: TurnClient,
        tools: ToolRegistry,
        *,
        system: str = "You are a helpful assistant. Use tools when needed; never invent results.",
        max_rounds: int = DEFAULT_MAX_STEPS,
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
        request_max_tokens: int = DEFAULT_REQUEST_MAX_TOKENS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        token_margin: int = DEFAULT_TOKEN_MARGIN,
    ):
        """注入模型、工具和执行上限；超时单位为秒，Token 预算按整次请求累计。"""
        if (
            min(max_rounds, tool_timeout, request_max_tokens, max_output_tokens) <= 0
            or token_margin < 0
        ):
            raise ValueError("Execution limits must be positive; token margin must be nonnegative")
        self.llm, self.tools = llm, tools
        self.runner = SkillRunner(tools)
        self.system = system
        self.max_rounds = max_rounds
        self.tool_timeout = tool_timeout
        self.request_max_tokens = request_max_tokens
        self.max_output_tokens = max_output_tokens
        self.token_margin = token_margin

    async def _model(
        self,
        state: TaskState,
        messages: list[Message],
        tools: list[dict],
        system: str,
        save: Callable[[], Awaitable[None]],
        reflection_record: FailureRecord | None = None,
    ) -> LLMTurn:
        """为一次模型调用预留预算，并按供应商 usage 结算。

        Args:
            state: 当前任务状态，原地更新请求次数、预留消耗与累计用量。
            messages: 本次模型输入；复盘输入与主对话输入分别构造。
            tools: 本次可用工具声明；复盘时传空列表。
            system: Anthropic 顶层系统提示词。
            save: 检查点回调，在发起请求前保存预留额度。
            reflection_record: 本次复盘所属记录；请求前标记已尝试，避免中断后重复复盘。

        Returns:
            包含完整内容块和停止原因的单轮响应。

        Raises:
            BudgetStop: 请求次数或剩余 Token 不足，不再发起模型生成请求。
            ValueError: 客户端返回负数输入计数。

        无 usage 或调用中断时保留预留消耗；本地字节估算不保证与供应商计数完全一致。
        """
        # ---- 1. 检查请求次数与输入开销，动态限制本次输出额度 ----
        if state.request_steps >= self.max_rounds:
            raise BudgetStop("max_rounds")
        counter = getattr(self.llm, "count_tokens", None)
        if counter is None:
            estimated_input = len(
                json.dumps([m.model_dump() for m in messages], ensure_ascii=False).encode()
            )
            estimated_input += len(json.dumps(tools, ensure_ascii=False).encode()) + len(
                system.encode()
            )
        else:
            estimated_input = await counter(messages, tools=tools, system=system)
        if estimated_input < 0:
            raise ValueError("Token count cannot be negative")
        remaining = (
            self.request_max_tokens - state.request_tokens - estimated_input - self.token_margin
        )
        if remaining <= 0:
            raise BudgetStop("token_budget_exhausted")
        output_cap = min(self.max_output_tokens, remaining)
        # ---- 2. 请求前保存额度与复盘标记，避免中断后丢失已发起调用的消耗 ----
        if reflection_record is not None:
            reflection_record.reflected = True
        reserved = estimated_input + output_cap
        state.request_tokens += reserved
        state.total_tokens += reserved
        state.request_steps += 1
        prior_estimated = state.usage_estimated
        state.usage_estimated = True
        await save()
        turn = await self.llm.run_turn(messages, tools=tools, system=system, max_tokens=output_cap)
        # ---- 3. 有实际 usage 时结算差额，否则保留预留值并标记估算 ----
        if turn.usage is not None:
            difference = turn.usage.total - reserved
            state.request_tokens += difference
            state.total_tokens += difference
            state.usage_estimated = prior_estimated
        else:
            state.usage_estimated = True
        await save()
        return turn

    async def run(
        self,
        user_input: str,
        *,
        history: list[Message] | None = None,
        context: ToolContext | None = None,
        state: TaskState | None = None,
        checkpoint: Callable[[TaskState], Awaitable[None]] | None = None,
    ) -> AgentResult:
        """执行一次用户请求，返回已完成或可恢复的任务结果。

        Args:
            user_input: 新任务目标，或继续任务时的补充信息。
            history: 新任务使用的会话历史；恢复任务使用检查点中的完整消息。
            context: 调用方提供的可信身份，不由模型参数指定。
            state: 待恢复的任务检查点；未提供时创建新任务。
            checkpoint: 接收状态副本的异步回调；未提供时仅在内存中执行。

        Returns:
            最终文本或暂停提示，以及用量、工具 trace 和后续恢复所需的任务状态。

        Raises:
            ValueError: 输入为空，或模型返回不符合约定的工具调用和停止原因。
            asyncio.CancelledError: 保存暂停状态后继续传播取消信号。

        模型、工具基础设施和检查点写入异常向上层传播；保存失败时无法保证最新进度已落库。
        """
        # 新请求只重置本次预算；失败计数、已完成操作与任务累计消耗不清零。
        if not user_input.strip():
            raise ValueError("User input must not be empty")
        if state is None:
            prior = [m.model_copy(deep=True) for m in (history or [])]
            state = TaskState(
                goal=user_input,
                messages=prior + [Message(role="user", content=user_input)],
                history_size=len(prior),
                task_id=(context.run_id if context and context.run_id else uuid4().hex),
            )
        else:
            state = state.model_copy(deep=True)
            state.resume_note = user_input
        state.status, state.reason = "running", None
        state.request_tokens, state.request_steps = 0, 0
        run_context = (context or ToolContext()).model_copy(update={"run_id": state.task_id})

        async def save() -> None:
            """传递状态副本，防止回调持有并修改 Loop 正在使用的对象。"""
            if checkpoint is not None:
                await checkpoint(state.model_copy(deep=True))

        async def stop(reason: str, text: str, *, waiting: bool = False) -> AgentResult:
            """先保存暂停状态，再返回提示；不伪造 assistant 消息补齐待执行工具。"""
            state.status = "waiting_user" if waiting else "paused"
            state.reason = reason
            await save()
            return self._result(state, text, reason)

        await save()
        try:
            while True:
                # ---- 1. 完成已持久化的工具调用，成功结果不重复执行 ----
                for operation in state.operations:
                    if operation.status == "done":
                        continue
                    if state.request_tokens >= self.request_max_tokens:
                        raise BudgetStop("token_budget_exhausted")
                    tool = self.tools.get(operation.call.name)
                    recovering = operation.status == "unknown"
                    if recovering and (tool is None or not tool.retry_safe):
                        return await stop(
                            "unknown_tool_result",
                            "工具结果未知，请先核实外部状态；本次没有重放该写操作。",
                            waiting=True,
                        )
                    # 先标记可能产生副作用，再进入 handler；中断后不得把该操作当成未执行。
                    operation.status = "unknown"
                    await save()
                    started = asyncio.get_running_loop().time()
                    call_context = run_context.model_copy(
                        update={
                            "operation_id": operation.id,
                            "tool_use_id": operation.call.id,
                            "recovering": recovering,
                        }
                    )
                    result = await self.runner.execute(
                        operation.call, timeout=self.tool_timeout, context=call_context
                    )
                    duration_ms = (asyncio.get_running_loop().time() - started) * 1000
                    logger.info(
                        "tool_executed tool=%s call_id=%s duration_ms=%.2f is_error=%s",
                        operation.call.name,
                        operation.call.id,
                        duration_ms,
                        result.get("is_error", False),
                    )
                    payload = json.loads(result["content"])
                    error_code = payload.get("error", {}).get("code")
                    read_only = tool is not None and (
                        tool.read_only
                        or operation.call.input.get("action") in tool.read_only_actions
                    )
                    # 写操作超时或异常不能证明事务未提交，保留 unknown 等待核实。
                    if not read_only and error_code in {"TOOL_TIMEOUT", "TOOL_ERROR"}:
                        return await stop(
                            "unknown_tool_result",
                            "写操作结果不确定，已保存检查点，继续前需要核实状态。",
                            waiting=True,
                        )
                    operation.result, operation.status = result, "done"
                    state.traces.append(
                        ToolTrace(
                            round=state.request_steps,
                            tool_use_id=operation.call.id,
                            name=operation.call.name,
                            is_error=result.get("is_error", False),
                            duration_ms=duration_ms,
                        )
                    )
                    key = observe(state.failures, operation.call.name, operation.call.input, result)
                    if key is not None:
                        record = state.failures[key]
                        state.failure_call_ids[key] = operation.call.id
                        if record.count == REFLECT_AT and not record.reflected:
                            state.reflection_pending.append(key)
                        if record.count >= ABORT_AT:
                            # 保留尚未执行的调用在检查点，不标记成已经执行。
                            await save()
                            return await stop(
                                "repeated_tool_error",
                                "同类工具错误已达上限，任务已暂停。请补充有效信息或调整目标后继续。",
                                waiting=True,
                            )
                    await save()
                # 整批结果配对完成后才写入 user 消息，避免未配对块进入下一次模型请求。
                if state.operations:
                    state.messages.append(
                        Message(role="user", content=[op.result for op in state.operations])
                    )
                    state.operations = []
                    await save()
                # 用户补充信息放在工具结果之后，保持 assistant/tool_result 的紧邻关系。
                if state.resume_note:
                    state.messages.append(Message(role="user", content=state.resume_note))
                    state.resume_note = None
                    await save()
                # ---- 2. 每个错误簇最多一次模型复盘，与主模型共享步骤/token 预算 ----
                if state.reflection_pending:
                    key = state.reflection_pending[0]
                    record = state.failures[key]
                    # 已尝试的复盘不因进程中断再次消耗模型请求；已成功关闭的错误簇也跳过。
                    if record.reflected or record.closed:
                        record.reflection = (
                            record.reflection or "上次复盘中断；核对约束，缺少事实时询问用户。"
                        )
                        state.reflection_pending.pop(0)
                        await save()
                        continue
                    turn = await self._model(
                        state,
                        [
                            Message(
                                role="user",
                                content=json.dumps(
                                    {
                                        "tool": record.tool,
                                        "error_code": record.code,
                                        "error": record.cause,
                                        "schema": self.tools.get(record.tool).spec()
                                        if self.tools.get(record.tool)
                                        else None,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        ],
                        [],
                        (
                            "分析工具失败，只输出 JSON：cause、change、missing_information、"
                            "next_action(retry或ask_user)。不要编造事实，不要输出内部推理。"
                        ),
                        save,
                        reflection_record=record,
                    )
                    record.reflected = True
                    state.reflection_pending.pop(0)
                    try:
                        reflection = Reflection.model_validate_json(turn.text)
                        record.reflection = reflection.model_dump_json()
                    except ValueError:
                        record.reflection = "复盘格式不合法；核对参数约束，缺少事实时询问用户。"
                        reflection = None
                    # 将简短纠错结论加入既有工具反馈，保持 Anthropic 消息配对。
                    for message in reversed(state.messages):
                        if isinstance(message.content, list):
                            for block in message.content:
                                if (
                                    block.get("type") == "tool_result"
                                    and block.get("tool_use_id") == state.failure_call_ids[key]
                                ):
                                    payload = json.loads(block["content"])
                                    payload["reflection"] = record.reflection
                                    block["content"] = json.dumps(payload, ensure_ascii=False)
                                    break
                            else:
                                continue
                            break
                    await save()
                    if reflection and reflection.next_action == "ask_user":
                        return await stop(
                            "needs_user_input",
                            reflection.missing_information or reflection.change,
                            waiting=True,
                        )
                    # 同批次可能有多个错误簇，先逐个复盘，再让主模型决策。
                    continue
                # ---- 3. 正常决策；没有工具调用时完成任务 ----
                turn = await self._model(
                    state, state.messages, self.tools.specs(), self.system, save
                )
                if turn.stop_reason == "max_tokens":
                    return await stop(
                        "max_tokens", "模型输出被截断，任务已暂停，未执行截断响应中的工具。"
                    )
                if turn.stop_reason not in {"end_turn", "stop_sequence", "refusal", "tool_use"}:
                    raise ValueError("Unsupported LLM stop reason")
                # 执行前校验整轮调用；协议错误时不能先执行一部分工具再报错。
                calls = [
                    ToolUse.model_validate(b) for b in turn.content if b.get("type") == "tool_use"
                ]
                seen = {
                    b["id"]
                    for m in state.messages[state.history_size :]
                    if isinstance(m.content, list)
                    for b in m.content
                    if b.get("type") == "tool_use"
                }
                if len({c.id for c in calls}) != len(calls) or any(c.id in seen for c in calls):
                    raise ValueError("LLM returned duplicate tool_use IDs")
                if turn.stop_reason == "tool_use" and not calls:
                    raise ValueError("Missing tool_use blocks")
                # 原样保留 thinking/signature 等供应商字段，仅向用户提取 text。
                state.messages.append(Message(role="assistant", content=turn.content))
                state.operations = [Operation(call=call) for call in calls]
                if not calls:
                    state.status, state.reason = "completed", turn.stop_reason
                    await save()
                    return self._result(state, turn.text, turn.stop_reason)
                await save()
        except BudgetStop as exc:
            return await stop(exc.reason, "本次请求执行预算已用尽，进度已保存，可以继续。")
        except asyncio.CancelledError:
            state.status, state.reason = "paused", "cancelled"
            await save()
            raise
        except Exception:
            state.status, state.reason = "paused", "execution_error"
            await save()
            raise

    @staticmethod
    def _result(state: TaskState, text: str, reason: str) -> AgentResult:
        """汇总本次响应与任务状态，不在 Loop 内写入会话历史。"""
        return AgentResult(
            run_id=state.task_id,
            text=text,
            stop_reason=reason,
            rounds=state.request_steps,
            messages=state.messages,
            tool_traces=state.traces,
            request_tokens=state.request_tokens,
            total_tokens=state.total_tokens,
            task_status=state.status,
            usage_estimated=state.usage_estimated,
            state=state,
        )
