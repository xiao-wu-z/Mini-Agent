"""Anthropic tool-use 多轮循环。

链路：
    用户输入 + 历史消息
      → LLM.run_turn(messages, tools, system)
      → 保存 assistant 完整 content blocks
      → 执行 tool_use，将 tool_result 作为 user 消息回灌
      → 继续调用 LLM，直到返回最终文本或达到轮次上限。

边界：
    - Loop 只编排模型和工具，不负责 Session 持久化。
    - 每次运行独立复制历史消息，不修改调用方传入的上下文。
    - 模型响应被截断时不执行工具；工具异常由 SkillRunner 封装后回灌。
"""

import logging
from time import monotonic
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from server.llm import LLMTurn, Message, ToolUse
from server.skill_runner import SkillRunner
from server.tools import ToolRegistry

logger = logging.getLogger(__name__)


# ============================================================================
# 模型接口与运行结果
# ============================================================================


class TurnClient(Protocol):
    """Loop 所需的最小模型接口，便于替换真实客户端和测试客户端。"""

    async def run_turn(
        self, messages: list[Message], *, tools: list[dict[str, Any]], system: str
    ) -> LLMTurn: ...


class ToolTrace(BaseModel):
    """单次工具执行记录；参数和结果保留在消息中，trace 只记录执行状态。"""

    round: int
    tool_use_id: str
    name: str
    is_error: bool
    duration_ms: float


class AgentResult(BaseModel):
    """一次用户请求的运行结果，messages 可供调用方持久化并用于下一次追问。"""

    run_id: str
    text: str
    stop_reason: Literal["end_turn", "stop_sequence", "refusal", "max_tokens", "max_rounds"]
    rounds: int
    messages: list[Message]
    tool_traces: list[ToolTrace] = Field(default_factory=list)


# ============================================================================
# Agent 主循环
# ============================================================================


