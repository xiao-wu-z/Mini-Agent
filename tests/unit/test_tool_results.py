"""大结果外置、分页读取、身份隔离和原文回源恢复验证。"""

import asyncio
import json

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.agent_loop import AgentLoop
from server.cache import SessionCache
from server.config import ToolResultPolicy
from server.conversations import ConversationService
from server.llm import LLMTurn, ToolUse
from server.skill_runner import SkillRunner
from server.store.base import Base
from server.store.sessions import SessionStore
from server.store.tool_results import ToolResultStore
from server.store.traces import TraceStore
from server.tools import Tool, create_default_tools
from skills._shared.context import ToolContext
from skills.calculator.handler import CalculatorArgs


def turn(call_id, name, arguments):
    return LLMTurn(
        content=[{"type": "tool_use", "id": call_id, "name": name, "input": arguments}],
        stop_reason="tool_use",
    )


def final():
    return LLMTurn(content=[{"type": "text", "text": "完成"}], stop_reason="end_turn")


@pytest.fixture
async def stores(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'results.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sessions = SessionStore(factory)
    info = await sessions.create("a", "结果窗口")
    redis = FakeRedis(decode_responses=True)
    results = ToolResultStore(factory, redis=redis)
    yield sessions, info, redis, results
    await redis.aclose()
    await engine.dispose()


async def test_large_result_roundtrip_and_paging_never_references_a_reference(stores):
    sessions, info, redis, results = stores
    executions = []
    raw = "事实资料" * 5000

    async def large(arguments, context):
        executions.append(context.operation_id)
        return {"evidence": raw}

    tools = create_default_tools(result_store=results)
    tools.register(Tool("large", "大结果", CalculatorArgs, large, retry_safe=True, read_only=True))

    class Model:
        def __init__(self):
            self.requests = []
            self.ref = None

        async def run_turn(self, messages, **kwargs):
            self.requests.append([m.model_copy(deep=True) for m in messages])
            if len(self.requests) == 1:
                return turn("large-call", "large", {"operation": "add", "left": 1, "right": 2})
            if len(self.requests) > 3:
                return final()
            feedback = messages[-1].content[0]
            if len(self.requests) == 2:
                projection = json.loads(feedback["content"])
                self.ref = projection["ref"]
                assert projection["size_bytes"] > 50000
                assert len(projection["preview"]) == 20
                assert feedback["tool_use_id"] == "large-call"
                await redis.flushdb()  # 发布引用后丢失缓存，仍必须可以继续读取。
                return turn("read-call", "read_tool_result", {"ref": self.ref, "limit": 128})
            page = json.loads(feedback["content"])["data"]
            assert page["ref"] == self.ref and page["next_offset"] == 128
            assert "ok" in page["content"] and page["has_more"]
            assert "preview" not in page
            return final()

    model = Model()
    loop = AgentLoop(
        model,
        tools,
        result_store=results,
        result_policy=ToolResultPolicy(inline_max_bytes=256, preview_chars=20),
    )
    service = ConversationService(
        sessions, SessionCache(redis, ttl_seconds=60), loop, timeout_seconds=120
    )
    reply = await service.chat("a", info.id, "读取资料")
    assert reply.task_status == "completed" and len(executions) == 1
    history = await service.history("a", info.id)
    assert raw in history[2].content[0]["content"]
    assert len(history[4].content[0]["content"]) > 256  # 有界读取不因小外置阈值产生新指针。
    context = ToolContext(user_id="a", session_id=info.id)
    restored = ToolResultStore(sessions.session_factory, redis=redis)
    original = await restored.get(model.ref, context=context)
    assert raw in original["content"]
    assert (await sessions.load_context("a", info.id))["result_views"]
    loop.request_max_tokens = 1
    assert (await service.chat("a", info.id, "先暂停的新任务")).task_status == "paused"
    await service.chat("a", info.id, "取消", action="cancel")
    loop.request_max_tokens = 64000
    await service.chat("a", info.id, "替换当前检查点的新任务")
    await redis.flushdb()
    assert await restored.get(model.ref, context=context) == original


async def test_reference_is_scoped_and_survives_cache_expiry_and_outage(stores, monkeypatch):
    sessions, info, redis, results = stores
    second = await sessions.create("a", "另一个窗口")
    context = ToolContext(user_id="a", session_id=info.id)
    ref = "a" * 32
    raw = {
        "type": "tool_result",
        "tool_use_id": "call",
        "is_error": True,
        "content": "中文原文" * 1000,
    }
    await results.put(ref, raw, context=context)
    assert await results.get(ref, context=ToolContext(user_id="b", session_id=info.id)) is None
    assert await results.get(ref, context=ToolContext(user_id="a", session_id=second.id)) is None
    await redis.flushdb()
    assert await results.get(ref, context=context) == raw

    async def unavailable(*args, **kwargs):
        raise RedisConnectionError()

    monkeypatch.setattr(redis, "get", unavailable)
    monkeypatch.setattr(redis, "set", unavailable)
    restored = ToolResultStore(sessions.session_factory, redis=redis)
    assert await restored.get(ref, context=context) == raw
    runner = SkillRunner(create_default_tools(result_store=restored))
    content = []
    offset = 0
    while offset < len(raw["content"]):
        feedback = await runner.execute(
            ToolUse(id="read", name="read_tool_result", input={"ref": ref, "offset": offset}),
            timeout=1,
            context=context,
        )
        page = json.loads(feedback["content"])["data"]
        assert page["original_is_error"] is True
        content.append(page["content"])
        offset = page["next_offset"]
    assert "".join(content) == raw["content"]
    with pytest.raises(ValueError, match="different result"):
        await results.put(ref, {**raw, "content": "不允许覆盖"}, context=context)


@pytest.mark.parametrize(
    "arguments,code",
    [
        ({"ref": "a" * 32}, "NOT_FOUND"),
        ({"ref": "a" * 32, "limit": 2049}, "INVALID_ARGUMENTS"),
        ({"ref": "a" * 32, "offset": -1}, "INVALID_ARGUMENTS"),
        ({"ref": "../redis-key"}, "INVALID_ARGUMENTS"),
        ({"ref": "a" * 32, "user_id": "b"}, "INVALID_ARGUMENTS"),
    ],
)
async def test_missing_reference_and_invalid_read_arguments_are_errors(stores, arguments, code):
    _, info, _, results = stores
    runner = SkillRunner(create_default_tools(result_store=results))
    feedback = await runner.execute(
        ToolUse(id="read", name="read_tool_result", input=arguments),
        timeout=1,
        context=ToolContext(user_id="a", session_id=info.id),
    )
    assert feedback["is_error"]
    assert json.loads(feedback["content"])["error"]["code"] == code


async def test_persisted_result_recovers_unknown_write_without_reexecution(stores, monkeypatch):
    sessions, info, redis, results = stores
    executions = []

    async def write(arguments, context):
        executions.append(context.operation_id)
        return {"large": "写入完成" * 1000}

    tools = create_default_tools(result_store=results)
    tools.register(Tool("write", "写操作", CalculatorArgs, write))

    class Model:
        def __init__(self):
            self.count = 0

        async def run_turn(self, *args, **kwargs):
            self.count += 1
            if self.count == 1:
                return turn("write-call", "write", {"operation": "add", "left": 1, "right": 2})
            return final()

    original = results.put

    async def persisted_then_crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(results, "put", persisted_then_crash)
    loop = AgentLoop(
        Model(), tools, result_store=results, result_policy=ToolResultPolicy(inline_max_bytes=256)
    )
    trace_store = TraceStore(sessions.session_factory)
    service = ConversationService(
        sessions,
        SessionCache(redis, ttl_seconds=60),
        loop,
        timeout_seconds=120,
        trace_store=trace_store,
    )
    with pytest.raises(asyncio.CancelledError):
        await service.chat("a", info.id, "执行")
    raw = await sessions.load_task("a", info.id)
    # 模拟进程在原文提交后、最新检查点提交前退出，只留下 unknown 恢复点。
    raw["operations"][0]["status"] = "unknown"
    raw["operations"][0]["result"] = None
    await sessions.save_task("a", info.id, raw, expected_version=0)
    monkeypatch.setattr(results, "put", original)
    await redis.flushdb()
    resumed = await service.chat("a", info.id, "继续", action="continue")
    assert resumed.task_status == "completed"
    assert len(executions) == 1
    assert raw["operations"][0]["id"] == executions[0]
    detail = await trace_store.detail("a", info.id, resumed.trace_id)
    assert any(s["name"] == "tool.result_reused" for s in detail["spans"])
    assert not any(s["name"] == "tool.write" for s in detail["spans"])
    assert len((await sessions.load_task("a", info.id))["traces"]) == 1


async def test_missing_reference_pauses_before_model_can_repeat_original_tool(stores):
    sessions, info, redis, results = stores

    class Model:
        def __init__(self):
            self.calls = 0

        async def run_turn(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                pytest.fail(
                    "Missing persisted result must require user input before another decision"
                )
            return turn("missing", "read_tool_result", {"ref": "f" * 32})

    model = Model()
    loop = AgentLoop(model, create_default_tools(result_store=results), result_store=results)
    service = ConversationService(
        sessions, SessionCache(redis, ttl_seconds=60), loop, timeout_seconds=120
    )
    reply = await service.chat("a", info.id, "读取之前的结果")
    assert reply.stop_reason == "result_unavailable"
    assert reply.task_status == "waiting_user"
    checkpoint = await sessions.load_task("a", info.id)
    assert checkpoint["operations"][0]["status"] == "done"
    assert checkpoint["operations"][0]["result"]["is_error"]
    assert await service.history("a", info.id) == []
