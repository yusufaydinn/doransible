"""Arka plan playbook worker'ı ve lifespan entegrasyonu (R1-V3C2C).

Merkez iddia: **arka planda bir şeyin çalışması, açıkça açılmış olmasına ve
açılış toparlamasının başarıyla bitmiş olmasına bağlıdır; kapanış ise çalışan
child gerçekten sonlandırılıp reap edilmeden tamamlanmış sayılmaz.**

Ölçülen sınırlar:

1. *Varsayılan kapalılık.* Ayar açılmadan ne bir worker thread'i ne de tek bir
   executor çağrısı doğar; API yüzeyi de büyümez.
2. *Eşzamanlılık ve temizlik aralığı.* Açıkken bile aynı anda **bir**
   çalıştırma vardır; periyodik janitor kendi thread'inde çalıştığı için bu
   sayıyı artırmaz ama uzun bir çalıştırma tarafından da **bloke edilmez**.
3. *Boşta bekleme.* Meşguliyet döngüsü yoktur ve kapanış talebi hem poll hem
   janitor beklemesini **anında** uyandırır; stop'tan sonra yeni Job alınmaz.
4. *Durdurma sözleşmesi.* ``stop`` yalnız iki thread'in de bittiği
   kanıtlandığında ``True`` döner; timeout sonrası tekrarlanan ve eşzamanlı
   çağrılar aynı canlı thread'i gözlemler ve yanlışlıkla ``True`` dönmez.
5. *Kapanış.* Çalışan bir child'a sonlandırma talebi gider, child reap edilir ve
   ancak ondan sonra shutdown döner; durmayan bir worker'ın üstüne shutdown
   **başarılı görünmez** ve engine bırakılmaz.
6. *Gözlemci bileşimi.* Kira, kapanış ve raw bütçe gözlemcilerinden hiçbiri
   diğerini devre dışı bırakmaz.
7. *Açılış sırası ve atomikliği.* C2A (commit + session kapanışı) → C2B →
   worker. Herhangi birinin arızası worker'ı **başlatmaz**; worker'ın kendi
   ``start``'ı düşerse de yarım başlamış bir thread veya sahipsiz bir engine
   kalmaz.

Testler taklit üzerine kurulmaz: veritabanı migration uygulanmış gerçek bir
SQLite'tır, dondurulmuş workspace gerçekten dondurulur, child gerçek bir
işletim sistemi sürecidir ve lifespan gerçek ``TestClient`` üzerinden
çalıştırılır. Taklit edilen tek şey, davranışı deterministik kılmak için
executor'ın yerine konan sahte çağrılabilirdir; gerçek executor'la uçtan uca
ölçüm dosyanın ortasındadır ve atlanmaz.
"""

from __future__ import annotations

import ast
import inspect
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings, ensure_app_data_dirs
from app.main import SHUTDOWN_INCOMPLETE_MESSAGE, _PlaybookRuntime, create_app
from app.models import Job, JobStatus, JobType
from app.services.execution import worker as wk
from app.services.execution.executor import ExecutionAttempt, ExecutionOutcome
from app.services.execution.worker import PlaybookWorker, ShutdownProcessObserver
from app.services.execution.workspace import secure_filesystem_available
from tests.test_execution_executor_run import (
    PLAYBOOK,
    PLAYBOOK_PATH,
    ReportingCommand,
    build_settings,
    read_job,
    run_directories,
    seed_job,
    steal_job,
)
from tests.test_runner_process import stub_command

pytestmark = pytest.mark.skipif(
    not secure_filesystem_available(),
    reason="Descriptor-relative dosya sistemi primitive'leri bu platformda yok (ADR-017).",
)

# Testlerin beklemeye razı olduğu **üst** sınır; hiçbir testte "bu kadar sürer"
# varsayımı yoktur.
WAIT_SECONDS = 30.0

# Kapanış testlerinde child'ın uyuyacağı süre. Kapanışın bu süreyi beklemeden
# dönmesi, sonlandırma talebinin gerçekten gittiğinin kanıtıdır.
CHILD_SLEEP_SECONDS = 120.0


# --- Yardımcılar -------------------------------------------------------------


def wait_until(predicate: Callable[[], bool], *, timeout: float = WAIT_SECONDS) -> bool:
    """Koşul gerçekleşene kadar sınırlı süre bekler; asla süresiz beklemez."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def worker_threads() -> list[threading.Thread]:
    """Adı worker'a ait olan canlı thread'ler (execution döngüsü **ve** janitor)."""
    return [thread for thread in threading.enumerate() if thread.name.startswith("playbook-worker")]


def observer_threads() -> list[threading.Thread]:
    """Canlı kapanış gözlemcisi thread'leri; kapanıştan sonra hiçbiri kalmamalıdır."""
    return [
        thread for thread in threading.enumerate() if thread.name == "playbook-shutdown-observer"
    ]


class RecordingObserver:
    """``BoundedProcessObserver`` protokolünü karşılayan, yalnız sayan gözlemci."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self, request_termination: Callable[[], None]) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class FakeExecutor:
    """Executor'ın yerine konan, eşzamanlılığı **ölçen** sahte çağrılabilir.

    Gerçek executor'ın süresi ve yan etkileri deterministik değildir; bu sınıf
    yalnız "kaç çağrı, aynı anda kaç tanesi ve hangi sırayla" sorularını
    yanıtlar. Gerçek executor'la uçtan uca ölçüm ayrı testlerdedir.
    """

    def __init__(self, *, outcomes: list[ExecutionOutcome] | None = None) -> None:
        self._lock = threading.Lock()
        self._outcomes = list(outcomes or [])
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.worker_ids: list[str] = []
        self.observers: list[Any] = []
        #: Set edildiğinde çağrı bu event beklenene kadar bloke olur.
        self.block = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(
        self,
        *,
        session_factory: Callable[[], Session],
        settings: Settings,
        worker_id: str,
        lifecycle_observer: Any = None,
    ) -> ExecutionAttempt:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.worker_ids.append(worker_id)
            self.observers.append(lifecycle_observer)
            outcome = self._outcomes.pop(0) if self._outcomes else ExecutionOutcome.IDLE
        self.entered.set()
        try:
            if self.block.is_set():
                self.release.wait(WAIT_SECONDS)
            if outcome is ExecutionOutcome.IDLE:
                return ExecutionAttempt(ExecutionOutcome.IDLE)
            return ExecutionAttempt(
                ExecutionOutcome.FINISHED, job_id=str(uuid.uuid4()), status=JobStatus.SUCCESSFUL
            )
        finally:
            with self._lock:
                self.active -= 1


class RecordingSweep:
    """``sweep_stale_execution_runs`` yerine konan, turları **ölçen** sahte süpürücü.

    Gerçek janitor'ın süresi ve yan etkileri deterministik değildir; burada
    yanıtlanan sorular "kaç tur, aynı anda kaç tanesi ve turlar sırasında bir
    çalıştırma sürüyor muydu"dur. Gerçek janitor'ın kendi davranışı
    ``test_execution_reconcile.py``'dadır.
    """

    def __init__(
        self, *, hold_seconds: float = 0.0, watch: Callable[[], bool] | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._hold = hold_seconds
        self._watch = watch
        self.calls = 0
        self.active = 0
        self.max_active = 0
        #: Bir tur, aktif bir çalıştırmanın **yanında** çalıştı mı.
        self.saw_active_execution = False
        self.entered = threading.Event()
        #: Set edildiğinde tur `release` beklenene kadar bloke olur.
        self.block = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self._watch is not None and self._watch():
                self.saw_active_execution = True
        self.entered.set()
        try:
            if self.block.is_set():
                self.release.wait(WAIT_SECONDS)
            elif self._hold > 0:
                time.sleep(self._hold)
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture
def source_project(tmp_path: Path) -> Path:
    """Dondurulmadan önceki özgün project ağacı."""
    root = tmp_path / "kaynak-proje"
    root.mkdir()
    (root / PLAYBOOK_PATH).write_text(PLAYBOOK, encoding="utf-8")
    return root


@pytest.fixture
def session_factory(migrated_engine: Engine) -> Callable[[], Session]:
    """Her çağrıda **yeni** bir session üreten factory (sözleşme gereği)."""

    def factory() -> Session:
        return Session(migrated_engine, expire_on_commit=False)

    return factory


@pytest.fixture
def stopped_workers() -> Iterator[list[PlaybookWorker]]:
    """Testin açtığı worker'ların sonunda **mutlaka** durdurulmasını sağlar.

    Sızan bir worker thread'i sonraki testlerin thread sayımını bozar ve
    daemon olmadığı için süreci kapanışta bekletirdi.
    """
    workers: list[PlaybookWorker] = []
    try:
        yield workers
    finally:
        for worker in workers:
            worker.stop(join_seconds=WAIT_SECONDS)


def start_worker(
    workers: list[PlaybookWorker],
    session_factory: Callable[[], Session],
    settings: Settings,
) -> PlaybookWorker:
    worker = PlaybookWorker(session_factory=session_factory, settings=settings)
    workers.append(worker)
    worker.start()
    return worker


# --- 1. Varsayılan kapalılık -------------------------------------------------


def test_the_default_settings_start_no_worker_and_no_executor_call(
    settings: Settings, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Varsayılan ayarda ne bir worker thread'i ne de tek bir executor çağrısı doğar."""
    fake = FakeExecutor()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    prepared = build_settings(settings, command=stub_command("success"))
    ensure_app_data_dirs(prepared)
    assert prepared.playbook_worker_enabled is False

    before = worker_threads()
    with TestClient(create_app(prepared)):
        time.sleep(0.2)
        assert worker_threads() == before

    assert fake.calls == 0
    assert worker_threads() == before


