"""请求内显式埋点：Trace、嵌套 Span 和瞬时事件。

链路：
    请求开始保存 Trace → 显式创建步骤 → 开始与结束分别落库
    → 累计 generation 用量 → 请求结束保存状态与有界摘要。

边界：
    不使用可变全局上下文，不执行工具，不改变 checkpoint 和消息事务。
    写入独立限时，失败降级；取消信号继续传播。进程退出后未收尾记录保持未完成。
    不采集完整 Prompt 或内部推理，已知凭据字段与常见凭据文本在截断前脱敏。
"""

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from itertools import islice
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from server.config import TracePolicy
from server.llm import TokenUsage
from server.store.sessions import utc_now
from server.store.traces import TraceStore

logger = logging.getLogger(__name__)
MILLISECONDS_PER_SECOND = 1000
MAX_CAPTURE_FIELDS = 32
MAX_CAPTURE_DEPTH = 4
REDACTED = "[REDACTED]"
PRIVATE_KEYS = frozenset({"thinking", "signature", "redacted_thinking"})
SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "api_key",
        "apikey",
        "authorization",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "cookie",
        "密钥",
        "密码",
    }
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_ -]?key|password|passwd|secret|token|密码|密钥)[\"']?\s*"
    r"(?::|=|：|是|\bis\b)\s*[\"']?)([^\s,;\"'}]+)"
)
BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"'}]+")
API_KEY = re.compile(r"\bsk-[a-zA-Z0-9_-]+")
URL_CREDENTIALS = re.compile(r"(\w+://)[^/@\s]+:[^/@\s]+@")
SpanType = Literal["span", "generation", "event"]
RecordStatus = Literal["running", "success", "error", "cancelled", "paused", "waiting_user"]

# ============================================================================
# 有界脱敏与埋点记录
# ============================================================================


def redact_text(value: str) -> str:
    """清理常见凭据表达式；普通异常原文不会经过此函数进入数据库。"""
    value = SECRET_ASSIGNMENT.sub(lambda match: match[1] + REDACTED, value)
    value = BEARER.sub("Bearer " + REDACTED, value)
    value = API_KEY.sub(REDACTED, value)
    return URL_CREDENTIALS.sub(lambda match: match[1] + REDACTED + "@", value)


