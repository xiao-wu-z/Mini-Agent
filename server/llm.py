"""OpenAI 兼容协议客户端；供业务层调用，不暴露公共付费调用接口。"""

import json
from collections.abc import AsyncIterator
from typing import Literal

import httpx
from pydantic import BaseModel

from server.config import Settings


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=(settings.llm_base_url or "http://localhost").rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}"},
            timeout=settings.llm_timeout,
            trust_env=False,
            transport=transport,
        )

    async def complete(self, messages: list[Message]) -> str:
        if self.settings.llm_provider == "echo":
            return self._echo(messages)
        response = await self.client.post(
            "chat/completions",
            json={
                "model": self.settings.llm_model,
                "messages": [message.model_dump() for message in messages],
                "stream": False,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"] or ""

    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        if self.settings.llm_provider == "echo":
            yield self._echo(messages)
            return
        async with self.client.stream(
            "POST",
            "chat/completions",
            json={
                "model": self.settings.llm_model,
                "messages": [message.model_dump() for message in messages],
                "stream": True,
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                if not data:
                    continue
                event = json.loads(data)
                if "error" in event:
                    raise RuntimeError("LLM provider returned a stream error")
                choices = event.get("choices", [])
                if choices and (content := choices[0].get("delta", {}).get("content")):
                    yield content

    @staticmethod
    def _echo(messages: list[Message]) -> str:
        return next((item.content for item in reversed(messages) if item.role == "user"), "")

    async def close(self) -> None:
        await self.client.aclose()
