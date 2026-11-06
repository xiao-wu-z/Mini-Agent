from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

import server.app as app_module
from server.config import get_settings


def test_enabled_services_are_checked_and_closed(monkeypatch):
    monkeypatch.setenv("AGENT_MYSQL_ENABLED", "true")
    monkeypatch.setenv("AGENT_REDIS_ENABLED", "true")
    database, redis, llm = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(app_module, "Database", lambda _: database)
    monkeypatch.setattr(app_module, "create_redis", lambda _: redis)
    monkeypatch.setattr(app_module, "LLMClient", lambda _: llm)
    with TestClient(app_module.create_app(get_settings())) as client:
        assert client.get("/health/ready").json()["checks"] == {"mysql": "ok", "redis": "ok"}
    database.close.assert_awaited_once()
    redis.aclose.assert_awaited_once()
    llm.close.assert_awaited_once()


def test_startup_failure_closes_created_resources(monkeypatch):
    import pytest

    monkeypatch.setenv("AGENT_MYSQL_ENABLED", "true")
    database = AsyncMock()
    monkeypatch.setattr(app_module, "Database", lambda _: database)

    def fail(_):
        raise RuntimeError("client initialization failed")

    monkeypatch.setattr(app_module, "LLMClient", fail)
    with pytest.raises(RuntimeError, match="client initialization failed"):
        with TestClient(app_module.create_app(get_settings())):
            pass
    database.close.assert_awaited_once()
