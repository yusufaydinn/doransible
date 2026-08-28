"""Health endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app import __version__
from app.core.config import Settings, get_settings
from app.schemas.health import HealthResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="Servis durumu")
def read_health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Backend'in çalıştığını doğrular.

    Bu endpoint dış bağımlılıkları (veritabanı, LLM provider) kontrol etmez;
    yalnızca sürecin ayakta olduğunu bildirir.
    """
    return HealthResponse(
        app_name=settings.app_name,
        version=__version__,
        environment=settings.environment,
    )