def test_a_disabled_worker_does_not_change_the_api_surface(
    settings: Settings, migrated_engine: Engine
) -> None:
    """Worker açık da olsa kapalı da olsa route sayısı ve sözleşmesi aynıdır."""
    disabled = build_settings(settings, command=stub_command("success"))
    enabled = build_settings(
        settings, command=stub_command("success"), playbook_worker_enabled=True
    )
    ensure_app_data_dirs(disabled)

    def surface(active: Settings) -> set[tuple[str, frozenset[str]]]:
        return {
            (path, frozenset(getattr(route, "methods", frozenset()) or frozenset()))
            for route in create_app(active).routes
            if (path := getattr(route, "path", None)) is not None
        }

    assert surface(disabled) == surface(enabled)


def test_no_public_execution_path_is_opened_by_main_or_worker() -> None:
    """Ne worker ne de lifespan bir HTTP yüzeyi, route veya şema ekler.

    Ölçüm metin araması değil gerçek import listesidir: docstring'de geçen bir
    modül adı testi ne geçirir ne düşürür.
    """
    assert _imported_modules(wk) == {
        "__future__",
        "logging",
        "math",
        "threading",
        "time",
        "uuid",
        "collections.abc",
        "sqlalchemy.orm",
        "app.core.config",
        "app.services.execution.executor",
        "app.services.execution.reconcile",
    }

    import app.main as main_module

    source = inspect.getsource(main_module)
    tree = ast.parse(source)
    # `include_router` yalnız mevcut iki router için çağrılır; üçüncü bir çağrı
    # veya modül içinde tanımlanmış bir route dekoratörü yeni bir yüzeydir.
    included = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "include_router"
    ]
    assert len(included) == 2
    decorators = [
        decorator
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for decorator in node.decorator_list
    ]
    assert not any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr in {"get", "post", "patch", "delete", "put"}
        for decorator in decorators
    )


def test_the_ping_execution_surface_is_untouched() -> None:
    """Worker ping servislerini ne import eder ne de yeniden yazar."""
    imported = _imported_modules(wk)
    assert not any("ping" in name for name in imported)
    assert not any(name.startswith("app.services.jobs") for name in imported)


def _imported_modules(module: Any) -> set[str]:
    """Modülün import ettiği bütün modül adları."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


# --- 2. Eşzamanlılık ---------------------------------------------------------


def test_pending_jobs_run_one_at_a_time(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker işleri teker teker alır; aynı anda ikinci bir çalıştırma olmaz."""
    fake = FakeExecutor(outcomes=[ExecutionOutcome.FINISHED] * 5)
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
    )

    worker = start_worker(stopped_workers, session_factory, prepared)

    assert wait_until(lambda: fake.calls >= 6)
    assert fake.max_active == 1
    # Worker kimliği süreç ömrü boyunca tektir; her denemede yeniden üretilmez.
    assert set(fake.worker_ids) == {worker.worker_id}
    assert uuid.UUID(worker.worker_id).version == 4


def test_the_janitor_keeps_sweeping_while_the_executor_blocks(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uzun bir çalıştırma temizlik aralığına üst sınır bırakmayı **durduramaz**.

    Ölçüm doğrudan Bulgu 3'ün kendisidir: executor, aralığın birçok katı boyunca
    bloke tutulur ve bu sırada en az iki janitor turu gerçekleşir. Janitor
    execution döngüsünün içinde olsaydı sayı sıfırda kalır ve
    ``execution_run_janitor_interval_seconds`` hiçbir şeyi sınırlamazdı.

    Aynı ölçüm iki sınırı daha kapatır: turlar executor **aktifken** çalışsa da
    aktif çalıştırma sayısı birdir (janitor Job acquire etmez) ve turlar
    birbiriyle örtüşmez.
    """
    fake = FakeExecutor()
    fake.block.set()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    sweep = RecordingSweep(watch=lambda: fake.active > 0)
    monkeypatch.setattr(wk, "sweep_stale_execution_runs", sweep)

    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
        execution_run_janitor_interval_seconds=0.05,
    )

    worker = start_worker(stopped_workers, session_factory, prepared)
    assert fake.entered.wait(WAIT_SECONDS), "executor çağrısı başlamadı"

    # Executor **hâlâ bloke**: aşağıdaki turların hepsi o pencerede oldu.
    assert wait_until(lambda: sweep.calls >= 2)
    assert fake.calls == 1
    assert fake.active == 1

    # Turlar gerçekten aktif bir çalıştırmanın yanında koştu.
    assert sweep.saw_active_execution is True
    # Ama eşzamanlılık artmadı ve turlar örtüşmedi.
    assert fake.max_active == 1
    assert sweep.max_active == 1

    fake.release.set()
    assert worker.stop(join_seconds=WAIT_SECONDS) is True
    assert worker_threads() == []


def test_janitor_rounds_never_overlap_even_when_a_sweep_outlasts_the_interval(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aralıktan uzun süren bir tur, ikinci bir turun üstüne binmez.

    Turlar birikip aynı anda çalışsaydı, aynı çalışma dizini ağacını iki kez
    dolaşan iki tur doğardı; tek thread bunu yapıyla imkânsız kılar.
    """
    fake = FakeExecutor()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    sweep = RecordingSweep(hold_seconds=0.2)
    monkeypatch.setattr(wk, "sweep_stale_execution_runs", sweep)

    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
        execution_run_janitor_interval_seconds=0.01,
    )

    start_worker(stopped_workers, session_factory, prepared)

    assert wait_until(lambda: sweep.calls >= 3)
    assert sweep.max_active == 1


