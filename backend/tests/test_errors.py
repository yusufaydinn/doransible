"""Standart hata cevabı davranışı (route/service katman ayrımı sözleşmesi)."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import (
    MAX_VALIDATION_ERRORS,
    MAX_VALIDATION_FIELD_LENGTH,
    AppError,
    NotFoundError,
    ValidationFailedError,
    sanitize_validation_errors,
)
from app.main import create_app


def test_unknown_route_returns_standard_error_envelope(client: TestClient) -> None:
    response = client.get("/bilinmeyen-endpoint")

    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "not_found"
    assert isinstance(payload["error"]["message"], str)


def test_method_not_allowed_uses_error_envelope(client: TestClient) -> None:
    response = client.post("/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    # Hata zarfı, HTTP semantiği için gereken başlıkları korumalıdır.
    assert "GET" in response.headers["allow"]


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (NotFoundError("Project bulunamadı."), 404, "not_found"),
        (ValidationFailedError("Path geçersiz."), 422, "validation_failed"),
        (AppError("Beklenmeyen durum."), 500, "internal_error"),
    ],
)
def test_app_errors_are_mapped_to_envelope(
    settings: Settings,
    error: AppError,
    expected_status: int,
    expected_code: str,
) -> None:
    app = create_app(settings)
    _add_failing_route(app, error)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/__test__/boom")

    assert response.status_code == expected_status
    assert response.json()["error"] == {
        "code": expected_code,
        "message": str(error),
        "details": None,
    }


def _add_failing_route(app: FastAPI, error: AppError) -> None:
    """Hata handler'larını doğrulamak için geçici bir route ekler."""

    @app.get("/__test__/boom")
    def _boom() -> None:
        raise error


# --- Doğrulama hatası sanitizasyonu -------------------------------------------


def test_validation_details_never_carry_the_submitted_value() -> None:
    """`input` ve `ctx` dışarı verilmez.

    Pydantic hata yapısı gönderilen ham değeri `input` alanında taşır; standart
    handler bunu olduğu gibi döndürseydi "token hata cevabında yer almaz"
    sözleşmesi ihlal edilirdi.
    """
    sanitized = sanitize_validation_errors(
        [
            {
                "type": "string_too_long",
                "loc": ("body", "preview_token"),
                "msg": "String should have at most 128 characters",
                "input": "GIZLI_TOKEN_" + "x" * 200,
                "ctx": {"max_length": 128, "error": ValueError("ham")},
                "url": "https://errors.pydantic.dev/2/v/string_too_long",
            }
        ]
    )

    assert sanitized == [
        {
            "type": "string_too_long",
            "loc": ["body", "preview_token"],
            "msg": "String should have at most 128 characters",
        }
    ]
    assert "GIZLI_TOKEN_" not in json.dumps(sanitized)


def test_sanitized_details_are_json_serializable() -> None:
    """`ctx` içindeki exception nesnesi cevabı bozmamalıdır."""
    sanitized = sanitize_validation_errors(
        [{"type": "value_error", "loc": ("body",), "msg": "x", "ctx": {"error": OSError()}}]
    )

    assert json.loads(json.dumps(sanitized)) == sanitized


def test_an_extra_field_name_is_clipped_not_dropped() -> None:
    """`loc` alan adlarını taşır; uzun bir ad kırpılır."""
    sanitized = sanitize_validation_errors(
        [{"type": "extra_forbidden", "loc": ("body", "z" * 500), "msg": "Extra inputs"}]
    )

    location = sanitized[0]["loc"][1]
    assert len(location) <= MAX_VALIDATION_FIELD_LENGTH + 1
    assert location.startswith("zzz")


def test_the_number_of_reported_errors_is_bounded() -> None:
    """Bozuk bir gövde cevabı şişiremez."""
    errors = [{"type": "missing", "loc": ("body", f"f{index}"), "msg": "x"} for index in range(100)]

    assert len(sanitize_validation_errors(errors)) == MAX_VALIDATION_ERRORS


def test_validation_errors_keep_the_standard_envelope_and_code(
    client: TestClient,
) -> None:
    """Mevcut zarf ve `request_validation_error` kodu korunur."""
    response = client.post("/api/projects", json={})

    assert response.status_code == 422
    payload = response.json()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message", "details"}
    assert payload["error"]["code"] == "request_validation_error"
    for item in payload["error"]["details"]:
        assert set(item) == {"type", "loc", "msg"}
