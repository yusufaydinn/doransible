"""Çalışan bir süreç boyunca Job kirasının yenilenmesi (R1-V3C1C2B1).

Merkez iddia: **kirasını kaybeden worker'ın süreci yaşamaya devam etmez.**

Ölçülen beş sınır:

1. *Zamanlama.* İlk heartbeat gözlemci başlar başlamaz yapılır; sonrakiler
   sınırlı bir aralıkla devam eder ve ``stop`` sonrasında yenisi başlamaz.
2. *Thread/session sahipliği.* Her heartbeat kendi kısa ömürlü session'ını
   açar ve kapatır; hiçbir session thread'ler arasında paylaşılmaz.
3. *Fail-closed.* Yanlış sahip, dolmuş kira, düşen veritabanı ve patlayan
   session factory — hepsi sonlandırma talebine çevrilir; hiçbiri sessizce
   "kira duruyor" diye okunmaz.
4. *Bileşim.* Lease gözlemcisi eklendiğinde runner'ın kendi raw bütçesi
   **kaybolmaz**; kısmi başlatma ve durdurma arızaları alt süreci sahipsiz
   bırakmaz.
5. *Katman sınırı.* Süreç katmanı veritabanı, session ve Job durumu bilmez;
   gözlemcisiz çağrıların davranışı değişmez.

Testlerin ortak kuralı: ne veritabanı ne de subprocess katmanı taklit edilir.
Kira gerçek bir SQLite satırında yenilenir, süreçler gerçek işletim sistemi
süreçleridir. Zamanlamaya bağlı bekleme yoktur: senkronizasyon
``threading.Event``/``Condition`` ile yapılır, gerçek süre gereken yerde dar ve
toleranslı bir üst sınır kullanılır.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import EXECUTION_RUN_DIRNAME
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
from app.services.ansible import process as process_module
from app.services.ansible.process import (
    BoundedProcessObserver,
    CompositeProcessObserver,
)
from app.services.execution import lease as lease_module
from app.services.execution.job_state import (
    AcquireOutcome,
    acquire_pending_playbook_job,
)
from app.services.execution.lease import PlaybookLeaseObserver
from app.services.execution.runner_env import RunnerEnvironment, build_runner_environment
from app.services.execution.runner_process import (
    RAW_DIRNAME,
    RunnerProcessLimits,
    RunnerProcessResult,
    run_playbook_process,
)
from tests.test_runner_process import freeze_workspace, stub_command

# Testlerin beklemeye razı olduğu **üst** sınır. Beklenen olay normalde
# milisaniyeler içinde gerçekleşir; sınır yalnız yüklü bir makinede testin
# takılmaması içindir ve hiçbir testte "bu kadar sürer" varsayımı yoktur.
WAIT_SECONDS = 10.0

# Hızlı ama gerçek bir kira: heartbeat aralığı kiradan kısadır (sözleşme).
FAST_HEARTBEAT = 0.02
FAST_LEASE = 30.0

PLAYBOOK_PATH = "site.yml"
ACTOR = "yerel-operator"

DEFAULT_LIMITS = RunnerProcessLimits(
    timeout_seconds=30.0, max_stdout_bytes=1_000_000, max_raw_bytes=10_000_000
)

# Hata metinlerinin gözlemciye yapışmadığını ölçmek için kullanılan işaretçi.
SENTINEL_FAILURE = "AOPS-SENTINEL-LEASE-FAILURE-4d1f"


# --- Yardımcılar -------------------------------------------------------------


class TerminationProbe:
    """Supervisor'ın ``request_termination`` çağrısını kaydeden sonda."""

    def __init__(self) -> None:
        self.requested = threading.Event()
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        self.requested.set()


class TrackedSession(Session):
    """Kapatıldığını **kendisi** bildiren session.

    SQLAlchemy kapalı bir session'ı dışarıdan sorgulanabilir bir bayrakla
    işaretlemez; "her tick session'ı kapatır" iddiası ancak böyle ölçülebilir.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.closed = False

    def close(self) -> None:
        super().close()
        self.closed = True


class SessionFactoryProbe:
    """Gerçek engine üzerinde kısa ömürlü session'lar üreten sayaçlı factory.

    Testler heartbeat sayısını **saymak** için kullanır; hiçbir yerde "şu kadar
    beklersem şu kadar heartbeat olur" varsayımı yoktur.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        failure: Callable[[], Exception] | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self._engine = engine
        self._failure = failure
        self._gate = gate
        self._condition = threading.Condition()
        self.calls = 0
        self.sessions: list[TrackedSession] = []
        self.thread_idents: list[int] = []

    def __call__(self) -> Session:
        with self._condition:
            self.calls += 1
            self.thread_idents.append(threading.get_ident())
            self._condition.notify_all()
        if self._gate is not None:
            self._gate.wait(WAIT_SECONDS)
        if self._failure is not None:
            raise self._failure()
        session = TrackedSession(self._engine, expire_on_commit=False)
        self.sessions.append(session)
        return session

    def wait_for_calls(self, count: int, *, timeout: float = WAIT_SECONDS) -> bool:
        """En az ``count`` heartbeat denemesi olana kadar bekler."""
        with self._condition:
            return self._condition.wait_for(lambda: self.calls >= count, timeout=timeout)

    def expect_no_call_beyond(self, count: int, *, timeout: float) -> bool:
        """``count`` üstüne **yeni** bir deneme gelmediğini doğrular."""
        with self._condition:
            return not self._condition.wait_for(lambda: self.calls > count, timeout=timeout)


