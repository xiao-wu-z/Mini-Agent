"""会话与历史存储：校验归属、顺序读取、整轮提交；模型调用期间不持有事务。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from server.llm import Message
from server.store.base import Base
from skills._shared.context import MAX_IDENTITY_LENGTH

ENTITY_ID_LENGTH = 32
MAX_SESSION_TITLE_LENGTH = 200
ROLE_LENGTH = 16
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
MYSQL_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_bin"}


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ConversationCode(StrEnum):
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_BUSY = "SESSION_BUSY"
    RUN_FAILED = "RUN_FAILED"
    RUN_TIMEOUT = "RUN_TIMEOUT"


class ConversationError(Exception):
    """服务层可映射为 HTTP 错误的稳定错误码，不包含原始基础设施异常。"""

    def __init__(self, code: ConversationCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SessionModel(Base):
    __tablename__ = "agent_session"
    __table_args__ = (
        Index("ix_agent_sessions_user_updated", "user_id", "updated_at", "id"),
        MYSQL_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ENTITY_ID_LENGTH), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(MAX_IDENTITY_LENGTH), nullable=False)
    title: Mapped[str] = mapped_column(String(MAX_SESSION_TITLE_LENGTH), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class MessageModel(Base):
    __tablename__ = "agent_message"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_conversation_messages_sequence"),
        Index("ix_conversation_messages_turn", "turn_id"),
        MYSQL_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ENTITY_ID_LENGTH), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(ENTITY_ID_LENGTH),
        ForeignKey("agent_session.id", name="fk_conversation_messages_session_id_agent_sessions"),
        nullable=False,
    )
    turn_id: Mapped[str] = mapped_column(String(ENTITY_ID_LENGTH), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(ROLE_LENGTH), nullable=False)
    content: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class SessionInfo(BaseModel):
    id: str
    title: str
    version: int
    created_at: datetime
    updated_at: datetime


def session_info(row: SessionModel) -> SessionInfo:
    return SessionInfo(
        id=row.id,
        title=row.title,
        version=row.version,
        created_at=row.created_at.replace(tzinfo=UTC),
        updated_at=row.updated_at.replace(tzinfo=UTC),
    )


class SessionStore:
    """所有会话读取先校验归属；整轮消息通过短事务保存。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def create(self, user_id: str, title: str) -> SessionInfo:
        now = utc_now()
        row = SessionModel(
            id=uuid4().hex,
            user_id=user_id,
            title=title,
            version=0,
            created_at=now,
            updated_at=now,
        )
        async with self.session_factory() as session, session.begin():
            session.add(row)
            await session.flush()
            return session_info(row)

    async def list_sessions(self, user_id: str, *, limit: int, offset: int) -> list[SessionInfo]:
        query = (
            select(SessionModel)
            .where(SessionModel.user_id == user_id)
            .order_by(
                SessionModel.updated_at.desc(),
                SessionModel.id,
            )
            .offset(offset)
            .limit(limit)
        )
        async with self.session_factory() as session:
            return [session_info(row) for row in (await session.scalars(query)).all()]

    async def get(self, user_id: str, session_id: str) -> SessionInfo:
        async with self.session_factory() as session:
            return session_info(await self._session(session, user_id, session_id))

    @staticmethod
    async def _session(
        session: AsyncSession, user_id: str, session_id: str, *, lock: bool = False
    ) -> SessionModel:
        query = select(SessionModel).where(
            SessionModel.id == session_id,
            SessionModel.user_id == user_id,
        )
        if lock:
            query = query.with_for_update()
        row = await session.scalar(query)
        if row is None:
            raise ConversationError(ConversationCode.SESSION_NOT_FOUND, "会话不存在。")
        return row

    async def load_messages(
        self,
        user_id: str,
        session_id: str,
        *,
        through_version: int,
        after: int = 0,
        limit: int | None = None,
    ) -> list[MessageModel]:
        async with self.session_factory() as session:
            await self._session(session, user_id, session_id)
            query = (
                select(MessageModel)
                .where(
                    MessageModel.session_id == session_id,
                    MessageModel.sequence > after,
                    MessageModel.sequence <= through_version,
                )
                .order_by(MessageModel.sequence)
            )
            if limit is not None:
                query = query.limit(limit)
            return list((await session.scalars(query)).all())

    async def save_turn(
        self,
        user_id: str,
        session_id: str,
        turn_id: str,
        messages: list[Message],
        *,
        expected_version: int,
    ) -> None:
        """整轮消息原子提交，避免存入未配对的 tool_use/tool_result。"""
        async with self.session_factory() as session, session.begin():
            owner = await self._session(session, user_id, session_id, lock=True)
            if owner.version != expected_version:
                raise ConversationError(
                    ConversationCode.SESSION_BUSY, "会话历史已变化，请重新加载。"
                )
            for message in messages:
                owner.version += 1
                session.add(
                    MessageModel(
                        id=uuid4().hex,
                        session_id=session_id,
                        turn_id=turn_id,
                        sequence=owner.version,
                        role=message.role,
                        content=message.content,
                        created_at=utc_now(),
                    )
                )
            owner.updated_at = utc_now()
