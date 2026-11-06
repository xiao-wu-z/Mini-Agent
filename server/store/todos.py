"""Todo 持久化：按用户与 Session 隔离，使用短事务保存每次工具操作。

边界：
    不调用模型，不管理对话历史。新增操作使用运行与调用标识生成的幂等键去重。
    业务实体使用随机 ID，对越权 ID 和不存在的 ID 均返回未找到。
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, Index, String, UniqueConstraint, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from server.store.base import Base
from skills._shared.context import MAX_IDENTITY_LENGTH

TODO_ID_LENGTH = 32
CREATE_KEY_LENGTH = 64
MAX_TITLE_LENGTH = 500
STATUS_LENGTH = 16
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class TodoStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"


def utc_now() -> datetime:
    """MySQL DATETIME 统一保存 UTC，无时区偏移；对外输出时明确附加 UTC。"""
    return datetime.now(UTC).replace(tzinfo=None)


class TodoModel(Base):
    """待办数据库实体；create_key 仅用于同一次创建调用的去重。"""

    __tablename__ = "todos"
    __table_args__ = (
        UniqueConstraint("user_id", "session_id", "create_key", name="uq_todos_scope_create_key"),
        Index("ix_todos_scope_created", "user_id", "session_id", "created_at", "id"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_bin"},
    )

    id: Mapped[str] = mapped_column(String(TODO_ID_LENGTH), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(MAX_IDENTITY_LENGTH), nullable=False)
    session_id: Mapped[str] = mapped_column(String(MAX_IDENTITY_LENGTH), nullable=False)
    title: Mapped[str] = mapped_column(String(MAX_TITLE_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(STATUS_LENGTH), nullable=False)
    create_key: Mapped[str] = mapped_column(String(CREATE_KEY_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class TodoItem(BaseModel):
    """可返回给模型的待办视图，不包含其他用户信息或内部幂等键。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    status: TodoStatus
    created_at: datetime
    updated_at: datetime


class TodoConflictError(ValueError):
    """同一个创建调用被用于不同内容，不能静默复用旧结果。"""


def to_item(row: TodoModel) -> TodoItem:
    return TodoItem(
        id=row.id,
        title=row.title,
        status=TodoStatus(row.status),
        created_at=row.created_at.replace(tzinfo=UTC),
        updated_at=row.updated_at.replace(tzinfo=UTC),
    )


class TodoStore:
    """使用异步 session factory，每个方法单独提交或回滚，不持有跨模型调用的事务。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def create(
        self, *, user_id: str, session_id: str, title: str, create_key: str
    ) -> TodoItem:
        """创建待办；唯一键竞争时回读已提交结果，相同调用不会重复写入。"""
        existing_query = select(TodoModel).where(
            TodoModel.user_id == user_id,
            TodoModel.session_id == session_id,
            TodoModel.create_key == create_key,
        )
        async with self.session_factory() as session:
            existing = await session.scalar(existing_query)
            if existing is not None:
                return self._check_replay(existing, title)
        now = utc_now()
        row = TodoModel(
            id=uuid4().hex,
            user_id=user_id,
            session_id=session_id,
            title=title,
            status=TodoStatus.PENDING,
            create_key=create_key,
            created_at=now,
            updated_at=now,
        )
        try:
            async with self.session_factory() as session, session.begin():
                session.add(row)
                await session.flush()
                item = to_item(row)
            return item
        except IntegrityError:
            async with self.session_factory() as session:
                existing = await session.scalar(existing_query)
                if existing is None:
                    raise
                return self._check_replay(existing, title)

    @staticmethod
    def _check_replay(row: TodoModel, title: str) -> TodoItem:
        if row.title != title:
            raise TodoConflictError("Create call was reused with a different title")
        return to_item(row)

    async def list_items(
        self,
        *,
        user_id: str,
        session_id: str,
        status: TodoStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[TodoItem], bool]:
        """稳定分页读取当前窗口的待办，多读一条判断是否还有下一页。"""
        query = select(TodoModel).where(
            TodoModel.user_id == user_id,
            TodoModel.session_id == session_id,
        )
        if status is not None:
            query = query.where(TodoModel.status == status)
        query = query.order_by(TodoModel.created_at, TodoModel.id).offset(offset).limit(limit + 1)
        async with self.session_factory() as session:
            rows = list((await session.scalars(query)).all())
            return [to_item(row) for row in rows[:limit]], len(rows) > limit

    async def complete(self, *, user_id: str, session_id: str, todo_id: str) -> TodoItem | None:
        """完成当前窗口的待办；重复完成保持成功，不更改原完成时间。"""
        query = (
            select(TodoModel)
            .where(
                TodoModel.id == todo_id,
                TodoModel.user_id == user_id,
                TodoModel.session_id == session_id,
            )
            .with_for_update()
        )
        async with self.session_factory() as session, session.begin():
            row = await session.scalar(query)
            if row is None:
                return None
            if row.status != TodoStatus.COMPLETED:
                row.status = TodoStatus.COMPLETED
                row.updated_at = utc_now()
            return to_item(row)

    async def delete(self, *, user_id: str, session_id: str, todo_id: str) -> bool:
        """删除操作在 SQL 条件中同时校验用户与 Session，不能仅凭 ID 修改记录。"""
        statement = delete(TodoModel).where(
            TodoModel.id == todo_id,
            TodoModel.user_id == user_id,
            TodoModel.session_id == session_id,
        )
        async with self.session_factory() as session, session.begin():
            result = await session.execute(statement)
            return result.rowcount == 1