def test_stop_wakes_the_janitor_out_of_a_long_interval(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uzun bir temizlik aralığı kapanışı geciktirmez: bekleme event üzerindedir."""
    fake = FakeExecutor()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    sweep = RecordingSweep()
    monkeypatch.setattr(wk, "sweep_stale_execution_runs", sweep)

    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
        # İki bekleme de bilinçli olarak uzundur; kapanış ikisini de beklememeli.
        execution_run_stale_seconds=1_000.0,
        execution_run_janitor_interval_seconds=1_800.0,
    )

    worker = start_worker(stopped_workers, session_factory, prepared)
    assert wait_until(lambda: fake.calls >= 1)

    started_at = time.monotonic()
    assert worker.stop(join_seconds=WAIT_SECONDS) is True
    elapsed = time.monotonic() - started_at

    # Bekleme hedefi bir `sleep` olsaydı kapanış yarım saat sürerdi.
    assert elapsed < 5.0
    assert sweep.calls == 0
    assert worker_threads() == []


def test_a_janitor_failure_does_not_kill_the_loop(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Süpürme arızası döngüyü sessizce durdurmaz; Job işlemeye devam edilir."""
    fake = FakeExecutor()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)

    def exploding_sweep(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk arizasi")

    monkeypatch.setattr(wk, "sweep_stale_execution_runs", exploding_sweep)
    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
        execution_run_janitor_interval_seconds=0.05,
    )

    start_worker(stopped_workers, session_factory, prepared)

    before = fake.calls
    assert wait_until(lambda: fake.calls > before + 2)


def test_an_executor_failure_backs_off_instead_of_tight_looping(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arıza döngüyü ne öldürür ne de saniyede binlerce denemeye çevirir."""
    calls = 0

    def exploding(**kwargs: Any) -> ExecutionAttempt:
        nonlocal calls
        calls += 1
        raise OSError("veritabani dustu")

    monkeypatch.setattr(wk, "execute_next_playbook_job", exploding)
    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
    )

    start_worker(stopped_workers, session_factory, prepared)

    # Döngü ölmemeli: en az birkaç deneme yapılmalı.
    assert wait_until(lambda: calls >= 2)
    # Ama tight loop de olmamalı: üstel gecikme 0.05 → 0.1 → 0.2 ... olduğu için
    # yarım saniyede yüzlerce deneme görülemez.
    time.sleep(0.5)
    assert calls < 40


# --- 3. Boşta bekleme ve durdurma --------------------------------------------


def test_idle_polling_does_not_busy_spin_and_stop_wakes_it_immediately(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boşta bekleme meşguliyet döngüsü değildir ve stop beklemeyi anında keser."""
    fake = FakeExecutor()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        # Poll aralığı bilinçli olarak uzundur: kapanış bunu beklemeden dönmeli.
        playbook_worker_poll_seconds=30.0,
    )

    worker = start_worker(stopped_workers, session_factory, prepared)
    assert wait_until(lambda: fake.calls >= 1)
    time.sleep(0.3)

    # 0.3 saniyede 30 saniyelik aralığın ikinci turu gelmez.
    assert fake.calls == 1

    started_at = time.monotonic()
    assert worker.stop(join_seconds=WAIT_SECONDS) is True
    elapsed = time.monotonic() - started_at

    # Bekleme hedefi bir `sleep` olsaydı kapanış 30 saniye sürerdi.
    assert elapsed < 5.0
    assert worker_threads() == []


def test_no_job_is_acquired_after_stop(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durdurma talebi konduktan sonra yeni bir acquire denemesi yapılmaz."""
    fake = FakeExecutor()
    fake.block.set()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.01,
    )

    worker = start_worker(stopped_workers, session_factory, prepared)
    assert fake.entered.wait(WAIT_SECONDS)
    assert fake.calls == 1

    # Çalıştırma sürerken durdurma istenir; sonra bloke çağrı serbest bırakılır.
    stop_thread = threading.Thread(target=lambda: worker.stop(join_seconds=WAIT_SECONDS))
    stop_thread.start()
    time.sleep(0.2)
    fake.release.set()
    stop_thread.join(WAIT_SECONDS)

    assert stop_thread.is_alive() is False
    time.sleep(0.2)
    assert fake.calls == 1
    assert worker_threads() == []


def test_a_worker_is_single_use(settings: Settings, session_factory: Callable[[], Session]) -> None:
    """İkinci bir ``start``, aynı kimlikle iki döngü açmanın yolu olurdu."""
    prepared = build_settings(settings, command=stub_command("success"))
    worker = PlaybookWorker(session_factory=session_factory, settings=prepared)

    assert worker.stop() is True
    with pytest.raises(RuntimeError):
        worker.start()


def test_stop_stays_false_until_the_execution_thread_really_ends(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bütçe dolduğunda ``stop`` ``False`` döner ve **tekrarı da** ``False`` döner.

    Ölçülen tam olarak Bulgu 1'dir: canlı thread'in referansı ilk timeout'ta
    düşürülseydi ikinci ``stop`` bekleyecek bir şey bulamaz ve hâlâ çalışan bir
    worker için ``True`` dönerdi. ``True`` yalnız thread'in gerçekten
    sonlandığı kanıtlandığında görülür ve o noktada geride ne worker ne de
    gözlemci thread'i kalır.
    """
    fake = FakeExecutor()
    fake.block.set()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
    )

    worker = start_worker(stopped_workers, session_factory, prepared)
    assert fake.entered.wait(WAIT_SECONDS), "executor çağrısı başlamadı"

    assert worker.stop(join_seconds=0.05) is False
    live = [thread for thread in worker_threads() if thread.name == "playbook-worker"]
    assert len(live) == 1 and live[0].is_alive()

    # İkinci çağrı **aynı** canlı thread'i gözlemler.
    assert worker.stop(join_seconds=0.05) is False
    assert [thread for thread in worker_threads() if thread.name == "playbook-worker"] == live

    fake.release.set()

    assert worker.stop(join_seconds=WAIT_SECONDS) is True
    assert worker_threads() == []
    assert wait_until(lambda: observer_threads() == [])


