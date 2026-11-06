"""收敛基础会话：消息按 turn_id 分组，旧运行记录保留为归档。

Revision ID: 0003_simplify_sessions
Revises: 0002_create_conversations
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_simplify_sessions"
down_revision = "0002_create_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_conversation_messages_run_id_agent_runs", "conversation_messages", type_="foreignkey"
    )
    op.drop_index("ix_conversation_messages_run", table_name="conversation_messages")
    op.alter_column(
        "conversation_messages",
        "run_id",
        new_column_name="turn_id",
        existing_type=sa.String(32),
        existing_nullable=False,
    )
    op.create_index("ix_conversation_messages_turn", "conversation_messages", ["turn_id"])
    op.rename_table("agent_runs", "agent_runs_archive")
    op.drop_column("agent_sessions", "active_run_id")


def downgrade() -> None:
    raise RuntimeError(
        "旧运行记录保留在 agent_runs_archive。回退旧运行模型需人工数据映射，不能自动回滚。"
    )
