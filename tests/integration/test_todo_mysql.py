"""显式启用的真实 MySQL 测试，只操作专用测试库和本次创建的临时数据。"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.store.todos import TodoModel, TodoStatus, TodoStore

TEST_MYSQL_URL_VARIABLE = "MINI_AGENT_TEST_MYSQL_URL"
TEST_DATABASE_SUFFIX = "_test"

pytestmark = pytest.mark.skipif(
    not os.environ.get(TEST_MYSQL_URL_VARIABLE),
    reason="Set MINI_AGENT_TEST_MYSQL_URL explicitly to enable MySQL integration tests",
)


async def test_mysql_persistence_scope_and_idempotency():
    url = make_url(os.environ[TEST_MYSQL_URL_VARIABLE])
    if not url.database or not url.database.endswith(TEST_DATABASE_SUFFIX):
        pytest.fail("Integration tests require a dedicated database ending in _test")
    if url.drivername != "mysql+asyncmy":
        pytest.fail("Integration tests require the mysql+asyncmy driver")
    engine = create_async_engine(url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = TodoStore(factory)
    user_id = f"integration-{uuid4().hex}"
    scope = {"user_id": user_id, "session_id": "window-1"}
    try:
        created = await store.create(**scope, title="MySQL 待办测试", create_key="same-call")
        replayed = await store.create(**scope, title="MySQL 待办测试", create_key="same-call")
        assert created.id == replayed.id
        for other_scope in (
            {"user_id": user_id, "session_id": "window-2"},
            {"user_id": user_id.upper(), "session_id": "window-1"},
        ):
            rows, _ = await store.list_items(**other_scope, status=None, limit=10, offset=0)
            assert rows == []
            assert await store.complete(**other_scope, todo_id=created.id) is None
            assert await store.delete(**other_scope, todo_id=created.id) is False
        completed = await store.complete(**scope, todo_id=created.id)
        assert completed is not None and completed.status == TodoStatus.COMPLETED
        restored = TodoStore(factory)
        rows, _ = await restored.list_items(
            **scope, status=TodoStatus.COMPLETED, limit=10, offset=0
        )
        assert [row.id for row in rows] == [created.id]
        assert await restored.delete(**scope, todo_id=created.id) is True
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(TodoModel).where(TodoModel.user_id == user_id))
        await engine.dispose()
