"""Chat SSE 的文本增量、工具 marker、错误与断连恢复测试。"""

import asyncio
import json
from collections import deque

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.agent_loop import AgentLoop
from server.app import create_app
from server.cache import SessionCache
from server.config import get_settings
from server.conversations import ConversationService
from server.llm import LLMTurn, TokenUsage
from server.sse import StreamEvent, stream_events
from server.store.base import Base
from server.store.sessions import SessionStore
from server.store.traces import TraceStore
from server.tools import Tool, create_default_tools
from skills.calculator.handler import CalculatorArgs


def final(text="完成"):
    return LLMTurn(
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=5, output_tokens=2),
    )


def decode_frames(value):
    result = []
    for frame in value.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.splitlines()
        result.append(
            (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        )
    return result


class Model:
    def __init__(self):
        self.turns = deque()
        self.requests = []

    async def run_turn(self, messages, **kwargs):
        self.requests.append([m.model_copy(deep=True) for m in messages])
        value = self.turns.popleft() if self.turns else final()
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture
async def chat_service(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sse.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = SessionStore(async_sessionmaker(engine, expire_on_commit=False))
    info = await sessions.create("a", "流式窗口")
    model = Model()
    service = ConversationService(
        sessions,
        SessionCache(None, ttl_seconds=60),
        AgentLoop(model, create_default_tools()),
        timeout_seconds=120,
        trace_store=TraceStore(sessions.session_factory),
    )
    app = create_app(get_settings())
    app.state.conversations = service
    yield service, model, info, app
    await engine.dispose()


async def test_chat_streams_text_and_paired_tool_marks_without_private_blocks(chat_service):
    service, model, info, app = chat_service
    model.turns.extend(
        [
            LLMTurn(
                content=[
                    {"type": "thinking", "thinking": "private-reason", "signature": "private-sign"},
                    {"type": "text", "text": "计算中"},
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "calculator",
                        "input": {"operation": "add", "left": 1, "right": 2},
                    },
                ],
                stop_reason="tool_use",
            ),
            final("3"),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/sessions/{info.id}/chat", json={"message": "计算"}, headers={"X-User-ID": "a"}
        )
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    events = decode_frames(response.text)
    assert [name for name, _ in events] == [
        "start",
        "text_delta",
        "tool_use",
        "tool_result",
        "text_delta",
        "done",
    ]
    use, result = events[2][1], events[3][1]
    assert use["tool_use_id"] == result["tool_use_id"] == "call"
    assert use["status"] == "running" and "工具调用中" in use["mark"]
    assert result["status"] == "success" and "工具 OK" in result["mark"]
    assert events[-1][1]["text"] == "3" and events[-1][1]["trace_id"]
    assert "private-reason" not in response.text and "private-sign" not in response.text
    history = await service.history("a", info.id)
    assert history[1].content[0]["signature"] == "private-sign"
    assert history[2].content[0]["tool_use_id"] == "call"
    assert "工具调用中" not in str([m.model_dump() for m in history])


async def test_sse_provider_error_is_sanitized_and_has_no_done(chat_service):
    service, model, info, app = chat_service
    model.turns.append(RuntimeError("password=private-password"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/sessions/{info.id}/chat", json={"message": "执行"}, headers={"X-User-ID": "a"}
        )
    events = decode_frames(response.text)
    assert response.status_code == 200 and events[-1][0] == "error"
    assert not any(name == "done" for name, _ in events)
    assert "private-password" not in response.text
    assert (await service.store.load_task("a", info.id))["status"] == "paused"


async def test_unknown_write_result_mark_requests_confirmation(chat_service):
    service, model, info, app = chat_service

    async def uncertain(arguments, context):
        raise RuntimeError("unknown transaction outcome")

    service.loop.tools.register(Tool("write", "写操作", CalculatorArgs, uncertain))
    model.turns.append(
        LLMTurn(
            content=[
                {
                    "type": "tool_use",
                    "id": "write-call",
                    "name": "write",
                    "input": {"operation": "add", "left": 1, "right": 2},
                }
            ],
            stop_reason="tool_use",
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/sessions/{info.id}/chat", json={"message": "写入"}, headers={"X-User-ID": "a"}
        )
    events = decode_frames(response.text)
    result = next(data for name, data in events if name == "tool_result")
    assert result["status"] == "unknown" and "结果待确认" in result["mark"]
    assert events[-1][1]["task_status"] == "waiting_user"


async def test_disconnect_cancels_worker_and_preserves_resume_state(chat_service):
    service, model, info, _ = chat_service

    class WaitingModel:
        async def stream_turn(self, messages, *, on_text_delta, **kwargs):
            await on_text_delta("尚未完成")
            await asyncio.Event().wait()

        async def run_turn(self, *args, **kwargs):
            pytest.fail("SSE must use streaming model call")

    service.loop.llm = WaitingModel()

    async def run(on_event):
        return await service.chat("a", info.id, "流式请求", on_event=on_event)

    stream = stream_events(run, heartbeat_seconds=1)
    assert decode_frames(await anext(stream))[0][0] == "start"
    assert decode_frames(await anext(stream))[0][0] == "text_delta"
    await stream.aclose()
    checkpoint = await service.store.load_task("a", info.id)
    assert checkpoint["status"] == "paused" and checkpoint["reason"] == "cancelled"
    assert await service.history("a", info.id) == []
    traces = await service.trace_store.list_traces("a", info.id, task_id=None, limit=10, offset=0)
    assert traces[0]["status"] == "cancelled"
    service.loop.llm = model
    result = await service.chat("a", info.id, "继续", action="continue")
    assert result.task_status == "completed" and result.turn_id == checkpoint["task_id"]
    assert "尚未完成" not in str(await service.history("a", info.id))


async def test_heartbeat_and_closing_stream_do_not_leave_background_worker():
    stopped = asyncio.Event()

    async def run(on_event):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    stream = stream_events(run, heartbeat_seconds=0.01)
    assert decode_frames(await anext(stream))[0][0] == "heartbeat"
    await stream.aclose()
    assert stopped.is_set()


async def test_internal_cancel_does_not_leave_stream_waiting_forever():
    async def run(on_event):
        raise asyncio.CancelledError()

    events = [frame async for frame in stream_events(run, heartbeat_seconds=0.01)]
    assert decode_frames("".join(events))[-1][0] == "error"


async def test_invalid_owner_is_http_error_before_stream_opens(chat_service):
    _, _, info, app = chat_service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/sessions/{info.id}/chat", json={"message": "禁止访问"}, headers={"X-User-ID": "b"}
        )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_text_cannot_inject_sse_frames():
    encoded = StreamEvent(
        event="text_delta", data={"text": "你好\n\nevent: done\ndata: {}"}
    ).encode()
    assert len(decode_frames(encoded)) == 1


async def test_asgi_disconnect_waits_for_checkpoint_and_releases_session_lock(chat_service):
    service, _, info, app = chat_service
    disconnected = asyncio.Event()

    class WaitingModel:
        async def stream_turn(self, messages, *, on_text_delta, **kwargs):
            await on_text_delta("断连测试")
            await asyncio.Event().wait()

    service.loop.llm = WaitingModel()
    body = json.dumps({"message": "真实 ASGI 断连"}).encode()
    request_read = False

    async def receive():
        nonlocal request_read
        if not request_read:
            request_read = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and b"event: text_delta" in message.get(
            "body", b""
        ):
            disconnected.set()

    path = f"/sessions/{info.id}/chat"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"x-user-id", b"a"), (b"content-type", b"application/json")],
        "server": ("test", 80),
        "client": ("test", 12345),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    checkpoint = await service.store.load_task("a", info.id)
    assert checkpoint["status"] == "paused" and checkpoint["reason"] == "cancelled"
    assert info.id not in service._locks
    traces = await service.trace_store.list_traces("a", info.id, task_id=None, limit=10, offset=0)
    assert traces[0]["status"] == "cancelled"
