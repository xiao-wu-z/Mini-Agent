"""请求预算、分级纠错、复盘以及执行检查点恢复。"""

import asyncio
import json
from collections import deque

import pytest

from server.agent_loop import AgentLoop, TaskState
from server.llm import LLMTurn, TokenUsage
from server.tools import Tool, create_default_tools
from skills.calculator.handler import CalculatorArgs


def final(text="完成"):
    return LLMTurn(
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=5, output_tokens=2),
    )


def call(call_id, **arguments):
    return LLMTurn(
        content=[{"type": "tool_use", "id": call_id, "name": "calculator", "input": arguments}],
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
        self.requests.append(([m.model_copy(deep=True) for m in messages], kwargs))
        return self.turns.popleft()


async def test_budget_refuses_request_before_model_call():
    model = Model()
    result = await AgentLoop(
        model, create_default_tools(), request_max_tokens=5, token_margin=0
    ).run("任务")
    assert result.stop_reason == "token_budget_exhausted"
    assert not model.requests
    assert result.state.status == "paused"


async def test_output_cap_and_cached_usage_are_counted():
    turn = final()
    turn.usage = TokenUsage(
        input_tokens=2, output_tokens=2, cache_read_input_tokens=3, cache_creation_input_tokens=1
    )
    model = Model(turn)
    result = await AgentLoop(
        model, create_default_tools(), request_max_tokens=15, token_margin=2
    ).run("任务")
    assert model.requests[0][1]["max_tokens"] == 8
    assert result.request_tokens == result.total_tokens == 8


async def test_missing_usage_keeps_reserved_upper_bound():
    turn = final()
    turn.usage = None
    result = await AgentLoop(
        Model(turn), create_default_tools(), request_max_tokens=15, token_margin=2
    ).run("任务")
    assert result.request_tokens == 13
    assert result.usage_estimated is True


async def test_budget_resume_uses_new_request_budget_but_keeps_task_total():
    model = Model(call("a", operation="add", left=1, right=2), final("3"))
    loop = AgentLoop(
        model, create_default_tools(), request_max_tokens=14, max_output_tokens=2, token_margin=0
    )
    # 单步预算先暂停，已执行结果留在状态中。
    loop.max_rounds = 1
    first = await loop.run("计算")
    assert first.stop_reason == "max_rounds"
    assert len(first.tool_traces) == 1
    second = await loop.run("继续", state=first.state)
    assert second.task_status == "completed"
    assert second.run_id == first.run_id
    assert second.request_tokens == 7 and second.total_tokens == 14
    assert len(second.tool_traces) == 1


async def test_semantic_same_error_reflects_once_then_trips():
    turns = [call(str(i), operation="add", left=f"invalid{i}", right=1) for i in range(4)]
    reflection = final(
        json.dumps(
            {
                "cause": "left 类型错误",
                "change": "使用数字",
                "missing_information": "",
                "next_action": "retry",
            },
            ensure_ascii=False,
        )
    )
    model = Model(*turns[:3], reflection, turns[3])
    result = await AgentLoop(model, create_default_tools(), token_margin=0).run("计算")
    assert result.stop_reason == "repeated_tool_error"
    assert result.rounds == 5  # 四次工具决策加一次复盘。
    record = next(iter(result.state.failures.values()))
    assert record.count == 4 and record.reflected
    assert len(result.tool_traces) == 4
    second_feedback = model.requests[2][0][-1].content[0]
    assert json.loads(second_feedback["content"])["recovery"]["count"] == 2
    assert model.requests[3][1]["tools"] == []


async def test_reflection_can_request_information_and_continue():
    invalid = [call(str(i), operation="add", left="bad", right=1) for i in range(3)]
    reflection = final(
        json.dumps(
            {
                "cause": "参数缺失",
                "change": "请补充数字",
                "missing_information": "left 是多少？",
                "next_action": "ask_user",
            },
            ensure_ascii=False,
        )
    )
    model = Model(*invalid, reflection, call("fixed", operation="add", left=2, right=1), final("3"))
    loop = AgentLoop(model, create_default_tools(), token_margin=0)
    paused = await loop.run("计算")
    assert paused.task_status == "waiting_user"
    resumed = await loop.run("left 为2", state=paused.state)
    assert resumed.text == "3"
    assert next(iter(resumed.state.failures.values())).closed is True
    assert resumed.total_tokens > resumed.request_tokens


async def test_checkpoint_survives_crash_before_tool_and_does_not_repeat_done_tool():
    model = Model(call("first", operation="add", left=1, right=2), final("3"))
    loop = AgentLoop(model, create_default_tools())
    saved = None

    async def checkpoint(state):
        nonlocal saved
        saved = state.model_dump(mode="json")
        if state.operations and state.operations[0].status == "unknown":
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await loop.run("计算", checkpoint=checkpoint)
    restored = TaskState.model_validate(saved)
    assert restored.operations[0].status == "unknown"
    result = await loop.run("继续", state=restored)
    assert result.task_status == "completed"
    assert len(result.tool_traces) == 1


async def test_unknown_non_idempotent_tool_is_not_replayed():
    invoked = 0

    async def unsafe(args, context):
        nonlocal invoked
        invoked += 1
        raise asyncio.CancelledError()

    tools = create_default_tools()
    tools.register(Tool("unsafe", "write", CalculatorArgs, unsafe))
    turn = LLMTurn(
        content=[
            {
                "type": "tool_use",
                "id": "a",
                "name": "unsafe",
                "input": {"operation": "add", "left": 1, "right": 2},
            }
        ],
        stop_reason="tool_use",
    )
    loop = AgentLoop(Model(turn), tools)
    saved = None

    async def checkpoint(state):
        nonlocal saved
        saved = state.model_dump(mode="json")

    with pytest.raises(asyncio.CancelledError):
        await loop.run("执行", checkpoint=checkpoint)
    result = await loop.run("继续", state=TaskState.model_validate(saved))
    assert result.stop_reason == "unknown_tool_result"
    assert invoked == 1


async def test_reflection_budget_exhaustion_defers_without_losing_one_attempt():
    invalid = [call(str(i), operation="add", left="bad", right=1) for i in range(3)]
    reflection = final(
        json.dumps(
            {
                "cause": "类型错误",
                "change": "修改 left",
                "missing_information": "left 是多少",
                "next_action": "ask_user",
            },
            ensure_ascii=False,
        )
    )
    model = Model(*invalid, reflection)
    loop = AgentLoop(model, create_default_tools(), max_rounds=3, token_margin=0)
    first = await loop.run("计算")
    assert first.stop_reason == "max_rounds"
    record = next(iter(first.state.failures.values()))
    assert not record.reflected
    resumed = await loop.run("继续", state=first.state)
    assert resumed.stop_reason == "needs_user_input"
    assert next(iter(resumed.state.failures.values())).reflected
    assert len(model.requests) == 4


async def test_multiple_error_clusters_get_their_own_reflection_feedback():
    turns = []
    for index in range(3):
        turns.append(
            LLMTurn(
                content=[
                    {
                        "type": "tool_use",
                        "id": f"left-{index}",
                        "name": "calculator",
                        "input": {"operation": "add", "left": "bad", "right": 1},
                    },
                    {
                        "type": "tool_use",
                        "id": f"right-{index}",
                        "name": "calculator",
                        "input": {"operation": "add", "left": 1, "right": "bad"},
                    },
                ],
                stop_reason="tool_use",
                usage=TokenUsage(input_tokens=5, output_tokens=2),
            )
        )
    reflections = [
        final(
            json.dumps(
                {
                    "cause": field,
                    "change": f"修改 {field}",
                    "missing_information": "",
                    "next_action": "retry",
                }
            )
        )
        for field in ("left", "right")
    ]
    model = Model(*turns, *reflections, final())
    result = await AgentLoop(model, create_default_tools()).run("计算")
    assert result.task_status == "completed"
    assert result.rounds == 6
    assert all(record.reflected for record in result.state.failures.values())
    feedback = model.requests[-1][0][-1].content
    for block, field in zip(feedback, ("left", "right"), strict=True):
        reflection = json.loads(json.loads(block["content"])["reflection"])
        assert reflection["cause"] == field
