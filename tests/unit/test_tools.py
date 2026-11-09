"""工具真实实现与 Loop 闭环测试，数据库使用独立的临时 SQLite 文件。"""

import json
from collections import deque
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.agent_loop import AgentLoop
from server.llm import LLMTurn, ToolUse
from server.skill_runner import SkillRunner
from server.store.base import Base
from server.store.todos import TodoStore
from server.tools import create_default_tools
from skills._shared.context import ToolContext
from skills._shared.errors import SkillErrorCode
from skills.search.handler import SearchArgs
from skills.todo.handler import TodoArgs
from skills.weather.handler import LOCAL_TIMEZONE


@pytest.fixture
async def todo_store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'todos.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield TodoStore(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


def scope(user="user-a", session="window-1", call_id="call-1"):
    return ToolContext(user_id=user, session_id=session, run_id="run-1", tool_use_id=call_id)


async def invoke(name, arguments, *, store=None, context=None):
    call = ToolUse(id="tool-call", name=name, input=arguments)
    feedback = await SkillRunner(create_default_tools(todo_store=store)).execute(
        call, timeout=1, context=context
    )
    return feedback, json.loads(feedback["content"])


def test_all_tools_are_registered_with_schemas():
    specs = create_default_tools().specs()
    assert [spec["name"] for spec in specs] == [
        "calculator",
        "search",
        "todo",
        "weather",
        "read_tool_result",
    ]
    for spec in specs:
        assert spec["description"]
        assert spec["input_schema"]["additionalProperties"] is False
    todo_schema = next(spec["input_schema"] for spec in specs if spec["name"] == "todo")
    assert "user_id" not in todo_schema["properties"]
    assert "session_id" not in todo_schema["properties"]


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("add", 15),
        ("subtract", 9),
        ("multiply", 36),
        ("divide", 4),
    ],
)
async def test_calculator_operations(operation, expected):
    _, result = await invoke("calculator", {"operation": operation, "left": 12, "right": 3})
    assert result == {"ok": True, "data": {"result": expected}}


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "divide", "left": 1, "right": 0},
        {"operation": "divide", "left": 1e100, "right": 1e-300},
        {"operation": "add", "left": "__import__('os')", "right": 1},
        {"operation": "add", "left": float("inf"), "right": 1},
    ],
)
async def test_calculator_rejects_invalid_or_nonfinite_results(arguments):
    feedback, result = await invoke("calculator", arguments)
    assert feedback["is_error"] is True
    assert result["error"]["code"] == SkillErrorCode.INVALID_ARGUMENTS


async def test_search_results_are_mock_and_unmatched_queries_are_empty():
    _, result = await invoke("search", {"query": "FastAPI是什么", "limit": 1})
    assert result["data"]["is_mock"] is True
    assert len(result["data"]["items"]) == 1
    assert result["data"]["items"][0]["url"].startswith("mock://")
    _, result = await invoke("search", {"query": "不存在的查询词"})
    assert result["data"]["items"] == []


async def test_weather_is_marked_mock_and_resolves_tomorrow():
    today = datetime.now(LOCAL_TIMEZONE).date()
    _, result = await invoke("weather", {"city": "杭州市", "day": "tomorrow"})
    data = result["data"]
    assert data["is_mock"] is True
    assert data["city"] == "杭州"
    assert data["date"] == (today + timedelta(days=1)).isoformat()
    assert data["condition"] == "小雨"
    _, error = await invoke("weather", {"city": "虚构城市"})
    assert error["error"]["code"] == SkillErrorCode.LOCATION_NOT_SUPPORTED


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "create"},
        {"action": "create", "title": "   "},
        {"action": "complete"},
        {"action": "delete", "todo_id": "not-an-id"},
        {"action": "list", "user_id": "another-user"},
        {"action": "create", "title": "标题", "status": "completed"},
        {"action": "list", "limit": 0},
    ],
)
def test_todo_parameters_validate_before_execution(arguments):
    with pytest.raises(ValidationError):
        TodoArgs.model_validate(arguments)


def test_blank_search_is_rejected():
    with pytest.raises(ValidationError):
        SearchArgs(query="  ")


async def test_todo_crud_and_completion_filter(todo_store):
    context = scope()
    _, created = await invoke(
        "todo", {"action": "create", "title": "带伞"}, store=todo_store, context=context
    )
    item = created["data"]["item"]
    todo_id = item["id"]
    assert item["status"] == "pending"
    assert item["created_at"].endswith("Z")
    _, listed = await invoke("todo", {"action": "list"}, store=todo_store, context=context)
    assert [row["id"] for row in listed["data"]["items"]] == [todo_id]
    _, completed = await invoke(
        "todo", {"action": "complete", "todo_id": todo_id}, store=todo_store, context=context
    )
    assert completed["data"]["item"]["status"] == "completed"
    _, completed_again = await invoke(
        "todo", {"action": "complete", "todo_id": todo_id}, store=todo_store, context=context
    )
    assert completed_again == completed
    _, pending = await invoke(
        "todo", {"action": "list", "status": "pending"}, store=todo_store, context=context
    )
    assert pending["data"]["items"] == []
    _, deleted = await invoke(
        "todo", {"action": "delete", "todo_id": todo_id}, store=todo_store, context=context
    )
    assert deleted["data"]["deleted"] is True
    _, missing = await invoke(
        "todo", {"action": "complete", "todo_id": todo_id}, store=todo_store, context=context
    )
    assert missing["error"]["code"] == SkillErrorCode.NOT_FOUND


