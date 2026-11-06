"""可信工具上下文；身份与调用标识由服务端传入，不暴露给模型填写。"""

from pydantic import BaseModel, ConfigDict, Field

MAX_IDENTITY_LENGTH = 64


class ToolContext(BaseModel):
    """每次调用独立持有身份和 trace 信息，不通过全局变量共享会话状态。"""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    user_id: str | None = Field(default=None, min_length=1, max_length=MAX_IDENTITY_LENGTH)
    session_id: str | None = Field(default=None, min_length=1, max_length=MAX_IDENTITY_LENGTH)
    run_id: str = ""
    tool_use_id: str = ""
