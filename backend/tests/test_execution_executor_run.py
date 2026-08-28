"""Tek atımlık playbook executor'ı (R1-V3C1C2B2B).

Merkez iddia: **bir Job ancak baştan sona doğrulanmış bir yoldan geçerse
terminal olur ve o yolun her arıza dalı geride ne çalışan bir süreç, ne açık bir
transaction, ne de bir çalışma alanı bırakır.**

Testlerin ortak kuralı: hiçbir katman taklit edilmez. Veritabanı migration
uygulanmış gerçek bir SQLite'tır, dondurulmuş workspace `freeze_workspace` ile
gerçekten dondurulur, child gerçek bir işletim sistemi sürecidir ve artifact
gerçekten diske yazılır. Taklit edilen tek şey Ansible'ın kendisidir
(``runner_stub.py``); gerçek `ansible-runner` ile uçtan uca ölçüm dosyanın
sonundadır ve atlanmaz.

Ölçülen altı sınır:

1. *Sıra.* Acquire → girdi → environment → artifact rezervasyonu → child →
   kira → normalize → run cleanup → artifact → terminal geçiş. Her arıza dalı
   kendi sabit hata koduna düşer ve child'ın hiç başlamadığı dallarda gerçekten
   başlamaz.
2. *Transaction sınırı.* Child çalışırken bu süreçte açık bir session yoktur:
   başka bir bağlantı aynı anda yazabilir ve her heartbeat kendi session'ını
   açıp kapatır.
3. *Kira.* Kaybedilmiş veya ölçülemeyen bir kira kısmi çıktıyı **yayımlatmaz**.
4. *Sızdırmazlık.* Bağlantı değeri, ham stderr, path, token ve digest ne
   artifact'e ne Job satırına ne de sonuç nesnesine girer.
5. *Temizlik sırası.* Run directory her yolda kaldırılır; kaldırılamazsa başarı
   ilan edilmez; yayımlanmış bir sonuç sonraki hiçbir arızada silinmez.
6. *Kapsam.* Çağrı başına en fazla bir Job işlenir; HTTP yüzeyi büyümez.
"""

from __future__ import annotations

import inspect
import json
import os
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import (
    EXECUTION_RUN_DIRNAME,
    PLAYBOOK_RUNNER_MIN_RESULT_BYTES,
    Settings,
    ensure_app_data_dirs,
)
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
from app.services.execution import executor as ex
from app.services.execution.executor import (
    ExecutionAttempt,
    ExecutionOutcome,
    execute_next_playbook_job,
)
from app.services.execution.workspace import (
    freeze_workspace,
    secure_filesystem_available,
)
from app.services.jobs.artifacts import JobArtifactUnavailableError
from app.services.security.redaction import REDACTED
from tests.support import make_settings
from tests.test_runner_process import real_runner_available, stub_command

pytestmark = pytest.mark.skipif(
    not secure_filesystem_available(),
    reason="Descriptor-relative dosya sistemi primitive'leri bu platformda yok (ADR-017).",
)

ACTOR = "yerel-operator"
PLAYBOOK_PATH = "site.yml"

# Testlerin beklemeye razı olduğu **üst** sınır; hiçbir testte "bu kadar sürer"
# varsayımı yoktur.
WAIT_SECONDS = 20.0

# Zararsız probe playbook'u: yalnız `debug`/`assert`.
PLAYBOOK = (
    "- name: probe\n"
    "  hosts: all\n"
    "  gather_facts: false\n"
    "  tasks:\n"
    "    - name: say\n"
    "      ansible.builtin.debug:\n"
    '        msg: "probe"\n'
    "    - name: assert\n"
    "      ansible.builtin.assert:\n"
    "        that: [true]\n"
)

# Bağlantı değeri olarak snapshot'a konan işaretçi. Ne artifact'e, ne Job
# satırına, ne de sonuç nesnesine girmelidir.
SENTINEL_USER = "AOPS-SENTINEL-CONNECTION-USER-4e1a"

# Stub runner'ın gördüğü host. Normalize recap'i yalnız dondurulmuş
# inventory'de bulunan hostlar için üretir; ikisi eşleşmek zorundadır.
PROBE_HOST = "probehost"

# Gerçek bir host başarısızlığı bildiren denetimli runner çıktısı (R1-V3G1B).
#
# `runner_stub.py`'nin davranış tablosuna yeni bir dal eklemek yerine burada
# tanımlanır: ölçülen şey Ansible'ın kendisi değil, **normalize'ın ürettiği
# `playbook_failed` kodunun result.json'a ve Job satırına aynı değerle
# taşınmasıdır. Süreç yine gerçek bir işletim sistemi sürecidir ve servisin
# ürettiği gerçek argv'yi alır; fazladan argümanları yok sayar.
_HOST_FAILURE_SOURCE = f"""
import json, sys

HOST = {PROBE_HOST!r}


def emit(event, **data):
    print(json.dumps({{"event": event, "event_data": data}}))


emit("playbook_on_task_start", task="Harden")
emit("runner_on_failed", host=HOST, task="Harden", res={{"failed": True}})
emit(
    "playbook_on_stats",
    ok={{}},
    changed={{}},
    failures={{HOST: 1}},
    dark={{}},
    skipped={{}},
    rescued={{}},
    ignored={{}},
    processed={{HOST: 1}},
)
sys.stdout.flush()
raise SystemExit(2)
"""


def host_failure_command() -> list[str]:
    """Recap'inde gerçek bir host failure bulunan çıktı üreten komut."""
    return [sys.executable, "-c", _HOST_FAILURE_SOURCE]


# Yayımlanan ``result.json``'ın **tam** alan kümesi (R1-V3J3A: schema_version=2).
RESULT_DOCUMENT_FIELDS = {
    "schema_version",
    "job_id",
    "return_code",
    "outcome",
    "error_code",
    "recap",
    "events",
    "events_truncated",
    "result_truncated",
    "ansible_output",
    "ansible_output_truncated",
}

# Üst düzey ``stdout`` taşıyan denetimli runner çıktısı (R1-V3J3A).
#
# `runner_stub.py`'nin davranış tablosuna dokunulmaz: ölçülen şey Ansible'ın
# kendisi değil, executor'ın yayımladığı belgenin display output'u gerçekten
# taşımasıdır. Süreç yine gerçek bir işletim sistemi sürecidir.
DISPLAY_SENTINEL = "ok: [probehost] => ansible_become_password=SENTINEL-DISPLAY-PW"

_DISPLAY_OUTPUT_SOURCE = f"""
import json, sys

HOST = {PROBE_HOST!r}


def emit(event, stdout, **data):
    print(json.dumps({{"event": event, "stdout": stdout, "event_data": data}}))


emit("playbook_on_task_start", "TASK [Ping] ****", task="Ping")
emit("runner_on_ok", {DISPLAY_SENTINEL!r}, host=HOST, task="Ping", res={{"changed": False}})
emit(
    "playbook_on_stats",
    "PLAY RECAP ****",
    ok={{HOST: 1}},
    changed={{}},
    failures={{}},
    dark={{}},
    skipped={{}},
    rescued={{}},
    ignored={{}},
    processed={{HOST: 1}},
)
sys.stdout.flush()
"""


def display_output_command() -> list[str]:
    """Üst düzey ``stdout`` satırları üreten, başarılı biten komut."""
    return [sys.executable, "-c", _DISPLAY_OUTPUT_SOURCE]


# --- Kurulum -----------------------------------------------------------------


def snapshot(**variables: str) -> str:
    """Uygulamanın ürettiği biçimde bir inventory snapshot metni."""
    return json.dumps({"all": {"hosts": {PROBE_HOST: variables}}}, indent=2, sort_keys=True) + "\n"


@pytest.fixture
def source_project(tmp_path: Path) -> Path:
    """Dondurulmadan önceki özgün project ağacı."""
    root = tmp_path / "kaynak-proje"
    root.mkdir()
    (root / PLAYBOOK_PATH).write_text(PLAYBOOK, encoding="utf-8")
    return root


@pytest.fixture
def source_inventory(tmp_path: Path) -> Path:
    """Özgün inventory dosyası; dondurmadan sonra **hiç** açılmamalıdır."""
    path = tmp_path / "hosts.ini"
    path.write_text(f"[all]\n{PROBE_HOST}\n", encoding="utf-8")
    return path


