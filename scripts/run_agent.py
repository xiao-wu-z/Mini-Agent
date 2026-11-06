"""Agent Loop 命令行入口。

用法：
    uv run python -m scripts.run_agent '计算 128 乘以 36'

链路：
    加载当前环境配置 → 创建模型客户端与工具集合 → 执行一次 Loop → 输出结果。
    模型和数据库客户端在退出时关闭；echo 模式只回显输入，不触发工具调用。
"""

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack
from uuid import uuid4

from server.agent_loop import AgentLoop
from server.config import get_settings
from server.llm import LLMClient
from server.store._db import Database
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
    arguments = parser.parse_args()
    settings = get_settings()
    context = ToolContext(user_id=arguments.user_id, session_id=arguments.session_id or uuid4().hex)
    async with AsyncExitStack() as stack:
        client = LLMClient(settings)
        stack.push_async_callback(client.close)
        database = Database(settings) if settings.mysql_enabled else None
        if database is not None:
            stack.push_async_callback(database.close)
        todo_store = TodoStore(database.session_factory) if database is not None else None
        tools = create_default_tools(todo_store=todo_store)
        result = await AgentLoop(client, tools).run(arguments.message, context=context)
        print(result.text)
        print(f"[stop_reason={result.stop_reason}, rounds={result.rounds}]")
        print(f"[session_id={context.session_id}]")


if __name__ == "__main__":
    asyncio.run(main())