def test_stop_stays_false_while_a_janitor_sweep_blocks(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bekleme yalnız execution döngüsü için değildir: janitor da beklenir.

    Kapanış janitor'ı beklemeseydi, süpürme sırasında kapanan bir süreç
    yarım kalmış bir ağaç temizliği bırakır ve ``stop`` bunu kapanmış sayardı.
    """
    fake = FakeExecutor()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    sweep = RecordingSweep()
    sweep.block.set()
    monkeypatch.setattr(wk, "sweep_stale_execution_runs", sweep)
    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
        execution_run_janitor_interval_seconds=0.05,
    )

    worker = start_worker(stopped_workers, session_factory, prepared)
    assert sweep.entered.wait(WAIT_SECONDS), "janitor turu başlamadı"

    assert worker.stop(join_seconds=0.05) is False
    assert worker.stop(join_seconds=0.05) is False
    janitor = [thread for thread in worker_threads() if thread.name == "playbook-worker-janitor"]
    assert len(janitor) == 1 and janitor[0].is_alive()

    sweep.release.set()

    assert worker.stop(join_seconds=WAIT_SECONDS) is True
    assert worker_threads() == []


def test_concurrent_stops_never_disagree_about_a_live_worker(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eşzamanlı ``stop`` çağrıları aynı gerçek thread'i gözlemler.

    Biri ``True`` diğeri ``False`` dönebilseydi, kapanışı ilk cevaba göre karar
    veren çağıran hâlâ çalışan bir worker'ı kapanmış sayabilirdi.
    """
    fake = FakeExecutor()
    fake.block.set()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
    )

    worker = start_worker(stopped_workers, session_factory, prepared)
    assert fake.entered.wait(WAIT_SECONDS), "executor çağrısı başlamadı"

    lock = threading.Lock()
    outcomes: list[bool] = []

    def _stop() -> None:
        outcome = worker.stop(join_seconds=0.05)
        with lock:
            outcomes.append(outcome)

    callers = [threading.Thread(target=_stop) for _ in range(4)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(WAIT_SECONDS)

    assert outcomes == [False, False, False, False]

    fake.release.set()
    assert worker.stop(join_seconds=WAIT_SECONDS) is True
    assert worker_threads() == []


# --- 4. Kapanış gözlemcisi ---------------------------------------------------


def test_the_shutdown_observer_requests_termination_when_stop_is_already_set() -> None:
    """Kapanış çoktan istenmişse child başlar başlamaz sonlandırma istenir."""
    stop = threading.Event()
    stop.set()
    observer = ShutdownProcessObserver(stop)
    requested = threading.Event()

    observer.start(requested.set)

    assert requested.is_set() is True
    assert observer.termination_requested is True
    # Hiç thread açılmamıştır; `stop` yine de güvenle çağrılabilir.
    observer.stop()
    observer.stop()
    assert observer.watch_failed is False


def test_the_shutdown_observer_requests_termination_when_stop_arrives_later() -> None:
    """Çalışırken gelen kapanış talebi child'a taşınır."""
    stop = threading.Event()
    observer = ShutdownProcessObserver(stop, tick_seconds=0.01)
    requested = threading.Event()

    observer.start(requested.set)
    assert requested.is_set() is False

    stop.set()

    assert requested.wait(WAIT_SECONDS) is True
    observer.stop()
    assert observer.termination_requested is True


def test_the_shutdown_observer_stops_idempotently_without_leaking_threads() -> None:
    """``stop`` iki kez çağrılabilir ve izleme thread'i geride kalmaz."""
    stop = threading.Event()
    observer = ShutdownProcessObserver(stop, tick_seconds=0.01)
    before = {thread.name for thread in threading.enumerate()}

    observer.start(lambda: None)
    observer.stop()
    observer.stop()

    assert observer.watch_failed is False
    assert wait_until(
        lambda: (
            not any(
                thread.name == "playbook-shutdown-observer" and thread.name not in before
                for thread in threading.enumerate()
            )
        )
    )
    # İkinci bir `start` tek kullanımlık sözleşmesini bozardı.
    with pytest.raises(RuntimeError):
        observer.start(lambda: None)


def test_the_shutdown_observer_never_sets_the_stop_event() -> None:
    """Gözlemci kapanış event'ini **okur**; hiçbir koşulda set etmez."""
    stop = threading.Event()
    observer = ShutdownProcessObserver(stop, tick_seconds=0.01)

    observer.start(lambda: None)
    time.sleep(0.1)
    observer.stop()

    assert stop.is_set() is False


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_the_shutdown_observer_rejects_meaningless_intervals(bad: float) -> None:
    """Sıfır bir tur meşguliyet döngüsü, sonsuz bir tur ise izlememek olurdu."""
    with pytest.raises(ValueError):
        ShutdownProcessObserver(threading.Event(), tick_seconds=bad)
    with pytest.raises(ValueError):
        ShutdownProcessObserver(threading.Event(), join_seconds=bad)


# --- 5. Gerçek child ile kapanış ---------------------------------------------


def test_lifespan_shutdown_terminates_and_reaps_a_running_child(
    db_session: Session,
    settings: Settings,
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Kapanış, çalışan child'ı sonlandırır ve **reap edilmeden** tamamlanmaz.

    Ölçüm dolaylı değildir: child gerçek bir süreçtir, kendi pid'ini bildirir ve
    shutdown döndükten sonra o pid ``waitpid`` ile artık bu sürecin çocuğu
    olmadığı için ``ECHILD`` verir. Süreye de bakılır — child iki dakika uyuyacak
    biçimde başlatılır, dolayısıyla kısa sürede dönen bir shutdown ancak
    sonlandırma talebi gerçekten gittiyse mümkündür.
    """
    child = ReportingCommand(tmp_path, "sleep", sleep_seconds=CHILD_SLEEP_SECONDS)
    runtime = build_settings(
        settings,
        command=child.command,
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
        playbook_runner_timeout_seconds=CHILD_SLEEP_SECONDS * 2,
        execution_run_stale_seconds=CHILD_SLEEP_SECONDS * 4,
    )
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    with TestClient(create_app(runtime)):
        assert child.wait_for_start(), "child başlamadı"
        started_at = time.monotonic()

    elapsed = time.monotonic() - started_at

    # Child'ın kendi uykusu beklenmedi: sonlandırma talebi gerçekten gitti.
    assert elapsed < CHILD_SLEEP_SECONDS / 2
    # Ve shutdown "unutarak" değil, reap ederek bitti.
    child.assert_reaped()
    assert worker_threads() == []
    # Executor terminal geçişi ve run directory temizliğini tamamladı.
    row = read_job(migrated_engine, job_id)
    assert row.status in (JobStatus.FAILED, JobStatus.SUCCESSFUL)
    assert row.finished_at is not None
    assert run_directories(runtime) == []


def test_an_enabled_worker_runs_a_real_pending_job_end_to_end(
    db_session: Session, settings: Settings, migrated_engine: Engine, source_project: Path
) -> None:
    """Açık worker, kuyruktaki gerçek bir Job'ı gerçekten çalıştırıp bitirir."""
    runtime = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
    )
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    with TestClient(create_app(runtime)):
        assert wait_until(
            lambda: read_job(migrated_engine, job_id).status is JobStatus.SUCCESSFUL,
        )

    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.SUCCESSFUL
    assert row.artifact_path == f"jobs/{job_id}/result.json"
    assert run_directories(runtime) == []
    assert worker_threads() == []


# --- 6. Gözlemci bileşimi ----------------------------------------------------


def test_the_raw_budget_survives_an_added_lifecycle_observer(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
) -> None:
    """Dışarıdan gözlemci verilmesi süreç katmanının raw bütçesini kapatmaz.

    Zincir ``(raw bütçesi, (kira, yaşam döngüsü))`` biçiminde kurulur; bu test
    zincirin **en dıştaki** halkasının hâlâ uygulandığını ölçer.
    """
    from app.services.execution.executor import execute_next_playbook_job

    runtime = build_settings(
        settings,
        command=stub_command("flood-raw", size_bytes=400_000, sleep_seconds=5),
        playbook_runner_max_raw_bytes=200_000,
    )
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)
    observer = RecordingObserver()

    attempt = execute_next_playbook_job(
        session_factory=session_factory,
        settings=runtime,
        worker_id=str(uuid.uuid4()),
        lifecycle_observer=observer,
    )

    # Raw bütçesi devre dışı kalmadı.
    assert attempt.error_code == "result_limit_exceeded"
    assert read_job(migrated_engine, job_id).status is JobStatus.FAILED
    # Yaşam döngüsü gözlemcisi de kuruldu ve durduruldu.
    assert observer.started == 1
    assert observer.stopped >= 1
    assert run_directories(runtime) == []


