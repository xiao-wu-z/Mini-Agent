"""Search skill：在固定资料集中搜索，结果明确标记为 Mock。

边界：
    不访问互联网、不编造实时搜索结果；未匹配时返回空列表。
    mock:// 地址只标识本地测试资料，不代表可访问的网页。
"""

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from skills._shared.context import ToolContext

MAX_QUERY_LENGTH = 200
DEFAULT_RESULT_LIMIT = 5
MAX_RESULT_LIMIT = 10
QUERY_TERMS = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+")


@dataclass(frozen=True)
class SearchDocument:
    title: str
    snippet: str
    url: str


MOCK_DOCUMENTS = (
    SearchDocument("FastAPI 入门", "FastAPI 使用 Python 类型声明构建 HTTP API。", "mock://fastapi"),
    SearchDocument(
        "Agent 工具调用", "Agent 通过工具 Schema 选择工具并读取执行结果。", "mock://agent"
    ),
    SearchDocument("Todo 待办管理", "待办可以新增、查询、完成和删除，按会话隔离。", "mock://todo"),
    SearchDocument("MySQL 持久化", "MySQL 保存待办数据，数据库结构通过迁移维护。", "mock://mysql"),
)


class SearchArgs(BaseModel):
    """搜索关键词与结果上限；空白查询和额外参数在执行前拒绝。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    limit: int = Field(default=DEFAULT_RESULT_LIMIT, ge=1, le=MAX_RESULT_LIMIT)


async def search(arguments: SearchArgs, context: ToolContext) -> dict:
    """按关键词匹配本地资料，返回可用于验证 Loop 的稳定结果。"""
    terms = QUERY_TERMS.findall(arguments.query.casefold())
    matches = []
    for document in MOCK_DOCUMENTS:
        searchable_text = f"{document.title} {document.snippet}".casefold()
        if any(term in searchable_text for term in terms):
            matches.append(
                {"title": document.title, "snippet": document.snippet, "url": document.url}
            )
    return {"is_mock": True, "query": arguments.query, "items": matches[: arguments.limit]}
