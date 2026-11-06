"""工具定义与注册。

职责：
    - 维护工具名称、描述、参数模型与 handler 的对应关系。
    - 将 Pydantic 参数模型转换为 Anthropic input_schema。
    - 组装默认工具集合，供 Agent Loop 使用。

边界：
    本模块不执行工具；参数校验、超时与错误回灌由 server.skill_runner 负责。
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from skills.calculator.handler import CalculatorArgs, calculate

# ============================================================================
# 工具定义与注册表
# ============================================================================


@dataclass(frozen=True)
class Tool:
    """工具声明：parameters 定义模型可填写的参数，handler 接收校验后的参数对象。"""

    name: str
    description: str
    parameters: type[BaseModel]
    handler: Callable[[Any], Awaitable[Any]]

    def spec(self) -> dict[str, Any]:
        """生成 Anthropic Messages API 的单个工具声明。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters.model_json_schema(),
        }


class ToolRegistry:
    """按名称管理工具；注册顺序即向模型暴露 Schema 的顺序。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具，同名工具不允许静默覆盖。

        Args:
            tool: 包含名称、描述、参数模型和异步 handler 的工具声明。

        Raises:
            ValueError: 工具名称不合法、描述为空或名称已经注册。
        """
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.name):
            raise ValueError("Tool name must be 1-64 letters, digits, underscores or hyphens")
        if not tool.description.strip():
            raise ValueError("Tool description is required")
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def specs(self) -> list[dict[str, Any]]:
        """按注册顺序返回全部工具的 Anthropic Schema。"""
        return [tool.spec() for tool in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        """按名称查找工具；不存在时返回 None，由执行层生成 UNKNOWN_TOOL。"""
        return self._tools.get(name)


# ============================================================================
# 默认工具注册
# ============================================================================


def create_default_tools() -> ToolRegistry:
    """创建独立的默认工具集合，新增能力在此注册对应 skill handler。"""
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="calculator",
            description="对两个数字做加、减、乘或除运算；除法的 right 不能为零。",
            parameters=CalculatorArgs,
            handler=calculate,
        )
    )
    return registry
