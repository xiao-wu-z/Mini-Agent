"""按项目约定将会话表命名为 agent_session，保留已有记录。

Revision ID: 0005_session_table_name
Revises: 0004_mt_agent_table_names
"""

from alembic import op

revision = "0005_session_table_name"
down_revision = "0004_mt_agent_table_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("agent_conversation", "agent_session")


def downgrade() -> None:
    op.rename_table("agent_session", "agent_conversation")
