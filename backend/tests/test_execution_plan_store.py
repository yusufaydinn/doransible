"""Hazırlanmış plan deposu, atomik claim ve temizlik (R1-V2, R1-V3A).

Merkez iddialar:

- Raw token veritabanına **hiç** yazılmaz; yalnızca SHA-256 özeti saklanır.
- Bir token tam olarak **bir kez** claim edilebilir; yanlış girdiyle — yanlış
  **aktör** dâhil — yapılan deneme token'ı tüketmez.
- Kaydı olmayan workspace ve workspace'i olmayan kayıt fail-closed toplanır.

:func:`claim_plan_row` burada commit **etmez**; testler transaction'ı kendileri
kapatır. Claim ile Job rezervasyonunun tek transaction'da bağlanması
`test_execution_authorize.py` içinde ölçülür.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, Select, select, text, update
from sqlalchemy.orm import Session

from app.models import (
    ExecutionMode,
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    Inventory,
    InventorySourceType,
    Job,
    JobStatus,
    JobType,
    Project,
)
from app.services.execution import store
from app.services.execution import workspace as ws
from app.services.execution.store import (
    MAX_RECONCILE_RECORDS,
    MAX_RECONCILE_WORKSPACES,
    TOKEN_LENGTH,
    ExecutionPlanInvalidError,
    claim_plan_row,
    expire_plan_by_token,
    input_fingerprint,
    reconcile_execution_plans,
    store_prepared_plan,
    sweep_expired_plans,
    token_digest,
)
from app.services.execution.workspace import freeze_workspace, workspace_exists


def select_plan() -> Select[tuple[ExecutionPlanRecord]]:
    """Kayıtları okuyan ortak sorgu."""
    return select(ExecutionPlanRecord)


SNAPSHOT = '{\n  "all": {\n    "hosts": {\n      "web01": {}\n    }\n  }\n}\n'
PLAYBOOK_PATH = "site.yml"
TTL = 600.0
# Planı hazırlayan aktör; `Settings.local_actor` karşılığı.
ACTOR = "yerel-operator"

pytestmark = pytest.mark.skipif(
    not ws.secure_filesystem_available(),
    reason="Descriptor-relative dosya sistemi primitive'leri bu platformda yok (ADR-017).",
)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "execution-plans"
    root.mkdir(mode=0o700)
    return root


@pytest.fixture
def source_project(tmp_path: Path) -> Path:
    root = tmp_path / "proje"
    root.mkdir()
    (root / "site.yml").write_text("---\n- hosts: all\n", encoding="utf-8")
    return root


@pytest.fixture
def records(db_session: Session, tmp_path: Path) -> tuple[Project, Inventory]:
    """Kayıtlı project ve ona bağlı inventory."""
    project = Project(name="Web", path=str(tmp_path / "proje"))
    db_session.add(project)
    db_session.commit()
    inventory = Inventory(
        name="Prod",
        path=str(tmp_path / "proje" / "hosts.ini"),
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    db_session.add(inventory)
    db_session.commit()
    return project, inventory


def _fingerprint(project: Project, inventory: Inventory) -> str:
    return input_fingerprint(
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        mode=ExecutionMode.CHECK,
        connection="ssh",
        become=False,
        limit=None,
        tags=None,
        skip_tags=None,
        host_key_policy="strict",
    )


def _prepare(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    *,
    now: datetime | None = None,
    ttl_seconds: float = TTL,
) -> tuple[str, str]:
    """Dondurulmuş workspace ve kayıt üretir; ``(token, workspace_id)`` döner."""
    project, inventory = records
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    prepared = store_prepared_plan(
        db_session,
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        fingerprint=_fingerprint(project, inventory),
        mode=ExecutionMode.CHECK,
        requested_by=ACTOR,
        workspace_id=frozen.workspace_id,
        manifest_digest=frozen.manifest_digest,
        ttl_seconds=ttl_seconds,
        now=now,
    )
    return prepared.token, frozen.workspace_id


def _claim(
    session: Session,
    records: tuple[Project, Inventory],
    token: str,
    **overrides: object,
) -> ExecutionPlanRecord:
    """Claim UPDATE'ini çalıştırır ve transaction'ı kapatır.

    Production'da commit'i :func:`claim_and_reserve_playbook_job` yapar; burada
    ölçülen tek şey UPDATE'in kendisidir, bu yüzden commit teste bırakılır.
    """
    project, inventory = records
    arguments: dict[str, object] = {
        "token": token,
        "project_id": project.id,
        "inventory_id": inventory.id,
        "playbook_path": PLAYBOOK_PATH,
        "fingerprint": _fingerprint(project, inventory),
        "mode": ExecutionMode.CHECK,
        "requested_by": ACTOR,
        "now": datetime.now(UTC),
    }
    arguments.update(overrides)
    try:
        record = claim_plan_row(session, **arguments)  # type: ignore[arg-type]
    except ExecutionPlanInvalidError:
        session.rollback()
        raise
    session.commit()
    return record


def test_only_the_token_hash_reaches_the_database(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Raw token hiçbir sütunda bulunmaz; yalnızca özeti saklanır."""
    token, workspace_id = _prepare(db_session, workspace_root, source_project, records)

    assert len(token) == TOKEN_LENGTH
    record = db_session.execute(text("SELECT * FROM execution_plans")).mappings().one()
    assert record["token_hash"] == token_digest(token)
    assert token not in str(dict(record))
    assert record["workspace_id"] == workspace_id
    # Absolute path veritabanına yazılmaz.
    assert str(workspace_root) not in str(dict(record))
    assert record["status"] == ExecutionPlanStatus.PREPARED.value
    # Aktör bağı kayıtta durur (R1-V3A) ve claim koşulunun parçasıdır.
    assert record["requested_by"] == ACTOR


