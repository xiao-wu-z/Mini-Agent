from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from server.app import create_app


def test_health() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_with_disabled_services() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"mysql": "disabled", "redis": "disabled"}


def test_readiness_failure_does_not_break_liveness() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.database = AsyncMock()
        app.state.database.ping.side_effect = RuntimeError("private database details")
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["mysql"] == "error"
        assert "private" not in response.text
        assert client.get("/health").status_code == 200
