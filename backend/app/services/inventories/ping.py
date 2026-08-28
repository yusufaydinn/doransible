"""Ping onay planı (preview) üretimi — T-204A.

Bu modül **hiçbir SSH bağlantısı kurmaz ve hiçbir ansible ad-hoc ping
çalıştırmaz**. Görevi, GUVENLIK.md bölüm 2 ve 7'nin istediği onay planını
üretmek ve gerçek çalıştırmanın (T-204B) üzerinde koşacağı **dondurulmuş**
snapshot'ı hazırlamaktır.

Özgün inventory yalnızca **bir kez** okunur. Sonraki bütün adımlar uygulamanın
kendi ürettiği snapshot üzerinde ilerler; böylece plan ile çalıştırma arasında
inventory veya ``group_vars`` değişse bile hedef kümesi ve güvenlik incelemesi
geçersizleşmez (TOCTOU).

Akış::

    limit doğrulama
    → inventory path'i kullanım anında yeniden doğrulama
    → Phase 1: ansible-inventory --list -i <ÖZGÜN>      (ini,yaml)
    → ham JSON üzerinde hostvar allowlist + SSH hedef doğrulaması
    → Snapshot A (grup topolojisi)                       [geçici workdir]
    → limit varsa Phase 1b: --limit, Snapshot A üzerinde (yalnızca yaml)
    → kesin hedef kümesi
    → Snapshot B (yalnızca hedefler)
    → meta + digest
    → atomik preview publish
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.errors import AppError, ValidationFailedError
from app.models import Inventory, Project
from app.services.ansible.host_patterns import (
    InvalidLimitPatternError,
    validate_limit_pattern,
)
from app.services.ansible.inventory_snapshot import (
    SnapshotPlan,
    build_snapshot_plan,
    render_full_snapshot,
    render_target_snapshot,
)
from app.services.inventories.parser import (
    YAML_ONLY_INVENTORY_PLUGINS,
    ParserLimits,
    load_parser_output,
    run_inventory_parser,
)
from app.services.inventories.service import get_inventory, resolve_inventory_path
from app.services.jobs.preview import (
    PreviewNotFoundError,
    PreviewStore,
    PreviewStoreUnavailableError,
)

# Onay planında gösterilen işlem. Kullanıcıdan alınmaz, kodda sabittir.
PING_OPERATION = "ansible.builtin.ping"

# Plan metni bilinçli olarak mutlak bir güvence vermez: ping uzak hostta geçici
# modül dosyası ve süreç oluşturur. "Hiçbir değişiklik yapılmaz" demek yanlış
# olurdu.
PING_OPERATION_EFFECT = (
    "Hedef host'lara SSH bağlantısı kurulur; uzak hostta geçici modül dosyaları "
    "ve süreç oluşabilir. Kalıcı yapılandırma veya sistem durumu değişikliği "
    "amaçlanmaz."
)

SNAPSHOT_FULL_FILENAME = "inventory-all.yml"


class PingNoHostsMatchedError(ValidationFailedError):
    """Limit hiçbir host ile eşleşmedi."""

    code = "ping_no_hosts_matched"


@dataclass(frozen=True)
class PingPlanInventory:
    """Plandaki inventory tanıtımı."""

    id: int
    name: str
    binding: str
    project_id: int | None
    project_name: str | None


@dataclass(frozen=True)
class PingPlan:
    """Kullanıcıya gösterilen onay planı.

    Yalnızca güvenli alanlar taşınır: host **adları** vardır; adres, kullanıcı,
    private key yolu ve diğer hostvar'lar **yoktur**.
    """

    inventory: PingPlanInventory
    operation: str
    operation_effect: str
    limit: str | None
    host_count: int
    hosts: tuple[str, ...]
    hosts_truncated: bool
    connection: str
    host_key_policy: str
    become: bool


@dataclass(frozen=True)
class PingPreview:
    """Preview cevabı: token, son kullanma zamanı ve plan."""

    preview_token: str
    expires_at: datetime
    plan: PingPlan


def create_ping_preview(
    session: Session,
    inventory_id: int,
    *,
    limit: str | None,
    inventory_roots: Sequence[Path],
    project_roots: Sequence[Path],
    key_roots: Sequence[Path],
    command: Sequence[str],
    limits: ParserLimits,
    store: PreviewStore,
    host_key_policy: str,
    max_listed_hosts: int,
    requested_by: str,
) -> PingPreview:
    """Ping onay planı üretir ve dondurulmuş snapshot'ı yayımlar.

    **SSH bağlantısı kurulmaz, ping çalıştırılmaz, Job kaydı veya artifact
    dizini oluşturulmaz.** Bu adım yalnızca planı ve onay token'ını üretir.

    Args:
        session: Aktif veritabanı session'ı.
        inventory_id: Kayıtlı inventory'nin kimliği.
        limit: Kullanıcının host pattern'i; ``None`` ise tüm inventory.
        inventory_roots: Standalone inventory için izin verilen root'lar.
        project_roots: Project kayıtları için izin verilen root'lar.
        key_roots: Private key dosyaları için izin verilen root'lar.
        command: `ansible-inventory` komutu (argüman listesi).
        limits: Parser timeout ve çıktı boyutu sınırları.
        store: Preview state deposu.
        host_key_policy: Planda gösterilecek host key politikası.
        max_listed_hosts: Planda listelenecek azami host adı sayısı.
        requested_by: Planı isteyen aktör. Meta'ya yazılır ve onay anında
            yeniden karşılaştırılır; böylece token yalnızca üretildiği bağlamda
            kullanılabilir.

    Returns:
        Token, son kullanma zamanı ve plan.

    Raises:
        InvalidLimitPatternError: Limit boş, bozuk veya yasaklı bir desen ise.
        NotFoundError: Inventory kaydı yoksa.
        PathNotAllowedError: Kayıtlı path artık izinli alanın dışındaysa.
        ProjectInactiveError: Bağlı project pasife alınmışsa.
        InventoryPathUnavailableError: Dosya silinmiş veya dosya değilse.
        InventoryUnsafeError: Inventory desteklenmeyen bir bağlantı tanımı
            içeriyorsa.
        PingNoHostsMatchedError: Limit hiçbir host ile eşleşmezse.
        PreviewStoreUnavailableError: Preview state yazılamazsa.
    """
    validated_limit = validate_limit_pattern(limit)
    inventory = get_inventory(session, inventory_id)
    inventory_path = resolve_inventory_path(
        session,
        inventory,
        inventory_roots=inventory_roots,
        project_roots=project_roots,
    )

    # Terk edilmiş state'ler yalnızca burada, tembel biçimde toplanır.
    store.sweep()

    # Phase 1 — özgün inventory'ye tek ve son erişim.
    raw_output = run_inventory_parser(inventory_path, command=command, limits=limits)
    parsed = load_parser_output(raw_output)
    plan_source = build_snapshot_plan(
        parsed.host_variables,
        parsed.direct_hosts,
        parsed.children,
        key_roots=key_roots,
    )

    targets = _resolve_targets(
        plan_source,
        limit=validated_limit,
        command=command,
        limits=limits,
    )
    if not targets:
        raise PingNoHostsMatchedError("Verilen limit inventory'deki hiçbir host ile eşleşmedi.")

    snapshot_text = render_target_snapshot(plan_source, targets)
    project = _linked_project(session, inventory)
    # Meta yalnızca onay için gereken bağlamı taşır. Secret, private key yolu ve
    # hostvar değerleri bilinçli olarak **yoktur**.
    meta = {
        "schema_version": 1,
        "inventory_id": inventory.id,
        "requested_by": requested_by,
        "limit": validated_limit,
        "host_count": len(targets),
        "host_key_policy": host_key_policy,
        "operation": PING_OPERATION,
    }

    token, expires_at = store.publish(meta=meta, snapshot_text=snapshot_text)
    listed = targets[:max_listed_hosts]
    return PingPreview(
        preview_token=token,
        expires_at=expires_at,
        plan=PingPlan(
            inventory=_describe_inventory(inventory, project),
            operation=PING_OPERATION,
            operation_effect=PING_OPERATION_EFFECT,
            limit=validated_limit,
            host_count=len(targets),
            hosts=listed,
            hosts_truncated=len(listed) < len(targets),
            connection="ssh",
            host_key_policy=host_key_policy,
            become=False,
        ),
    )


def cancel_ping_preview(
    token: str,
    *,
    store: PreviewStore,
    inventory_id: int,
    requested_by: str,
) -> None:
    """Bir onay planını iptal eder ve state'ini temizler.

    İptal de **tek kullanımlıktır**: token önce atomik olarak claim edilir,
    sonra state silinir.

    Yalnızca :class:`PreviewNotFoundError` yutulur — bilinmeyen, biçimsiz,
    süresi geçmiş, eşleşmeyen veya daha önce kullanılmış bir token için
    yapılacak bir şey kalmamıştır ve iptal idempotenttir. Böyle bir token'ın
    "vardı" mı "yoktu" mu olduğu cevaptan anlaşılmaz.

    Altyapı arızaları (izin, I/O, kök güvenliği, meta okuma, temizlik)
    **yutulmaz**: :class:`PreviewStoreUnavailableError` yukarı geçer ve endpoint
    ``500`` döner. Temizlenemeyen bir state'i ``204`` ile örtmek, diskte kalan
    claim edilmiş state'i fark edilemez hâle getirirdi.
    """
    try:
        record = store.claim(token, inventory_id=inventory_id, requested_by=requested_by)
    except PreviewNotFoundError:
        return
    store.discard(record)


def _resolve_targets(
    plan_source: SnapshotPlan,
    *,
    limit: str | None,
    command: Sequence[str],
    limits: ParserLimits,
) -> tuple[str, ...]:
    """Kesin hedef kümesini çözer.

    Limit verilmemişse snapshot'taki bütün host'lar hedeftir ve ek bir süreç
    başlatılmaz. Limit verilmişse çözümleme **Snapshot A üzerinde** yapılır:
    özgün inventory ikinci kez okunmaz.

    Snapshot A'nın ayrıştırılabilir olduğu Phase 1'de zaten kanıtlanmıştır; bu
    yüzden bu adımdaki **her** başarısızlık limitin kendisine atfedilir ve
    ``ping_invalid_limit`` olarak sınıflandırılır. Ansible'ın hata metni,
    çıkış kodu veya traceback'i kullanıcıya hiç gösterilmez — ölçüldüğü üzere
    ``--limit '!'`` girdisi Ansible'ı traceback ile çökertir ve bu, bozuk
    kurulum gibi raporlanmamalıdır.
    """
    if limit is None:
        return plan_source.host_names()

    with tempfile.TemporaryDirectory(prefix="ansibleops-ping-preview-") as raw_dir:
        work_dir = Path(raw_dir)
        _restrict_directory(work_dir)
        snapshot_a = work_dir / SNAPSHOT_FULL_FILENAME
        _write_private_text(snapshot_a, render_full_snapshot(plan_source))

        try:
            raw_output = run_inventory_parser(
                snapshot_a,
                command=command,
                limits=limits,
                limit=limit,
                inventory_plugins=YAML_ONLY_INVENTORY_PLUGINS,
            )
            resolved = load_parser_output(raw_output)
        except AppError as exc:
            raise _invalid_limit() from exc

    return tuple(sorted(resolved.host_variables))


def _invalid_limit() -> AppError:
    """Phase 1b arızalarını tek bir limit hatasına indirger."""
    return InvalidLimitPatternError(
        "Limit deseni bu inventory üzerinde çözümlenemedi. Host adı, grup adı "
        "veya bunların `,` `:` `:&` `:!` ile birleştirilmiş hâlini kullanın."
    )


def _linked_project(session: Session, inventory: Inventory) -> Project | None:
    """Inventory'ye bağlı project kaydını döndürür (varsa)."""
    if inventory.project_id is None:
        return None
    return session.get(Project, inventory.project_id)


