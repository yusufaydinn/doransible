"""Inventory CRUD, içerik, ping preview ve ping geçmişi endpoint'leri.

(T-201, T-202, T-204A, T-204B2, R1-V3J1A.)

Route'lar yalnızca istek/cevap dönüşümü yapar; path doğrulaması, allowlist
kontrolü, project bağı kararı, SQL, artifact okuma ve belge doğrulaması
``app.services.inventories`` içindedir (route/service katman ayrımı sözleşmesi). Aktör hiçbir
endpoint'te istekten (body, path, header veya query) alınmaz; yalnız
``settings.local_actor`` kullanılır.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models import Inventory
from app.schemas.inventory import InventoryCreate, InventoryHostsResponse, InventoryResponse
from app.schemas.ping import (
    PingHistoryResponse,
    PingPreviewCreate,
    PingPreviewResponse,
    PingPreviewTokenRequest,
    PingRunResponse,
)
from app.services import inventories as inventory_service
from app.services.inventories import (
    DEFAULT_PING_HISTORY_LIMIT,
    MAX_PING_HISTORY_LIMIT,
    MIN_PING_HISTORY_LIMIT,
    InventoryContents,
    ParserLimits,
)
from app.services.inventories.ping import PingPreview
from app.services.inventories.ping_confirm import PingRun
from app.services.inventories.ping_history import PingHistoryPage
from app.services.jobs import PreviewStore
from app.services.jobs.artifacts import JobArtifactStore

router = APIRouter(prefix="/inventories", tags=["inventories"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post(
    "",
    response_model=InventoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Inventory kaydet",
)
def create_inventory(
    payload: InventoryCreate,
    session: SessionDep,
    settings: SettingsDep,
) -> Inventory:
    """Var olan bir inventory dosyasını kaydeder.

    Dosya kopyalanmaz, oluşturulmaz ve **okunmaz**; yalnızca kaydı tutulur.

    İki allowlist birlikte verilir çünkü sınır akışa göre değişir: standalone
    inventory ``inventory_roots`` altında, project'e bağlı inventory ise
    project'in kendi kökü altında olmalıdır (ADR-015).
    """
    return inventory_service.create_inventory(
        session,
        name=payload.name,
        path=payload.path,
        source_type=payload.source_type,
        project_id=payload.project_id,
        inventory_roots=settings.resolve_inventory_root_allowlist(),
        project_roots=settings.resolve_project_root_allowlist(),
    )


@router.get("", response_model=list[InventoryResponse], summary="Inventory listesi")
def list_inventories(
    session: SessionDep,
    project_id: Annotated[
        int | None, Query(ge=1, description="Yalnızca bu project'e bağlı kayıtlar")
    ] = None,
) -> list[Inventory]:
    """Kayıtlı inventory'leri döndürür."""
    return inventory_service.list_inventories(session, project_id=project_id)


@router.get("/{inventory_id}", response_model=InventoryResponse, summary="Inventory detayı")
def get_inventory(inventory_id: int, session: SessionDep) -> Inventory:
    """Tek bir inventory kaydını döndürür."""
    return inventory_service.get_inventory(session, inventory_id)


@router.get(
    "/{inventory_id}/hosts",
    response_model=InventoryHostsResponse,
    summary="Inventory içindeki host ve grupları listele",
)
def get_inventory_hosts(
    inventory_id: int,
    session: SessionDep,
    settings: SettingsDep,
) -> InventoryContents:
    """Kayıtlı inventory'nin host ve grup içeriğini döndürür (T-202).

    Endpoint **path veya komut parametresi almaz**: okunacak dosya yalnızca
    veritabanındaki kayıttan belirlenir ve kayıt kullanım anında yeniden
    doğrulanır (GUVENLIK.md bölüm 4).
    """
    return inventory_service.get_inventory_hosts(
        session,
        inventory_id,
        inventory_roots=settings.resolve_inventory_root_allowlist(),
        project_roots=settings.resolve_project_root_allowlist(),
        command=settings.ansible_inventory_command,
        limits=ParserLimits.from_settings(settings),
    )


def _preview_store(settings: Settings) -> PreviewStore:
    """Ayarlardan preview state deposu üretir."""
    return PreviewStore(
        settings.resolve_ping_preview_dir(),
        ttl_seconds=settings.ping_preview_ttl_seconds,
        claim_stale_seconds=settings.ping_preview_claim_stale_seconds,
    )


@router.post(
    "/{inventory_id}/ping/preview",
    response_model=PingPreviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Ping onay planı oluştur",
)
def create_ping_preview(
    inventory_id: int,
    payload: PingPreviewCreate,
    session: SessionDep,
    settings: SettingsDep,
) -> PingPreview:
    """Gerçek execution öncesinde onaylanacak planı üretir (T-204A).

    Bu endpoint **hiçbir SSH bağlantısı kurmaz ve ping çalıştırmaz**; Job
    kaydı veya artifact dizini de oluşturmaz. Yalnızca inventory'yi bir kez
    okur, güvenlik doğrulamalarını yapar, hedef kümesini dondurur ve tek
    kullanımlık bir onay token'ı döndürür (GUVENLIK.md bölüm 2 ve 7).
    """
    return inventory_service.create_ping_preview(
        session,
        inventory_id,
        limit=payload.limit,
        inventory_roots=settings.resolve_inventory_root_allowlist(),
        project_roots=settings.resolve_project_root_allowlist(),
        key_roots=settings.resolve_ssh_key_root_allowlist(),
        command=settings.ansible_inventory_command,
        limits=ParserLimits.from_settings(settings),
        store=_preview_store(settings),
        host_key_policy=settings.ssh_host_key_policy,
        max_listed_hosts=settings.ping_preview_max_listed_hosts,
        requested_by=settings.local_actor,
    )


