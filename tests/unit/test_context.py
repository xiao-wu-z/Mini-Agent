"""首尾保护、摘要滚动、统一预算和恢复后原始历史提交的回归测试。"""

import json
from collections import deque

import pytest

from server.agent_loop import AgentLoop, TaskState
from server.config import ContextPolicy
from server.context import SUMMARY_SYSTEM, ContextState, message_units, summary_input
from server.llm import LLMTurn, Message, TokenUsage
from server.tools import ToolRegistry


def text(role, content):
    return Message(role=role, content=content)


def tool_pair():
    return [
        Message(
            role="assistant",
            content=[
                {"type": "thinking", "thinking": "private", "signature": "signature"},
                {"type": "tool_use", "id": "a", "name": "x", "input": {}},
                {"type": "tool_use", "id": "b", "name": "x", "input": {}},
            ],
        ),
        Message(
            role="user",
            content=[
                {"type": "tool_result", "tool_use_id": "a", "content": "A"},
                {"type": "tool_result", "tool_use_id": "b", "content": "B", "is_error": True},
            ],
        ),
    ]


def final(content):
    return LLMTurn(
        content=[{"type": "text", "text": content}],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=2),
    )


class CompressionModel:
    def __init__(self, *turns):
        self.turns = deque(turns)
        self.requests = []

    async def count_tokens(self, messages, *, tools, system):
        return len(json.dumps([m.model_dump() for m in messages], ensure_ascii=False)) + len(system)

    async def run_turn(self, messages, **kwargs):
        self.requests.append(([m.model_copy(deep=True) for m in messages], kwargs))
        result = self.turns.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def history():
    return [
        text("user" if index % 2 == 0 else "assistant", f"{index}:" + "资料" * 100)
        for index in range(10)
    ]


def policy(**kwargs):
    return ContextPolicy(
        window_tokens=2400,
        keep_head_units=2,
        keep_tail_units=2,
        summary_max_tokens=64,
        min_interval_seconds=0,
        **kwargs,
    )


def test_first_and_last_cut_points_keep_whole_multi_tool_batches():
    source = [
        text("user", "根目标"),
        *tool_pair(),
        text("assistant", "结论"),
        text("user", "旧问题"),
        text("assistant", "旧答案"),
        *tool_pair(),
        text("assistant", "最新结论"),
    ]
    state = ContextState()
    start, end = state.compression_range(source, policy())
    assert start == 3 and end == 6
    state.summary, state.start, state.end = "中段摘要", start, end
    view = state.render(source, goal="目标", goal_index=0)
    assert view[:3] == source[:3]
    assert view[-3:] == source[-3:]
    assert view[1].content[0]["signature"] == "signature"
    message_units(view)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "wrong", "orphan"])
def test_context_rejects_broken_tool_batches(damage):
    pair = tool_pair()
    if damage == "missing":
        pair.pop()
    elif damage == "duplicate":
        pair[1].content[1]["tool_use_id"] = "a"
    elif damage == "wrong":
        pair[1].content[1]["tool_use_id"] = "other"
    else:
        pair.pop(0)
    with pytest.raises(ValueError):
        message_units(pair)


def test_summary_never_receives_thinking_and_preserves_reference_data():
    pair = tool_pair()
    pair[1].content[0]["content"] = '{"ref":"result-id"}'
    rendered = summary_input("旧摘要", pair)[0].content
    assert "private" not in rendered and "signature" not in rendered
    assert "result-id" in rendered and "旧摘要" in rendered
    assert pair[0].content[0]["thinking"] == "private"


