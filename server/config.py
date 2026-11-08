"""配置优先级：初始化参数 > 系统环境变量 > 当前环境 dotenv > TOML > 默认值。"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
from sqlalchemy import URL

ROOT = Path(__file__).resolve().parent.parent
Environment = Literal["development", "testing", "production"]
DEFAULT_REQUEST_MAX_TOKENS = 64000
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_MAX_STEPS = 8
DEFAULT_TOKEN_MARGIN = 256
DEFAULT_TOOL_TIMEOUT_SECONDS = 10


class ContextPolicy(BaseModel):
    """上下文容量与首尾保留策略；阈值基于预留输出后的可用输入容量。"""

    window_tokens: int = Field(default=32000, ge=1)
    keep_head_units: int = Field(default=2, ge=1)
    keep_tail_units: int = Field(default=6, ge=1)
    soft_ratio: float = Field(default=0.70, gt=0, lt=1)
    hard_ratio: float = Field(default=0.85, gt=0, lt=1)
    summary_max_tokens: int = Field(default=1024, ge=1)
    min_interval_seconds: float = Field(default=60, ge=0)

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        """硬阈值必须高于软阈值，避免频次控制掩盖必须执行的压缩。"""
        if self.soft_ratio >= self.hard_ratio:
            raise ValueError("context soft_ratio must be below hard_ratio")
        return self


class ToolResultPolicy(BaseModel):
    """大结果外置与分页读取限制；字符分页与 UTF-8 字节阈值分别计量。"""

    inline_max_bytes: int = Field(default=8192, ge=1)
    preview_chars: int = Field(default=256, ge=0)
    cache_ttl_seconds: int = Field(default=3600, ge=1)


def selected_environment() -> Environment:
    value = os.environ.get("APP_ENV", "development")
    if value not in {"development", "testing", "production"}:
        raise ValueError("APP_ENV must be development, testing or production")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
        env_nested_delimiter="__",
    )

    app_env: Environment = Field(default="development", validation_alias="APP_ENV")
    app_name: str = "Mini Agent"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    reload: bool = False
    docs_enabled: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    cors_origins: list[str] = []

    mysql_enabled: bool = True
    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_user: str = ""
    mysql_password: SecretStr = SecretStr("")
    mysql_db: str = "mini_agent"
    mysql_pool_size: int = Field(default=5, ge=1)
    mysql_max_overflow: int = Field(default=10, ge=0)
    mysql_pool_recycle: int = Field(default=1800, ge=1)
    mysql_connect_timeout: int = Field(default=5, ge=1)

    redis_enabled: bool = False
    redis_host: str = "127.0.0.1"
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_db: int = Field(default=0, ge=0)
    redis_password: SecretStr = SecretStr("")
    redis_ssl: bool = False
    redis_timeout: float = Field(default=3, gt=0)

    llm_provider: Literal["echo", "anthropic"] = "echo"
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_timeout: float = Field(default=60, gt=0)
    llm_max_tokens: int = Field(default=4096, ge=1)
    llm_count_tokens: bool = True
    request_max_tokens: int = Field(default=DEFAULT_REQUEST_MAX_TOKENS, ge=1)
    request_max_output_tokens: int = Field(default=DEFAULT_MAX_OUTPUT_TOKENS, ge=1)
    agent_tool_timeout_seconds: float = Field(default=DEFAULT_TOOL_TIMEOUT_SECONDS, gt=0)
    request_max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1)
    token_safety_margin: int = Field(default=DEFAULT_TOKEN_MARGIN, ge=0)
    llm_auth_header: Literal["x-api-key", "authorization"] = "x-api-key"

    context: ContextPolicy = Field(default_factory=ContextPolicy)
    tool_result: ToolResultPolicy = Field(default_factory=ToolResultPolicy)
    session_cache_ttl_seconds: int = Field(default=300, ge=1)
    agent_run_timeout_seconds: float = Field(default=120, gt=0)
    api_token: SecretStr = SecretStr("")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            TomlConfigSettingsSource(
                settings_cls, toml_file=ROOT / "config" / f"{selected_environment()}.toml"
            ),
        )

    @model_validator(mode="after")
    def validate_services(self) -> Self:
        if self.mysql_enabled and not all((self.mysql_host, self.mysql_user, self.mysql_db)):
            raise ValueError("MySQL requires AGENT_MYSQL_HOST / AGENT_MYSQL_USER / AGENT_MYSQL_DB")
        if self.llm_provider == "anthropic":
            if not all((self.llm_base_url, self.llm_model, self.llm_api_key.get_secret_value())):
                raise ValueError(
                    "LLM requires AGENT_LLM_BASE_URL / AGENT_LLM_MODEL / AGENT_LLM_API_KEY"
                )
            if not self.llm_base_url.startswith(("http://", "https://")):
                raise ValueError("AGENT_LLM_BASE_URL must use http:// or https://")
        if self.app_env == "production":
            if self.reload:
                raise ValueError("Production must disable reload")
            if not self.mysql_enabled or not self.mysql_password.get_secret_value():
                raise ValueError("Production requires MySQL and AGENT_MYSQL_PASSWORD")
            if self.llm_provider == "echo":
                raise ValueError("Production requires a real LLM provider")
        return self

    @property
    def mysql_url(self) -> URL:
        return URL.create(
            "mysql+asyncmy",
            username=self.mysql_user,
            password=self.mysql_password.get_secret_value(),
            host=self.mysql_host,
            port=self.mysql_port,
            database=self.mysql_db,
            query={"charset": "utf8mb4"},
        )


@lru_cache
def get_settings() -> Settings:
    environment = selected_environment()
    return Settings(app_env=environment, _env_file=ROOT / f".env.{environment}")