def test_the_lease_observer_survives_an_added_lifecycle_observer(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Yaşam döngüsü gözlemcisi eklenmesi kira gözlemcisini kapatmaz.

    Satır çalışma sırasında başka bir worker'a geçirilir. Kira gözlemcisi hâlâ
    zincirde olduğu için heartbeat kaybı görülür, sonlandırma talep edilir ve
    kısmi çıktı **yayımlanmaz** — R1-V3C1C2B2B'deki davranışın birebir aynısı.
    """
    from app.services.execution.executor import execute_next_playbook_job

    child = ReportingCommand(tmp_path, "sleep", sleep_seconds=30)
    runtime = build_settings(settings, command=child.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)
    observer = RecordingObserver()

    result: dict[str, ExecutionAttempt] = {}

    def _work() -> None:
        result["attempt"] = execute_next_playbook_job(
            session_factory=session_factory,
            settings=runtime,
            worker_id=str(uuid.uuid4()),
            lifecycle_observer=observer,
        )

    runner = threading.Thread(target=_work)
    runner.start()
    try:
        assert child.wait_for_start(), "child başlamadı"
        thief = steal_job(migrated_engine, job_id)
    finally:
        runner.join(timeout=WAIT_SECONDS)
    assert not runner.is_alive(), "çalıştırma zamanında bitmedi"

    # Kira gözlemcisi devre dışı kalmadı: kayıp görüldü ve süreç kesildi.
    assert result["attempt"].outcome is ExecutionOutcome.OWNERSHIP_LOST
    assert read_job(migrated_engine, job_id).worker_id == thief
    # Yaşam döngüsü gözlemcisi de aynı zincirde çalıştı.
    assert observer.started == 1
    assert observer.stopped >= 1
    assert run_directories(runtime) == []


def test_a_shutdown_observer_terminates_a_child_inside_the_full_chain(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Gerçek kapanış gözlemcisi, kira ve raw gözlemcileriyle **birlikte** keser.

    Üçü de zincirdeyken kapanış event'i set edilir; child sonlandırılır, reap
    edilir ve Job terminal olur. Yani üçüncü gözlemcinin eklenmesi ne kirayı ne
    raw bütçesini kapatır, ne de kendisi diğerleri yüzünden etkisiz kalır.
    """
    from app.services.execution.executor import execute_next_playbook_job

    child = ReportingCommand(tmp_path, "sleep", sleep_seconds=CHILD_SLEEP_SECONDS)
    runtime = build_settings(
        settings,
        command=child.command,
        playbook_runner_timeout_seconds=CHILD_SLEEP_SECONDS * 2,
        execution_run_stale_seconds=CHILD_SLEEP_SECONDS * 4,
    )
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)
    stop = threading.Event()
    observer = ShutdownProcessObserver(stop, tick_seconds=0.01)

    result: dict[str, ExecutionAttempt] = {}

    def _work() -> None:
        result["attempt"] = execute_next_playbook_job(
            session_factory=session_factory,
            settings=runtime,
            worker_id=str(uuid.uuid4()),
            lifecycle_observer=observer,
        )

    runner = threading.Thread(target=_work)
    runner.start()
    try:
        assert child.wait_for_start(), "child başlamadı"
        started_at = time.monotonic()
        stop.set()
    finally:
        runner.join(timeout=WAIT_SECONDS)
    assert not runner.is_alive(), "çalıştırma zamanında bitmedi"

    assert time.monotonic() - started_at < CHILD_SLEEP_SECONDS / 2
    assert observer.termination_requested is True
    child.assert_reaped()
    assert read_job(migrated_engine, job_id).status is JobStatus.FAILED
    assert run_directories(runtime) == []


def test_the_one_shot_call_without_an_observer_is_unchanged(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
) -> None:
    """Gözlemcisiz çağrı R1-V3C1C2B2B'deki yolunu birebir korur."""
    from app.services.execution.executor import execute_next_playbook_job

    runtime = build_settings(settings, command=stub_command("success"))
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    attempt = execute_next_playbook_job(
        session_factory=session_factory, settings=runtime, worker_id=str(uuid.uuid4())
    )

    assert attempt.outcome is ExecutionOutcome.FINISHED
    assert read_job(migrated_engine, job_id).status is JobStatus.SUCCESSFUL
    assert run_directories(runtime) == []


# --- 7. Açılış sırası --------------------------------------------------------


def test_startup_runs_reconciliation_then_janitor_then_the_worker(
    settings: Settings, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sıra sabittir: C2A (commit + session kapanışı) → C2B → worker.

    Session'ın gerçekten kapandığı da ölçülür: janitor başladığında C2A'nın
    session'ı açık bir transaction taşımamalıdır. Aksi hâlde pahalı dosya
    sistemi temizliği boyunca bir SQLite yazma kilidi elde tutulurdu.
    """
    import app.main as main_module

    order: list[str] = []
    captured: dict[str, Session] = {}

    def fake_reconcile(session: Session, *, now: datetime | None = None) -> int:
        order.append("reconcile")
        captured["session"] = session
        # Gerçek bir sorgu açılır: aksi hâlde "transaction kapandı" iddiası hiç
        # açılmamış bir transaction üzerinden ölçülmüş olurdu.
        session.execute(select(Job.id)).all()
        assert session.in_transaction() is True
        assert now is not None and now.tzinfo is not None
        captured["now"] = now  # type: ignore[assignment]
        return 0

    def fake_sweep(*args: Any, now: datetime | None = None, **kwargs: Any) -> Any:
        order.append("sweep")
        # C2A'nın session'ı bu noktada tamamen kapanmış olmalıdır.
        assert captured["session"].in_transaction() is False
        # Ve iki bileşen aynı karar anını görmelidir.
        assert now is captured["now"]
        return None

    class FakeWorker:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def start(self) -> None:
            order.append("worker")

        def stop(self, **kwargs: Any) -> bool:
            order.append("worker-stop")
            return True

    monkeypatch.setattr(main_module, "reconcile_stale_playbook_jobs", fake_reconcile)
    monkeypatch.setattr(main_module, "sweep_stale_execution_runs", fake_sweep)
    monkeypatch.setattr(main_module, "PlaybookWorker", FakeWorker)

    prepared = build_settings(
        settings, command=stub_command("success"), playbook_worker_enabled=True
    )
    ensure_app_data_dirs(prepared)

    with TestClient(create_app(prepared)):
        pass

    assert order == ["reconcile", "sweep", "worker", "worker-stop"]


@pytest.mark.parametrize("failing", ["reconcile_stale_playbook_jobs", "sweep_stale_execution_runs"])
def test_a_recovery_failure_keeps_the_worker_closed(
    settings: Settings, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """C2A veya C2B arıza verirse worker **başlatılmaz** (fail-closed).

    Arızayı "0 satır uzlaştırıldı" veya "kök boş" diye okuyup ardından
    çalıştırmaya başlamak, hâlâ ``running`` duran bir satırın üstüne ikinci bir
    çalıştırma açmanın yolu olurdu.
    """
    import app.main as main_module

    started: list[str] = []

    def exploding(*args: Any, **kwargs: Any) -> Any:
        raise OSError("toparlama arizasi")

    class FakeWorker:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def start(self) -> None:
            started.append("worker")

        def stop(self, **kwargs: Any) -> bool:
            return True

    monkeypatch.setattr(main_module, failing, exploding)
    monkeypatch.setattr(main_module, "PlaybookWorker", FakeWorker)

    prepared = build_settings(
        settings, command=stub_command("success"), playbook_worker_enabled=True
    )
    ensure_app_data_dirs(prepared)

    with TestClient(create_app(prepared)):
        pass

    assert started == []
    assert worker_threads() == []


def test_a_recovery_failure_still_produces_a_usable_app(
    settings: Settings, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Toparlama arızası uygulamayı açılmaktan alıkoymaz; yalnız worker doğmaz."""
    import app.main as main_module

    def exploding(*args: Any, **kwargs: Any) -> Any:
        raise OSError("toparlama arizasi")

    monkeypatch.setattr(main_module, "sweep_stale_execution_runs", exploding)
    prepared = build_settings(
        settings, command=stub_command("success"), playbook_worker_enabled=True
    )
    ensure_app_data_dirs(prepared)

    with TestClient(create_app(prepared)) as client:
        assert client.get("/health").status_code == 200

    assert worker_threads() == []


# --- 8. Açılış toparlamasının gerçek etkisi ----------------------------------


def seed_running_job(
    session: Session,
    runtime: Settings,
    source_project: Path,
    *,
    lease_expires_at: datetime,
) -> str:
    """``running`` ve kirası verilen ana kadar süren bir PLAYBOOK Job'ı kurar.

    Sahiplik alanları eksiksiz yazılır: ``running`` bir PLAYBOOK satırının
    sahipsiz olamayacağı ve kirasının heartbeat'ini aşması gerektiği
    veritabanının kendi CHECK kısıtlarıyla zorunludur.
    """
    job_id = seed_job(session, runtime, source_project)
    started = datetime.now(UTC) - timedelta(hours=2)
    session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status=JobStatus.RUNNING,
            worker_id=str(uuid.uuid4()),
            heartbeat_at=lease_expires_at - timedelta(minutes=1),
            lease_expires_at=lease_expires_at,
            started_at=started,
        )
    )
    session.commit()
    return job_id