def build_settings(
    base: Settings,
    *,
    command: list[str],
    **overrides: Any,
) -> Settings:
    """Test ayarlarını **doğrulayıcılardan geçirerek** üretir.

    ``model_copy`` bilinçli olarak kullanılmaz: alan doğrulayıcıları o yolda
    çalışmaz ve testler ürünün kabul etmeyeceği bir yapılandırmayla yeşil
    kalabilirdi.
    """
    values: dict[str, Any] = {
        "environment": "test",
        "app_data_dir": base.app_data_dir,
        "database_url": base.database_url,
        "project_root_allowlist": list(base.project_root_allowlist),
        "inventory_root_allowlist": list(base.inventory_root_allowlist),
        "ssh_key_root_allowlist": list(base.ssh_key_root_allowlist),
        "ansible_runner_command": command,
        "playbook_runner_timeout_seconds": 60.0,
        "playbook_worker_lease_seconds": 60.0,
        "playbook_worker_heartbeat_seconds": 0.05,
    }
    values.update(overrides)
    return make_settings(**values)


@pytest.fixture
def runtime(settings: Settings) -> Settings:
    """`app-data` ağacı kurulmuş, stub runner kullanan ayarlar."""
    prepared = build_settings(settings, command=stub_command("success"))
    ensure_app_data_dirs(prepared)
    return prepared


@pytest.fixture
def session_factory(migrated_engine: Engine) -> Callable[[], Session]:
    """Her çağrıda **yeni** bir session üreten factory (sözleşme gereği)."""

    def factory() -> Session:
        return Session(migrated_engine, expire_on_commit=False)

    return factory