def failing_session_factory() -> Session:
    """Session hiç üretilemez: factory'nin kendisi düşer."""
    raise OperationalError(SENTINEL_FAILURE, {}, Exception(SENTINEL_FAILURE))


@pytest.fixture
def records(db_session: Session, tmp_path: Path) -> tuple[Project, Inventory]:
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


@pytest.fixture
def running_job(db_session: Session, records: tuple[Project, Inventory]) -> tuple[str, str]:
    """Gerçek acquire yolundan geçmiş, bu worker'a ait ``running`` bir Job.

    Satır elle yazılmaz: kira alanlarını uygulamanın kendi geçişi kurar, böylece
    heartbeat gerçekten "acquire edilmiş bir satırı" yeniler.

    Returns:
        ``(job_id, worker_id)``.
    """
    project, inventory = records
    moment = datetime.now(UTC)
    plan_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    worker_id = str(uuid.uuid4())

    db_session.add(
        ExecutionPlanRecord(
            id=plan_id,
            token_hash=uuid.uuid4().hex * 2,
            project_id=project.id,
            inventory_id=inventory.id,
            playbook_path=PLAYBOOK_PATH,
            requested_by=ACTOR,
            input_fingerprint=uuid.uuid4().hex * 2,
            workspace_id=str(uuid.uuid4()),
            manifest_digest=uuid.uuid4().hex * 2,
            status=ExecutionPlanStatus.CLAIMED,
            created_at=moment,
            expires_at=moment + timedelta(hours=1),
            claimed_at=moment,
        )
    )
    db_session.flush()
    db_session.add(
        Job(
            id=job_id,
            job_type=JobType.PLAYBOOK,
            status=JobStatus.PENDING,
            execution_plan_id=plan_id,
            project_id=project.id,
            inventory_id=inventory.id,
            playbook_path=PLAYBOOK_PATH,
            limit_pattern=None,
            requested_by=ACTOR,
            created_at=moment,
        )
    )
    db_session.commit()

    result = acquire_pending_playbook_job(db_session, worker_id=worker_id, lease_seconds=FAST_LEASE)
    assert result.outcome is AcquireOutcome.ACQUIRED
    assert result.context is not None
    return job_id, worker_id


def expire_lease(session: Session, job_id: str) -> None:
    """Satırın kirasını geçmişe alır: stale recovery'nin devralabildiği hâl.

    ``heartbeat_at`` da geriye alınır; veritabanı
    ``ck_jobs_running_playbook_lease_outlives_heartbeat`` ile kiranın
    heartbeat'ten sonra dolmasını zaten şart koşar.
    """
    moment = datetime.now(UTC)
    session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            heartbeat_at=moment - timedelta(hours=2),
            lease_expires_at=moment - timedelta(hours=1),
        )
    )
    session.commit()


def read_lease(engine: Engine, job_id: str) -> tuple[str | None, datetime | None, datetime | None]:
    """Bağımsız bir bağlantıdan **commit edilmiş** kira alanlarını okur."""
    with Session(engine) as observer:
        row = observer.execute(
            select(Job.worker_id, Job.heartbeat_at, Job.lease_expires_at).where(Job.id == job_id)
        ).one()
    return (row.worker_id, row.heartbeat_at, row.lease_expires_at)


@pytest.fixture
def observers() -> Iterator[list[PlaybookLeaseObserver]]:
    """Testin açtığı gözlemcileri her yolda durdurur.

    Bir assertion düşse bile heartbeat thread'i geride kalmamalıdır; kalsaydı
    sonraki testler kendilerinin olmayan bir kirayı yenileyen bir thread'le
    çalışırdı.
    """
    opened: list[PlaybookLeaseObserver] = []
    try:
        yield opened
    finally:
        for observer in opened:
            observer.stop()


def make_observer(
    opened: list[PlaybookLeaseObserver],
    *,
    session_factory: Callable[[], Session],
    job_id: str,
    worker_id: str,
    heartbeat_seconds: float = FAST_HEARTBEAT,
    lease_seconds: float = FAST_LEASE,
) -> PlaybookLeaseObserver:
    """Gözlemciyi kurar ve temizlik listesine yazar; **başlatmaz**.

    Süreçle birlikte kullanılan gözlemciyi başlatan taraf süreç katmanıdır:
    ``request_termination`` supervisor'ın kendi çağrısıdır ve testte taklit
    edilmez.
    """
    observer = PlaybookLeaseObserver(
        session_factory=session_factory,
        job_id=job_id,
        worker_id=worker_id,
        heartbeat_seconds=heartbeat_seconds,
        lease_seconds=lease_seconds,
    )
    opened.append(observer)
    return observer


def start_observer(
    opened: list[PlaybookLeaseObserver],
    probe: TerminationProbe,
    *,
    session_factory: Callable[[], Session],
    job_id: str,
    worker_id: str,
    heartbeat_seconds: float = FAST_HEARTBEAT,
    lease_seconds: float = FAST_LEASE,
) -> PlaybookLeaseObserver:
    """Gözlemciyi kurar, verilen sonda ile başlatır ve temizlik listesine yazar."""
    observer = make_observer(
        opened,
        session_factory=session_factory,
        job_id=job_id,
        worker_id=worker_id,
        heartbeat_seconds=heartbeat_seconds,
        lease_seconds=lease_seconds,
    )
    observer.start(probe)
    return observer


# --- 1: zamanlama ------------------------------------------------------------


