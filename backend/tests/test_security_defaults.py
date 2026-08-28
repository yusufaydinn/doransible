"""Güvenli varsayılanların regresyona uğramadığını doğrular (GUVENLIK.md)."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent


def test_cors_allows_only_configured_origin(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": "http://localhost:5173"})

    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_rejects_unknown_origin(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": "http://evil.example"})

    assert "access-control-allow-origin" not in response.headers


def test_env_example_contains_no_assigned_secret_values() -> None:
    """`.env.example` gerçek değer içermemelidir (GUVENLIK.md bölüm 3)."""
    content = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8")
    secret_assignment = re.compile(
        r"^\s*[A-Z_]*(KEY|TOKEN|PASSWORD|SECRET)[A-Z_]*\s*=\s*\S+",
        re.MULTILINE,
    )

    assert secret_assignment.search(content) is None


def test_gitignore_excludes_runtime_and_secret_paths() -> None:
    lines = {
        line.strip() for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }

    for required in ("app-data/", ".env", "*.key", "*.pem", "secrets/"):
        assert required in lines
