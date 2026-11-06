"""Agent Loop 命令行入口。

用法：
    uv run python -m scripts.run_agent '计算 128 乘以 36'

链路：
    加载配置 → 创建或恢复会话 → 读取历史并执行 Loop → 持久化结果。
    模型和数据库客户端在退出时关闭；echo 模式只回显输入，不触发工具调用。
"""

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack
from uuid import uuid4

from server.agent_loop import AgentLoop
from server.cache import SessionCache, create_redis
from server.config import get_settings
from server.conversations import ConversationService
from server.llm import LLMClient
from server.store._db import Database
from server.store.sessions import SessionStore
from server.store.todos import TodoStore
from server.tools import create_default_tools
from skills._shared.context import ToolContext

LOCAL_DEMO_USER_ID = "local-user"


async def main() -> None:
    """读取命令行输入，运行 Agent 并输出最终文本及停止状态。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="运行一次带工具的 Agent")
    parser.add_argument("message", nargs="?", default="你好")
    parser.add_argument("--user-id", default=LOCAL_DEMO_USER_ID)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--session-title", default="命令行会话")
    parser.add_argument("--stateless", action="store_true", help="仅调试 Loop，不保存对话历史")
    arguments = parser.parse_args()
    settings = get_settings()
    if not arguments.stateless and not settings.mysql_enabled:
        parser.error("持久化会话需要启用 MySQL；只调试 Loop 时可显式使用 --stateless。")
    async with AsyncExitStack() as stack:
        client = LLMClient(settings)
        stack.push_async_callback(client.close)
        database = Database(settings) if settings.mysql_enabled else None
        if database is not None:
            stack.push_async_callback(database.close)
        todo_store = TodoStore(database.session_factory) if database is not None else None
        tools = create_default_tools(todo_store=todo_store)
        loop = AgentLoop(client, tools)
        if arguments.stateless:
            context = ToolContext(
                user_id=arguments.user_id, session_id=arguments.session_id or uuid4().hex
            )
            result = await loop.run(arguments.message, context=context)
            session_id = context.session_id
        else:
            redis = create_redis(settings)
            stack.push_async_callback(redis.aclose)
            store = SessionStore(database.session_factory)
            if arguments.session_id is None:
                info = await store.create(arguments.user_id, arguments.session_title)
            else:
                info = await store.get(arguments.user_id, arguments.session_id)
            session_id = info.id
            print(f"[session_id={session_id}]")
            service = ConversationService(
                store,
                SessionCache(
                    redis,
                    ttl_seconds=settings.session_cache_ttl_seconds,
                ),
                loop,
                timeout_seconds=settings.agent_run_timeout_seconds,
                recent_turns=settings.context_recent_turns,
            )
            result = await service.chat(
                arguments.user_id,
                session_id,
                arguments.message,
            )
        print(result.text)
        print(f"[stop_reason={result.stop_reason}, rounds={result.rounds}]")
        if arguments.stateless:
            print(f"[session_id={session_id}, stateless=true]")


if __name__ == "__main__":
    asyncio.run(main())
