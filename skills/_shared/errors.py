"""可向模型说明的工具错误；原始基础设施异常由执行器隐藏。"""

from enum import StrEnum


class SkillErrorCode(StrEnum):
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_ERROR = "TOOL_ERROR"
    CONTEXT_REQUIRED = "CONTEXT_REQUIRED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    LOCATION_NOT_SUPPORTED = "LOCATION_NOT_SUPPORTED"


class SkillError(Exception):
    """handler 可预期的业务失败，message 必须可公开且不包含凭据。"""

    def __init__(self, code: SkillErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