async def test_compression_retains_original_messages_and_uses_shared_budget():
    source = history()
    model = CompressionModel(final("保留事实与约束"), final("答案"))
    loop = AgentLoop(
        model, ToolRegistry(), context_policy=policy(), max_output_tokens=64, token_margin=0
    )
    result = await loop.run("新问题", history=source)
    assert result.text == "答案" and result.rounds == 2
    assert result.total_tokens == 24
    assert model.requests[0][1]["system"] == SUMMARY_SYSTEM
    assert model.requests[0][1]["tools"] == []
    assert model.requests[0][1]["max_tokens"] == 64
    assert result.messages[: len(source)] == source
    assert [m.content for m in result.state.task_messages] == [
        "新问题",
        [{"type": "text", "text": "答案"}],
    ]
    assert result.state.context_state.version == 1
    sent = model.requests[1][0]
    assert sent[:2] == source[:2]
    assert "保留事实与约束" in sent[2].content
    assert sent[-2] == source[-1]
    assert result.state.checkpoint.phase == "completed"


async def test_rolling_summary_merges_old_summary_and_only_new_middle():
    model = CompressionModel(final("第一份摘要"), final("答案"))
    loop = AgentLoop(
        model, ToolRegistry(), context_policy=policy(), max_output_tokens=64, token_margin=0
    )
    first = await loop.run("第一问", history=history())
    old = first.state.context_state
    added = [text("user" if index % 2 == 0 else "assistant", "新增" * 120) for index in range(4)]
    model.turns.extend([final("合并摘要"), final("第二答案")])
    second = await loop.run("第二问", history=first.messages + added, context_state=old)
    compression = json.loads(model.requests[2][0][0].content)
    assert compression["previous_summary"] == "第一份摘要"
    assert compression["new_records"][0]["content"] == first.messages[old.end].content
    assert second.state.context_state.end > old.end
    assert second.state.context_state.start == old.start
    assert second.state.context_state.version == 2
    assert old.summary == "第一份摘要"


async def test_long_task_can_compress_without_losing_task_goal_or_commit_messages():
    source = history()
    model = CompressionModel(final("过程摘要"), final("完成"))
    state = TaskState(
        goal="原始目标", messages=source, task_messages=source, context_state=ContextState()
    )
    loop = AgentLoop(
        model, ToolRegistry(), context_policy=policy(), max_output_tokens=64, token_margin=0
    )
    result = await loop.run("继续", state=state)
    assert result.state.task_messages[: len(source)] == source
    assert result.state.task_messages[-2].content == "继续"
    assert result.state.context_state.version == 1


async def test_no_safe_middle_pauses_instead_of_truncating():
    model = CompressionModel()
    loop = AgentLoop(
        model, ToolRegistry(), context_policy=policy(), max_output_tokens=64, token_margin=0
    )
    result = await loop.run("很长的目标" * 1000)
    assert result.stop_reason == "context_limit"
    assert not model.requests
    assert result.state.task_messages[0].content == "很长的目标" * 1000


@pytest.mark.parametrize(
    "turn",
    [
        final(""),
        RuntimeError("provider failed"),
        LLMTurn(content=[{"type": "text", "text": "截断摘要"}], stop_reason="max_tokens"),
    ],
)
async def test_failed_hard_compression_does_not_publish_summary(turn):
    model = CompressionModel(turn)
    loop = AgentLoop(
        model, ToolRegistry(), context_policy=policy(), max_output_tokens=64, token_margin=0
    )
    result = await loop.run("问题", history=history())
    assert result.stop_reason == "compression_failed"
    assert result.state.context_state.version == 0
    assert not result.state.context_state.summary
    assert len(model.requests) == 1


async def test_summary_call_counts_toward_round_limit_and_resume_reuses_checkpoint():
    model = CompressionModel(final("摘要"), final("答案"))
    loop = AgentLoop(
        model,
        ToolRegistry(),
        context_policy=policy(),
        max_rounds=1,
        max_output_tokens=64,
        token_margin=0,
    )
    paused = await loop.run("问题", history=history())
    assert paused.stop_reason == "max_rounds"
    assert paused.state.context_state.summary == "摘要"
    snapshot = TaskState.model_validate_json(paused.state.model_dump_json())
    resumed = await loop.run("继续", state=snapshot)
    assert resumed.text == "答案"
    assert resumed.total_tokens == 24 and resumed.request_tokens == 12
    assert resumed.state.context_state.version == 1
    assert resumed.state.checkpoint.version > paused.state.checkpoint.version
    assert resumed.state.checkpoint.id != paused.state.checkpoint.id