def make_run_directory(runtime: Settings, job_id: str, *, age_seconds: float) -> Path:
    """Kökün altına verilen yaşta gerçek bir çalışma dizini açar."""
    path = runtime.resolve_execution_run_dir() / job_id
    path.mkdir(parents=True)
    path.chmod(0o700)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_startup_terminalizes_an_expired_job_and_then_collects_its_directory(
    db_session: Session, settings: Settings, migrated_engine: Engine, source_project: Path
) -> None:
    """Kirası dolmuş satır önce terminal olur; **ardından** dizini toplanabilir.

    Bu, sıranın neden zorunlu olduğunun doğrudan ölçümüdür: C2B ``running``
    görünen her Job'ın dizinini kirasına bakmadan korur, dolayısıyla C2A önce
    çalışmasaydı bu dizin hiçbir zaman toplanamazdı.
    """
    runtime = build_settings(
        settings,
        command=stub_command("success"),
        execution_run_stale_seconds=100.0,
    )
    ensure_app_data_dirs(runtime)
    job_id = seed_running_job(
        db_session, runtime, source_project, lease_expires_at=datetime.now(UTC) - timedelta(hours=1)
    )
    run_dir = make_run_directory(runtime, job_id, age_seconds=1_000.0)

    with TestClient(create_app(runtime)):
        pass

    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.error_code == "interrupted_by_restart"
    assert run_dir.exists() is False


def test_startup_preserves_the_directory_of_a_live_job(
    db_session: Session, settings: Settings, migrated_engine: Engine, source_project: Path
) -> None:
    """Kirası süren bir Job'ın çalışma alanı altından silinmez."""
    runtime = build_settings(
        settings,
        command=stub_command("success"),
        execution_run_stale_seconds=100.0,
    )
    ensure_app_data_dirs(runtime)
    job_id = seed_running_job(
        db_session, runtime, source_project, lease_expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    run_dir = make_run_directory(runtime, job_id, age_seconds=1_000.0)

    with TestClient(create_app(runtime)):
        pass

    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.RUNNING
    assert run_dir.exists() is True


def test_startup_recovery_runs_even_when_the_worker_is_disabled(
    db_session: Session, settings: Settings, migrated_engine: Engine, source_project: Path
) -> None:
    """Worker kapalıyken de toparlama uygulanır; yalnız arka plan döngüsü doğmaz."""
    runtime = build_settings(
        settings,
        command=stub_command("success"),
        execution_run_stale_seconds=100.0,
    )
    ensure_app_data_dirs(runtime)
    assert runtime.playbook_worker_enabled is False
    job_id = seed_running_job(
        db_session, runtime, source_project, lease_expires_at=datetime.now(UTC) - timedelta(hours=1)
    )
    run_dir = make_run_directory(runtime, job_id, age_seconds=1_000.0)

    with TestClient(create_app(runtime)):
        pass

    assert read_job(migrated_engine, job_id).status is JobStatus.FAILED
    assert run_dir.exists() is False
    assert worker_threads() == []


def test_a_pending_job_is_untouched_while_the_worker_is_disabled(
    db_session: Session, settings: Settings, migrated_engine: Engine, source_project: Path
) -> None:
    """Kapalı worker kuyruğa dokunmaz: ``pending`` satır ``pending`` kalır."""
    runtime = build_settings(settings, command=stub_command("success"))
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    with TestClient(create_app(runtime)):
        time.sleep(0.3)

    with Session(migrated_engine) as observer:
        status = observer.execute(select(Job.status).where(Job.id == job_id)).scalar_one()
    assert status is JobStatus.PENDING
    assert run_directories(runtime) == []
    assert (
        db_session.execute(
            select(Job.id).where(Job.job_type == JobType.PLAYBOOK, Job.status == JobStatus.RUNNING)
        ).first()
        is None
    )


# --- 9. Kapanış sahipliği ----------------------------------------------------


class CountingEngine:
    """``Engine`` yerine konan, yalnız ``dispose`` sayan nesne."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.disposed = 0
        self._order = order

    def dispose(self) -> None:
        self.disposed += 1
        if self._order is not None:
            self._order.append("dispose")


class ControllableWorker:
    """``PlaybookWorker`` yerine konan, ``stop`` cevabı kontrol edilebilir worker.

    Gerçek thread'lerin ``stop`` sözleşmesi (timeout → tekrar bekleme → ``True``)
    bu dosyanın 3. bölümünde gerçek worker üzerinde ölçülür. Burada ölçülen ayrı
    bir şeydir: **çağıranın** o cevaba nasıl davrandığı.
    """

    def __init__(self, *, stops: bool = True, order: list[str] | None = None) -> None:
        self.stops = stops
        self.stop_calls = 0
        self.start_calls = 0
        self._order = order

    def start(self) -> None:
        self.start_calls += 1

    def stop(self, **kwargs: Any) -> bool:
        self.stop_calls += 1
        if self._order is not None:
            self._order.append("worker-stop")
        return self.stops


def test_shutdown_keeps_the_engine_and_fails_visibly_while_the_worker_may_run() -> None:
    """Durduğu kanıtlanamayan bir worker'ın altından engine çekilmez ve kapanış **başarısız olur**.

    Sessizce dönmek, FastAPI lifespan'inin shutdown'ı tamamlanmış göstermesi
    olurdu: worker ve muhtemel child hâlâ canlıyken süreç kapanmış sayılırdı.
    """
    engine = CountingEngine()
    worker = ControllableWorker(stops=False)
    runtime = _PlaybookRuntime(engine=cast(Engine, engine), worker=cast(PlaybookWorker, worker))

    with pytest.raises(RuntimeError) as raised:
        runtime.shutdown()

    assert str(raised.value) == SHUTDOWN_INCOMPLETE_MESSAGE
    assert engine.disposed == 0
    assert worker.stop_calls == 1
    # Mesaj sabittir: path, DSN, Job kimliği veya exception metni taşımaz.
    assert "/" not in SHUTDOWN_INCOMPLETE_MESSAGE
    assert "sqlite" not in SHUTDOWN_INCOMPLETE_MESSAGE.lower()


def test_shutdown_releases_the_engine_exactly_once_and_after_the_worker() -> None:
    """Sıra tersine çevrilemez: önce worker durur, **sonra** havuz kapanır."""
    order: list[str] = []
    engine = CountingEngine(order)
    worker = ControllableWorker(stops=True, order=order)
    runtime = _PlaybookRuntime(engine=cast(Engine, engine), worker=cast(PlaybookWorker, worker))

    runtime.shutdown()

    assert order == ["worker-stop", "dispose"]
    assert engine.disposed == 1


def test_a_shutdown_can_be_retried_after_the_worker_is_released() -> None:
    """İlk timeout kapanışı bitirmez; worker durunca ikinci deneme gerçekten kapatır."""
    order: list[str] = []
    engine = CountingEngine(order)
    worker = ControllableWorker(stops=False, order=order)
    runtime = _PlaybookRuntime(engine=cast(Engine, engine), worker=cast(PlaybookWorker, worker))

    with pytest.raises(RuntimeError):
        runtime.shutdown()
    assert engine.disposed == 0

    worker.stops = True
    runtime.shutdown()

    assert order == ["worker-stop", "worker-stop", "dispose"]
    assert engine.disposed == 1


def test_lifespan_shutdown_does_not_report_success_while_the_worker_runs(
    settings: Settings, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan, durmayan bir worker'ın üstüne kapanışı tamamlanmış göstermez."""
    import app.main as main_module

    worker = ControllableWorker(stops=False)
    monkeypatch.setattr(main_module, "PlaybookWorker", lambda **kwargs: worker)
    prepared = build_settings(
        settings, command=stub_command("success"), playbook_worker_enabled=True
    )
    ensure_app_data_dirs(prepared)

    with pytest.raises(RuntimeError, match=SHUTDOWN_INCOMPLETE_MESSAGE):
        with TestClient(create_app(prepared)) as client:
            assert client.get("/health").status_code == 200

    assert worker.stop_calls == 1


# --- 10. Worker açılamadığında -----------------------------------------------


# Önceki thread'in entrypoint'e girdiği görüldükten sonra, arıza üretilmeden
# önce ona bırakılan süre. Bu pay ölçümün **çekirdeğidir**: yayımlanma bariyeri
# olmayan bir uygulamada bu süre, ilk thread'in bir Job acquire edip
# executor'ı çağırmasına fazlasıyla yeter. Yani "0 çağrı" sonucu, thread'in
# fırsat bulamamış olmasından değil, bariyerin çalışmasından gelir.
FIRST_THREAD_SETTLE_SECONDS = 0.2


class DeferredFailingThread:
    """``start`` çağrısında düşen sahte thread — ama **önce** sırasını bekleyerek.

    Beklemek bilinçlidir: önceki thread'in gerçekten işletim sistemi üzerinde
    başlayıp entrypoint'ine girdiği kanıtlanmadan arıza üretilseydi, ölçülen şey
    "yayım öncesi yan etki yok" değil "thread hiç çalışmadı" olurdu.
    """

    def __init__(self, name: str, *, after: threading.Event | None) -> None:
        self.name = name
        self._after = after

    def start(self) -> None:
        if self._after is not None:
            assert self._after.wait(WAIT_SECONDS), "önceki thread entrypoint'e girmedi"
            time.sleep(FIRST_THREAD_SETTLE_SECONDS)
        raise RuntimeError("can't start new thread")

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        return None


class DelayedStartThread:
    """``start``'ı geciktiren ama sonunda **başarıyla** başlatan sarmalayıcı.

    Gecikme, işletim sisteminin ikinci thread'i geç vermesini taklit eder. Bu
    yolun sonu başarıdır: ``start`` başarıyla dönüyorsa iki thread de çalışıyor
    olmalıdır. Bariyerde bir timeout olsaydı, yeterince gecikmiş bir başlangıçta
    ilk thread sessizce ölür ve worker açık görünüp hiçbir Job işlemezdi.
    """

    def __init__(
        self, thread: threading.Thread, *, after: threading.Event | None, delay_seconds: float
    ) -> None:
        self._thread = thread
        self._after = after
        self._delay = delay_seconds

    @property
    def name(self) -> str:
        return self._thread.name

    def start(self) -> None:
        if self._after is not None:
            assert self._after.wait(WAIT_SECONDS), "önceki thread entrypoint'e girmedi"
        time.sleep(self._delay)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)