class AgentLoop:
    """使用原生 tool_use / tool_result 协议编排模型和工具。

    同轮工具顺序执行，全部结果在下一条 user 消息中集中回灌。
    实例只保存依赖和执行配置；消息、轮次与 trace 均属于单次 run。
    """

    def __init__(
        self,
        llm: TurnClient,
        tools: ToolRegistry,
        *,
        system: str = "You are a helpful assistant. Use tools when needed; never invent results.",
        max_rounds: int = 8,
        tool_timeout: float = 10,
    ):
        """初始化运行依赖与执行边界。

        Args:
            llm: 实现 run_turn 的模型客户端。
            tools: 已注册工具；同时提供 Schema 和执行函数。
            system: 单独传给 Anthropic Messages API 的系统提示词。
            max_rounds: 一次 run 最多发起的模型请求数，包含最终回答轮。
            tool_timeout: 单次工具调用的超时秒数。

        Raises:
            ValueError: 轮次上限或工具超时不是正数。
        """
        if max_rounds < 1 or tool_timeout <= 0:
            raise ValueError("max_rounds and tool_timeout must be positive")
        self.llm = llm
        self.tools = tools
        self.runner = SkillRunner(tools)
        self.system = system
        self.max_rounds = max_rounds
        self.tool_timeout = tool_timeout

    async def run(self, user_input: str, *, history: list[Message] | None = None) -> AgentResult:
        """处理一次用户输入，执行工具后继续调用模型。

        Args:
            user_input: 当前用户输入，不允许为空或仅包含空白。
            history: 当前会话已有消息；调用前应已配对完成工具调用与结果。

        Returns:
            最终文本、停止原因、完整消息以及工具执行 trace。
            max_tokens 和 max_rounds 表示运行未正常完成，由调用方决定如何提示。

        Raises:
            ValueError: 输入为空、工具块不合法、调用 ID 重复或停止原因不受支持。
            httpx.HTTPError: 模型请求失败，由调用方处理重试或错误响应。
            asyncio.CancelledError: 运行被取消，取消信号原样传播。
        """
        if not user_input.strip():
            raise ValueError("User input must not be empty")
        # 每次调用独立持有消息，复用同一个 Loop 实例也不会共享会话状态。
        messages = [message.model_copy(deep=True) for message in (history or [])]
        messages.append(Message(role="user", content=user_input))
        run_id = uuid4().hex
        traces: list[ToolTrace] = []
        rounds = 0
        specs = self.tools.specs()
        seen_call_ids: set[str] = set()

        # ---- 1. 调用模型：轮次预算包含工具修正与最终回答 ----
        while rounds < self.max_rounds:
            rounds += 1
            logger.info("llm_turn run_id=%s round=%s", run_id, rounds)
            turn = await self.llm.run_turn(messages, tools=specs, system=self.system)
            # max_tokens 可能截断工具参数，不能执行其中看似完整的部分调用。
            if turn.stop_reason == "max_tokens":
                return AgentResult(
                    run_id=run_id,
                    text=turn.text,
                    stop_reason="max_tokens",
                    rounds=rounds,
                    messages=messages,
                    tool_traces=traces,
                )

            # ---- 2. 校验整轮工具块，避免执行到一半才发现协议错误 ----
            calls = [
                ToolUse.model_validate(block)
                for block in turn.content
                if block.get("type") == "tool_use"
            ]
            if turn.stop_reason not in {"tool_use", "end_turn", "stop_sequence", "refusal"}:
                raise ValueError(f"Unsupported LLM stop_reason: {turn.stop_reason}")
            if len({call.id for call in calls}) != len(calls) or any(
                call.id in seen_call_ids for call in calls
            ):
                raise ValueError("LLM returned duplicate tool_use IDs")
            if turn.stop_reason == "tool_use" and not calls:
                raise ValueError("LLM stopped for tool_use without any tool_use blocks")
            if not calls and turn.stop_reason not in {"end_turn", "stop_sequence", "refusal"}:
                raise ValueError(f"Unsupported LLM stop_reason: {turn.stop_reason}")
            seen_call_ids.update(call.id for call in calls)

            # ---- 3. 回写 assistant；thinking/signature 等字段必须完整保留 ----
            messages.append(Message(role="assistant", content=turn.content))
            if not calls:
                logger.info("run_completed run_id=%s rounds=%s", run_id, rounds)
                return AgentResult(
                    run_id=run_id,
                    text=turn.text,
                    stop_reason=turn.stop_reason,
                    rounds=rounds,
                    messages=messages,
                    tool_traces=traces,
                )

            # ---- 4. 顺序执行本轮工具，并记录调用 ID、耗时与错误状态 ----
            results = []
            for call in calls:
                started = monotonic()
                result = await self.runner.execute(call, timeout=self.tool_timeout)
                trace = ToolTrace(
                    round=rounds,
                    tool_use_id=call.id,
                    name=call.name,
                    is_error=result.get("is_error", False),
                    duration_ms=round((monotonic() - started) * 1000, 3),
                )
                traces.append(trace)
                logger.info(
                    "tool_completed run_id=%s round=%s tool=%s call_id=%s "
                    "is_error=%s duration_ms=%s",
                    run_id,
                    rounds,
                    call.name,
                    call.id,
                    trace.is_error,
                    trace.duration_ms,
                )
                results.append(result)

            # ---- 5. 集中回灌结果，保持 tool_use_id 与上一条 assistant 一一配对 ----
            messages.append(Message(role="user", content=results))

        # 所有已执行工具均已回灌；达到预算后直接结束，不额外请求模型收尾。
        logger.warning("run_limit_reached run_id=%s rounds=%s", run_id, rounds)
        return AgentResult(
            run_id=run_id,
            text="已达到最大执行轮次，任务可能尚未完成。",
            stop_reason="max_rounds",
            rounds=rounds,
            messages=messages,
            tool_traces=traces,
        )
