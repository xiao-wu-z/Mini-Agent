"""独立的请求 Trace 和步骤 Span；业务消息与检查点不迁移、不重写。

Revision ID: 0008_trace_spans
Revises: 0007_context_results
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_trace_spans"
down_revision = "0007_context_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建埋点表及检索索引，不在服务启动时隐式建表。"""
    op.create_table(
        "agent_trace",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=True),
        sa.Column("checkpoint_id", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stop_reason", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("input_summary", sa.Text(), nullable=True),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=False),
        sa.Column("request_tokens", sa.Integer(), nullable=False),
        sa.Column("usage_estimated", sa.Boolean(), nullable=False),
        sa.Column("dropped_spans", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_trace"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_session.id"], name="fk_agent_trace_session_id_agent_session"
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index(
        "ix_agent_trace_scope_started", "agent_trace", ["user_id", "session_id", "started_at", "id"]
    )
    op.create_index("ix_agent_trace_task_started", "agent_trace", ["task_id", "started_at", "id"])
    op.create_table(
        "agent_span",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("parent_span_id", sa.String(length=32), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("input_summary", sa.Text(), nullable=True),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("stop_reason", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("tool_use_id", sa.String(length=200), nullable=True),
        sa.Column("operation_id", sa.String(length=32), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=False),
        sa.Column("usage_estimated", sa.Boolean(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_span"),
        sa.ForeignKeyConstraint(
            ["trace_id"], ["agent_trace.id"], name="fk_agent_span_trace_id_agent_trace"
        ),
        sa.UniqueConstraint("trace_id", "sequence", name="uq_agent_span_trace_sequence"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index("ix_agent_span_parent", "agent_span", ["trace_id", "parent_span_id"])


def downgrade() -> None:
    """删除埋点记录，不影响会话、历史、工具结果或任务恢复状态。"""
    op.drop_table("agent_span")
    op.drop_table("agent_trace")
