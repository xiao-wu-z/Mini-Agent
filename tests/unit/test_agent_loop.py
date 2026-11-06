import asyncio
import json
from collections import deque
from unittest.mock import AsyncMock

import httpx
import pytest

from server.agent_loop import AgentLoop
from server.config import get_settings
from server.llm import LLMClient, LLMTurn, Message
from server.tools import Tool, create_default_tools
from skills.calculator.handler import CalculatorArgs


def tool_call(call_id="toolu_1", name="calculator", **arguments):
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments}


def tool_turn(*blocks):
    return LLMTurn(content=list(blocks), stop_reason="tool_use")


def final(text="完成"):
    return LLMTurn(content=[{"type": "text", "text": text}], stop_reason="end_turn")


class ScriptedLLM:
    def __init__(self, *turns):
        self.turns = deque(turns)
        self.requests = []

    async def run_turn(self, messages, *, tools, system):
        self.requests.append({"messages": [m.model_dump() for m in messages], "tools": tools})
        return self.turns.popleft()


async def test_direct_reply_does_not_call_tools():
    llm = ScriptedLLM(final("你好"))
    result = await AgentLoop(llm, create_default_tools()).run("你好")
    assert result.text == "你好"
    assert result.stop_reason == "end_turn"
    assert result.rounds == 1
    assert not result.tool_traces


async def test_native_http_roundtrip_and_full_block_feedback(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "https://model.example.invalid")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_LLM_MODEL", "test-model")
    requests = []
    blocks = [
        {"type": "thinking", "thinking": "opaque", "signature": "provider-signature"},
        {"type": "text", "text": "计算中"},
        tool_call(operation="multiply", left=128, right=36),
    ]

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body["tools"][0]["input_schema"]["type"] == "object"
            return httpx.Response(200, json={"content": blocks, "stop_reason": "tool_use"})
        assert body["messages"][-2] == {"role": "assistant", "content": blocks}
        tool_result = body["messages"][-1]
        assert tool_result["role"] == "user"
        assert tool_result["content"][0]["tool_use_id"] == "toolu_1"
        assert json.loads(tool_result["content"][0]["content"])["data"]["result"] == 4608
        return httpx.Response(200, json=final("结果是 4608").model_dump())

    client = LLMClient(get_settings(), transport=httpx.MockTransport(respond))
    try:
        result = await AgentLoop(client, create_default_tools()).run("128 乘以 36")
    finally:
        await client.close()
    assert result.text == "结果是 4608"
    assert result.rounds == 2
    assert len(result.tool_traces) == 1
    assert result.tool_traces[0].is_error is False


async def test_multiple_calls_are_collected_in_one_user_message():
    llm = ScriptedLLM(
        tool_turn(
            tool_call("a", operation="add", left=1, right=2),
            tool_call("b", operation="multiply", left=3, right=4),
        ),
        final(),
    )
    result = await AgentLoop(llm, create_default_tools()).run("两个计算")
    results = llm.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert [json.loads(r["content"])["data"]["result"] for r in results] == [3, 12]
    assert len(result.tool_traces) == 2


async def test_repeated_tool_rounds_before_final():
    llm = ScriptedLLM(
        tool_turn(tool_call("a", operation="add", left=1, right=2)),
        tool_turn(tool_call("b", operation="multiply", left=3, right=4)),
        final("12"),
    )
    result = await AgentLoop(llm, create_default_tools()).run("先加后乘")
    assert result.rounds == 3
    assert [m.role for m in result.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert result.text == "12"


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (tool_call(name="unknown"), "UNKNOWN_TOOL"),
        (tool_call(operation="add", left="invalid", right=2), "INVALID_ARGUMENTS"),
        (tool_call(operation="add", left=1), "INVALID_ARGUMENTS"),
        (tool_call(operation="divide", left=1, right=0), "INVALID_ARGUMENTS"),
    ],
)
async def test_tool_errors_are_fed_back_for_next_decision(call, code):
    llm = ScriptedLLM(tool_turn(call), final("请调整参数"))
    result = await AgentLoop(llm, create_default_tools()).run("执行")
    feedback = llm.requests[1]["messages"][-1]["content"][0]
    assert feedback["is_error"] is True
    assert json.loads(feedback["content"])["error"]["code"] == code
    assert result.tool_traces[0].is_error is True


