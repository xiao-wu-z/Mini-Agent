"""tool_use → 参数校验 → skill handler → tool_result。

执行链路：
    通过工具注册表定位 handler，使用对应参数模型校验 input，
    在超时范围内执行异步 handler，将返回值封装为 JSON 字符串。

错误约定：
    未知工具、参数错误、执行超时和普通异常均返回 is_error=true 的 tool_result，
    供下一轮模型修正调用；任务取消信号原样传播。
"""

import asyncio
import json
import logging
from typing import Any

from pydantic import ValidationError

from server.llm import ToolUse
from server.tools import ToolRegistry
from skills._shared.context import ToolContext
from skills._shared.errors import SkillError, SkillErrorCode

logger = logging.getLogger(__name__)


class SkillRunner:
    """统一工具执行边界，不承担模型调用和循环控制。"""

    def __init__(self, tools: ToolRegistry):
        self.tools = tools

    async def execute(
        self, call: ToolUse, *, timeout: float, context: ToolContext | None = None
    ) -> dict[str, Any]:
        """执行一个工具，返回与 call.id 配对的 Anthropic tool_result。

        Args:
            call: 模型返回且已通过协议校验的 tool_use 块。
            timeout: 单次 handler 执行的超时秒数。
            context: 可信用户、会话和调用标识，不从模型参数读取。

        Returns:
            成功时 content 包含 ok/data；失败时包含错误码并设置 is_error。

        Raises:
            asyncio.CancelledError: 外部取消或 handler 主动取消，交由调用方处理。
        """
        # ---- 1. 查找工具并校验参数，校验失败时不调用 handler ----
        tool = self.tools.get(call.name)
        if tool is None:
            return self._error(call.id, SkillErrorCode.UNKNOWN_TOOL, "Tool is not registered")
        try:
            arguments = tool.parameters.model_validate(call.input)
        except ValidationError as exc:
            details = exc.errors(include_input=False, include_context=False, include_url=False)
            return self._error(
                call.id, SkillErrorCode.INVALID_ARGUMENTS, json.dumps(details, ensure_ascii=False)
            )
        # ---- 2. 执行并序列化结果，普通执行异常统一转为错误回灌 ----
        try:
            async with asyncio.timeout(timeout):
                data = await tool.handler(arguments, context or ToolContext())
            content = json.dumps({"ok": True, "data": data}, ensure_ascii=False, allow_nan=False)
            return {"type": "tool_result", "tool_use_id": call.id, "content": content}
        except SkillError as exc:
            return self._error(call.id, exc.code, exc.message)
        except TimeoutError:
            return self._error(call.id, SkillErrorCode.TOOL_TIMEOUT, "Tool execution timed out")
        except Exception as exc:
            # 不向模型或日志透传异常中的数据库地址、凭据等原始详情。
            logger.warning("tool_failed tool=%s error_type=%s", call.name, type(exc).__name__)
            return self._error(call.id, SkillErrorCode.TOOL_ERROR, "Tool execution failed")

    @staticmethod
    def _error(call_id: str, code: str, message: str) -> dict[str, Any]:
        """统一生成错误结果，保留调用 ID，使模型可以关联失败的工具。"""
        return {
            "type": "tool_result",
            "tool_use_id": call_id,
            "is_error": True,
            "content": json.dumps(
                {"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=False
            ),
        }
