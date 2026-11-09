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
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from server.agent_recover import ABORT_AT, REFLECT_AT, FailureRecord, Reflection, observe
from server.config import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_STEPS,
    DEFAULT_REQUEST_MAX_TOKENS,
    DEFAULT_TOKEN_MARGIN,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    ContextPolicy,
    ToolResultPolicy,
)
from server.context import SUMMARY_SYSTEM, ContextState, message_units, summary_input
from server.llm import LLMTurn, Message, TextDeltaCallback, TokenUsage, ToolUse
from server.skill_runner import SkillRunner
from server.sse import EventSink, StreamEvent, tool_result_event, tool_use_event
from server.store.tool_results import ToolResultStore
from server.tools import ToolRegistry
from server.tracing import SpanHandle, TraceRecorder
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


class CheckpointInfo(BaseModel):
    """最新恢复点的标识信息；与其关联的任务状态一同原子保存。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    version: int = 0
    phase: str = "prepare"
    updated_at: float = 0


class TaskState(BaseModel):
    """可序列化的当前任务状态；恢复时复制使用，不修改调用方持有的对象。

    messages 保存完整原始上下文，task_messages 单独保存待提交的本任务原文。
    context_state 只决定模型工作视图，不能改变原文或影响历史提交范围。
    request_* 每次继续时重新计数，total_tokens 与失败记录跨请求保留。
    history_saved 仅随完整历史原子提交，用于恢复完成检查点与历史提交之间的中断。
    """

    task_id: str = Field(default_factory=lambda: uuid4().hex)
    goal: str
    status: Literal["running", "paused", "waiting_user", "completed", "cancelled"] = "running"
    reason: str | None = None
    messages: list[Message] = Field(default_factory=list)
    task_messages: list[Message] = Field(default_factory=list)
    context_state: ContextState = Field(default_factory=ContextState)
    checkpoint: CheckpointInfo = Field(default_factory=CheckpointInfo)
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

    @model_validator(mode="before")
    @classmethod
    def upgrade_checkpoint(cls, value: Any) -> Any:
        """读取旧恢复点时一次性分离任务原文；新状态不再使用 history_size 提交消息。"""
        if isinstance(value, dict) and "history_size" in value:
            value = dict(value)
            size = value.pop("history_size")
            value.setdefault("task_messages", value.get("messages", [])[size:])
        return value


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
        context_policy: ContextPolicy | None = None,
        result_policy: ToolResultPolicy | None = None,
        result_store: ToolResultStore | None = None,
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
        self.context_policy = context_policy or ContextPolicy()
        self.result_policy = result_policy or ToolResultPolicy()
        self.result_store = result_store

    async def _count(self, messages: list[Message], tools: list[dict], system: str) -> int:
        """以同一口径计数预算与容量，FakeLLM 无计数接口时使用 UTF-8 字节估算。"""
        counter = getattr(self.llm, "count_tokens", None)
        if counter is not None:
            count = await counter(messages, tools=tools, system=system)
        else:
            body = {
                "messages": [m.model_dump() for m in messages],
                "tools": tools,
                "system": system,
            }
            count = len(json.dumps(body, ensure_ascii=False).encode())
        if count < 0:
            raise ValueError("Token count cannot be negative")
        return count

    async def _model(
        self,
        state: TaskState,
        messages: list[Message],
        tools: list[dict],
        system: str,
        save: Callable[[], Awaitable[None]],
        reflection_record: FailureRecord | None = None,
        output_limit: int | None = None,
        phase: str = "model",
        trace: TraceRecorder | None = None,
        parent_span_id: str | None = None,
        on_text_delta: TextDeltaCallback | None = None,
    ) -> LLMTurn:
        """为一次模型调用预留预算，并按供应商 usage 结算。

        Args:
            state: 当前任务状态，原地更新请求次数、预留消耗与累计用量。
            messages: 本次模型输入；复盘输入与主对话输入分别构造。
            tools: 本次可用工具声明；复盘时传空列表。
            system: Anthropic 顶层系统提示词。
            save: 检查点回调，在发起请求前保存预留额度。
            reflection_record: 本次复盘所属记录；请求前标记已尝试，避免中断后重复复盘。
            output_limit: 摘要等辅助调用的额外输出上限，与部署和剩余预算共同限制。
            phase: 随额度预留一同保存的当前执行阶段。
            trace: 请求内采集器，不保存于任务检查点，也不参与模型决策。
            parent_span_id: 调用方显式指定的所属步骤。
            on_text_delta: 仅主决策的公开文本增量回调；摘要和复盘不传入。

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
        estimated_input = await self._count(messages, tools, system)
        remaining = (
            self.request_max_tokens - state.request_tokens - estimated_input - self.token_margin
        )
        if remaining <= 0:
            raise BudgetStop("token_budget_exhausted")
        context_remaining = self.context_policy.window_tokens - estimated_input - self.token_margin
        if context_remaining <= 0:
            raise BudgetStop("context_limit")
        output_cap = min(self.max_output_tokens, remaining, context_remaining)
        if output_limit is not None:
            output_cap = min(output_cap, output_limit)
        # ---- 2. 请求前保存额度与复盘标记，避免中断后丢失已发起调用的消耗 ----
        if reflection_record is not None:
            reflection_record.reflected = True
        reserved = estimated_input + output_cap
        state.request_tokens += reserved
        state.total_tokens += reserved
        state.request_steps += 1
        prior_estimated = state.usage_estimated
        state.usage_estimated = True
        state.checkpoint.phase = phase
        await save()
        recorder = trace or TraceRecorder()
        settings = getattr(self.llm, "settings", None)
        model_name = getattr(settings, "llm_model", None)
        generation_name = {
            "model": "decision",
            "compression": "summary",
            "reflection": "reflection",
        }
        async with recorder.span(
            "generation." + generation_name[phase],
            type="generation",
            parent_id=parent_span_id,
            input={
                "message_count": len(messages),
                "tool_names": [t["name"] for t in tools],
                "input_tokens": estimated_input,
                "max_output_tokens": output_cap,
            },
            model=model_name,
        ) as generation:
            turn = None
            try:
                stream_turn = getattr(self.llm, "stream_turn", None)
                if on_text_delta is not None and stream_turn is not None:
                    turn = await stream_turn(
                        messages,
                        tools=tools,
                        system=system,
                        max_tokens=output_cap,
                        on_text_delta=on_text_delta,
                    )
                else:
                    turn = await self.llm.run_turn(
                        messages, tools=tools, system=system, max_tokens=output_cap
                    )
                    if on_text_delta is not None and turn.text:
                        await on_text_delta(turn.text)
            finally:
                usage = turn.usage if turn is not None else None
                generation.usage(
                    usage or TokenUsage(input_tokens=estimated_input, output_tokens=output_cap),
                    estimated=usage is None,
                )
            generation.output(
                {
                    "text": turn.text,
                    "tool_names": [
                        b.get("name") for b in turn.content if b.get("type") == "tool_use"
                    ],
                },
                stop_reason=turn.stop_reason,
            )
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

    async def _prepare_context(
        self,
        state: TaskState,
        save: Callable[[], Awaitable[None]],
        trace: TraceRecorder | None = None,
        parent_span_id: str | None = None,
    ) -> list[Message]:
        """模型决策前同步压缩中段；先生成并校验新视图，再保存摘要检查点。

        软阈值允许受频次限制，硬阈值必须处理。摘要分批输入同一预算执行器；
        失败或未缩短输入时不覆盖旧摘要，容量不足则暂停，绝不硬截协议块。
        """
        policy = self.context_policy
        recorder = trace or TraceRecorder()
        goal_index = len(state.messages) - len(state.task_messages)

        def render(context: ContextState) -> list[Message]:
            """每次计数均使用相同的任务目标与原始序列。"""
            return context.render(state.messages, goal=state.goal, goal_index=goal_index)

        view = render(state.context_state)
        tokens = await self._count(view, self.tools.specs(), self.system)
        input_capacity = policy.window_tokens - self.max_output_tokens - self.token_margin
        if input_capacity <= 0:
            raise BudgetStop("context_limit")
        soft = int(input_capacity * policy.soft_ratio)
        hard = int(input_capacity * policy.hard_ratio)
        while tokens >= soft:
            required = tokens >= hard
            context = state.context_state
            if not required and time.time() - context.compressed_at < policy.min_interval_seconds:
                return view
            interval = context.compression_range(state.messages, policy)
            if interval is None:
                if required:
                    raise BudgetStop("context_limit")
                return view
            start, target_end = interval
            projected = context.projected(state.messages)
            ends = [end for _, end in message_units(projected) if start < end <= target_end]
            # ---- 1. 摘要输入也受容量限制；过长时缩小为完整单元组成的前缀 ----
            while ends:
                end = ends[-1]
                prompt = summary_input(context.summary, projected[start:end])
                prompt_tokens = await self._count(prompt, [], SUMMARY_SYSTEM)
                if (
                    prompt_tokens + policy.summary_max_tokens + self.token_margin
                    <= policy.window_tokens
                ):
                    break
                ends = ends[: len(ends) // 2]
            if not ends:
                if required:
                    raise BudgetStop("context_limit")
                return view
            # ---- 2. 摘要调用计入请求次数和 Token；无有效完整文本时保留旧视图 ----
            try:
                turn = await self._model(
                    state,
                    prompt,
                    [],
                    SUMMARY_SYSTEM,
                    save,
                    output_limit=policy.summary_max_tokens,
                    phase="compression",
                    trace=trace,
                    parent_span_id=parent_span_id,
                )
            except (BudgetStop, asyncio.CancelledError):
                raise
            except Exception:
                logger.warning("context_compression_failed")
                await recorder.event(
                    "context.compression_failed",
                    parent_id=parent_span_id,
                    attributes={"reason": "model_error"},
                )
                if required:
                    raise BudgetStop("compression_failed") from None
                return view
            if (
                turn.stop_reason not in {"end_turn", "stop_sequence"}
                or not turn.text.strip()
                or any(block.get("type") == "tool_use" for block in turn.content)
            ):
                await recorder.event(
                    "context.compression_failed",
                    parent_id=parent_span_id,
                    attributes={"reason": "invalid_summary"},
                )
                if required:
                    raise BudgetStop("compression_failed")
                return view
            candidate = context.model_copy(deep=True)
            candidate.start = context.start if context.summary else start
            candidate.end, candidate.summary = end, turn.text.strip()
            candidate.version += 1
            candidate.compressed_at = time.time()
            candidate_view = render(candidate)
            candidate_tokens = await self._count(candidate_view, self.tools.specs(), self.system)
            if candidate_tokens >= tokens:
                await recorder.event(
                    "context.compression_failed",
                    parent_id=parent_span_id,
                    attributes={"reason": "ineffective_summary"},
                )
                if required:
                    raise BudgetStop("compression_ineffective")
                return view
            # ---- 3. 发布新摘要与边界；原始消息、稳定操作标识不变 ----
            state.context_state = candidate
            await save()
            await recorder.event(
                "context.compressed",
                parent_id=parent_span_id,
                attributes={
                    "input_tokens_before": tokens,
                    "input_tokens_after": candidate_tokens,
                    "summary_version": candidate.version,
                    "start_sequence": candidate.start + 1,
                    "until_sequence": candidate.end,
                },
            )
            view, tokens = candidate_view, candidate_tokens
            if tokens < hard:
                return view
        return view

    @staticmethod
    def _apply_reflection(state: TaskState, key: str) -> None:
        """只增强本任务的匹配反馈，避免历史任务复用调用 ID 时被意外修改。"""
        call_id = state.failure_call_ids[key]
        task_start = len(state.messages) - len(state.task_messages)
        for task_index, message in enumerate(state.task_messages):
            if not isinstance(message.content, list):
                continue
            if not any(block.get("tool_use_id") == call_id for block in message.content):
                continue
            source_index = task_start + task_index
            groups = [message.content, state.messages[source_index].content]
            projected = state.context_state.result_views.get(source_index)
            if projected is not None:
                groups.append(projected)
            for blocks in groups:
                for block in blocks:
                    if block.get("type") == "tool_result" and block.get("tool_use_id") == call_id:
                        payload = json.loads(block["content"])
                        payload["reflection"] = state.failures[key].reflection
                        block["content"] = json.dumps(payload, ensure_ascii=False)
            return

    async def _collect_results(self, state: TaskState, context: ToolContext) -> None:
        """整批回灌原文并建立有界结果视图；持久化成功后才发布引用。

        读取引用的工具已经通过分页限制输出，不再次外置为新引用，避免指针套指针。
        """
        results = [op.result for op in state.operations]
        views = []
        for operation in state.operations:
            result = operation.result
            if (
                operation.call.name == "read_tool_result"
                or len(result["content"].encode()) <= self.result_policy.inline_max_bytes
            ):
                views.append(result)
                continue
            if self.result_store is None:
                raise BudgetStop("result_storage_unavailable")
            await self.result_store.put(operation.id, result, context=context)
            preview = result["content"][: self.result_policy.preview_chars]
            views.append(
                {
                    **result,
                    "content": json.dumps(
                        {
                            "ref": operation.id,
                            "summary": "结果较大，使用 read_tool_result 分页读取。",
                            "preview": preview,
                            "preview_is_partial": len(preview) < len(result["content"]),
                            "size_bytes": len(result["content"].encode()),
                            "has_more": len(preview) < len(result["content"]),
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        if views != results:
            state.context_state.result_views[len(state.messages)] = views
        message = Message(role="user", content=results)
        state.messages.append(message)
        state.task_messages.append(message.model_copy(deep=True))
        state.operations = []
        state.checkpoint.phase = "tool_results"

    async def run(
        self,
        user_input: str,
        *,
        history: list[Message] | None = None,
        context: ToolContext | None = None,
        state: TaskState | None = None,
        checkpoint: Callable[[TaskState], Awaitable[None]] | None = None,
        context_state: ContextState | None = None,
        trace: TraceRecorder | None = None,
        on_event: EventSink | None = None,
    ) -> AgentResult:
        """执行一次用户请求，返回已完成或可恢复的任务结果。

        Args:
            user_input: 新任务目标，或继续任务时的补充信息。
            history: 新任务使用的会话历史；恢复任务使用检查点中的完整消息。
            context: 调用方提供的可信身份，不由模型参数指定。
            state: 待恢复的任务检查点；未提供时创建新任务。
            checkpoint: 接收状态副本的异步回调；未提供时仅在内存中执行。
            context_state: 新任务沿用的会话摘要与结果引用视图；恢复任务使用检查点中的值。
            trace: 显式请求内采集器，跨恢复请求创建新的采集器，不沿用旧 Span。
            on_event: 公开文本与工具执行摘要的异步回调；内部推理不进入该回调。

        Returns:
            最终文本或暂停提示，以及用量、工具 trace 和后续恢复所需的任务状态。

        Raises:
            ValueError: 输入为空，或模型返回不符合约定的工具调用和停止原因。
            asyncio.CancelledError: 保存暂停状态后继续传播取消信号。

        模型、工具基础设施和检查点写入异常向上层传播；保存失败时无法保证最新进度已落库。
        """
        recorder = trace or TraceRecorder()
        round_span_id: str | None = None
        round_handle: SpanHandle | None = None
        # 新请求只重置本次预算；失败计数、已完成操作与任务累计消耗不清零。
        if not user_input.strip():
            raise ValueError("User input must not be empty")
        if state is None:
            prior = [m.model_copy(deep=True) for m in (history or [])]
            state = TaskState(
                goal=user_input,
                messages=prior + [Message(role="user", content=user_input)],
                task_messages=[Message(role="user", content=user_input)],
                context_state=(context_state or ContextState()).model_copy(deep=True),
                task_id=(context.run_id if context and context.run_id else uuid4().hex),
            )
        else:
            state = state.model_copy(deep=True)
            state.resume_note = user_input
        state.status, state.reason = "running", None
        state.request_tokens, state.request_steps = 0, 0
        run_context = (context or ToolContext()).model_copy(update={"run_id": state.task_id})

        async def emit_text(text: str) -> None:
            """主模型文本直接推送；以最终 done 状态判断是否完整完成。"""
            if on_event is not None and text:
                await on_event(
                    StreamEvent(
                        event="text_delta",
                        data={
                            "text": text,
                            "round": state.request_steps,
                        },
                    )
                )

        async def save() -> None:
            """传递状态副本，防止回调持有并修改 Loop 正在使用的对象。"""
            state.checkpoint.version += 1
            state.checkpoint.id = uuid4().hex
            state.checkpoint.updated_at = time.time()
            if checkpoint is not None:
                await checkpoint(state.model_copy(deep=True))

        async def stop(reason: str, text: str, *, waiting: bool = False) -> AgentResult:
            """先保存暂停状态，再返回提示；不伪造 assistant 消息补齐待执行工具。"""
            state.status = "waiting_user" if waiting else "paused"
            state.reason = reason
            state.checkpoint.phase = state.status
            await recorder.event(
                "task.paused",
                parent_id=round_span_id,
                attributes={"reason": reason, "status": state.status},
            )
            await recorder.finish_span(round_handle, status=state.status, reason=reason)
            await save()
            return self._result(state, text, reason)

        await save()
        try:
            if state.operations:
                round_handle = await recorder.start_span("loop.resume")
                round_span_id = round_handle.id
            while True:
                # ---- 1. 完成已持久化的工具调用，成功结果不重复执行 ----
                for operation in state.operations:
                    if operation.status == "done":
                        continue
                    if state.request_tokens >= self.request_max_tokens:
                        raise BudgetStop("token_budget_exhausted")
                    tool = self.tools.get(operation.call.name)
                    recovering = operation.status == "unknown"
                    cached_result = (
                        await self.result_store.get(operation.id, context=run_context)
                        if self.result_store is not None
                        else None
                    )
                    if (
                        recovering
                        and cached_result is None
                        and (tool is None or not tool.retry_safe)
                    ):
                        return await stop(
                            "unknown_tool_result",
                            "工具结果未知，请先核实外部状态；本次没有重放该写操作。",
                            waiting=True,
                        )
                    # 先标记可能产生副作用，再进入 handler；中断后不得把该操作当成未执行。
                    operation.status = "unknown"
                    state.checkpoint.phase = "tools"
                    await save()
                    call_context = run_context.model_copy(
                        update={
                            "operation_id": operation.id,
                            "tool_use_id": operation.call.id,
                            "recovering": recovering,
                        }
                    )
                    if cached_result is not None:
                        await recorder.event(
                            "tool.result_reused",
                            parent_id=round_span_id,
                            attributes={
                                "tool": operation.call.name,
                                "operation_id": operation.id,
                                "tool_use_id": operation.call.id,
                            },
                        )
                        result = cached_result
                        duration_ms = 0
                    else:
                        if on_event is not None:
                            await on_event(tool_use_event(operation.call.name, operation.call.id))
                        async with recorder.span(
                            "tool." + operation.call.name,
                            parent_id=round_span_id,
                            input=operation.call.input,
                            tool_use_id=operation.call.id,
                            operation_id=operation.id,
                        ) as tool_span:
                            started = asyncio.get_running_loop().time()
                            result = await self.runner.execute(
                                operation.call, timeout=self.tool_timeout, context=call_context
                            )
                            duration_ms = (asyncio.get_running_loop().time() - started) * 1000
                            tool_payload = json.loads(result["content"])
                            result_bytes = len(result["content"].encode())
                            tool_span.output(
                                {
                                    "is_error": result.get("is_error", False),
                                    "size_bytes": result_bytes,
                                    "result": tool_payload
                                    if result_bytes <= self.result_policy.inline_max_bytes
                                    else "[large result omitted]",
                                },
                                error_code=tool_payload.get("error", {}).get("code"),
                            )
                    logger.info(
                        "tool_result tool=%s call_id=%s duration_ms=%.2f is_error=%s reused=%s",
                        operation.call.name,
                        operation.call.id,
                        duration_ms,
                        result.get("is_error", False),
                        cached_result is not None,
                    )
                    payload = json.loads(result["content"])
                    error_code = payload.get("error", {}).get("code")
                    read_only = tool is not None and (
                        tool.read_only
                        or operation.call.input.get("action") in tool.read_only_actions
                    )
                    # 写操作超时或异常不能证明事务未提交，保留 unknown 等待核实。
                    if not read_only and error_code in {"TOOL_TIMEOUT", "TOOL_ERROR"}:
                        if on_event is not None:
                            await on_event(
                                tool_result_event(
                                    operation.call.name,
                                    operation.call.id,
                                    is_error=True,
                                    duration_ms=duration_ms,
                                    unknown=True,
                                )
                            )
                        return await stop(
                            "unknown_tool_result",
                            "写操作结果不确定，已保存检查点，继续前需要核实状态。",
                            waiting=True,
                        )
                    # 先放入状态，外置存储失败时仍能保留已执行结果，避免盲目重放写操作。
                    operation.result, operation.status = result, "done"
                    if cached_result is None:
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
                    if (
                        len(result["content"].encode()) > self.result_policy.inline_max_bytes
                        and self.result_store is not None
                        and operation.call.name != "read_tool_result"
                    ):
                        await self.result_store.put(operation.id, result, context=run_context)
                        await recorder.event(
                            "tool.result_persisted",
                            parent_id=round_span_id,
                            attributes={"ref": operation.id},
                        )
                    if on_event is not None:
                        await on_event(
                            tool_result_event(
                                operation.call.name,
                                operation.call.id,
                                is_error=result.get("is_error", False),
                                duration_ms=duration_ms,
                                reused=cached_result is not None,
                            )
                        )
                    # 引用丢失时交还用户决定，避免模型紧接着重新调用原写工具。
                    if operation.call.name == "read_tool_result" and error_code == "NOT_FOUND":
                        return await stop(
                            "result_unavailable",
                            "保存的结果不可用，已暂停；未重新执行原工具，请确认后续处理方式。",
                            waiting=True,
                        )
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
                    await self._collect_results(state, run_context)
                    await save()
                await recorder.finish_span(round_handle)
                round_handle, round_span_id = None, None
                # 用户补充信息放在工具结果之后，保持 assistant/tool_result 的紧邻关系。
                if state.resume_note:
                    message = Message(role="user", content=state.resume_note)
                    state.messages.append(message)
                    state.task_messages.append(message.model_copy(deep=True))
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
                        phase="reflection",
                        trace=recorder,
                        parent_span_id=round_span_id,
                    )
                    record.reflected = True
                    state.reflection_pending.pop(0)
                    try:
                        reflection = Reflection.model_validate_json(turn.text)
                        record.reflection = reflection.model_dump_json()
                    except ValueError:
                        record.reflection = "复盘格式不合法；核对参数约束，缺少事实时询问用户。"
                        reflection = None
                    self._apply_reflection(state, key)
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
                async with recorder.span(
                    "context.prepare",
                    input={
                        "source_message_count": len(state.messages),
                        "summary_version": state.context_state.version,
                    },
                ) as context_span:
                    try:
                        view = await self._prepare_context(state, save, recorder, context_span.id)
                    except BudgetStop as exc:
                        context_span.output(
                            {"reason": exc.reason}, status="paused", stop_reason=exc.reason
                        )
                        raise
                    context_span.output(
                        {
                            "message_count": len(view),
                            "summary_version": state.context_state.version,
                            "summary_until_sequence": state.context_state.end,
                        }
                    )
                # 模型决策与其触发的工具执行是同一轮的兄弟步骤；轮次父节点显式保留。
                round_handle = await recorder.start_span(
                    "loop.round", attributes={"round": state.request_steps + 1}
                )
                round_span_id = round_handle.id
                turn = await self._model(
                    state,
                    view,
                    self.tools.specs(),
                    self.system,
                    save,
                    trace=recorder,
                    parent_span_id=round_span_id,
                    on_text_delta=emit_text if on_event is not None else None,
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
                    for m in state.task_messages
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
                state.task_messages.append(
                    Message(role="assistant", content=turn.content).model_copy(deep=True)
                )
                state.operations = [Operation(call=call) for call in calls]
                if not calls:
                    state.status, state.reason = "completed", turn.stop_reason
                    state.checkpoint.phase = "completed"
                    await save()
                    await recorder.event("task.completed", parent_id=round_span_id)
                    await recorder.finish_span(round_handle)
                    return self._result(state, turn.text, turn.stop_reason)
                await save()
        except BudgetStop as exc:
            return await stop(exc.reason, "本次请求执行预算已用尽，进度已保存，可以继续。")
        except asyncio.CancelledError:
            await recorder.finish_span(round_handle, status="cancelled", reason="request_cancelled")
            state.status, state.reason = "paused", "cancelled"
            state.checkpoint.phase = "paused"
            await save()
            raise
        except Exception:
            await recorder.finish_span(round_handle, status="error", reason="execution_error")
            state.status, state.reason = "paused", "execution_error"
            state.checkpoint.phase = "paused"
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
