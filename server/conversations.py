"""基础会话服务：读取历史 → 组装 context → 执行 Loop → 保存整轮消息。

MySQL 保存完整历史，Redis 仅缓存读取结果。单进程内同窗口串行执行；
不提供分布式运行调度、请求重放或自动重试写工具。
"""

import asyncio
import logging

from pydantic import BaseModel

from server.agent_loop import AgentLoop, ToolTrace
from server.cache import SessionCache
from server.context import build_context
from server.llm import Message
from server.store.sessions import ConversationCode, ConversationError, SessionStore
from skills._shared.context import ToolContext

logger = logging.getLogger(__name__)


class ChatResponse(BaseModel):
    session_id: str
    turn_id: str
    text: str
    stop_reason: str
    rounds: int
    tool_traces: list[ToolTrace]


class ConversationService:
    def __init__(
        self,
        store: SessionStore,
        cache: SessionCache,
        loop: AgentLoop,
        *,
        timeout_seconds: float,
        recent_turns: int,
    ):
        self.store = store
        self.cache = cache
        self.loop = loop
        self.timeout_seconds = timeout_seconds
        self.recent_turns = recent_turns
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

    async def chat(self, user_id: str, session_id: str, text: str) -> ChatResponse:
        await self.store.get(user_id, session_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise ConversationError(ConversationCode.SESSION_BUSY, "会话正在处理请求。")
        async with lock:
            try:
                return await self._chat(user_id, session_id, text)
            finally:
                self._locks.pop(session_id, None)

    async def _chat(self, user_id: str, session_id: str, text: str) -> ChatResponse:
        info = await self.store.get(user_id, session_id)
        history = await self.history(user_id, session_id)
        context = build_context(history, recent_turns=self.recent_turns)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await self.loop.run(
                    text,
                    history=context,
                    context=ToolContext(user_id=user_id, session_id=session_id),
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
                code, "处理失败，请先检查工具状态，不要自动重复写操作。"
            ) from exc
        messages = result.messages[len(context) :]
        if result.stop_reason in {"max_rounds", "max_tokens"}:
            messages.append(
                Message(role="assistant", content=[{"type": "text", "text": result.text}])
            )
        await self.store.save_turn(
            user_id,
            session_id,
            result.run_id,
            messages,
            expected_version=info.version,
        )
        return ChatResponse(
            session_id=session_id,
            turn_id=result.run_id,
            text=result.text,
            stop_reason=result.stop_reason,
            rounds=result.rounds,
            tool_traces=result.tool_traces,
        )
