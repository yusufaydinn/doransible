"""T-002 kabul kriteri: ``/health`` 200 döner."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import __version__


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app_name": "DORAnsible",
        "version": __version__,
        "environment": "test",
    }


def test_health_does_not_leak_infrastructure_details(client: TestClient) -> None:
    """Health cevabı DSN, dosya yolu veya secret sızdırmamalıdır."""
    body = client.get("/health").text.lower()

    assert "sqlite" not in body
    assert "app-data" not in body
    assert "app_data_dir" not in body
    assert "database_url" not in body
