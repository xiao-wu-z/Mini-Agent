"""Todo skill：新增、查询、完成与删除当前窗口的待办。

链路：
    模型填写业务参数 → 校验可信上下文 → TodoStore 短事务 → 返回持久化结果。

边界：
    不接受模型填写 user_id/session_id；操作其他窗口的 ID 与不存在的 ID 一样返回未找到。
    工具不管理 Session 生命周期，仅使用调用方传入的身份与窗口标识。
"""

from hashlib import sha256
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.store.todos import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MAX_TITLE_LENGTH,
    TODO_ID_LENGTH,
    TodoConflictError,
    TodoStatus,
    TodoStore,
)
from skills._shared.context import ToolContext
from skills._shared.errors import SkillError, SkillErrorCode

TODO_ID_PATTERN = rf"^[0-9a-f]{{{TODO_ID_LENGTH}}}$"


class TodoArgs(BaseModel):
    """每种操作只接受需要的参数；分页和状态过滤仅用于 list。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["create", "list", "complete", "delete"]
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_LENGTH)
    todo_id: str | None = Field(default=None, pattern=TODO_ID_PATTERN)
    status: TodoStatus | None = None
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.action == "create" and self.title is None:
            raise ValueError("create requires title")
        if self.action in {"complete", "delete"} and self.todo_id is None:
            raise ValueError("complete/delete requires todo_id from a previous todo result")
        if self.action != "create" and self.title is not None:
            raise ValueError("title is only valid for create")
        if self.action not in {"complete", "delete"} and self.todo_id is not None:
            raise ValueError("todo_id is only valid for complete/delete")
        if self.action != "list" and (
            self.status is not None or self.limit != DEFAULT_PAGE_SIZE or self.offset != 0
        ):
            raise ValueError("status/limit/offset are only valid for list")
        return self


class TodoHandler:
    """注入持久化依赖，每次执行使用独立上下文，不保存用户或 Session 状态。"""

    def __init__(self, store: TodoStore | None):
        self.store = store

    async def run(self, arguments: TodoArgs, context: ToolContext) -> dict:
        """按动作执行数据库操作；只有事务成功后才返回成功结果。"""
        if context.user_id is None or context.session_id is None:
            raise SkillError(
                SkillErrorCode.CONTEXT_REQUIRED, "Todo 需要调用方提供用户和会话上下文。"
            )
        if self.store is None:
            raise SkillError(SkillErrorCode.DEPENDENCY_UNAVAILABLE, "Todo 数据库未启用。")
        scope = {"user_id": context.user_id, "session_id": context.session_id}
        if arguments.action == "list":
            items, has_more = await self.store.list_items(
                **scope,
                status=arguments.status,
                limit=arguments.limit,
                offset=arguments.offset,
            )
            return {
                "action": "list",
                "items": [item.model_dump(mode="json") for item in items],
                "has_more": has_more,
                "offset": arguments.offset,
                "limit": arguments.limit,
            }
        if arguments.action == "create":
            if not context.run_id or not context.tool_use_id:
                raise SkillError(
                    SkillErrorCode.CONTEXT_REQUIRED, "新增待办需要运行和工具调用标识。"
                )
            create_key = sha256(f"{context.run_id}:{context.tool_use_id}".encode()).hexdigest()
            try:
                item = await self.store.create(
                    **scope, title=arguments.title, create_key=create_key
                )
            except TodoConflictError as exc:
                raise SkillError(
                    SkillErrorCode.CONFLICT, "同一工具调用不能用于创建不同的待办。"
                ) from exc
            return {"action": "create", "item": item.model_dump(mode="json")}
        if arguments.action == "complete":
            item = await self.store.complete(**scope, todo_id=arguments.todo_id)
            if item is None:
                raise SkillError(SkillErrorCode.NOT_FOUND, "当前会话中未找到该待办。")
            return {"action": "complete", "item": item.model_dump(mode="json")}
        was_deleted = await self.store.delete(**scope, todo_id=arguments.todo_id)
        if not was_deleted:
            raise SkillError(SkillErrorCode.NOT_FOUND, "当前会话中未找到该待办。")
        return {"action": "delete", "todo_id": arguments.todo_id, "deleted": True}