def test_the_first_heartbeat_does_not_wait_for_an_interval(
    migrated_engine: Engine,
    running_job: tuple[str, str],
    observers: list[PlaybookLeaseObserver],
) -> None:
    """İlk heartbeat gözlemci başlar başlamaz yapılır.

    Aralık kiranın hemen altında seçilir: ilk heartbeat bir aralık beklenerek
    yapılsaydı bu test o aralığın sonuna kadar hiçbir yenileme görmezdi.
    """
    job_id, worker_id = running_job
    _, before_heartbeat, _ = read_lease(migrated_engine, job_id)
    factory = SessionFactoryProbe(migrated_engine)
    probe = TerminationProbe()

    observer = start_observer(
        observers,
        probe,
        session_factory=factory,
        job_id=job_id,
        worker_id=worker_id,
        heartbeat_seconds=20.0,
        lease_seconds=30.0,
    )

    assert factory.wait_for_calls(1) is True
    observer.stop()

    _, after_heartbeat, _ = read_lease(migrated_engine, job_id)
    assert before_heartbeat is not None and after_heartbeat is not None
    assert after_heartbeat >= before_heartbeat
    assert observer.lease_lost is False
    assert observer.heartbeat_failed is False
    assert probe.calls == 0


def test_the_owner_keeps_extending_its_lease(
    migrated_engine: Engine,
    running_job: tuple[str, str],
    observers: list[PlaybookLeaseObserver],
) -> None:
    """Doğru sahip kirayı **periyodik olarak** ileri taşır."""
    job_id, worker_id = running_job
    _, _, first_expiry = read_lease(migrated_engine, job_id)
    factory = SessionFactoryProbe(migrated_engine)
    probe = TerminationProbe()

    observer = start_observer(
        observers, probe, session_factory=factory, job_id=job_id, worker_id=worker_id
    )
    assert factory.wait_for_calls(3) is True
    observer.stop()

    worker, heartbeat_at, expiry = read_lease(migrated_engine, job_id)
    assert worker == worker_id
    assert heartbeat_at is not None
    assert first_expiry is not None and expiry is not None
    # Kira ileri taşındı ve satır hâlâ aynı worker'ın.
    assert expiry >= first_expiry
    assert expiry > heartbeat_at
    assert observer.lease_lost is False
    assert observer.heartbeat_failed is False
    assert probe.calls == 0


def test_no_heartbeat_starts_after_stop(
    migrated_engine: Engine,
    running_job: tuple[str, str],
    observers: list[PlaybookLeaseObserver],
) -> None:
    """``stop`` sonrasında yeni bir heartbeat **başlamaz**.

    Durdurulmuş bir gözlemcinin yenilemeye devam etmesi, sürecin çoktan
    bittiği bir işin kirasını canlı tutardı: satır ne devralınabilir ne de
    stale sayılabilirdi.
    """
    job_id, worker_id = running_job
    factory = SessionFactoryProbe(migrated_engine)
    probe = TerminationProbe()

    observer = start_observer(
        observers, probe, session_factory=factory, job_id=job_id, worker_id=worker_id
    )
    assert factory.wait_for_calls(2) is True
    observer.stop()
    settled = factory.calls

    # Aralığın kat kat üstünde bir pencere: yeni bir deneme gelseydi görülürdü.
    assert factory.expect_no_call_beyond(settled, timeout=FAST_HEARTBEAT * 50) is True
    assert factory.calls == settled


def test_the_heartbeat_thread_does_not_outlive_stop(
    migrated_engine: Engine,
    running_job: tuple[str, str],
    observers: list[PlaybookLeaseObserver],
) -> None:
    """Durdurulan gözlemcinin thread'i geride kalmaz."""
    job_id, worker_id = running_job
    factory = SessionFactoryProbe(migrated_engine)
    probe = TerminationProbe()

    observer = start_observer(
        observers, probe, session_factory=factory, job_id=job_id, worker_id=worker_id
    )
    thread = observer._thread
    assert thread is not None
    assert factory.wait_for_calls(1) is True

    observer.stop()

    assert thread.is_alive() is False
    assert observer.heartbeat_failed is False


# --- 2: thread ve session sahipliği ------------------------------------------


def test_every_heartbeat_opens_and_closes_its_own_session(
    migrated_engine: Engine,
    running_job: tuple[str, str],
    observers: list[PlaybookLeaseObserver],
) -> None:
    """Her tick **yeni** bir session açar, kullanır ve kapatır.

    SQLAlchemy :class:`Session` thread-safe değildir: çağıranın session'ı
    ödünç alınsaydı, aynı session iki thread'den kullanılırdı. Uzun çalıştırma
    boyunca açık tutulan tek bir session ise bağlantıyı (SQLite'ta yazma
    kilidini) çalıştırma süresince elde tutardı.
    """
    job_id, worker_id = running_job
    factory = SessionFactoryProbe(migrated_engine)
    probe = TerminationProbe()

    observer = start_observer(
        observers, probe, session_factory=factory, job_id=job_id, worker_id=worker_id
    )
    assert factory.wait_for_calls(3) is True
    observer.stop()

    assert len(factory.sessions) >= 3
    assert len({id(session) for session in factory.sessions}) == len(factory.sessions)
    assert all(session.closed for session in factory.sessions)

    # Session'ların hepsi **tek** bir thread'de ve o thread ana thread değil.
    assert set(factory.thread_idents) == {factory.thread_idents[0]}
    assert factory.thread_idents[0] != threading.get_ident()


# --- 3: fail-closed ----------------------------------------------------------


