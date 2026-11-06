"""Redis 客户端和历史缓存，数据库中的消息数量作为缓存水位。"""

import logging
from hashlib import sha256

from pydantic import TypeAdapter, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from server.config import Settings
from server.llm import Message

logger = logging.getLogger(__name__)
HISTORY_ADAPTER = TypeAdapter(list[Message])


class SessionCache:
    """历史只读缓存；Redis 不可用时回源 MySQL。"""

    def __init__(self, redis: Redis | None, *, ttl_seconds: int):
        self.redis = redis
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def scope_key(user_id: str, session_id: str) -> str:
        digest = sha256(f"{len(user_id)}:{user_id}:{session_id}".encode()).hexdigest()
        return f"mini-agent:session:{digest}"

    async def get_history(
        self, user_id: str, session_id: str, version: int
    ) -> list[Message] | None:
        if self.redis is None:
            return None
        try:
            data = await self.redis.get(f"{self.scope_key(user_id, session_id)}:history:v{version}")
            return HISTORY_ADAPTER.validate_json(data) if data is not None else None
        except (RedisError, ValidationError):
            logger.warning("session_cache_read_failed")
            return None

    async def set_history(
        self,
        user_id: str,
        session_id: str,
        version: int,
        history: list[Message],
    ) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.set(
                f"{self.scope_key(user_id, session_id)}:history:v{version}",
                HISTORY_ADAPTER.dump_json(history),
                ex=self.ttl_seconds,
            )
        except RedisError:
            logger.warning("session_cache_write_failed")


def create_redis(settings: Settings) -> Redis:
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password.get_secret_value() or None,
        ssl=settings.redis_ssl,
        socket_connect_timeout=settings.redis_timeout,
        socket_timeout=settings.redis_timeout,
        decode_responses=True,
    )
