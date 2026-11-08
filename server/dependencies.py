"""路由通过 Depends 获取由应用生命周期管理的客户端。"""

from fastapi import Request
from redis.asyncio import Redis

from server.llm import LLMClient
from server.tools import ToolRegistry


def get_tools(request: Request) -> ToolRegistry:
    """获取应用工具集合；用户和会话上下文仍由每次 Agent 请求显式提供。"""
    return request.app.state.tools


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_redis(request: Request) -> Redis:
    client = request.app.state.redis
    if client is None:
        raise RuntimeError("Redis is disabled in this environment")
    return client
