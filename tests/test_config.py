import pytest
from pydantic import ValidationError

from server.app import create_app
from server.config import get_settings


def test_testing_defaults():
    settings = get_settings()
    assert settings.app_env == "testing"
    assert settings.mysql_db == "mini_agent_test"
    assert settings.mysql_enabled is False
    assert settings.llm_provider == "echo"


def test_development_defaults(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    settings = get_settings()
    assert settings.reload is True
    assert settings.mysql_db == "mini_agent_dev"


def test_sources_and_environment_isolation(isolated_environment, monkeypatch):
    (isolated_environment / ".env.development").write_text("AGENT_MYSQL_DB=wrong_database\n")
    (isolated_environment / ".env.testing").write_text(
        "AGENT_MYSQL_DB=dotenv_database\nAGENT_LLM_TIMEOUT=12\n"
    )
    monkeypatch.setenv("AGENT_LLM_TIMEOUT", "25")
    settings = get_settings()
    assert settings.mysql_db == "dotenv_database"
    assert settings.llm_timeout == 25


def test_unknown_environment_rejected(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prodution")
    with pytest.raises(ValueError, match="APP_ENV"):
        get_settings()


def test_missing_llm_key_rejected(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_PROVIDER", "openai_compatible")
    with pytest.raises(ValidationError, match="AGENT_LLM_API_KEY"):
        get_settings()


def test_production_requires_credentials(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValidationError):
        get_settings()


def production_environment(monkeypatch):
    for key, value in {
        "APP_ENV": "production",
        "AGENT_MYSQL_HOST": "mysql.example.invalid",
        "AGENT_MYSQL_USER": "service",
        "AGENT_MYSQL_PASSWORD": "test@password:/#%",
        "AGENT_LLM_BASE_URL": "https://llm.example.invalid/v1",
        "AGENT_LLM_MODEL": "test-model",
        "AGENT_LLM_API_KEY": "test-private-key",
    }.items():
        monkeypatch.setenv(key, value)


def test_production_disables_docs_and_hides_secrets(monkeypatch):
    production_environment(monkeypatch)
    settings = get_settings()
    assert settings.reload is False
    assert settings.docs_enabled is False
    assert create_app(settings).openapi_url is None
    assert "test-private-key" not in repr(settings)
    assert "test@password" not in repr(settings)
    assert settings.mysql_url.password == "test@password:/#%"
    assert "test@password" not in str(settings.mysql_url)


@pytest.mark.parametrize(
    ("key", "value"),
    [("AGENT_RELOAD", "true"), ("AGENT_MYSQL_ENABLED", "false"), ("AGENT_MYSQL_PASSWORD", "")],
)
def test_production_rejects_invalid_overrides(monkeypatch, key, value):
    production_environment(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        get_settings()