class BarrierThreadingShim:
    """worker.py'nin gördüğü ``threading``; **konuma** göre bir ``start``'ı bozar.

    Bozulan thread adına göre değil, oluşturulma sırasına göre seçilir: ölçülen
    şey hangi döngünün ikinci açıldığı değildir. Sözleşme simetriktir — hangisi
    ikinci açılırsa açılsın, yayım tamamlanmadan ne executor ne de janitor yan
    etkisi doğmalıdır. Ada bağlanan bir test, sırayı ters çevirmenin "çözüm"
    sayılmasına izin verirdi.

    ``delay_seconds`` verilirse seçilen ``start`` düşmez, yalnız gecikir ve
    sonunda başarılı olur.

    Patch bilinçli olarak modül referansı üzerinden yapılır: gerçek
    ``threading.Thread``'i global olarak değiştirmek, testin süresi boyunca
    süreçteki her thread oluşturmasını etkilerdi.
    """

    def __init__(self, *, fail_at: int, delay_seconds: float | None = None) -> None:
        self._fail_at = fail_at
        self._delay = delay_seconds
        self.created: list[str] = []
        self.entered: dict[str, threading.Event] = {}
        self._previous: threading.Event | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(threading, name)

    def Thread(self, *, target: Any, name: str, daemon: bool) -> Any:
        self.created.append(name)
        entered = threading.Event()
        self.entered[name] = entered

        def entrypoint() -> None:
            # Entrypoint'e **girildiği** an işaretlenir; worker'ın kendi ilk
            # adımı bundan sonra gelir.
            entered.set()
            target()

        if len(self.created) == self._fail_at and self._delay is None:
            return DeferredFailingThread(name, after=self._previous)

        thread = threading.Thread(target=entrypoint, name=name, daemon=daemon)
        if len(self.created) == self._fail_at:
            assert self._delay is not None
            return DelayedStartThread(thread, after=self._previous, delay_seconds=self._delay)

        self._previous = entered
        return thread


@pytest.mark.parametrize("fail_at", [1, 2])
def test_a_failed_start_publishes_nothing_and_attempts_no_execution(
    settings: Settings,
    session_factory: Callable[[], Session],
    monkeypatch: pytest.MonkeyPatch,
    fail_at: int,
) -> None:
    """Başarısız bir başlangıç **tek bir** acquire veya süpürme denemesi üretmez.

    Thread sızıntısını kapatmak yetmez. İki thread aynı anda başlatılamaz;
    bariyer olmasaydı ilk thread, ikinci ``Thread.start`` düşmeden önce bir Job
    alıp çalıştırmaya başlayabilirdi — ``start`` çağırana **başarısız** dönerken
    arkada bir çalıştırma sürerdi.

    Ölçüm iki konum için de yapılır: hangi thread ikinci açılırsa açılsın sonuç
    aynı olmalıdır (yalnız sırayı ters çevirmek bir güvence değildir).
    """
    shim = BarrierThreadingShim(fail_at=fail_at)
    monkeypatch.setattr(wk, "threading", shim)
    fake = FakeExecutor()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    sweep = RecordingSweep()
    monkeypatch.setattr(wk, "sweep_stale_execution_runs", sweep)
    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        # İki aralık da bilinçli olarak çok kısadır: yayım gelseydi ikisi de
        # anında bir yan etki üretirdi.
        playbook_worker_poll_seconds=0.05,
        execution_run_janitor_interval_seconds=0.05,
    )
    worker = PlaybookWorker(session_factory=session_factory, settings=prepared)

    # Asıl arıza gölgelenmeden yukarı taşınır.
    with pytest.raises(RuntimeError):
        worker.start()

    # Yayımlanmamış bir worker hiçbir şey denemedi.
    assert fake.calls == 0
    assert sweep.calls == 0
    # Başlamış olan thread geri alındı: ne canlı bir thread ne de sahipsiz bir
    # döngü kaldı.
    assert wait_until(lambda: worker_threads() == [])
    assert observer_threads() == []
    assert worker.stop(join_seconds=WAIT_SECONDS) is True
    # Tek kullanımlık sözleşme korunur: düşmüş bir `start` yeniden denenemez.
    with pytest.raises(RuntimeError):
        worker.start()
    assert fake.calls == 0
    assert sweep.calls == 0


# --- 11. Gecikmiş ama başarılı yayım -----------------------------------------

# İkinci `Thread.start`'ın geciktiği süre. Ölçüm süreye değil sonuca bakar:
# gecikme ne olursa olsun **başarılı** bir `start` iki çalışan thread demektir.
DELAYED_START_SECONDS = 0.3


