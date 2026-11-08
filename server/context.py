"""Context 组装：保留最近完整对话轮，不拆散工具调用和结果。

完整历史仍保存在 MySQL；这里只决定本次发送给模型的历史窗口。
system 提示词和工具 Schema 由 Loop 单独传递，不重复写入消息表。
"""

from server.llm import Message


def build_context(history: list[Message], *, recent_turns: int) -> list[Message]:
    """以普通 user 输入划分轮次，tool_result 所在的 user 消息仍属于原轮次。"""
    if recent_turns < 1:
        raise ValueError("recent_turns must be positive")
    starts = [
        index
        for index, message in enumerate(history)
        if message.role == "user" and isinstance(message.content, str)
    ]
    if not starts:
        return []
    start = starts[-recent_turns] if len(starts) >= recent_turns else starts[0]
    return [message.model_copy(deep=True) for message in history[start:]]
