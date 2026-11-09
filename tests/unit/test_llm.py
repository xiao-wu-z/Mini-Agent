import json

import httpx
import pytest

from server.config import get_settings
from server.llm import LLMClient, Message


def real_settings(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "https://llm.example.invalid/v1")
    monkeypatch.setenv("AGENT_LLM_MODEL", "test-model")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "test-api-key")
    return get_settings()


async def test_completion_request(monkeypatch):
    def respond(request):
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "test-api-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert json.loads(request.content)["model"] == "test-model"
        assert json.loads(request.content)["max_tokens"] == 4096
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "你好"}], "stop_reason": "end_turn"}
        )

    client = LLMClient(real_settings(monkeypatch), transport=httpx.MockTransport(respond))
    try:
        assert await client.complete([Message(role="user", content="hello")]) == "你好"
    finally:
        await client.close()


async def test_stream_skips_metadata_and_stops_at_message_stop(monkeypatch):
    payload = (
        ": keepalive\n\n"
        'data: {"type":"message_start","message":{"role":"assistant"}}\n\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"你好"}}\n\n'
        'data: {"type":"message_delta","usage":{"output_tokens":3}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )
    client = LLMClient(
        real_settings(monkeypatch),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=payload)),
    )
    try:
        chunks = [chunk async for chunk in client.stream([Message(role="user", content="hello")])]
        assert chunks == ["你好"]
    finally:
        await client.close()


async def test_run_turn_uses_native_tools_and_system(monkeypatch):
    settings = real_settings(monkeypatch)
    settings.llm_base_url = "https://llm.example.invalid/anthropic"
    settings.llm_auth_header = "authorization"
    tools = [{"name": "example", "description": "An example", "input_schema": {"type": "object"}}]
    blocks = [
        {"type": "thinking", "thinking": "opaque-provider-block", "signature": "opaque-signature"},
        {"type": "tool_use", "id": "toolu_1", "name": "example", "input": {}},
    ]

    def respond(request):
        body = json.loads(request.content)
        assert request.url.path == "/anthropic/v1/messages"
        assert request.headers["Authorization"] == "Bearer test-api-key"
        assert body["system"] == "System rules"
        assert body["tools"] == tools
        assert body["messages"] == [{"role": "user", "content": "hello"}]
        assert body["stream"] is False
        return httpx.Response(200, json={"content": blocks, "stop_reason": "tool_use"})

    client = LLMClient(settings, transport=httpx.MockTransport(respond))
    try:
        turn = await client.run_turn(
            [Message(role="user", content="hello")], system="System rules", tools=tools
        )
        assert turn.content == blocks
        assert turn.stop_reason == "tool_use"
        assert turn.text == ""
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload",
    [
        'data: {"type":"error","error":{"message":"private"}}\n\n',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"partial"}}\n\n',
    ],
)
async def test_broken_stream_is_not_treated_as_complete(monkeypatch, payload):
    client = LLMClient(
        real_settings(monkeypatch),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=payload)),
    )
    try:
        with pytest.raises(RuntimeError):
            _ = [part async for part in client.stream([Message(role="user", content="hello")])]
    finally:
        await client.close()


async def test_provider_failure_is_reported(monkeypatch):
    client = LLMClient(
        real_settings(monkeypatch),
        transport=httpx.MockTransport(lambda _: httpx.Response(429)),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete([Message(role="user", content="hello")])
    finally:
        await client.close()


async def test_echo_does_not_call_network():
    def fail(_):
        pytest.fail("Echo must not access network")

    client = LLMClient(get_settings(), transport=httpx.MockTransport(fail))
    try:
        assert await client.complete([Message(role="user", content="hello")]) == "hello"
    finally:
        await client.close()


async def test_token_count_fallback_and_output_cap(monkeypatch):
    settings = real_settings(monkeypatch)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "你好"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 4, "output_tokens": 2, "cache_read_input_tokens": 3},
            },
        )

    client = LLMClient(settings, transport=httpx.MockTransport(respond))
    try:
        messages = [Message(role="user", content="你好")]
        assert await client.count_tokens(messages, tools=[], system="规则") > 0
        result = await client.run_turn(messages, max_tokens=7)
        assert requests[-1][1]["max_tokens"] == 7
        assert result.usage.total == 9
    finally:
        await client.close()


