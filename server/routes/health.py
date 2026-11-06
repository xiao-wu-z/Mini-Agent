import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health")
async def health() -> dict[str, str]:
    """进程存活探针，不访问外部服务。"""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    """检查已启用的基础设施；不会调用计费模型接口。"""
    checks = {}
    for name, service in (
        ("mysql", request.app.state.database),
        ("redis", request.app.state.redis),
    ):
        if service is None:
            checks[name] = "disabled"
            continue
        try:
            async with asyncio.timeout(5):
                await service.ping()
            checks[name] = "ok"
        except Exception:
            logger.warning("Readiness check failed: %s", name)
            checks[name] = "error"
    healthy = "error" not in checks.values()
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "error", "checks": checks},
    )