def test_a_wrong_owner_loses_the_lease_and_demands_termination(
    migrated_engine: Engine,
    running_job: tuple[str, str],
    observers: list[PlaybookLeaseObserver],
) -> None:
    """Satırın sahibi olmayan worker kirayı uzatamaz; süreç sonlandırılmalıdır."""
    job_id, owner_id = running_job
    intruder = str(uuid.uuid4())
    before = read_lease(migrated_engine, job_id)
    factory = SessionFactoryProbe(migrated_engine)
    probe = TerminationProbe()

    observer = start_observer(
        observers, probe, session_factory=factory, job_id=job_id, worker_id=intruder
    )

    assert probe.requested.wait(WAIT_SECONDS) is True
    observer.stop()

    assert observer.lease_lost is True
    assert observer.heartbeat_failed is False
    # Satır dokunulmadan kaldı: gerçek sahibin kirası bozulmadı.
    assert read_lease(migrated_engine, job_id) == before
    assert before[0] == owner_id
    # Kaybedilmiş bir kira yeniden denenmez.
    assert factory.expect_no_call_beyond(factory.calls, timeout=FAST_HEARTBEAT * 50) is True


def test_an_expired_lease_is_never_revived(
    db_session: Session,
    migrated_engine: Engine,
    running_job: tuple[str, str],
    observers: list[PlaybookLeaseObserver],
) -> None:
    """Kirası dolmuş satır, sahibi tarafından bile canlandırılmaz.

    Dolmuş kira, stale recovery'nin devralmaya hak kazandığı satırdır; onu
    canlandırmak aynı işin iki worker tarafından çalıştırılmasına kapı açardı.
    """
    job_id, worker_id = running_job
    expire_lease(db_session, job_id)
    before = read_lease(migrated_engine, job_id)

    factory = SessionFactoryProbe(migrated_engine)
    probe = TerminationProbe()
    observer = start_observer(
        observers, probe, session_factory=factory, job_id=job_id, worker_id=worker_id
    )

    assert probe.requested.wait(WAIT_SECONDS) is True
    observer.stop()

    assert observer.lease_lost is True
    assert observer.heartbeat_failed is False
    assert read_lease(migrated_engine, job_id) == before


def test_a_failing_session_factory_is_a_heartbeat_failure(
    running_job: tuple[str, str],
    observers: list[PlaybookLeaseObserver],
) -> None:
    """Session hiç açılamıyorsa kira **kanıtlanamamıştır**: süreç durdurulur."""
    job_id, worker_id = running_job
    probe = TerminationProbe()

    observer = start_observer(
        observers,
        probe,
        session_factory=failing_session_factory,
        job_id=job_id,
        worker_id=worker_id,
    )

    assert probe.requested.wait(WAIT_SECONDS) is True
    observer.stop()

    assert observer.heartbeat_failed is True
    assert observer.lease_lost is False


def test_a_database_failure_during_the_update_is_a_heartbeat_failure(
    monkeypatch: pytest.MonkeyPatch,
    migrated_engine: Engine,
    running_job: tuple[str, str],
    observers: list[PlaybookLeaseObserver],
) -> None:
    """``UPDATE`` düşerse arıza yutulmaz, sonlandırma talebine çevrilir.

    Sessizce yutulan bir veritabanı arızası, kirası yenilenmediği hâlde
    çalışmaya devam eden bir süreç bırakırdı.
    """

    def _explode(*_args: object, **_kwargs: object) -> bool:
        raise OperationalError(SENTINEL_FAILURE, {}, Exception(SENTINEL_FAILURE))

    monkeypatch.setattr(lease_module, "heartbeat_playbook_job", _explode)
    job_id, worker_id = running_job
    factory = SessionFactoryProbe(migrated_engine)
    probe = TerminationProbe()

    observer = start_observer(
        observers, probe, session_factory=factory, job_id=job_id, worker_id=worker_id
    )

    assert probe.requested.wait(WAIT_SECONDS) is True
    observer.stop()

    assert observer.heartbeat_failed is True
    assert observer.lease_lost is False
    # Arıza yolunda da session kapanır: sızan bir session bağlantıyı tutardı.
    assert factory.sessions and all(session.closed for session in factory.sessions)
    # Arızalanan bir heartbeat tekrar denenmez.
    assert factory.expect_no_call_beyond(factory.calls, timeout=FAST_HEARTBEAT * 50) is True


def test_no_failure_detail_is_kept_on_the_observer(
    running_job: tuple[str, str],
    observers: list[PlaybookLeaseObserver],
) -> None:
    """Gözlemcinin taşıdığı tek sonuç iki boolean'dır.

    Exception metni sürücü mesajını, DSN'i ve path'i taşır; kalıcı bir alana
    yazılsaydı sonucun okunduğu her yerde görünür olurdu.
    """
    job_id, worker_id = running_job
    probe = TerminationProbe()

    observer = start_observer(
        observers,
        probe,
        session_factory=failing_session_factory,
        job_id=job_id,
        worker_id=worker_id,
    )
    assert probe.requested.wait(WAIT_SECONDS) is True
    observer.stop()

    assert SENTINEL_FAILURE not in repr(vars(observer))
    public = {name: value for name, value in vars(observer).items() if not name.startswith("_")}
    assert public == {"lease_lost": False, "heartbeat_failed": True}


