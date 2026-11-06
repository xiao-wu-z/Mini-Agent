import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.cache import create_redis
from server.config import Settings, get_settings
from server.llm import LLMClient
from server.routes.health import router as health_router
from server.store._db import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        async with AsyncExitStack() as stack:
            app.state.database = None
            app.state.redis = None
            if settings.mysql_enabled:
                app.state.database = Database(settings)
                stack.push_async_callback(app.state.database.close)
            if settings.redis_enabled:
                app.state.redis = create_redis(settings)
                stack.push_async_callback(app.state.redis.aclose)
            app.state.llm = LLMClient(settings)
            stack.push_async_callback(app.state.llm.close)
            yield

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(health_router)
    return app
