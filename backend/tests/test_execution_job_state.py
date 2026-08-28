"""PLAYBOOK Job acquire + heartbeat + finish + reconcile durum makinesi.

R1-V3C1C1A/B ve R1-V3C2A.

Ölçülen beş sınır:

1. *Atomiklik.* ``pending → running`` geçişi ile sahiplik/başlangıç/kira
   alanlarının tamamı **aynı** commit'te kalıcı olur. Bağımsız bir bağlantı,
   commit'ten hemen önce hâlâ sahipsiz bir ``pending`` satır görür.
2. *Yarış.* İki ayrı session/connection aynı adayı görse de yalnız biri
   kazanır; kaybeden hiçbir execution context üretmez ve hiçbir satır
   değiştirmez.
3. *Bağ doğrulaması.* Yetkilendirmesi eksik veya tutarsız bir Job ne
   çalıştırılır ne de ``pending`` bırakılır: terminal ``failed`` olur.
4. *Kira sözleşmesi.* Kirayı yalnız sahibi ve yalnız kira **dolmadan**
   yenileyebilir; sınır kesindir (``lease_expires_at == now`` yenilenmez).
   Terminal geçişte kira alanları aynı commit'te boşaltılır.
5. *Transaction hijyeni.* Başarı, no-op ve hata yollarının hiçbiri çağırana
   açık transaction bırakmaz; commit hatası rollback'le kapanır.

Startup reconciliation aynı beş sınırın üzerine bir **yetki** sınırı koyar:
kirası dolmuş bir satırı kapatan tek gerekçe kira süresidir. Sahiplik farkı,
restart olgusu veya satırın yaşı tek başına yetmez; canlı bir kira her koşulda
korunur ve kapatılan satır ``pending``'e geri döndürülmez.

Finish yolunda ayrıca bir **sonuç sözleşmesi** ölçülür: ``successful`` ile
``failed`` farklı alan invariantları taşır, hata kodu sabit bir sözlükten
gelir ve ``artifact_path`` yalnız **aynı** Job'ın yayımlanmış sonucunu
gösterebilir. Geçersiz bir sonuç veritabanına hiç ulaşmaz.

Testler gerçek veritabanı davranışını ölçer: rowcount mock'lanmaz, yarış iki
gerçek session/connection üzerinde koşulur ve şema gerçek migration zinciriyle
kurulur.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import math
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import pytest
from sqlalchemy import Engine, event, func, select, text, update
from sqlalchemy.exc import OperationalError
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
from app.services.execution import job_state
from app.services.execution.job_state import (
    ERROR_EXECUTION_BINDING_INVALID,
    ERROR_INTERRUPTED_BY_RESTART,
    FINISH_ERROR_CODES,
    MAX_LEASE_SECONDS,
    AcquiredPlaybookJob,
    AcquireOutcome,
    acquire_pending_playbook_job,
    finish_playbook_job,
    heartbeat_playbook_job,
    reconcile_stale_playbook_jobs,
)

PLAYBOOK_PATH = "site.yml"
ACTOR = "yerel-operator"
LEASE = 30.0
ACTIVE_PLAYBOOK_INDEX = "uq_jobs_active_playbook_global"


@pytest.fixture
def records(db_session: Session, tmp_path: Any) -> tuple[Project, Inventory]:
    """Job ve planın FK'lerini karşılayan asgari kayıtlar."""
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


def _hex64() -> str:
    """Tekil, 64 küçük harfli hex karakter (token_hash/digest biçimi)."""
    return uuid.uuid4().hex * 2


