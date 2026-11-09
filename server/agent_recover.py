"""工具同错识别与分级纠错状态。

职责：
    - 按工具、动作、错误码与字段约束归并同类失败，记录任务内的失败次数。
    - 第二次起增强工具反馈；第三次复盘、第四次熔断的执行由 Loop 控制。
    - 定义简短复盘结论的结构和长度限制。

链路：
    工具结果 → 成功则关闭同工具动作的错误记录
    → 失败则生成指纹、累加计数、增强 tool_result → 返回指纹供 Loop 判断。

边界：
    本模块不调用模型、不执行工具、不保存数据库，也不解析模型内部推理。
    失败记录由调用方传入，随任务检查点保留，不使用全局可变计数器。
"""

import json
from typing import Any

from pydantic import BaseModel, Field

# ============================================================================
# 分级阈值与纠错状态
# ============================================================================

HINT_AT = 2
REFLECT_AT = 3
ABORT_AT = 4
MAX_REFLECTION_LENGTH = 500


class FailureRecord(BaseModel):
    """一个错误簇的累计记录；成功后关闭，下次同错重新计数。"""

    tool: str
    action: str
    code: str
    cause: str
    count: int = 0
    reflected: bool = False  # 表示已发起复盘，不保证复盘响应已完整收到。
    reflection: str | None = None
    closed: bool = False


class Reflection(BaseModel):
    """复盘的简短纠错结论；供后续决策使用，不要求模型输出完整思考过程。"""

    cause: str = Field(max_length=MAX_REFLECTION_LENGTH)
    change: str = Field(max_length=MAX_REFLECTION_LENGTH)
    missing_information: str = Field(max_length=MAX_REFLECTION_LENGTH)
    next_action: str = Field(pattern=r"^(retry|ask_user)$")


# ============================================================================
# 同错归并与工具反馈增强
# ============================================================================


def fingerprint(tool: str, arguments: dict[str, Any], error: dict) -> tuple[str, str]:
    """生成同错指纹与归一化错误原因。

    参数校验错误使用字段路径、约束类型和约束说明；列表下标归一化为通配符，
    避免模型只换错误值或数组位置就绕过同错计数。其他业务错误保留稳定错误说明。

    Args:
        tool: 工具注册名。
        arguments: 本次调用参数，仅使用 action 区分工具内的业务动作。
        error: 执行器返回的标准错误，包含 code 和 message。

    Returns:
        用于索引失败记录的指纹，以及归一化后的错误原因。
    """
    cause = str(error["message"])
    try:
        details = json.loads(cause)
    except (ValueError, TypeError):
        details = None
    if isinstance(details, list):
        issues = []
        for item in details:
            if isinstance(item, dict):
                path = ["*" if isinstance(part, int) else str(part) for part in item.get("loc", [])]
                issues.append((path, item.get("type", ""), item.get("msg", "")))
        cause = json.dumps(sorted(issues), ensure_ascii=False)
    key = json.dumps([tool, arguments.get("action", ""), error["code"], cause], ensure_ascii=False)
    return key, cause


def observe(
    records: dict[str, FailureRecord], tool: str, arguments: dict, result: dict
) -> str | None:
    """处理一次工具结果，原地更新失败记录与工具反馈。

    Args:
        records: 当前任务的失败记录；无关工具成功不会清空这些记录。
        tool: 工具注册名。
        arguments: 已执行调用的业务参数，用于识别动作。
        result: 标准 tool_result；达到提示阈值时原地增强 content，保留 is_error。

    Returns:
        失败时返回错误指纹，供 Loop 判断复盘与熔断；成功时关闭同动作记录并返回 None。
    """
    # ---- 1. 成功仅关闭同工具动作的记录，不重置其他错误簇 ----
    action = str(arguments.get("action", ""))
    if not result.get("is_error"):
        for record in records.values():
            if record.tool == tool and record.action == action:
                record.closed = True
        return None
    # ---- 2. 归并同类失败；已关闭的错误簇重新开始计数 ----
    payload = json.loads(result["content"])
    error = payload["error"]
    key, cause = fingerprint(tool, arguments, error)
    record = records.get(key)
    if record is None or record.closed:
        record = FailureRecord(tool=tool, action=action, code=error["code"], cause=cause)
        records[key] = record
    record.count += 1
    # ---- 3. 增强原工具结果，不插入额外协议消息，也不在此发起复盘请求 ----
    if record.count >= HINT_AT:
        payload["recovery"] = {
            "count": record.count,
            "hint": (
                "检查出错字段与约束，改变导致失败的参数；缺少信息时询问用户。"
                "不要重复未知结果的写操作。"
            ),
        }
        result["content"] = json.dumps(payload, ensure_ascii=False)
    return key
