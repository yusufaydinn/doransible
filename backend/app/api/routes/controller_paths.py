"""Controller path browse endpoint'i (R1-V3J0C).

Project ve Inventory formlarındaki manuel path alanının yanına eklenen
"Gözat…" dialogunun tek backend yüzeyidir. Bu bir upload veya tarayıcının
native ``input[type=file]`` özelliği **değildir**: tarayıcı cihazının dosya
sistemi hiç okunmaz, yalnızca controller'daki izinli yollar listelenir.

Route yalnızca istek/cevap dönüşümü yapar; allowlist kontrolü, symlink
eleme ve sınır kararı ``app.services.browse`` içindedir (route/service katman ayrımı sözleşmesi).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.schemas.browse import BrowseResponse
from app.services.browse import BrowseListing, BrowseScope, list_controller_paths
from app.services.security.paths import MAX_PATH_LENGTH

router = APIRouter(prefix="/controller-paths", tags=["controller-paths"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get(
    "",
    response_model=BrowseResponse,
    summary="Controller'daki izinli dizin/dosyalarda gez",
)
def browse_controller_paths(
    session: SessionDep,
    settings: SettingsDep,
    scope: BrowseScope,
    project_id: Annotated[
        int | None,
        Query(ge=1, description="Yalnız project_inventory scope'unda zorunlu"),
    ] = None,
    path: Annotated[
        str | None,
        Query(
            max_length=MAX_PATH_LENGTH,
            description="Listelenecek dizin; verilmezse başlangıç görünümü döner",
        ),
    ] = None,
) -> BrowseListing:
    """İzinli bir kök altında **tek seviye** dizin listelemesi döndürür.

    Recursive taramaz, dosya içeriği okumaz, subprocess çalıştırmaz, yazmaz.
    Allowlist kontrolü her zaman varlık kontrolünden **önce** uygulanır; bu
    yüzden allowlist dışındaki mevcut ve mevcut olmayan bir yol aynı generic
    403 cevabını üretir (GUVENLIK.md bölüm 4).
    """
    return list_controller_paths(
        session,
        scope=scope,
        project_id=project_id,
        path=path,
        project_roots=settings.resolve_project_root_allowlist(),
        inventory_roots=settings.resolve_inventory_root_allowlist(),
    )
