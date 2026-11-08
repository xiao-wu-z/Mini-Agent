"""会话和消息表沿用 mt-agent 命名，保留全部数据与已有索引。

Revision ID: 0004_mt_agent_table_names
Revises: 0003_simplify_sessions
"""

from alembic import op

revision = "0004_mt_agent_table_names"
down_revision = "0003_simplify_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("agent_sessions", "agent_conversation")
    op.rename_table("conversation_messages", "agent_message")


def downgrade() -> None:
    op.rename_table("agent_message", "conversation_messages")
    op.rename_table("agent_conversation", "agent_sessions")