def test_a_hanging_heartbeat_is_bounded_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    migrated_engine: Engine,
    running_job: tuple[str, str],
) -> None:
    """Asılı kalan bir heartbeat ``stop``'u süresiz bekletmez.

    Yolda kalmış bir ``UPDATE``'in sonucu beklenmez ve okunmaz: kiranın canlı
    olduğu bu yolda kanıtlanamamıştır, sonuç fail-closed'dır.
    """
    monkeypatch.setattr(lease_module, "LEASE_OBSERVER_JOIN_SECONDS", 0.2)
    job_id, worker_id = running_job
    gate = threading.Event()
    factory = SessionFactoryProbe(migrated_engine, gate=gate)
    observer = PlaybookLeaseObserver(
        session_factory=factory,
        job_id=job_id,
        worker_id=worker_id,
        heartbeat_seconds=FAST_HEARTBEAT,
        lease_seconds=FAST_LEASE,
    )
    thread: threading.Thread | None = None
    try:
        observer.start(TerminationProbe())
        thread = observer._thread
        assert factory.wait_for_calls(1) is True

        started = time.monotonic()
        observer.stop()
        elapsed = time.monotonic() - started

        assert elapsed < WAIT_SECONDS
        assert observer.heartbeat_failed is True
    finally:
        # Thread daemon'dır ve süreci ayakta tutmaz; yine de test bir thread'i
        # arkada bırakmaz.
        gate.set()
        if thread is not None:
            thread.join(timeout=WAIT_SECONDS)
            assert thread.is_alive() is False


# --- Yaşam döngüsü sözleşmesi ------------------------------------------------


def test_the_observer_is_single_use(
    migrated_engine: Engine,
    running_job: tuple[str, str],
    observers: list[PlaybookLeaseObserver],
) -> None:
    """İkinci bir ``start`` ve durdurulmuş bir gözlemcinin ``start``'ı reddedilir.

    İdempotent bir ``start``, aynı Job için iki heartbeat thread'i açmanın veya
    durdurulmuş bir gözlemciyi sessizce diriltmenin yolu olurdu.
    """
    job_id, worker_id = running_job
    factory = SessionFactoryProbe(migrated_engine)
    observer = start_observer(
        observers, TerminationProbe(), session_factory=factory, job_id=job_id, worker_id=worker_id
    )

    with pytest.raises(RuntimeError):
        observer.start(TerminationProbe())

    observer.stop()
    # `stop` idempotenttir.
    observer.stop()
    with pytest.raises(RuntimeError):
        observer.start(TerminationProbe())


def test_stop_without_start_is_a_no_op(migrated_engine: Engine) -> None:
    """Hiç başlatılmamış gözlemcinin durdurulması hata üretmez."""
    observer = PlaybookLeaseObserver(
        session_factory=SessionFactoryProbe(migrated_engine),
        job_id=str(uuid.uuid4()),
        worker_id=str(uuid.uuid4()),
        heartbeat_seconds=FAST_HEARTBEAT,
        lease_seconds=FAST_LEASE,
    )
    observer.stop()
    assert observer.lease_lost is False
    assert observer.heartbeat_failed is False


@pytest.mark.parametrize(
    ("job_id", "worker_id", "heartbeat_seconds", "lease_seconds"),
    [
        ("not-a-uuid", None, FAST_HEARTBEAT, FAST_LEASE),
        (None, "not-a-uuid", FAST_HEARTBEAT, FAST_LEASE),
        # Sürüm 4 olmayan bir UUID uygulamanın ürettiği kimlik değildir.
        ("00000000-0000-1000-8000-000000000000", None, FAST_HEARTBEAT, FAST_LEASE),
        (None, None, 0.0, FAST_LEASE),
        (None, None, FAST_HEARTBEAT, float("inf")),
        # Kirasından seyrek atan bir heartbeat, kirayı düzenli olarak süresi
        # geçmiş bırakırdı.
        (None, None, 30.0, 30.0),
        (None, None, 60.0, 30.0),
    ],
)
def test_invalid_inputs_are_rejected_before_any_thread_or_query(
    migrated_engine: Engine,
    job_id: str | None,
    worker_id: str | None,
    heartbeat_seconds: float,
    lease_seconds: float,
) -> None:
    """Geçersiz girdi thread açılmadan ve veritabanına gidilmeden reddedilir."""
    factory = SessionFactoryProbe(migrated_engine)
    before = threading.active_count()

    with pytest.raises(ValueError):
        PlaybookLeaseObserver(
            session_factory=factory,
            job_id=job_id if job_id is not None else str(uuid.uuid4()),
            worker_id=worker_id if worker_id is not None else str(uuid.uuid4()),
            heartbeat_seconds=heartbeat_seconds,
            lease_seconds=lease_seconds,
        )

    assert factory.calls == 0
    assert threading.active_count() == before


# --- 4: gözlemci bileşimi ----------------------------------------------------


class RecordingObserver:
    """Yaşam döngüsü çağrılarını ortak bir seyir defterine yazan gözlemci."""

    def __init__(
        self,
        name: str,
        journal: list[str],
        *,
        fail_on_start: bool = False,
        fail_on_stop: bool = False,
    ) -> None:
        self.name = name
        self._journal = journal
        self._fail_on_start = fail_on_start
        self._fail_on_stop = fail_on_stop
        self.request_termination: Callable[[], None] | None = None

    def start(self, request_termination: Callable[[], None]) -> None:
        self._journal.append(f"start:{self.name}")
        if self._fail_on_start:
            raise RuntimeError(f"start:{self.name}")
        self.request_termination = request_termination

    def stop(self) -> None:
        self._journal.append(f"stop:{self.name}")
        if self._fail_on_stop:
            raise RuntimeError(f"stop:{self.name}")


def test_a_composite_starts_in_order_and_stops_in_reverse() -> None:
    """Bileşik gözlemci sırayla başlatır, **ters sırada** durdurur."""
    journal: list[str] = []
    composite = CompositeProcessObserver(
        RecordingObserver("raw", journal), RecordingObserver("lease", journal)
    )
    probe = TerminationProbe()

    composite.start(probe)
    composite.stop()
    # İkinci durdurma hiçbir şey yapmaz.
    composite.stop()

    assert journal == ["start:raw", "start:lease", "stop:lease", "stop:raw"]