async def test_model_can_repair_invalid_arguments():
    llm = ScriptedLLM(
        tool_turn(tool_call("a", operation="add", left=1)),
        tool_turn(tool_call("b", operation="add", left=1, right=2)),
        final("3"),
    )
    result = await AgentLoop(llm, create_default_tools()).run("1 + 2")
    assert result.text == "3"
    assert [trace.is_error for trace in result.tool_traces] == [True, False]


async def test_timeout_is_a_tool_error():
    async def wait(_, context):
        await asyncio.Event().wait()

    tools = create_default_tools()
    tools.register(Tool("wait", "Wait", CalculatorArgs, wait))
    llm = ScriptedLLM(tool_turn(tool_call(name="wait", operation="add", left=1, right=2)), final())
    await AgentLoop(llm, tools, tool_timeout=0.01).run("等待")
    feedback = llm.requests[1]["messages"][-1]["content"][0]
    assert json.loads(feedback["content"])["error"]["code"] == "TOOL_TIMEOUT"


async def test_max_rounds_stops_even_if_model_keeps_using_tools():
    llm = ScriptedLLM(
        tool_turn(tool_call("a", operation="add", left=1, right=2)),
        tool_turn(tool_call("b", operation="add", left=3, right=4)),
    )
    result = await AgentLoop(llm, create_default_tools(), max_rounds=2).run("一直计算")
    assert result.stop_reason == "max_rounds"
    assert result.rounds == len(llm.requests) == 2
    assert len(result.tool_traces) == 2
    assert result.messages[-1].content[0]["tool_use_id"] == "b"


async def test_truncated_response_never_executes_tools():
    llm = ScriptedLLM(
        LLMTurn(content=[tool_call(operation="add", left=1, right=2)], stop_reason="max_tokens")
    )
    result = await AgentLoop(llm, create_default_tools()).run("计算")
    assert result.stop_reason == "max_tokens"
    assert not result.tool_traces
    assert len(result.messages) == 1


@pytest.mark.parametrize(
    "blocks",
    [
        [],
        [tool_call("same"), tool_call("same")],
        [{"type": "tool_use", "name": "calculator", "input": {}}],
    ],
)
async def test_malformed_turn_is_rejected_before_any_execution(blocks):
    tools = create_default_tools()
    loop = AgentLoop(ScriptedLLM(tool_turn(*blocks)), tools)
    loop.runner.execute = AsyncMock()
    with pytest.raises(ValueError):
        await loop.run("执行")
    loop.runner.execute.assert_not_awaited()


async def test_tool_call_ids_cannot_be_replayed_in_same_run():
    call = tool_call("same", operation="add", left=1, right=2)
    llm = ScriptedLLM(tool_turn(call), tool_turn(call))
    with pytest.raises(ValueError, match="duplicate"):
        await AgentLoop(llm, create_default_tools()).run("执行")


async def test_history_can_be_reused_without_mutation():
    original = [
        Message(role="user", content="我叫小明"),
        Message(role="assistant", content="记住了"),
    ]
    llm = ScriptedLLM(final("小明"), final("另一个会话"))
    loop = AgentLoop(llm, create_default_tools())
    result = await loop.run("我叫什么？", history=original)
    await loop.run("你好")
    assert len(original) == 2
    assert len(result.messages) == 4
    assert len(llm.requests[1]["messages"]) == 1


async def test_cancellation_propagates():
    async def cancel(_, context):
        raise asyncio.CancelledError()

    tools = create_default_tools()
    tools.register(Tool("cancel", "Cancel", CalculatorArgs, cancel))
    llm = ScriptedLLM(tool_turn(tool_call(name="cancel", operation="add", left=1, right=2)))
    with pytest.raises(asyncio.CancelledError):
        await AgentLoop(llm, tools).run("执行")


def test_registry_rejects_duplicate_tools_and_exposes_schema():
    tools = create_default_tools()
    spec = tools.specs()[0]
    assert spec["name"] == "calculator"
    assert set(spec["input_schema"]["required"]) == {"operation", "left", "right"}
    with pytest.raises(ValueError, match="already registered"):
        tools.register(Tool("calculator", "Duplicate", CalculatorArgs, AsyncMock()))
