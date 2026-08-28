"""Domain hata tipleri ve standart API hata cevabı.

Bütün hata cevapları tek bir zarf kullanır::

    {"error": {"code": "not_found", "message": "...", "details": null}}

Böylece frontend tek bir yapı üzerinden hata gösterebilir (route/service katman ayrımı sözleşmesi).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """Uygulama domain hatalarının ortak atası.

    Alt sınıflar ``status_code`` ve ``code`` alanlarını ezerek HTTP karşılığını
    belirler. Hata mesajları kullanıcıya gösterilir; bu nedenle secret değer
    içermemelidir (GUVENLIK.md bölüm 3).
    """

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    """İstenen kayıt veya dosya bulunamadı."""

    status_code = 404
    code = "not_found"


class ValidationFailedError(AppError):
    """Girdi doğrulaması domain kurallarına takıldı."""

    status_code = 422
    code = "validation_failed"


# Doğrulama hatasında dışarı verilen alanlar. `input` ve `ctx` bilinçli olarak
# **yoktur**: Pydantic `input` içine kullanıcının gönderdiği ham değeri koyar ve
# bu, "token hata cevabında yer almaz" sözleşmesini doğrudan ihlal ederdi.
# Ölçülen örnek: 129 karakterlik bir `preview_token` gönderildiğinde
# `string_too_long` hatasının `input` alanı token'ın tamamını geri yansıtıyordu.
# `ctx` de kullanıcı değeri veya serialize edilemeyen bir exception nesnesi
# taşıyabildiği için tümüyle atılır.
_SAFE_VALIDATION_FIELDS = ("type", "loc", "msg")

# Cevabı sınırlı tutar; girdi ne kadar bozuk olursa olsun boyut patlamaz.
MAX_VALIDATION_ERRORS = 20
MAX_VALIDATION_FIELD_LENGTH = 200


def sanitize_validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Pydantic doğrulama hatalarından kullanıcı girdisini çıkarır.

    Yalnızca ``type``, ``loc`` ve genel ``msg`` korunur; gönderilen değerin
    kendisi hiçbir alana girmez. Ham request body de loglanmaz.

    Args:
        errors: ``RequestValidationError.errors()`` çıktısı.

    Returns:
        Yalnızca güvenli alanları taşıyan, sayısı ve uzunluğu sınırlı liste.
    """
    sanitized: list[dict[str, Any]] = []
    for error in errors[:MAX_VALIDATION_ERRORS]:
        if not isinstance(error, Mapping):  # pragma: no cover - savunma amaçlı
            continue
        sanitized.append(
            {
                "type": _clip(str(error.get("type", "validation_error"))),
                # `loc` alan **adlarını** taşır, değerleri değil. `extra=forbid`
                # durumunda son parça istemcinin gönderdiği fazladan alanın adı
                # olabilir; bu yüzden o da kırpılır.
                "loc": [_clip(str(part)) for part in _as_sequence(error.get("loc"))],
                "msg": _clip(str(error.get("msg", ""))),
            }
        )
    return sanitized


def _as_sequence(value: Any) -> Sequence[Any]:
    """``loc`` değerini güvenli biçimde sıralanabilir hâle getirir."""
    if isinstance(value, (list, tuple)):
        return value
    return () if value is None else (value,)


def _clip(value: str) -> str:
    """Tek bir alanı üst sınıra kırpar."""
    if len(value) <= MAX_VALIDATION_FIELD_LENGTH:
        return value
    return value[:MAX_VALIDATION_FIELD_LENGTH] + "…"


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Standart hata zarfını içeren bir ``JSONResponse`` üretir."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Uygulamaya standart hata cevabı üreten handler'ları bağlar."""

    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            422,
            "request_validation_error",
            "İstek gövdesi veya parametreleri geçersiz.",
            sanitize_validation_errors(exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error_response(
            exc.status_code,
            _HTTP_ERROR_CODES.get(exc.status_code, "http_error"),
            str(exc.detail),
            headers=exc.headers,
        )


_HTTP_ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
}
