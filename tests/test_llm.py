import json

import httpx
import pytest

from server.config import get_settings
from server.llm import LLMClient, Message


def real_settings(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "https://llm.example.invalid/compatible-mode/v1")
    monkeypatch.setenv("AGENT_LLM_MODEL", "test-model")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "test-api-key")
    return get_settings()


async def test_completion_request(monkeypatch):
    def respond(request):
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-api-key"
        assert json.loads(request.content)["model"] == "test-model"
        return httpx.Response(200, json={"choices": [{"message": {"content": "你好"}}]})

    client = LLMClient(real_settings(monkeypatch), transport=httpx.MockTransport(respond))
    try:
        assert await client.complete([Message(role="user", content="hello")]) == "你好"
    finally:
        await client.close()


async def test_stream_skips_metadata_and_stops_at_done(monkeypatch):
    payload = (
        ": keepalive\n\n"
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
        'data: {"choices":[],"usage":{"total_tokens":3}}\n\n'
        "data: [DONE]\n\n"
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
