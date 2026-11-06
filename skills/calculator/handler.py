"""Calculator skill：两数四则运算。

输入：
    operation 指定 add/subtract/multiply/divide，left 和 right 为有限数值。

输出：
    {"result": 运算结果}，由 SkillRunner 封装为 Anthropic tool_result。

边界：
    不解析或执行任意表达式；除零与结果序列化错误由执行层统一处理。
    工具描述和注册入口见 server.tools。
"""

import operator
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CalculatorArgs(BaseModel):
    """模型可填写的参数，禁止额外字段、非有限数值及过大的操作数。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    operation: Literal["add", "subtract", "multiply", "divide"]
    left: float = Field(ge=-1e100, le=1e100)
    right: float = Field(ge=-1e100, le=1e100)


async def calculate(arguments: CalculatorArgs) -> dict[str, float]:
    """执行一次算术运算。

    Args:
        arguments: 已通过 CalculatorArgs 校验的操作类型和两个操作数。

    Returns:
        包含 result 的字典。

    Raises:
        ZeroDivisionError: divide 操作的 right 为零。
    """
    operations = {
        "add": operator.add,
        "subtract": operator.sub,
        "multiply": operator.mul,
        "divide": operator.truediv,
    }
    return {"result": operations[arguments.operation](arguments.left, arguments.right)}
