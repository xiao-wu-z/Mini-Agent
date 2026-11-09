"""持久化滚动摘要与工具结果引用，保留原始消息历史。

Revision ID: 0007_context_results
Revises: 0006_task_checkpoint
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_context_results"
down_revision = "0006_task_checkpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加会话工作视图及独立结果原文，已有消息不重写、不删除。"""
    op.add_column("agent_session", sa.Column("context_state", sa.JSON(), nullable=True))
    op.create_table(
        "agent_tool_result",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(32), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_tool_result"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_session.id"],
            name="fk_agent_tool_result_session_id_agent_session",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index("ix_agent_tool_result_scope", "agent_tool_result", ["user_id", "session_id"])


def downgrade() -> None:
    """回退会删除摘要和引用原文；原始对话消息仍保留。"""
    op.drop_table("agent_tool_result")
    op.drop_column("agent_session", "context_state")
