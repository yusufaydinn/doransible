"""Crash execution-run janitor'ı (R1-V3C2B).

Janitor'ın riski, temizlik primitive'inin riskiyle aynı ama bir adım daha
geniştir: burada hedefi **janitor seçer**. Bu yüzden testlerin çoğu "silinmesi
gereken gitti mi" sorusunu değil, **dokunulmaması gerekenin birebir kaldığını**
ölçer.

Ölçülen beş sınır:

1. *Hedef seçimi.* Aday olabilecek tek şey kökün doğrudan çocuğu olan,
   canonical UUID4 adlı, gerçek bir dizindir. Canonical adlı bir symlink, dosya,
   FIFO veya socket aday değildir; hedefleri hiç açılmaz.
2. *Yaş.* Sınır kesindir: yaş eşiği **aşmalıdır**. Eşitlik ve gelecekteki
   ``mtime`` korunur.
3. *Aktif Job koruması.* ``running`` bir PLAYBOOK Job'ının dizini, kirasının
   dolmuş olup olmadığına **bakılmadan** korunur. Kira kararı C2A'nındır.
4. *Fail-closed sıra.* Kök doğrulaması, girdi sınırı ve veritabanı arızası
   silme başlamadan **önce** yükselir; tek bir adayın arızası ise turu
   bitirmez, sayılır ve o girdi yerinde kalır.
5. *Transaction hijyeni.* Aktif Job snapshot'ı kısa ömürlü bir session'da
   alınır ve session, ilk silmeden **önce** kapanır.

Testler gerçek dosya sistemi davranışını ölçer: gerçek dizinler, gerçek
symlink/FIFO/socket girdileri, gerçek izinler ve gerçek migration zinciriyle
kurulmuş bir veritabanı kullanılır.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
import math
import os
import socket
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import EXECUTION_RUN_DIRNAME
from app.models import (
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    Inventory,
    InventorySourceType,
    Job,
    JobStatus,
    JobType,
    Project,
)
from app.services.execution import reconcile, runner_env
from app.services.execution.reconcile import (
    ExecutionRunSweepResult,
    sweep_stale_execution_runs,
)
from app.services.execution.runner_env import MAX_CLEANUP_DEPTH, RunnerEnvironmentError
from app.services.execution.workspace import secure_filesystem_available

pytestmark = pytest.mark.skipif(
    not secure_filesystem_available(),
    reason="Descriptor-relative dosya sistemi primitive'leri bu platformda yok (ADR-017).",
)

ACTOR = "yerel-operator"
PLAYBOOK_PATH = "site.yml"
STALE = 3600.0

EMPTY = ExecutionRunSweepResult(
    removed=0,
    preserved_active=0,
    preserved_young=0,
    preserved_unexpected=0,
    cleanup_failed=0,
)


# --- Kurulum -----------------------------------------------------------------


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    """Önceden var olan, 0700 ve doğru adlı bir execution run kökü.

    Kökü test'in kurması bilinçlidir: sözleşme gereği janitor de temizlik
    primitive'i de kökü **oluşturmaz**, yalnız niteliklerini doğrular.
    """
    root = tmp_path / "app-data" / EXECUTION_RUN_DIRNAME
    root.mkdir(parents=True)
    root.chmod(0o700)
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """Kökün **dışında**, hiçbir senaryoda dokunulmaması gereken bir ağaç."""
    root = tmp_path / "disarida"
    (root / "alt").mkdir(parents=True)
    (root / "alt" / "kiymetli.txt").write_text("dokunulmadi", encoding="utf-8")
    (root / "kiymetli.txt").write_text("dokunulmadi", encoding="utf-8")
    return root


@pytest.fixture
def session_factory(migrated_engine: Engine) -> Callable[[], Session]:
    """Her çağrıda **yeni** bir session üreten factory (sözleşme gereği)."""

    def factory() -> Session:
        return Session(migrated_engine, expire_on_commit=False)

    return factory


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


def _hex64() -> str:
    """Tekil, 64 küçük harfli hex karakter (token_hash/digest biçimi)."""
    return uuid.uuid4().hex * 2


def _seed_job(
    session: Session,
    records: tuple[Project, Inventory],
    *,
    job_id: str,
    status: JobStatus,
    lease_expires_at: datetime | None = None,
) -> str:
    """Verilen kimlikte ve durumda bir PLAYBOOK Job satırı yazar.

    Plan kaydı her satır için üretilir: etkin bir PLAYBOOK Job'ı, veritabanı
    kısıtı gereği (``ck_jobs_active_playbook_is_authorized``) yetkilendirildiği
    planı taşımak zorundadır.
    """
    project, inventory = records
    moment = datetime.now(UTC)
    plan_id = str(uuid.uuid4())
    session.add(
        ExecutionPlanRecord(
            id=plan_id,
            token_hash=_hex64(),
            project_id=project.id,
            inventory_id=inventory.id,
            playbook_path=PLAYBOOK_PATH,
            requested_by=ACTOR,
            input_fingerprint=_hex64(),
            workspace_id=str(uuid.uuid4()),
            manifest_digest=_hex64(),
            status=ExecutionPlanStatus.CLAIMED,
            created_at=moment,
            expires_at=moment + timedelta(hours=1),
            claimed_at=moment,
        )
    )
    session.flush()

    fields: dict[str, Any] = {
        "id": job_id,
        "job_type": JobType.PLAYBOOK,
        "status": status,
        "execution_plan_id": plan_id,
        "project_id": project.id,
        "inventory_id": inventory.id,
        "playbook_path": PLAYBOOK_PATH,
        "requested_by": ACTOR,
        "created_at": moment,
    }
    if status is JobStatus.RUNNING:
        # Kira `heartbeat_at`'ten sonra dolmalıdır
        # (``ck_jobs_running_playbook_lease_outlives_heartbeat``).
        lease = lease_expires_at or moment + timedelta(seconds=60)
        fields.update(
            worker_id=str(uuid.uuid4()),
            started_at=moment - timedelta(minutes=5),
            heartbeat_at=lease - timedelta(seconds=30),
            lease_expires_at=lease,
        )
    elif status in (JobStatus.SUCCESSFUL, JobStatus.FAILED, JobStatus.CANCELED):
        fields.update(started_at=moment - timedelta(minutes=5), finished_at=moment)
    session.add(Job(**fields))
    session.commit()
    return job_id


def _run_dir(
    run_root: Path,
    *,
    age_seconds: float,
    job_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """Verilen yaşta, içi dolu bir Job çalışma dizini kurar.

    ``mtime`` içerik yazıldıktan **sonra** ayarlanır: alt girdi oluşturmak
    dizinin kendi ``mtime``'ını tazeler ve yaş ölçümü sessizce anlamsızlaşırdı.
    """
    name = job_id or str(uuid.uuid4())
    job_dir = run_root / name
    (job_dir / "artifacts" / "raw").mkdir(parents=True)
    (job_dir / "artifacts" / "raw" / "stdout").write_text("iz", encoding="utf-8")
    job_dir.chmod(0o700)
    _age(job_dir, age_seconds, now=now)
    return name


def _age(path: Path, age_seconds: float, *, now: datetime | None = None) -> None:
    """Girdinin ``mtime``'ını verilen karar anına göre geriye alır.

    Sınır testleri ``now``'ı açıkça verir: duvar saatiyle kurulan bir yaş,
    kurulum ile ölçüm arasında geçen süre kadar büyür ve "tam eşikte" iddiası
    hiçbir zaman gerçekten eşitlikte ölçülmezdi.
    """
    moment = (now or datetime.now(UTC)).timestamp() - age_seconds
    os.utime(path, (moment, moment), follow_symlinks=False)


def _fingerprint(base: Path) -> dict[str, tuple[int, str]]:
    """Ağacın biçim, izin ve içerik parmak izi.

    Symlink **izlenmez**: bağlantının kendisi hedefi çözülmeden kaydedilir,
    yoksa dış hedefin korunduğu iddiası bağlantının kendisiyle karışırdı.
    """
    found: dict[str, tuple[int, str]] = {}
    for path in sorted(base.rglob("*")):
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            payload = f"symlink:{os.readlink(path)}"
        elif stat.S_ISDIR(status.st_mode):
            payload = "dir"
        elif stat.S_ISREG(status.st_mode):
            payload = f"file:{path.read_bytes()!r}"
        else:
            payload = f"special:{stat.S_IFMT(status.st_mode)}"
        found[str(path.relative_to(base))] = (stat.S_IMODE(status.st_mode), payload)
    return found


def _sweep(
    session_factory: Callable[[], Session], run_root: Path, **overrides: Any
) -> ExecutionRunSweepResult:
    arguments: dict[str, Any] = {"execution_run_root": run_root, "stale_seconds": STALE}
    arguments.update(overrides)
    return sweep_stale_execution_runs(session_factory, **arguments)


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


@contextmanager
def _failing_statements(engine: Engine, *, fragment: str) -> Iterator[None]:
    """Verilen parçayı içeren her SQL ifadesini gerçek execution sınırında düşürür."""

    def _raise(_conn: Any, _cursor: Any, statement: str, *_args: Any, **_kwargs: Any) -> None:
        if fragment in statement:
            raise OperationalError(statement, {}, Exception("disk I/O error"))

    event.listen(engine, "before_cursor_execute", _raise)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", _raise)


# --- Mutlu yol ---------------------------------------------------------------


def test_an_old_orphan_run_directory_is_removed(
    run_root: Path, session_factory: Callable[[], Session]
) -> None:
    """Hiçbir Job'a ait olmayan eski bir ağaç, içeriğiyle birlikte kaldırılır."""
    job_id = _run_dir(run_root, age_seconds=STALE + 60)

    result = _sweep(session_factory, run_root)

    assert result == ExecutionRunSweepResult(
        removed=1,
        preserved_active=0,
        preserved_young=0,
        preserved_unexpected=0,
        cleanup_failed=0,
    )
    assert not (run_root / job_id).exists()
    # Kökün kendisi ne silinir ne de izni değişir.
    assert run_root.is_dir()
    assert stat.S_IMODE(run_root.stat().st_mode) == 0o700


