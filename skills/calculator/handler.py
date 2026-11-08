"""Calculator skill：两数四则运算。

输入：
    operation 指定 add/subtract/multiply/divide，left 和 right 为有限数值。

输出：
    {"result": 运算结果}，由 SkillRunner 封装为 Anthropic tool_result。

边界：
    不解析或执行任意表达式；除零与结果序列化错误由执行层统一处理。
    工具描述和注册入口见 server.tools。
"""

import math
import operator
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from skills._shared.context import ToolContext
from skills._shared.errors import SkillError, SkillErrorCode

MAX_OPERAND_MAGNITUDE = 1e100


class CalculatorArgs(BaseModel):
    """模型可填写的参数，禁止额外字段、非有限数值及过大的操作数。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    operation: Literal["add", "subtract", "multiply", "divide"]
    left: float = Field(ge=-MAX_OPERAND_MAGNITUDE, le=MAX_OPERAND_MAGNITUDE)
    right: float = Field(ge=-MAX_OPERAND_MAGNITUDE, le=MAX_OPERAND_MAGNITUDE)


async def calculate(arguments: CalculatorArgs, context: ToolContext) -> dict[str, float]:
    """执行一次算术运算。

    Args:
        arguments: 已通过 CalculatorArgs 校验的操作类型和两个操作数。
        context: 调用方传入的可信上下文，本工具不读取身份信息。

    Returns:
        包含 result 的字典。

    Raises:
        SkillError: 除数为零或计算结果不是有限数值。
    """
    operations = {
        "add": operator.add,
        "subtract": operator.sub,
        "multiply": operator.mul,
        "divide": operator.truediv,
    }
    if arguments.operation == "divide" and arguments.right == 0:
        raise SkillError(SkillErrorCode.INVALID_ARGUMENTS, "除数不能为零。")
    result = operations[arguments.operation](arguments.left, arguments.right)
    if not math.isfinite(result):
        raise SkillError(SkillErrorCode.INVALID_ARGUMENTS, "计算结果超出有限数值范围。")
    return {"result": result}
