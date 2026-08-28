"""Plan claim'i ile PLAYBOOK Job rezervasyonunun atomik bağı (R1-V3A).

Merkez iddia tek cümlededir: **bir token'ın tek geçerli sonucu bir Job'dur.**
Ölçülen dört sınır:

1. *Transaction sınırı.* Claim ile Job aynı commit'te kalıcı olur. Testler bunu
   dolaylı değil doğrudan ölçer: commit anından **önce** ikinci bir bağlantıdan
   bakıldığında ne claim ne de Job görünür.
2. *Rollback.* Job rezervasyonu veritabanı hatasıyla düşerse plan ``prepared``
   kalır, token yeniden kullanılabilir ve orphan Job oluşmaz.
3. *Dondurulmuş içerik.* Job rezerve edilmeden önce workspace'in içeriği
   diskteki gerçek baytlardan yeniden özetlenir; en küçük fark fail-closed
   reddedilir ve plan ``expired`` olur.
4. *Sızıntı.* Hiçbir hata token'ı, token'ın ön ekini, absolute path'i veya
   digest içeriğini taşımaz.

Bu dilimde hâlâ **hiçbir şey çalıştırılmaz**: burada oluşan Job ``pending``
durumunda durur, artifact üretilmez ve onu çalıştıran bir yol yoktur.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
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
from app.services.execution import workspace as ws
from app.services.execution.authorize import (
    AuthorizedPlaybookJob,
    claim_and_reserve_playbook_job,
)
from app.services.execution.store import (
    ExecutionPlanInvalidError,
    input_fingerprint,
    store_prepared_plan,
)
from app.services.execution.workspace import freeze_workspace

SNAPSHOT = '{\n  "all": {\n    "hosts": {\n      "web01": {}\n    }\n  }\n}\n'
PLAYBOOK_PATH = "site.yml"
PLAYBOOK_TEXT = "---\n- hosts: all\n"
TTL = 600.0
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
    (root / PLAYBOOK_PATH).write_text(PLAYBOOK_TEXT, encoding="utf-8")
    (root / "roles" / "web" / "tasks").mkdir(parents=True)
    (root / "roles" / "web" / "tasks" / "main.yml").write_text(
        "- name: task\n  ansible.builtin.debug:\n    msg: 'x'\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def records(db_session: Session, tmp_path: Path) -> tuple[Project, Inventory]:
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
    session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Dondurulmuş workspace ve plan kaydı üretir; ``(token, workspace_id)``."""
    project, inventory = records
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    prepared = store_prepared_plan(
        session,
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        fingerprint=_fingerprint(project, inventory),
        mode=ExecutionMode.CHECK,
        requested_by=ACTOR,
        workspace_id=frozen.workspace_id,
        manifest_digest=frozen.manifest_digest,
        ttl_seconds=TTL,
        now=now,
    )
    return prepared.token, frozen.workspace_id


def _authorize(
    session: Session,
    workspace_root: Path,
    records: tuple[Project, Inventory],
    token: str,
    **overrides: Any,
) -> AuthorizedPlaybookJob:
    project, inventory = records
    arguments: dict[str, Any] = {
        "token": token,
        "project_id": project.id,
        "inventory_id": inventory.id,
        "playbook_path": PLAYBOOK_PATH,
        "fingerprint": _fingerprint(project, inventory),
        "mode": ExecutionMode.CHECK,
        "requested_by": ACTOR,
        "workspace_root": workspace_root,
    }
    arguments.update(overrides)
    return claim_and_reserve_playbook_job(session, **arguments)


def _plan(session: Session) -> ExecutionPlanRecord:
    session.expire_all()
    return session.execute(select(ExecutionPlanRecord)).scalar_one()


def _jobs(session: Session) -> list[Job]:
    session.expire_all()
    return list(session.execute(select(Job)).scalars().all())


# --- Mutlu yol ---------------------------------------------------------------


