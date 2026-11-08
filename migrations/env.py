"""Alembic 使用与服务一致的 APP_ENV / dotenv 配置。"""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from server.config import get_settings
from server.store import (
    sessions,  # noqa: F401 -- 注册会话与消息模型。
    todos,  # noqa: F401 -- 注册 ORM 模型，供 autogenerate 读取 metadata。
    tool_results,  # noqa: F401 -- 注册独立工具结果原文模型。
)
from server.store.base import Base

# 新增模型后，在这里导入对应模块，让 autogenerate 识别 Base.metadata。
target_metadata = Base.metadata
settings = get_settings()
if not settings.mysql_enabled:
    raise RuntimeError("Migrations require AGENT_MYSQL_ENABLED=true and a dedicated database")


def include_object(obj, name, type_, reflected, compare_to):
    # 0003 保留的旧运行记录仅用于审计，不属于当前业务模型。
    return not (type_ == "table" and name == "agent_runs_archive")


def run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    engine = create_async_engine(
        settings.mysql_url,
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": settings.mysql_connect_timeout},
        hide_parameters=True,
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=settings.mysql_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