def test_every_old_orphan_is_removed_in_one_sweep(
    run_root: Path, session_factory: Callable[[], Session]
) -> None:
    """Tur "ilk kalıntıyı topla" değildir: eski adayların tamamı kapanır."""
    names = [_run_dir(run_root, age_seconds=STALE + delta) for delta in (1, 60, 86_400)]

    result = _sweep(session_factory, run_root)

    assert result.removed == len(names)
    assert list(run_root.iterdir()) == []


@pytest.mark.parametrize(
    "age",
    [
        pytest.param(STALE - 1, id="younger-than-the-threshold"),
        pytest.param(STALE, id="exactly-at-the-threshold"),
    ],
)
def test_young_and_boundary_aged_directories_are_preserved(
    run_root: Path, session_factory: Callable[[], Session], age: float
) -> None:
    """Yaş sınırı kesindir: eşiği **aşmayan** dizin korunur.

    Eşitlikte silmek, eşiğin "bu süre boyunca dokunulmaz" sözünü bir saniyelik
    yuvarlama farkına bırakırdı.
    """
    moment = datetime.now(UTC)
    job_id = _run_dir(run_root, age_seconds=age, now=moment)
    before = _fingerprint(run_root)

    result = _sweep(session_factory, run_root, now=moment)

    assert result.removed == 0
    assert result.preserved_young == 1
    assert (run_root / job_id).is_dir()
    assert _fingerprint(run_root) == before