def test_claim_reserves_exactly_one_pending_playbook_job(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Doğru token, bağlam ve aktör: plan claimed, tam bir ``pending`` Job."""
    project, inventory = records
    token, workspace_id = _prepare(db_session, workspace_root, source_project, records)

    authorized = _authorize(db_session, workspace_root, records, token)

    plan = _plan(db_session)
    assert plan.status is ExecutionPlanStatus.CLAIMED
    assert plan.claimed_at is not None

    jobs = _jobs(db_session)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == authorized.job_id
    assert uuid.UUID(job.id).version == 4
    assert job.job_type is JobType.PLAYBOOK
    assert job.status is JobStatus.PENDING
    # Bağlayıcı alanlar istekten değil **plandan** gelir.
    assert job.execution_plan_id == plan.id
    assert job.project_id == project.id
    assert job.inventory_id == inventory.id
    assert job.playbook_path == PLAYBOOK_PATH
    assert job.requested_by == ACTOR
    # Bu dilimde hiçbir şey çalıştırılmaz: Job boş bir rezervasyondur.
    assert job.limit_pattern is None
    assert job.artifact_path is None
    assert job.return_code is None
    assert job.started_at is None
    assert job.finished_at is None

    assert authorized.workspace_id == workspace_id
    assert authorized.manifest_digest == plan.manifest_digest


def test_claim_and_job_become_durable_in_a_single_commit(
    db_session: Session,
    migrated_engine: Engine,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Transaction sınırı doğrudan ölçülür: commit'ten önce ikisi de görünmez.

    Bağımsız bir bağlantı, commit çağrılmadan hemen önce hâlâ ``prepared`` bir
    plan ve **sıfır** Job görür. "Önce claim'i commit et, sonra Job yarat" ya da
    tersi bir sıra bu ölçümü geçemezdi.
    """
    token, _ = _prepare(db_session, workspace_root, source_project, records)
    observed: list[tuple[ExecutionPlanStatus, int]] = []
    commits: list[int] = []
    real_commit = db_session.commit

    def _observing_commit() -> None:
        observed.append(_observe(migrated_engine))
        commits.append(1)
        real_commit()

    db_session.commit = _observing_commit  # type: ignore[method-assign]
    try:
        _authorize(db_session, workspace_root, records, token)
    finally:
        del db_session.commit

    assert commits == [1], "başarılı yol tek commit ile tamamlanmalı"
    assert observed == [(ExecutionPlanStatus.PREPARED, 0)]
    # Commit sonrası ikisi de aynı anda kalıcıdır.
    assert _observe(migrated_engine) == (ExecutionPlanStatus.CLAIMED, 1)


def test_a_second_job_cannot_reuse_the_same_plan(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """``execution_plan_id`` unique'tir: bir onay biletinden ikinci Job doğmaz.

    Tek kullanım garantisi iki bağımsız yerde durur; burada ölçülen, plan
    satırının durumu değil **veritabanı kısıtının** kendisidir.
    """
    project, inventory = records
    token, _ = _prepare(db_session, workspace_root, source_project, records)
    authorized = _authorize(db_session, workspace_root, records, token)

    db_session.add(
        Job(
            id=str(uuid.uuid4()),
            job_type=JobType.PLAYBOOK,
            status=JobStatus.PENDING,
            execution_plan_id=authorized.plan_id,
            inventory_id=inventory.id,
            project_id=project.id,
            playbook_path=PLAYBOOK_PATH,
            requested_by=ACTOR,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    assert len(_jobs(db_session)) == 1


# --- Token korunan yollar ----------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"requested_by": "baska-aktor"}, id="actor"),
        pytest.param({"project_id": 987}, id="project"),
        pytest.param({"inventory_id": 987}, id="inventory"),
        pytest.param({"playbook_path": "baska.yml"}, id="playbook"),
        pytest.param({"fingerprint": "0" * 64}, id="fingerprint"),
    ],
)
def test_wrong_context_creates_no_job_and_keeps_the_token(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    override: dict[str, Any],
) -> None:
    """Yanlış aktör veya bağlam: Job yok, plan ``prepared``, token kullanılabilir."""
    token, _ = _prepare(db_session, workspace_root, source_project, records)

    with pytest.raises(ExecutionPlanInvalidError):
        _authorize(db_session, workspace_root, records, token, **override)

    assert _plan(db_session).status is ExecutionPlanStatus.PREPARED
    assert _jobs(db_session) == []
    # Doğru bağlamla token hâlâ tam olarak bir Job üretir.
    assert _authorize(db_session, workspace_root, records, token) is not None
    assert len(_jobs(db_session)) == 1


def test_expired_token_creates_no_job(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Süresi geçmiş token hiçbir satırı eşleştirmez."""
    past = datetime.now(UTC) - timedelta(seconds=3600)
    token, _ = _prepare(db_session, workspace_root, source_project, records, now=past)

    with pytest.raises(ExecutionPlanInvalidError):
        _authorize(db_session, workspace_root, records, token)

    assert _jobs(db_session) == []


def test_second_claim_creates_no_second_job(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Tüketilmiş token ikinci bir Job üretmez."""
    token, _ = _prepare(db_session, workspace_root, source_project, records)
    _authorize(db_session, workspace_root, records, token)

    with pytest.raises(ExecutionPlanInvalidError):
        _authorize(db_session, workspace_root, records, token)

    assert len(_jobs(db_session)) == 1


def test_concurrent_claims_produce_exactly_one_job(
    db_session: Session,
    migrated_engine: Engine,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """İki eşzamanlı çağrıdan tam olarak biri kazanır ve tam olarak bir Job oluşur."""
    project, inventory = records
    token, _ = _prepare(db_session, workspace_root, source_project, records)
    fingerprint = _fingerprint(project, inventory)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        with Session(migrated_engine) as session:
            barrier.wait(timeout=10)
            try:
                claim_and_reserve_playbook_job(
                    session,
                    token=token,
                    project_id=project.id,
                    inventory_id=inventory.id,
                    playbook_path=PLAYBOOK_PATH,
                    fingerprint=fingerprint,
                    mode=ExecutionMode.CHECK,
                    requested_by=ACTOR,
                    workspace_root=workspace_root,
                )
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
    assert _plan(db_session).status is ExecutionPlanStatus.CLAIMED
    assert len(_jobs(db_session)) == 1


def test_failed_job_insert_rolls_the_claim_back(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job INSERT düşerse claim de düşer: token geri gelir, orphan Job kalmaz.

    Hata taklit edilmez, gerçek bir kısıt ihlaliyle üretilir: rezervasyon,
    kimliği zaten kullanılan bir Job satırı yazmayı dener.
    """
    project, inventory = records
    collision = str(uuid.uuid4())
    db_session.add(
        Job(
            id=collision,
            job_type=JobType.PING,
            status=JobStatus.FAILED,
            inventory_id=inventory.id,
            project_id=project.id,
            requested_by=ACTOR,
        )
    )
    db_session.commit()

    token, _ = _prepare(db_session, workspace_root, source_project, records)
    monkeypatch.setattr("app.services.execution.authorize.uuid.uuid4", lambda: uuid.UUID(collision))

    with pytest.raises(IntegrityError):
        _authorize(db_session, workspace_root, records, token)

    # Onaylanan içerik sağlamdı; başarısız olan yalnızca rezervasyondu.
    assert _plan(db_session).status is ExecutionPlanStatus.PREPARED
    assert [job.id for job in _jobs(db_session)] == [collision]

    # Session kullanılabilir durumda kaldı ve token yeniden kullanılabilir.
    monkeypatch.undo()
    authorized = _authorize(db_session, workspace_root, records, token)
    assert authorized.job_id != collision
    assert len(_jobs(db_session)) == 2


def test_failed_final_commit_rolls_the_claim_back(
    db_session: Session,
    migrated_engine: Engine,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Arıza commit anında yüzeye çıkarsa da claim ile Job birlikte düşer.

    Bir kısıt ihlali her zaman ``flush``'ta görünmez: deferred kontroller ve
    dialect farkları onu commit'e kadar erteleyebilir. Yakalanmamış bir commit
    hatası session'ı çağırana kirli bırakır ve tüketilmiş görünen bir token'ın
    arkasında hiçbir Job bulunmazdı.
    """
    token, _ = _prepare(db_session, workspace_root, source_project, records)
    real_commit = db_session.commit
    attempts: list[int] = []

    def _failing_commit() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("COMMIT", {}, Exception("disk I/O error"))
        real_commit()

    db_session.commit = _failing_commit  # type: ignore[method-assign]
    try:
        with pytest.raises(OperationalError):
            _authorize(db_session, workspace_root, records, token)

        # Rollback servisin içinde çalıştı: plan `prepared`, Job yok.
        assert _observe(migrated_engine) == (ExecutionPlanStatus.PREPARED, 0)
    finally:
        del db_session.commit

    # Session kullanılabilir kaldı ve aynı token tam bir Job üretir.
    authorized = _authorize(db_session, workspace_root, records, token)
    assert _observe(migrated_engine) == (ExecutionPlanStatus.CLAIMED, 1)
    assert [job.id for job in _jobs(db_session)] == [authorized.job_id]


# --- Dondurulmuş içerik ------------------------------------------------------


def test_missing_workspace_expires_the_plan_without_a_job(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Dondurulmuş içerik kaybolduysa: Job yok, plan ``expired``, token geri gelmez."""
    token, workspace_id = _prepare(db_session, workspace_root, source_project, records)
    assert ws.remove_workspace(workspace_root, workspace_id) is True

    with pytest.raises(ExecutionPlanInvalidError):
        _authorize(db_session, workspace_root, records, token)

    assert _plan(db_session).status is ExecutionPlanStatus.EXPIRED
    assert _jobs(db_session) == []
    # Bilet geri alınamaz: içeriği değişmiş bir planı yeniden denemek mümkün değil.
    with pytest.raises(ExecutionPlanInvalidError):
        _authorize(db_session, workspace_root, records, token)
    assert _jobs(db_session) == []


def test_source_project_is_never_reopened(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Özgün ağaç değişse, hatta tümüyle silinse de yetkilendirme etkilenmez.

    Doğrulanan tek şey dondurulmuş kopyadır; özgün project yeniden açılsaydı
    silinmiş bir ağaç yetkilendirmeyi düşürürdü.
    """
    token, _ = _prepare(db_session, workspace_root, source_project, records)
    (source_project / PLAYBOOK_PATH).write_text("---\n- hosts: baska\n", encoding="utf-8")
    for path in sorted(source_project.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    source_project.rmdir()

    authorized = _authorize(db_session, workspace_root, records, token)

    assert authorized.playbook_path == PLAYBOOK_PATH
    assert len(_jobs(db_session)) == 1


def _append(path: Path) -> None:
    """Dosyanın sonuna tek satır ekler; izin bitleri korunur."""
    path.write_text(path.read_text(encoding="utf-8") + "# sonradan\n", encoding="utf-8")


def _retag_manifest_entry(path: Path) -> None:
    """Yalnız ``manifest.json``'ı bozar; dondurulmuş baytlara dokunmaz.

    Bir girdinin sha256'sı değiştirilir: diskteki gerçek içerik değişmediği için
    yeniden hesaplanan digest kayıttakiyle **tutar**. Manifest'e körü körüne
    güvenen bir doğrulama bu noktada "her şey yolunda" derdi.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    document["entries"][-1]["sha256"] = "0" * 64
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


FROZEN_MUTATIONS: dict[str, tuple[tuple[str, ...], Any]] = {
    "playbook": (("project", PLAYBOOK_PATH), _append),
    "nested": (("project", "roles", "web", "tasks", "main.yml"), _append),
    "inventory": (("inventory", "hosts.yml"), _append),
    "manifest": (("manifest.json",), _retag_manifest_entry),
}


@pytest.mark.parametrize("mutation", sorted(FROZEN_MUTATIONS))
def test_modified_frozen_content_is_refused(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    mutation: str,
) -> None:
    """Dondurulmuş içeriğin herhangi bir baytı değişirse yetkilendirme düşer.

    Manifest dosyası ayrı bir vakadır: içerik doğrulaması onu kapsamaz, bu
    yüzden yalnız manifest'in değiştirildiği durum ancak manifest'in kendisi
    yeniden doğrulandığı için yakalanır. Manifest'e körü körüne güvenen bir
    doğrulama bu vakayı kaçırırdı.
    """
    token, workspace_id = _prepare(db_session, workspace_root, source_project, records)
    relative, mutate = FROZEN_MUTATIONS[mutation]
    mutate(workspace_root.joinpath(workspace_id, *relative))

    with pytest.raises(ExecutionPlanInvalidError):
        _authorize(db_session, workspace_root, records, token)

    assert _jobs(db_session) == []
    assert _plan(db_session).status is ExecutionPlanStatus.EXPIRED


@pytest.mark.parametrize("shape", ["extra_file", "extra_root_entry", "symlink", "special", "mode"])
def test_unexpected_frozen_entries_are_refused(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    shape: str,
) -> None:
    """Eklenen, symlink'e çevrilen, özel veya izni değişmiş girdi fail-closed reddedilir."""
    token, workspace_id = _prepare(db_session, workspace_root, source_project, records)
    workspace = workspace_root / workspace_id
    frozen_project = workspace / "project"

    if shape == "extra_file":
        (frozen_project / "eklenen.yml").write_text("---\n", encoding="utf-8")
    elif shape == "extra_root_entry":
        (workspace / "fazladan").mkdir(mode=0o700)
    elif shape == "symlink":
        (frozen_project / "kisayol.yml").symlink_to(frozen_project / PLAYBOOK_PATH)
    elif shape == "special":
        os.mkfifo(frozen_project / "boru", 0o600)
    else:
        (frozen_project / PLAYBOOK_PATH).chmod(0o644)

    with pytest.raises(ExecutionPlanInvalidError):
        _authorize(db_session, workspace_root, records, token)

    assert _jobs(db_session) == []
    assert _plan(db_session).status is ExecutionPlanStatus.EXPIRED


# --- Sızıntı -----------------------------------------------------------------


def test_errors_leak_neither_token_nor_paths(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Hata mesajı ve ``details`` token, ön ek, path veya digest taşımaz."""
    token, workspace_id = _prepare(db_session, workspace_root, source_project, records)
    digest = _plan(db_session).manifest_digest
    (workspace_root / workspace_id / "project" / PLAYBOOK_PATH).write_text(
        "---\n- hosts: degisti\n", encoding="utf-8"
    )

    with pytest.raises(ExecutionPlanInvalidError) as integrity_error:
        _authorize(db_session, workspace_root, records, token)

    token_reused, _ = _prepare(db_session, workspace_root, source_project, records)
    with pytest.raises(ExecutionPlanInvalidError) as mismatch_error:
        _authorize(db_session, workspace_root, records, token_reused, project_id=987)

    for error in (integrity_error.value, mismatch_error.value):
        rendered = f"{error.message} {error.details}"
        # Hangi kontrolün takıldığı da dışarıdan görünmez: iki yol aynı kodu döner.
        assert error.code == "execution_plan_invalid"
        assert error.details == {"reason": "invalid"}
        for secret in (token, token[:8], token_reused[:8], workspace_id, digest):
            assert secret not in rendered
        for path in (str(workspace_root), str(source_project)):
            assert path not in rendered


def _observe(engine: Engine) -> tuple[ExecutionPlanStatus, int]:
    """Bağımsız bir bağlantıdan **commit edilmiş** durumu okur."""
    with Session(engine) as observer:
        status = observer.execute(select(ExecutionPlanRecord.status)).scalar_one()
        jobs = observer.execute(select(func.count()).select_from(Job)).scalar_one()
    return status, int(jobs)