def test_a_partial_start_failure_stops_what_was_already_started() -> None:
    """Başlatılamayan bir gözlemci, başlamış olanları arkada bırakmaz.

    Yarım kurulmuş bir zincir, kimsenin durdurmadığı bir ölçüm thread'i
    bırakırdı; asıl hata da bu temizlik tarafından gölgelenmemelidir.
    """
    journal: list[str] = []
    composite = CompositeProcessObserver(
        RecordingObserver("raw", journal),
        RecordingObserver("lease", journal, fail_on_start=True),
        RecordingObserver("never", journal),
    )

    with pytest.raises(RuntimeError, match="start:lease"):
        composite.start(TerminationProbe())

    assert journal == ["start:raw", "start:lease", "stop:raw"]


def test_a_failing_stop_does_not_prevent_the_other_observers_from_stopping() -> None:
    """Bir ``stop`` hata verse de kalanlar durdurulur; ilk hata yükseltilir.

    Hata yutulsaydı çağıran, gözlemcisi arızalanmış bir çalıştırmayı sorunsuz
    sanırdı; yükseltilen hata alt sürecin sahipsiz kalmamasını sağlar.
    """
    journal: list[str] = []
    composite = CompositeProcessObserver(
        RecordingObserver("raw", journal, fail_on_stop=True),
        RecordingObserver("lease", journal, fail_on_stop=True),
    )
    composite.start(TerminationProbe())

    with pytest.raises(RuntimeError, match="stop:lease"):
        composite.stop()

    assert journal == ["start:raw", "start:lease", "stop:lease", "stop:raw"]


def test_the_lease_observer_satisfies_the_generic_process_protocol(
    migrated_engine: Engine,
) -> None:
    """Lease gözlemcisi generic protokolü karşılar.

    Atama mypy tarafından da ölçülür: süreç katmanı yalnız
    :class:`BoundedProcessObserver` bilir, lease gözlemcisini **tanımaz**.
    """
    observer: BoundedProcessObserver = PlaybookLeaseObserver(
        session_factory=SessionFactoryProbe(migrated_engine),
        job_id=str(uuid.uuid4()),
        worker_id=str(uuid.uuid4()),
        heartbeat_seconds=FAST_HEARTBEAT,
        lease_seconds=FAST_LEASE,
    )
    assert callable(observer.start)
    assert callable(observer.stop)


# --- 5: gerçek süreçle bileşim ----------------------------------------------


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    """`runner_env`'in beklediği biçimde, önceden var olan 0700 execution kökü.

    Kök zaten kurulmuş olabilir: veritabanı fixture'ı aynı ``app-data`` ağacını
    uygulamanın kendi yolundan üretir.
    """
    root = tmp_path / "app-data" / EXECUTION_RUN_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


@pytest.fixture
def plan_root(tmp_path: Path) -> Path:
    """``app-data/execution-plans`` karşılığı: dondurulmuş workspace'lerin kökü."""
    root = tmp_path / "app-data" / "execution-plans"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def frozen_workspace(plan_root: Path) -> Path:
    """Gerçek düzende, tam bir dondurulmuş workspace."""
    return freeze_workspace(plan_root)


@pytest.fixture
def environment(
    run_root: Path, running_job: tuple[str, str], frozen_workspace: Path, tmp_path: Path
) -> RunnerEnvironment:
    """Job'ın **kendi** kimliğiyle kurulmuş gerçek runner environment'ı."""
    job_id, _ = running_job
    return build_runner_environment(
        execution_run_root=run_root,
        job_id=job_id,
        frozen_project_root=frozen_workspace / "project",
        ssh_policy="strict",
        known_hosts=tmp_path / "app-data" / "ssh" / "known_hosts",
    )


def run_stub_with_observer(
    *,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_workspace: Path,
    behaviour: str,
    observer: BoundedProcessObserver | None,
    limits: RunnerProcessLimits | None = None,
    stub_options: dict[str, object] | None = None,
) -> RunnerProcessResult:
    """Sahte runner CLI'sini gerçek süreç olarak, verilen gözlemciyle çalıştırır."""
    return run_playbook_process(
        command=stub_command(behaviour, **(stub_options or {})),
        runner_environment=environment,
        job_id=job_id,
        frozen_project_root=frozen_workspace / "project",
        frozen_inventory_path=frozen_workspace / "inventory" / "hosts.yml",
        playbook_path=PLAYBOOK_PATH,
        mode=ExecutionMode.CHECK,
        limits=limits or DEFAULT_LIMITS,
        observer=observer,
    )


def process_is_alive(pid: int) -> bool:
    """Süreç hâlâ yaşıyor mu (zombie sayılmaz)."""
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return False
    return stat_path.read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0] != "Z"


def wait_for_child_report(report: Path) -> None:
    """Child'ın gerçekten çalıştığını **kanıt** ile bekler.

    Beklenen şey bir süre değil bir olaydır: stub, uykuya dalmadan önce kendi
    pid/pgid'sini bu dosyaya yazar. Zamanlama tahminiyle beklemek, child daha
    başlamadan sonlandırılan bir çalıştırmayı "erken sonlandırma kanıtı" gibi
    gösterirdi. Üst sınır yalnız testin takılmaması içindir.
    """
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if report.exists() and report.stat().st_size > 0:
            return
        time.sleep(0.01)
    raise AssertionError("Child raporu beklenen sürede yazılmadı.")


