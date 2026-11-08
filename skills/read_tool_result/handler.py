"""大工具结果的有界读取能力。

ref 仅定位已保存的结果，不是工具执行指令；身份由服务端提供。
分页以 Unicode 字符为单位，不会切坏 UTF-8 字节；读取结果始终有上限。
"""

from pydantic import BaseModel, ConfigDict, Field

from server.store.sessions import ENTITY_ID_LENGTH
from server.store.tool_results import ToolResultStore
from skills._shared.context import ToolContext
from skills._shared.errors import SkillError, SkillErrorCode

DEFAULT_PAGE_CHARS = 1024
MAX_PAGE_CHARS = 2048


class ReadToolResultArgs(BaseModel):
    """模型可填写的引用与分页范围，不暴露用户、会话及存储位置。"""

    model_config = ConfigDict(extra="forbid")
    ref: str = Field(pattern=rf"^[0-9a-f]{{{ENTITY_ID_LENGTH}}}$")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=DEFAULT_PAGE_CHARS, ge=1, le=MAX_PAGE_CHARS)


class ReadToolResultHandler:
    """校验可信身份，读取已保存结果的一页；缺失时返回明确错误。"""

    def __init__(self, store: ToolResultStore | None):
        self.store = store

    async def run(self, arguments: ReadToolResultArgs, context: ToolContext) -> dict:
        """返回片段、下一页位置及原结果错误标识，不将原工具失败误作成功。"""
        if not context.user_id or not context.session_id:
            raise SkillError(SkillErrorCode.CONTEXT_REQUIRED, "读取结果需要用户与会话身份。")
        if self.store is None:
            raise SkillError(SkillErrorCode.DEPENDENCY_UNAVAILABLE, "结果持久化未启用。")
        result = await self.store.get(arguments.ref, context=context)
        if result is None:
            raise SkillError(
                SkillErrorCode.NOT_FOUND, "结果不存在或不属于当前会话，不能自动重跑原工具。"
            )
        content = result["content"]
        if arguments.offset > len(content):
            raise SkillError(SkillErrorCode.INVALID_ARGUMENTS, "offset 超出结果字符范围。")
        end = min(arguments.offset + arguments.limit, len(content))
        return {
            "ref": arguments.ref,
            "content": content[arguments.offset : end],
            "offset": arguments.offset,
            "next_offset": end,
            "total_chars": len(content),
            "has_more": end < len(content),
            "original_is_error": result.get("is_error", False),
        }
