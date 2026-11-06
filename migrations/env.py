"""Alembic 使用与服务一致的 APP_ENV / dotenv 配置。"""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from server.config import get_settings
from server.store.base import Base

# 新增模型后，在这里 import 对应模块，让 autogenerate 识别 Base.metadata。
target_metadata = Base.metadata
settings = get_settings()
if not settings.mysql_enabled:
    raise RuntimeError("Migrations require AGENT_MYSQL_ENABLED=true and a dedicated database")


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
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