def group_has_live_members(process_group: int) -> bool | None:
    """Process group'ta zombie olmayan üye kaldı mı; ölçüm belirsizse ``None``.

    Ölçüm uygulamanın kendi yardımcısıyla yapılır: testin kendi tarama
    kuralını yazması, ürünün gördüğünden farklı bir "canlı" tanımı üretirdi.
    """
    return process_module._process_group_has_live_members(process_group)


def test_the_lease_is_renewed_while_a_real_child_runs(
    migrated_engine: Engine,
    running_job: tuple[str, str],
    environment: RunnerEnvironment,
    frozen_workspace: Path,
    observers: list[PlaybookLeaseObserver],
) -> None:
    """Raw bütçesi ve lease gözlemcisi **aynı** süreçte birlikte çalışır.

    Lease gözlemcisinin eklenmesi runner'ın kendi ölçümünü devre dışı bırakmaz:
    çalıştırma normal biter, raw alanı yine silinir ve kira boyunca yenilenir.
    """
    job_id, worker_id = running_job
    _, _, before_expiry = read_lease(migrated_engine, job_id)
    factory = SessionFactoryProbe(migrated_engine)
    observer = make_observer(observers, session_factory=factory, job_id=job_id, worker_id=worker_id)

    result = run_stub_with_observer(
        environment=environment,
        job_id=job_id,
        frozen_workspace=frozen_workspace,
        behaviour="write-raw",
        observer=observer,
        stub_options={"size_bytes": 1024},
    )

    assert result.return_code == 0
    assert result.timed_out is False
    assert result.raw_limit_exceeded is False
    assert not (environment.run_dir / RAW_DIRNAME).exists()

    assert observer.lease_lost is False
    assert observer.heartbeat_failed is False
    assert factory.calls >= 1
    _, heartbeat_at, expiry = read_lease(migrated_engine, job_id)
    assert before_expiry is not None and expiry is not None and heartbeat_at is not None
    assert expiry >= before_expiry
    # Süreç bittiğinde gözlemci de durmuştur.
    assert observer._thread is None


def test_the_raw_limit_still_cuts_the_process_with_a_lease_observer_attached(
    migrated_engine: Engine,
    running_job: tuple[str, str],
    environment: RunnerEnvironment,
    frozen_workspace: Path,
    observers: list[PlaybookLeaseObserver],
) -> None:
    """Lease gözlemcisi eklendiğinde raw bütçesi **kaybolmaz**.

    İki gözlemciden birini seçmek zorunda kalmak, diğerinin sınırının o
    çalıştırmada hiç uygulanmaması demek olurdu.
    """
    job_id, worker_id = running_job
    observer = make_observer(
        observers,
        session_factory=SessionFactoryProbe(migrated_engine),
        job_id=job_id,
        worker_id=worker_id,
    )

    started = time.monotonic()
    result = run_stub_with_observer(
        environment=environment,
        job_id=job_id,
        frozen_workspace=frozen_workspace,
        behaviour="flood-raw",
        observer=observer,
        limits=RunnerProcessLimits(
            timeout_seconds=60.0, max_stdout_bytes=1_000_000, max_raw_bytes=1_000_000
        ),
        stub_options={"size_bytes": 300_000, "sleep_seconds": 60},
    )
    elapsed = time.monotonic() - started

    assert result.raw_limit_exceeded is True
    assert result.timed_out is False
    assert elapsed < 30.0
    # Raw sınırı kesti; kira sağlıklıydı.
    assert observer.lease_lost is False
    assert observer.heartbeat_failed is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ölçümü")
def test_losing_the_lease_terminates_and_reaps_a_real_sleeping_child(
    db_session: Session,
    migrated_engine: Engine,
    running_job: tuple[str, str],
    environment: RunnerEnvironment,
    frozen_workspace: Path,
    tmp_path: Path,
    observers: list[PlaybookLeaseObserver],
) -> None:
    """Kira kaybı, 60 saniye uyuyan gerçek bir child'ı erken sonlandırır.

    Ölçülen şey iddia değil sonuçtur: leader reap edilir, process group'ta
    yaşayan üye kalmaz ve çalıştırma timeout'a kadar sürmez.
    """
    job_id, worker_id = running_job
    # Kira sürecin başlangıcında **dolmuş** olsun: ilk heartbeat kaybı görür.
    expire_lease(db_session, job_id)

    report = tmp_path / "child-report.json"

    def gated_session_factory() -> Session:
        """İlk heartbeat, child'ın gerçekten çalıştığı **kanıtlandıktan** sonra.

        Aksi hâlde kira kaybı child daha uykuya dalmadan görülebilir ve test,
        çalışan bir sürecin sonlandırıldığını değil, hiç başlamamış bir sürecin
        yokluğunu ölçerdi.
        """
        wait_for_child_report(report)
        return Session(migrated_engine, expire_on_commit=False)

    observer = make_observer(
        observers,
        session_factory=gated_session_factory,
        job_id=job_id,
        worker_id=worker_id,
    )

    started = time.monotonic()
    result = run_stub_with_observer(
        environment=environment,
        job_id=job_id,
        frozen_workspace=frozen_workspace,
        behaviour="sleep",
        observer=observer,
        limits=RunnerProcessLimits(
            timeout_seconds=60.0, max_stdout_bytes=1_000_000, max_raw_bytes=10_000_000
        ),
        stub_options={"sleep_seconds": 60, "report": report},
    )
    elapsed = time.monotonic() - started

    assert observer.lease_lost is True
    assert observer.heartbeat_failed is False
    assert result.timed_out is False
    # 60 saniyelik uyku sonlandırma ile kesildi.
    assert elapsed < 30.0

    # Rapor gerçekten yazıldı: süreç başlamış ve sonra sonlandırılmıştı.
    child = json.loads(report.read_text(encoding="utf-8"))
    assert process_is_alive(int(child["pid"])) is False
    # Torun süreçler de kapandı. Ölçüm belirsiz kalırsa (``None``) test bunu
    # "canlı üye var" saymaz; kesin olan tek şey canlı üye **görülmemesidir**.
    assert group_has_live_members(int(child["pgid"])) is not True


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ölçümü")
def test_an_observer_that_cannot_start_leaves_no_orphaned_child(
    environment: RunnerEnvironment,
    running_job: tuple[str, str],
    frozen_workspace: Path,
    tmp_path: Path,
) -> None:
    """Gözlemci başlatılamazsa child sahipsiz bırakılmaz.

    Hata yutulsaydı ölçülmeyen bir süreç arkada çalışmaya devam ederdi;
    yükseltilen hata çağıranın çalıştırmayı arızalı saymasını sağlar.
    """
    job_id, _ = running_job
    report = tmp_path / "child-report.json"

    class LateFailingObserver:
        """Child gerçekten başladıktan **sonra** düşen gözlemci."""

        def start(self, request_termination: Callable[[], None]) -> None:
            wait_for_child_report(report)
            raise RuntimeError("gozlemci baslatilamadi")

        def stop(self) -> None:  # pragma: no cover - başlatılamayan gözlemci durdurulmaz
            raise AssertionError("Baslatilamayan gozlemci durdurulmaz.")

    with pytest.raises(RuntimeError, match="gozlemci baslatilamadi"):
        run_stub_with_observer(
            environment=environment,
            job_id=job_id,
            frozen_workspace=frozen_workspace,
            behaviour="sleep",
            observer=LateFailingObserver(),
            stub_options={"sleep_seconds": 60, "report": report},
        )

    child = json.loads(report.read_text(encoding="utf-8"))
    assert process_is_alive(int(child["pid"])) is False
    assert group_has_live_members(int(child["pgid"])) is not True
    # İç raw gözlemcisi de durduruldu ve raw alanı silindi.
    assert not (environment.run_dir / RAW_DIRNAME).exists()