async def test_todo_isolates_windows_and_users(todo_store):
    _, created = await invoke(
        "todo", {"action": "create", "title": "窗口一的待办"}, store=todo_store, context=scope()
    )
    todo_id = created["data"]["item"]["id"]
    for other_context in (scope(session="window-2"), scope(user="user-b"), scope(user="USER-A")):
        _, listed = await invoke(
            "todo", {"action": "list"}, store=todo_store, context=other_context
        )
        assert listed["data"]["items"] == []
        for action in ("complete", "delete"):
            _, rejected = await invoke(
                "todo",
                {"action": action, "todo_id": todo_id},
                store=todo_store,
                context=other_context,
            )
            assert rejected["error"]["code"] == SkillErrorCode.NOT_FOUND
    _, listed = await invoke("todo", {"action": "list"}, store=todo_store, context=scope())
    assert listed["data"]["items"][0]["status"] == "pending"


async def test_create_replay_is_idempotent_and_detects_conflicts(todo_store):
    arguments = {"action": "create", "title": "只创建一次"}
    _, first = await invoke("todo", arguments, store=todo_store, context=scope())
    _, replay = await invoke("todo", arguments, store=todo_store, context=scope())
    assert first == replay
    _, conflict = await invoke(
        "todo", {"action": "create", "title": "同一调用换了内容"}, store=todo_store, context=scope()
    )
    assert conflict["error"]["code"] == SkillErrorCode.CONFLICT
    _, listed = await invoke("todo", {"action": "list"}, store=todo_store, context=scope())
    assert len(listed["data"]["items"]) == 1


async def test_todo_pagination_and_store_recreation(todo_store):
    for index in range(3):
        await invoke(
            "todo",
            {"action": "create", "title": f"任务{index}"},
            store=todo_store,
            context=scope(call_id=f"create-{index}"),
        )
    recreated = TodoStore(todo_store.session_factory)
    _, first = await invoke(
        "todo", {"action": "list", "limit": 2}, store=recreated, context=scope()
    )
    _, last = await invoke(
        "todo", {"action": "list", "limit": 2, "offset": 2}, store=recreated, context=scope()
    )
    assert first["data"]["has_more"] is True
    assert last["data"]["has_more"] is False
    assert len(first["data"]["items"]) + len(last["data"]["items"]) == 3


async def test_todo_requires_trusted_context_and_database(todo_store):
    _, no_context = await invoke("todo", {"action": "list"}, store=todo_store)
    assert no_context["error"]["code"] == SkillErrorCode.CONTEXT_REQUIRED
    _, no_store = await invoke("todo", {"action": "list"}, context=scope())
    assert no_store["error"]["code"] == SkillErrorCode.DEPENDENCY_UNAVAILABLE
    _, no_call_id = await invoke(
        "todo",
        {"action": "create", "title": "未写入"},
        store=todo_store,
        context=ToolContext(user_id="user-a", session_id="window-1"),
    )
    assert no_call_id["error"]["code"] == SkillErrorCode.CONTEXT_REQUIRED


async def test_storage_failure_does_not_claim_success():
    store = AsyncMock(spec=TodoStore)
    store.create.side_effect = RuntimeError("private-database-password")
    feedback, result = await invoke(
        "todo", {"action": "create", "title": "待办"}, store=store, context=scope()
    )
    assert feedback["is_error"] is True
    assert result["error"]["code"] == SkillErrorCode.TOOL_ERROR
    assert "private" not in feedback["content"]


async def test_weather_to_todo_loop_and_tool_followup(todo_store):
    turns = deque(
        [
            LLMTurn(
                content=[
                    {
                        "type": "tool_use",
                        "id": "weather-call",
                        "name": "weather",
                        "input": {"city": "杭州", "day": "tomorrow"},
                    }
                ],
                stop_reason="tool_use",
            ),
            LLMTurn(
                content=[
                    {
                        "type": "tool_use",
                        "id": "todo-call",
                        "name": "todo",
                        "input": {"action": "create", "title": "明天带伞"},
                    }
                ],
                stop_reason="tool_use",
            ),
            LLMTurn(
                content=[{"type": "text", "text": "模拟天气为小雨，已记录带伞待办。"}],
                stop_reason="end_turn",
            ),
            LLMTurn(
                content=[
                    {
                        "type": "tool_use",
                        "id": "list-call",
                        "name": "todo",
                        "input": {"action": "list"},
                    }
                ],
                stop_reason="tool_use",
            ),
            LLMTurn(content=[{"type": "text", "text": "待办：明天带伞。"}], stop_reason="end_turn"),
        ]
    )
    requests = []

    class ScenarioLLM:
        async def run_turn(self, messages, *, tools, system, max_tokens=None):
            requests.append([message.model_dump() for message in messages])
            return turns.popleft()

    loop = AgentLoop(ScenarioLLM(), create_default_tools(todo_store=todo_store))
    first = await loop.run("查杭州明天天气，记待办带伞", context=scope())
    followup = await loop.run("我的待办有哪些？", history=first.messages, context=scope())
    assert [trace.name for trace in first.tool_traces] == ["weather", "todo"]
    assert all(not trace.is_error for trace in first.tool_traces)
    assert json.loads(requests[1][-1]["content"][0]["content"])["data"]["is_mock"] is True
    listed = json.loads(requests[-1][-1]["content"][0]["content"])
    assert listed["data"]["items"][0]["title"] == "明天带伞"
    assert followup.stop_reason == "end_turn"
