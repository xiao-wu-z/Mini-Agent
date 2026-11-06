"""配置优先级：初始化参数 > 系统环境变量 > 当前环境 dotenv > TOML > 默认值。"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
from sqlalchemy import URL

ROOT = Path(__file__).resolve().parent.parent
Environment = Literal["development", "testing", "production"]


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

    llm_provider: Literal["echo", "openai_compatible"] = "echo"
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_timeout: float = Field(default=60, gt=0)

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
        if self.llm_provider == "openai_compatible":
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