def test_a_run_without_an_observer_is_unchanged(
    environment: RunnerEnvironment,
    running_job: tuple[str, str],
    frozen_workspace: Path,
) -> None:
    """``observer=None`` mevcut runner davranışını birebir korur."""
    job_id, _ = running_job
    assert inspect.signature(run_playbook_process).parameters["observer"].default is None

    result = run_stub_with_observer(
        environment=environment,
        job_id=job_id,
        frozen_workspace=frozen_workspace,
        behaviour="success",
        observer=None,
    )

    assert result.return_code == 0
    assert result.timed_out is False
    assert result.oversized_stream is None
    assert result.raw_limit_exceeded is False
    assert not (environment.run_dir / RAW_DIRNAME).exists()


# --- 6: katman sınırı --------------------------------------------------------


def imported_modules(path: Path) -> set[str]:
    """Modülün import ettiği bütün modül adları (AST üzerinden)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_the_process_layer_never_learns_about_the_database_or_job_state() -> None:
    """Runner ve ortak süreç katmanı DB, session ve Job durumu **bilmez**.

    Kontrol metin araması değil gerçek import listesidir: docstring'de geçen
    bir modül adı testi ne geçirir ne düşürür.

    Tek istisna ``app.models.execution_mode``'dur (R1-V3H1B2B):
    :class:`~app.models.execution_mode.ExecutionMode` yalnız stdlib ve
    SQLAlchemy'nin sütun tipi tanımına bağlı, Session veya başka bir ORM
    modeli taşımayan bir değer tipidir; onu ``build_runner_arguments``'ın kip
    parametresi için almak DB/session sınırını **delmez**. Başka hiçbir
    ``app.models`` alt modülü bu istisnaya girmez.
    """
    backend = Path(__file__).resolve().parents[1]
    forbidden = (
        "sqlalchemy",
        "app.db",
        "app.models",
        "fastapi",
        "app.services.execution.job_state",
        "app.services.execution.lease",
    )
    allowed_leaf = "app.models.execution_mode"
    for relative in ("app/services/execution/runner_process.py", "app/services/ansible/process.py"):
        for name in imported_modules(backend / relative):
            if name == allowed_leaf:
                continue
            assert not name.startswith(forbidden), f"{relative}: {name}"


def test_the_lease_module_imports_nothing_beyond_its_contract() -> None:
    """Lease gözlemcisi yalnız session ve durum makinesini tanır.

    Runner, workspace, artifact, FastAPI ve model katmanları burada yoktur;
    gözlemci bir süreç başlatmaz ve dosya sistemine dokunmaz.
    """
    backend = Path(__file__).resolve().parents[1]
    assert imported_modules(backend / "app/services/execution/lease.py") == {
        "__future__",
        "math",
        "threading",
        "uuid",
        "collections.abc",
        "contextlib",
        "sqlalchemy.orm",
        "app.services.execution.job_state",
    }


def test_the_lease_module_never_logs() -> None:
    """Gözlemci hiçbir arızayı log'a veya stderr'e yazmaz.

    Bir log satırı sürücü mesajını, DSN'i veya path'i taşırdı; hata ayrıntısı
    yalnız iki boolean'a indirgenir.
    """
    source = (Path(__file__).resolve().parents[1] / "app/services/execution/lease.py").read_text(
        encoding="utf-8"
    )
    forbidden = {"logging", "logger", "print", "warnings", "stderr", "traceback"}
    called: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
        elif isinstance(node, ast.Name):
            called.add(node.id)
    assert called & forbidden == set()