def test_a_future_timestamp_is_preserved(
    run_root: Path, session_factory: Callable[[], Session]
) -> None:
    """Gelecekte duran bir ``mtime`` silme gerekçesi değildir.

    Negatif bir yaş saat kayması ya da kurcalanmış bir zaman damgasıdır; ikisi
    de "bu alan terk edilmiş" demek için yeterli değildir.
    """
    _run_dir(run_root, age_seconds=-86_400)
    before = _fingerprint(run_root)

    result = _sweep(session_factory, run_root)

    assert result.removed == 0
    assert result.preserved_young == 1
    assert _fingerprint(run_root) == before


# --- Aktif Job koruması ------------------------------------------------------


@pytest.mark.parametrize(
    "lease_offset",
    [
        pytest.param(60.0, id="live-lease"),
        pytest.param(-600.0, id="expired-lease"),
    ],
)
def test_a_running_playbook_directory_is_preserved_whatever_its_lease(
    run_root: Path,
    db_session: Session,
    session_factory: Callable[[], Session],
    records: tuple[Project, Inventory],
    lease_offset: float,
) -> None:
    """``running`` bir Job'ın alanı, kirası dolmuş olsa bile korunur.

    Kira kararının tek sahibi C2A'dır ve açılışta **önce** o çalışır. Janitor da
    kirayı yorumlasaydı iki bileşen aynı kararı bağımsız verirdi: birinin canlı
    saydığı bir execution'ın çalışma alanı diğeri tarafından altından silinirdi.
    """
    job_id = _run_dir(run_root, age_seconds=STALE + 600)
    _seed_job(
        db_session,
        records,
        job_id=job_id,
        status=JobStatus.RUNNING,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_offset),
    )
    before = _fingerprint(run_root)

    result = _sweep(session_factory, run_root)

    assert result == ExecutionRunSweepResult(
        removed=0,
        preserved_active=1,
        preserved_young=0,
        preserved_unexpected=0,
        cleanup_failed=0,
    )
    assert _fingerprint(run_root) == before


@pytest.mark.parametrize("status", [JobStatus.SUCCESSFUL, JobStatus.FAILED, JobStatus.CANCELED])
def test_a_terminal_jobs_leftover_directory_is_removed(
    run_root: Path,
    db_session: Session,
    session_factory: Callable[[], Session],
    records: tuple[Project, Inventory],
    status: JobStatus,
) -> None:
    """Terminal bir Job'ın arkasında kalan eski alan toplanır."""
    job_id = _run_dir(run_root, age_seconds=STALE + 60)
    _seed_job(db_session, records, job_id=job_id, status=status)

    assert _sweep(session_factory, run_root).removed == 1

    assert not (run_root / job_id).exists()


def test_a_directory_without_any_job_row_is_removed(
    run_root: Path,
    db_session: Session,
    session_factory: Callable[[], Session],
    records: tuple[Project, Inventory],
) -> None:
    """Veritabanında karşılığı **hiç** bulunmayan eski alan da toplanır.

    Bu, gerçek crash tablosudur: satır silinmiş, migration'dan önce üretilmiş ya
    da hiç yazılamamış olabilir. Koruma yalnız ``running`` bir satırın varlığına
    bağlıdır; yokluk koruma üretmez.
    """
    orphan = _run_dir(run_root, age_seconds=STALE + 60)
    live = _run_dir(run_root, age_seconds=STALE + 60)
    _seed_job(db_session, records, job_id=live, status=JobStatus.RUNNING)

    result = _sweep(session_factory, run_root)

    assert result.removed == 1
    assert result.preserved_active == 1
    assert not (run_root / orphan).exists()
    assert (run_root / live).is_dir()


