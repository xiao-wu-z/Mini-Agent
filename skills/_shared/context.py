"""可信工具上下文；身份与调用标识由服务端传入，不暴露给模型填写。"""

from pydantic import BaseModel, ConfigDict, Field

MAX_IDENTITY_LENGTH = 64


class ToolContext(BaseModel):
    """服务端提供的不可变调用上下文，不通过全局变量共享会话状态。

    run_id 标识任务，tool_use_id 用于协议配对，operation_id 在恢复时保持稳定用于去重。
    recovering 表示重试结果未知的旧操作；handler 不得把未找到结果当作本次执行成功。
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    user_id: str | None = Field(default=None, min_length=1, max_length=MAX_IDENTITY_LENGTH)
    session_id: str | None = Field(default=None, min_length=1, max_length=MAX_IDENTITY_LENGTH)
    run_id: str = ""
    tool_use_id: str = ""
    operation_id: str = ""
    recovering: bool = False
