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
from server.tools import Tool, create_default_tools
from skills.calculator.handler import CalculatorArgs


class TestModel:
    __test__ = False

    def __init__(self):
        self.requests = []
        self.turns = deque()

    async def run_turn(self, messages, *, tools, system, max_tokens=None):
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
    await service.chat("a", info.id, "新请求", action="continue")


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


async def test_todo_write_then_crash_resumes_without_duplicate(conversation, monkeypatch):
    service, model, _ = conversation
    info = await service.store.create("a", "恢复任务")
    model.turns.extend(
        [
            LLMTurn(
                content=[
                    {
                        "type": "tool_use",
                        "id": "todo-create",
                        "name": "todo",
                        "input": {"action": "create", "title": "带伞"},
                    }
                ],
                stop_reason="tool_use",
            ),
            LLMTurn(content=[{"type": "text", "text": "已记录带伞"}], stop_reason="end_turn"),
        ]
    )
    original = TodoStore.create

    async def write_then_crash(self, **kwargs):
        await original(self, **kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(TodoStore, "create", write_then_crash)
    with pytest.raises(asyncio.CancelledError):
        await service.chat("a", info.id, "记待办带伞")
    checkpoint = await service.store.load_task("a", info.id)
    operation_id = checkpoint["operations"][0]["id"]
    assert checkpoint["operations"][0]["status"] == "unknown"
    with pytest.raises(ConversationError):
        await service.chat("a", info.id, "新任务不能覆盖")
    monkeypatch.setattr(TodoStore, "create", original)
    restored = ConversationService(
        service.store, service.cache, service.loop, timeout_seconds=120, recent_turns=2
    )
    result = await restored.chat("a", info.id, "继续", action="continue")
    assert result.task_status == "completed"
    assert result.turn_id == checkpoint["task_id"]
    items, _ = await TodoStore(service.store.session_factory).list_items(
        user_id="a", session_id=info.id, status=None, limit=10, offset=0
    )
    assert len(items) == 1
    assert operation_id
    saved = await service.store.load_task("a", info.id)
    assert saved["history_saved"] is True
    history = await restored.history("a", info.id)
    assert history[1].content[0]["id"] == "todo-create"
    assert history[2].content[0]["tool_use_id"] == "todo-create"


async def test_cancel_pending_task_allows_new_goal(conversation):
    service, model, _ = conversation
    info = await service.store.create("a", "窗口")
    service.loop.request_max_tokens = 1
    result = await service.chat("a", info.id, "预算不足")
    assert result.task_status == "paused"
    cancelled = await service.chat("a", info.id, "取消", action="cancel")
    assert cancelled.task_status == "cancelled"
    service.loop.request_max_tokens = 64000
    result = await service.chat("a", info.id, "新目标")
    assert result.task_status == "completed"


async def test_completed_checkpoint_is_committed_once_after_service_crash(
    conversation, monkeypatch
):
    service, model, _ = conversation
    info = await service.store.create("a", "窗口")
    original = service.store.save_turn

    async def fail(*args, **kwargs):
        raise RuntimeError("simulated database interruption")

    monkeypatch.setattr(service.store, "save_turn", fail)
    with pytest.raises(RuntimeError):
        await service.chat("a", info.id, "旧目标")
    assert (await service.store.load_task("a", info.id))["status"] == "completed"
    monkeypatch.setattr(service.store, "save_turn", original)
    await service.chat("a", info.id, "新目标")
    assert len(model.requests) == 2
    assert [
        message.content
        for message in await service.history("a", info.id)
        if isinstance(message.content, str)
    ] == ["旧目标", "新目标"]


async def test_continue_completed_task_replays_answer_without_model_call(conversation):
    service, model, _ = conversation
    info = await service.store.create("a", "窗口")
    first = await service.chat("a", info.id, "目标")
    count = len(model.requests)
    replay = await service.chat("a", info.id, "继续", action="continue")
    assert replay.text == first.text
    assert replay.turn_id == first.turn_id
    assert replay.request_tokens == 0
    assert len(model.requests) == count


async def test_unknown_write_requires_explicit_confirmation_before_retry(conversation):
    service, model, _ = conversation
    info = await service.store.create("a", "未知写操作")
    attempts = []

    async def write(arguments, context):
        attempts.append(context.operation_id)
        if len(attempts) == 1:
            # 模拟开始调用后中断；调用方无法从检查点判断是否产生副作用。
            raise asyncio.CancelledError()
        return {"written": True}

    service.loop.tools.register(
        Tool("unsafe_write", "测试未知副作用的写工具", CalculatorArgs, write)
    )
    model.turns.append(
        LLMTurn(
            content=[
                {
                    "type": "tool_use",
                    "id": "write",
                    "name": "unsafe_write",
                    "input": {"operation": "add", "left": 1, "right": 2},
                }
            ],
            stop_reason="tool_use",
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await service.chat("a", info.id, "执行写操作")
    operation_id = (await service.task("a", info.id))["operations"][0]["operation_id"]
    paused = await service.chat("a", info.id, "继续", action="continue")
    assert paused.task_status == "waiting_user"
    assert len(attempts) == 1
    with pytest.raises(ConversationError):
        await service.chat(
            "a", info.id, "继续", action="continue", confirmed_not_executed=["wrong-id"]
        )
    with pytest.raises(ConversationError):
        await service.chat(
            "b", info.id, "继续", action="continue", confirmed_not_executed=[operation_id]
        )
    completed = await service.chat(
        "a",
        info.id,
        "已核实尚未执行",
        action="continue",
        confirmed_not_executed=[operation_id],
    )
    assert completed.task_status == "completed"
    assert attempts == [operation_id, operation_id]
