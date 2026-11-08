"""Anthropic Messages 模型适配层。

职责：
    - 定义 Message、ToolUse 和 LLMTurn，保留完整 content blocks。
    - 构造 /v1/messages 请求，提供单轮调用和纯文本 SSE 接口。
    - 提供无需联网的 echo 模式，便于检查应用入口。

边界：
    本模块只与模型通信；工具注册、执行和多轮控制分别由 tools、skill_runner、
    agent_loop 负责。工具调用使用 run_turn，收齐整轮内容后再交给 Loop 执行。
"""

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from server.config import Settings

# ============================================================================
# Anthropic 消息与响应协议
# ============================================================================


class Message(BaseModel):
    """会话中的 user/assistant 消息；system 提示词通过请求顶层字段单独传入。"""

    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]


class ToolUse(BaseModel):
    """模型发起的工具调用，id 用于和后续 tool_result 配对。"""

    type: Literal["tool_use"] = "tool_use"
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    input: dict[str, Any]


class TokenUsage(BaseModel):
    """供应商返回的 Token 用量；普通输入、缓存创建和缓存读取分别累计。"""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        """返回请求预算使用的总消耗，缓存输入也计入执行预算。"""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


class LLMTurn(BaseModel):
    """保留完整 content blocks，包含回灌所需的供应商签名等字段。"""

    content: list[dict[str, Any]]
    stop_reason: str
    usage: TokenUsage | None = None

    @property
    def text(self) -> str:
        """只提取 text 块，不把 thinking 或工具内容当作最终回答。"""
        return "".join(block["text"] for block in self.content if block.get("type") == "text")


# ============================================================================
# 模型客户端
# ============================================================================


class LLMClient:
    """复用异步 HTTP 连接的 Anthropic 客户端，由应用生命周期或调用方关闭。"""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        key = settings.llm_api_key.get_secret_value()
        headers = {"anthropic-version": "2023-06-01"}
        headers[settings.llm_auth_header] = (
            f"Bearer {key}" if settings.llm_auth_header == "authorization" else key
        )
        self.client = httpx.AsyncClient(
            base_url=(settings.llm_base_url or "http://localhost").rstrip("/") + "/",
            headers=headers,
            timeout=settings.llm_timeout,
            trust_env=False,
            transport=transport,
        )
        self.messages_path = (
            "messages" if self.client.base_url.path.rstrip("/").endswith("/v1") else "v1/messages"
        )

    def _body(
        self,
        messages: list[Message],
        *,
        system: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> dict[str, Any]:
        """组装单次请求体；省略未配置的 system 和空工具列表。"""
        body: dict[str, Any] = {
            "model": self.settings.llm_model,
            "max_tokens": self.settings.llm_max_tokens,
            "messages": [message.model_dump() for message in messages],
            "stream": stream,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        return body

    async def run_turn(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
        max_tokens: int | None = None,
    ) -> LLMTurn:
        """请求一次完整模型响应，不在客户端内部执行工具或继续循环。

        Args:
            messages: 当前会话消息，包括前轮 assistant 和对应工具结果。
            tools: Anthropic 工具声明列表，参数格式为 input_schema。
            system: 请求顶层的系统提示词，不放入 messages。
            max_tokens: 本次生成的输出上限，同时受部署配置的 llm_max_tokens 限制。

        Returns:
            包含完整 content blocks 和 stop_reason 的 LLMTurn。

        Raises:
            httpx.HTTPError: 连接失败、超时或接口返回错误状态。
            ValueError: 响应不是合法 JSON，或缺少必要协议字段。
        """
        if self.settings.llm_provider == "echo":
            return LLMTurn(
                content=[{"type": "text", "text": self._echo(messages)}],
                stop_reason="end_turn",
                usage=TokenUsage(),
            )
        body = self._body(messages, system=system, tools=tools, stream=False)
        if max_tokens is not None:
            body["max_tokens"] = min(max_tokens, self.settings.llm_max_tokens)
        response = await self.client.post(self.messages_path, json=body)
        response.raise_for_status()
        return LLMTurn.model_validate(response.json())

    async def count_tokens(self, messages: list[Message], *, tools: list[dict], system: str) -> int:
        """计数本次完整模型输入，供 Loop 在生成前预留预算。

        Args:
            messages: 将要发送的完整协议消息。
            tools: 本次请求暴露的工具 Schema，也属于输入开销。
            system: 与生成请求一致的顶层系统提示词。

        Returns:
            供应商报告的输入 Token 数。关闭计数或接口不支持时，返回 UTF-8 请求字节数
            作为保守估算；echo 不调用模型，返回零。

        Raises:
            httpx.HTTPError: 除不支持接口之外的 HTTP 错误，交由上层暂停任务。
            ValueError: 计数响应中的 input_tokens 无法转换为整数。

        字节估算不是供应商精确计数，不承诺绝对零超支。
        """
        body = self._body(messages, system=system, tools=tools, stream=False)
        body.pop("stream")
        body.pop("max_tokens")
        if self.settings.llm_provider == "echo":
            return 0
        # 仅在接口明确不支持时降级，避免掩盖鉴权失败、限流等服务错误。
        if self.settings.llm_count_tokens:
            response = await self.client.post(self.messages_path + "/count_tokens", json=body)
            if response.status_code not in {404, 405, 501}:
                response.raise_for_status()
                return int(response.json()["input_tokens"])
        return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))

    async def complete(self, messages: list[Message], *, system: str = "") -> str:
        """发起不带工具的单轮请求，并提取全部 text 块。"""
        return (await self.run_turn(messages, system=system)).text

    async def stream(self, messages: list[Message], *, system: str = "") -> AsyncIterator[str]:
        """逐段返回纯文本 SSE 内容，以 message_stop 判断响应完整结束。

        Args:
            messages: 当前会话消息。
            system: 请求顶层的系统提示词。

        Yields:
            content_block_delta 事件中的 text_delta 文本。

        Raises:
            httpx.HTTPError: 请求失败或读取超时。
            RuntimeError: 服务端报告流错误，或连接在 message_stop 前结束。
        """
        if self.settings.llm_provider == "echo":
            yield self._echo(messages)
            return
        async with self.client.stream(
            "POST",
            self.messages_path,
            json=self._body(messages, system=system, tools=None, stream=True),
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:") or not (data := line[5:].strip()):
                    continue
                event = json.loads(data)
                if event.get("type") == "error":
                    raise RuntimeError("LLM provider returned a stream error")
                if event.get("type") == "message_stop":
                    return
                delta = event.get("delta", {})
                if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    yield delta["text"]
        raise RuntimeError("LLM stream ended before message_stop")

    @staticmethod
    def _echo(messages: list[Message]) -> str:
        """返回最近的用户文本，跳过仅包含 tool_result 的回灌消息。"""
        for message in reversed(messages):
            if message.role == "user":
                if isinstance(message.content, str):
                    return message.content
                text = "".join(
                    block["text"] for block in message.content if block.get("type") == "text"
                )
                if text:
                    return text
        return ""

    async def close(self) -> None:
        """关闭 HTTP 连接池，释放客户端资源。"""
        await self.client.aclose()
