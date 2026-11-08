import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from server.agent_loop import AgentLoop
from server.cache import SessionCache, create_redis
from server.config import Settings, get_settings
from server.conversations import ConversationService
from server.llm import LLMClient
from server.routes.health import router as health_router
from server.routes.sessions import router as sessions_router
from server.store._db import Database
from server.store.sessions import ConversationCode, ConversationError, SessionStore
from server.store.todos import TodoStore
from server.store.tool_results import ToolResultStore
from server.tools import create_default_tools


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
            todo_store = (
                TodoStore(app.state.database.session_factory)
                if app.state.database is not None
                else None
            )
            result_store = (
                ToolResultStore(
                    app.state.database.session_factory,
                    redis=app.state.redis,
                    cache_ttl_seconds=settings.tool_result.cache_ttl_seconds,
                )
                if app.state.database is not None
                else None
            )
            app.state.tools = create_default_tools(todo_store=todo_store, result_store=result_store)
            app.state.conversations = None
            if app.state.database is not None:
                app.state.conversations = ConversationService(
                    SessionStore(app.state.database.session_factory),
                    SessionCache(
                        app.state.redis,
                        ttl_seconds=settings.session_cache_ttl_seconds,
                    ),
                    AgentLoop(
                        app.state.llm,
                        app.state.tools,
                        max_rounds=settings.request_max_steps,
                        tool_timeout=settings.agent_tool_timeout_seconds,
                        request_max_tokens=settings.request_max_tokens,
                        max_output_tokens=min(
                            settings.request_max_output_tokens, settings.llm_max_tokens
                        ),
                        token_margin=settings.token_safety_margin,
                        context_policy=settings.context,
                        result_policy=settings.tool_result,
                        result_store=result_store,
                    ),
                    timeout_seconds=settings.agent_run_timeout_seconds,
                )
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
    app.include_router(sessions_router)

    @app.exception_handler(ConversationError)
    async def conversation_error(request: Request, exc: ConversationError) -> JSONResponse:
        if exc.code == ConversationCode.SESSION_NOT_FOUND:
            code = status.HTTP_404_NOT_FOUND
        elif exc.code in {
            ConversationCode.SESSION_BUSY,
            ConversationCode.TASK_PENDING,
            ConversationCode.TASK_NOT_FOUND,
        }:
            code = status.HTTP_409_CONFLICT
        elif exc.code == ConversationCode.RUN_TIMEOUT:
            code = status.HTTP_504_GATEWAY_TIMEOUT
        else:
            code = status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(status_code=code, content={"code": exc.code, "message": exc.message})

    return app