def sse_payload(*events):
    return "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)


async def test_stream_turn_emits_text_before_completion_and_preserves_tool_blocks(monkeypatch):
    import asyncio

    seen = asyncio.Event()
    received = []
    first = sse_payload(
        {
            "type": "message_start",
            "message": {
                "usage": {"input_tokens": 4, "cache_read_input_tokens": 3, "output_tokens": 1}
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "thinking",
                "thinking": "",
                "signature": "",
                "provider_field": "opaque",
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "内部内容"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig-1"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "你好"},
        },
    )
    rest = sse_payload(
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "call", "name": "calculator", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"operation":"add","left":1,'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '"right":2}'},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    )

    class ResponseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first.encode()
            assert seen.is_set(), "Text must be emitted before the backend completes the response"
            yield rest.encode()

    def respond(request):
        body = json.loads(request.content)
        assert body["stream"] and body["max_tokens"] == 128
        assert body["system"] == "规则"
        return httpx.Response(200, stream=ResponseStream())

    async def receive(text):
        received.append(text)
        seen.set()

    client = LLMClient(real_settings(monkeypatch), transport=httpx.MockTransport(respond))
    try:
        turn = await client.stream_turn(
            [Message(role="user", content="你好")],
            tools=[],
            system="规则",
            max_tokens=128,
            on_text_delta=receive,
        )
    finally:
        await client.close()
    assert received == ["你好"]
    assert turn.content[0] == {
        "type": "thinking",
        "thinking": "内部内容",
        "signature": "sig-1",
        "provider_field": "opaque",
    }
    assert turn.content[2]["input"] == {"operation": "add", "left": 1, "right": 2}
    assert turn.stop_reason == "tool_use" and turn.usage.total == 12


async def test_truncated_stream_never_parses_or_executes_partial_tool_arguments(monkeypatch):
    from server.agent_loop import AgentLoop
    from server.tools import create_default_tools

    payload = sse_payload(
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "partial",
                "name": "calculator",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"left":'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "max_tokens"},
            "usage": {"output_tokens": 4},
        },
        {"type": "message_stop"},
    )

    def respond(request):
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 5})
        return httpx.Response(200, text=payload)

    events = []

    async def emit(event):
        events.append(event)

    client = LLMClient(real_settings(monkeypatch), transport=httpx.MockTransport(respond))
    try:
        result = await AgentLoop(client, create_default_tools()).run("计算", on_event=emit)
    finally:
        await client.close()
    assert result.stop_reason == "max_tokens"
    assert result.request_tokens == 9 and not result.tool_traces
    assert not any(event.event == "tool_use" for event in events)


@pytest.mark.parametrize(
    "suffix",
    [
        [],
        [{"type": "error", "error": {"message": "private"}}],
        [{"type": "message_stop"}],
    ],
)
async def test_incomplete_tool_stream_is_rejected(monkeypatch, suffix):
    payload = sse_payload(
        {"type": "message_start", "message": {}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "call", "name": "calculator", "input": {}},
        },
        *suffix,
    )
    client = LLMClient(
        real_settings(monkeypatch),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=payload)),
    )

    async def emit(text):
        pytest.fail("No public text exists in this stream")

    try:
        with pytest.raises((RuntimeError, ValueError)):
            await client.stream_turn(
                [Message(role="user", content="执行")],
                tools=[],
                system="",
                max_tokens=128,
                on_text_delta=emit,
            )
    finally:
        await client.close()
