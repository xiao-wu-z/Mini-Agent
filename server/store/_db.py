"""异步 MySQL 连接池；session 由 FastAPI 请求依赖提供。"""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.engine = create_async_engine(
            settings.mysql_url,
            pool_pre_ping=True,
            pool_size=settings.mysql_pool_size,
            max_overflow=settings.mysql_max_overflow,
            pool_recycle=settings.mysql_pool_recycle,
            connect_args={"connect_timeout": settings.mysql_connect_timeout},
            hide_parameters=True,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self.engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    database: Database | None = request.app.state.database
    if database is None:
        raise RuntimeError("MySQL is disabled in this environment")
    async with database.session_factory() as session, session.begin():
        yield session
