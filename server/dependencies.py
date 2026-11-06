"""路由通过 Depends 获取由应用生命周期管理的客户端。"""

from fastapi import Request
from redis.asyncio import Redis

from server.llm import LLMClient


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_redis(request: Request) -> Redis:
    client = request.app.state.redis
    if client is None:
        raise RuntimeError("Redis is disabled in this environment")
    return client
