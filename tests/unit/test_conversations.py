"""基础 session、history、context 及 HTTP 接口测试。"""

import asyncio
from collections import deque

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.agent_loop import AgentLoop
from server.app import create_app
from server.cache import SessionCache
from server.config import get_settings
from server.context import build_context
from server.conversations import ConversationService
from server.llm import LLMTurn
from server.store.base import Base
from server.store.sessions import ConversationError, SessionStore
from server.store.todos import TodoStore
from server.tools import create_default_tools


class TestModel:
    __test__ = False

    def __init__(self):
        self.requests = []
        self.turns = deque()

    async def run_turn(self, messages, *, tools, system):
        self.requests.append([m.model_copy(deep=True) for m in messages])
        return (
            self.turns.popleft()
            if self.turns
            else LLMTurn(content=[{"type": "text", "text": "完成"}], stop_reason="end_turn")
        )


@pytest.fixture
async def conversation(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    store = SessionStore(async_sessionmaker(engine, expire_on_commit=False))
    redis = FakeRedis(decode_responses=True)
    cache = SessionCache(redis, ttl_seconds=300)
    model = TestModel()
    loop = AgentLoop(model, create_default_tools(todo_store=TodoStore(store.session_factory)))
    service = ConversationService(store, cache, loop, timeout_seconds=120, recent_turns=2)
    yield service, model, redis
    await redis.aclose()
    await engine.dispose()


async def test_session_and_history_isolation(conversation):
    service, model, _ = conversation
    first = await service.store.create("a", "天气")
    second = await service.store.create("a", "周报")
    await service.chat("a", first.id, "我叫小明")
    await service.chat("a", first.id, "我叫什么")
    assert model.requests[-1][0].content == "我叫小明"
    await service.chat("a", second.id, "写周报")
    assert len(model.requests[-1]) == 1
    assert len(await service.store.list_sessions("a", limit=10, offset=0)) == 2
    with pytest.raises(ConversationError):
        await service.history("b", first.id)


async def test_cache_and_service_recreation(conversation):
    service, model, redis = conversation
    info = await service.store.create("a", "窗口")
    await service.chat("a", info.id, "之前的状态")
    await service.history("a", info.id)
    await redis.flushdb()
    restored = ConversationService(
        SessionStore(service.store.session_factory),
        service.cache,
        service.loop,
        timeout_seconds=120,
        recent_turns=2,
    )
    await restored.chat("a", info.id, "继续")
    assert model.requests[-1][0].content == "之前的状态"


async def test_redis_outage_does_not_prevent_chat(conversation, monkeypatch):
    service, _, redis = conversation

    async def fail(*args, **kwargs):
        raise RedisConnectionError()

    monkeypatch.setattr(redis, "get", fail)
    monkeypatch.setattr(redis, "set", fail)
    info = await service.store.create("a", "窗口")
    await service.chat("a", info.id, "你好")
    assert len(await service.history("a", info.id)) == 2


async def test_context_keeps_complete_tool_rounds(conversation):
    service, model, _ = conversation
    info = await service.store.create("a", "窗口")
    model.turns.extend(
        [
            LLMTurn(
                content=[
                    {
                        "type": "tool_use",
                        "id": "call1",
                        "name": "todo",
                        "input": {"action": "create", "title": "带伞"},
                    }
                ],
                stop_reason="tool_use",
            ),
            LLMTurn(content=[{"type": "text", "text": "已保存"}], stop_reason="end_turn"),
        ]
    )
    await service.chat("a", info.id, "记待办带伞")
    history = await service.history("a", info.id)
    assert len(history) == 4
    assert history[2].content[0]["tool_use_id"] == "call1"
    items, _ = await TodoStore(service.store.session_factory).list_items(
        user_id="a", session_id=info.id, status=None, limit=10, offset=0
    )
    assert items[0].title == "带伞"
    selected = build_context(history, recent_turns=1)
    assert len(selected) == 4
    await service.chat("a", info.id, "下一轮")
    await service.chat("a", info.id, "第三轮")
    await service.chat("a", info.id, "第四轮")
    assert model.requests[-1][0].content == "下一轮"
    assert len(await service.history("a", info.id)) == 10


async def test_failure_never_saves_unpaired_tool_calls(conversation):
    service, model, _ = conversation
    info = await service.store.create("a", "窗口")

    class FailedModel:
        async def run_turn(self, *args, **kwargs):
            raise TimeoutError()

    service.loop.llm = FailedModel()
    with pytest.raises(ConversationError):
        await service.chat("a", info.id, "失败请求")
    assert await service.history("a", info.id) == []
    service.loop.llm = model
    await service.chat("a", info.id, "新请求")


async def test_same_window_busy(conversation):
    service, _, _ = conversation
    info = await service.store.create("a", "窗口")
    started, release = asyncio.Event(), asyncio.Event()

    class BlockingModel:
        async def run_turn(self, *args, **kwargs):
            started.set()
            await release.wait()
            return LLMTurn(content=[{"type": "text", "text": "完成"}], stop_reason="end_turn")

    service.loop.llm = BlockingModel()
    task = asyncio.create_task(service.chat("a", info.id, "等待"))
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        with pytest.raises(ConversationError):
            await service.chat("a", info.id, "并发")
    finally:
        release.set()
        await task


async def test_http_history_pagination_and_private_blocks(conversation):
    service, model, _ = conversation
    app = create_app(get_settings())
    app.state.conversations = service
    headers = {"X-User-ID": "a"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/sessions", json={"title": "窗口"}, headers=headers)
        session_id = response.json()["id"]
        model.turns.append(
            LLMTurn(
                content=[
                    {"type": "thinking", "thinking": "private", "signature": "secret"},
                    {"type": "text", "text": "你好"},
                ],
                stop_reason="end_turn",
            )
        )
        assert (
            await client.post(
                f"/sessions/{session_id}/chat", json={"message": "你好"}, headers=headers
            )
        ).status_code == 200
        page = await client.get(f"/sessions/{session_id}/messages?limit=1", headers=headers)
        assert page.json()["has_more"] is True
        second = await client.get(f"/sessions/{session_id}/messages?after=1", headers=headers)
        assert "private" not in second.text and "secret" not in second.text
        assert (
            await client.get(f"/sessions/{session_id}", headers={"X-User-ID": "b"})
        ).status_code == 404
