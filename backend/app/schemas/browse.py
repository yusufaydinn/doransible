"""Controller path browse endpoint şeması (R1-V3J0C)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.services.browse.service import BrowseScope, EntryKind


class BrowseEntryResponse(BaseModel):
    """Listelenen tek bir dizin/dosya girdisi.

    Bilinçli olarak **yok**: ``size``, ``modified_at``, owner/permission
    bilgisi, dosya içeriği. Bir seçici için gereksizdir; her eklenen alan
    yeni bir sızıntı/uyum yüküdür.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str
    path: str = Field(description="Kanonik, çözümlenmiş mutlak yol")
    kind: EntryKind
    selectable: bool = Field(description="Bu scope'ta bu girdinin seçilebilir olup olmadığı")


class BrowseResponse(BaseModel):
    """``GET /api/controller-paths`` cevabı.

    Backend parent/breadcrumb hesaplamaz; frontend kendi navigasyon yığınını
    tutar. ``current_path`` yalnızca sentetik kök seçici görünümünde
    ``null``dır (birden fazla allowlist kökü varken).
    """

    model_config = ConfigDict(from_attributes=True)

    scope: BrowseScope
    current_path: str | None = Field(
        description="Listelenen dizin; sentetik kök seçici görünümünde null"
    )
    target_kind: EntryKind = Field(description="Bu scope'ta seçilebilir girdi türü")
    entries: list[BrowseEntryResponse]
    truncated: bool = Field(description="Dizin girdi sayısı sunucu sınırını aştıysa true")