def test_a_pending_jobs_leftover_directory_is_removed(
    run_root: Path,
    db_session: Session,
    session_factory: Callable[[], Session],
    records: tuple[Project, Inventory],
) -> None:
    """``pending`` bir Job'ın kimliğiyle eşleşen eski kalıntı güvenle kaldırılır.

    ``pending`` bir Job henüz **çalışmıyordur**: aynı kimlikte eski bir alan
    duruyorsa o, çökmüş bir önceki denemeden kalmıştır ve hazırlık onu "aynı
    kimlikte girdi var" diye reddedeceği için kuyruğu kalıcı olarak tıkardı.
    """
    job_id = _run_dir(run_root, age_seconds=STALE + 60)
    _seed_job(db_session, records, job_id=job_id, status=JobStatus.PENDING)

    assert _sweep(session_factory, run_root).removed == 1

    assert not (run_root / job_id).exists()


# --- Aday olmayan girdiler ---------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("gecici", id="plain-name"),
        pytest.param("c232ab00-9414-11ec-b3c8-9e6bdeced846", id="uuid1"),
        pytest.param("6F9619FF-8B86-D011-B42D-00CF4FC964FF", id="uppercase-uuid"),
        pytest.param("artifacts", id="looks-like-a-managed-dirname"),
    ],
)
def test_a_directory_with_a_non_canonical_name_is_preserved(
    run_root: Path, session_factory: Callable[[], Session], name: str
) -> None:
    """Canonical UUID4 olmayan bir ad aday değildir; yaşı ne olursa olsun kalır."""
    entry = run_root / name
    entry.mkdir()
    (entry / "iz.txt").write_text("kalmali", encoding="utf-8")
    _age(entry, STALE * 10)
    before = _fingerprint(run_root)

    result = _sweep(session_factory, run_root)

    assert result.removed == 0
    assert result.preserved_unexpected == 1
    assert _fingerprint(run_root) == before


def test_a_canonical_symlink_is_preserved_and_its_target_is_untouched(
    run_root: Path, outside: Path, session_factory: Callable[[], Session]
) -> None:
    """Canonical adlı bir bağlantı aday değildir ve hedefi hiç açılmaz.

    Aday olsaydı, kökün altına konan tek bir bağlantı silmeyi ağacın dışına
    taşırdı — janitor'ın var oluş amacına taban tabana zıt bir sonuç.
    """
    link = run_root / str(uuid.uuid4())
    os.symlink(outside, link)
    _age(link, STALE * 10)
    before = _fingerprint(outside)

    result = _sweep(session_factory, run_root)

    assert result.removed == 0
    assert result.preserved_unexpected == 1
    assert link.is_symlink()
    assert _fingerprint(outside) == before
    assert (outside / "kiymetli.txt").read_text(encoding="utf-8") == "dokunulmadi"


