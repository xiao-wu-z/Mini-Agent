"""真实 MySQL/Redis 的基础会话与历史恢复验证。"""

import asyncio
import os
from collections import deque
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
from server.store.todos import TodoModel, TodoStore
from server.tools import create_default_tools

MYSQL_VARIABLE = "MINI_AGENT_TEST_MYSQL_URL"
REDIS_VARIABLE = "MINI_AGENT_TEST_REDIS_URL"
pytestmark = pytest.mark.skipif(
    not os.environ.get(MYSQL_VARIABLE) or not os.environ.get(REDIS_VARIABLE),
    reason="Dedicated integration URLs required",
)


async def test_mysql_redis_session_history(monkeypatch):
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
        turns = deque(
            [
                LLMTurn(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "todo-call",
                            "name": "todo",
                            "input": {"action": "create", "title": "真实数据库恢复测试"},
                        }
                    ],
                    stop_reason="tool_use",
                ),
                LLMTurn(content=[{"type": "text", "text": "已保存"}], stop_reason="end_turn"),
            ]
        )

        class TaskModel:
            async def run_turn(self, messages, **kwargs):
                return turns.popleft()

        task_loop = AgentLoop(TaskModel(), create_default_tools(todo_store=TodoStore(factory)))
        task_service = ConversationService(
            store, cache, task_loop, timeout_seconds=120, recent_turns=12
        )
        original = TodoStore.create

        async def write_then_crash(self, **kwargs):
            await original(self, **kwargs)
            raise asyncio.CancelledError()

        monkeypatch.setattr(TodoStore, "create", write_then_crash)
        with pytest.raises(asyncio.CancelledError):
            await task_service.chat(user_id, first.id, "新增待办")
        monkeypatch.setattr(TodoStore, "create", original)
        restored_task_service = ConversationService(
            SessionStore(factory), cache, task_loop, timeout_seconds=120, recent_turns=12
        )
        result = await restored_task_service.chat(user_id, first.id, "继续", action="continue")
        assert result.task_status == "completed"
        items, _ = await TodoStore(factory).list_items(
            user_id=user_id, session_id=first.id, status=None, limit=10, offset=0
        )
        assert len(items) == 1
        assert (await store.load_task(user_id, first.id))["history_saved"] is True

    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(TodoModel).where(TodoModel.user_id == user_id))
            await session.execute(delete(MessageModel).where(MessageModel.session_id.in_(ids)))
            await session.execute(delete(SessionModel).where(SessionModel.id.in_(ids)))
        for session_id in ids:
            async for key in redis.scan_iter(match=f"{cache.scope_key(user_id, session_id)}:*"):
                await redis.delete(key)
        await redis.aclose()
        await engine.dispose()
