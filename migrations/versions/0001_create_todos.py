"""创建按用户和会话隔离的待办表。

Revision ID: 0001_create_todos
Revises: None
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_create_todos"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "todos",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("create_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_todos"),
        sa.UniqueConstraint(
            "user_id", "session_id", "create_key", name="uq_todos_scope_create_key"
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index(
        "ix_todos_scope_created", "todos", ["user_id", "session_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_todos_scope_created", table_name="todos")
    op.drop_table("todos")
