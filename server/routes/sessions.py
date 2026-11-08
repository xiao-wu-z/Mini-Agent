"""会话与聊天 HTTP 入口；身份必须由可信调用方提供，不接受模型指定归属。

部署边界：
    X-User-ID 是开发环境/可信网关身份，不是终端用户认证。
    生产入口必须配置服务令牌并由已认证网关注入用户身份，不应直接暴露公网。
"""

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from server.conversations import ChatResponse, ConversationService
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
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=MAX_USER_INPUT_LENGTH)


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


@router.post("/{session_id}/chat", response_model=ChatResponse)
async def chat(
    session_id: SessionId,
    body: ChatRequest,
    user_id: User,
    service: Service,
) -> ChatResponse:
    return await service.chat(user_id, session_id, body.message)