def test_claim_consumes_the_token_exactly_once(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """İlk claim başarılı, ikincisi reddedilir."""
    token, workspace_id = _prepare(db_session, workspace_root, source_project, records)

    claimed = _claim(db_session, records, token)
    assert claimed.workspace_id == workspace_id

    record = db_session.execute(select_plan()).scalar_one()
    assert record.status is ExecutionPlanStatus.CLAIMED
    assert record.claimed_at is not None

    with pytest.raises(ExecutionPlanInvalidError):
        _claim(db_session, records, token)


@pytest.mark.parametrize(
    "override",
    [
        {"project_id": 987},
        {"inventory_id": 987},
        {"playbook_path": "baska.yml"},
        {"fingerprint": "0" * 64},
        {"requested_by": "baska-aktor"},
    ],
)
def test_wrong_input_does_not_consume_the_token(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    override: dict[str, object],
) -> None:
    """Yanlış bağlamla — yanlış aktör dâhil — gelen deneme token'ı tüketmez."""
    token, _ = _prepare(db_session, workspace_root, source_project, records)

    with pytest.raises(ExecutionPlanInvalidError):
        _claim(db_session, records, token, **override)

    record = db_session.execute(select_plan()).scalar_one()
    assert record.status is ExecutionPlanStatus.PREPARED
    # Doğru bağlamla hâlâ kullanılabilir.
    assert _claim(db_session, records, token) is not None


def test_expired_token_cannot_be_claimed(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Süresi geçmiş token claim edilemez."""
    past = datetime.now(UTC) - timedelta(seconds=3600)
    token, _ = _prepare(db_session, workspace_root, source_project, records, now=past)

    with pytest.raises(ExecutionPlanInvalidError):
        _claim(db_session, records, token)


def test_malformed_token_is_rejected_without_a_query(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Biçimsiz token hiçbir kaydı etkilemez."""
    _prepare(db_session, workspace_root, source_project, records)

    for candidate in ["", "kısa", "../../etc/passwd", "x" * 200]:
        with pytest.raises(ExecutionPlanInvalidError):
            _claim(db_session, records, candidate)

    record = db_session.execute(select_plan()).scalar_one()
    assert record.status is ExecutionPlanStatus.PREPARED


def test_expiring_by_token_consumes_a_claimed_plan(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """``expire_plan_by_token`` durumdan bağımsız olarak bileti geri alınamaz kılar.

    Çağıran bunu claim UPDATE'ini rollback ettikten **sonra** kullanır: kayıt o
    anda yeniden ``prepared`` görünür, ama içeriği artık onaylanan içerik
    olmadığı için token yeniden claim edilebilir bırakılmaz.
    """
    token, _ = _prepare(db_session, workspace_root, source_project, records)

    expire_plan_by_token(db_session, token=token)

    record = db_session.execute(select_plan()).scalar_one()
    assert record.status is ExecutionPlanStatus.EXPIRED
    with pytest.raises(ExecutionPlanInvalidError):
        _claim(db_session, records, token)


def test_error_message_never_carries_the_token(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Hata mesajı ve ``details`` token'ın hiçbir parçasını taşımaz."""
    token, _ = _prepare(db_session, workspace_root, source_project, records)
    _claim(db_session, records, token)

    with pytest.raises(ExecutionPlanInvalidError) as exc_info:
        _claim(db_session, records, token)

    error = exc_info.value
    assert error.details == {"reason": "invalid"}
    assert token not in error.message
    assert token[:8] not in error.message
    assert str(workspace_root) not in error.message


def test_concurrent_claims_have_exactly_one_winner(
    db_session: Session,
    migrated_engine: Engine,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """İki eşzamanlı claim yarışında tam olarak biri kazanır."""
    token, _ = _prepare(db_session, workspace_root, source_project, records)
    project, inventory = records
    fingerprint = _fingerprint(project, inventory)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        with Session(migrated_engine) as session:
            barrier.wait(timeout=10)
            try:
                claim_plan_row(
                    session,
                    token=token,
                    project_id=project.id,
                    inventory_id=inventory.id,
                    playbook_path=PLAYBOOK_PATH,
                    fingerprint=fingerprint,
                    mode=ExecutionMode.CHECK,
                    requested_by=ACTOR,
                    now=datetime.now(UTC),
                )
                session.commit()
                result = "won"
            except Exception:  # noqa: BLE001 - kaybeden her sebeple kaybeder
                session.rollback()
                result = "lost"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert outcomes.count("won") == 1, outcomes
    db_session.expire_all()
    record = db_session.execute(select_plan()).scalar_one()
    assert record.status is ExecutionPlanStatus.CLAIMED


def test_sweep_collects_expired_plans_only(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Süresi geçen planın workspace'i silinir; geçerli plan korunur."""
    past = datetime.now(UTC) - timedelta(seconds=3600)
    _, stale_workspace = _prepare(db_session, workspace_root, source_project, records, now=past)
    _, live_workspace = _prepare(db_session, workspace_root, source_project, records)

    result = sweep_expired_plans(db_session, workspace_root=workspace_root)

    assert result.expired_records == 1
    assert result.removed_workspaces == 1
    assert workspace_exists(workspace_root, stale_workspace) is False
    assert workspace_exists(workspace_root, live_workspace) is True

    statuses = {
        record.workspace_id: record.status
        for record in db_session.execute(select_plan()).scalars().all()
    }
    assert statuses[stale_workspace] is ExecutionPlanStatus.EXPIRED
    assert statuses[live_workspace] is ExecutionPlanStatus.PREPARED


def test_reconciliation_handles_every_crash_window(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Crash pencerelerinin hepsi tek turda uzlaştırılır."""
    now = datetime.now(UTC)
    old = now - timedelta(seconds=7200)

    # 1) Workspace yayımlandı, DB kaydı yazılamadı (yaşlı orphan).
    orphan = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    _age(workspace_root / orphan.workspace_id, old)

    # 2) Az önce yayımlanmış orphan: kaydı yazılmak üzere olabilir, korunur.
    fresh = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )

    # 3) DB kaydı var, workspace yok.
    token_missing, missing_workspace = _prepare(db_session, workspace_root, source_project, records)
    assert ws.remove_workspace(workspace_root, missing_workspace) is True

    # 4) Yarım kalmış staging.
    staging = workspace_root / f"{ws.STAGING_PREFIX}{'b' * 32}"
    staging.mkdir(mode=0o700)
    _age(staging, old)

    # 5) Geçerli, süresi dolmamış plan: dokunulmaz.
    token_live, live_workspace = _prepare(db_session, workspace_root, source_project, records)

    # 6) Süresi geçmiş plan.
    _, expired_workspace = _prepare(
        db_session, workspace_root, source_project, records, now=now - timedelta(seconds=3600)
    )

    result = reconcile_execution_plans(
        db_session, workspace_root=workspace_root, staging_stale_seconds=900, now=now
    )

    assert result.orphan_workspaces == 1
    assert result.stale_staging == 1
    assert result.missing_workspaces == 1
    assert result.expired_records == 1

    assert workspace_exists(workspace_root, orphan.workspace_id) is False
    assert workspace_exists(workspace_root, fresh.workspace_id) is True
    assert workspace_exists(workspace_root, expired_workspace) is False
    assert not staging.exists()

    # Geçerli plan hâlâ claim edilebilir; kayıp workspace'in planı edilemez —
    # uzlaştırma o kaydı `expired` yaptığı için claim koşulu artık tutmaz.
    assert workspace_exists(workspace_root, live_workspace) is True
    assert _claim(db_session, records, token_live) is not None
    with pytest.raises(ExecutionPlanInvalidError):
        _claim(db_session, records, token_missing)


def test_reconciliation_never_infers_orphans_from_a_bounded_page(
    db_session: Session,
    workspace_root: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Sayfalama sınırının ötesindeki geçerli planlar orphan sayılmaz.

    Regresyon: reconciliation, non-expired kayıtların yalnız ilk sayfasını
    okuyup "gerisi orphan" varsayıyordu. 500'üncü satırdan sonraki prepared ve
    claimed planların dondurulmuş içeriği, yaş eşiğini geçtiği anda siliniyordu.
    """
    now = datetime.now(UTC)
    old = now - timedelta(seconds=7200)
    project, inventory = records

    # Sayfalama sınırının **ötesine** taşan, hepsi geçerli ve yaşlı planlar.
    workspace_ids: list[str] = []
    for _ in range(MAX_RECONCILE_RECORDS + 2):
        workspace_id = _fake_workspace(workspace_root, old)
        _store_record(db_session, records, workspace_id, now=now)
        workspace_ids.append(workspace_id)

    # Son iki plandan biri claim edilmiş olsun: claimed kayıtlar da korunmalı.
    claimed_workspace = workspace_ids[-1]
    claimed_token = _token_for(db_session, claimed_workspace)
    assert claimed_token is not None
    db_session.execute(
        update(ExecutionPlanRecord)
        .where(ExecutionPlanRecord.workspace_id == claimed_workspace)
        .values(status=ExecutionPlanStatus.CLAIMED, claimed_at=now)
    )
    db_session.commit()

    # Gerçek orphan: kaydı olmayan, yaşlı bir dizin.
    orphan_workspace = _fake_workspace(workspace_root, old)

    # Dizin sayısı tek turluk pencereyi aştığı için tur sayısı da sınırlıdır:
    # imleç her turda ilerlediğinden `ceil(N / pencere)` tur bütün listeyi kapar.
    orphans = _drain(db_session, workspace_root, now=now, entries=len(workspace_ids) + 1)

    assert orphans == 1
    assert workspace_exists(workspace_root, orphan_workspace) is False
    # Sayfanın dışında kalanlar dâhil bütün geçerli planlar korunur.
    for workspace_id in workspace_ids:
        assert workspace_exists(workspace_root, workspace_id) is True, workspace_id
    assert workspace_exists(workspace_root, claimed_workspace) is True

    statuses = {
        record.workspace_id: record.status
        for record in db_session.execute(select_plan()).scalars().all()
    }
    assert statuses[workspace_ids[-2]] is ExecutionPlanStatus.PREPARED
    assert statuses[claimed_workspace] is ExecutionPlanStatus.CLAIMED


def test_expired_workspace_removal_is_retried_next_round(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """İlk turda silinemeyen expired workspace bir sonraki turda yeniden denenir."""
    now = datetime.now(UTC)
    _, workspace_id = _prepare(
        db_session, workspace_root, source_project, records, now=now - timedelta(seconds=3600)
    )

    monkeypatch.setattr(store, "remove_workspace", lambda *_args, **_kwargs: False)
    first = reconcile_execution_plans(
        db_session, workspace_root=workspace_root, staging_stale_seconds=900, now=now
    )
    assert first.removed_workspaces == 0
    assert workspace_exists(workspace_root, workspace_id) is True
    record = db_session.execute(select_plan()).scalar_one()
    assert record.status is ExecutionPlanStatus.EXPIRED

    monkeypatch.undo()
    second = reconcile_execution_plans(
        db_session, workspace_root=workspace_root, staging_stale_seconds=900, now=now
    )

    # Kayıt zaten expired olduğu için ikinci turda yeni bir kayıt işaretlenmez;
    # silme yine de tekrarlanır.
    assert second.expired_records == 0
    assert second.removed_workspaces == 1
    assert second.orphan_workspaces == 0
    assert workspace_exists(workspace_root, workspace_id) is False


def test_reconciliation_advances_past_a_full_first_window(
    db_session: Session,
    workspace_root: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Dolu bir ilk pencere, arkasındaki dizinleri süresiz aç bırakamaz.

    Regresyon: pencere her turda listenin **aynı** ilk ``MAX_RECONCILE_WORKSPACES``
    dizininden seçiliyordu. İlk pencere baştan sona geçerli prepared/claimed
    planlardan oluştuğunda, hemen arkasındaki orphan ve silinememiş expired
    dizinler hiçbir turda incelenmiyordu.

    Düzen sıralamaya göre kurulur (workspace adları sıralanıp rollere göre
    dağıtılır), bu yüzden sonuç UUID rastlantısına bağlı değildir.
    """
    now = datetime.now(UTC)
    old = now - timedelta(seconds=7200)

    names = _sorted_workspace_names(MAX_RECONCILE_WORKSPACES + 2)
    plan_workspaces = names[:MAX_RECONCILE_WORKSPACES]
    orphan_workspace = names[MAX_RECONCILE_WORKSPACES]
    stuck_expired_workspace = names[MAX_RECONCILE_WORKSPACES + 1]

    # 1) İlk pencereyi baştan sona dolduran geçerli planlar.
    for workspace_id in plan_workspaces:
        _fake_workspace(workspace_root, old, workspace_id=workspace_id)
        _store_record(db_session, records, workspace_id, now=now)
    claimed_workspace = plan_workspaces[-1]
    db_session.execute(
        update(ExecutionPlanRecord)
        .where(ExecutionPlanRecord.workspace_id == claimed_workspace)
        .values(status=ExecutionPlanStatus.CLAIMED, claimed_at=now)
    )
    db_session.commit()

    # 2) Pencerenin arkasında: kaydı olmayan yaşlı orphan.
    _fake_workspace(workspace_root, old, workspace_id=orphan_workspace)

    # 3) Pencerenin arkasında: kaydı zaten `expired`, dizini silinememiş plan.
    _fake_workspace(workspace_root, old, workspace_id=stuck_expired_workspace)
    _store_record(db_session, records, stuck_expired_workspace, now=now)
    db_session.execute(
        update(ExecutionPlanRecord)
        .where(ExecutionPlanRecord.workspace_id == stuck_expired_workspace)
        .values(status=ExecutionPlanStatus.EXPIRED)
    )
    db_session.commit()

    first = reconcile_execution_plans(
        db_session, workspace_root=workspace_root, staging_stale_seconds=900, now=now
    )

    # İlk tur yalnız dolu pencereyi işler: hiçbir şey silinmez.
    assert first.orphan_workspaces == 0
    assert first.removed_workspaces == 0
    assert workspace_exists(workspace_root, orphan_workspace) is True
    assert workspace_exists(workspace_root, stuck_expired_workspace) is True

    second = reconcile_execution_plans(
        db_session, workspace_root=workspace_root, staging_stale_seconds=900, now=now
    )

    # İkinci tur bekleyen pencereye ilerler ve ikisini de toplar.
    assert second.orphan_workspaces == 1
    assert second.removed_workspaces == 1
    assert workspace_exists(workspace_root, orphan_workspace) is False
    assert workspace_exists(workspace_root, stuck_expired_workspace) is False

    # Üçüncü tur listeyi başa sarar; sarma da hiçbir geçerli planı silmez.
    third = reconcile_execution_plans(
        db_session, workspace_root=workspace_root, staging_stale_seconds=900, now=now
    )
    assert third.orphan_workspaces == 0
    assert third.removed_workspaces == 0

    # Bütün turlar boyunca prepared/claimed planlar korunur.
    for workspace_id in plan_workspaces:
        assert workspace_exists(workspace_root, workspace_id) is True, workspace_id
    statuses = {
        record.workspace_id: record.status
        for record in db_session.execute(select_plan()).scalars().all()
    }
    assert statuses[plan_workspaces[0]] is ExecutionPlanStatus.PREPARED
    assert statuses[claimed_workspace] is ExecutionPlanStatus.CLAIMED


def test_unusable_cursor_restarts_from_the_beginning_without_deleting_plans(
    db_session: Session,
    workspace_root: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Bozuk veya symlink bir imleç fail-closed yok sayılır; kararlar değişmez.

    İmleç yalnız pencerenin **yerini** belirler. Güvenilmez olduğunda tur baştan
    başlar: geçerli planlar yine korunur, gerçek orphan yine silinir ve symlink
    imlecin gösterdiği kök dışı hedefe dokunulmaz.
    """
    now = datetime.now(UTC)
    old = now - timedelta(seconds=7200)

    names = _sorted_workspace_names(3)
    for workspace_id in names[:2]:
        _fake_workspace(workspace_root, old, workspace_id=workspace_id)
        _store_record(db_session, records, workspace_id, now=now)
    orphan_workspace = names[2]
    _fake_workspace(workspace_root, old, workspace_id=orphan_workspace)

    cursor_path = workspace_root / ws.MAINTENANCE_CURSOR_FILENAME

    # 1) Bozuk içerik.
    cursor_path.write_text("{bu json değil", encoding="utf-8")
    assert ws.read_maintenance_cursor(workspace_root) is None

    # 2) Kök dışını gösteren symlink.
    outside = workspace_root.parent / "outside.txt"
    outside.write_text("dokunulmadı", encoding="utf-8")
    cursor_path.unlink()
    cursor_path.symlink_to(outside)
    assert ws.read_maintenance_cursor(workspace_root) is None

    result = reconcile_execution_plans(
        db_session, workspace_root=workspace_root, staging_stale_seconds=900, now=now
    )

    assert result.orphan_workspaces == 1
    assert workspace_exists(workspace_root, orphan_workspace) is False
    for workspace_id in names[:2]:
        assert workspace_exists(workspace_root, workspace_id) is True
    # Symlink izlenmedi: kök dışındaki hedef ne silindi ne de üzerine yazıldı.
    assert outside.exists()
    assert outside.read_text(encoding="utf-8") == "dokunulmadı"
    assert cursor_path.is_symlink() is False


def _sorted_workspace_names(count: int) -> list[str]:
    """Sıralı, benzersiz workspace adları.

    Testler sıraya göre rol dağıttığı için üretim rastlantısı sonuca karışmaz.
    """
    names: set[str] = set()
    while len(names) < count:
        names.add(str(uuid.uuid4()))
    return sorted(names)


def _drain(session: Session, workspace_root: Path, *, now: datetime, entries: int) -> int:
    """Bütün pencereleri kapsayacak kadar tur çalıştırır; orphan sayısını toplar."""
    rounds = -(-entries // store.MAX_RECONCILE_WORKSPACES)
    orphans = 0
    for _ in range(rounds):
        result = reconcile_execution_plans(
            session, workspace_root=workspace_root, staging_stale_seconds=900, now=now
        )
        orphans += result.orphan_workspaces
    return orphans


def _fake_workspace(root: Path, moment: datetime, *, workspace_id: str | None = None) -> str:
    """Yayımlanmış bir workspace dizinini ucuz biçimde taklit eder.

    İçerik dondurmak bu testlerin ölçtüğü şey değildir; ölçülen, dizinin
    veritabanı kayıtlarıyla eşleştirilme kararıdır. ``workspace_id`` verildiğinde
    dizin adı — dolayısıyla sıralamadaki yeri — testin denetimindedir.
    """
    workspace_id = workspace_id or str(uuid.uuid4())
    (root / workspace_id).mkdir(mode=0o700)
    _age(root / workspace_id, moment)
    return workspace_id


def _store_record(
    session: Session,
    records: tuple[Project, Inventory],
    workspace_id: str,
    *,
    now: datetime,
) -> str:
    """Verilen workspace'e bağlı, geçerli bir plan kaydı yazar; token döndürür."""
    project, inventory = records
    prepared = store_prepared_plan(
        session,
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        fingerprint=_fingerprint(project, inventory),
        mode=ExecutionMode.CHECK,
        requested_by=ACTOR,
        workspace_id=workspace_id,
        manifest_digest="f" * 64,
        ttl_seconds=TTL,
        now=now,
    )
    return prepared.token


def _token_for(session: Session, workspace_id: str) -> str | None:
    """Kaydın varlığını doğrular (token'ın kendisi saklanmaz)."""
    record = session.execute(
        select_plan().where(ExecutionPlanRecord.workspace_id == workspace_id)
    ).scalar_one_or_none()
    return None if record is None else record.token_hash


def _age(path: Path, moment: datetime) -> None:
    """Dizinin mtime'ını geçmişe alır (crash penceresi simülasyonu)."""
    timestamp = moment.timestamp()
    os.utime(path, (timestamp, timestamp))


# --- Aktif Job'a bağlı planın korunması (R1-V3C1C1B) --------------------------


def _plan_id(session: Session, workspace_id: str) -> str:
    """Workspace adına karşılık gelen plan kaydının kimliği."""
    return session.execute(
        select(ExecutionPlanRecord.id).where(ExecutionPlanRecord.workspace_id == workspace_id)
    ).scalar_one()


def _expired_claimed_plan(
    session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    *,
    age_seconds: float,
) -> tuple[str, str]:
    """TTL'si geçmiş, **claim edilmiş** bir plan yazar.

    Gerçek yaşam döngüsünün aynısıdır: bilet geçerliyken claim edilmiş, Job
    üretilmiş, sonra TTL geçmiştir. ``claim_plan_row`` süresi geçmiş bir bileti
    zaten eşleştirmediği için durum doğrudan yazılır; ölçülen şey claim değil,
    temizliğin claim **sonrası** davranışıdır.

    Returns:
        ``(plan_id, workspace_id)``.
    """
    past = datetime.now(UTC) - timedelta(seconds=age_seconds)
    _, workspace_id = _prepare(session, workspace_root, source_project, records, now=past)
    plan_id = _plan_id(session, workspace_id)
    session.execute(
        update(ExecutionPlanRecord)
        .where(ExecutionPlanRecord.id == plan_id)
        .values(status=ExecutionPlanStatus.CLAIMED, claimed_at=past)
    )
    session.commit()
    return plan_id, workspace_id


def _playbook_job(
    session: Session,
    records: tuple[Project, Inventory],
    plan_id: str,
    *,
    status: JobStatus,
) -> str:
    """Verilen plana bağlı bir PLAYBOOK Job'ı yazar."""
    project, inventory = records
    job_id = str(uuid.uuid4())
    moment = datetime.now(UTC)
    running = status is JobStatus.RUNNING
    active = status in {JobStatus.PENDING, JobStatus.RUNNING}
    session.add(
        Job(
            id=job_id,
            job_type=JobType.PLAYBOOK,
            status=status,
            execution_plan_id=plan_id,
            project_id=project.id,
            inventory_id=inventory.id,
            playbook_path=PLAYBOOK_PATH,
            requested_by=ACTOR,
            started_at=moment if status is not JobStatus.PENDING else None,
            finished_at=None if active else moment,
            worker_id=str(uuid.uuid4()) if running else None,
            heartbeat_at=moment if running else None,
            lease_expires_at=moment + timedelta(seconds=60) if running else None,
        )
    )
    session.commit()
    return job_id


def _plan_status(session: Session, workspace_id: str) -> ExecutionPlanStatus:
    session.expire_all()
    return session.execute(
        select(ExecutionPlanRecord.status).where(ExecutionPlanRecord.workspace_id == workspace_id)
    ).scalar_one()


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_expired_plan_bound_to_an_active_job_is_protected(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    status: JobStatus,
) -> None:
    """Kuyrukta bekleyen veya çalışan bir Job'ın planı TTL geçse de korunur.

    TTL bir biletin ne kadar süre *claim edilebilir* kaldığını söyler, claim
    edilmiş bir biletin işinin ne kadar sürebileceğini değil. Workspace
    silinseydi, çalışmakta olan bir execution'ın project ağacı ve inventory
    snapshot'ı altından çekilirdi; ``pending`` bir Job ise hiç başlayamadan,
    kullanıcının haberi olmadan çalıştırılamaz hâle gelirdi.

    İki sonuç **birlikte** doğrulanır: dizin duruyor **ve** kayıt hâlâ
    ``claimed``. Yalnız birini ölçmek, "expired işaretlendi ama dizin silinmedi"
    gibi yarım bir düzeltmenin testi geçmesine izin verirdi — böyle bir kayıt
    reconciliation'ın bir sonraki turunda zaten silinirdi.
    """
    _, workspace_id = _expired_claimed_plan(
        db_session, workspace_root, source_project, records, age_seconds=3600
    )
    _playbook_job(db_session, records, _plan_id(db_session, workspace_id), status=status)

    result = sweep_expired_plans(db_session, workspace_root=workspace_root)

    assert result.expired_records == 0
    assert result.removed_workspaces == 0
    assert workspace_exists(workspace_root, workspace_id) is True
    assert _plan_status(db_session, workspace_id) is ExecutionPlanStatus.CLAIMED


@pytest.mark.parametrize("status", [JobStatus.SUCCESSFUL, JobStatus.FAILED, JobStatus.CANCELED])
def test_terminal_job_does_not_protect_its_plan(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    status: JobStatus,
) -> None:
    """Job bittiğinde koruma da biter; dondurulmuş içerik toplanır.

    Koruma Job'ın *varlığına* değil **etkin** olmasına bağlıdır: aksi hâlde her
    çalıştırılmış planın workspace'i sonsuza kadar diskte kalırdı.
    """
    _, workspace_id = _expired_claimed_plan(
        db_session, workspace_root, source_project, records, age_seconds=3600
    )
    _playbook_job(db_session, records, _plan_id(db_session, workspace_id), status=status)

    result = sweep_expired_plans(db_session, workspace_root=workspace_root)

    assert result.expired_records == 1
    assert result.removed_workspaces == 1
    assert workspace_exists(workspace_root, workspace_id) is False
    assert _plan_status(db_session, workspace_id) is ExecutionPlanStatus.EXPIRED


def test_protection_is_applied_before_the_limit(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Korunan **en eski** plan, arkasındaki uygun planı aç bırakmaz.

    Regresyon riski bir uygulama ayrıntısında değil, sıralamadadır: sorgu
    ``expires_at``'e göre sıralanır ve ``LIMIT``'lenir. Koruma ``LIMIT``'ten
    sonra — sayfa çekildikten sonra Python'da — uygulansaydı, korunan en eski
    plan tek kişilik sayfayı işgal eder ve arkasındaki gerçekten atıl plan her
    turda temizlenmeden kalırdı. ``limit=1`` bu kusuru görünür kılan en dar
    ölçümdür.
    """
    _, protected = _expired_claimed_plan(
        db_session, workspace_root, source_project, records, age_seconds=7200
    )
    _playbook_job(db_session, records, _plan_id(db_session, protected), status=JobStatus.RUNNING)
    _, collectable = _prepare(
        db_session,
        workspace_root,
        source_project,
        records,
        now=datetime.now(UTC) - timedelta(seconds=3600),
    )

    result = sweep_expired_plans(db_session, workspace_root=workspace_root, limit=1)

    assert result.expired_records == 1
    assert result.removed_workspaces == 1
    assert workspace_exists(workspace_root, protected) is True
    assert workspace_exists(workspace_root, collectable) is False
    assert _plan_status(db_session, protected) is ExecutionPlanStatus.CLAIMED
    assert _plan_status(db_session, collectable) is ExecutionPlanStatus.EXPIRED


def _force_ping_plan_binding(engine: Engine, *, job_id: str, plan_id: str) -> None:
    """Bir PING satırına, veritabanının reddettiği plan bağını yazar.

    ``ck_jobs_ping_has_no_execution_plan`` böyle bir satırı normal yoldan
    üretilemez kılar — ve tam bu yüzden temizlik sorgusundaki ``job_type``
    koşulunun ayrıca ölçülmesi gerekir: koruma yalnız "plana bağlı bir Job var
    mı" diye sorsaydı, kısıt ileride gevşediğinde veya bir ping satırı başka
    bir yoldan plan taşıdığında playbook planı süresiz korunur, dondurulmuş
    project ve inventory içeriği diskte kalırdı.

    Invariant **yalnız test veritabanında**, tek bir ``UPDATE`` boyunca ve tek
    bir pragma ile delinir; pragma çağrının sonunda geri kapatılır, böylece
    bağlantı havuza kısıt doğrulaması kapalı hâlde dönmez.
    """
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        try:
            cursor.execute("PRAGMA ignore_check_constraints=ON")
            cursor.execute("UPDATE jobs SET execution_plan_id = ? WHERE id = ?", (plan_id, job_id))
            cursor.execute("PRAGMA ignore_check_constraints=OFF")
        finally:
            cursor.close()
        raw.commit()
    finally:
        raw.close()


def test_a_ping_job_never_protects_an_execution_plan(
    db_session: Session,
    migrated_engine: Engine,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """PING satırı koruma sağlamaz; plan normal biçimde toplanır."""
    project, inventory = records
    _, workspace_id = _expired_claimed_plan(
        db_session, workspace_root, source_project, records, age_seconds=3600
    )
    job_id = str(uuid.uuid4())
    db_session.add(
        Job(
            id=job_id,
            job_type=JobType.PING,
            status=JobStatus.RUNNING,
            project_id=project.id,
            inventory_id=inventory.id,
            requested_by=ACTOR,
            started_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    _force_ping_plan_binding(
        migrated_engine, job_id=job_id, plan_id=_plan_id(db_session, workspace_id)
    )

    result = sweep_expired_plans(db_session, workspace_root=workspace_root)

    assert result.expired_records == 1
    assert result.removed_workspaces == 1
    assert workspace_exists(workspace_root, workspace_id) is False
    assert _plan_status(db_session, workspace_id) is ExecutionPlanStatus.EXPIRED


def test_reconciliation_also_preserves_an_active_jobs_workspace(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Koruma reconciliation turunda da geçerlidir.

    Reconciliation :func:`sweep_expired_plans`'i kendi içinde çağırır ve
    ardından **kaydı olmayan** dizinleri toplar. Korunan plan ``expired``
    olmadığı için ikinci aşamada da dokunulmaz; ölçüm, korumanın yalnız doğrudan
    sweep çağrısında değil gerçek bakım yolunda da durduğunu gösterir.
    """
    _, workspace_id = _expired_claimed_plan(
        db_session, workspace_root, source_project, records, age_seconds=3600
    )
    _playbook_job(db_session, records, _plan_id(db_session, workspace_id), status=JobStatus.RUNNING)

    result = reconcile_execution_plans(
        db_session, workspace_root=workspace_root, staging_stale_seconds=900
    )

    assert result.expired_records == 0
    assert result.removed_workspaces == 0
    assert result.orphan_workspaces == 0
    assert workspace_exists(workspace_root, workspace_id) is True
    assert _plan_status(db_session, workspace_id) is ExecutionPlanStatus.CLAIMED
