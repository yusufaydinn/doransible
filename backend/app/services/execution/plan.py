"""Check-mode execution plan preview servisi (R1-V1).

Bu servis **hiçbir şey çalıştırmaz**: `ansible-runner` çağrılmaz,
`ansible-playbook` başlatılmaz, Job kaydı ve artifact üretilmez, plan state'i
diske yazılmaz ve onay token'ı dağıtılmaz. Üretilen belge yalnızca kullanıcının
okuyacağı bir özettir.

**`executable=False` bağlayıcı bir sözleşmedir.** Plan cevabı ileride
çalıştırılabilir bir onay olarak yeniden yorumlanamaz: ADR-021 Kapı C hâlâ
OPEN'dır ve gerçek çalıştırma, frozen execution workspace ile TTL'li,
tek kullanımlık generic plan token'ının (R1-V2) uygulanmasını bekler. Ping
akışının `PreviewStore`'una bu dilimde dokunulmaz.

Girdi güvenliği iki karara dayanır:

1. **Playbook path'i dosya sisteminde açılmaz.** Kullanıcının gönderdiği göreli
   yol, mevcut keşif (:mod:`app.services.projects.discovery`) sonucundaki
   girdilerle **birebir** karşılaştırılır. Traversal (``../../etc/hosts``),
   project dışına çıkan symlink ve keşfedilmemiş serbest path aynı sebeple
   reddedilir: listede yoklar. Metadata da kullanıcı girdisinden değil keşif
   descriptor'ından üretilir; girdiyi yeniden ``stat`` etmek, keşfin uyguladığı
   sınırları ikinci ve zayıf bir yoldan atlatmaya açık olurdu.
2. **Inventory yalnızca kayıt üzerinden çözülür.** Path kullanım anında
   yeniden doğrulanır ve host kümesi, ping akışının kullandığı aynı allowlist
   doğrulamasından (:func:`build_snapshot_plan`) geçirilir; hostvar, private key
   yolu ve diğer bağlantı ayrıntıları plana **girmez**.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from app.core.errors import AppError, ValidationFailedError
from app.models import ExecutionMode, Inventory, Project
from app.services.ansible.inventory_snapshot import SnapshotPlan, build_snapshot_plan
from app.services.inventories.parser import (
    ParserLimits,
    load_parser_output,
    run_inventory_parser,
)
from app.services.inventories.service import get_inventory, resolve_inventory_path
from app.services.projects.discovery import DiscoveredPlaybook, ScanLimits
from app.services.projects.service import (
    ProjectInactiveError,
    get_project,
    list_project_playbooks,
)

# Planda gösterilecek azami host adı. `host_count` bu sınırdan bağımsızdır ve
# daima kesin toplamı taşır; kırpma `hosts_truncated` ile görünür kılınır.
MAX_PREVIEW_HOSTS = 100

# Bu dilimde plan alanlarının çoğu hâlâ sabittir: kullanıcı limit, tags,
# skip_tags, forks veya timeout **gönderemez**. `mode` bu kümenin dışına
# R1-V3H2A ile çıkmıştır — çağıran artık `ExecutionMode.CHECK` veya
# `ExecutionMode.NORMAL` **seçer**; bu modülün ürettiği plan yalnız o seçimi
# taşır, kendi bir varsayım eklemez.
#
# Kalan sabitler ve aşağıdaki dataclass alanları `Literal` ile yazılır: değeri
# gevşetmek artık type check aşamasında görünür bir sözleşme ihlali olur,
# response modelindeki aynı `Literal` de ikinci savunma hattını oluşturur.
PLAN_CONNECTION: Literal["ssh"] = "ssh"
PLAN_BECOME: Literal[False] = False

# `limit` bilinçli olarak plana **açıkça `null`** yazılır (Kapı C notu): kapsam
# dışı olduğu için "belirtilmemiş" ile "yok" arasındaki fark okuyucuya bırakılmaz.
PLAN_LIMIT: Literal[None] = None
PLAN_TAGS: Literal[None] = None
PLAN_SKIP_TAGS: Literal[None] = None

# Plan neden çalıştırılamaz. Makine tarafından okunabilir tek sabit; arayüz
# gerekçeyi bu koda göre gösterir.
NOT_EXECUTABLE_REASON: Literal["execution_not_enabled"] = "execution_not_enabled"

# Yalnız project'e bağlı inventory kabul edilir (ADR-021 Karar 11).
PLAN_BINDING: Literal["project"] = "project"


class InventoryNotLinkedToProjectError(AppError):
    """Inventory bu project'e bağlı değil.

    Hem standalone (project'siz) hem de **başka** bir project'e bağlı inventory
    aynı kodu ve aynı mesajı alır. Ayrım yapmak, isteyen bir istemciye bir
    inventory'nin hangi project'e ait olduğunu tarayarak öğrenme imkânı verirdi;
    ``details`` bu yüzden yalnızca istekte zaten bulunan kimlikleri taşır.

    Girdi biçimsel olarak geçerlidir, reddedilme sebebi kayıtların durumudur;
    bu yüzden 422 değil 409 döner (``inventory_path_unavailable`` ile aynı
    yaklaşım).
    """

    status_code = 409
    code = "inventory_not_linked_to_project"


class PlaybookNotDiscoveredError(ValidationFailedError):
    """İstenen playbook, project'in keşif sonucunda yok.

    Traversal, project dışına çıkan symlink, silinmiş dosya ve hiç var olmayan
    yol **aynı** cevabı üretir: aksi hâlde endpoint, izin verilmeyen yollar için
    "var/yok" bilgisi sızdıran bir dosya sistemi sondası olurdu. Keşif sonucu
    ``truncated`` ise de sonuç aynıdır (fail-closed): listede olmayan bir
    playbook plana giremez.
    """

    code = "playbook_not_discovered"


@dataclass(frozen=True)
class ExecutionPlanProject:
    """Plandaki project tanıtımı. Sunucudaki dizin yolu **yer almaz**."""

    id: int
    name: str


@dataclass(frozen=True)
class ExecutionPlanInventory:
    """Plandaki inventory tanıtımı. Dosya yolu **yer almaz**."""

    id: int
    name: str
    binding: Literal["project"]


@dataclass(frozen=True)
class ExecutionPlanPlaybook:
    """Plandaki playbook tanıtımı.

    ``path`` project köküne **göreli** ve POSIX ayraçlıdır; keşif
    descriptor'ından olduğu gibi taşınır (GUVENLIK.md bölüm 3).
    """

    path: str
    name: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True)
class ExecutionPlan:
    """Kullanıcıya gösterilen, çalıştırılamaz plan özeti (seçilen kip: check/normal)."""

    project: ExecutionPlanProject
    inventory: ExecutionPlanInventory
    playbook: ExecutionPlanPlaybook
    mode: ExecutionMode
    limit: Literal[None]
    tags: Literal[None]
    skip_tags: Literal[None]
    host_count: int
    hosts: list[str]
    hosts_truncated: bool
    connection: Literal["ssh"]
    host_key_policy: str
    become: Literal[False]
    executable: Literal[False]
    not_executable_reason: Literal["execution_not_enabled"]
    generated_at: datetime


def build_execution_plan(
    session: Session,
    project_id: int,
    *,
    mode: ExecutionMode,
    inventory_id: int,
    playbook_path: str,
    project_roots: Sequence[Path],
    inventory_roots: Sequence[Path],
    key_roots: Sequence[Path],
    command: Sequence[str],
    parser_limits: ParserLimits,
    scan_limits: ScanLimits,
    host_key_policy: str,
) -> ExecutionPlan:
    """Seçilen kip için execution planı üretir; **hiçbir şey çalıştırmaz**.

    Doğrulama sırası bilinçlidir ve ucuzdan pahalıya değil, **dardan genişe**
    ilerler:

    ```text
    project kaydı → aktiflik
    → inventory kaydı → project bağı
    → playbook keşfi (project kökü ve allowlist yeniden doğrulanır)
    → inventory path'i yeniden doğrulanır
    → parser (tek alt süreç)
    → hostvar allowlist doğrulaması
    ```

    Alt süreç en sonda başlatılır: bağ veya playbook girdisi geçersizken
    `ansible-inventory` çalıştırmak, reddedilecek bir istek için iş yapmak
    olurdu. Parser'ın geçici çalışma dizini başarı, hata ve timeout yollarının
    **hepsinde** :func:`run_inventory_parser` içindeki context manager ile
    silinir; bu servis ayrı bir geçici alan açmaz.

    Servis veritabanına **yazmaz**: insert, update, delete veya commit yoktur.

    Args:
        session: Aktif veritabanı session'ı.
        project_id: Planın hedeflediği project kaydı.
        mode: Planın taşıyacağı çalıştırma kipi. Çağıran **açıkça** verir;
            burada bir varsayım kurulmaz.
        inventory_id: Kullanılacak, aynı project'e bağlı inventory kaydı.
        playbook_path: Project köküne göreli, keşifte görünen playbook yolu.
        project_roots: Project kayıtları için izin verilen root'lar.
        inventory_roots: Standalone inventory için izin verilen root'lar
            (bağ doğrulaması için aynı kodun çağrılabilmesi gerekir).
        key_roots: Private key dosyaları için izin verilen root'lar.
        command: `ansible-inventory` komutu (argüman listesi).
        parser_limits: Parser timeout ve çıktı boyutu sınırları.
        scan_limits: Playbook keşif sınırları.
        host_key_policy: Planda gösterilecek host key politikası.

    Returns:
        ``executable=False`` taşıyan :class:`ExecutionPlan`.

    Raises:
        NotFoundError: Project veya inventory kaydı yoksa.
        ProjectInactiveError: Project pasife alınmışsa.
        InventoryNotLinkedToProjectError: Inventory standalone'sa veya başka bir
            project'e bağlıysa.
        PathNotAllowedError: Kayıtlı project/inventory path'i artık izinli
            alanın dışındaysa.
        ProjectPathUnavailableError: Project dizini yoksa, dosyaya dönüştüyse
            veya tarama sırasında değiştiyse.
        PlaybookNotDiscoveredError: Playbook keşif sonucunda yoksa.
        InventoryPathUnavailableError: Inventory dosyası silinmişse veya dosya
            olmaktan çıkmışsa.
        InventoryOutsideProjectError: Inventory dosyası project kökünün
            dışındaysa.
        InventoryParseFailedError: Inventory ayrıştırılamazsa.
        InventoryUnsafeError: Inventory desteklenmeyen bir bağlantı tanımı
            içeriyorsa.
    """
    project = resolve_active_project(session, project_id)
    inventory = resolve_linked_inventory(session, project, inventory_id=inventory_id)
    playbook = resolve_discovered_playbook(
        session,
        project,
        playbook_path=playbook_path,
        project_roots=project_roots,
        scan_limits=scan_limits,
    )
    host_names = _resolve_host_names(
        session,
        inventory,
        project_roots=project_roots,
        inventory_roots=inventory_roots,
        key_roots=key_roots,
        command=command,
        parser_limits=parser_limits,
    )

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
        generated_at=datetime.now(UTC),
    )


def resolve_active_project(session: Session, project_id: int) -> Project:
    """Project kaydını getirir ve pasif kaydı reddeder."""
    project = get_project(session, project_id)
    if not project.is_active:
        raise ProjectInactiveError(
            "Pasif project için execution planı üretilemez.",
            details={"project_id": project.id},
        )
    return project


def resolve_linked_inventory(
    session: Session,
    project: Project,
    *,
    inventory_id: int,
) -> Inventory:
    """Inventory'nin **bu** project'e bağlı olduğunu doğrular (fail-closed).

    Standalone inventory ile çalıştırma bu dilimde bilinçli olarak kapsam
    dışıdır (ADR-021 Karar 11) ve sessizce project'siz bir plana düşülmez.
    """
    inventory = get_inventory(session, inventory_id)
    if inventory.project_id != project.id:
        raise InventoryNotLinkedToProjectError(
            "Bu inventory seçilen project'e bağlı değil. Execution planı yalnızca "
            "project'e bağlı bir inventory ile üretilir.",
            details={"project_id": project.id, "inventory_id": inventory_id},
        )
    return inventory


def resolve_discovered_playbook(
    session: Session,
    project: Project,
    *,
    playbook_path: str,
    project_roots: Sequence[Path],
    scan_limits: ScanLimits,
) -> DiscoveredPlaybook:
    """Keşif sonucunda birebir eşleşen playbook descriptor'ını döndürür.

    Kullanıcı girdisi normalize edilmez, birleştirilmez ve dosya sisteminde
    çözülmez; yalnızca eşitlik karşılaştırmasında kullanılır. Bu yüzden girdi
    ne kadar bozuk olursa olsun bir path işlemine dönüşmez.
    """
    scan = list_project_playbooks(
        session,
        project.id,
        allowed_roots=project_roots,
        limits=scan_limits,
    )
    for candidate in scan.playbooks:
        if candidate.path == playbook_path:
            return candidate

    raise PlaybookNotDiscoveredError(
        "Bu playbook project'in keşif sonucunda yok. Yalnızca listelenen "
        "playbook'lar için plan üretilebilir.",
        details={"project_id": project.id},
    )


def resolve_snapshot_plan(
    session: Session,
    inventory: Inventory,
    *,
    project_roots: Sequence[Path],
    inventory_roots: Sequence[Path],
    key_roots: Sequence[Path],
    command: Sequence[str],
    parser_limits: ParserLimits,
) -> SnapshotPlan:
    """Inventory'yi ayrıştırır ve doğrulanmış snapshot planını döndürür.

    Host kümesi ping akışının kullandığı **aynı** allowlist doğrulamasından
    geçer: desteklenmeyen bir bağlantı değişkeni veya izinsiz bir private key
    yolu, plan üretimini de durdurur. Doğrulanan hostvar değerlerinin kendisi
    API cevabına taşınmaz.

    Parser'ın geçici çalışma dizini başarı, hata ve timeout yollarının
    hepsinde :func:`run_inventory_parser` içindeki context manager ile silinir.
    """
    inventory_path = resolve_inventory_path(
        session,
        inventory,
        inventory_roots=inventory_roots,
        project_roots=project_roots,
    )
    raw_output = run_inventory_parser(inventory_path, command=command, limits=parser_limits)
    parsed = load_parser_output(raw_output)
    return build_snapshot_plan(
        parsed.host_variables,
        parsed.direct_hosts,
        parsed.children,
        key_roots=key_roots,
    )


def _resolve_host_names(
    session: Session,
    inventory: Inventory,
    *,
    project_roots: Sequence[Path],
    inventory_roots: Sequence[Path],
    key_roots: Sequence[Path],
    command: Sequence[str],
    parser_limits: ParserLimits,
) -> tuple[str, ...]:
    """Inventory'nin doğrulanmış host adlarını alfabetik sırayla döndürür."""
    return resolve_snapshot_plan(
        session,
        inventory,
        project_roots=project_roots,
        inventory_roots=inventory_roots,
        key_roots=key_roots,
        command=command,
        parser_limits=parser_limits,
    ).host_names()