def _describe_inventory(inventory: Inventory, project: Project | None) -> PingPlanInventory:
    """Plandaki inventory tanıtımını kurar.

    Sunucudaki dosya yolu bilinçli olarak **taşınmaz**: onay için gereken bilgi
    hangi kaydın hedeflendiğidir, dosyanın diskteki yeri değildir.
    """
    return PingPlanInventory(
        id=inventory.id,
        name=inventory.name,
        binding="project" if inventory.project_id is not None else "standalone",
        project_id=inventory.project_id,
        project_name=project.name if project is not None else None,
    )


def _write_private_text(path: Path, content: str) -> None:
    """Geçici snapshot'ı 0600 izniyle yazar.

    ``O_EXCL``: var olan bir dosyanın üzerine yazılmaz. ``O_NOFOLLOW`` (varsa):
    aynı ada konmuş bir symlink izlenmez. Dizin ``mkdtemp`` ile 0700 ve
    tahmin edilemez bir adla açılır; bu bayraklar yine de savunma katmanıdır.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        raise PreviewStoreUnavailableError("Ping önizleme çalışma dosyası yazılamadı.") from exc


def _restrict_directory(path: Path) -> None:
    """Geçici çalışma dizinini POSIX üzerinde 0700 yapar."""
    if os.name != "posix":
        return
    try:
        os.chmod(path, 0o700)
    except OSError as exc:  # pragma: no cover - platform davranışı
        raise PreviewStoreUnavailableError("Ping önizleme çalışma dizini hazırlanamadı.") from exc
