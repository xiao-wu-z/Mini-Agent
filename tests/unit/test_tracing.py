"""请求追踪的落库、嵌套关系、用量、故障隔离和查询权限验证。"""

import asyncio
import json
from collections import deque

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.agent_loop import AgentLoop
from server.app import create_app
from server.cache import SessionCache
from server.config import ContextPolicy, TracePolicy, get_settings
from server.conversations import ConversationService
from server.llm import LLMTurn, Message, TokenUsage
from server.store.base import Base
from server.store.sessions import ConversationError, SessionStore
from server.store.traces import TraceStore
from server.tools import Tool, ToolRegistry, create_default_tools
from server.tracing import TraceRecorder
from skills.calculator.handler import CalculatorArgs


def final(text="完成", usage=None):
    return LLMTurn(
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=usage if usage is not None else TokenUsage(input_tokens=5, output_tokens=2),
    )


def call(call_id="call", *, left=1, right=2, name="calculator"):
    return LLMTurn(
        content=[
            {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": {"operation": "add", "left": left, "right": right},
            }
        ],
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=5, output_tokens=2),
    )


class Model:
    def __init__(self, *turns):
        self.turns = deque(turns)
        self.requests = []

    async def count_tokens(self, *args, **kwargs):
        return 5

    async def run_turn(self, messages, **kwargs):
        self.requests.append([m.model_copy(deep=True) for m in messages])
        value = self.turns.popleft() if self.turns else final()
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture
async def traced(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'traces.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sessions = SessionStore(factory)
    store = TraceStore(factory)
    info = await sessions.create("a", "埋点窗口")
    model = Model()
    loop = AgentLoop(model, create_default_tools())
    service = ConversationService(
        sessions,
        SessionCache(None, ttl_seconds=60),
        loop,
        timeout_seconds=120,
        trace_store=store,
        trace_policy=TracePolicy(write_timeout_seconds=1),
    )
    yield service, model, store, info
    await engine.dispose()


async def test_model_and_tool_are_siblings_and_parent_covers_actual_execution(traced):
    service, model, store, info = traced
    model.turns.extend(
        [
            call(),
            final(
                "3",
                TokenUsage(
                    input_tokens=3,
                    output_tokens=2,
                    cache_creation_input_tokens=4,
                    cache_read_input_tokens=6,
                ),
            ),
        ]
    )
    reply = await service.chat("a", info.id, "计算")
    detail = await store.detail("a", info.id, reply.trace_id)
    root = detail["trace"]
    assert root["task_id"] == reply.turn_id and root["status"] == "success"
    assert root["request_tokens"] == reply.request_tokens == 22
    assert root["input_tokens"] == 8 and root["cache_read_input_tokens"] == 6
    assert root["ended_at"] and root["duration_ms"] >= 0 and not root["incomplete"]
    spans = detail["spans"]
    tool = next(s for s in spans if s["name"] == "tool.calculator")
    generations = [s for s in spans if s["type"] == "generation"]
    assert len(generations) == 2
    assert tool["parent_span_id"] == generations[0]["parent_span_id"]
    parent = next(s for s in spans if s["id"] == tool["parent_span_id"])
    assert parent["started_at"] <= tool["started_at"] <= tool["ended_at"] <= parent["ended_at"]
    assert tool["tool_use_id"] == "call" and tool["operation_id"]
    assert json.loads(tool["output_summary"])["result"]["data"]["result"] == 3
    assert (
        sum(
            s["input_tokens"]
            + s["output_tokens"]
            + s["cache_creation_input_tokens"]
            + s["cache_read_input_tokens"]
            for s in generations
        )
        == root["request_tokens"]
    )
    assert detail["tree"] and not detail["truncated"]


async def test_handled_tool_error_does_not_make_request_fail(traced):
    service, model, store, info = traced
    model.turns.extend([call(left="wrong"), call("fixed"), final("已修正")])
    reply = await service.chat("a", info.id, "执行")
    detail = await store.detail("a", info.id, reply.trace_id)
    tools = [s for s in detail["spans"] if s["name"] == "tool.calculator"]
    assert [s["status"] for s in tools] == ["error", "success"]
    assert tools[0]["error_code"] == "INVALID_ARGUMENTS"
    assert detail["trace"]["status"] == "success"


async def test_continue_creates_new_trace_and_only_counts_this_request(traced):
    service, model, store, info = traced
    service.loop.max_rounds = 1
    model.turns.extend([call(), final("3")])
    first = await service.chat("a", info.id, "计算")
    checkpoint = (await service.store.load_task("a", info.id))["checkpoint"]["id"]
    second = await service.chat("a", info.id, "继续", action="continue")
    assert first.turn_id == second.turn_id and first.trace_id != second.trace_id
    traces = await store.list_traces("a", info.id, task_id=first.turn_id, limit=10, offset=0)
    assert len(traces) == 2
    by_id = {item["id"]: item for item in traces}
    assert by_id[first.trace_id]["status"] == "paused"
    assert by_id[first.trace_id]["stop_reason"] == "max_rounds"
    assert by_id[second.trace_id]["checkpoint_id"] == checkpoint
    assert all(item["request_tokens"] == 7 for item in traces)
    assert second.total_tokens == 14
    detail = await store.detail("a", info.id, second.trace_id)
    assert any(s["name"] == "task.resumed" for s in detail["spans"])
    assert not any(s["name"].startswith("tool.") and s["type"] == "span" for s in detail["spans"])


async def test_completed_answer_replay_has_zero_model_usage(traced):
    service, model, store, info = traced
    first = await service.chat("a", info.id, "问题")
    replay = await service.chat("a", info.id, "继续", action="continue")
    detail = await store.detail("a", info.id, replay.trace_id)
    assert first.trace_id != replay.trace_id and len(model.requests) == 1
    assert detail["trace"]["request_tokens"] == 0
    assert [s["name"] for s in detail["spans"]] == ["task.answer_reused"]


@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_trace_write_failure_never_repeats_tools_or_breaks_business(
    traced, monkeypatch, failure
):
    service, model, store, info = traced
    executions = []

    async def execute(arguments, context):
        executions.append(context.operation_id)
        return {"result": 3}

    service.loop.tools.register(Tool("write", "测试写操作", CalculatorArgs, execute))
    model.turns.extend([call(name="write"), final("成功")])

    async def fail(*args, **kwargs):
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("password=should-not-be-logged")

    for method in ("create_trace", "update_trace", "create_span", "update_span"):
        monkeypatch.setattr(store, method, fail)
    service.trace_policy.write_timeout_seconds = 0.001
    reply = await asyncio.wait_for(service.chat("a", info.id, "执行"), timeout=2)
    assert reply.text == "成功" and len(executions) == 1
    assert len(await service.history("a", info.id)) == 4


async def test_provider_error_records_reservation_without_leaking_exception(traced):
    service, model, store, info = traced
    model.turns.append(RuntimeError("authorization=private"))
    with pytest.raises(ConversationError):
        await service.chat("a", info.id, "调用模型")
    traces = await store.list_traces("a", info.id, task_id=None, limit=10, offset=0)
    detail = await store.detail("a", info.id, traces[0]["id"])
    assert detail["trace"]["status"] == "error"
    assert detail["trace"]["usage_estimated"]
    generation = next(s for s in detail["spans"] if s["type"] == "generation")
    assert generation["status"] == "error" and generation["error_code"] == "RuntimeError"
    assert generation["input_tokens"] + generation["output_tokens"] == traces[0]["request_tokens"]
    assert "private" not in str(detail)


async def test_cancelled_request_closes_trace_and_open_step(traced):
    service, model, store, info = traced
    model.turns.append(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await service.chat("a", info.id, "取消执行")
    trace = (await store.list_traces("a", info.id, task_id=None, limit=10, offset=0))[0]
    detail = await store.detail("a", info.id, trace["id"])
    assert trace["status"] == "cancelled"
    assert all(not s["incomplete"] for s in detail["spans"])
    assert any(s["status"] == "cancelled" for s in detail["spans"])


async def test_bounded_capture_redacts_credentials_and_thinking(traced):
    _, _, store, info = traced
    recorder = TraceRecorder(store, policy=TracePolicy(preview_chars=256, write_timeout_seconds=1))
    await recorder.start(
        user_id="a",
        session_id=info.id,
        action="new",
        text="password=secret-text Bearer authorization-value sk-testvalue",
    )
    async with recorder.span(
        "capture",
        input={
            "api_key": "secret-key",
            "password": "secret-pass",
            "nested": {"thinking": "hidden-reasoning", "signature": "hidden-signature"},
            "data": "x" * 10000,
            "number": float("nan"),
        },
    ) as span:
        span.output({"access_token": "secret-access", "result": "y" * 10000})
    await recorder.finish("success", reason="end_turn")
    detail = await store.detail("a", info.id, recorder.id)
    rendered = str(detail)
    for secret in (
        "secret-text",
        "authorization-value",
        "sk-testvalue",
        "secret-key",
        "secret-pass",
        "hidden-reasoning",
        "hidden-signature",
        "secret-access",
    ):
        assert secret not in rendered
    assert all(
        len(s[name]) <= 256 for s in detail["spans"] for name in ("input_summary", "output_summary")
    )


async def test_span_cap_preserves_generation_totals(traced):
    service, model, store, info = traced
    service.trace_policy.max_spans = 1
    reply = await service.chat("a", info.id, "问题")
    detail = await store.detail("a", info.id, reply.trace_id)
    assert len(detail["spans"]) == 1
    assert detail["trace"]["dropped_spans"] > 0
    assert detail["trace"]["request_tokens"] == reply.request_tokens == 7


async def test_http_trace_list_detail_pagination_and_isolation(traced):
    service, _, _, info = traced
    first = await service.chat("a", info.id, "第一问")
    await service.chat("a", info.id, "第二问")
    other = await service.store.create("a", "其他窗口")
    app = create_app(get_settings())
    app.state.conversations = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        headers = {"X-User-ID": "a"}
        path = f"/sessions/{info.id}/traces"
        page = await client.get(path, params={"limit": 1}, headers=headers)
        assert page.status_code == 200 and page.json()["has_more"]
        filtered = await client.get(path, params={"task_id": first.turn_id}, headers=headers)
        assert len(filtered.json()["items"]) == 1
        detail = await client.get(f"{path}/{first.trace_id}", headers=headers)
        assert detail.status_code == 200 and detail.json()["tree"]
        denied = await client.get(f"{path}/{first.trace_id}", headers={"X-User-ID": "b"})
        assert denied.status_code == 404
        wrong_window = await client.get(
            f"/sessions/{other.id}/traces/{first.trace_id}", headers=headers
        )
        assert wrong_window.status_code == 404


async def test_unfinished_records_are_visible_without_fabricated_success(traced):
    _, _, store, info = traced
    recorder = TraceRecorder(store)
    await recorder.start(user_id="a", session_id=info.id, action="new", text="模拟进程退出")
    handle = await recorder.start_span("unfinished")
    detail = await store.detail("a", info.id, recorder.id)
    assert detail["trace"]["status"] == "running" and detail["trace"]["incomplete"]
    assert detail["spans"][0]["id"] == handle.id and detail["spans"][0]["incomplete"]


async def test_compression_generation_is_nested_and_included_once(traced, monkeypatch):
    service, model, store, info = traced
    source = [
        Message(role="user" if i % 2 == 0 else "assistant", content="资料" * 100) for i in range(10)
    ]
    await service.store.save_turn("a", info.id, "old-task", source, expected_version=0)
    service.loop.tools = ToolRegistry()
    service.loop.context_policy = ContextPolicy(
        window_tokens=2400, keep_head_units=2, keep_tail_units=2, summary_max_tokens=64
    )
    service.loop.max_output_tokens, service.loop.token_margin = 64, 0

    async def count(messages, *, tools, system):
        return len(json.dumps([m.model_dump() for m in messages], ensure_ascii=False)) + len(system)

    monkeypatch.setattr(model, "count_tokens", count)
    model.turns.extend([final("摘要"), final("答案")])
    reply = await service.chat("a", info.id, "问题")
    detail = await store.detail("a", info.id, reply.trace_id)
    generations = [s for s in detail["spans"] if s["type"] == "generation"]
    assert [s["name"] for s in generations] == ["generation.summary", "generation.decision"]
    parent = next(s for s in detail["spans"] if s["id"] == generations[0]["parent_span_id"])
    assert parent["name"] == "context.prepare"
    assert json.loads(parent["output_summary"])["summary_version"] == 1
    assert detail["trace"]["request_tokens"] == reply.request_tokens == 14


async def test_reflection_is_a_generation_with_usage_and_pause_is_not_success(traced):
    service, model, store, info = traced
    reflection = json.dumps(
        {
            "cause": "类型错误",
            "change": "修正 left",
            "missing_information": "left 是多少",
            "next_action": "ask_user",
        }
    )
    model.turns.extend(
        [call("a", left="bad"), call("b", left="bad"), call("c", left="bad"), final(reflection)]
    )
    reply = await service.chat("a", info.id, "计算")
    detail = await store.detail("a", info.id, reply.trace_id)
    assert detail["trace"]["status"] == "waiting_user"
    assert detail["trace"]["stop_reason"] == "needs_user_input"
    generations = [s for s in detail["spans"] if s["type"] == "generation"]
    assert len(generations) == 4 and generations[-1]["name"] == "generation.reflection"
    assert detail["trace"]["request_tokens"] == reply.request_tokens == 28
    assert any(s["name"] == "task.paused" for s in detail["spans"])


async def test_disabled_tracing_keeps_chat_behavior_and_writes_nothing(traced):
    service, _, store, info = traced
    service.trace_policy.enabled = False
    reply = await service.chat("a", info.id, "正常聊天")
    assert reply.text == "完成" and reply.trace_id is None
    assert await store.list_traces("a", info.id, task_id=None, limit=10, offset=0) == []


async def test_budget_rejection_has_no_generation_or_reserved_usage(traced):
    service, model, store, info = traced
    service.loop.request_max_tokens = 1
    reply = await service.chat("a", info.id, "预算不足")
    detail = await store.detail("a", info.id, reply.trace_id)
    assert detail["trace"]["status"] == "paused"
    assert detail["trace"]["request_tokens"] == 0
    assert not model.requests
    assert not any(s["type"] == "generation" for s in detail["spans"])
    assert all(s["status"] != "error" for s in detail["spans"])


async def test_cancel_action_creates_its_own_trace_on_existing_task(traced):
    service, _, store, info = traced
    service.loop.request_max_tokens = 1
    paused = await service.chat("a", info.id, "任务")
    cancelled = await service.chat("a", info.id, "取消", action="cancel")
    detail = await store.detail("a", info.id, cancelled.trace_id)
    assert cancelled.trace_id != paused.trace_id and cancelled.turn_id == paused.turn_id
    assert detail["trace"]["action"] == "cancel"
    assert detail["trace"]["status"] == "cancelled" and detail["trace"]["request_tokens"] == 0
