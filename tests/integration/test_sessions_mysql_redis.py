"""真实 MySQL/Redis 的基础会话与历史恢复验证。"""

import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.agent_loop import AgentLoop
from server.cache import SessionCache
from server.conversations import ConversationService
from server.llm import LLMTurn
from server.store.sessions import MessageModel, SessionModel, SessionStore
from server.tools import create_default_tools

MYSQL_VARIABLE = "MINI_AGENT_TEST_MYSQL_URL"
REDIS_VARIABLE = "MINI_AGENT_TEST_REDIS_URL"
pytestmark = pytest.mark.skipif(
    not os.environ.get(MYSQL_VARIABLE) or not os.environ.get(REDIS_VARIABLE),
    reason="Dedicated integration URLs required",
)


async def test_mysql_redis_session_history():
    url = make_url(os.environ[MYSQL_VARIABLE])
    assert url.database.endswith("_test")
    assert make_url(os.environ[REDIS_VARIABLE]).database == "1"
    engine = create_async_engine(url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(os.environ[REDIS_VARIABLE], decode_responses=True)
    cache = SessionCache(redis, ttl_seconds=300)
    store = SessionStore(factory)
    user_id = f"sessions-{uuid4().hex}"
    ids = []

    class Model:
        def __init__(self):
            self.requests = []

        async def run_turn(self, messages, **kwargs):
            self.requests.append([message.model_copy(deep=True) for message in messages])
            return LLMTurn(content=[{"type": "text", "text": "完成"}], stop_reason="end_turn")

    model = Model()
    loop = AgentLoop(model, create_default_tools())
    try:
        first = await store.create(user_id, "天气")
        second = await store.create(user_id, "周报")
        ids.extend([first.id, second.id])
        service = ConversationService(store, cache, loop, timeout_seconds=120, recent_turns=12)
        await service.chat(user_id, first.id, "我叫小明")
        restored = ConversationService(
            SessionStore(factory),
            SessionCache(redis, ttl_seconds=300),
            loop,
            timeout_seconds=120,
            recent_turns=12,
        )
        await restored.chat(user_id, first.id, "我叫什么")
        assert model.requests[-1][0].content == "我叫小明"
        await restored.chat(user_id, second.id, "写周报")
        assert len(model.requests[-1]) == 1
        assert len(await restored.history(user_id, first.id)) == 4
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(MessageModel).where(MessageModel.session_id.in_(ids)))
            await session.execute(delete(SessionModel).where(SessionModel.id.in_(ids)))
        for session_id in ids:
            async for key in redis.scan_iter(match=f"{cache.scope_key(user_id, session_id)}:*"):
                await redis.delete(key)
        await redis.aclose()
        await engine.dispose()