class RecordingEvent:
    """``threading.Event`` yerine konan, ``wait`` çağrısının timeout'unu kaydeden event.

    Kaynak metninde ``wait()`` aramak yerine **çağrının kendisi** ölçülür: bir
    sabitin adı değişse ya da timeout başka bir yoldan geçirilse metin araması
    bunu göremezdi.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_timeouts.append(timeout)
        return self._event.wait(timeout)

    def set(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()


def test_a_delayed_but_successful_start_leaves_both_threads_working(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """İkinci ``Thread.start`` gecikip sonunda başarılırsa **iki** thread de çalışır.

    Bariyerde bir timeout olsaydı bu yol sessizce bozulurdu: ilk thread süre
    dolduğu için çıkar, ``start`` yine de başarıyla dönerdi ve worker açık
    görünüp hiçbir Job işlemezdi. Ölçüm bu yüzden "start başarılı döndü"yle
    yetinmez; executor'ın gerçekten çağrıldığını ve janitor'ın gerçekten
    süpürdüğünü de arar.
    """
    shim = BarrierThreadingShim(fail_at=2, delay_seconds=DELAYED_START_SECONDS)
    monkeypatch.setattr(wk, "threading", shim)
    fake = FakeExecutor()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    sweep = RecordingSweep()
    monkeypatch.setattr(wk, "sweep_stale_execution_runs", sweep)
    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
        execution_run_janitor_interval_seconds=0.05,
    )

    worker = start_worker(stopped_workers, session_factory, prepared)

    # İki thread de canlı ve sahiplenilmiş.
    assert sorted(thread.name for thread in worker_threads()) == [
        "playbook-worker",
        "playbook-worker-janitor",
    ]
    # İkisi de gerçekten iş yapıyor: gecikmiş yayım hiçbirini öldürmedi.
    assert wait_until(lambda: fake.calls >= 1)
    assert wait_until(lambda: sweep.calls >= 1)

    assert worker.stop(join_seconds=WAIT_SECONDS) is True
    assert worker_threads() == []


def test_the_publication_barrier_is_awaited_without_a_timeout(
    settings: Settings,
    session_factory: Callable[[], Session],
    stopped_workers: list[PlaybookWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yayım beklemesi **süresizdir**; entrypoint'ler ``wait(timeout=None)`` çağırır.

    Bariyeri açan iki terminal yol vardır (başarılı yayım ve geri alım) ve
    ``start`` bunların dışında dönmez; bekleyen thread mutlaka uyandırılır.
    Buraya konan herhangi bir süre, sonunda başarılı olan gecikmiş bir
    başlangıçta thread'i sessizce öldürmenin yolu olurdu.
    """
    fake = FakeExecutor()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    sweep = RecordingSweep()
    monkeypatch.setattr(wk, "sweep_stale_execution_runs", sweep)
    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
        execution_run_janitor_interval_seconds=0.05,
    )

    worker = PlaybookWorker(session_factory=session_factory, settings=prepared)
    barrier = RecordingEvent()
    monkeypatch.setattr(worker, "_ready", barrier)
    stopped_workers.append(worker)
    worker.start()

    # İki entrypoint de bariyeri bekledi ve ikisi de süre vermedi.
    assert wait_until(lambda: len(barrier.wait_timeouts) == 2)
    assert barrier.wait_timeouts == [None, None]
    # Ve yayımı gördükten sonra gerçekten çalıştılar.
    assert wait_until(lambda: fake.calls >= 1)
    assert wait_until(lambda: sweep.calls >= 1)
    assert worker.stop(join_seconds=WAIT_SECONDS) is True


class RecordingLogger:
    """``_logger`` yerine konan, yazılan **tam** çağrıyı saklayan kayıt tutucu.

    Yalnız metne bakmak yetmez: bir arızanın metni ikinci bir argümanla ya da
    ``exc_info=True`` ile de log'a girebilirdi. Bu yüzden argümanlar da saklanır.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self.warnings.append((message, args, kwargs))


def test_a_worker_that_cannot_start_keeps_execution_closed_without_leaking(
    settings: Settings,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``worker.start`` düşerse execution kapalı kalır; engine ve thread sızmaz.

    Fail-closed davranış üç parçadır ve üçü de ölçülür: uygulama açılır, arka
    planda tek bir executor çağrısı doğmaz ve arıza **sabit** bir metinle
    bildirilir. Engine'in sahipliği lifespan'de kalır; kapanış onu bırakır.
    """
    import app.main as main_module

    fake = FakeExecutor()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)

    class UnstartableWorker:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

        def stop(self, **kwargs: Any) -> bool:
            return True

    monkeypatch.setattr(main_module, "PlaybookWorker", UnstartableWorker)

    created: list[Engine] = []
    real_create = main_module.create_db_engine

    def recording_create(active: Settings) -> Engine:
        engine = real_create(active)
        created.append(engine)
        return engine

    disposed: list[Engine] = []
    real_dispose = Engine.dispose

    def counting_dispose(self: Engine, close: bool = True) -> None:
        disposed.append(self)
        real_dispose(self, close=close)

    recorder = RecordingLogger()
    monkeypatch.setattr(main_module, "create_db_engine", recording_create)
    monkeypatch.setattr(Engine, "dispose", counting_dispose)
    monkeypatch.setattr(main_module, "_logger", recorder)

    prepared = build_settings(
        settings, command=stub_command("success"), playbook_worker_enabled=True
    )
    ensure_app_data_dirs(prepared)

    with TestClient(create_app(prepared)) as client:
        assert client.get("/health").status_code == 200
        time.sleep(0.2)

    assert fake.calls == 0
    assert worker_threads() == []
    # Engine sızmadı: worker'ın durduğu kanıtlandıktan sonra bırakıldı.
    assert created[-1] in disposed
    # Log **sabittir**: tek bir çağrı, sabit metin, argümansız ve `exc_info`'suz.
    # İşletim sisteminin hata metni hiçbir yolla log'a girmez.
    assert recorder.warnings == [(main_module._LOG_WORKER_START_FAILED, (), {})]


def test_a_real_worker_that_fails_to_publish_opens_the_app_without_executing(
    settings: Settings,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aynı yol **gerçek** worker'la: uygulama açılır, arka planda hiçbir şey denenmez.

    Bir önceki test main.py'nin arıza karşısındaki davranışını sahte bir
    worker'la yalıtır; bu test aynı yolu gerçek :class:`PlaybookWorker` ve
    kontrollü bir thread fabrikasıyla uçtan uca ölçer. Ölçülen şey lifespan'in
    tamamıdır: fail-closed açılış, sıfır çalıştırma denemesi, sabit log, tam bir
    kez dispose edilen engine ve sızmayan thread.
    """
    import app.main as main_module

    shim = BarrierThreadingShim(fail_at=2)
    monkeypatch.setattr(wk, "threading", shim)
    fake = FakeExecutor()
    monkeypatch.setattr(wk, "execute_next_playbook_job", fake)
    # Yalnız **janitor thread'inin** turları sayılır: açılıştaki (c) adımı
    # `app.main`'in kendi referansını kullanır ve bu sayaca girmez.
    sweep = RecordingSweep()
    monkeypatch.setattr(wk, "sweep_stale_execution_runs", sweep)

    created: list[Engine] = []
    real_create = main_module.create_db_engine

    def recording_create(active: Settings) -> Engine:
        engine = real_create(active)
        created.append(engine)
        return engine

    disposed: list[Engine] = []
    real_dispose = Engine.dispose

    def counting_dispose(self: Engine, close: bool = True) -> None:
        disposed.append(self)
        real_dispose(self, close=close)

    recorder = RecordingLogger()
    monkeypatch.setattr(main_module, "create_db_engine", recording_create)
    monkeypatch.setattr(Engine, "dispose", counting_dispose)
    monkeypatch.setattr(main_module, "_logger", recorder)

    prepared = build_settings(
        settings,
        command=stub_command("success"),
        playbook_worker_enabled=True,
        playbook_worker_poll_seconds=0.05,
        execution_run_janitor_interval_seconds=0.05,
    )
    ensure_app_data_dirs(prepared)

    with TestClient(create_app(prepared)) as client:
        assert client.get("/health").status_code == 200
        time.sleep(0.2)

    assert fake.calls == 0
    assert sweep.calls == 0
    assert recorder.warnings == [(main_module._LOG_WORKER_START_FAILED, (), {})]
    # Engine sahipliği lifespan'de kaldı ve kapanışta **tam bir kez** bırakıldı.
    assert disposed.count(created[-1]) == 1
    assert worker_threads() == []
    assert observer_threads() == []
