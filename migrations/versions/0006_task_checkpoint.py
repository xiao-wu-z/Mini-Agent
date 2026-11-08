"""在会话保存一个当前任务的检查点，不引入运行调度表。

Revision ID: 0006_task_checkpoint
Revises: 0005_session_table_name
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_task_checkpoint"
down_revision = "0005_session_table_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加可空检查点列；已有会话保持原历史，无需回填任务状态。"""
    op.add_column("agent_session", sa.Column("task_state", sa.JSON(), nullable=True))


def downgrade() -> None:
    """移除任务检查点；回退会丢弃未完成任务进度，保留既有消息历史。"""
    op.drop_column("agent_session", "task_state")
