"""Agent Loop 命令行入口。

用法：
    uv run python -m scripts.run_agent '计算 128 乘以 36'

链路：
    加载当前环境配置 → 创建模型客户端与工具集合 → 执行一次 Loop → 输出结果。
    模型客户端在 finally 中关闭；echo 模式只回显输入，不触发工具调用。
"""

import asyncio
import logging
import sys

from server.agent_loop import AgentLoop
from server.config import get_settings
from server.llm import LLMClient
from server.tools import create_default_tools


async def main() -> None:
    """读取命令行输入，运行 Agent 并输出最终文本及停止状态。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    user_input = " ".join(sys.argv[1:]) or "你好"
    client = LLMClient(get_settings())
    try:
        result = await AgentLoop(client, create_default_tools()).run(user_input)
        print(result.text)
        print(f"[stop_reason={result.stop_reason}, rounds={result.rounds}]")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
