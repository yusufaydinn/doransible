"""Inventory endpoint şemaları (T-201)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.inventory import NAME_MAX_LENGTH, PATH_MAX_LENGTH, InventorySourceType

InventoryName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX_LENGTH),
]
InventoryPath = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=PATH_MAX_LENGTH),
]


class InventoryCreate(BaseModel):
    """Yeni inventory kaydı isteği.

    ``extra="forbid"``: istemci ``id`` veya zaman damgaları gibi sunucuya ait
    alanları set etmeye çalışamaz; bilinmeyen alan 422 üretir.

    ``source_type`` serbest metin değil enum'dur; ``ini`` ve ``yaml`` dışındaki
    her değer istek doğrulamasında reddedilir.
    """

    model_config = ConfigDict(extra="forbid")

    name: InventoryName = Field(description="Inventory görünen adı")
    path: InventoryPath = Field(description="Inventory dosyasının mutlak yolu")
    source_type: InventorySourceType = Field(description="Dosya biçimi: ini veya yaml")
    project_id: int | None = Field(
        default=None,
        ge=1,
        description="Bağlanacak project; verilmezse inventory standalone olur",
    )


class InventoryResponse(BaseModel):
    """Inventory kaydının API gösterimi.

    Dosya **içeriği** döndürülmez; bu görev yalnızca metadata yönetir. Host ve
    grup önizlemesi T-202 kapsamındadır.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int | None
    name: str
    path: str = Field(description="Normalize edilmiş kanonik dosya yolu")
    source_type: InventorySourceType
    created_at: datetime
    updated_at: datetime


class InventoryGroupResponse(BaseModel):
    """Bir inventory grubu ve etkin host listesi."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    hosts: list[str] = Field(description="Alt gruplardan gelenler dâhil, ada göre sıralı")


class InventoryHostResponse(BaseModel):
    """Tek bir host; ait olduğu gruplar ve maskelenmiş değişkenleri."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    groups: list[str] = Field(description="Üst gruplar dâhil, ada göre sıralı")
    variables: dict[str, Any] = Field(
        description=(
            "Host değişkenleri. Secret anahtarları ve secret görünümlü değerler "
            "`***` ile maskelenir (GUVENLIK.md bölüm 9)."
        )
    )


class InventoryHostsResponse(BaseModel):
    """Inventory içeriğinin normalize edilmiş gösterimi (T-202).

    Ham ``ansible-inventory --list`` JSON'u bilinçli olarak dışarı verilmez:
    o çıktı sürüme bağlı, grup merkezli ve maskelenmemiştir. Buradaki gösterim
    kararlı sıralıdır; istemci sıralamaya güvenebilir.
    """

    model_config = ConfigDict(from_attributes=True)

    inventory_id: int
    groups: list[InventoryGroupResponse]
    hosts: list[InventoryHostResponse]