def seed_job(
    session: Session,
    runtime: Settings,
    source_project: Path,
    *,
    inventory_snapshot: str | None = None,
    playbook_path: str = PLAYBOOK_PATH,
    plan_status: ExecutionPlanStatus = ExecutionPlanStatus.CLAIMED,
    mode: ExecutionMode = ExecutionMode.CHECK,
) -> str:
    """Gerçek bir dondurulmuş workspace'e bağlı ``pending`` PLAYBOOK Job'ı kurar.

    Workspace elle kurulmaz: `freeze_workspace` neyi dondurduysa manifest de onu
    doğrular ve testler gerçek bütünlük yolunu ölçer.
    """
    frozen = freeze_workspace(
        runtime.resolve_execution_plan_dir(),
        project_root=source_project,
        inventory_snapshot=inventory_snapshot
        if inventory_snapshot is not None
        else snapshot(ansible_user=SENTINEL_USER),
    )
    # `path` tekil bir sütundur ve aynı testte iki Job kurulabilir. Yol yalnız
    # FK/metadata içindir: çalıştırma özgün ağacı **hiç** açmaz, dondurulmuş
    # kopyayı kullanır.
    suffix = uuid.uuid4().hex[:8]
    project = Project(name=f"Web-{suffix}", path=f"{source_project}-{suffix}")
    session.add(project)
    session.commit()
    inventory = Inventory(
        name=f"Prod-{suffix}",
        path=f"{source_project}-{suffix}/hosts.ini",
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    session.add(inventory)
    session.commit()

    moment = datetime.now(UTC)
    plan_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    session.add(
        ExecutionPlanRecord(
            id=plan_id,
            token_hash=uuid.uuid4().hex * 2,
            project_id=project.id,
            inventory_id=inventory.id,
            playbook_path=playbook_path,
            requested_by=ACTOR,
            input_fingerprint=uuid.uuid4().hex * 2,
            workspace_id=frozen.workspace_id,
            manifest_digest=frozen.manifest_digest,
            status=plan_status,
            mode=mode,
            created_at=moment,
            expires_at=moment + timedelta(hours=1),
            claimed_at=moment,
        )
    )
    session.flush()
    session.add(
        Job(
            id=job_id,
            job_type=JobType.PLAYBOOK,
            status=JobStatus.PENDING,
            execution_plan_id=plan_id,
            project_id=project.id,
            inventory_id=inventory.id,
            playbook_path=playbook_path,
            limit_pattern=None,
            requested_by=ACTOR,
            mode=mode,
            created_at=moment,
        )
    )
    session.commit()
    return job_id


@pytest.fixture
def pending_job(db_session: Session, runtime: Settings, source_project: Path) -> str:
    return seed_job(db_session, runtime, source_project)


def run_once(
    session_factory: Callable[[], Session], runtime: Settings, worker: str | None = None
) -> ExecutionAttempt:
    return execute_next_playbook_job(
        session_factory=session_factory,
        settings=runtime,
        worker_id=worker if worker is not None else str(uuid.uuid4()),
    )


# --- Gözlem ------------------------------------------------------------------


def read_job(engine: Engine, job_id: str) -> Any:
    """Bağımsız bir bağlantıdan **commit edilmiş** Job satırını okur."""
    with Session(engine) as observer:
        return observer.execute(
            select(
                Job.status,
                Job.return_code,
                Job.error_code,
                Job.artifact_path,
                Job.result_truncated,
                Job.worker_id,
                Job.finished_at,
                Job.lease_expires_at,
            ).where(Job.id == job_id)
        ).one()


def run_root(runtime: Settings) -> Path:
    return runtime.app_data_dir / EXECUTION_RUN_DIRNAME


def artifact_dir(runtime: Settings, job_id: str) -> Path:
    return runtime.app_data_dir / "jobs" / job_id


def result_file(runtime: Settings, job_id: str) -> Path:
    return artifact_dir(runtime, job_id) / "result.json"


def run_directories(runtime: Settings) -> list[str]:
    root = run_root(runtime)
    return sorted(entry.name for entry in root.iterdir()) if root.exists() else []


def tree_fingerprint(base: Path) -> dict[str, tuple[bool, int, int]]:
    """Ad, tür, izin ve boyut özeti; yeni girdi de değişen bayt da yakalanır."""
    found: dict[str, tuple[bool, int, int]] = {}
    for path in sorted(base.rglob("*")):
        status = path.lstat()
        found[str(path.relative_to(base))] = (
            path.is_dir(),
            stat.S_IMODE(status.st_mode),
            status.st_size if stat.S_ISREG(status.st_mode) else 0,
        )
    return found


class ReportingCommand:
    """Child'ın **gerçekten** çalışıp çalışmadığını kanıtlayan stub komutu."""

    def __init__(self, tmp_path: Path, behaviour: str, **options: object) -> None:
        self.report = tmp_path / f"rapor-{uuid.uuid4().hex}.json"
        self.command = stub_command(behaviour, report=self.report, **options)

    @property
    def started(self) -> bool:
        return self.report.exists()

    @property
    def pid(self) -> int:
        """Child'ın **kendi** bildirdiği pid; reap kanıtı buradan okunur."""
        return int(json.loads(self.report.read_text(encoding="utf-8"))["pid"])

    def assert_reaped(self) -> None:
        """Child'ın reap edildiğini `waitpid` ile doğrudan kanıtlar.

        Reap edilmiş bir pid artık bu sürecin çocuğu değildir ve ``waitpid``
        ``ECHILD`` verir; hâlâ çalışan ya da zombie duran bir çocuk ise
        ``(0, 0)`` veya ``(pid, status)`` döndürürdü.
        """
        with pytest.raises(ChildProcessError):
            os.waitpid(self.pid, os.WNOHANG)

    def wait_for_start(self, timeout: float = WAIT_SECONDS) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.report.exists():
                return True
            time.sleep(0.01)
        return False


# --- 1. Boş kuyruk ve bağ geçersizliği ---------------------------------------


def test_an_empty_queue_touches_nothing(
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    """Alınacak iş yoksa süreç, dosya sistemi ve artifact hiç ele alınmaz."""
    never = ReportingCommand(tmp_path, "report-only")
    runtime = build_settings(settings, command=never.command)
    ensure_app_data_dirs(runtime)
    before = tree_fingerprint(runtime.app_data_dir)

    attempt = run_once(session_factory, runtime)

    assert attempt == ExecutionAttempt(ExecutionOutcome.IDLE)
    assert attempt.job_id is None
    assert never.started is False
    assert tree_fingerprint(runtime.app_data_dir) == before


def test_an_invalid_binding_never_starts_a_child(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Plan bağı geçersizse Job child hiç başlamadan terminal olur."""
    never = ReportingCommand(tmp_path, "report-only")
    runtime = build_settings(settings, command=never.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(
        db_session,
        runtime,
        source_project,
        # `prepared` bir plan henüz claim edilmemiştir: Job'ın yetkilendirmesi
        # tamamlanmamış demektir ve bağ geçersizdir.
        plan_status=ExecutionPlanStatus.PREPARED,
    )

    attempt = run_once(session_factory, runtime)

    assert attempt.outcome is ExecutionOutcome.BINDING_INVALID
    assert never.started is False
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.error_code == "execution_binding_invalid"
    assert row.artifact_path is None
    assert run_directories(runtime) == []
    assert not artifact_dir(runtime, job_id).exists()


# --- 2. Mutlu yol ------------------------------------------------------------


def test_a_valid_job_finishes_successfully(
    pending_job: str,
    runtime: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
) -> None:
    """Geçerli bir Job gerçek bir child ile ``successful`` olur."""
    attempt = run_once(session_factory, runtime)

    assert attempt.outcome is ExecutionOutcome.FINISHED
    assert attempt.job_id == pending_job
    assert attempt.status is JobStatus.SUCCESSFUL
    assert attempt.error_code is None

    row = read_job(migrated_engine, pending_job)
    assert row.status is JobStatus.SUCCESSFUL
    assert row.return_code == 0
    assert row.error_code is None
    assert row.artifact_path == f"jobs/{pending_job}/result.json"
    assert row.result_truncated is False
    # Kira terminal satırda boşaltılır.
    assert row.worker_id is None
    assert row.lease_expires_at is None
    assert row.finished_at is not None

    assert result_file(runtime, pending_job).is_file()
    assert run_directories(runtime) == []


@pytest.mark.parametrize(
    ("mode", "expects_check"),
    [(ExecutionMode.CHECK, True), (ExecutionMode.NORMAL, False)],
    ids=["check", "normal"],
)
def test_the_acquired_jobs_mode_reaches_the_runner_argv_unchanged(
    mode: ExecutionMode,
    expects_check: bool,
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """`AcquiredPlaybookJob.mode`, executor'dan runner argv'sine değişmeden ulaşır.

    Kip ayardan, request'ten veya bir sabitten yeniden üretilmez (R1-V3H1B2B):
    doğrudan acquire'ın bağladığı Job satırından gelir. ``CHECK`` argv'ye tam
    bir kez ``--cmdline=--check`` ekler, ``NORMAL`` onu hiç eklemez ve argv'nin
    geri kalanı iki kipte de birebir aynı kalır.
    """
    child = ReportingCommand(tmp_path, "success")
    runtime = build_settings(settings, command=child.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project, mode=mode)

    attempt = run_once(session_factory, runtime)

    assert attempt.outcome is ExecutionOutcome.FINISHED
    assert attempt.status is JobStatus.SUCCESSFUL

    argv = json.loads(child.report.read_text(encoding="utf-8"))["argv"]
    produced = argv[argv.index("run") :]
    if expects_check:
        assert produced.count("--cmdline=--check") == 1
        assert produced[-1] == "--cmdline=--check"
    else:
        assert "--cmdline=--check" not in produced
        assert "--check" not in produced
        assert not any(item == "--cmdline" or item.startswith("--cmdline=") for item in produced)
        assert produced[-2:] == ["-p", PLAYBOOK_PATH]

    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.SUCCESSFUL


def test_the_published_result_carries_only_the_normalized_schema(
    pending_job: str, runtime: Settings, session_factory: Callable[[], Session]
) -> None:
    """`result.json` normalize şemasının **tam** karşılığıdır, fazlası değil."""
    run_once(session_factory, runtime)

    document = json.loads(result_file(runtime, pending_job).read_text(encoding="utf-8"))
    assert set(document) == RESULT_DOCUMENT_FIELDS
    assert document["schema_version"] == 2
    assert document["job_id"] == pending_job
    assert document["outcome"] == "successful"
    assert document["error_code"] is None
    assert set(document["recap"]) == {PROBE_HOST}
    # Event'ler yalnız dar allowlist alanlarını taşır.
    for event in document["events"]:
        assert set(event) == {"event", "host", "task", "changed", "failed"}


# --- 3. Normalize edilmiş arızalar -------------------------------------------


def test_a_nonzero_return_code_becomes_a_terminal_failure(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
) -> None:
    """Terminal event geçerli ama rc sıfır değil: ``failed`` + ``runner_failed``."""
    runtime = build_settings(
        settings, command=stub_command("write-raw", size_bytes=16, exit_code=2)
    )
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    attempt = run_once(session_factory, runtime)

    assert attempt.status is JobStatus.FAILED
    assert attempt.error_code == "runner_failed"
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.return_code == 2
    assert row.error_code == "runner_failed"
    # Başarısız bir sonuç da yayımlanır: kullanıcı neden başarısız olduğunu
    # görebilmelidir.
    assert row.artifact_path == f"jobs/{job_id}/result.json"
    assert run_directories(runtime) == []


def test_a_reported_host_failure_carries_the_playbook_code_to_both_records(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
) -> None:
    """Doğrulanmış bir host başarısızlığı iki kayda da **aynı** kodla yazılır.

    Sınıflandırma tek bir yerde (normalize) yapılır ve executor onu yalnız
    taşır: aynı ``NormalizedRun`` hem ``result.json``'a hem Job satırına gider.
    İkisinin ayrışması, sonuç okuma yolundaki DB ↔ artifact eşitlik kontrolünü
    düşürür ve kullanıcı hiçbir sonuç göremezdi.

    Karşılaştırma noktası ``test_a_nonzero_return_code_becomes_a_terminal_failure``
    ile aynı ``rc=2``'dir; ayrımı yapan tek şey recap'in gerçekten bir failure
    bildirmesidir.
    """
    runtime = build_settings(settings, command=host_failure_command())
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    attempt = run_once(session_factory, runtime)

    assert attempt.status is JobStatus.FAILED
    assert attempt.error_code == "playbook_failed"

    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.return_code == 2
    assert row.error_code == "playbook_failed"
    assert row.artifact_path == f"jobs/{job_id}/result.json"

    document = json.loads(result_file(runtime, job_id).read_text(encoding="utf-8"))
    assert document["error_code"] == row.error_code
    assert document["outcome"] == "failed"
    assert document["return_code"] == row.return_code
    # Kanıt gerçekten taşınır: kod boş bir zarftan değil, dolu bir recap'ten gelir.
    assert document["recap"][PROBE_HOST]["failures"] == 1
    assert any(event["failed"] for event in document["events"])
    # Alan kümesi genişlemez: yeni kod yeni bir alan getirmez.
    assert set(document) == RESULT_DOCUMENT_FIELDS
    assert document["schema_version"] == 2
    assert run_directories(runtime) == []


def test_a_timeout_becomes_runner_timeout(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
) -> None:
    """Sınırı aşan çalıştırma sonlandırılır ve ``runner_timeout`` olur."""
    runtime = build_settings(
        settings,
        command=stub_command("sleep", sleep_seconds=30),
        playbook_runner_timeout_seconds=0.5,
    )
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    attempt = run_once(session_factory, runtime)

    assert attempt.error_code == "runner_timeout"
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.error_code == "runner_timeout"
    assert row.artifact_path == f"jobs/{job_id}/result.json"
    assert run_directories(runtime) == []


@pytest.mark.parametrize(
    ("behaviour", "options", "limits", "truncated"),
    [
        # stdout sınırı: akış aşıldığı anda süreç sonlandırılır.
        (
            "flood-stdout",
            {"size_bytes": 3_000_000, "sleep_seconds": 5},
            {"playbook_runner_max_stdout_bytes": 200_000},
            False,
        ),
        # raw bütçesi: artifact dizini şişerse sonlandırma talep edilir.
        (
            "flood-raw",
            {"size_bytes": 400_000, "sleep_seconds": 5},
            {"playbook_runner_max_raw_bytes": 200_000},
            False,
        ),
        # event sayısı: çıktı geçerli ama işlenemeyecek kadar çok satır.
        ("success", {}, {"playbook_runner_max_events": 1}, False),
        # sonuç boyutu: normalize edilmiş belge sınırı aşıyor. Değer, ayarların
        # kabul ettiği **en küçük** bütçedir (`PLAYBOOK_RUNNER_MIN_RESULT_BYTES`)
        # ve sabitten okunur: taban şema sürümüyle birlikte arttı (R1-V3J3A'da
        # 256 → 320) ve elle yazılmış bir kopya, ayarların artık reddettiği bir
        # değeri test edilir sanardı. Daha küçük bir sayı (eskiden 40) sonucu
        # yine bu koda düşürürdü ama yayımlanan arıza zarfını kendi bütçesinin
        # dışında bırakır ve okuyucuya production'ın kendi geçerli belgesini
        # reddettirirdi.
        (
            "success",
            {},
            {"playbook_runner_max_result_bytes": PLAYBOOK_RUNNER_MIN_RESULT_BYTES},
            True,
        ),
    ],
)
def test_limit_breaches_become_result_limit_exceeded(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    behaviour: str,
    options: dict[str, Any],
    limits: dict[str, Any],
    truncated: bool,
) -> None:
    """Dört sınırın dördü de aynı sabit koda düşer ve kırpılmayı açıkça söyler."""
    runtime = build_settings(settings, command=stub_command(behaviour, **options), **limits)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    attempt = run_once(session_factory, runtime)

    assert attempt.error_code == "result_limit_exceeded"
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.error_code == "result_limit_exceeded"
    assert row.result_truncated is truncated
    assert run_directories(runtime) == []


@pytest.mark.parametrize(
    ("behaviour", "error_code"),
    [("invalid-json", "runner_output_invalid"), ("no-terminal-event", "runner_output_invalid")],
)
def test_unusable_runner_output_is_terminal(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    behaviour: str,
    error_code: str,
) -> None:
    """Kısmi veya bozuk çıktı "başarılı ve tam" gibi sunulmaz."""
    runtime = build_settings(settings, command=stub_command(behaviour))
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    run_once(session_factory, runtime)

    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.error_code == error_code


# --- 4. Child başlamadan biten yollar ----------------------------------------


def test_a_tampered_manifest_is_terminal_before_any_child(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Dondurulmuş içerik değişmişse child **hiç** başlamaz."""
    never = ReportingCommand(tmp_path, "report-only")
    runtime = build_settings(settings, command=never.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    workspace = next(runtime.resolve_execution_plan_dir().iterdir())
    (workspace / "project" / PLAYBOOK_PATH).write_text("- hosts: all\n", encoding="utf-8")

    attempt = run_once(session_factory, runtime)

    assert attempt.error_code == "workspace_integrity_failed"
    assert never.started is False
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.error_code == "workspace_integrity_failed"
    assert row.artifact_path is None
    assert run_directories(runtime) == []
    assert not artifact_dir(runtime, job_id).exists()


def test_a_missing_workspace_is_workspace_unavailable(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Workspace hiç yoksa sebep bütünlük değil erişilebilirliktir."""
    never = ReportingCommand(tmp_path, "report-only")
    runtime = build_settings(settings, command=never.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    workspace = next(runtime.resolve_execution_plan_dir().iterdir())
    for path in sorted(workspace.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    workspace.rmdir()

    attempt = run_once(session_factory, runtime)

    assert attempt.error_code == "workspace_unavailable"
    assert never.started is False
    assert read_job(migrated_engine, job_id).error_code == "workspace_unavailable"
    assert run_directories(runtime) == []


def test_a_key_outside_the_effective_allowlist_is_terminal(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Preview anındaki key doğrulaması kalıcı garanti değildir.

    Snapshot allowlist'in **dışında** bir private key gösteriyorsa çalıştırma
    reddedilir; anahtar taşınmış veya allowlist daralmış olabilir.
    """
    never = ReportingCommand(tmp_path, "report-only")
    outside_key = tmp_path / "disarida.pem"
    outside_key.write_text("anahtar", encoding="utf-8")
    runtime = build_settings(settings, command=never.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(
        db_session,
        runtime,
        source_project,
        inventory_snapshot=snapshot(ansible_ssh_private_key_file=str(outside_key)),
    )

    attempt = run_once(session_factory, runtime)

    assert attempt.error_code == "workspace_integrity_failed"
    assert never.started is False
    assert read_job(migrated_engine, job_id).error_code == "workspace_integrity_failed"
    assert run_directories(runtime) == []


def test_a_runner_that_cannot_be_launched_is_terminal(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Komut çalıştırılamıyorsa sonuç ``runner_start_failed``'dır."""
    runtime = build_settings(settings, command=[str(tmp_path / "hic-olmayan-runner")])
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    attempt = run_once(session_factory, runtime)

    assert attempt.error_code == "runner_start_failed"
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.error_code == "runner_start_failed"
    assert row.artifact_path is None
    assert run_directories(runtime) == []
    assert not artifact_dir(runtime, job_id).exists()


def test_a_playbook_missing_from_the_frozen_tree_is_workspace_integrity(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Runner'ın düzen reddi workspace'e yüklenir, başlatma arızasına değil."""
    never = ReportingCommand(tmp_path, "report-only")
    runtime = build_settings(settings, command=never.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project, playbook_path="yok/olmayan.yml")

    attempt = run_once(session_factory, runtime)

    assert attempt.error_code == "workspace_integrity_failed"
    assert never.started is False
    assert read_job(migrated_engine, job_id).error_code == "workspace_integrity_failed"
    assert run_directories(runtime) == []


def test_an_artifact_directory_that_cannot_be_reserved_stops_the_run(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sonucun konacağı yer hazırlanamıyorsa child başlatılmaz."""
    never = ReportingCommand(tmp_path, "report-only")
    runtime = build_settings(settings, command=never.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    def _refuse(self: Any, value: str) -> str:
        raise JobArtifactUnavailableError("Job artifact dizini oluşturulamadı.")

    monkeypatch.setattr(ex.JobArtifactStore, "create", _refuse)

    attempt = run_once(session_factory, runtime)

    assert attempt.error_code == "runner_failed"
    assert never.started is False
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.artifact_path is None
    # Rezervasyon başarısız olsa da run directory geride kalmaz.
    assert run_directories(runtime) == []


# --- 5. Kira -----------------------------------------------------------------


def steal_job(engine: Engine, job_id: str) -> str:
    """Satırı başka bir worker'a geçirir: kirayı kaybetmenin gerçek biçimi."""
    thief = str(uuid.uuid4())
    with Session(engine) as session:
        session.execute(update(Job).where(Job.id == job_id).values(worker_id=thief))
        session.commit()
    return thief


def test_a_lost_lease_cuts_the_child_and_publishes_nothing(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Kirasını kaybeden worker kısmi çıktıyı **yayımlamaz**.

    Satır çalışma sırasında başka bir worker'a geçirilir; heartbeat hiçbir satırı
    etkilemez, gözlemci sonlandırma talep eder ve süreç kesilir. Elde kalan çıktı
    kısmidir ve devralan tarafın sonucunun üstüne yazılamaz.
    """
    child = ReportingCommand(tmp_path, "sleep", sleep_seconds=30)
    runtime = build_settings(settings, command=child.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    result: dict[str, ExecutionAttempt] = {}

    def _work() -> None:
        result["attempt"] = run_once(session_factory, runtime)

    worker = threading.Thread(target=_work)
    worker.start()
    try:
        assert child.wait_for_start(), "child başlamadı"
        thief = steal_job(migrated_engine, job_id)
    finally:
        worker.join(timeout=WAIT_SECONDS)
    assert not worker.is_alive(), "çalıştırma zamanında bitmedi"

    assert result["attempt"].outcome is ExecutionOutcome.OWNERSHIP_LOST
    assert result["attempt"].job_id == job_id
    assert result["attempt"].status is None

    # Sonuç ne yayımlandı ne de satır ezildi.
    assert not result_file(runtime, job_id).exists()
    assert not artifact_dir(runtime, job_id).exists()
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.RUNNING
    assert row.worker_id == thief
    assert row.artifact_path is None
    assert run_directories(runtime) == []


def test_a_heartbeat_database_failure_is_fail_closed(
    db_session: Session,
    settings: Settings,
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Ölçülemeyen kira geçerli kira sayılmaz; sonuç yayımlanmaz.

    Yalnız **heartbeat** session'ı düşer: heartbeat kendi thread'inde çalıştığı
    için factory çağıran thread'e bakarak ayrım yapabilir. Böylece acquire ve
    finish gerçek veritabanında kalır ve testin ölçtüğü tek şey kiranın
    ölçülememesi olur.
    """
    child = ReportingCommand(tmp_path, "sleep", sleep_seconds=30)
    runtime = build_settings(settings, command=child.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)
    main_thread = threading.get_ident()

    def factory() -> Session:
        if threading.get_ident() != main_thread:
            raise OperationalError("heartbeat", {}, Exception("heartbeat"))
        return Session(migrated_engine, expire_on_commit=False)

    attempt = run_once(factory, runtime)

    assert attempt.outcome is ExecutionOutcome.FINISHED
    assert attempt.status is JobStatus.FAILED
    assert attempt.error_code == "runner_failed"
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.error_code == "runner_failed"
    assert row.artifact_path is None
    assert not result_file(runtime, job_id).exists()
    assert run_directories(runtime) == []


# --- 6. Transaction sınırları ------------------------------------------------


class TrackedSession(Session):
    """Kapatıldığını **kendisi** bildiren session."""

    closed = False

    def close(self) -> None:
        super().close()
        self.closed = True


def test_another_connection_can_write_while_the_child_runs(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Child çalışırken bu süreçte açık bir transaction **yoktur**.

    Kanıt dolaylı değil doğrudandır: child koşarken başka bir bağlantı yazar ve
    commit eder. Uzun ömürlü bir session açık kalsaydı SQLite'ın yazma kilidi
    bu commit'i bloklardı.
    """
    child = ReportingCommand(tmp_path, "sleep", sleep_seconds=3)
    runtime = build_settings(settings, command=child.command)
    ensure_app_data_dirs(runtime)
    seed_job(db_session, runtime, source_project)

    written: dict[str, bool] = {}
    worker = threading.Thread(target=lambda: run_once(session_factory, runtime))
    worker.start()
    try:
        assert child.wait_for_start(), "child başlamadı"
        with Session(migrated_engine) as outsider:
            outsider.add(Project(name=f"Yan-{uuid.uuid4().hex[:8]}", path=str(tmp_path / "yan")))
            outsider.commit()
        written["ok"] = True
    finally:
        worker.join(timeout=WAIT_SECONDS)

    assert written.get("ok") is True
    assert not worker.is_alive()


def test_every_heartbeat_uses_its_own_closed_session(
    db_session: Session,
    settings: Settings,
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Her session tek bir işe aittir ve o iş biter bitmez kapanır."""
    child = ReportingCommand(tmp_path, "sleep", sleep_seconds=1.5)
    runtime = build_settings(settings, command=child.command)
    ensure_app_data_dirs(runtime)
    seed_job(db_session, runtime, source_project)

    produced: list[TrackedSession] = []
    threads: list[int] = []
    guard = threading.Lock()

    def factory() -> Session:
        session = TrackedSession(migrated_engine, expire_on_commit=False)
        with guard:
            produced.append(session)
            threads.append(threading.get_ident())
        return session

    run_once(factory, runtime)

    # Acquire + en az bir heartbeat + finish.
    assert len(produced) >= 3
    assert all(session.closed for session in produced)
    # Heartbeat'ler çağıranın thread'inde değil, gözlemcinin thread'indedir.
    assert len(set(threads)) >= 2
    # Aynı session iki kez kullanılmaz.
    assert len({id(session) for session in produced}) == len(produced)


# --- 7. Sızdırmazlık ---------------------------------------------------------


def test_no_sentinel_value_reaches_the_artifact_or_the_job_row(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
) -> None:
    """Bağlantı değeri, ham stderr, path, token ve digest dışarı çıkmaz.

    Stub bilinçli olarak **sızdırır**: bağlantı değerini task adına ve stderr'e
    geri yazar — Ansible'ın gerçekten yaptığı şey budur. Maskeleme ancak
    gerçekten sızdıran bir çıktıda ölçülebilir.
    """
    runtime = build_settings(settings, command=stub_command("leak", leak_text=SENTINEL_USER))
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    with Session(migrated_engine) as reader:
        plan = reader.execute(
            select(
                ExecutionPlanRecord.token_hash,
                ExecutionPlanRecord.manifest_digest,
                ExecutionPlanRecord.workspace_id,
            ).where(ExecutionPlanRecord.id.is_not(None))
        ).one()

    attempt = run_once(session_factory, runtime)
    assert attempt.status is JobStatus.SUCCESSFUL

    published = result_file(runtime, job_id).read_text(encoding="utf-8")
    row = read_job(migrated_engine, job_id)
    surfaces = [published, repr(attempt), str(attempt), repr(row), str(dict(row._mapping))]

    forbidden = (
        SENTINEL_USER,
        plan.token_hash,
        plan.manifest_digest,
        plan.workspace_id,
        str(runtime.app_data_dir),
        "UNREACHABLE",
        str(source_project),
    )
    for surface in surfaces:
        for secret in forbidden:
            assert secret not in surface, secret

    # Değer atılmadı, **maskelendi**: task adı yerinde duruyor.
    document = json.loads(published)
    tasks = {event["task"] for event in document["events"] if event["task"]}
    assert tasks == {f"connect as {REDACTED}"}


def test_the_attempt_result_exposes_only_four_safe_fields() -> None:
    """Sonuç nesnesi yalnız sabit sözlükten ve Job satırından gelen alanları taşır."""
    attempt = ExecutionAttempt(
        ExecutionOutcome.FINISHED,
        job_id="0f9d6f5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f",
        status=JobStatus.FAILED,
        error_code="runner_failed",
    )
    assert set(ExecutionAttempt.__dataclass_fields__) == {
        "outcome",
        "job_id",
        "status",
        "error_code",
    }
    # `repr` alanların tamamını basar; gizlenecek bir şey yoktur çünkü hiç
    # alınmamıştır.
    assert "runner_failed" in repr(attempt)
    # Değişmez ve serialize edilebilir bir taşıma nesnesi değildir.
    with pytest.raises(AttributeError):
        attempt.status = JobStatus.SUCCESSFUL  # type: ignore[misc]
    for attribute in ("model_dump", "model_dump_json", "to_dict", "serialize"):
        assert not hasattr(attempt, attribute)


@pytest.mark.parametrize(
    ("outcome", "fields"),
    [
        (ExecutionOutcome.IDLE, {"job_id": "0f9d6f5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"}),
        (ExecutionOutcome.FINISHED, {}),
        (
            ExecutionOutcome.OWNERSHIP_LOST,
            {"job_id": "0f9d6f5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f", "status": JobStatus.SUCCESSFUL},
        ),
    ],
)
def test_inconsistent_attempt_combinations_are_refused(
    outcome: ExecutionOutcome, fields: dict[str, Any]
) -> None:
    """ "idle ama Job'ı var" gibi bir sonuç okuyanı yanıltırdı."""
    with pytest.raises(ValueError):
        ExecutionAttempt(outcome, **fields)


# --- 8. Artifact ve temizlik sırası ------------------------------------------


def test_an_artifact_write_failure_is_terminal_and_cleans_the_directory(
    pending_job: str,
    runtime: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sonuç yayımlanamazsa Job başarısızdır ve boş dizin geride kalmaz."""

    def _refuse(self: Any, job_id: str, result: dict[str, Any]) -> str:
        raise JobArtifactUnavailableError("Job sonucu yazılamadı.")

    monkeypatch.setattr(ex.JobArtifactStore, "write_result", _refuse)

    attempt = run_once(session_factory, runtime)

    assert attempt.status is JobStatus.FAILED
    assert attempt.error_code == "runner_failed"
    row = read_job(migrated_engine, pending_job)
    assert row.status is JobStatus.FAILED
    assert row.artifact_path is None
    assert not artifact_dir(runtime, pending_job).exists()
    assert run_directories(runtime) == []


def test_a_run_directory_cleanup_failure_prevents_declaring_success(
    pending_job: str,
    runtime: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Temizlenemeyen bir çalışma alanı başarı ilan ettirmez.

    Geride kalan alan, aynı kimlikle yapılacak bir sonraki hazırlığı fail-closed
    düşürür; bunu "başarılı" diye kaydetmek hatayı ilk görülebildiği yerden
    kaldırırdı.
    """
    monkeypatch.setattr(
        ex,
        "remove_execution_run_directory",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ex.RunnerEnvironmentError("temizlenemedi", details={"reason": "run_dir_not_removed"})
        ),
    )

    attempt = run_once(session_factory, runtime)

    assert attempt.status is JobStatus.FAILED
    assert attempt.error_code == "runner_failed"
    row = read_job(migrated_engine, pending_job)
    assert row.status is JobStatus.FAILED
    assert row.error_code == "runner_failed"
    assert row.artifact_path is None
    # Başarılı çalıştırmanın normalize sonucu **yayımlanmadı**.
    assert not result_file(runtime, pending_job).exists()
    assert not artifact_dir(runtime, pending_job).exists()


def test_a_finish_failure_preserves_the_published_result(
    pending_job: str,
    runtime: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal geçiş düşerse hata yükselir ama görünür sonuç silinmez.

    Veritabanı arızasını "iş yok" diye yutmak kuyruğun sessizce durması olurdu;
    yayımlanmış bir ``result.json``'u geri almak ise kullanıcının gördüğü kaydı
    yok etmek olurdu.
    """

    def _fail(*args: Any, **kwargs: Any) -> bool:
        raise OperationalError("finish", {}, Exception("finish"))

    monkeypatch.setattr(ex, "finish_playbook_job", _fail)

    with pytest.raises(OperationalError):
        run_once(session_factory, runtime)

    assert result_file(runtime, pending_job).is_file()
    assert run_directories(runtime) == []
    # Job kirası hâlâ bu worker'da: toplaması stale recovery'nin işidir.
    assert read_job(migrated_engine, pending_job).status is JobStatus.RUNNING


def test_a_lost_finish_race_yields_ownership_lost_without_overwriting(
    pending_job: str,
    runtime: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Satır araya girilip devralınmışsa sonuç **yeniden yazılmaya çalışılmaz**."""
    real_finish = ex.finish_playbook_job
    thief: dict[str, str] = {}

    def _steal_then_finish(*args: Any, **kwargs: Any) -> bool:
        thief["worker"] = steal_job(migrated_engine, pending_job)
        return bool(real_finish(*args, **kwargs))

    monkeypatch.setattr(ex, "finish_playbook_job", _steal_then_finish)

    attempt = run_once(session_factory, runtime)

    assert attempt.outcome is ExecutionOutcome.OWNERSHIP_LOST
    assert attempt.job_id == pending_job
    row = read_job(migrated_engine, pending_job)
    assert row.status is JobStatus.RUNNING
    assert row.worker_id == thief["worker"]
    assert row.artifact_path is None
    # Yayımlanmış sonuç korunur; kaybedilen yarış onu silmez.
    assert result_file(runtime, pending_job).is_file()
    assert run_directories(runtime) == []


def test_an_unexpected_normalize_failure_leaves_neither_directory_behind(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sözleşme dışı bir istisna de rezerve edilmiş artifact dizinini bırakmaz.

    ``normalize_runner_output`` child reap edildikten **sonra**, artifact dizini
    çoktan rezerve edilmişken çalışır. Oradan yükselen beklenmeyen bir hata
    eskiden boş bir ``jobs/<uuid>`` dizini geride bırakırdı; o kalıntı hiçbir
    zaman yayımlanmayacak bir sonucun yerini tutardı.
    """
    child = ReportingCommand(tmp_path, "success")
    runtime = build_settings(settings, command=child.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    def _explode(**_kwargs: Any) -> Any:
        raise RuntimeError("normalize patladi")

    monkeypatch.setattr(ex, "normalize_runner_output", _explode)

    # İstisna **yutulmaz**: çağıran arızayı olduğu gibi görür.
    with pytest.raises(RuntimeError):
        run_once(session_factory, runtime)

    # Child gerçekten çalıştı ve gerçekten reap edildi.
    assert child.started is True
    child.assert_reaped()
    # Ne çalışma alanı ne de yayımlanmamış artifact dizini kaldı.
    assert run_directories(runtime) == []
    assert not artifact_dir(runtime, job_id).exists()
    # Job mevcut sözleşmeye göre terminal `failed`: `running` bırakılsaydı kuyruk
    # kira dolana kadar tıkalı kalırdı.
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.error_code == "runner_failed"
    assert row.artifact_path is None


def test_a_partially_created_artifact_reservation_is_rolled_back(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`create` dizini açtıktan sonra düşerse kalıntı geri alınır.

    Rezervasyon atomik değildir: ``mkdir`` başarılı olup ardından gelen
    ``fsync``/``fchmod`` düşebilir. Eskiden bu yol boş bir ``jobs/<uuid>``
    bırakırdı.
    """
    never = ReportingCommand(tmp_path, "report-only")
    runtime = build_settings(settings, command=never.command)
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    def _half_create(self: Any, value: str) -> str:
        directory = runtime.app_data_dir / "jobs" / value
        directory.mkdir(mode=0o700, parents=True)
        raise JobArtifactUnavailableError("Job artifact dizini oluşturulamadı.")

    monkeypatch.setattr(ex.JobArtifactStore, "create", _half_create)

    attempt = run_once(session_factory, runtime)

    assert attempt.error_code == "runner_failed"
    assert never.started is False
    assert not artifact_dir(runtime, job_id).exists()
    assert run_directories(runtime) == []
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.FAILED
    assert row.artifact_path is None


def test_an_unexpected_failure_after_publication_preserves_the_result(
    pending_job: str,
    runtime: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yayımlanmış ``result.json`` beklenmeyen bir istisnada da silinmez.

    Temizlik "yayımlanmamış olanı topla" sınırındadır; görünür bir sonucu geri
    almak, kullanıcının gördüğü kaydı yok etmek olurdu. Sınırı deponun kendisi
    reddederek uygular.
    """
    real_write = ex.JobArtifactStore.write_result

    def _publish_then_explode(self: Any, job_id: str, result: dict[str, Any]) -> str:
        real_write(self, job_id, result)
        raise RuntimeError("yayimdan sonra patladi")

    monkeypatch.setattr(ex.JobArtifactStore, "write_result", _publish_then_explode)

    with pytest.raises(RuntimeError):
        run_once(session_factory, runtime)

    assert result_file(runtime, pending_job).is_file()
    assert json.loads(result_file(runtime, pending_job).read_text(encoding="utf-8"))
    assert run_directories(runtime) == []
    assert read_job(migrated_engine, pending_job).status is JobStatus.FAILED


@pytest.mark.parametrize(
    "cleanup_failure",
    [
        JobArtifactUnavailableError("Job artifact dizini temizlenemedi."),
        # Deponun sözleşmesinde olmayan, sıradan bir hata. Bastırılmasaydı asıl
        # arızanın yerine geçer ve teşhis temizlik katmanını işaret ederdi.
        RuntimeError("temizlik sirasinda beklenmeyen hata"),
    ],
    ids=["contract", "unexpected"],
)
def test_an_artifact_cleanup_failure_does_not_shadow_the_real_error(
    pending_job: str,
    runtime: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: BaseException,
) -> None:
    """Temizliğin kendi arızası asıl istisnayı gizlemez.

    Çağıran teşhisi asıl hatadan okur; onu ikincil bir temizlik hatasıyla
    değiştirmek, arızayı yanlış katmana yüklerdi. Sonuç, temizliğin **hangi**
    hatayla düştüğünden bağımsızdır.
    """

    def _explode(**_kwargs: Any) -> Any:
        raise RuntimeError("normalize patladi")

    def _refuse_cleanup(self: Any, job_id: str, *, missing_ok: bool = False) -> None:
        raise cleanup_failure

    monkeypatch.setattr(ex, "normalize_runner_output", _explode)
    monkeypatch.setattr(ex.JobArtifactStore, "cleanup", _refuse_cleanup)

    with pytest.raises(RuntimeError) as error:
        run_once(session_factory, runtime)

    # Yüzeye çıkan **asıl** iş hatasıdır; mesajı olduğu gibi korunur.
    assert str(error.value) == "normalize patladi"
    assert str(cleanup_failure) not in str(error.value)
    assert read_job(migrated_engine, pending_job).status is JobStatus.FAILED


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit], ids=["sigint", "exit"])
def test_an_artifact_cleanup_interrupt_is_not_swallowed(
    pending_job: str,
    runtime: Settings,
    session_factory: Callable[[], Session],
    monkeypatch: pytest.MonkeyPatch,
    interrupt: type[BaseException],
) -> None:
    """En iyi çaba temizliği kesme sinyalini **yutmaz**.

    ``BaseException``'ı da bastıran bir "her şeyi yut" bloğu, süreci
    durdurulamaz hâle getirirdi. Sınır ``Exception``'dadır.
    """

    def _explode(**_kwargs: Any) -> Any:
        raise RuntimeError("normalize patladi")

    def _interrupt(self: Any, job_id: str, *, missing_ok: bool = False) -> None:
        raise interrupt

    monkeypatch.setattr(ex, "normalize_runner_output", _explode)
    monkeypatch.setattr(ex.JobArtifactStore, "cleanup", _interrupt)

    with pytest.raises(interrupt):
        run_once(session_factory, runtime)


def test_a_run_directory_cleanup_error_does_not_shadow_the_real_error(
    pending_job: str,
    runtime: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`finally`'deki run cleanup'ı asıl istisnanın yerine **geçemez**.

    Bir ``finally`` bloğundan yükselen hata, yayılmakta olan istisnayı sessizce
    değiştirir; bu yüzden oradaki temizlik sıradan hiçbir hatayı dışarı vermez.
    """

    def _explode(**_kwargs: Any) -> Any:
        raise RuntimeError("normalize patladi")

    def _refuse_removal(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("run cleanup beklenmeyen hata")

    monkeypatch.setattr(ex, "normalize_runner_output", _explode)
    monkeypatch.setattr(ex, "remove_execution_run_directory", _refuse_removal)

    with pytest.raises(RuntimeError) as error:
        run_once(session_factory, runtime)

    assert str(error.value) == "normalize patladi"
    assert read_job(migrated_engine, pending_job).status is JobStatus.FAILED


def test_no_cleanup_helper_suppresses_a_base_exception() -> None:
    """Executor'ın en iyi çaba temizlikleri kesme sinyallerini yutmaz.

    Davranış testleri tek tek yolları ölçer; bu ölçüm, yarın eklenecek yeni bir
    yardımcının aynı hatayı sessizce tekrarlamasını engeller.
    """
    source = inspect.getsource(ex)

    assert "suppress(BaseException)" not in source
    # Modüldeki `except BaseException` dalları **yalnız** yeniden yükseltir;
    # hiçbiri sessizce yutmaz.
    for block in source.split("except BaseException:")[1:]:
        body = block.split("\n\n")[0]
        assert "raise" in body, body


@pytest.mark.parametrize(
    ("behaviour", "options"),
    [
        ("success", {}),
        ("write-raw", {"size_bytes": 32, "exit_code": 3}),
        ("invalid-json", {}),
        ("sleep", {"sleep_seconds": 30}),
    ],
)
def test_the_run_directory_is_gone_on_every_path(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    source_project: Path,
    behaviour: str,
    options: dict[str, Any],
) -> None:
    """Başarı, rc hatası, bozuk çıktı ve timeout: dördünde de kalıntı yok."""
    runtime = build_settings(
        settings,
        command=stub_command(behaviour, **options),
        playbook_runner_timeout_seconds=0.5,
    )
    ensure_app_data_dirs(runtime)
    seed_job(db_session, runtime, source_project)

    run_once(session_factory, runtime)

    assert run_directories(runtime) == []
    assert run_root(runtime).is_dir()
    assert stat.S_IMODE(run_root(runtime).stat().st_mode) == 0o700


def test_neighbouring_run_and_artifact_directories_survive(
    pending_job: str,
    runtime: Settings,
    session_factory: Callable[[], Session],
) -> None:
    """Temizlik yalnız bu Job'a dokunur; komşu Job'ların alanları kalır."""
    neighbour = str(uuid.uuid4())
    neighbour_run = run_root(runtime) / neighbour
    (neighbour_run / "home").mkdir(parents=True)
    neighbour_run.chmod(0o700)
    (neighbour_run / "iz.txt").write_text("kalmali", encoding="utf-8")
    neighbour_artifact = runtime.app_data_dir / "jobs" / neighbour
    neighbour_artifact.mkdir(parents=True)
    (neighbour_artifact / "result.json").write_text('{"komsu": true}', encoding="utf-8")

    before = tree_fingerprint(neighbour_run) | tree_fingerprint(neighbour_artifact)

    run_once(session_factory, runtime)

    assert run_directories(runtime) == [neighbour]
    assert tree_fingerprint(neighbour_run) | tree_fingerprint(neighbour_artifact) == before
    assert (neighbour_artifact / "result.json").read_text(encoding="utf-8") == '{"komsu": true}'
    assert result_file(runtime, pending_job).is_file()


# --- 9. Kapsam ---------------------------------------------------------------


def test_the_original_project_and_inventory_are_never_opened(
    pending_job: str,
    runtime: Settings,
    session_factory: Callable[[], Session],
    source_project: Path,
    source_inventory: Path,
) -> None:
    """Çalıştırma yalnız dondurulmuş kopyaya bakar.

    Özgün ağaç okunamaz hâle getirilir: çalıştırma yine de başarılı olmalıdır.
    Kanıt dolaylı bir sayaç değil, dosya sisteminin kendisidir.
    """
    source_inventory.chmod(0o000)
    (source_project / PLAYBOOK_PATH).chmod(0o000)
    source_project.chmod(0o000)
    try:
        attempt = run_once(session_factory, runtime)
    finally:
        source_project.chmod(0o700)
        (source_project / PLAYBOOK_PATH).chmod(0o600)
        source_inventory.chmod(0o600)

    assert attempt.status is JobStatus.SUCCESSFUL
    assert result_file(runtime, pending_job).is_file()


def test_one_call_handles_at_most_one_job(
    db_session: Session,
    runtime: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
) -> None:
    """Fonksiyon bir döngü değildir: her çağrı tek bir Job işler.

    İki ``pending`` PLAYBOOK Job'ı aynı anda **var olamaz** — kısmi tekil indeks
    (``ACTIVE_PLAYBOOK_INDEX``) global aktif sınırı 1'de tutar. Bu yüzden ölçüm
    şöyle kurulur: birinci Job bitirilir, kuyruğa ikincisi girer ve onu alan
    hiçbir şey **olmaz**; ancak ikinci bir açık çağrı onu ele alır.
    """
    first = seed_job(db_session, runtime, source_project)

    attempt = run_once(session_factory, runtime)
    assert attempt.outcome is ExecutionOutcome.FINISHED
    assert attempt.job_id == first

    second = seed_job(db_session, runtime, source_project)
    # Heartbeat aralığının kat kat üstünde bekle: arka planda çalışan bir döngü
    # veya zamanlayıcı olsaydı bu pencerede Job'ı alırdı.
    time.sleep(0.5)
    waiting = read_job(migrated_engine, second)
    assert waiting.status is JobStatus.PENDING
    assert waiting.worker_id is None
    assert not artifact_dir(runtime, second).exists()
    assert run_directories(runtime) == []

    again = run_once(session_factory, runtime)
    assert again.job_id == second
    assert read_job(migrated_engine, second).status is JobStatus.SUCCESSFUL


def test_the_error_codes_come_from_the_shared_dictionary() -> None:
    """Executor **yeni** hata kodu icat etmez."""
    from app.services.execution.job_state import FINISH_ERROR_CODES

    produced = {
        ex.ERROR_WORKSPACE_UNAVAILABLE,
        ex.ERROR_WORKSPACE_INTEGRITY_FAILED,
        ex.ERROR_RUNNER_START_FAILED,
        ex.ERROR_RUNNER_FAILED,
    }
    assert produced <= FINISH_ERROR_CODES


def test_the_executor_never_classifies_a_run_as_a_playbook_failure() -> None:
    """Sınıflandırma normalize'ın işidir; executor onu yalnız taşır.

    Executor'ın kendi arıza dalları — artifact rezervasyonu/yazımı, run
    directory temizliği, kira kaybı ve beklenmeyen exception — çalıştırmanın
    **sonucu** hakkında hiçbir şey bilmez. Oralardan ``playbook_failed``
    çıkabilseydi bir altyapı arızası doğrulanmış bir Ansible bulgusu gibi
    görünürdü. Kaynak metni ölçülür: kodu adıyla yazan bir dal da, sabiti
    import eden bir dal da burada düşer.
    """
    from app.services.execution.normalize import ERROR_PLAYBOOK_FAILED

    assert not hasattr(ex, "ERROR_PLAYBOOK_FAILED")
    assert ERROR_PLAYBOOK_FAILED not in inspect.getsource(ex)


def test_the_http_surface_stays_at_the_single_launch_route() -> None:
    """Executor'ın kendisi hiçbir endpoint eklemez; launch yüzeyi R1-V3D1'de kaldığı yerdedir.

    R1-V3D1 tek bir launch endpoint'i açtı (15 → 16); R1-V3D2B üç GET okuma
    endpoint'i ekledi (16 → 19); R1-V3J0C tek bir salt-okunur controller path
    browse endpoint'i ekledi (19 → 20); R1-V3J1 kalıcı ping geçmişi için tek bir
    salt-okunur ``ping-runs`` endpoint'i ekledi (20 → 21). R1-V3J2 yalnız
    frontend cursor pagination'dı ve R1-V3J3A yalnız mevcut sonuç cevabını
    genişletti; ikisi de route eklemedi.

    Sayı hâlâ bir kilittir: executor tarafından ya da başka bir dilim tarafından
    sessizce eklenen fazladan bir yol testi düşürür.
    """
    routes = sorted(Path("app/api/routes").glob("*.py"))
    assert {route.name for route in routes} == {
        "__init__.py",
        "controller_paths.py",
        "executions.py",
        "health.py",
        "inventories.py",
        "jobs.py",
        "projects.py",
    }
    decorators = sum(module.read_text(encoding="utf-8").count("@router.") for module in routes)
    assert decorators == 21


def test_only_the_background_worker_calls_the_executor() -> None:
    """Executor'ı tekrar tekrar çağıran **tek** yer worker döngüsüdür.

    R1-V3C2C'ye kadar hiçbir çağıran yoktu; artık bir tane vardır ve liste
    **tam eşitlikle** ölçülür. Özellikle ``app/main.py`` ve ``app/api`` bu
    listede bulunmaz: bir HTTP isteğinin ya da lifespan'in doğrudan çalıştırma
    başlatabildiği ikinci bir yol, worker'ın eşzamanlılık sözünü sessizce
    geçersiz kılardı.
    """
    callers = [
        str(module)
        for module in sorted(Path("app").rglob("*.py"))
        if "execute_next_playbook_job" in module.read_text(encoding="utf-8")
        and module.name != "executor.py"
    ]
    assert callers == [
        "app/services/execution/__init__.py",
        "app/services/execution/worker.py",
    ]
    assert "execute_next_playbook_job" not in Path("app/main.py").read_text(encoding="utf-8")
    assert not any(
        "execute_next_playbook_job" in module.read_text(encoding="utf-8")
        for module in Path("app/api").rglob("*.py")
    )


# --- 10. Gerçek ansible-runner -----------------------------------------------


@pytest.fixture
def local_project(tmp_path: Path) -> Path:
    """Gerçek runner ile çalıştırılacak zararsız project ağacı."""
    root = tmp_path / "gercek-proje"
    root.mkdir()
    (root / PLAYBOOK_PATH).write_text(PLAYBOOK, encoding="utf-8")
    return root


@pytest.mark.skipif(
    not real_runner_available(),
    reason="ansible-runner bu platformda çalıştırılamıyor.",
)
def test_real_ansible_runner_completes_a_job_end_to_end(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    local_project: Path,
) -> None:
    """Gerçek `ansible-runner` 2.4.3 ile uçtan uca ölçüm.

    Subprocess katmanı **atlanmaz**: gerçek CLI, ürünün ürettiği gerçek argv ile
    çalışır ve sonucu ürünün kendi normalize/artifact/DB yolundan geçer. Dış
    network, SSH ve gerçek credential kullanılmaz; playbook yalnız zararsız
    ``debug``/``assert`` task'ları içerir.

    Fixture'daki ``ansible_connection: local`` bir **production inventory
    politikası değildir**: uygulamanın kendi snapshot üreticisi bu değişkeni
    bilinçli olarak snapshot'a yazmaz (tek kanonik yol ``ssh``'tir). Burada
    testin sahibi olduğu dondurulmuş snapshot'a elle konur ve tek işlevi,
    gerçek süreç entegrasyonunu dış bağlantı olmadan ölçebilmektir.
    """
    runtime = build_settings(
        settings,
        command=["ansible-runner"],
        playbook_runner_timeout_seconds=240.0,
    )
    ensure_app_data_dirs(runtime)
    job_id = seed_job(
        db_session,
        runtime,
        local_project,
        inventory_snapshot=json.dumps(
            {"all": {"hosts": {PROBE_HOST: {"ansible_connection": "local"}}}},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )

    attempt = run_once(session_factory, runtime)

    assert attempt.outcome is ExecutionOutcome.FINISHED
    assert attempt.status is JobStatus.SUCCESSFUL

    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.SUCCESSFUL
    assert row.return_code == 0
    assert row.error_code is None
    assert row.artifact_path == f"jobs/{job_id}/result.json"

    document = json.loads(result_file(runtime, job_id).read_text(encoding="utf-8"))
    assert document["outcome"] == "successful"
    assert document["recap"][PROBE_HOST]["ok"] == 2
    assert document["recap"][PROBE_HOST]["failures"] == 0
    assert {event["task"] for event in document["events"]} == {"say", "assert"}

    # Gerçek bir çalıştırmadan sonra da çalışma alanı geride kalmaz.
    assert run_directories(runtime) == []
    assert stat.S_IMODE(run_root(runtime).stat().st_mode) == 0o700


# --- R1-V3J3A: yayımlanan belgenin display output'u --------------------------


def test_the_published_result_carries_the_raw_display_output(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
) -> None:
    """Gerçek bir çalıştırmanın ``result.json``'ı ham display metnini taşır.

    Ölçülen zincir uçtan uca gerçektir: gerçek bir süreç üst düzey ``stdout``
    satırları üretir, normalize onları bounded biçimde toplar ve executor
    belgeyi ``schema_version=2`` olarak yayımlar. Sentinel'in belgede
    **bulunması** beklenen sonuçtur — sözleşme ham çıktıyı taşımaktır.
    """
    runtime = build_settings(settings, command=display_output_command())
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    attempt = run_once(session_factory, runtime)

    assert attempt.status is JobStatus.SUCCESSFUL
    row = read_job(migrated_engine, job_id)
    assert row.status is JobStatus.SUCCESSFUL
    assert row.artifact_path == f"jobs/{job_id}/result.json"

    document = json.loads(result_file(runtime, job_id).read_text(encoding="utf-8"))

    assert set(document) == RESULT_DOCUMENT_FIELDS
    assert document["schema_version"] == 2
    assert document["ansible_output"] == (f"TASK [Ping] ****\n{DISPLAY_SENTINEL}\nPLAY RECAP ****")
    assert document["ansible_output_truncated"] is False
    # Structured yüzey değişmez ve ham metni **tekrar** taşımaz.
    assert set(document["recap"]) == {PROBE_HOST}
    for event in document["events"]:
        assert set(event) == {"event", "host", "task", "changed", "failed"}
        assert "SENTINEL-DISPLAY-PW" not in json.dumps(event)

    # Ham run directory yine kaldırılır: çıktı artifact'e taşındı, diskte kalmadı.
    assert run_directories(runtime) == []


def test_the_display_output_never_reaches_the_job_row_or_the_attempt(
    db_session: Session,
    settings: Settings,
    session_factory: Callable[[], Session],
    migrated_engine: Engine,
    source_project: Path,
) -> None:
    """Ham metin **yalnız** artifact'tedir: Job satırına ve attempt'e girmez."""
    runtime = build_settings(settings, command=display_output_command())
    ensure_app_data_dirs(runtime)
    job_id = seed_job(db_session, runtime, source_project)

    attempt = run_once(session_factory, runtime)
    published = result_file(runtime, job_id).read_text(encoding="utf-8")
    row = read_job(migrated_engine, job_id)

    # Vacuous değil: metin gerçekten yayımlanmış belgede.
    assert "SENTINEL-DISPLAY-PW" in published

    for surface in (repr(attempt), str(attempt), repr(row), str(dict(row._mapping))):
        assert "SENTINEL-DISPLAY-PW" not in surface
    assert row.artifact_path == f"jobs/{job_id}/result.json"
