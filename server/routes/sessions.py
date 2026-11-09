"""会话与聊天 HTTP 入口；身份必须由可信调用方提供，不接受模型指定归属。

部署边界：
    X-User-ID 是开发环境/可信网关身份，不是终端用户认证。
    生产入口必须配置服务令牌并由已认证网关注入用户身份，不应直接暴露公网。
"""

import secrets
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.conversations import ConversationService
from server.sse import EventSink, stream_events
from server.store.sessions import (
    DEFAULT_LIST_LIMIT,
    ENTITY_ID_LENGTH,
    MAX_LIST_LIMIT,
    MAX_SESSION_TITLE_LENGTH,
    SessionInfo,
)
from skills._shared.context import MAX_IDENTITY_LENGTH

router = APIRouter(prefix="/sessions", tags=["sessions"])
SessionId = Annotated[str, Path(pattern=rf"^[0-9a-f]{{{ENTITY_ID_LENGTH}}}$")]
MAX_USER_INPUT_LENGTH = 20000
PUBLIC_BLOCK_TYPES = frozenset({"text", "tool_use", "tool_result"})


def current_user(
    request: Request,
    user_id: Annotated[
        str, Header(alias="X-User-ID", min_length=1, max_length=MAX_IDENTITY_LENGTH)
    ],
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    settings = request.app.state.settings
    token = settings.api_token.get_secret_value()
    if settings.app_env == "production" and not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "会话 API 尚未配置网关服务令牌。")
    if token and not secrets.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "服务令牌无效。")
    if not user_id.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "用户身份不能为空。")
    return user_id.strip()


def conversation_service(request: Request) -> ConversationService:
    service = request.app.state.conversations
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "会话服务需要启用 MySQL。")
    return service


User = Annotated[str, Depends(current_user)]
Service = Annotated[ConversationService, Depends(conversation_service)]


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(default="新会话", min_length=1, max_length=MAX_SESSION_TITLE_LENGTH)


class ChatRequest(BaseModel):
    """任务操作请求；人工核实的操作 ID 与模型业务参数分开传递。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(default="继续", min_length=1, max_length=MAX_USER_INPUT_LENGTH)
    action: Literal["new", "continue", "cancel"] = "new"
    confirmed_not_executed: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_new_input(self):
        """新任务必须显式提供目标；继续和取消可以省略消息文本。"""
        if self.action == "new" and "message" not in self.model_fields_set:
            raise ValueError("new requires message")
        return self


@router.post("", response_model=SessionInfo, status_code=status.HTTP_201_CREATED)
async def create_session(body: CreateSession, user_id: User, service: Service) -> SessionInfo:
    return await service.store.create(user_id, body.title)


@router.get("")
async def list_sessions(
    user_id: User,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = DEFAULT_LIST_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    items = await service.store.list_sessions(user_id, limit=limit + 1, offset=offset)
    return {
        "items": items[:limit],
        "has_more": len(items) > limit,
        "offset": offset,
        "limit": limit,
    }


@router.get("/{session_id}", response_model=SessionInfo)
async def get_session(session_id: SessionId, user_id: User, service: Service) -> SessionInfo:
    return await service.store.get(user_id, session_id)


@router.get("/{session_id}/messages")
async def messages(
    session_id: SessionId,
    user_id: User,
    service: Service,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = DEFAULT_LIST_LIMIT,
) -> dict[str, Any]:
    info = await service.store.get(user_id, session_id)
    records = await service.store.load_messages(
        user_id,
        session_id,
        through_version=info.version,
        after=after,
        limit=limit + 1,
    )
    items = []
    for row in records[:limit]:
        content = row.content
        if isinstance(content, list):
            content = [block for block in content if block.get("type") in PUBLIC_BLOCK_TYPES]
        items.append(
            {"sequence": row.sequence, "turn_id": row.turn_id, "role": row.role, "content": content}
        )
    return {
        "items": items,
        "has_more": len(records) > limit,
        "next_after": items[-1]["sequence"] if items else after,
        "version": info.version,
    }


@router.post(
    "/{session_id}/chat",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def chat(
    session_id: SessionId,
    body: ChatRequest,
    user_id: User,
    service: Service,
    request: Request,
) -> StreamingResponse:
    """以 SSE 推送公开文本、工具状态和最终结果；断连后取消执行并保存恢复状态。"""
    await service.store.get(user_id, session_id)

    async def run(on_event: EventSink):
        return await service.chat(
            user_id,
            session_id,
            body.message,
            action=body.action,
            confirmed_not_executed=body.confirmed_not_executed,
            on_event=on_event,
        )

    return StreamingResponse(
        stream_events(run, heartbeat_seconds=request.app.state.settings.sse_heartbeat_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{session_id}/task")
async def current_task(session_id: SessionId, user_id: User, service: Service) -> dict:
    """查询当前窗口的任务摘要；归属校验与检查点过滤由会话服务统一处理。"""
    return await service.task(user_id, session_id)


@router.get("/{session_id}/traces")
async def list_traces(
    session_id: SessionId,
    user_id: User,
    service: Service,
    task_id: Annotated[str | None, Query(pattern=rf"^[0-9a-f]{{{ENTITY_ID_LENGTH}}}$")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = DEFAULT_LIST_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """按窗口查询请求记录，支持 task_id 筛选，复用现有网关身份与归属校验。"""
    await service.store.get(user_id, session_id)
    if service.trace_store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Trace 查询未启用。")
    items = await service.trace_store.list_traces(
        user_id, session_id, task_id=task_id, limit=limit + 1, offset=offset
    )
    return {
        "items": items[:limit],
        "has_more": len(items) > limit,
        "offset": offset,
        "limit": limit,
    }


@router.get("/{session_id}/traces/{trace_id}")
async def trace_detail(
    session_id: SessionId,
    trace_id: SessionId,
    user_id: User,
    service: Service,
) -> dict:
    """查询一次请求的步骤树；其他用户或窗口的 Trace 一律不可读取。"""
    await service.store.get(user_id, session_id)
    if service.trace_store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Trace 查询未启用。")
    detail = await service.trace_store.detail(user_id, session_id, trace_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trace 不存在。")
    return detail