async def test_soft_failure_uses_old_view_when_capacity_allows():
    model = CompressionModel(RuntimeError("summary unavailable"), final("正常回答"))
    selected = policy(soft_ratio=0.4, hard_ratio=0.95).model_copy(update={"window_tokens": 4000})
    loop = AgentLoop(
        model, ToolRegistry(), context_policy=selected, max_output_tokens=64, token_margin=0
    )
    result = await loop.run("问题", history=history())
    assert result.text == "正常回答" and result.rounds == 2
    assert result.state.context_state.version == 0
    assert result.messages[:10] == history()


@pytest.mark.parametrize("hard,expected_calls", [(False, 1), (True, 2)])
async def test_soft_cooldown_is_bypassed_at_hard_threshold(hard, expected_calls):
    import time

    context = ContextState(summary="已有摘要", start=2, end=3, version=1, compressed_at=time.time())
    selected = policy(soft_ratio=0.4, hard_ratio=0.85 if hard else 0.95)
    if not hard:
        selected.window_tokens = 4000
    selected.min_interval_seconds = 3600
    model = CompressionModel(*([final("新摘要"), final("答案")] if hard else [final("答案")]))
    result = await AgentLoop(
        model, ToolRegistry(), context_policy=selected, max_output_tokens=64, token_margin=0
    ).run("问题", history=history(), context_state=context)
    assert result.text == "答案"
    assert len(model.requests) == expected_calls
    assert result.state.context_state.version == (2 if hard else 1)


async def test_summary_that_does_not_reduce_size_keeps_old_state():
    model = CompressionModel(final("比原文还长" * 1000))
    result = await AgentLoop(
        model, ToolRegistry(), context_policy=policy(), max_output_tokens=64, token_margin=0
    ).run("问题", history=history())
    assert result.stop_reason == "compression_ineffective"
    assert result.state.context_state.version == 0


def test_current_goal_is_retained_verbatim_when_its_source_is_summarized():
    source = [
        text("user", "会话开头"),
        text("assistant", "开头回复"),
        text("user", "当前任务的精确约束"),
        text("assistant", "中间进展"),
        text("user", "最近追问"),
    ]
    context = ContextState(summary="进展摘要", start=2, end=4, version=1)
    view = context.render(source, goal="当前任务的精确约束", goal_index=2)
    assert "当前任务的精确约束" in view[2].content
    assert view[:2] == source[:2] and view[-1] == source[-1]


def test_external_result_view_cannot_change_error_status_or_call_ids():
    source = tool_pair()
    view = [dict(block) for block in source[1].content]
    view[1]["is_error"] = False
    with pytest.raises(ValueError):
        ContextState(result_views={1: view}).projected(source)


def test_nested_context_configuration_from_environment(monkeypatch):
    from server.config import get_settings

    monkeypatch.setenv("AGENT_CONTEXT__KEEP_HEAD_UNITS", "3")
    monkeypatch.setenv("AGENT_CONTEXT__WINDOW_TOKENS", "48000")
    monkeypatch.setenv("AGENT_TOOL_RESULT__INLINE_MAX_BYTES", "4096")
    settings = get_settings()
    assert settings.context.keep_head_units == 3
    assert settings.context.window_tokens == 48000
    assert settings.tool_result.inline_max_bytes == 4096


def test_invalid_context_thresholds_are_rejected():
    with pytest.raises(ValueError, match="soft_ratio"):
        ContextPolicy(soft_ratio=0.9, hard_ratio=0.8)
