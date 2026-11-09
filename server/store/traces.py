"""Trace 与 Span 的 MySQL 存储和按会话归属过滤的查询。

链路：
    请求创建 Trace → 步骤开始时插入 Span → 各自结束时短事务更新
    → 按用户、Session、Task 查询请求列表 → 读取单条 Trace 的有序步骤。

边界：
    埋点事务独立于业务事务，超时与故障降级由 TraceRecorder 统一处理。
    查询不会把未收尾记录改成成功；未结束可能意味着仍在执行或进程中断。
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from server.store.base import Base
from server.store.sessions import ENTITY_ID_LENGTH, MYSQL_OPTIONS
from skills._shared.context import MAX_IDENTITY_LENGTH

TRACE_NAME_LENGTH = 100
TRACE_STATE_LENGTH = 32
TRACE_MODEL_LENGTH = 200
TRACE_CODE_LENGTH = 100
MAX_TRACE_QUERY_SPANS = 1000

# ============================================================================
# 请求与步骤实体
# ============================================================================


class TraceModel(Base):
    """一次用户请求的执行概况；继续请求沿用 task_id，新建 trace id。"""

    __tablename__ = "agent_trace"
    __table_args__ = (
        Index("ix_agent_trace_scope_started", "user_id", "session_id", "started_at", "id"),
        Index("ix_agent_trace_task_started", "task_id", "started_at", "id"),
        MYSQL_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ENTITY_ID_LENGTH), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(MAX_IDENTITY_LENGTH), nullable=False)
    session_id: Mapped[str] = mapped_column(
        String(ENTITY_ID_LENGTH), ForeignKey("agent_session.id"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(String(ENTITY_ID_LENGTH))
    checkpoint_id: Mapped[str | None] = mapped_column(String(ENTITY_ID_LENGTH))
    action: Mapped[str] = mapped_column(String(TRACE_STATE_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(TRACE_STATE_LENGTH), nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(TRACE_CODE_LENGTH))
    started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime())
    duration_ms: Mapped[float | None] = mapped_column(Float())
    input_summary: Mapped[str | None] = mapped_column(Text())
    output_summary: Mapped[str | None] = mapped_column(Text())
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    request_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    usage_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    dropped_spans: Mapped[int] = mapped_column(Integer, nullable=False)


class SpanModel(Base):
    """可嵌套的执行步骤；generation、普通 span 和 event 共用一张表。"""

    __tablename__ = "agent_span"
    __table_args__ = (
        UniqueConstraint("trace_id", "sequence", name="uq_agent_span_trace_sequence"),
        Index("ix_agent_span_parent", "trace_id", "parent_span_id"),
        MYSQL_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(ENTITY_ID_LENGTH), primary_key=True)
    trace_id: Mapped[str] = mapped_column(
        String(ENTITY_ID_LENGTH), ForeignKey("agent_trace.id"), nullable=False
    )
    parent_span_id: Mapped[str | None] = mapped_column(String(ENTITY_ID_LENGTH))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(TRACE_STATE_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(TRACE_NAME_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(TRACE_STATE_LENGTH), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime())
    duration_ms: Mapped[float | None] = mapped_column(Float())
    input_summary: Mapped[str | None] = mapped_column(Text())
    output_summary: Mapped[str | None] = mapped_column(Text())
    error_code: Mapped[str | None] = mapped_column(String(TRACE_CODE_LENGTH))
    stop_reason: Mapped[str | None] = mapped_column(String(TRACE_CODE_LENGTH))
    model: Mapped[str | None] = mapped_column(String(TRACE_MODEL_LENGTH))
    tool_use_id: Mapped[str | None] = mapped_column(String(TRACE_MODEL_LENGTH))
    operation_id: Mapped[str | None] = mapped_column(String(ENTITY_ID_LENGTH))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    usage_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False)


# ============================================================================
# 独立短事务与有界查询
# ============================================================================


def public_row(row: TraceModel | SpanModel) -> dict[str, Any]:
    """序列化受控字段，并为 UTC 数据库时间补充明确时区。"""
    data = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    for name in ("started_at", "ended_at"):
        if data[name] is not None:
            data[name] = data[name].replace(tzinfo=UTC)
    data["incomplete"] = row.ended_at is None
    return data


class TraceStore:
    """不持有业务事务；所有查询都要求用户与 Session 匹配。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def create_trace(self, values: dict) -> None:
        """插入请求起点，不等待整个请求结束后才记录。"""
        async with self.session_factory() as session, session.begin():
            session.add(TraceModel(**values))

    async def update_trace(self, trace_id: str, values: dict) -> None:
        """独立提交关联信息、用量或最终状态。"""
        async with self.session_factory() as session, session.begin():
            await session.execute(
                update(TraceModel).where(TraceModel.id == trace_id).values(**values)
            )

    async def create_span(self, values: dict) -> None:
        """先保存步骤开始记录，进程退出后仍可看到未完成的步骤。"""
        async with self.session_factory() as session, session.begin():
            session.add(SpanModel(**values))

    async def update_span(self, span_id: str, values: dict) -> None:
        """更新步骤结果，不参与工具或消息的事务提交。"""
        async with self.session_factory() as session, session.begin():
            await session.execute(update(SpanModel).where(SpanModel.id == span_id).values(**values))

    async def list_traces(
        self, user_id: str, session_id: str, *, task_id: str | None, limit: int, offset: int
    ) -> list[dict]:
        """按开始时间倒序分页，可在同一窗口内筛选一个任务的多次请求。"""
        query = select(TraceModel).where(
            TraceModel.user_id == user_id, TraceModel.session_id == session_id
        )
        if task_id is not None:
            query = query.where(TraceModel.task_id == task_id)
        query = query.order_by(TraceModel.started_at.desc(), TraceModel.id.desc())
        async with self.session_factory() as session:
            rows = await session.scalars(query.limit(limit).offset(offset))
            return [public_row(row) for row in rows]

    async def detail(self, user_id: str, session_id: str, trace_id: str) -> dict | None:
        """返回请求与步骤树；父步骤缺失时保留子步骤，不掩盖埋点写入缺口。"""
        async with self.session_factory() as session:
            trace = await session.scalar(
                select(TraceModel).where(
                    TraceModel.id == trace_id,
                    TraceModel.user_id == user_id,
                    TraceModel.session_id == session_id,
                )
            )
            if trace is None:
                return None
            rows = await session.scalars(
                select(SpanModel)
                .where(SpanModel.trace_id == trace_id)
                .order_by(SpanModel.sequence)
                .limit(MAX_TRACE_QUERY_SPANS + 1)
            )
            spans = [public_row(row) for row in rows]
        truncated = len(spans) > MAX_TRACE_QUERY_SPANS
        spans = spans[:MAX_TRACE_QUERY_SPANS]
        nodes = {span["id"]: {**span, "children": []} for span in spans}
        roots = []
        for span in spans:
            node = nodes[span["id"]]
            parent = nodes.get(span["parent_span_id"])
            # 父节点必须先开始，避免损坏数据构成环，也兼容丢失的父埋点。
            if parent is not None and parent["sequence"] < span["sequence"]:
                parent["children"].append(node)
            else:
                roots.append(node)
        return {"trace": public_row(trace), "spans": spans, "tree": roots, "truncated": truncated}
