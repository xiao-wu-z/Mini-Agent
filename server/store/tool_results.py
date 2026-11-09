"""工具结果原文存储与 Redis 读取缓存。

链路：
    工具执行 → 按稳定 operation_id 保存完整结果 → 发布 ref
    → 按用户与会话校验读取 → Redis 未命中时回源 MySQL。

边界：
    结果独立于当前任务检查点，任务取消、完成或创建新任务不会使旧 ref 失效。
    写入失败向上传播，不发布未持久化引用；缓存失败仅降级，不重新执行原工具。
"""

import json
import logging
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import JSON, ForeignKey, Index, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from server.cache import SessionCache
from server.config import ToolResultPolicy
from server.store.base import Base
from server.store.sessions import ENTITY_ID_LENGTH, MYSQL_OPTIONS
from skills._shared.context import MAX_IDENTITY_LENGTH, ToolContext

logger = logging.getLogger(__name__)

# ============================================================================
# 独立结果原文；会话与消息仍使用原有两张表
# ============================================================================


class ToolResultModel(Base):
    """以稳定操作 ID 索引不可变结果；ref 不包含凭据、数据库位置或 Redis key。"""

    __tablename__ = "agent_tool_result"
    __table_args__ = (Index("ix_agent_tool_result_scope", "user_id", "session_id"), MYSQL_OPTIONS)

    id: Mapped[str] = mapped_column(String(ENTITY_ID_LENGTH), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(MAX_IDENTITY_LENGTH), nullable=False)
    session_id: Mapped[str] = mapped_column(
        String(ENTITY_ID_LENGTH), ForeignKey("agent_session.id"), nullable=False
    )
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ToolResultStore:
    """保存与读取完整结果；缓存键同时绑定用户、会话和引用。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        redis: Redis | None = None,
        cache_ttl_seconds: int | None = None,
    ):
        self.session_factory = session_factory
        self.redis = redis
        self.cache_ttl_seconds = (
            ToolResultPolicy().cache_ttl_seconds if cache_ttl_seconds is None else cache_ttl_seconds
        )

    async def put(self, ref: str, result: dict, *, context: ToolContext) -> None:
        """持久化原文后填充缓存；重复操作仅允许写入相同内容。

        Raises:
            ValueError: 缺少可信身份，或同一操作标识被用于不同结果。
        """
        if not context.user_id or not context.session_id:
            raise ValueError("Result persistence requires trusted user and session")
        try:
            async with self.session_factory() as session, session.begin():
                session.add(
                    ToolResultModel(
                        id=ref,
                        user_id=context.user_id,
                        session_id=context.session_id,
                        result=result,
                    )
                )
        except IntegrityError:
            existing = await self.get(ref, context=context, use_cache=False)
            if existing != result:
                raise ValueError("Result ref already belongs to a different result") from None
        await self._cache(ref, result, context)

    async def get(self, ref: str, *, context: ToolContext, use_cache: bool = True) -> dict | None:
        """按身份读取结果；不存在或不属于当前窗口时返回 None，绝不重跑原工具。"""
        if not context.user_id or not context.session_id:
            return None
        key = f"{SessionCache.scope_key(context.user_id, context.session_id)}:result:{ref}"
        if use_cache and self.redis is not None:
            try:
                cached = await self.redis.get(key)
                if cached is not None:
                    result = json.loads(cached)
                    if isinstance(result, dict) and isinstance(result.get("content"), str):
                        return result
            except (RedisError, ValueError, TypeError):
                logger.warning("tool_result_cache_read_failed")
        async with self.session_factory() as session:
            row = await session.scalar(
                select(ToolResultModel).where(
                    ToolResultModel.id == ref,
                    ToolResultModel.user_id == context.user_id,
                    ToolResultModel.session_id == context.session_id,
                )
            )
            result = row.result if row is not None else None
        if result is not None:
            await self._cache(ref, result, context)
        return result

    async def _cache(self, ref: str, result: dict, context: ToolContext) -> None:
        """缓存写入失败不影响已持久化的结果与后续恢复。"""
        if self.redis is None:
            return
        key = f"{SessionCache.scope_key(context.user_id, context.session_id)}:result:{ref}"
        try:
            await self.redis.set(
                key, json.dumps(result, ensure_ascii=False), ex=self.cache_ttl_seconds
            )
        except RedisError:
            logger.warning("tool_result_cache_write_failed")
