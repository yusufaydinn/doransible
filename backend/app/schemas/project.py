"""Project endpoint şemaları (T-102)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.project import DESCRIPTION_MAX_LENGTH, NAME_MAX_LENGTH, PATH_MAX_LENGTH

ProjectName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX_LENGTH),
]
ProjectPath = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=PATH_MAX_LENGTH),
]
ProjectDescription = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=DESCRIPTION_MAX_LENGTH),
]


class ProjectCreate(BaseModel):
    """Yeni project kaydı isteği.

    ``extra="forbid"``: istemci ``id``, ``is_active`` veya türetilmiş
    ``path_key`` gibi alanları set etmeye çalışamaz; bilinmeyen alan 422 üretir.
    """

    model_config = ConfigDict(extra="forbid")

    name: ProjectName = Field(description="Project görünen adı")
    path: ProjectPath = Field(description="Project kökünün mutlak dizin yolu")
    description: ProjectDescription | None = Field(default=None, description="Serbest açıklama")


class ProjectResponse(BaseModel):
    """Project kaydının API gösterimi.

    Türetilmiş ``path_key`` bilinçli olarak dışarı verilmez; iç karşılaştırma
    detayıdır ve API sözleşmesinin parçası değildir.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    path: str = Field(description="Normalize edilmiş kanonik dizin yolu")
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PlaybookEntry(BaseModel):
    """Keşfedilmiş tek bir playbook adayı."""

    model_config = ConfigDict(from_attributes=True)

    path: str = Field(description="Project köküne göreli, POSIX ayraçlı yol")
    name: str = Field(description="Kullanıcıya gösterilecek ad")
    size_bytes: int = Field(description="Dosya boyutu")
    modified_at: datetime = Field(description="Dosyanın son değiştirilme zamanı")


class PlaybookListResponse(BaseModel):
    """Playbook keşfi sonucu.

    Sunucudaki absolute path'ler bilinçli olarak dışarı verilmez; yalnızca
    project köküne göreli yollar döner (GUVENLIK.md bölüm 3).
    """

    model_config = ConfigDict(from_attributes=True)

    project_id: int
    playbooks: list[PlaybookEntry]
    skipped_unreadable_files: int = Field(
        description=(
            "Okunamadığı için listeye alınmayan aday dosya sayısı. "
            "Tipi belirlenemeyen girdiler de buraya sayılır."
        )
    )
    skipped_unreadable_directories: int = Field(
        description="Listelenemediği için taranamayan alt dizin sayısı"
    )
    truncated: bool = Field(description="Keşif sınırına takılıp liste kırpıldıysa true")
    scanned_at: datetime
