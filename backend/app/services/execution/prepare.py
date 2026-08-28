"""Execution planını onaya hazırlama akışı (R1-V2).

R1-V1'in bilgilendirici önizlemesi ile bu akış arasındaki fark, tek bir cümlede
toplanır: **önizleme okur, hazırlama dondurur.** Hazırlama, kullanıcının
onaylayacağı içeriği kopyalayıp sabitler ve o dondurulmuş içeriğe bağlı,
TTL'li, tek kullanımlık bir token üretir.

Bu dilimde yine **hiçbir şey çalıştırılmaz**: `ansible-runner` çağrılmaz,
`ansible-playbook` başlatılmaz, Job kaydı, artifact ve SSH bağlantısı yoktur.
Başlatılan tek alt süreç, özgün inventory'yi okuyan `ansible-inventory`'dir.

Sıra bilinçlidir::

    bounded temizlik
    → project kaydı + aktiflik
    → inventory kaydı + project bağı
    → playbook keşif ön kontrolü (özgün ağaç)
    → inventory parser + allowlist doğrulaması (özgün dosya)
    → FREEZE: project ağacı + normalize inventory snapshot'ı kopyalanır
    → plan **yalnız dondurulmuş içerikten** yeniden hesaplanır
    → token üretilir ve kaydedilir

Dondurma tamamlandıktan sonra özgün project ağacı ve özgün inventory dosyası
**bir daha açılmaz**. Plan da manifest de dondurulmuş kopyayı anlatır; aksi
hâlde kullanıcı bir içeriği onaylarken plan başka bir içeriği tarif ederdi.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import ExecutionMode, Inventory, Project
from app.services.ansible.inventory_snapshot import render_full_snapshot, snapshot_host_names
from app.services.execution.plan import (
    MAX_PREVIEW_HOSTS,
    NOT_EXECUTABLE_REASON,
    PLAN_BECOME,
    PLAN_BINDING,
    PLAN_CONNECTION,
    PLAN_LIMIT,
    PLAN_SKIP_TAGS,
    PLAN_TAGS,
    ExecutionPlan,
    ExecutionPlanInventory,
    ExecutionPlanPlaybook,
    ExecutionPlanProject,
    PlaybookNotDiscoveredError,
    resolve_active_project,
    resolve_discovered_playbook,
    resolve_linked_inventory,
    resolve_snapshot_plan,
)
from app.services.execution.store import (
    MAX_SWEEP_RECORDS,
    input_fingerprint,
    store_prepared_plan,
    sweep_expired_plans,
)
from app.services.execution.workspace import (
    freeze_workspace,
    read_frozen_inventory,
    remove_workspace,
    workspace_project_root,
)
from app.services.inventories.parser import ParserLimits
from app.services.projects.discovery import ScanLimits, discover_playbooks
from app.services.projects.service import ProjectPathUnavailableError, resolve_project_root


@dataclass(frozen=True)
class PreparedExecutionPlan:
    """Onaya hazırlanmış plan.

    ``token`` yalnızca bu nesnede ve tek bir HTTP cevabında bulunur; hiçbir
    yerde saklanmaz, loglanmaz ve tekrar üretilemez.
    """

    plan_id: str
    token: str
    expires_at: datetime
    manifest_digest: str
    plan: ExecutionPlan


def prepare_execution_plan(
    session: Session,
    project_id: int,
    *,
    mode: ExecutionMode,
    inventory_id: int,
    playbook_path: str,
    requested_by: str,
    workspace_root: Path,
    project_roots: Sequence[Path],
    inventory_roots: Sequence[Path],
    key_roots: Sequence[Path],
    command: Sequence[str],
    parser_limits: ParserLimits,
    scan_limits: ScanLimits,
    host_key_policy: str,
    ttl_seconds: float,
    now: datetime | None = None,
) -> PreparedExecutionPlan:
    """Planı dondurur ve tek kullanımlık token üretir; **hiçbir şey çalıştırmaz**.

    ``requested_by`` sunucunun kendi ayarından gelir ve plana bağlanır (R1-V3A);
    aynı değer claim koşulunun parçasıdır. İstemci aktörü seçemez ve aktör API
    cevabına çıkmaz.

    Execution mode ise (R1-V3H2A) çağıranın **açıkça** verdiği değerdir:
    kipi hem girdi özetine hem de plan satırına yazan tek kaynak budur. Zorunlu
    ve default'suz olması bilinçlidir — bir varsayım kurulsaydı, kipi hiç
    düşünmeyen bir çağrı (route dâhil) sessizce ``check`` üretirdi.

    Returns:
        Raw token'ı **bir kez** taşıyan :class:`PreparedExecutionPlan`.

    Raises:
        NotFoundError: Project veya inventory kaydı yoksa.
        ProjectInactiveError: Project pasife alınmışsa.
        InventoryNotLinkedToProjectError: Inventory bu project'e bağlı değilse.
        PlaybookNotDiscoveredError: Playbook keşifte yoksa (özgün veya
            dondurulmuş ağaçta).
        InventoryUnsafeError: Inventory desteklenmeyen bir bağlantı tanımı
            içeriyorsa.
        WorkspaceUnsafeError: Project ağacı güvenle dondurulamıyorsa.
        WorkspaceUnavailableError: Kopya yazılamazsa.
    """
    moment = now or datetime.now(UTC)
    _sweep_quietly(session, workspace_root=workspace_root, now=moment)

    project = resolve_active_project(session, project_id)
    inventory = resolve_linked_inventory(session, project, inventory_id=inventory_id)
    # Ön kontrol: dondurmadan önce ucuz bir ret. Bağlayıcı kontrol aşağıda,
    # dondurulmuş ağaç üzerinde tekrarlanır.
    resolve_discovered_playbook(
        session,
        project,
        playbook_path=playbook_path,
        project_roots=project_roots,
        scan_limits=scan_limits,
    )
    snapshot = resolve_snapshot_plan(
        session,
        inventory,
        project_roots=project_roots,
        inventory_roots=inventory_roots,
        key_roots=key_roots,
        command=command,
        parser_limits=parser_limits,
    )

    frozen = freeze_workspace(
        workspace_root,
        project_root=resolve_project_root(project, allowed_roots=project_roots),
        # Ham inventory dosyası **kopyalanmaz**: dondurulan şey, allowlist'ten
        # geçmiş ve uygulamanın kendi ürettiği normalize snapshot'tır.
        inventory_snapshot=render_full_snapshot(snapshot),
        now=moment,
    )

    try:
        plan = _plan_from_frozen(
            workspace_root,
            frozen.workspace_id,
            mode=mode,
            project=project,
            inventory=inventory,
            playbook_path=playbook_path,
            scan_limits=scan_limits,
            host_key_policy=host_key_policy,
            now=moment,
        )
        prepared = store_prepared_plan(
            session,
            project_id=project.id,
            inventory_id=inventory.id,
            playbook_path=playbook_path,
            fingerprint=input_fingerprint(
                project_id=project.id,
                inventory_id=inventory.id,
                playbook_path=playbook_path,
                mode=mode,
                connection=PLAN_CONNECTION,
                become=PLAN_BECOME,
                limit=PLAN_LIMIT,
                tags=PLAN_TAGS,
                skip_tags=PLAN_SKIP_TAGS,
                host_key_policy=host_key_policy,
            ),
            mode=mode,
            requested_by=requested_by,
            workspace_id=frozen.workspace_id,
            manifest_digest=frozen.manifest_digest,
            ttl_seconds=ttl_seconds,
            now=moment,
        )
    except BaseException:
        # Kaydı olmayan bir workspace bırakılmaz. Temizlik hatası asıl hatayı
        # gölgelemez; kalıntı reconciliation tarafından da toplanır.
        with contextlib.suppress(OSError, AppError):
            remove_workspace(workspace_root, frozen.workspace_id)
        raise

    return PreparedExecutionPlan(
        plan_id=prepared.plan_id,
        token=prepared.token,
        expires_at=prepared.expires_at,
        manifest_digest=frozen.manifest_digest,
        plan=plan,
    )


def _plan_from_frozen(
    workspace_root: Path,
    workspace_id: str,
    *,
    mode: ExecutionMode,
    project: Project,
    inventory: Inventory,
    playbook_path: str,
    scan_limits: ScanLimits,
    host_key_policy: str,
    now: datetime,
) -> ExecutionPlan:
    """Planı **yalnız dondurulmuş içerikten** hesaplar.

    Playbook metadata'sı dondurulmuş kopyanın kendisinden okunur; host listesi
    dondurulmuş snapshot'tan çözülür. Özgün ağaç bu noktada hiç açılmaz, bu
    yüzden hazırlama sonrasında özgün dosyaların değişmesi veya silinmesi planı
    etkilemez.
    """
    frozen_project = workspace_project_root(workspace_root, workspace_id)
    try:
        scan = discover_playbooks(frozen_project, project_id=project.id, limits=scan_limits)
    except OSError as exc:  # pragma: no cover - savunma amaçlı
        raise ProjectPathUnavailableError(
            "Dondurulmuş project kopyası okunamadı.",
            details={"project_id": project.id, "reason": "missing"},
        ) from exc

    playbook = next(
        (candidate for candidate in scan.playbooks if candidate.path == playbook_path), None
    )
    if playbook is None:
        # Özgün ağaçta bulunan playbook dondurulmuş kopyada yoksa (kopyalama
        # sırasında silinmiş olabilir) plan fail-closed reddedilir.
        raise PlaybookNotDiscoveredError(
            "Bu playbook dondurulmuş kopyada bulunamadı. Planı yeniden hazırlayın.",
            details={"project_id": project.id},
        )

    # Snapshot yapısı ve host adları burada **yeniden** doğrulanır: dondurma
    # anındaki doğrulama, okuma anının kalıcı garantisi sayılmaz.
    host_names = snapshot_host_names(read_frozen_inventory(workspace_root, workspace_id))
    listed = list(host_names[:MAX_PREVIEW_HOSTS])

    return ExecutionPlan(
        project=ExecutionPlanProject(id=project.id, name=project.name),
        inventory=ExecutionPlanInventory(
            id=inventory.id,
            name=inventory.name,
            binding=PLAN_BINDING,
        ),
        playbook=ExecutionPlanPlaybook(
            path=playbook.path,
            name=playbook.name,
            size_bytes=playbook.size_bytes,
            modified_at=playbook.modified_at,
        ),
        mode=mode,
        limit=PLAN_LIMIT,
        tags=PLAN_TAGS,
        skip_tags=PLAN_SKIP_TAGS,
        host_count=len(host_names),
        hosts=listed,
        hosts_truncated=len(host_names) > len(listed),
        connection=PLAN_CONNECTION,
        host_key_policy=host_key_policy,
        become=PLAN_BECOME,
        executable=False,
        not_executable_reason=NOT_EXECUTABLE_REASON,
        generated_at=now,
    )


def _sweep_quietly(session: Session, *, workspace_root: Path, now: datetime) -> None:
    """İstek başında bounded temizlik; hatası isteği düşürmez.

    Temizlik bir bakım işidir: diske erişilemediği için hazırlama isteğini
    reddetmek, kullanıcı açısından güvenliği artırmaz. Güvenlik kararları
    temizliğe bağlı değildir.
    """
    try:
        sweep_expired_plans(
            session, workspace_root=workspace_root, now=now, limit=MAX_SWEEP_RECORDS
        )
    except (SQLAlchemyError, OSError, AppError):
        session.rollback()