@pytest.mark.parametrize("kind", ["file", "fifo", "socket"])
def test_a_canonical_non_directory_entry_is_preserved(
    run_root: Path,
    session_factory: Callable[[], Session],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Canonical adlı bir dosya, FIFO veya socket aday değildir ve silinmez.

    Girdinin **kendisi de** kaldırılmaz: beklenen nesnenin yerinde başka bir
    şeyin durması, janitor'ın sessizce üstesinden geleceği bir durum değildir.
    FIFO ayrıca hiç **açılmaz**; açmak okuyucu/yazıcı beklerken bloklardı.
    """
    name = str(uuid.uuid4())
    entry = run_root / name
    with contextlib.ExitStack() as stack:
        if kind == "file":
            entry.write_text("veri", encoding="utf-8")
        elif kind == "fifo":
            os.mkfifo(entry, 0o600)
        else:
            # AF_UNIX yolu kısa tutulur: bind, uzun mutlak yolları kabul etmez.
            monkeypatch.chdir(run_root)
            sock = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            sock.bind(name)
        _age(entry, STALE * 10)
        before = _fingerprint(run_root)

        result = _sweep(session_factory, run_root)

        assert result.removed == 0
        assert result.preserved_unexpected == 1
        assert _fingerprint(run_root) == before


# --- Global fail-closed yollar -----------------------------------------------


def test_a_relative_root_is_rejected_before_anything_is_removed(
    session_factory: Callable[[], Session],
) -> None:
    """Relative bir kök sürecin çalışma dizinine göre çözülürdü."""
    with pytest.raises(RunnerEnvironmentError) as error:
        _sweep(session_factory, Path(EXECUTION_RUN_DIRNAME))

    assert error.value.details == {"reason": "execution_run_root_not_absolute"}


def test_a_root_with_an_unexpected_name_is_rejected(
    tmp_path: Path, session_factory: Callable[[], Session]
) -> None:
    """Kök adı sabittir; serbest bir ad keyfi bir dizini janitor hedefi yapardı."""
    impostor = tmp_path / "tmp"
    impostor.mkdir()
    impostor.chmod(0o700)
    _run_dir(impostor, age_seconds=STALE * 10)
    before = _fingerprint(impostor)

    with pytest.raises(RunnerEnvironmentError) as error:
        _sweep(session_factory, impostor)

    assert error.value.details == {"reason": "execution_run_root_unexpected_name"}
    assert _fingerprint(impostor) == before


def test_a_symlinked_root_is_rejected_and_its_target_is_untouched(
    tmp_path: Path, session_factory: Callable[[], Session]
) -> None:
    """Kökün yerine konmuş bağlantı izlenmez; hedefinin altı taranmaz."""
    real = tmp_path / "gercek-kok"
    real.mkdir()
    real.chmod(0o700)
    _run_dir(real, age_seconds=STALE * 10)
    link = tmp_path / EXECUTION_RUN_DIRNAME
    os.symlink(real, link)
    before = _fingerprint(real)

    with pytest.raises(RunnerEnvironmentError) as error:
        _sweep(session_factory, link)

    assert error.value.details == {"reason": "execution_run_root_unavailable"}
    assert _fingerprint(real) == before


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777, 0o750])
def test_a_root_with_the_wrong_permission_is_rejected_and_not_chmodded(
    tmp_path: Path, session_factory: Callable[[], Session], mode: int
) -> None:
    """İzin **düzeltilmez**: yanlış kurulmuş bir kök sessizce kabul edilmez."""
    root = tmp_path / EXECUTION_RUN_DIRNAME
    root.mkdir()
    job_id = _run_dir(root, age_seconds=STALE * 10)
    root.chmod(mode)

    with pytest.raises(RunnerEnvironmentError) as error:
        _sweep(session_factory, root)

    assert error.value.details == {"reason": "execution_run_root_not_private"}
    assert stat.S_IMODE(root.stat().st_mode) == mode
    assert (root / job_id).is_dir()


def test_the_root_entry_limit_stops_the_sweep_before_any_removal(
    run_root: Path, session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kök beklenmedik büyüklükteyse hiçbir aday silinmez.

    Sınır girdiler **incelenmeden** uygulanır: beklenmedik bir tabloda tek tek
    "hangisini silelim" kararına girmek, asıl sorunu (yanlış kök ya da fark
    edilmemiş bir crash döngüsü) toplu silmeyle örtmek olurdu.
    """
    names = [_run_dir(run_root, age_seconds=STALE * 10) for _ in range(3)]
    monkeypatch.setattr(runner_env, "MAX_RUN_ROOT_ENTRIES", len(names) - 1)
    before = _fingerprint(run_root)

    with pytest.raises(RunnerEnvironmentError) as error:
        _sweep(session_factory, run_root)

    assert error.value.details == {"reason": "execution_run_root_too_many_entries"}
    assert _fingerprint(run_root) == before

    # Sınır tam yettiğinde aynı kök normal biçimde toplanır.
    monkeypatch.setattr(runner_env, "MAX_RUN_ROOT_ENTRIES", len(names))
    assert _sweep(session_factory, run_root).removed == len(names)


def test_a_database_failure_removes_nothing(
    run_root: Path, migrated_engine: Engine, session_factory: Callable[[], Session]
) -> None:
    """Aktif Job okuması düşerse hiçbir aday silinmez ve hata yükselir.

    Arıza boş bir aktif kümeye **çevrilmez**: öyle olsaydı bir disk hatası,
    çalışan bir Job'ın çalışma alanının silinmesine yol açardı — janitor'ın
    verebileceği en pahalı yanlış karar budur.
    """
    _run_dir(run_root, age_seconds=STALE * 10)
    before = _fingerprint(run_root)

    with _failing_statements(migrated_engine, fragment="FROM jobs"):
        with pytest.raises(OperationalError):
            _sweep(session_factory, run_root)

    assert _fingerprint(run_root) == before
    # Arıza geçtikten sonra aynı kök normal biçimde toplanır.
    assert _sweep(session_factory, run_root).removed == 1


def test_the_session_is_closed_before_any_cleanup_starts(
    run_root: Path,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot'tan sonra session kapanır; temizlik açık transaction'la koşmaz.

    Ölçüm dolaylı değildir: ``close()`` ve her silme aynı olay listesine yazılır
    ve silme anında session'ın transaction'ı olmadığı da doğrulanır. Uzun süren
    descriptor-relative bir temizlik boyunca açık tutulan bir SQLite
    transaction'ı, o süre boyunca bütün yazarları bloklardı (ADR-019 Karar 6/4).
    """
    _run_dir(run_root, age_seconds=STALE * 10)
    _run_dir(run_root, age_seconds=STALE * 10)
    events: list[str] = []
    sessions: list[Session] = []

    class TrackedSession(Session):
        def close(self) -> None:
            events.append("close")
            super().close()

    def factory() -> Session:
        session = TrackedSession(migrated_engine)
        sessions.append(session)
        return session

    real_remove = reconcile.remove_execution_run_directory

    def recording_remove(root: Path, job_id: str, **kwargs: Any) -> bool:
        events.append("remove")
        assert sessions, "aktif Job snapshot'ı alınmadan silme başladı"
        assert not sessions[0].in_transaction()
        return real_remove(root, job_id, **kwargs)

    monkeypatch.setattr(reconcile, "remove_execution_run_directory", recording_remove)

    result = _sweep(factory, run_root)

    assert result.removed == 2
    assert events == ["close", "remove", "remove"]
    assert len(sessions) == 1, "tur başına tek, kısa ömürlü session"


# --- Aday başına arıza -------------------------------------------------------


def test_a_failed_candidate_is_preserved_counted_and_does_not_stop_the_sweep(
    run_root: Path, session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tek bir adayın fail-closed reddi turu bitirmez.

    Diğer adaylar yine değerlendirilir, arıza sayılır ve düşen girdi **yerinde**
    kalır. Turu ilk hatada kesmek, tek bir bozuk kalıntının bütün janitor'ı
    kalıcı olarak etkisiz hâle getirmesi demekti.
    """
    doomed = _run_dir(run_root, age_seconds=STALE * 10)
    healthy = _run_dir(run_root, age_seconds=STALE * 10)
    real_remove = reconcile.remove_execution_run_directory

    def failing_remove(root: Path, job_id: str, **kwargs: Any) -> bool:
        if job_id == doomed:
            raise RunnerEnvironmentError(
                "temizlik düştü", details={"reason": "run_dir_unavailable"}
            )
        return real_remove(root, job_id, **kwargs)

    monkeypatch.setattr(reconcile, "remove_execution_run_directory", failing_remove)

    result = _sweep(session_factory, run_root)

    assert result == ExecutionRunSweepResult(
        removed=1,
        preserved_active=0,
        preserved_young=0,
        preserved_unexpected=0,
        cleanup_failed=1,
    )
    assert (run_root / doomed).is_dir()
    assert not (run_root / healthy).exists()


def test_an_unreadable_subtree_is_a_real_counted_failure(
    run_root: Path, session_factory: Callable[[], Session]
) -> None:
    """Gerçek bir izin arızası da aynı biçimde sayılır ve ağaç korunur.

    Ölçüm mock'lanmış bir hata değil, temizlik primitive'inin gerçek
    fail-closed yolu üzerinden yapılır: okunamayan bir alt dizin sessizce
    atlanmaz.
    """
    if os.geteuid() == 0:  # pragma: no cover - CI kullanıcısına bağlı
        pytest.skip("root için dosya izinleri erişimi kısıtlamaz.")

    doomed = _run_dir(run_root, age_seconds=STALE * 10)
    healthy = _run_dir(run_root, age_seconds=STALE * 10)
    locked = run_root / doomed / "artifacts"
    locked.chmod(0o000)
    try:
        result = _sweep(session_factory, run_root)

        assert result.removed == 1
        assert result.cleanup_failed == 1
        assert (run_root / doomed).is_dir()
        assert not (run_root / healthy).exists()
    finally:
        locked.chmod(0o700)


def test_a_candidate_swapped_for_a_symlink_after_the_scan_is_not_followed(
    run_root: Path,
    outside: Path,
    session_factory: Callable[[], Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tarama ile silme arasındaki değiş-tokuş dış hedefi silmez.

    Aday listesi bir **hak** değildir: hedef silme anında kökten yeniden
    türetilir ve primitive kendi canonical/doğrudan-çocuk/symlink kontrollerini
    yeniden yapar. Listede görüldüğü için silinen bir ad, tam da bu pencerede
    yerine konan bir bağlantı üzerinden ağacın dışına taşardı.
    """
    job_id = str(uuid.uuid4())
    (run_root / job_id).mkdir(mode=0o700)
    _age(run_root / job_id, STALE * 10)
    real_remove = reconcile.remove_execution_run_directory
    swapped: list[str] = []

    def swapping_remove(root: Path, name: str, **kwargs: Any) -> bool:
        if not swapped:
            swapped.append(name)
            os.rmdir(root / name)
            os.symlink(outside, root / name)
        return real_remove(root, name, **kwargs)

    monkeypatch.setattr(reconcile, "remove_execution_run_directory", swapping_remove)
    before = _fingerprint(outside)

    result = _sweep(session_factory, run_root)

    assert swapped == [job_id], "değiş-tokuş gerçekten kuruldu"
    assert result.removed == 0
    assert result.cleanup_failed == 1
    assert (run_root / job_id).is_symlink()
    assert _fingerprint(outside) == before


def test_a_candidate_replaced_by_a_new_real_directory_is_preserved(
    run_root: Path, session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aynı adla oluşturulmuş **yeni ve gerçek** bir dizin silinmez.

    Symlink değiş-tokuşundan farklı olarak burada yerine konan şey kusursuz bir
    aday gibi görünür: canonical adlı, 0700, gerçek bir dizin. Ad üzerinden
    çalışan bir janitor onu silerdi — oysa o dizin bir sonraki denemenin çalışma
    alanı olabilir ve bu turun kararı onun için hiç verilmemiştir. Ayrım ancak
    listelenen **nesnenin** kimliğiyle yapılabilir.

    Aynı turda ikinci, dokunulmamış bir orphan da bulunur: kimlik kontrolü
    sıradan temizliği durdurmamalıdır.
    """
    doomed = _run_dir(run_root, age_seconds=STALE * 10)
    untouched = _run_dir(run_root, age_seconds=STALE * 10)
    marker = "YENI-DENEMENIN-ALANI-a71c"
    real_remove = reconcile.remove_execution_run_directory
    passed: dict[str, Any] = {}
    raised: list[RunnerEnvironmentError] = []

    def replacing_remove(root: Path, name: str, **kwargs: Any) -> bool:
        if name == doomed and not passed:
            passed.update(kwargs)
            # Eski ağaç gider, **yeni** bir dizin aynı adla açılır. Inode'un
            # yeniden kullanılması olağandır; ayrım yalnız kimliğin tamamıyla
            # yapılabilir.
            real_remove(root, name)
            replacement = root / name
            replacement.mkdir(mode=0o700)
            (replacement / "yeni.txt").write_text(marker, encoding="utf-8")
        try:
            return real_remove(root, name, **kwargs)
        except RunnerEnvironmentError as error:
            raised.append(error)
            raise

    monkeypatch.setattr(reconcile, "remove_execution_run_directory", replacing_remove)

    result = _sweep(session_factory, run_root)

    assert result == ExecutionRunSweepResult(
        removed=1,
        preserved_active=0,
        preserved_young=0,
        preserved_unexpected=0,
        cleanup_failed=1,
    )
    # Listelenen kimlik gerçekten remover'a aktarıldı.
    assert passed["expected_identity"] is not None
    assert passed["missing_ok"] is True
    # Yeni dizin, marker dosyası ve içeriği birebir duruyor.
    replacement = run_root / doomed
    assert replacement.is_dir() and not replacement.is_symlink()
    assert stat.S_IMODE(replacement.stat().st_mode) == 0o700
    assert (replacement / "yeni.txt").read_text(encoding="utf-8") == marker
    # Kimlik uyuşmazlığı sıradan temizliği durdurmadı.
    assert not (run_root / untouched).exists()
    # İçeriden yükselen hata da sızdırmıyor.
    assert [error.details for error in raised] == [{"reason": "run_dir_identity_changed"}]
    rendered = f"{raised[0]} {raised[0].details}"
    for secret in (doomed, marker, str(run_root)):
        assert secret not in rendered


def test_the_inner_depth_limit_is_still_enforced_by_the_remover(
    run_root: Path, session_factory: Callable[[], Session]
) -> None:
    """Janitor kendi yürüyüşünü yapmaz: iç ağacın sınırları primitive'indir.

    Derinlik sınırını aşan bir ağaç, janitor'ın "ama ben topluyorum" gerekçesiyle
    sınırsız bir silmeye dönüşmez; fail-closed sayılır ve ağaç olduğu gibi kalır.
    """
    job_id = str(uuid.uuid4())
    job_dir = run_root / job_id
    job_dir.mkdir(mode=0o700)
    job_dir.joinpath(*["d"] * (MAX_CLEANUP_DEPTH + 1)).mkdir(parents=True)
    _age(job_dir, STALE * 10)
    before = _fingerprint(job_dir)

    result = _sweep(session_factory, run_root)

    assert result.removed == 0
    assert result.cleanup_failed == 1
    assert _fingerprint(job_dir) == before


# --- Girdi doğrulaması -------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"stale_seconds": 0.0}, id="stale-zero"),
        pytest.param({"stale_seconds": -1.0}, id="stale-negative"),
        pytest.param({"stale_seconds": math.nan}, id="stale-nan"),
        pytest.param({"stale_seconds": math.inf}, id="stale-infinite"),
        pytest.param({"now": datetime.now()}, id="naive-now"),  # noqa: DTZ005
    ],
)
def test_invalid_input_is_refused_without_touching_the_database_or_the_disk(
    run_root: Path,
    session_factory: Callable[[], Session],
    counted_statements: list[str],
    overrides: dict[str, Any],
) -> None:
    """Geçersiz eşik veya naive an, hiçbir yan etki üretmeden reddedilir.

    Sıfır veya negatif bir eşik, yeni oluşturulmuş bir çalışma alanını da stale
    sayardı: çalışan bir Job'ın alanını altından silmenin en kısa yolu budur.
    """
    _run_dir(run_root, age_seconds=STALE * 10)
    before = _fingerprint(run_root)
    counted_statements.clear()

    with pytest.raises(ValueError):
        _sweep(session_factory, run_root, **overrides)

    assert counted_statements == []
    assert _fingerprint(run_root) == before


def test_the_default_decision_moment_is_utc_now(
    run_root: Path, session_factory: Callable[[], Session]
) -> None:
    """``now`` verilmezse karar anı UTC şimdisidir."""
    old = _run_dir(run_root, age_seconds=STALE + 60)
    young = _run_dir(run_root, age_seconds=STALE - 60)

    result = _sweep(session_factory, run_root, now=None)

    assert (result.removed, result.preserved_young) == (1, 1)
    assert not (run_root / old).exists()
    assert (run_root / young).is_dir()


# --- Sonuç sözleşmesi ve kapsam kilidi ---------------------------------------


def test_the_result_carries_no_paths_or_identifiers(
    run_root: Path,
    db_session: Session,
    session_factory: Callable[[], Session],
    records: tuple[Project, Inventory],
) -> None:
    """Sonuç yalnız sayı taşır: path, Job kimliği veya dizin içeriği yoktur.

    Sonuç ileride loglanacak ve muhtemelen API'ye çıkacak bir değerdir; içine
    konan tek bir workspace yolu, kalıntının okunabildiği her yerde görünür
    olurdu.
    """
    removed_id = _run_dir(run_root, age_seconds=STALE * 10)
    active_id = _run_dir(run_root, age_seconds=STALE * 10)
    _seed_job(db_session, records, job_id=active_id, status=JobStatus.RUNNING)
    (run_root / "beklenmeyen").mkdir()

    result = _sweep(session_factory, run_root)

    assert result == ExecutionRunSweepResult(
        removed=1,
        preserved_active=1,
        preserved_young=0,
        preserved_unexpected=1,
        cleanup_failed=0,
    )
    values = [getattr(result, field.name) for field in dataclasses.fields(result)]
    assert len(values) == 5
    assert all(isinstance(value, int) and not isinstance(value, bool) for value in values)
    rendered = repr(result)
    for secret in (removed_id, active_id, str(run_root), EXECUTION_RUN_DIRNAME):
        assert secret not in rendered


def test_the_result_is_immutable() -> None:
    """Sayaçlar sonradan değiştirilemez: sonuç bir kayıttır, bir biriktirici değil."""
    with pytest.raises(AttributeError):
        EMPTY.removed = 5  # type: ignore[misc]


def test_the_janitor_imports_no_process_or_job_state_layer() -> None:
    """Janitor yalnız modelleri, temizlik primitive'ini ve SQLAlchemy'yi tanır.

    İddia bir yasak listesi değil **tam eşitliktir**: yasak listesi yalnız bugün
    akla gelen modülleri yakalar, oysa eklenmemesi gereken şey henüz adı
    konmamış olandır. Özellikle ``shutil``, ``glob`` ve süreç katmanı burada
    bulunmamalıdır — bunların her biri, denetlenen descriptor-relative sınırın
    yanından dolaşan ikinci bir silme biçiminin ilk adımı olurdu.
    """
    tree = ast.parse(inspect.getsource(reconcile))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported == {
        "__future__",
        "contextlib",
        "math",
        "collections.abc",
        "dataclasses",
        "datetime",
        "pathlib",
        "sqlalchemy",
        "sqlalchemy.exc",
        "sqlalchemy.orm",
        "app.models",
        "app.services.execution.runner_env",
    }


def test_the_janitor_uses_no_free_path_removal() -> None:
    """Silme yalnız mevcut primitive üzerinden yapılır.

    Sözleşmenin bu yarısı davranışla ölçülemez: ``rmtree``/``unlink``/``rmdir``
    çağıran bir janitor de testleri geçebilirdi. Bu yüzden kaynağın kendisinde
    silme yapan hiçbir çağrı bulunmadığı ölçülür.
    """
    tree = ast.parse(inspect.getsource(reconcile))
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                called.add(target.id)
            elif isinstance(target, ast.Attribute):
                called.add(target.attr)

    for forbidden in ("rmtree", "unlink", "rmdir", "remove", "removedirs", "glob", "rglob", "walk"):
        assert forbidden not in called, forbidden
    assert "remove_execution_run_directory" in called


def test_only_startup_and_the_worker_loop_call_the_janitor() -> None:
    """Janitor'ı çağıran yerler **tam olarak** lifespan ve worker döngüsüdür.

    R1-V3C2C'ye kadar hiçbir çağıran yoktu; artık iki tane vardır ve liste tam
    eşitlikle ölçülür. Bir endpoint modülünün bu listeye girmesi, temizliği
    dışarıdan tetiklenebilir hâle getirirdi: janitor kararını Job durumundan
    alır ve HTTP isteğiyle tetiklenen bir tur, aktif bir çalıştırmanın
    penceresine denk getirilebilirdi.
    """
    callers = [
        str(module)
        for module in sorted(Path("app").rglob("*.py"))
        if "sweep_stale_execution_runs" in module.read_text(encoding="utf-8")
        and module.name not in {"reconcile.py", "__init__.py"}
    ]

    assert callers == ["app/main.py", "app/services/execution/worker.py"]
    assert not any(
        "sweep_stale_execution_runs" in module.read_text(encoding="utf-8")
        for module in Path("app/api").rglob("*.py")
    )
    # Public yüzey janitor yüzünden büyümez: launch route'u R1-V3D1'de eklendi
    # (15 → 16), Job okuma route'ları R1-V3D2B'de (16 → 19), controller path
    # browse route'u R1-V3J0C'de (19 → 20) ve kalıcı ping geçmişinin
    # `ping-runs` yolu R1-V3J1'de (20 → 21). Hepsi yalnız Job rezerve eder,
    # okur, controller allowlist'ini listeler ya da ping geçmişini döker;
    # hiçbiri temizlik tetiklemez. R1-V3J2 yalnız frontend cursor pagination'dı
    # ve R1-V3J3A yalnız mevcut sonuç cevabını genişletti; ikisi de route
    # eklemedi.
    routes = sorted(Path("app/api/routes").glob("*.py"))
    decorators = sum(module.read_text(encoding="utf-8").count("@router.") for module in routes)
    assert decorators == 21