@router.post(
    "/{inventory_id}/ping/preview/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Ping onay planını iptal et",
)
def cancel_ping_preview(
    inventory_id: int,
    payload: PingPreviewTokenRequest,
    settings: SettingsDep,
) -> Response:
    """Onay planını iptal eder ve state'ini temizler.

    Token doğrulaması idempotenttir: bilinmeyen, biçimsiz, süresi geçmiş,
    bu inventory veya aktörle eşleşmeyen ya da daha önce kullanılmış bir token
    da ``204`` alır. Aksi hâlde cevap farkı, bir token'ın var olup olmadığını
    sızdıran bir oracle olurdu.

    Altyapı arızası bunun istisnasıdır: state temizlenemezse ``500
    ping_preview_unavailable`` döner. Başarısızlıklar sessizce yutulmaz
    (hata görünürlüğü sözleşmesi).
    """
    inventory_service.cancel_ping_preview(
        payload.preview_token,
        store=_preview_store(settings),
        inventory_id=inventory_id,
        requested_by=settings.local_actor,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{inventory_id}/ping",
    response_model=PingRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Onaylanmış ping planını çalıştır",
)
def run_inventory_ping(
    inventory_id: int,
    payload: PingPreviewTokenRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> PingRun:
    """Onaylanmış planı gerçek SSH bağlantılarıyla çalıştırır (T-204B2).

    Gövde **yalnızca** ``preview_token`` taşır: limit, timeout, forks, modül,
    modül argümanı ve inventory path'i istemciden alınmaz. Hedef kümesi ve
    bağlantı alanları, preview sırasında dondurulmuş ve token ile claim edilen
    snapshot'tan gelir; özgün inventory dosyası **yeniden okunmaz**.

    Geçerli bir Ansible sonucu (unreachable veya failed host) altyapı hatası
    değildir ve ``200`` döner; Job durumu ``failed`` olur.
    """
    return inventory_service.confirm_ping(
        session,
        inventory_id,
        preview_token=payload.preview_token,
        store=_preview_store(settings),
        artifacts=JobArtifactStore(settings.app_data_dir),
        key_roots=settings.resolve_ssh_key_root_allowlist(),
        command=settings.ansible_ad_hoc_command,
        app_data_dir=settings.app_data_dir,
        known_hosts_path=settings.ssh_known_hosts_path,
        host_key_policy=settings.ssh_host_key_policy,
        forks=settings.ping_forks,
        connect_timeout=settings.ssh_connect_timeout_seconds,
        timeout_seconds=settings.ping_timeout_seconds,
        max_output_bytes=settings.ping_max_output_bytes,
        job_stale_seconds=settings.job_stale_seconds,
        requested_by=settings.local_actor,
    )


@router.get(
    "/{inventory_id}/ping-runs",
    response_model=PingHistoryResponse,
    summary="Tamamlanmış ping ölçümlerinin geçmişi",
)
def list_inventory_ping_runs(
    inventory_id: int,
    session: SessionDep,
    settings: SettingsDep,
    response: Response,
    limit: Annotated[
        int,
        Query(
            ge=MIN_PING_HISTORY_LIMIT,
            le=MAX_PING_HISTORY_LIMIT,
            description="Döndürülecek azami ölçüm sayısı; en yeni ölçüm başta",
        ),
    ] = DEFAULT_PING_HISTORY_LIMIT,
) -> PingHistoryPage:
    """Bir inventory'nin tamamlanmış ping ölçümlerini en yeni önce döndürür (R1-V3J1A).

    Endpoint **salt okunurdur**: yeni bir ölçüm başlatmaz, hiçbir SSH bağlantısı
    kurmaz, Job kaydı veya artifact yazmaz. Yalnızca zaten kalıcı olan Job
    satırları ile onların yayımlanmış ``result.json`` belgeleri okunur.

    Cevap host adı, host mesajı, aktör, artifact yolu, project/inventory path'i
    ve ham çıktı **taşımaz**; yalnız kimlik, durum, çıkış kodu, zaman damgaları
    ve durum sayımları döner (GUVENLIK.md bölüm 3).

    ``Cache-Control: no-store``: ölçüm geçmişi bir ara katmanda veya tarayıcı
    diskinde saklanmamalıdır.
    """
    response.headers["Cache-Control"] = "no-store"
    return inventory_service.list_ping_runs(
        session,
        inventory_id,
        requested_by=settings.local_actor,
        app_data_dir=settings.app_data_dir,
        limit=limit,
    )
