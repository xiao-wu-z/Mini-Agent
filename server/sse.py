"""SSE 编码与请求生命周期桥接。

链路：
    业务回调 → 有界队列 → SSE 事件；业务完成后发送 done，执行失败发送脱敏 error。
    连接关闭时取消并等待业务任务收尾，保证 Loop 有机会保存检查点与 Trace。

边界：
    不复制 Agent Loop，不保存消息或执行工具。心跳只维持连接，不调用模型。
    SSE 建立后的业务错误通过事件返回；鉴权和会话归属由路由提前校验。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any, Literal

import anyio
from pydantic import BaseModel

from server.store.sessions import ConversationCode, ConversationError

STREAM_QUEUE_MAXSIZE = 32
MAX_TOOL_MARK_NAME_LENGTH = 64
logger = logging.getLogger(__name__)


class StreamEvent(BaseModel):
    """公开流事件，不携带模型 thinking、签名或原始基础设施异常。"""

    event: Literal["start", "text_delta", "tool_use", "tool_result", "done", "error", "heartbeat"]
    data: dict[str, Any]

    def encode(self) -> str:
        """JSON 转义文本中的换行，避免业务文本改变 SSE 帧边界。"""
        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"


EventSink = Callable[[StreamEvent], Awaitable[None]]


def tool_mark_name(name: str) -> str:
    """限制展示名并清理 marker 分隔符，前端仍需按普通文本安全渲染。"""
    return (
        name.replace("[", "（")
        .replace("]", "）")
        .replace("·", "/")
        .replace("\n", " ")
        .replace("\r", " ")[:MAX_TOOL_MARK_NAME_LENGTH]
    )


def tool_use_event(name: str, call_id: str) -> StreamEvent:
    """实际执行前的工具调用标记，不包含模型参数或内部推理。"""
    return StreamEvent(
        event="tool_use",
        data={
            "name": name,
            "tool_use_id": call_id,
            "status": "running",
            "mark": f"[工具调用中 · {tool_mark_name(name)}]",
        },
    )


def tool_result_event(
    name: str,
    call_id: str,
    *,
    is_error: bool,
    duration_ms: float,
    reused: bool = False,
    unknown: bool = False,
) -> StreamEvent:
    """生成可更新同一工具卡的结果标记；未知副作用不能展示为已成功或确定失败。"""
    if unknown:
        status, label = "unknown", "结果待确认"
    elif is_error:
        status, label = "error", "工具失败"
    else:
        status, label = "success", "结果已恢复" if reused else "工具 OK"
    return StreamEvent(
        event="tool_result",
        data={
            "name": name,
            "tool_use_id": call_id,
            "status": status,
            "is_error": is_error,
            "duration_ms": duration_ms,
            "reused": reused,
            "mark": f"[tool={tool_mark_name(name)} · {label}]",
        },
    )


async def stream_events(
    run: Callable[[EventSink], Awaitable[BaseModel]], *, heartbeat_seconds: float
) -> AsyncIterator[str]:
    """边执行业务边发送事件；限制积压，并在客户端退出时等待取消收尾。"""
    queue: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=STREAM_QUEUE_MAXSIZE)

    async def produce() -> None:
        try:
            result = await run(queue.put)
            await queue.put(StreamEvent(event="done", data=result.model_dump(mode="json")))
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            await queue.put(
                StreamEvent(
                    event="error",
                    data={
                        "code": ConversationCode.RUN_FAILED,
                        "message": "请求执行已中断，请查询任务状态后继续。",
                    },
                )
            )
        except Exception as exc:
            # 仅稳定业务错误可公开；其余异常不透传 repr/str，避免泄露凭据。
            code = exc.code if isinstance(exc, ConversationError) else ConversationCode.RUN_FAILED
            message = (
                exc.message
                if isinstance(exc, ConversationError)
                else "请求执行失败，进度可能已保存，请查询任务状态。"
            )
            logger.warning("sse_request_failed error_type=%s", type(exc).__name__)
            await queue.put(StreamEvent(event="error", data={"code": code, "message": message}))

    worker = asyncio.create_task(produce())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield StreamEvent(event="heartbeat", data={}).encode()
                continue
            yield event.encode()
            if event.event in {"done", "error"}:
                break
    finally:
        if not worker.done():
            worker.cancel()
        # Starlette 在断连时取消其响应任务组；屏蔽该作用域以等待子任务保存恢复状态。
        with anyio.CancelScope(shield=True), suppress(asyncio.CancelledError):
            await worker
