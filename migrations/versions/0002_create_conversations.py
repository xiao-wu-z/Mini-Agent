"""创建会话、运行与消息表；DDL 快照见 migrations/sql。

Revision ID: 0002_create_conversations
Revises: 0001_create_todos
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_create_conversations"
down_revision = "0001_create_todos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active_run_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_sessions"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index(
        "ix_agent_sessions_user_updated", "agent_sessions", ["user_id", "updated_at", "id"]
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("session_id", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("tool_results", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_sessions.id"], name="fk_agent_runs_session_id_agent_sessions"
        ),
        sa.UniqueConstraint("session_id", "request_id", name="uq_agent_runs_session_request"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("session_id", sa.String(32), nullable=False),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_messages"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_sessions.id"],
            name="fk_conversation_messages_session_id_agent_sessions",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_runs.id"], name="fk_conversation_messages_run_id_agent_runs"
        ),
        sa.UniqueConstraint("session_id", "sequence", name="uq_conversation_messages_sequence"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index("ix_conversation_messages_run", "conversation_messages", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_conversation_messages_run", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_table("agent_runs")
    op.drop_index("ix_agent_sessions_user_updated", table_name="agent_sessions")
    op.drop_table("agent_sessions")
