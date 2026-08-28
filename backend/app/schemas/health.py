"""Health endpoint şemaları."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Backend'in ayakta olduğunu bildiren cevap.

    Yalnızca kamuya açık bilgi taşır; DSN, path ve secret değerleri
    döndürülmez (GUVENLIK.md bölüm 3).
    """

    status: Literal["ok"] = "ok"
    app_name: str = Field(description="Uygulama görünen adı")
    version: str = Field(description="Backend sürümü")
    environment: str = Field(description="Çalışma ortamı adı")