def safe_value(value: Any, *, chars: int, depth: int = 0) -> Any:
    """限制递归、字段数与字符串长度，在采集边界排除内部推理与凭据。"""
    if depth >= MAX_CAPTURE_DEPTH:
        return "[nested content omitted]"
    if isinstance(value, dict):
        if value.get("type") in PRIVATE_KEYS:
            return "[private block omitted]"
        result = {}
        for key, item in islice(value.items(), MAX_CAPTURE_FIELDS):
            normalized = str(key).lower().replace("-", "_")
            if normalized in PRIVATE_KEYS:
                continue
            is_secret = normalized in SECRET_KEYS or any(
                normalized.endswith("_" + name) for name in SECRET_KEYS
            )
            result[str(key)[:chars]] = (
                REDACTED if is_secret else safe_value(item, chars=chars, depth=depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            safe_value(item, chars=chars, depth=depth + 1) for item in value[:MAX_CAPTURE_FIELDS]
        ]
    if isinstance(value, str):
        return redact_text(value)[:chars]
    if isinstance(value, float) and not math.isfinite(value):
        return "[non-finite value]"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return "[unsupported value]"


class TraceData(BaseModel):
    """单次请求的数据；累计用量只由模型调用贡献，不从父 Span 重复汇总。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    user_id: str
    session_id: str
    task_id: str | None = None
    checkpoint_id: str | None = None
    action: str
    status: RecordStatus = "running"
    stop_reason: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    duration_ms: float | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    request_tokens: int = 0
    usage_estimated: bool = False
    dropped_spans: int = 0


class SpanData(BaseModel):
    """单个步骤的受控字段；attributes 仅容纳有界扩展信息。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    trace_id: str
    parent_span_id: str | None
    sequence: int
    type: SpanType
    name: str
    status: RecordStatus = "running"
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    duration_ms: float | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    error_code: str | None = None
    stop_reason: str | None = None
    model: str | None = None
    tool_use_id: str | None = None
    operation_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    usage_estimated: bool = False
    attributes: dict = Field(default_factory=dict)


class SpanHandle:
    """步骤内设置输出、错误与用量；持久化由所属 Recorder 统一完成。"""

    def __init__(self, recorder: "TraceRecorder", data: SpanData | None):
        self.recorder = recorder
        self.data = data
        self.started_clock = time.monotonic()

    @property
    def id(self) -> str | None:
        return self.data.id if self.data is not None else None

    def output(
        self,
        value: Any,
        *,
        error_code: str | None = None,
        stop_reason: str | None = None,
        attributes: dict | None = None,
        status: RecordStatus | None = None,
    ) -> None:
        """记录有界输出，业务错误通过稳定错误码表示，不存原始异常详情。"""
        if self.data is None:
            return
        self.data.output_summary = self.recorder.preview(value)
        self.data.error_code = error_code
        self.data.stop_reason = stop_reason
        if status is not None:
            self.data.status = status
        if error_code is not None:
            self.data.status = "error"
        if attributes is not None:
            self.data.attributes.update(self.recorder.attributes(attributes))

    def usage(self, usage: TokenUsage, *, estimated: bool) -> None:
        """仅由 generation 调用一次；丢弃步骤详情时也保留请求累计用量。"""
        if self.recorder.data is not None:
            root = self.recorder.data
            root.input_tokens += usage.input_tokens
            root.output_tokens += usage.output_tokens
            root.cache_creation_input_tokens += usage.cache_creation_input_tokens
            root.cache_read_input_tokens += usage.cache_read_input_tokens
            root.request_tokens += usage.total
            root.usage_estimated |= estimated
        if self.data is not None:
            for name, value in usage.model_dump().items():
                setattr(self.data, name, value)
            self.data.usage_estimated = estimated


# ============================================================================
# 请求内采集器；显式传递，无全局 Session 状态
# ============================================================================


class TraceRecorder:
    """独立限时写入，埋点失败不重试业务操作；未注入存储时退化为空采集器。"""

    def __init__(self, store: TraceStore | None = None, *, policy: TracePolicy | None = None):
        self.policy = policy or TracePolicy()
        self.store = store if self.policy.enabled else None
        self.data: TraceData | None = None
        self.sequence = 0
        self.write_failed = False
        self.started_clock = time.monotonic()

    @property
    def id(self) -> str | None:
        return self.data.id if self.data is not None else None

    def preview(self, value: Any) -> str:
        """序列化脱敏后截断，只作排障摘要，不作为完整重放输入。"""
        try:
            safe = safe_value(value, chars=self.policy.preview_chars)
            rendered = json.dumps(safe, ensure_ascii=False, allow_nan=False)
            return rendered[: self.policy.preview_chars]
        except Exception:
            return "[capture unavailable]"

    def attributes(self, value: dict) -> dict:
        """扩展字段也限制总大小，避免大结果或凭据绕过摘要限制。"""
        try:
            safe = safe_value(value, chars=self.policy.preview_chars)
            if len(json.dumps(safe, ensure_ascii=False)) > self.policy.preview_chars:
                return {"preview": self.preview(safe)}
            return safe
        except Exception:
            return {"capture": "unavailable"}

    async def _write(self, operation: Callable[[], Awaitable[None]]) -> None:
        """每次使用独立超时；不吞掉调用方取消，也不输出基础设施异常原文。"""
        if self.write_failed:
            return
        try:
            async with asyncio.timeout(self.policy.write_timeout_seconds):
                await operation()
        except Exception as exc:
            # 本请求后续写入直接降级，避免重复超时耗尽 Agent 的业务执行时间。
            self.write_failed = True
            logger.warning(
                "trace_write_failed trace_id=%s error_type=%s", self.id, type(exc).__name__
            )

    async def start(self, *, user_id: str, session_id: str, action: str, text: str) -> None:
        """请求鉴权与会话归属确认后创建 Trace，记录脱敏输入。"""
        if self.store is None:
            return
        self.started_clock = time.monotonic()
        self.data = TraceData(
            user_id=user_id, session_id=session_id, action=action, input_summary=self.preview(text)
        )
        await self._write(lambda: self.store.create_trace(self.data.model_dump()))

    async def bind_task(self, task_id: str, *, checkpoint_id: str | None = None) -> None:
        """新任务绑定稳定任务 ID；继续/取消同时关联请求开始时的恢复点。"""
        if self.data is None:
            return
        self.data.task_id, self.data.checkpoint_id = task_id, checkpoint_id
        await self._write(
            lambda: self.store.update_trace(
                self.data.id,
                {
                    "task_id": task_id,
                    "checkpoint_id": checkpoint_id,
                },
            )
        )

    async def finish(self, status: RecordStatus, *, reason: str, output: Any = None) -> None:
        """请求退出时保存状态；使用单调时钟计算耗时，UTC 时间用于检索。"""
        if self.data is None:
            return
        self.data.status, self.data.stop_reason = status, reason
        self.data.ended_at = utc_now()
        self.data.duration_ms = (time.monotonic() - self.started_clock) * MILLISECONDS_PER_SECOND
        self.data.output_summary = self.preview(output)
        await self._write(
            lambda: self.store.update_trace(self.data.id, self.data.model_dump(exclude={"id"}))
        )

    async def start_span(
        self,
        name: str,
        *,
        type: SpanType = "span",
        parent_id: str | None = None,
        input: Any = None,
        attributes: dict | None = None,
        model: str | None = None,
        tool_use_id: str | None = None,
        operation_id: str | None = None,
    ) -> SpanHandle:
        """显式开始跨循环迭代的步骤；调用方负责在全部子步骤结束后关闭。"""
        data = None
        if self.data is not None:
            if self.sequence >= self.policy.max_spans:
                self.data.dropped_spans += 1
            else:
                self.sequence += 1
                data = SpanData(
                    trace_id=self.data.id,
                    parent_span_id=parent_id,
                    sequence=self.sequence,
                    type=type,
                    name=name,
                    input_summary=self.preview(input),
                    attributes=self.attributes(attributes or {}),
                    model=model,
                    tool_use_id=tool_use_id,
                    operation_id=operation_id,
                )
                await self._write(lambda: self.store.create_span(data.model_dump()))
        return SpanHandle(self, data)

    async def finish_span(
        self,
        handle: SpanHandle | None,
        *,
        status: RecordStatus = "success",
        reason: str | None = None,
    ) -> None:
        """关闭尚未结束的步骤；重复关闭不会覆盖已记录的异常或耗时。"""
        if handle is None or handle.data is None or handle.data.ended_at is not None:
            return
        data = handle.data
        if data.status == "running":
            data.status = status
        if reason is not None:
            data.stop_reason = reason
        data.ended_at = utc_now()
        data.duration_ms = (time.monotonic() - handle.started_clock) * MILLISECONDS_PER_SECOND
        await self._write(lambda: self.store.update_span(data.id, data.model_dump(exclude={"id"})))

    @asynccontextmanager
    async def span(
        self,
        name: str,
        *,
        type: SpanType = "span",
        parent_id: str | None = None,
        input: Any = None,
        attributes: dict | None = None,
        model: str | None = None,
        tool_use_id: str | None = None,
        operation_id: str | None = None,
    ) -> AsyncIterator[SpanHandle]:
        """显式包围一个执行步骤，开始与结束分别保存。

        Args:
            name: 稳定的业务步骤名称。
            type: 普通步骤、模型生成或瞬时事件。
            parent_id: 调用方指定的父步骤，不依赖隐式上下文。
            input: 可公开的参数或输入概况，采集时统一脱敏并限制大小。
            attributes: 有界辅助字段，如轮次、摘要版本和结果引用。
            model: generation 使用的模型名。
            tool_use_id: Anthropic 调用标识。
            operation_id: 跨恢复请求保持稳定的工具操作标识。

        Yields:
            可设置步骤输出、错误及模型用量的句柄。
        """
        handle = await self.start_span(
            name,
            type=type,
            parent_id=parent_id,
            input=input,
            attributes=attributes,
            model=model,
            tool_use_id=tool_use_id,
            operation_id=operation_id,
        )
        data = handle.data
        try:
            yield handle
        except asyncio.CancelledError:
            if data is not None:
                data.status, data.error_code = "cancelled", "CancelledError"
            raise
        except Exception as exc:
            if data is not None and data.status == "running":
                data.status, data.error_code = "error", exc.__class__.__name__
            raise
        finally:
            await self.finish_span(handle)

    async def event(
        self, name: str, *, parent_id: str | None = None, attributes: dict | None = None
    ) -> None:
        """事件复用 Span 表，不另建事件存储或触发任何模型调用。"""
        async with self.span(name, type="event", parent_id=parent_id, attributes=attributes):
            pass