def _seed(
    session: Session,
    records: tuple[Project, Inventory],
    *,
    plan_status: ExecutionPlanStatus = ExecutionPlanStatus.CLAIMED,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    plan_overrides: dict[str, Any] | None = None,
    job_overrides: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Claim edilmiş bir plan ve ona bağlı ``pending`` PLAYBOOK Job'ı yazar.

    Varsayılan hâl **geçerli** bir yetkilendirmedir; her uyuşmazlık testi
    yalnızca tek bir alanı bozar, böylece reddin sebebi tekildir.
    """
    project, inventory = records
    moment = created_at or datetime.now(UTC)
    plan_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())

    plan_fields: dict[str, Any] = {
        "id": plan_id,
        "token_hash": _hex64(),
        "project_id": project.id,
        "inventory_id": inventory.id,
        "playbook_path": PLAYBOOK_PATH,
        "requested_by": ACTOR,
        "input_fingerprint": _hex64(),
        "workspace_id": str(uuid.uuid4()),
        "manifest_digest": _hex64(),
        "status": plan_status,
        "created_at": moment,
        "expires_at": expires_at or (moment + timedelta(hours=1)),
        "claimed_at": moment if plan_status is ExecutionPlanStatus.CLAIMED else None,
    }
    plan_fields.update(plan_overrides or {})
    session.add(ExecutionPlanRecord(**plan_fields))
    session.flush()

    job_fields: dict[str, Any] = {
        "id": job_id,
        "job_type": JobType.PLAYBOOK,
        "status": JobStatus.PENDING,
        "execution_plan_id": plan_id,
        "project_id": project.id,
        "inventory_id": inventory.id,
        "playbook_path": PLAYBOOK_PATH,
        "limit_pattern": None,
        "requested_by": ACTOR,
        "created_at": moment,
    }
    job_fields.update(job_overrides or {})
    session.add(Job(**job_fields))
    session.commit()
    return job_id, plan_id


def _acquire(session: Session, **overrides: Any) -> job_state.AcquireResult:
    arguments: dict[str, Any] = {"worker_id": str(uuid.uuid4()), "lease_seconds": LEASE}
    arguments.update(overrides)
    return acquire_pending_playbook_job(session, **arguments)


def _job(session: Session, job_id: str) -> Job:
    session.expire_all()
    return session.execute(select(Job).where(Job.id == job_id)).scalar_one()


def _naive(moment: datetime) -> datetime:
    """SQLite ``DateTime`` sütunları naive UTC döndürür; karşılaştırma böyle yapılır."""
    return moment.astimezone(UTC).replace(tzinfo=None)


def _snapshot(
    engine: Engine, job_id: str
) -> tuple[JobStatus, str | None, datetime | None, datetime | None, datetime | None]:
    """Bağımsız bir bağlantıdan **commit edilmiş** satır durumunu okur."""
    with Session(engine) as observer:
        row = observer.execute(
            select(
                Job.status,
                Job.worker_id,
                Job.started_at,
                Job.heartbeat_at,
                Job.lease_expires_at,
            ).where(Job.id == job_id)
        ).one()
    return (row.status, row.worker_id, row.started_at, row.heartbeat_at, row.lease_expires_at)


def _raw_sql(engine: Engine, statement: str, parameters: tuple[Any, ...] = ()) -> None:
    """ORM ve FK doğrulamasını atlayarak ham SQL çalıştırır.

    İki test bunu kullanır: model validator'ının (canonical ``workspace_id``) ve
    ``RESTRICT`` foreign key'in üretilmesine izin vermediği **bozuk** satırlar.
    Servisin bu satırlara karşı savunması ancak böyle ölçülebilir. PRAGMA
    çağrının sonunda geri açılır: bağlantı havuza FK doğrulaması kapalı hâlde
    dönerse sonraki testler sessizce zayıflardı.
    """
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.execute(statement, parameters)
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
        raw.commit()
    finally:
        raw.close()


@pytest.fixture
def counted_statements(migrated_engine: Engine) -> Iterator[list[str]]:
    """Engine üzerinde çalıştırılan her SQL ifadesini kaydeder."""
    seen: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any, **_kwargs: Any) -> None:
        seen.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _record)


# --- Acquire: mutlu yol ------------------------------------------------------


def test_valid_pending_job_is_acquired_with_immutable_context(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Geçerli Job ``running`` olur ve değişmez bir execution context döner."""
    project, inventory = records
    job_id, plan_id = _seed(db_session, records)
    worker = str(uuid.uuid4())

    result = _acquire(db_session, worker_id=worker)

    assert result.outcome is AcquireOutcome.ACQUIRED
    context = result.context
    assert context is not None
    assert context == AcquiredPlaybookJob(
        job_id=job_id,
        execution_plan_id=plan_id,
        workspace_id=context.workspace_id,
        manifest_digest=context.manifest_digest,
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        requested_by=ACTOR,
        mode=ExecutionMode.CHECK,
        worker_id=worker,
    )
    # Context gerçekten immutable'dır.
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.job_id = "baska"  # type: ignore[misc]

    job = _job(db_session, job_id)
    assert job.status is JobStatus.RUNNING
    assert job.worker_id == worker


def test_all_lease_fields_are_written_in_the_same_transition(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Sahiplik, başlangıç ve kira alanları tek geçişte ve tek commit'te yazılır.

    Ölçüm dolaylı değildir: bağımsız bir bağlantı, commit çağrılmadan hemen önce
    hâlâ **sahipsiz** bir ``pending`` satır görür. "Önce running yap, sonra
    kirayı yaz" gibi iki adımlı bir uygulama bu ölçümü geçemezdi.
    """
    job_id, _ = _seed(db_session, records)
    worker = str(uuid.uuid4())
    moment = datetime.now(UTC)
    observed: list[tuple[Any, ...]] = []
    real_commit = db_session.commit

    def _observing_commit() -> None:
        observed.append(_snapshot(migrated_engine, job_id))
        real_commit()

    db_session.commit = _observing_commit  # type: ignore[method-assign]
    try:
        result = _acquire(db_session, worker_id=worker, now=moment)
    finally:
        del db_session.commit

    assert result.outcome is AcquireOutcome.ACQUIRED
    assert observed == [(JobStatus.PENDING, None, None, None, None)], "tek commit, tek geçiş"
    assert _snapshot(migrated_engine, job_id) == (
        JobStatus.RUNNING,
        worker,
        _naive(moment),
        _naive(moment),
        _naive(moment + timedelta(seconds=LEASE)),
    )


def test_oldest_pending_candidate_is_selected(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Birden çok aday varsa **en eski** ``pending`` Job alınır.

    Global aktif PLAYBOOK sınırı 1 olduğu için ikinci bir ``pending`` satır
    üretilebilmesi adına ``uq_jobs_active_playbook_global`` **yalnız bu testin
    veritabanında** düşürülür. Sıralama gene de gerçek bir sözleşmedir: kuyruk
    sırası veritabanı invariantının bir yan etkisine bırakılırsa, sınır ileride
    gevşetildiğinde seçim sessizce sürücünün satır sırasına düşerdi.
    """
    old = datetime.now(UTC) - timedelta(minutes=10)
    new = datetime.now(UTC) - timedelta(minutes=1)
    _raw_sql(migrated_engine, f"DROP INDEX {ACTIVE_PLAYBOOK_INDEX}")
    newer_id, _ = _seed(db_session, records, created_at=new)
    older_id, _ = _seed(db_session, records, created_at=old)

    result = _acquire(db_session)

    assert result.context is not None
    assert result.context.job_id == older_id
    assert _job(db_session, newer_id).status is JobStatus.PENDING


def test_ping_and_terminal_jobs_are_never_candidates(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Aday havuzu yalnız ``pending`` PLAYBOOK satırlarıdır."""
    project, inventory = records
    db_session.add(
        Job(
            id=str(uuid.uuid4()),
            job_type=JobType.PING,
            status=JobStatus.PENDING,
            project_id=project.id,
            inventory_id=inventory.id,
            requested_by=ACTOR,
        )
    )
    db_session.commit()
    _seed(db_session, records, job_overrides={"status": JobStatus.FAILED})

    assert _acquire(db_session).outcome is AcquireOutcome.IDLE


def test_empty_queue_is_idle(db_session: Session) -> None:
    """Aday yoksa sonuç açıkça ``IDLE``'dır; hata değildir."""
    result = _acquire(db_session)

    assert result.outcome is AcquireOutcome.IDLE
    assert result.context is None


# --- Acquire: yarış ----------------------------------------------------------


LEADER = "lider"
FOLLOWER = "takipci"


def test_only_one_of_two_racing_sessions_wins(
    db_session: Session,
    migrated_engine: Engine,
    records: tuple[Project, Inventory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aynı adayı gören iki session/connection'dan yalnız biri kazanır.

    Yarış mock'lanmış bir ``rowcount`` ile değil, dosya tabanlı SQLite üzerinde
    iki gerçek session/connection ile koşulur. Kritik nokta bir barrier ile
    kurulur: **ikisi de** aynı ``pending`` adayı okumadan hiçbiri yazmaya
    başlamaz. Aday ``SELECT``'inin bir rezervasyon olmadığı, ancak iki taraf da
    aynı satırı gördükten sonra ölçülebilir.

    Yazma sırası bilinçli olarak sabitlenir. SQLite tek yazar kilidiyle çalışır;
    sırayı rastlantıya bırakmak testi kilit zamanlamasına bağımlı kılar ve
    ölçülen şey atomiklik değil, busy-timeout davranışı olurdu. Sıra
    sabitlendiğinde iddia da güçlenir: **hangi** tarafın kazanması gerektiği
    baştan bellidir.
    """
    job_id, _ = _seed(db_session, records)
    workers = {LEADER: str(uuid.uuid4()), FOLLOWER: str(uuid.uuid4())}
    both_read = threading.Barrier(2)
    leader_finished = threading.Event()
    real_validator = job_state._binding_is_valid
    outcomes: dict[str, AcquireOutcome] = {}
    failures: list[BaseException] = []
    lock = threading.Lock()

    def _gated_validator(candidate: Any, plan: Any) -> bool:
        both_read.wait(timeout=10)
        if threading.current_thread().name == FOLLOWER:
            assert leader_finished.wait(timeout=10)
        return real_validator(candidate, plan)

    monkeypatch.setattr(job_state, "_binding_is_valid", _gated_validator)

    def attempt(name: str) -> None:
        try:
            with Session(migrated_engine) as session:
                result = acquire_pending_playbook_job(
                    session, worker_id=workers[name], lease_seconds=LEASE
                )
            with lock:
                outcomes[name] = result.outcome
                if result.outcome is AcquireOutcome.ACQUIRED:
                    assert result.context is not None
                    assert result.context.worker_id == workers[name]
        except BaseException as error:  # noqa: BLE001 - hata testin kendisine taşınır
            with lock:
                failures.append(error)
        finally:
            if name == LEADER:
                leader_finished.set()
            both_read.abort()

    threads = [threading.Thread(target=attempt, args=(name,), name=name) for name in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert failures == []
    assert outcomes == {LEADER: AcquireOutcome.ACQUIRED, FOLLOWER: AcquireOutcome.IDLE}
    job = _job(db_session, job_id)
    assert job.status is JobStatus.RUNNING
    assert job.worker_id == workers[LEADER]


def test_lost_race_produces_no_context_and_changes_nothing(
    db_session: Session,
    records: tuple[Project, Inventory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aday seçildikten sonra satır kaybedilirse: context yok, satır bozulmaz.

    Enterleme bilinçli olarak **aynı bağlantı** üzerinden yapılır: SQLite'ta
    gerçek çapraz-bağlantı enterlemesi kilit beklemesine dönüşür ve ölçülmek
    istenen şey kilit davranışı değil, koşullu ``UPDATE``'in hiçbir satırı
    etkilemediğinde ne yaptığıdır.
    """
    job_id, _ = _seed(db_session, records)
    real_validator = job_state._binding_is_valid

    def _stealing_validator(candidate: Any, plan: Any) -> bool:
        db_session.execute(update(Job).where(Job.id == job_id).values(status=JobStatus.CANCELED))
        return real_validator(candidate, plan)

    monkeypatch.setattr(job_state, "_binding_is_valid", _stealing_validator)

    result = _acquire(db_session)

    assert result.outcome is AcquireOutcome.IDLE
    assert result.context is None
    assert not db_session.in_transaction()
    # Kaybeden taraf rollback etti: çalınmış görünen değişiklik de geri alındı.
    job = _job(db_session, job_id)
    assert job.status is JobStatus.PENDING
    assert job.worker_id is None


# --- Acquire: plan bağı ------------------------------------------------------


def test_prepared_plan_is_refused(db_session: Session, records: tuple[Project, Inventory]) -> None:
    """Henüz claim edilmemiş plana bağlı Job çalıştırılmaz."""
    job_id, _ = _seed(db_session, records, plan_status=ExecutionPlanStatus.PREPARED)

    assert _acquire(db_session).outcome is AcquireOutcome.BINDING_INVALID
    assert _job(db_session, job_id).status is JobStatus.FAILED


@pytest.mark.parametrize("plan_status", [ExecutionPlanStatus.PREPARED, ExecutionPlanStatus.EXPIRED])
def test_non_claimed_plan_states_are_refused(
    db_session: Session,
    records: tuple[Project, Inventory],
    plan_status: ExecutionPlanStatus,
) -> None:
    """``claimed`` dışındaki her plan durumu reddedilir."""
    _seed(db_session, records, plan_status=plan_status)

    assert _acquire(db_session).outcome is AcquireOutcome.BINDING_INVALID


def test_missing_plan_is_refused(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Bağlı plan satırı yoksa Job çalıştırılmaz.

    Bu satır normal yoldan üretilemez (``RESTRICT`` foreign key planın
    silinmesini engeller); tam da bu yüzden savunmanın ölçülmesi gerekir:
    yetkilendirmesi kaybolmuş bir Job'ın çalıştırılabilir kalması, onay
    zincirinin tümünü anlamsızlaştırırdı.
    """
    job_id, plan_id = _seed(db_session, records)
    _raw_sql(migrated_engine, "DELETE FROM execution_plans WHERE id = ?", (plan_id,))

    assert _acquire(db_session).outcome is AcquireOutcome.BINDING_INVALID
    assert _job(db_session, job_id).status is JobStatus.FAILED


def test_limit_pattern_is_never_authorized(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """``limit_pattern`` taşıyan bir Job hiçbir plana bağlı sayılmaz.

    ``limit`` bir uyuşmazlık değil bir **yasaktır**: onaylanan plan ``limit``
    taşımaz, dolayısıyla limit taşıyan bir Job kullanıcının onayladığından başka
    bir hedef kümesine çalışırdı.

    Bu durum ``pending`` bir PLAYBOOK satırında **üretilemez**:
    ``ck_jobs_active_playbook_is_authorized`` onu ham SQL ile bile reddeder
    (``execution_plan_id IS NULL`` durumu için de aynısı geçerlidir; o yol
    :func:`test_missing_plan_is_refused` ile ölçülür). Bu yüzden ölçüm,
    gerçek satırlardan okunan gerçek ``Row``'larla doğrudan bağ yüklemi
    üzerinde yapılır: yasak, veritabanı kısıtı ileride gevşetilse de servis
    katmanında durmalıdır.
    """
    project, inventory = records
    _, plan_id = _seed(db_session, records)
    job_id = str(uuid.uuid4())
    # PING satırı `limit_pattern` taşıyabilir; tek fark budur.
    db_session.add(
        Job(
            id=job_id,
            job_type=JobType.PING,
            status=JobStatus.PENDING,
            project_id=project.id,
            inventory_id=inventory.id,
            playbook_path=PLAYBOOK_PATH,
            limit_pattern="web01",
            requested_by=ACTOR,
        )
    )
    db_session.commit()

    candidate = db_session.execute(
        select(
            Job.id,
            Job.execution_plan_id,
            Job.project_id,
            Job.inventory_id,
            Job.playbook_path,
            Job.requested_by,
            Job.limit_pattern,
            Job.mode,
        ).where(Job.id == job_id)
    ).one()
    plan = db_session.execute(
        select(
            ExecutionPlanRecord.status,
            ExecutionPlanRecord.project_id,
            ExecutionPlanRecord.inventory_id,
            ExecutionPlanRecord.playbook_path,
            ExecutionPlanRecord.requested_by,
            ExecutionPlanRecord.workspace_id,
            ExecutionPlanRecord.manifest_digest,
            ExecutionPlanRecord.mode,
        ).where(ExecutionPlanRecord.id == plan_id)
    ).one()

    assert job_state._binding_is_valid(candidate, plan) is False
    # Limit dışındaki her alan uyuşuyor: reddin sebebi tekildir.
    assert candidate.project_id == plan.project_id
    assert candidate.inventory_id == plan.inventory_id
    assert candidate.playbook_path == plan.playbook_path
    assert candidate.requested_by == plan.requested_by
    assert candidate.mode == plan.mode


# --- Acquire: execution mode bağı (R1-V3H1B2A) -------------------------------


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(ExecutionMode.CHECK, id="check"),
        pytest.param(ExecutionMode.NORMAL, id="normal"),
    ],
)
def test_acquired_context_carries_the_jobs_own_mode(
    db_session: Session, records: tuple[Project, Inventory], mode: ExecutionMode
) -> None:
    """Kazanılmış bağlam, Job satırının kipini taşır.

    İki kip de ölçülür: yalnız ``check`` ölçülseydi, kipi sabit yazan bir
    uygulama da yeşil kalırdı ve alanın gerçekten satırdan okunduğu
    görülemezdi. Bağlamın kipi Job satırındaki değerle **aynı** nesnedir;
    plan ile eşitliği kabul koşulu olduğu için ikisi arasında fark yoktur.
    """
    job_id, _ = _seed(
        db_session,
        records,
        plan_overrides={"mode": mode},
        job_overrides={"mode": mode},
    )

    result = _acquire(db_session)

    assert result.outcome is AcquireOutcome.ACQUIRED
    context = result.context
    assert context is not None
    assert context.mode is mode
    job = _job(db_session, job_id)
    assert job.status is JobStatus.RUNNING
    assert job.mode is mode


@pytest.mark.parametrize(
    ("job_mode", "plan_mode"),
    [
        pytest.param(ExecutionMode.CHECK, ExecutionMode.NORMAL, id="check-job-normal-plan"),
        pytest.param(ExecutionMode.NORMAL, ExecutionMode.CHECK, id="normal-job-check-plan"),
    ],
)
def test_mode_mismatch_is_refused_in_both_directions(
    db_session: Session,
    records: tuple[Project, Inventory],
    job_mode: ExecutionMode,
    plan_mode: ExecutionMode,
) -> None:
    """Job ile planın kipi ayrışırsa yetkilendirme geçersizdir.

    İki yön de ölçülür çünkü ikisi farklı hataları temsil eder: ``normal`` Job
    + ``check`` plan, kullanıcının onayladığından **daha geniş** yetkiyle
    çalışmak demektir; ters yön ise onaylanan işin hiç yapılmaması. Tek yönlü
    bir kontrol ilkini yakalayıp ikincisini sessizce geçirirdi.

    Red, kipe **özel** bir sonuç üretmez: sonuç, bağı bozuk her Job ile aynı
    generic ``BINDING_INVALID`` yoludur. Kipe özel bir hata kodu, geçersiz bir
    satırın hangi bağlama ait olduğunu deneme yanılmayla öğrenilebilir kılardı.
    """
    job_id, _ = _seed(
        db_session,
        records,
        plan_overrides={"mode": plan_mode},
        job_overrides={"mode": job_mode},
    )

    result = _acquire(db_session)

    assert result.outcome is AcquireOutcome.BINDING_INVALID
    assert result.context is None
    job = _job(db_session, job_id)
    assert job.status is JobStatus.FAILED
    assert job.error_code == ERROR_EXECUTION_BINDING_INVALID
    assert job.finished_at is not None
    # Çalıştırma hiç başlamadı: ne sahiplik, ne kira, ne başlangıç anı yazıldı.
    assert job.started_at is None
    assert job.worker_id is None
    assert job.heartbeat_at is None
    assert job.lease_expires_at is None
    assert job.return_code is None
    assert job.artifact_path is None


@pytest.mark.parametrize(
    ("job_mode", "plan_mode"),
    [
        pytest.param(ExecutionMode.CHECK, ExecutionMode.NORMAL, id="check-job-normal-plan"),
        pytest.param(ExecutionMode.NORMAL, ExecutionMode.CHECK, id="normal-job-check-plan"),
    ],
)
def test_mode_mismatch_rows_are_accepted_by_the_database(
    db_session: Session,
    records: tuple[Project, Inventory],
    job_mode: ExecutionMode,
    plan_mode: ExecutionMode,
) -> None:
    """Uyuşmazlığı reddeden şema değil, servis katmanıdır.

    İki satırın kendi ``execution_mode`` CHECK kısıtları geçerlidir ve veritabanı
    ikisini de yazar; ayrışan tek şey **çapraz** bağdır. Ölçüm olmasaydı,
    uyuşmazlık testleri satır hiç yazılamadığı için de yeşil görünebilir ve
    servis katmanındaki kontrolün varlığı kanıtlanmamış olurdu.
    """
    job_id, plan_id = _seed(
        db_session,
        records,
        plan_overrides={"mode": plan_mode},
        job_overrides={"mode": job_mode},
    )

    job = _job(db_session, job_id)
    plan = db_session.execute(
        select(ExecutionPlanRecord).where(ExecutionPlanRecord.id == plan_id)
    ).scalar_one()

    assert job.mode is job_mode
    assert plan.mode is plan_mode
    assert job.status is JobStatus.PENDING
    # Kip dışında her alan uyuşuyor: reddin sebebi tekildir.
    assert job.project_id == plan.project_id
    assert job.inventory_id == plan.inventory_id
    assert job.playbook_path == plan.playbook_path
    assert job.requested_by == plan.requested_by
    assert plan.status is ExecutionPlanStatus.CLAIMED


def test_acquired_context_requires_an_explicit_mode() -> None:
    """``AcquiredPlaybookJob.mode`` zorunludur ve varsayılanı yoktur.

    Varsayılan taşısaydı, kipi hiç okumayan bir çağrı sessizce ``check``
    üretirdi: ``normal`` çalıştırılması gereken bir iş, hiçbir hata vermeden
    ``--check`` altında koşar ve kullanıcı işin yapıldığını sanırdı.
    """
    field = next(item for item in dataclasses.fields(AcquiredPlaybookJob) if item.name == "mode")
    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING
    assert field.type in {"ExecutionMode", ExecutionMode}

    with pytest.raises(TypeError):
        AcquiredPlaybookJob(  # type: ignore[call-arg]
            job_id=str(uuid.uuid4()),
            execution_plan_id=str(uuid.uuid4()),
            workspace_id=str(uuid.uuid4()),
            manifest_digest="a" * 64,
            project_id=1,
            inventory_id=1,
            playbook_path=PLAYBOOK_PATH,
            requested_by=ACTOR,
            worker_id=str(uuid.uuid4()),
        )


def test_project_mismatch_is_refused(
    db_session: Session, tmp_path: Any, records: tuple[Project, Inventory]
) -> None:
    """Job ile planın project'i ayrışırsa yetkilendirme geçersizdir."""
    other = Project(name="Diger", path=str(tmp_path / "diger"))
    db_session.add(other)
    db_session.commit()
    _seed(db_session, records, job_overrides={"project_id": other.id})

    assert _acquire(db_session).outcome is AcquireOutcome.BINDING_INVALID


def test_inventory_mismatch_is_refused(
    db_session: Session, tmp_path: Any, records: tuple[Project, Inventory]
) -> None:
    """Job ile planın inventory'si ayrışırsa yetkilendirme geçersizdir."""
    project, _ = records
    other = Inventory(
        name="Test",
        path=str(tmp_path / "proje" / "test.ini"),
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    db_session.add(other)
    db_session.commit()
    _seed(db_session, records, job_overrides={"inventory_id": other.id})

    assert _acquire(db_session).outcome is AcquireOutcome.BINDING_INVALID


@pytest.mark.parametrize(
    "job_overrides",
    [
        pytest.param({"playbook_path": "baska.yml"}, id="playbook"),
        pytest.param({"requested_by": "baska-aktor"}, id="actor"),
    ],
)
def test_job_field_mismatch_is_refused(
    db_session: Session,
    records: tuple[Project, Inventory],
    job_overrides: dict[str, Any],
) -> None:
    """Playbook ve aktör uyuşmazlıkları ayrı ayrı reddedilir."""
    job_id, _ = _seed(db_session, records, job_overrides=job_overrides)

    assert _acquire(db_session).outcome is AcquireOutcome.BINDING_INVALID
    assert _job(db_session, job_id).status is JobStatus.FAILED


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("workspace_id", "workspace-1", id="workspace-not-uuid"),
        pytest.param("workspace_id", str(uuid.uuid1()), id="workspace-wrong-version"),
        pytest.param("manifest_digest", "A" * 64, id="digest-uppercase"),
        pytest.param("manifest_digest", "a" * 63, id="digest-short"),
        pytest.param("manifest_digest", "z" * 64, id="digest-not-hex"),
    ],
)
def test_invalid_workspace_or_digest_is_refused(
    db_session: Session,
    migrated_engine: Engine,
    records: tuple[Project, Inventory],
    column: str,
    value: str,
) -> None:
    """Canonical olmayan ``workspace_id`` veya biçimsiz digest reddedilir."""
    job_id, plan_id = _seed(db_session, records)
    _raw_sql(
        migrated_engine,
        f"UPDATE execution_plans SET {column} = ? WHERE id = ?",
        (value, plan_id),
    )

    assert _acquire(db_session).outcome is AcquireOutcome.BINDING_INVALID
    assert _job(db_session, job_id).status is JobStatus.FAILED


def test_binding_invalid_job_is_closed_and_never_reappears(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Bağı geçersiz Job terminal ``failed`` olur; ``pending``/``running`` kalmaz.

    Kuyruğu tıkamaması da ölçülür: ikinci bir tur aynı satırı yeniden seçmez.
    Global aktif PLAYBOOK sınırı 1 olduğu için bozuk bir satırın ``pending``
    bırakılması bütün playbook execution'ını durdururdu.
    """
    job_id, _ = _seed(db_session, records, plan_status=ExecutionPlanStatus.PREPARED)

    result = _acquire(db_session)

    assert result.outcome is AcquireOutcome.BINDING_INVALID
    assert result.context is None
    job = _job(db_session, job_id)
    assert job.status is JobStatus.FAILED
    assert job.error_code == ERROR_EXECUTION_BINDING_INVALID
    assert job.finished_at is not None
    assert job.return_code is None
    assert job.artifact_path is None
    assert job.result_truncated is False
    assert job.worker_id is None
    assert job.heartbeat_at is None
    assert job.lease_expires_at is None
    # İkinci tur aynı satırı yeniden seçmez.
    assert _acquire(db_session).outcome is AcquireOutcome.IDLE


def test_expired_plan_ttl_alone_does_not_refuse_a_claimed_plan(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """TTL'si sonradan geçmiş **claimed** plan, Job'ı tek başına geçersiz yapmaz.

    TTL bir biletin ne kadar süre *claim edilebilir* kaldığını söyler. Bilet bir
    kez claim edilip Job üretmişse yetkilendirme çoktan gerçekleşmiştir; kuyrukta
    bekleyen işi sonradan geçen bir TTL yüzünden düşürmek, kullanıcının
    onayladığı her işi habersizce iptal ederdi.
    """
    past = datetime.now(UTC) - timedelta(hours=2)
    job_id, _ = _seed(
        db_session,
        records,
        created_at=past,
        expires_at=past + timedelta(seconds=1),
    )

    result = _acquire(db_session)

    assert result.outcome is AcquireOutcome.ACQUIRED
    assert _job(db_session, job_id).status is JobStatus.RUNNING


# --- Acquire: hata ve transaction hijyeni ------------------------------------


def test_commit_failure_rolls_back_and_reraises(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Commit arızası yutulmaz: rollback edilir, hata yeniden yükselir.

    Bir kısıt ihlali her zaman ``execute`` anında görünmez; yakalanmamış bir
    commit hatası session'ı çağırana kirli bırakır ve "alınmış görünen" bir
    Job'ın arkasında hiçbir sahiplik kalmazdı.
    """
    job_id, _ = _seed(db_session, records)
    attempts: list[int] = []
    real_commit = db_session.commit

    def _failing_commit() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("COMMIT", {}, Exception("disk I/O error"))
        real_commit()

    db_session.commit = _failing_commit  # type: ignore[method-assign]
    try:
        with pytest.raises(OperationalError):
            _acquire(db_session)
        assert not db_session.in_transaction()
        assert _snapshot(migrated_engine, job_id) == (JobStatus.PENDING, None, None, None, None)
    finally:
        del db_session.commit

    # Session kullanılabilir kaldı: aynı Job sonraki turda alınabilir.
    assert _acquire(db_session).outcome is AcquireOutcome.ACQUIRED


def test_binding_failure_commit_error_rolls_back_and_reraises(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Bağ kapatma commit'i düşerse Job ``pending`` kalır ve hata yükselir."""
    job_id, _ = _seed(db_session, records, plan_status=ExecutionPlanStatus.PREPARED)

    def _failing_commit() -> None:
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    db_session.commit = _failing_commit  # type: ignore[method-assign]
    try:
        with pytest.raises(OperationalError):
            _acquire(db_session)
        assert not db_session.in_transaction()
        assert _snapshot(migrated_engine, job_id) == (JobStatus.PENDING, None, None, None, None)
    finally:
        del db_session.commit


@contextmanager
def _failing_statements(engine: Engine, *, fragment: str) -> Iterator[None]:
    """Verilen parçayı içeren her SQL ifadesini gerçek execution sınırında düşürür.

    Enjeksiyon session veya servis katmanında değil, sürücüye giden ifadenin
    üzerinde yapılır: ölçülmek istenen şey "servis bir istisnayı nasıl
    yakalıyor" değil, **gerçek bir okuma arızasında** session'ın çağırana hangi
    durumda döndüğüdür. Listener çıkışta koşulsuz kaldırılır; engine testler
    arasında paylaşılmasa da açık kalan bir listener sonraki sorguları sessizce
    düşürürdü.
    """

    def _raise(_conn: Any, _cursor: Any, statement: str, *_args: Any, **_kwargs: Any) -> None:
        if fragment in statement:
            raise OperationalError(statement, {}, Exception("disk I/O error"))

    event.listen(engine, "before_cursor_execute", _raise)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", _raise)


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param("FROM jobs", id="candidate-select"),
        pytest.param("FROM execution_plans", id="plan-select"),
    ],
)
def test_read_failure_rolls_back_and_reraises(
    db_session: Session,
    migrated_engine: Engine,
    records: tuple[Project, Inventory],
    fragment: str,
) -> None:
    """Aday veya plan okuması düşerse session temiz ve satırlar değişmemiş kalır.

    Okuma da bir transaction açar. Yalnız yazma yolunu korumak, aday ``SELECT``
    veya plan ``SELECT`` düştüğünde çağırana **açık ve failed** bir transaction
    devrederdi: aynı session ile atılan bir sonraki her sorgu, asıl arızayla
    ilgisiz bir ``PendingRollbackError`` ile düşerdi.

    Arıza ayrıca bir sonuca **çevrilmez**: ``IDLE`` veya ``BINDING_INVALID``
    dönmek, disk arızasını boş bir kuyruk ya da bozuk bir yetkilendirme gibi
    gösterirdi. Bu ikinci iddia, sadece transaction'ı temizleyen bir düzeltmenin
    testi geçmesini engeller.
    """
    job_id, _ = _seed(db_session, records)
    before = _snapshot(migrated_engine, job_id)

    with _failing_statements(migrated_engine, fragment=fragment):
        with pytest.raises(OperationalError) as error:
            _acquire(db_session)
        assert not db_session.in_transaction()

    # Düşen ifade gerçekten hedeflenen `SELECT`'tir: plan okuması vakasında aday
    # `SELECT`'i başarıyla çalışmış, arıza ikinci okumada yüzeye çıkmıştır.
    assert fragment in str(error.value.statement)
    # Listener kaldırıldı: session hâlâ sorgu çalıştırabilir durumdadır.
    assert _job(db_session, job_id).status is JobStatus.PENDING
    assert _snapshot(migrated_engine, job_id) == before
    # Arıza geçtikten sonra aynı session aynı işi normal biçimde alır.
    assert _acquire(db_session).outcome is AcquireOutcome.ACQUIRED


@pytest.mark.parametrize("case", ["idle", "acquired", "binding_invalid"])
def test_acquire_leaves_no_open_transaction(
    db_session: Session, records: tuple[Project, Inventory], case: str
) -> None:
    """Hiçbir sonuç yolu çağırana açık transaction bırakmaz."""
    if case == "acquired":
        _seed(db_session, records)
    elif case == "binding_invalid":
        _seed(db_session, records, plan_status=ExecutionPlanStatus.PREPARED)
    db_session.commit()
    assert not db_session.in_transaction()

    _acquire(db_session)

    assert not db_session.in_transaction()


def test_context_carries_no_token_hash_or_absolute_path(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Context ne token özeti, ne fingerprint, ne de absolute path taşır."""
    job_id, plan_id = _seed(db_session, records)
    plan = db_session.execute(
        select(ExecutionPlanRecord).where(ExecutionPlanRecord.id == plan_id)
    ).scalar_one()
    secrets = {plan.token_hash, plan.input_fingerprint}

    context = _acquire(db_session).context

    assert context is not None
    assert {field.name for field in dataclasses.fields(context)} == {
        "job_id",
        "execution_plan_id",
        "workspace_id",
        "manifest_digest",
        "project_id",
        "inventory_id",
        "playbook_path",
        "requested_by",
        # Doğrulanmış execution mode (R1-V3H1B2A). Alan kümesi bir yasak listesi
        # değil **tam eşitliktir**: kipin eklenmesi bilinçli bir sözleşme
        # değişikliğidir ve yanına sessizce başka bir execution parametresi
        # (token, environment, path) giremez.
        "mode",
        "worker_id",
    }
    rendered = repr(context)
    for secret in secrets:
        assert secret not in rendered
    for value in dataclasses.asdict(context).values():
        assert not (isinstance(value, str) and value.startswith("/"))
    assert context.job_id == job_id


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"worker_id": "worker-1"}, id="worker-not-uuid"),
        pytest.param({"worker_id": str(uuid.uuid1())}, id="worker-wrong-version"),
        pytest.param({"worker_id": str(uuid.uuid4()).upper()}, id="worker-not-canonical"),
        pytest.param({"now": datetime.now()}, id="naive-now"),  # noqa: DTZ005
        pytest.param({"lease_seconds": 0.0}, id="lease-zero"),
        pytest.param({"lease_seconds": -1.0}, id="lease-negative"),
        pytest.param({"lease_seconds": math.nan}, id="lease-nan"),
        pytest.param({"lease_seconds": math.inf}, id="lease-infinite"),
        pytest.param({"lease_seconds": MAX_LEASE_SECONDS + 1}, id="lease-too-long"),
    ],
)
def test_invalid_acquire_input_is_refused_without_touching_the_database(
    db_session: Session,
    records: tuple[Project, Inventory],
    counted_statements: list[str],
    overrides: dict[str, Any],
) -> None:
    """Geçersiz worker, naive an veya geçersiz lease DB'ye dokunmadan reddedilir."""
    job_id, _ = _seed(db_session, records)
    counted_statements.clear()

    with pytest.raises(ValueError):
        _acquire(db_session, **overrides)

    assert counted_statements == []
    assert not db_session.in_transaction()
    assert _job(db_session, job_id).status is JobStatus.PENDING


def test_acquire_errors_do_not_leak_identifiers(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Girdi hataları reddedilen değeri mesaja yazmaz."""
    _seed(db_session, records)
    worker = "gizli-worker-kimligi"

    with pytest.raises(ValueError) as error:
        _acquire(db_session, worker_id=worker)

    assert worker not in str(error.value)


# --- Heartbeat ---------------------------------------------------------------


def _running(
    session: Session,
    records: tuple[Project, Inventory],
    *,
    worker_id: str,
    heartbeat_at: datetime,
    lease_expires_at: datetime,
) -> str:
    """Sahibi ve kirası belli bir ``running`` PLAYBOOK Job'ı yazar."""
    job_id, _ = _seed(
        session,
        records,
        job_overrides={
            "status": JobStatus.RUNNING,
            "worker_id": worker_id,
            "started_at": heartbeat_at,
            "heartbeat_at": heartbeat_at,
            "lease_expires_at": lease_expires_at,
        },
    )
    return job_id


def test_owner_extends_the_lease(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Doğru sahip, canlı kirayı tek koşullu UPDATE ile uzatır."""
    worker = str(uuid.uuid4())
    start = datetime.now(UTC) - timedelta(seconds=10)
    job_id = _running(
        db_session,
        records,
        worker_id=worker,
        heartbeat_at=start,
        lease_expires_at=start + timedelta(seconds=LEASE),
    )
    moment = datetime.now(UTC)

    renewed = heartbeat_playbook_job(
        db_session, job_id=job_id, worker_id=worker, lease_seconds=LEASE, now=moment
    )

    assert renewed is True
    assert _snapshot(migrated_engine, job_id) == (
        JobStatus.RUNNING,
        worker,
        _naive(start),
        _naive(moment),
        _naive(moment + timedelta(seconds=LEASE)),
    )
    assert not db_session.in_transaction()


def test_a_different_worker_cannot_renew(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Yanlış worker kirayı yenileyemez ve satırı değiştiremez."""
    owner = str(uuid.uuid4())
    start = datetime.now(UTC) - timedelta(seconds=10)
    lease_end = start + timedelta(seconds=LEASE)
    job_id = _running(
        db_session, records, worker_id=owner, heartbeat_at=start, lease_expires_at=lease_end
    )
    before = _snapshot(migrated_engine, job_id)

    renewed = heartbeat_playbook_job(
        db_session, job_id=job_id, worker_id=str(uuid.uuid4()), lease_seconds=LEASE
    )

    assert renewed is False
    assert _snapshot(migrated_engine, job_id) == before
    assert not db_session.in_transaction()


@pytest.mark.parametrize("offset", [-1.0, 0.0])
def test_expired_and_boundary_equal_leases_cannot_be_revived(
    db_session: Session,
    migrated_engine: Engine,
    records: tuple[Project, Inventory],
    offset: float,
) -> None:
    """Dolmuş kira da, tam sınırdaki kira da canlandırılamaz.

    Sınır kesin olmasaydı ``lease_expires_at == now`` taşıyan bir satır hem
    stale recovery tarafından devralınabilir hem de eski sahibi tarafından
    yenilenebilir olurdu; aynı iş iki worker'a ait sayılırdı.
    """
    worker = str(uuid.uuid4())
    moment = datetime.now(UTC)
    start = moment - timedelta(seconds=60)
    job_id = _running(
        db_session,
        records,
        worker_id=worker,
        heartbeat_at=start,
        lease_expires_at=moment + timedelta(seconds=offset),
    )
    before = _snapshot(migrated_engine, job_id)

    renewed = heartbeat_playbook_job(
        db_session, job_id=job_id, worker_id=worker, lease_seconds=LEASE, now=moment
    )

    assert renewed is False
    assert _snapshot(migrated_engine, job_id) == before


@pytest.mark.parametrize(
    "status",
    [JobStatus.PENDING, JobStatus.SUCCESSFUL, JobStatus.FAILED, JobStatus.CANCELED],
)
def test_pending_and_terminal_jobs_cannot_be_renewed(
    db_session: Session, records: tuple[Project, Inventory], status: JobStatus
) -> None:
    """Sahibi olmayan (``pending``) ve artık kimseye ait olmayan Job yenilenemez."""
    job_id, _ = _seed(db_session, records, job_overrides={"status": status})

    renewed = heartbeat_playbook_job(
        db_session, job_id=job_id, worker_id=str(uuid.uuid4()), lease_seconds=LEASE
    )

    assert renewed is False
    assert not db_session.in_transaction()


def test_ping_job_cannot_be_renewed(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Ping'in worker'ı ve kirası yoktur; heartbeat onu hiç görmez."""
    project, inventory = records
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

    assert (
        heartbeat_playbook_job(
            db_session, job_id=job_id, worker_id=str(uuid.uuid4()), lease_seconds=LEASE
        )
        is False
    )


def test_unknown_job_is_a_plain_no_op(db_session: Session) -> None:
    """Bilinmeyen Job açık bir ``False``'tur; hata değildir."""
    assert (
        heartbeat_playbook_job(
            db_session,
            job_id=str(uuid.uuid4()),
            worker_id=str(uuid.uuid4()),
            lease_seconds=LEASE,
        )
        is False
    )
    assert not db_session.in_transaction()


def test_heartbeat_commit_failure_rolls_back_and_reraises(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Yenileme commit'i düşerse kira uzamaz, hata yutulmaz."""
    worker = str(uuid.uuid4())
    start = datetime.now(UTC) - timedelta(seconds=10)
    job_id = _running(
        db_session,
        records,
        worker_id=worker,
        heartbeat_at=start,
        lease_expires_at=start + timedelta(seconds=LEASE),
    )
    before = _snapshot(migrated_engine, job_id)

    def _failing_commit() -> None:
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    db_session.commit = _failing_commit  # type: ignore[method-assign]
    try:
        with pytest.raises(OperationalError):
            heartbeat_playbook_job(db_session, job_id=job_id, worker_id=worker, lease_seconds=LEASE)
        assert not db_session.in_transaction()
        assert _snapshot(migrated_engine, job_id) == before
    finally:
        del db_session.commit

    # Session kullanılabilir kaldı.
    assert (
        heartbeat_playbook_job(db_session, job_id=job_id, worker_id=worker, lease_seconds=LEASE)
        is True
    )


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"job_id": "job-1"}, id="job-not-uuid"),
        pytest.param({"worker_id": "worker-1"}, id="worker-not-uuid"),
        pytest.param({"now": datetime.now()}, id="naive-now"),  # noqa: DTZ005
        pytest.param({"lease_seconds": 0.0}, id="lease-zero"),
        pytest.param({"lease_seconds": math.nan}, id="lease-nan"),
        pytest.param({"lease_seconds": MAX_LEASE_SECONDS + 1}, id="lease-too-long"),
    ],
)
def test_invalid_heartbeat_input_is_refused_without_touching_the_database(
    db_session: Session,
    records: tuple[Project, Inventory],
    counted_statements: list[str],
    overrides: dict[str, Any],
) -> None:
    """Geçersiz girdiler DB'ye dokunmadan reddedilir."""
    worker = str(uuid.uuid4())
    start = datetime.now(UTC)
    job_id = _running(
        db_session,
        records,
        worker_id=worker,
        heartbeat_at=start,
        lease_expires_at=start + timedelta(seconds=LEASE),
    )
    counted_statements.clear()
    arguments: dict[str, Any] = {"job_id": job_id, "worker_id": worker, "lease_seconds": LEASE}
    arguments.update(overrides)

    with pytest.raises(ValueError):
        heartbeat_playbook_job(db_session, **arguments)

    assert counted_statements == []
    assert not db_session.in_transaction()


# --- Finish ------------------------------------------------------------------


def _artifact(job_id: str) -> str:
    """Bir Job'ın yayımlanmış sonucunun **tek** geçerli göreli konumu."""
    return f"jobs/{job_id}/result.json"


def _finish(session: Session, job_id: str, worker_id: str, /, **overrides: Any) -> bool:
    """Varsayılanı **geçerli bir başarı** olan finish çağrısı.

    Her ret testi yalnızca tek bir alanı bozar; böylece reddin sebebi tekildir.
    ``job_id``/``worker_id`` positional-only'dır: geçersiz kimlik testleri onları
    da ``overrides`` üzerinden değiştirebilmelidir.
    """
    arguments: dict[str, Any] = {
        "job_id": job_id,
        "worker_id": worker_id,
        "status": JobStatus.SUCCESSFUL,
        "return_code": 0,
        "error_code": None,
        "artifact_path": _artifact(job_id),
        "result_truncated": False,
    }
    arguments.update(overrides)
    return finish_playbook_job(session, **arguments)


def _failure_arguments(job_id: str) -> dict[str, Any]:
    """Varsayılan **geçerli bir başarısızlık** sonucu."""
    return {
        "status": JobStatus.FAILED,
        "return_code": 2,
        "error_code": "runner_failed",
        "artifact_path": _artifact(job_id),
        "result_truncated": False,
    }


def _result(engine: Engine, job_id: str) -> tuple[Any, ...]:
    """Bağımsız bir bağlantıdan **commit edilmiş** sonuç ve kira alanları."""
    with Session(engine) as observer:
        row = observer.execute(
            select(
                Job.status,
                Job.return_code,
                Job.error_code,
                Job.artifact_path,
                Job.result_truncated,
                Job.finished_at,
                Job.worker_id,
                Job.heartbeat_at,
                Job.lease_expires_at,
            ).where(Job.id == job_id)
        ).one()
    return tuple(row)


@pytest.fixture
def owned(db_session: Session, records: tuple[Project, Inventory]) -> tuple[str, str, datetime]:
    """Sahibi ve kirası belli, çalışan bir PLAYBOOK Job'ı.

    ``(job_id, worker_id, started_at)`` döner.
    """
    worker = str(uuid.uuid4())
    start = datetime.now(UTC) - timedelta(seconds=10)
    job_id = _running(
        db_session,
        records,
        worker_id=worker,
        heartbeat_at=start,
        lease_expires_at=start + timedelta(seconds=LEASE),
    )
    return job_id, worker, start


def test_owner_writes_a_successful_result_and_clears_the_lease(
    db_session: Session, migrated_engine: Engine, owned: tuple[str, str, datetime]
) -> None:
    """Başarılı sonucun bütün alanları ve kira temizliği **tek** geçişte yazılır.

    Ölçüm dolaylı değildir: bağımsız bir bağlantı, commit çağrılmadan hemen önce
    hâlâ sahipli ve ``running`` bir satır görür. "Önce terminal yap, sonra kirayı
    boşalt" gibi iki adımlı bir uygulama bu ölçümü geçemezdi — böyle bir satırı
    ``ck_jobs_idle_playbook_has_no_lease`` zaten reddeder.
    """
    job_id, worker, start = owned
    moment = datetime.now(UTC)
    observed: list[tuple[Any, ...]] = []
    real_commit = db_session.commit

    def _observing_commit() -> None:
        observed.append(_snapshot(migrated_engine, job_id))
        real_commit()

    db_session.commit = _observing_commit  # type: ignore[method-assign]
    try:
        finished = _finish(db_session, job_id, worker, now=moment)
    finally:
        del db_session.commit

    assert finished is True
    assert observed == [
        (
            JobStatus.RUNNING,
            worker,
            _naive(start),
            _naive(start),
            _naive(start + timedelta(seconds=LEASE)),
        )
    ], "tek commit, tek geçiş"
    assert _result(migrated_engine, job_id) == (
        JobStatus.SUCCESSFUL,
        0,
        None,
        _artifact(job_id),
        False,
        _naive(moment),
        None,
        None,
        None,
    )
    assert not db_session.in_transaction()


def test_owner_writes_a_failed_result(
    db_session: Session, migrated_engine: Engine, owned: tuple[str, str, datetime]
) -> None:
    """Başarısız sonuç, hata kodu ve kırpılma göstergesiyle birlikte kaydedilir."""
    job_id, worker, _ = owned
    moment = datetime.now(UTC)

    finished = _finish(
        db_session,
        job_id,
        worker,
        status=JobStatus.FAILED,
        return_code=None,
        error_code="runner_timeout",
        artifact_path=None,
        result_truncated=True,
        now=moment,
    )

    assert finished is True
    assert _result(migrated_engine, job_id) == (
        JobStatus.FAILED,
        None,
        "runner_timeout",
        None,
        True,
        _naive(moment),
        None,
        None,
        None,
    )
    assert not db_session.in_transaction()


@pytest.mark.parametrize("error_code", sorted(FINISH_ERROR_CODES))
def test_every_allowed_error_code_is_accepted(
    db_session: Session,
    migrated_engine: Engine,
    owned: tuple[str, str, datetime],
    error_code: str,
) -> None:
    """Allowlist'teki her kod gerçekten yazılabilir; liste ölü bir sabit değildir."""
    job_id, worker, _ = owned

    assert _finish(
        db_session,
        job_id,
        worker,
        **{**_failure_arguments(job_id), "error_code": error_code},
    )
    assert _result(migrated_engine, job_id)[2] == error_code


def test_a_different_worker_cannot_write_a_result(
    db_session: Session, migrated_engine: Engine, owned: tuple[str, str, datetime]
) -> None:
    """Yanlış sahip sonuç yazamaz ve satırı hiç değiştirmez.

    Bu, stale recovery devraldıktan sonra geri dönen eski worker'ın senaryosudur:
    onun yazacağı sonuç artık başka birinin çalıştırdığı işe aittir.
    """
    job_id, _, _ = owned
    before = _result(migrated_engine, job_id)

    finished = _finish(db_session, job_id, str(uuid.uuid4()))

    assert finished is False
    assert _result(migrated_engine, job_id) == before
    assert not db_session.in_transaction()


@pytest.mark.parametrize(
    "status",
    [JobStatus.PENDING, JobStatus.SUCCESSFUL, JobStatus.FAILED, JobStatus.CANCELED],
)
def test_pending_and_terminal_jobs_cannot_be_finished(
    db_session: Session,
    migrated_engine: Engine,
    records: tuple[Project, Inventory],
    status: JobStatus,
) -> None:
    """Hiç başlamamış ve zaten bitmiş Job'a sonuç yazılamaz.

    ``pending`` bir satırın sahibi yoktur; terminal bir satırınki ise artık
    kimse değildir. İkisinde de ``worker_id IS NULL`` olduğu için koşul zaten
    tutmaz, ama ölçüm ``status`` koşulunun da yerinde olduğunu gösterir.
    """
    job_id, _ = _seed(db_session, records, job_overrides={"status": status})
    before = _result(migrated_engine, job_id)

    finished = _finish(db_session, job_id, str(uuid.uuid4()))

    assert finished is False
    assert _result(migrated_engine, job_id) == before
    assert not db_session.in_transaction()


def test_ping_job_cannot_be_finished_through_this_path(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Ping'in kendi terminal yolu vardır; bu fonksiyon onu hiç görmez."""
    project, inventory = records
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

    assert _finish(db_session, job_id, str(uuid.uuid4())) is False


def test_unknown_job_finish_is_a_plain_no_op(db_session: Session) -> None:
    """Bilinmeyen Job açık bir ``False``'tur; hata değildir."""
    job_id = str(uuid.uuid4())

    assert _finish(db_session, job_id, str(uuid.uuid4())) is False
    assert not db_session.in_transaction()


def test_a_second_finish_never_overwrites_the_first_result(
    db_session: Session, migrated_engine: Engine, owned: tuple[str, str, datetime]
) -> None:
    """İkinci sonuç yazımı birinciyi değiştirmez.

    Kritik olan yalnız ``False`` dönmesi değil, satırın **birebir** aynı
    kalmasıdır: worker'ın `finally` yolu ile normal sonuç yolu aynı Job için iki
    kez çağrılabilir ve ikincisi başarılı bir çalıştırmayı `failed` diye yeniden
    yazamamalıdır.
    """
    job_id, worker, _ = owned
    assert _finish(db_session, job_id, worker) is True
    after_first = _result(migrated_engine, job_id)

    second = _finish(db_session, job_id, worker, **_failure_arguments(job_id))

    assert second is False
    assert _result(migrated_engine, job_id) == after_first
    assert not db_session.in_transaction()


def test_lost_race_writes_no_result(
    db_session: Session,
    migrated_engine: Engine,
    owned: tuple[str, str, datetime],
) -> None:
    """Satır arada başka bir el tarafından terminalize edilmişse sonuç yazılmaz.

    Yarış, ``UPDATE``'in koşulunun tutmadığı gerçek bir satır üzerinde ölçülür:
    stale recovery'nin devraldığı ve kapattığı bir Job, eski sahibinin sonucunu
    artık kabul etmemelidir.
    """
    job_id, worker, _ = owned
    with Session(migrated_engine) as thief:
        thief.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.FAILED,
                error_code="runner_timeout",
                finished_at=datetime.now(UTC),
                worker_id=None,
                heartbeat_at=None,
                lease_expires_at=None,
            )
        )
        thief.commit()
    before = _result(migrated_engine, job_id)

    assert _finish(db_session, job_id, worker) is False
    assert _result(migrated_engine, job_id) == before


# --- Finish: sonuç sözleşmesi -------------------------------------------------


@pytest.mark.parametrize(
    "make_overrides",
    [
        pytest.param(lambda job_id: {"return_code": 1}, id="nonzero-rc"),
        pytest.param(lambda job_id: {"return_code": None}, id="missing-rc"),
        pytest.param(lambda job_id: {"error_code": "runner_failed"}, id="error-code"),
        pytest.param(lambda job_id: {"artifact_path": None}, id="missing-artifact"),
        pytest.param(lambda job_id: {"result_truncated": True}, id="truncated"),
    ],
)
def test_successful_result_invariant_is_enforced(
    db_session: Session,
    migrated_engine: Engine,
    owned: tuple[str, str, datetime],
    counted_statements: list[str],
    make_overrides: Any,
) -> None:
    """``successful`` kendisiyle çelişen hiçbir sonuç alanı taşıyamaz.

    Sıfır olmayan bir ``return_code``, bir hata kodu, eksik bir artifact veya
    kırpılmış bir sonuç taşıyan "başarı", kaydın kendisini okunamaz kılardı:
    kullanıcı ekranda başarı görürken kayıt bir başarısızlığı anlatırdı.
    """
    job_id, worker, _ = owned
    before = _result(migrated_engine, job_id)
    counted_statements.clear()

    with pytest.raises(ValueError):
        _finish(db_session, job_id, worker, **make_overrides(job_id))

    assert counted_statements == []
    assert _result(migrated_engine, job_id) == before
    assert not db_session.in_transaction()


@pytest.mark.parametrize(
    "error_code",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("boom", id="free-text"),
        pytest.param("RUNNER_FAILED", id="wrong-case"),
        pytest.param("runner_failed ", id="trailing-space"),
        # Bu kod bir çalıştırma sonucu değildir; yalnız acquire yolu yazar.
        pytest.param(ERROR_EXECUTION_BINDING_INVALID, id="binding-invalid"),
        # Ping akışının kodu buraya sızmamalıdır.
        pytest.param("interrupted_by_restart", id="other-domain"),
    ],
)
def test_failed_result_requires_a_known_error_code(
    db_session: Session,
    migrated_engine: Engine,
    owned: tuple[str, str, datetime],
    counted_statements: list[str],
    error_code: str | None,
) -> None:
    """Başarısızlık yalnız sabit sözlükteki bir kodla kaydedilebilir.

    Serbest metin kabul edilseydi, hata mesajı olarak yazılmış tek bir workspace
    yolu veya komut satırı sonucun okunduğu her yerde görünürdü.
    """
    job_id, worker, _ = owned
    before = _result(migrated_engine, job_id)
    counted_statements.clear()

    with pytest.raises(ValueError):
        _finish(
            db_session,
            job_id,
            worker,
            **{**_failure_arguments(job_id), "error_code": error_code},
        )

    assert counted_statements == []
    assert _result(migrated_engine, job_id) == before
    assert not db_session.in_transaction()


@pytest.mark.parametrize(
    "make_path",
    [
        pytest.param(lambda job_id: f"/var/lib/app-data/jobs/{job_id}/result.json", id="absolute"),
        pytest.param(lambda job_id: f"jobs/{job_id}/../{job_id}/result.json", id="traversal"),
        pytest.param(lambda job_id: f"jobs/../jobs/{job_id}/result.json", id="traversal-prefix"),
        pytest.param(lambda job_id: f"jobs/{str(uuid.uuid4())}/result.json", id="other-job"),
        pytest.param(lambda job_id: f"jobs/{job_id}/stdout.log", id="other-file"),
        pytest.param(lambda job_id: f"jobs/{job_id}", id="directory-only"),
        pytest.param(lambda job_id: f"jobs/{job_id}/result.json/", id="trailing-slash"),
        pytest.param(lambda job_id: f"./jobs/{job_id}/result.json", id="dot-prefix"),
    ],
)
@pytest.mark.parametrize("outcome", ["successful", "failed"])
def test_artifact_path_must_be_this_jobs_published_result(
    db_session: Session,
    migrated_engine: Engine,
    owned: tuple[str, str, datetime],
    counted_statements: list[str],
    make_path: Any,
    outcome: str,
) -> None:
    """Absolute, traversal, başka Job veya başka dosya adı reddedilir.

    Ret her iki terminal durumda da geçerlidir: başka bir Job'ın sonucunu
    gösteren bir kayıt, iki çalıştırmanın kanıtını birbirine karıştırırdı ve
    kök dışına çıkan bir path'i sonradan okuyan her katman kendi kontrolünü
    yeniden yapmak zorunda kalırdı.
    """
    job_id, worker, _ = owned
    overrides: dict[str, Any] = {"artifact_path": make_path(job_id)}
    if outcome == "failed":
        overrides = {**_failure_arguments(job_id), **overrides}
    before = _result(migrated_engine, job_id)
    counted_statements.clear()

    with pytest.raises(ValueError):
        _finish(db_session, job_id, worker, **overrides)

    assert counted_statements == []
    assert _result(migrated_engine, job_id) == before
    assert not db_session.in_transaction()


def test_failed_result_may_omit_the_artifact(
    db_session: Session, migrated_engine: Engine, owned: tuple[str, str, datetime]
) -> None:
    """Sonuç yazılamamışsa ``artifact_path`` boş kalır; sahte kanıt üretilmez."""
    job_id, worker, _ = owned

    assert _finish(
        db_session,
        job_id,
        worker,
        **{**_failure_arguments(job_id), "artifact_path": None},
    )
    assert _result(migrated_engine, job_id)[3] is None


@pytest.mark.parametrize(
    "make_overrides",
    [
        pytest.param(lambda job_id: {"job_id": "job-1"}, id="job-not-uuid"),
        pytest.param(lambda job_id: {"job_id": str(uuid.uuid1())}, id="job-wrong-version"),
        pytest.param(lambda job_id: {"job_id": job_id.upper()}, id="job-not-canonical"),
        pytest.param(lambda job_id: {"worker_id": "worker-1"}, id="worker-not-uuid"),
        pytest.param(lambda job_id: {"status": JobStatus.RUNNING}, id="status-running"),
        pytest.param(lambda job_id: {"status": JobStatus.PENDING}, id="status-pending"),
        # `canceled` terminaldir ama bir *çalıştırma sonucu* değildir.
        pytest.param(lambda job_id: {"status": JobStatus.CANCELED}, id="status-canceled"),
        # `JobStatus` bir `StrEnum`'dur: ham dizgi üyeye **eşittir** ve yalnız
        # küme üyeliğine bakan bir kontrolü geçerdi. Durum tip sisteminden
        # gelmeli, çağıranın elindeki serbest metinden değil.
        #
        # Diğer alanlar bilinçle **geçerli bir başarısızlık** olarak verilir:
        # tür kontrolü olmadan bu çağrılar sonuç invariantına da takılmaz ve
        # ham dizgi doğrudan `UPDATE`'e giderdi. Böylece reddin tek olası
        # sebebi tür kontrolüdür.
        pytest.param(
            lambda job_id: {**_failure_arguments(job_id), "status": "successful"},
            id="status-raw-successful",
        ),
        pytest.param(
            lambda job_id: {**_failure_arguments(job_id), "status": "failed"},
            id="status-raw-failed",
        ),
        pytest.param(lambda job_id: {"now": datetime.now()}, id="naive-now"),  # noqa: DTZ005
        # `bool`, `int` alt sınıfıdır: `True` sessizce 1, `False` sessizce 0
        # olarak yazılırdı ve ikincisi başarısızlığı başarı gibi gösterirdi.
        pytest.param(
            lambda job_id: {**_failure_arguments(job_id), "return_code": True},
            id="rc-bool-true",
        ),
        pytest.param(
            lambda job_id: {**_failure_arguments(job_id), "return_code": False},
            id="rc-bool-false",
        ),
        pytest.param(lambda job_id: {"return_code": False}, id="success-rc-bool-false"),
        pytest.param(
            lambda job_id: {**_failure_arguments(job_id), "return_code": "2"},
            id="rc-string",
        ),
        pytest.param(
            lambda job_id: {**_failure_arguments(job_id), "result_truncated": 1},
            id="truncated-int",
        ),
        pytest.param(
            lambda job_id: {**_failure_arguments(job_id), "result_truncated": None},
            id="truncated-none",
        ),
    ],
)
def test_invalid_finish_input_is_refused_without_touching_the_database(
    db_session: Session,
    migrated_engine: Engine,
    owned: tuple[str, str, datetime],
    counted_statements: list[str],
    make_overrides: Any,
) -> None:
    """Geçersiz kimlik, durum, an veya tür DB'ye **hiç** dokunmadan reddedilir."""
    job_id, worker, _ = owned
    before = _result(migrated_engine, job_id)
    counted_statements.clear()

    with pytest.raises(ValueError):
        _finish(db_session, job_id, worker, **make_overrides(job_id))

    assert counted_statements == []
    assert _result(migrated_engine, job_id) == before
    assert not db_session.in_transaction()


def test_finish_errors_do_not_leak_identifiers(
    db_session: Session, owned: tuple[str, str, datetime]
) -> None:
    """Girdi hataları reddedilen değeri mesaja yazmaz."""
    job_id, worker, _ = owned
    secret = f"/srv/gizli-kok/jobs/{job_id}/result.json"

    with pytest.raises(ValueError) as error:
        _finish(db_session, job_id, worker, artifact_path=secret)

    assert secret not in str(error.value)
    assert "gizli-kok" not in str(error.value)


def test_finish_update_failure_rolls_back_and_reraises(
    db_session: Session, migrated_engine: Engine, owned: tuple[str, str, datetime]
) -> None:
    """``UPDATE`` arızası yutulmaz: rollback edilir, hata yeniden yükselir.

    Arıza ayrıca bir sonuca **çevrilmez**: ``False`` dönmek, disk arızasını
    "sahipliğini kaybettin" gibi gösterirdi ve worker sonucu sessizce kaybederdi.
    """
    job_id, worker, _ = owned
    before = _result(migrated_engine, job_id)

    with _failing_statements(migrated_engine, fragment="UPDATE jobs"):
        with pytest.raises(OperationalError):
            _finish(db_session, job_id, worker)
        assert not db_session.in_transaction()

    assert _result(migrated_engine, job_id) == before
    # Arıza geçtikten sonra aynı session aynı sonucu normal biçimde yazar.
    assert _finish(db_session, job_id, worker) is True


def test_finish_commit_failure_rolls_back_and_reraises(
    db_session: Session, migrated_engine: Engine, owned: tuple[str, str, datetime]
) -> None:
    """Commit arızasında satır ``running`` kalır ve session kullanılabilir olur."""
    job_id, worker, _ = owned
    before = _result(migrated_engine, job_id)
    attempts: list[int] = []
    real_commit = db_session.commit

    def _failing_commit() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("COMMIT", {}, Exception("disk I/O error"))
        real_commit()

    db_session.commit = _failing_commit  # type: ignore[method-assign]
    try:
        with pytest.raises(OperationalError):
            _finish(db_session, job_id, worker)
        assert not db_session.in_transaction()
        assert _result(migrated_engine, job_id) == before
    finally:
        del db_session.commit

    assert _finish(db_session, job_id, worker) is True


@pytest.mark.parametrize("case", ["finished", "no-op", "invalid"])
def test_finish_leaves_no_open_transaction(
    db_session: Session, owned: tuple[str, str, datetime], case: str
) -> None:
    """Başarı, no-op ve hata yollarının hiçbiri açık transaction bırakmaz."""
    job_id, worker, _ = owned
    assert not db_session.in_transaction()

    if case == "finished":
        assert _finish(db_session, job_id, worker) is True
    elif case == "no-op":
        assert _finish(db_session, job_id, str(uuid.uuid4())) is False
    else:
        with pytest.raises(ValueError):
            _finish(db_session, job_id, worker, return_code=7)

    assert not db_session.in_transaction()


# --- Startup reconciliation --------------------------------------------------


def _stale(
    session: Session,
    records: tuple[Project, Inventory],
    *,
    lease_expires_at: datetime,
    worker_id: str | None = None,
    started_at: datetime | None = None,
) -> tuple[str, str, datetime]:
    """Kirası verilen anda dolan, sahibi belli bir ``running`` PLAYBOOK Job'ı.

    ``(job_id, worker_id, started_at)`` döner. ``heartbeat_at`` bilinçli olarak
    kiranın gerisinde tutulur: ``ck_jobs_running_playbook_lease_outlives_heartbeat``
    aksini zaten reddeder ve gerçek bir worker'ın bıraktığı satır da böyledir.
    """
    worker = worker_id or str(uuid.uuid4())
    start = started_at or (lease_expires_at - timedelta(seconds=2 * LEASE))
    job_id = _running(
        session,
        records,
        worker_id=worker,
        heartbeat_at=start,
        lease_expires_at=lease_expires_at,
    )
    return job_id, worker, start


@pytest.mark.parametrize(
    "offset",
    [
        pytest.param(-1.0, id="lease-already-expired"),
        pytest.param(0.0, id="lease-exactly-at-the-boundary"),
    ],
)
def test_expired_and_boundary_leases_are_closed_as_interrupted(
    db_session: Session,
    migrated_engine: Engine,
    records: tuple[Project, Inventory],
    offset: float,
) -> None:
    """Kirası dolmuş — sınırdakiler dâhil — ``running`` satır terminal olur.

    Sınır kesindir: ``lease_expires_at == now`` stale sayılır. Bu,
    :func:`heartbeat_playbook_job`'ın aynı sınırda yenilemeyi reddeden
    sözleşmesinin diğer yarısıdır; iki taraf farklı davransaydı tam o anda duran
    bir satır ya iki worker'a birden ait olur ya da hiç kapatılamazdı.
    """
    moment = datetime.now(UTC)
    job_id, _, _ = _stale(db_session, records, lease_expires_at=moment + timedelta(seconds=offset))

    assert reconcile_stale_playbook_jobs(db_session, now=moment) == 1

    assert _result(migrated_engine, job_id) == (
        JobStatus.FAILED,
        None,
        ERROR_INTERRUPTED_BY_RESTART,
        None,
        False,
        _naive(moment),
        None,
        None,
        None,
    )
    assert not db_session.in_transaction()


def test_started_at_survives_while_ownership_and_lease_are_cleared(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Çalıştırmanın başladığı an korunur; sahiplik ve kira alanları boşalır.

    ``started_at`` bir sonuç değil, olmuş bir olayın kaydıdır: kesilme onu
    geçersiz kılmaz ve silmek "bu iş hiç başlamadı" demek olurdu. Sahiplik
    alanları ise aynı geçişte boşaltılmak zorundadır —
    ``ck_jobs_idle_playbook_has_no_lease`` terminal bir satırda duran kirayı
    zaten reddeder.
    """
    moment = datetime.now(UTC)
    start = moment - timedelta(minutes=5)
    job_id, _, _ = _stale(
        db_session,
        records,
        lease_expires_at=moment - timedelta(seconds=1),
        started_at=start,
    )

    assert reconcile_stale_playbook_jobs(db_session, now=moment) == 1

    assert _snapshot(migrated_engine, job_id) == (
        JobStatus.FAILED,
        None,
        _naive(start),
        None,
        None,
    )


def test_every_expired_row_is_closed_in_a_single_call(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Birden çok stale satır tek çağrıda kapanır ve sayı gerçek satır sayısıdır.

    Global aktif PLAYBOOK sınırı 1 olduğu için ikinci bir ``running`` satır
    üretilebilmesi adına ``uq_jobs_active_playbook_global`` **yalnız bu testin
    veritabanında** düşürülür. Toplu davranış gene de gerçek bir sözleşmedir:
    uzlaştırma "ilk stale satırı kapat" değildir ve sınır ileride gevşetilirse
    geride kapatılmamış bir satır bırakmamalıdır.
    """
    moment = datetime.now(UTC)
    _raw_sql(migrated_engine, f"DROP INDEX {ACTIVE_PLAYBOOK_INDEX}")
    first, _, _ = _stale(db_session, records, lease_expires_at=moment - timedelta(seconds=1))
    second, _, _ = _stale(db_session, records, lease_expires_at=moment - timedelta(hours=3))

    assert reconcile_stale_playbook_jobs(db_session, now=moment) == 2

    for job_id in (first, second):
        assert _result(migrated_engine, job_id)[:3] == (
            JobStatus.FAILED,
            None,
            ERROR_INTERRUPTED_BY_RESTART,
        )


def test_a_live_lease_is_never_touched(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Kirası gelecekte olan satır, sahibi kim olursa olsun korunur.

    Açılıştaki yeni worker'ın kimliğinin satırdakinden farklı olması **stale
    kanıtı değildir**: aynı veritabanına bakan ikinci bir canlı backend süreci
    olabilir. Tek yetki kira süresidir.
    """
    moment = datetime.now(UTC)
    worker = str(uuid.uuid4())
    job_id, _, start = _stale(
        db_session,
        records,
        lease_expires_at=moment + timedelta(seconds=1),
        worker_id=worker,
    )
    before = _snapshot(migrated_engine, job_id)

    assert reconcile_stale_playbook_jobs(db_session, now=moment) == 0

    assert _snapshot(migrated_engine, job_id) == before
    assert before[0] is JobStatus.RUNNING
    assert before[1] == worker
    assert before[2] == _naive(start)
    assert not db_session.in_transaction()


def test_pending_jobs_are_never_reconciled(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """``pending`` bir Job henüz başlamamıştır; uzlaştırma onu kapatmaz.

    Korumayı burada asıl taşıyan şey veritabanı invariantıdır:
    ``ck_jobs_idle_playbook_has_no_lease`` yüzünden ``pending`` bir satırın
    ``lease_expires_at``'i ``NULL``'dur ve kira koşulu onu zaten dışarıda
    bırakır. ``UPDATE``'teki ``status`` koşulu bu invariantın ikinci
    savunmasıdır; ikisi birden gevşemedikçe satır kapanmaz.
    """
    job_id, _ = _seed(db_session, records)

    assert reconcile_stale_playbook_jobs(db_session, now=datetime.now(UTC)) == 0

    assert _snapshot(migrated_engine, job_id) == (JobStatus.PENDING, None, None, None, None)


@pytest.mark.parametrize("status", [JobStatus.SUCCESSFUL, JobStatus.FAILED, JobStatus.CANCELED])
def test_terminal_jobs_are_never_rewritten(
    db_session: Session,
    migrated_engine: Engine,
    records: tuple[Project, Inventory],
    status: JobStatus,
) -> None:
    """Terminal bir satırın sonucu uzlaştırma tarafından ezilmez.

    Ezilseydi başarıyla biten bir çalıştırma, sonraki her açılışta "restart
    kesti" diye yeniden yazılır ve gerçek sonuç kaybolurdu. ``pending``
    satırlarda olduğu gibi, terminal satırların kirası da ``NULL``'dur; ``status``
    koşulu o invariantın ikinci savunmasıdır.
    """
    moment = datetime.now(UTC)
    job_id, _ = _seed(
        db_session,
        records,
        job_overrides={
            "status": status,
            "started_at": moment - timedelta(minutes=1),
            "finished_at": moment - timedelta(seconds=30),
            "return_code": 0,
        },
    )
    before = _result(migrated_engine, job_id)

    assert reconcile_stale_playbook_jobs(db_session, now=moment) == 0

    assert _result(migrated_engine, job_id) == before


def test_ping_jobs_are_never_reconciled(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """PING'in kendi sahiplik modeli vardır; bu yol ona dokunmaz.

    Ping satırlarının kirası **yoktur** (``ck_jobs_ping_has_no_lease``); onları
    burada kapatmak, iki farklı yaşam döngüsünü tek kurala bağlamak olurdu.
    Kirasız bir satır kira koşuluna zaten takılmaz — kirası olan bir ping satırı
    ise veritabanı tarafından hiç kabul edilmez, dolayısıyla test edilebilir bir
    hâli yoktur. ``UPDATE``'teki ``job_type`` koşulu bu invariantın ikinci
    savunmasıdır.
    """
    project, inventory = records
    job_id = str(uuid.uuid4())
    started = datetime.now(UTC) - timedelta(hours=2)
    db_session.add(
        Job(
            id=job_id,
            job_type=JobType.PING,
            status=JobStatus.RUNNING,
            project_id=project.id,
            inventory_id=inventory.id,
            requested_by=ACTOR,
            started_at=started,
        )
    )
    db_session.commit()

    assert reconcile_stale_playbook_jobs(db_session, now=datetime.now(UTC)) == 0

    assert _snapshot(migrated_engine, job_id) == (
        JobStatus.RUNNING,
        None,
        _naive(started),
        None,
        None,
    )


def test_a_second_call_is_idempotent(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Kapatılan satır ikinci çağrıda yeniden ele alınmaz ve değişmez.

    Satır ``pending``'e döndürülmediği için kuyruğa da geri düşmez: onaylanmış
    tek bir istek, kullanıcının haberi olmadan ikinci kez çalıştırılamaz.
    """
    moment = datetime.now(UTC)
    job_id, _, _ = _stale(db_session, records, lease_expires_at=moment - timedelta(seconds=1))

    assert reconcile_stale_playbook_jobs(db_session, now=moment) == 1
    after_first = _result(migrated_engine, job_id)

    later = moment + timedelta(minutes=1)
    assert reconcile_stale_playbook_jobs(db_session, now=later) == 0
    assert not db_session.in_transaction()

    assert _result(migrated_engine, job_id) == after_first
    assert _job(db_session, job_id).status is JobStatus.FAILED


def test_a_heartbeat_that_wins_the_race_keeps_the_row_running(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Karar anı ile ``UPDATE`` arasında yenilenen kira satırı kurtarır.

    Bu, "önce SELECT ile aday seç, sonra koşulsuz yaz" uygulamasının
    **davranışsal** reddidir: böyle bir uygulamada aday okumadan sonra kirasını
    yenileyen canlı bir worker'ın Job'ı yine de kapatılır ve çalışan bir
    execution'ın kaydı yalan olurdu. Koşul ``UPDATE``'in içinde durduğu için
    satır yazma anında artık eşleşmez.

    Yarış gerçek bir yarıştır: yenileme, sürücüye giden ``UPDATE``'in hemen
    öncesinde, ayrı bir session/connection üzerinden ve **gerçek**
    :func:`heartbeat_playbook_job` ile yapılır. Satırın kirası tam sınırda
    (``lease_expires_at == now``) durduğu için uzlaştırmaya göre stale'dir, ama
    bir an öncesine göre hâlâ canlıdır — sahibi meşru biçimde yenileyebilir.
    """
    moment = datetime.now(UTC)
    job_id, worker, start = _stale(db_session, records, lease_expires_at=moment)
    racer_moment = moment - timedelta(seconds=1)
    renewals: list[bool] = []

    def _renew(_conn: Any, _cursor: Any, statement: str, *_args: Any, **_kwargs: Any) -> None:
        if renewals or "UPDATE jobs" not in statement:
            return
        renewals.append(False)
        with Session(migrated_engine) as racer:
            renewals[0] = heartbeat_playbook_job(
                racer, job_id=job_id, worker_id=worker, lease_seconds=LEASE, now=racer_moment
            )

    event.listen(migrated_engine, "before_cursor_execute", _renew)
    try:
        reconciled = reconcile_stale_playbook_jobs(db_session, now=moment)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _renew)

    assert renewals == [True], "yarış gerçekten kuruldu ve kira ileri taşındı"
    assert reconciled == 0
    assert _snapshot(migrated_engine, job_id) == (
        JobStatus.RUNNING,
        worker,
        _naive(start),
        _naive(racer_moment),
        _naive(racer_moment + timedelta(seconds=LEASE)),
    )
    assert not db_session.in_transaction()


def test_reconciliation_reads_no_candidates_before_writing(
    db_session: Session,
    migrated_engine: Engine,
    records: tuple[Project, Inventory],
    counted_statements: list[str],
) -> None:
    """Geçiş tek bir ifadedir: ``jobs`` üzerinde önce okuyan bir sorgu yoktur.

    Ölçüm kaynak metnini aramaz — sürücüye giden ifadeleri sayar. Bir aday
    ``SELECT``'i eklenirse test kırılır; yalnız ``UPDATE``'in koşulunu gevşeten
    bir değişiklik ise yukarıdaki yarış testine takılır.
    """
    moment = datetime.now(UTC)
    _stale(db_session, records, lease_expires_at=moment - timedelta(seconds=1))
    counted_statements.clear()

    assert reconcile_stale_playbook_jobs(db_session, now=moment) == 1

    touched = [statement for statement in counted_statements if "jobs" in statement]
    assert len(touched) == 1
    assert touched[0].lstrip().upper().startswith("UPDATE")


def test_reconciliation_update_failure_rolls_back_and_reraises(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """``UPDATE`` arızası yutulmaz: rollback edilir, hata yeniden yükselir.

    Arıza bir sonuca **çevrilmez**: ``0`` dönmek, bir disk arızasını "kapatılacak
    satır yoktu" diye gösterirdi ve asılı kalan Job açılışta fark edilmeden
    kalırdı.
    """
    moment = datetime.now(UTC)
    job_id, _, _ = _stale(db_session, records, lease_expires_at=moment - timedelta(seconds=1))
    before = _snapshot(migrated_engine, job_id)

    with _failing_statements(migrated_engine, fragment="UPDATE jobs"):
        with pytest.raises(OperationalError):
            reconcile_stale_playbook_jobs(db_session, now=moment)
        assert not db_session.in_transaction()

    assert _snapshot(migrated_engine, job_id) == before
    # Arıza geçtikten sonra aynı session aynı satırı normal biçimde kapatır.
    assert reconcile_stale_playbook_jobs(db_session, now=moment) == 1


def test_reconciliation_commit_failure_rolls_back_and_reraises(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Commit arızasında satır ``running`` kalır ve session kullanılabilir olur."""
    moment = datetime.now(UTC)
    job_id, _, _ = _stale(db_session, records, lease_expires_at=moment - timedelta(seconds=1))
    before = _snapshot(migrated_engine, job_id)
    attempts: list[int] = []
    real_commit = db_session.commit

    def _failing_commit() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("COMMIT", {}, Exception("disk I/O error"))
        real_commit()

    db_session.commit = _failing_commit  # type: ignore[method-assign]
    try:
        with pytest.raises(OperationalError):
            reconcile_stale_playbook_jobs(db_session, now=moment)
        assert not db_session.in_transaction()
        assert _snapshot(migrated_engine, job_id) == before
    finally:
        del db_session.commit

    assert reconcile_stale_playbook_jobs(db_session, now=moment) == 1


class _OffsetlessZone(tzinfo):
    """Aware görünen ama offset'i olmayan bir zaman dilimi.

    ``tzinfo``'su dolu olduğu için naive sayılmaz, ancak ``utcoffset()``
    ``None`` döndürdüğü için UTC'ye çevrilemez ve kira karşılaştırması
    yapılamaz. Kontrolün yalnız ``tzinfo is None``'a bakmadığı böyle ölçülür.
    """

    def utcoffset(self, _dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, _dt: datetime | None) -> str | None:
        return None

    def dst(self, _dt: datetime | None) -> timedelta | None:
        return None


@pytest.mark.parametrize(
    "moment",
    [
        pytest.param(datetime.now(), id="naive-now"),  # noqa: DTZ005
        pytest.param(datetime.now().replace(tzinfo=_OffsetlessZone()), id="offsetless-now"),  # noqa: DTZ005
    ],
)
def test_invalid_reconciliation_input_is_refused_without_touching_the_database(
    db_session: Session,
    migrated_engine: Engine,
    records: tuple[Project, Inventory],
    counted_statements: list[str],
    moment: datetime,
) -> None:
    """Geçersiz karar anı DB'ye dokunmadan reddedilir.

    Naive bir an, yerel saat ile UTC arasındaki farkı kira karşılaştırmasına
    taşırdı: saat farkı kadar erken dolan bir kira, hâlâ çalışan bir Job'ı stale
    gösterirdi.
    """
    job_id, _, _ = _stale(
        db_session, records, lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    before = _snapshot(migrated_engine, job_id)
    counted_statements.clear()

    with pytest.raises(ValueError):
        reconcile_stale_playbook_jobs(db_session, now=moment)

    assert counted_statements == []
    assert not db_session.in_transaction()
    assert _snapshot(migrated_engine, job_id) == before


def test_the_default_decision_moment_is_utc_now(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """``now`` verilmezse karar anı UTC şimdisidir."""
    before = datetime.now(UTC)
    job_id, _, _ = _stale(db_session, records, lease_expires_at=before - timedelta(seconds=1))

    assert reconcile_stale_playbook_jobs(db_session) == 1

    finished = _result(migrated_engine, job_id)[5]
    assert isinstance(finished, datetime)
    assert _naive(before) <= finished <= _naive(datetime.now(UTC))


def test_the_restart_code_carries_no_sensitive_data(
    db_session: Session, migrated_engine: Engine, records: tuple[Project, Inventory]
) -> None:
    """Kapatılan satır serbest metin, worker kimliği veya path taşımaz.

    ``error_code`` API'ye çıkacak sabit bir sözlüktür: içine yazılacak tek bir
    worker kimliği veya workspace yolu, sonucun okunabildiği her yerde görünür
    olurdu.
    """
    moment = datetime.now(UTC)
    job_id, worker, _ = _stale(db_session, records, lease_expires_at=moment - timedelta(seconds=1))

    assert reconcile_stale_playbook_jobs(db_session, now=moment) == 1

    status, return_code, error_code, artifact_path, *_ = _result(migrated_engine, job_id)
    assert status is JobStatus.FAILED
    assert return_code is None
    assert artifact_path is None
    assert error_code == "interrupted_by_restart"
    assert error_code == ERROR_INTERRUPTED_BY_RESTART
    for secret in (worker, job_id, PLAYBOOK_PATH, "/"):
        assert secret not in error_code


def test_the_restart_code_is_not_a_normal_worker_result(
    db_session: Session, owned: tuple[str, str, datetime]
) -> None:
    """Bir worker sıradan sonuç olarak "restart kesti" yazamaz.

    Kod finish allowlist'ine eklenseydi, gerçekten kesilen execution'lar ile
    normal biten execution'lar aynı kayıtta karışır ve uzlaştırmanın izi
    kaybolurdu.
    """
    assert ERROR_INTERRUPTED_BY_RESTART not in FINISH_ERROR_CODES

    job_id, worker, _ = owned
    with pytest.raises(ValueError):
        _finish(
            db_session,
            job_id,
            worker,
            **{**_failure_arguments(job_id), "error_code": ERROR_INTERRUPTED_BY_RESTART},
        )


def test_finish_module_imports_no_runner_or_filesystem_layer() -> None:
    """Durum makinesi runner, normalize, workspace veya artifact katmanını çekmez.

    Import yüzeyi bir üslup tercihi değil, sözleşmenin kendisidir: bu modül
    çalıştırma yolunun parçası değildir ve ona bağlanan bir import, ileride
    sessizce bir çalıştırma kapısı açılmasının ilk adımı olurdu.

    İddia bir yasak listesi değil **tam eşitliktir**: yasak listesi yalnız
    bugün akla gelen modülleri yakalar, oysa eklenmemesi gereken şey henüz adı
    konmamış olandır.
    """
    tree = ast.parse(inspect.getsource(job_state))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported == {
        "__future__",
        "math",
        "re",
        "uuid",
        "dataclasses",
        "datetime",
        "enum",
        "typing",
        "sqlalchemy",
        "sqlalchemy.exc",
        "sqlalchemy.orm",
        "app.models",
    }


def test_result_invariant_rejects_a_context_without_a_win() -> None:
    """``AcquireResult`` kazanılmamış bir sonuca context iliştirilmesine izin vermez."""
    context = AcquiredPlaybookJob(
        job_id=str(uuid.uuid4()),
        execution_plan_id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        manifest_digest="a" * 64,
        project_id=1,
        inventory_id=1,
        playbook_path=PLAYBOOK_PATH,
        requested_by=ACTOR,
        mode=ExecutionMode.CHECK,
        worker_id=str(uuid.uuid4()),
    )

    with pytest.raises(ValueError):
        job_state.AcquireResult(AcquireOutcome.IDLE, context)
    with pytest.raises(ValueError):
        job_state.AcquireResult(AcquireOutcome.ACQUIRED, None)


def test_raw_sql_helper_restores_foreign_key_enforcement(migrated_engine: Engine) -> None:
    """Ham SQL yardımcısı foreign key doğrulamasını açık bırakır.

    ``_raw_sql`` PRAGMA'yı geri açmasaydı, havuza dönen bağlantı foreign key
    doğrulaması kapalı hâlde yeniden kullanılır ve sonraki testler sessizce
    zayıflardı.
    """
    _raw_sql(migrated_engine, "SELECT 1")

    with Session(migrated_engine) as session:
        enabled = session.execute(text("PRAGMA foreign_keys")).scalar_one()
        assert int(enabled) == 1
        assert session.execute(select(func.count()).select_from(Job)).scalar_one() == 0
