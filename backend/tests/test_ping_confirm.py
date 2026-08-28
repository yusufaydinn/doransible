"""Ping confirm orkestrasyonu ve API sözleşmesi (T-204B2).

Preview state'i burada **doğrudan depoya yayımlanır**: bu dosyanın konusu
onaylanmış bir planın nasıl çalıştırıldığıdır, planın nasıl üretildiği değil
(o T-204A testlerindedir). Böylece confirm regresyonları `ansible-inventory`
kurulumundan bağımsız çalışır ve hiçbir platformda atlanmaz.

`ansible` ad-hoc komutu yerine gerçek bir süreç olarak çalışan stub konur;
subprocess katmanı (argüman aktarımı, timeout, çıktı sınırı) taklit edilmez.
Gerçek Ansible ile kapalı-port doğrulaması `test_ping_confirm_real.py`
içindedir.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import uuid
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.models import Inventory, InventorySourceType, Job, JobStatus, JobType
from app.services.ansible.inventory_snapshot import InventoryUnsafeError
from app.services.ansible.ping_execution import PingInvalidOutputError
from app.services.ansible.process import ProcessOutcome
from app.services.inventories import ping_confirm
from app.services.inventories.ping_confirm import (
    AnsibleUnavailableError,
    PingArtifactWriteFailedError,
    PingJobArtifactUnavailableError,
    PingKnownHostsUnavailableError,
    PingOutputTooLargeError,
    PingRun,
    PingTimeoutError,
    confirm_ping,
)
from app.services.jobs.artifacts import (
    RESULT_FILENAME,
    JobArtifactStore,
    JobArtifactUnavailableError,
)
from app.services.jobs.preview import PreviewNotFoundError, PreviewStore, token_digest
from app.services.jobs.service import ActivePingJobConflictError
from tests.support import stub_ping_command

HOSTS = ("db01", "web01", "web02")


# --- Ortak yardımcılar --------------------------------------------------------


def _snapshot(hosts: Sequence[str] = HOSTS) -> str:
    """Preview'ın ürettiğiyle aynı dar yapıda bir hedef snapshot'ı."""
    document = {
        "all": {
            "hosts": {
                name: {"ansible_host": "127.0.0.1", "ansible_port": 1} for name in sorted(hosts)
            }
        }
    }
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _meta(inventory_id: int, requested_by: str, hosts: Sequence[str], limit: str | None) -> dict:
    return {
        "schema_version": 1,
        "inventory_id": inventory_id,
        "requested_by": requested_by,
        "limit": limit,
        "host_count": len(hosts),
        "host_key_policy": "strict",
        "operation": "ansible.builtin.ping",
    }


@pytest.fixture
def store(settings: Settings) -> PreviewStore:
    return PreviewStore(
        settings.resolve_ping_preview_dir(),
        ttl_seconds=settings.ping_preview_ttl_seconds,
        claim_stale_seconds=settings.ping_preview_claim_stale_seconds,
    )


@pytest.fixture
def artifacts(settings: Settings) -> JobArtifactStore:
    return JobArtifactStore(settings.app_data_dir)


@pytest.fixture
def inventory(db_session: Session, inventory_root: Path) -> Inventory:
    """Kayıtlı bir standalone inventory.

    Dosya gerçekten yazılır; confirm'in onu **açmadığı** ancak var olan bir
    dosyayla anlamlı biçimde ölçülebilir.
    """
    path = inventory_root / "hosts.ini"
    path.write_text("[web]\nweb01 ansible_host=127.0.0.1\n", encoding="utf-8")
    item = Inventory(name="lab", path=str(path), source_type=InventorySourceType.INI)
    db_session.add(item)
    db_session.commit()
    return item


@pytest.fixture
def publish(store: PreviewStore, settings: Settings, inventory: Inventory) -> Callable[..., str]:
    """Onaylanmış bir plan yayımlar ve token'ını döndürür."""

    def _publish(
        *,
        hosts: Sequence[str] = HOSTS,
        limit: str | None = None,
        inventory_id: int | None = None,
        requested_by: str | None = None,
    ) -> str:
        target = inventory.id if inventory_id is None else inventory_id
        actor = settings.local_actor if requested_by is None else requested_by
        token, _ = store.publish(
            meta=_meta(target, actor, hosts, limit),
            snapshot_text=_snapshot(hosts),
        )
        return token

    return _publish


@pytest.fixture
def confirm(
    db_session: Session,
    settings: Settings,
    store: PreviewStore,
    artifacts: JobArtifactStore,
    inventory: Inventory,
) -> Callable[..., PingRun]:
    """Servis katmanını ayarlarla bağlayan çağrı yardımcısı."""

    def _confirm(
        token: str,
        *,
        behaviour: str = "success",
        session: Session | None = None,
        inventory_id: int | None = None,
        store_override: PreviewStore | None = None,
        artifacts_override: JobArtifactStore | None = None,
        command: Sequence[str] | None = None,
        **options: Any,
    ) -> PingRun:
        return confirm_ping(
            session if session is not None else db_session,
            inventory.id if inventory_id is None else inventory_id,
            preview_token=token,
            store=store_override if store_override is not None else store,
            artifacts=artifacts_override if artifacts_override is not None else artifacts,
            key_roots=settings.resolve_ssh_key_root_allowlist(),
            command=command if command is not None else stub_ping_command(behaviour, **options),
            app_data_dir=settings.app_data_dir,
            known_hosts_path=settings.ssh_known_hosts_path,
            host_key_policy=settings.ssh_host_key_policy,
            forks=settings.ping_forks,
            connect_timeout=settings.ssh_connect_timeout_seconds,
            timeout_seconds=settings.ping_timeout_seconds,
            max_output_bytes=settings.ping_max_output_bytes,
            job_stale_seconds=settings.job_stale_seconds,
            requested_by=settings.local_actor,
        )

    return _confirm


def _jobs(session: Session) -> list[Job]:
    """Kalıcı Job kayıtlarını taze okur.

    ``rollback`` bilinçlidir: bu session'da açık kalmış bir okuma
    transaction'ı, başka bir bağlantının commit ettiği satırları gizleyebilir.
    """
    session.rollback()
    return list(session.execute(select(Job).order_by(Job.created_at)).scalars().all())


def _job_dirs(settings: Settings) -> list[Path]:
    root = settings.app_data_dir / "jobs"
    return sorted(root.iterdir()) if root.is_dir() else []


def _preview_dirs(settings: Settings) -> list[Path]:
    root = settings.resolve_ping_preview_dir()
    return sorted(root.iterdir()) if root.is_dir() else []


def _details(error: AppError) -> dict[str, Any]:
    """Hata detayını sözlük olarak daraltır."""
    assert isinstance(error.details, dict)
    return cast(dict[str, Any], error.details)


def _result(settings: Settings, job_id: str) -> dict[str, Any]:
    raw = (settings.app_data_dir / "jobs" / job_id / RESULT_FILENAME).read_text("utf-8")
    return cast(dict[str, Any], json.loads(raw))


def _stale_job(
    session: Session,
    inventory_id: int,
    *,
    status: JobStatus,
    created_at: datetime,
    started_at: datetime | None,
) -> Job:
    job = Job(
        id=str(uuid.uuid4()),
        job_type=JobType.PING,
        status=status,
        inventory_id=inventory_id,
        requested_by="actor",
        created_at=created_at,
        started_at=started_at,
    )
    session.add(job)
    session.commit()
    return job


@pytest.fixture
def recorded_processes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Başlatılan bütün alt süreçlerin argv'sini kaydeder."""
    invocations: list[list[str]] = []
    real_popen = subprocess.Popen

    def _capture(args: Any, **kwargs: Any) -> Any:
        invocations.append(list(args))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _capture)
    return invocations


# --- Mutlu yol ----------------------------------------------------------------


def test_reachable_run_is_successful_and_deterministically_ordered(
    confirm: Callable[..., PingRun], publish: Callable[..., str], db_session: Session
) -> None:
    run = confirm(publish())

    assert run.status == "successful"
    assert run.job_type == "ping"
    assert run.return_code == 0
    assert [host.name for host in run.hosts] == ["db01", "web01", "web02"]
    assert {host.status for host in run.hosts} == {"reachable"}
    # Reachable mesajı normalde boştur: "pong" bilgi taşımaz.
    assert {host.message for host in run.hosts} == {None}
    assert run.summary.total == 3
    assert run.summary.reachable == 3
    assert run.summary.unreachable == 0
    assert run.started_at <= run.finished_at

    jobs = _jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].id == run.job_id
    assert jobs[0].status is JobStatus.SUCCESSFUL
    assert jobs[0].artifact_path == f"jobs/{run.job_id}/{RESULT_FILENAME}"
    assert jobs[0].started_at is not None and jobs[0].finished_at is not None


def test_command_is_the_fixed_ping_without_limit_or_original_inventory(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    inventory: Inventory,
    recorded_processes: list[list[str]],
) -> None:
    confirm(publish(limit="web"))

    assert len(recorded_processes) == 1
    argv = recorded_processes[0]
    snapshot = argv[argv.index("-i") + 1]
    assert argv[-9:] == [
        "all",
        "-i",
        snapshot,
        "-m",
        "ping",
        "--forks",
        "10",
        "-T",
        "10",
    ]
    # Onaylanmış limit snapshot'a **çözülmüştür**; komuta hiç geçmez.
    assert "--limit" not in argv
    assert "web" not in argv
    assert inventory.path not in argv
    assert Path(snapshot).name == "inventory-targets.yml"
    assert not any(part in {"ssh", "sshpass", "shell", "command"} for part in argv)


@pytest.mark.parametrize(
    ("behaviour", "expected"),
    [
        ("unreachable", ("failed", "unreachable", 4)),
        ("mixed", ("failed", None, 2)),
    ],
)
def test_valid_ansible_failures_are_not_infrastructure_errors(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    behaviour: str,
    expected: tuple[str, str | None, int],
) -> None:
    """rc=2/4 gibi geçerli sonuçlar hata değildir; Job `failed` olur."""
    status, host_status, return_code = expected

    run = confirm(publish(), behaviour=behaviour)

    assert run.status == status
    assert run.return_code == return_code
    if host_status is not None:
        assert {host.status for host in run.hosts} == {host_status}
    assert _jobs(db_session)[0].status is JobStatus.FAILED


def test_missing_host_result_is_reported_as_no_result(
    confirm: Callable[..., PingRun], publish: Callable[..., str]
) -> None:
    """Beklenen bir host için sonuç yoksa sessizce başarılı sayılmaz."""
    run = confirm(publish(), behaviour="partial")

    assert run.status == "failed"
    assert run.summary.reachable == 1
    assert run.summary.no_result == 2
    assert [host.message for host in run.hosts if host.status == "no_result"] == [None, None]


def test_ansible_219_diagnostic_block_does_not_hide_a_mixed_result(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
) -> None:
    """rc=4 + Ansible-core 2.19 tanı bloğu, gerçek 4+1 sonucu kaybetmemeli.

    Canlı bulgu: 5 hostluk bir inventory'de dördü erişilebilir, biri kapalı.
    Ansible-core 2.19, UNREACHABLE bloğunun hemen önünde kendi tanısal
    [ERROR]/Origin/dict bloğunu basar; parser bunu yabancı metin sanıp
    `ping_invalid_output` üretmemeli.
    """
    hosts = (
        "ubuntu-demo-2",
        "ubuntu-demo-3",
        "ubuntu-demo-4",
        "ubuntu-demo-5",
        "ubuntu-demo-6",
    )

    run = confirm(publish(hosts=hosts), behaviour="ansible-2-19-mixed")

    # Job başarısız sayılır (bir host erişilemez) ama arızası
    # `ping_invalid_output` değildir: parser normal biçimde sonuç üretmiştir.
    assert run.status == "failed"
    assert run.return_code == 4
    assert run.summary.total == 5
    assert run.summary.reachable == 4
    assert run.summary.unreachable == 1
    assert run.summary.failed == 0
    assert run.summary.no_result == 0

    by_name = {host.name: host for host in run.hosts}
    assert by_name["ubuntu-demo-6"].status == "unreachable"
    for name in ("ubuntu-demo-2", "ubuntu-demo-3", "ubuntu-demo-4", "ubuntu-demo-5"):
        assert by_name[name].status == "reachable"
        assert by_name[name].message is None

    # Tanı bloğu hiçbir sonuca veya artifact'e taşınmaz.
    document = json.dumps(_result(settings, run.job_id))
    for leak in ("[ERROR]", "Origin:", "adhoc 'ping' task", "async_val"):
        assert leak not in document

    # Bağlantı adresi ve portu — tanı bloğunda da, UNREACHABLE mesajında da —
    # response'a ya da artifact'e sızmaz.
    assert by_name["ubuntu-demo-6"].message is not None
    assert "127.0.0.1" not in by_name["ubuntu-demo-6"].message
    assert "port 1" not in by_name["ubuntu-demo-6"].message
    assert "***" in by_name["ubuntu-demo-6"].message
    assert "127.0.0.1" not in document

    assert _jobs(db_session)[0].status is JobStatus.FAILED


def test_connection_values_are_masked_in_host_messages(
    confirm: Callable[..., PingRun], publish: Callable[..., str], settings: Settings
) -> None:
    """Adres ve port mesaj yoluyla geri sızmaz.

    Onay planı bu hostvar'ları bilinçli olarak dışarı vermez; Ansible'ın
    bağlantı hatası onları geri taşırdı.
    """
    run = confirm(publish(), behaviour="echo-destination")

    document = json.dumps(_result(settings, run.job_id))
    for host in run.hosts:
        assert host.message is not None
        assert "127.0.0.1" not in host.message
        assert "port 1:" not in host.message
        assert "***" in host.message
        assert "Connection refused" in host.message
        # Host **adı** maskelenmez: o zaten planın parçasıdır.
        assert host.name in {"db01", "web01", "web02"}
    assert "127.0.0.1" not in document


def test_host_messages_are_redacted_and_path_masked(
    confirm: Callable[..., PingRun], publish: Callable[..., str], settings: Settings
) -> None:
    run = confirm(publish(), behaviour="leaky")

    for host in run.hosts:
        assert host.status == "unreachable"
        assert host.message is not None
        assert "hunter2" not in host.message
        assert "/root" not in host.message
    document = _result(settings, run.job_id)
    assert "hunter2" not in json.dumps(document)
    assert "/root" not in json.dumps(document)


# --- Onaylanan plan dondurulmuştur --------------------------------------------


@pytest.mark.parametrize("mutation", ["rewrite", "delete", "chmod"])
def test_claimed_snapshot_is_used_even_if_the_inventory_file_changes(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    inventory: Inventory,
    mutation: str,
) -> None:
    """Onay ile çalıştırma arasındaki değişiklik hedef kümesini etkilemez."""
    token = publish()
    path = Path(inventory.path)
    if mutation == "rewrite":
        path.write_text("[web]\nsaldirgan ansible_host=203.0.113.9\n", encoding="utf-8")
    elif mutation == "delete":
        path.unlink()
    else:
        os.chmod(path, 0o000)

    run = confirm(token)

    assert [host.name for host in run.hosts] == list(HOSTS)
    assert run.status == "successful"


def test_confirm_never_opens_the_original_inventory_path(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    inventory: Inventory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Özgün dosya hiçbir katmanda açılmaz — ne uygulama ne alt süreç."""
    token = publish()
    opened: list[str] = []
    real_open = os.open
    target = os.path.realpath(inventory.path)

    def _record(path: Any, *args: Any, **kwargs: Any) -> int:
        if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path) == inventory.path:
            opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _record)

    run = confirm(token)

    assert opened == []
    assert run.status == "successful"
    assert Path(target).read_text(encoding="utf-8")  # dosya el değmemiş durur


@pytest.mark.parametrize("attack", ["delete", "outside", "symlink"])
def test_private_key_is_revalidated_before_any_process_starts(
    confirm: Callable[..., PingRun],
    store: PreviewStore,
    settings: Settings,
    inventory: Inventory,
    secrets_root: Path,
    tmp_path: Path,
    recorded_processes: list[list[str]],
    attack: str,
) -> None:
    """Preview'daki anahtar doğrulaması kalıcı garanti değildir."""
    key = secrets_root / "id_ed25519"
    key.write_text("anahtar", encoding="utf-8")
    outside = tmp_path / "disarida"
    outside.write_text("anahtar", encoding="utf-8")
    snapshot = json.dumps(
        {
            "all": {
                "hosts": {
                    "web01": {
                        "ansible_host": "127.0.0.1",
                        "ansible_ssh_private_key_file": str(
                            outside if attack == "outside" else key
                        ),
                    }
                }
            }
        }
    )
    token, _ = store.publish(
        meta=_meta(inventory.id, settings.local_actor, ("web01",), None),
        snapshot_text=snapshot,
    )
    if attack == "delete":
        key.unlink()
    elif attack == "symlink":
        key.unlink()
        os.symlink(outside, key)
        os.chmod(outside, 0o600)

    with pytest.raises(InventoryUnsafeError) as error:
        confirm(token)

    assert error.value.status_code == 422
    assert error.value.code == "ping_inventory_unsafe"
    assert recorded_processes == []
    assert _job_dirs(settings) == []


# --- Token sözleşmesi ---------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    ["a" * 43, "kisa", "../../etc/passwd", "GIZLI" + "x" * 200],
    ids=["unknown", "malformed", "traversal", "oversized"],
)
def test_unusable_tokens_start_no_execution(
    confirm: Callable[..., PingRun],
    settings: Settings,
    db_session: Session,
    recorded_processes: list[list[str]],
    token: str,
) -> None:
    with pytest.raises(PreviewNotFoundError):
        confirm(token)

    assert recorded_processes == []
    assert _jobs(db_session) == []
    assert _job_dirs(settings) == []


def test_replayed_token_cannot_run_twice(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
) -> None:
    token = publish()
    first = confirm(token)

    with pytest.raises(PreviewNotFoundError):
        confirm(token)

    assert len(_jobs(db_session)) == 1
    assert _jobs(db_session)[0].id == first.job_id


def test_expired_token_is_rejected(
    confirm: Callable[..., PingRun],
    store: PreviewStore,
    settings: Settings,
    inventory: Inventory,
    db_session: Session,
    recorded_processes: list[list[str]],
) -> None:
    token, _ = store.publish(
        meta=_meta(inventory.id, settings.local_actor, HOSTS, None),
        snapshot_text=_snapshot(),
        now=datetime.now(UTC) - timedelta(seconds=settings.ping_preview_ttl_seconds + 60),
    )

    with pytest.raises(PreviewNotFoundError) as error:
        confirm(token)

    assert error.value.details == {"reason": "expired"}
    assert recorded_processes == []
    assert _jobs(db_session) == []


@pytest.mark.parametrize("mismatch", ["inventory", "actor"])
def test_token_bound_to_another_context_is_rejected(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
    recorded_processes: list[list[str]],
    mismatch: str,
) -> None:
    token = publish(
        inventory_id=4242 if mismatch == "inventory" else None,
        requested_by="baska-aktor" if mismatch == "actor" else None,
    )

    with pytest.raises(PreviewNotFoundError):
        confirm(token)

    assert recorded_processes == []
    assert _jobs(db_session) == []


@pytest.mark.parametrize(
    ("approved", "current"), [("strict", "accept_new"), ("accept_new", "strict")]
)
def test_host_key_policy_change_after_the_preview_blocks_execution(
    confirm: Callable[..., PingRun],
    store: PreviewStore,
    settings: Settings,
    inventory: Inventory,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    recorded_processes: list[list[str]],
    approved: str,
    current: str,
) -> None:
    """Onaylanan host-key politikası ile çalıştırılan politika ayrışamaz.

    `strict` ile onaylanmış bir plan, ayar arada `accept_new` yapıldığında
    kullanıcının görmediği daha gevşek bir politikayla koşardı; ters yön de
    onaylanandan farklı bir işi çalıştırırdı. Eski politikayı kullanmak da
    çözüm değildir: o, güncel yönetici ayarını sessizce delerdi.
    """
    meta = _meta(inventory.id, settings.local_actor, HOSTS, None)
    meta["host_key_policy"] = approved
    token, _ = store.publish(meta=meta, snapshot_text=_snapshot())
    settings.ssh_host_key_policy = current
    workspaces: list[Path] = []
    real_mkdtemp = ping_confirm.tempfile.mkdtemp

    def _record(*args: Any, **kwargs: Any) -> str:
        created = str(real_mkdtemp(*args, **kwargs))
        workspaces.append(Path(created))
        return created

    monkeypatch.setattr(ping_confirm.tempfile, "mkdtemp", _record)

    with pytest.raises(PreviewNotFoundError) as error:
        confirm(token)

    assert error.value.status_code == 409
    assert error.value.code == "ping_preview_invalid"
    assert error.value.details == {"reason": "mismatch"}
    # Workspace, known_hosts, Job ve artifact'ten önce durulur.
    assert recorded_processes == []
    assert workspaces == []
    assert _jobs(db_session) == []
    assert _job_dirs(settings) == []
    assert not (settings.app_data_dir / "ssh" / "known_hosts").exists()
    # Token tüketilmiştir: aynı plan yeniden denenemez.
    assert _preview_dirs(settings) == []
    with pytest.raises(PreviewNotFoundError):
        confirm(token)


def test_tampered_snapshot_is_rejected_by_the_digest(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    settings: Settings,
    db_session: Session,
    recorded_processes: list[list[str]],
) -> None:
    """Dondurulmuş snapshot onay ile çalıştırma arasında değiştirilemez."""
    token = publish()
    snapshot = _preview_dirs(settings)[0] / "inventory-targets.yml"
    snapshot.write_text(_snapshot(("saldirgan",)), encoding="utf-8")

    with pytest.raises(PreviewNotFoundError) as error:
        confirm(token)

    assert error.value.details == {"reason": "mismatch"}
    assert recorded_processes == []
    assert _jobs(db_session) == []


def test_concurrent_use_of_one_token_runs_exactly_once(
    publish: Callable[..., str],
    confirm: Callable[..., PingRun],
    migrated_engine: Engine,
    db_session: Session,
    settings: Settings,
) -> None:
    token = publish()

    def _attempt() -> str:
        with Session(migrated_engine, expire_on_commit=False) as session:
            try:
                confirm(token, session=session)
            except PreviewNotFoundError:
                return "rejected"
            return "ran"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(future.result() for future in [pool.submit(_attempt) for _ in range(2)])

    assert results == ["ran", "rejected"]
    assert len(_jobs(db_session)) == 1
    assert len(_job_dirs(settings)) == 1


def test_token_is_never_written_to_disk_or_into_the_result(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    settings: Settings,
) -> None:
    token = publish()
    run = confirm(token)

    document = json.dumps(_result(settings, run.job_id))
    assert token not in document
    assert token_digest(token) not in document
    for candidate in settings.app_data_dir.rglob("*"):
        if candidate.is_file():
            assert token.encode() not in candidate.read_bytes()


# --- Aktif Job ve stale kurtarma ----------------------------------------------


def test_fresh_active_job_conflicts_and_still_consumes_the_token(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    inventory: Inventory,
    settings: Settings,
    recorded_processes: list[list[str]],
) -> None:
    """Çatışma olsa bile token tüketilmiş kalır: claim en başta yapılır."""
    now = datetime.now(UTC)
    active = _stale_job(
        db_session, inventory.id, status=JobStatus.RUNNING, created_at=now, started_at=now
    )
    token = publish()

    with pytest.raises(ActivePingJobConflictError) as error:
        confirm(token)

    assert error.value.status_code == 409
    assert error.value.code == "job_already_running"
    assert error.value.details == {"job_id": active.id}
    assert recorded_processes == []
    assert _preview_dirs(settings) == []
    with pytest.raises(PreviewNotFoundError):
        confirm(token)


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_stale_job_is_recovered_and_the_new_ping_proceeds(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    inventory: Inventory,
    status: JobStatus,
) -> None:
    old = datetime.now(UTC) - timedelta(hours=1)
    stale = _stale_job(
        db_session,
        inventory.id,
        status=status,
        created_at=old,
        started_at=old if status is JobStatus.RUNNING else None,
    )

    run = confirm(publish())

    jobs = {job.id: job for job in _jobs(db_session)}
    assert jobs[stale.id].status is JobStatus.FAILED
    assert jobs[run.job_id].status is JobStatus.SUCCESSFUL
    assert run.status == "successful"


def test_running_job_staleness_uses_started_at_not_created_at(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    inventory: Inventory,
) -> None:
    """Uzun süre önce oluşturulmuş ama **az önce başlamış** iş taze sayılır."""
    _stale_job(
        db_session,
        inventory.id,
        status=JobStatus.RUNNING,
        created_at=datetime.now(UTC) - timedelta(hours=5),
        started_at=datetime.now(UTC),
    )

    with pytest.raises(ActivePingJobConflictError):
        confirm(publish())


def test_pending_job_staleness_uses_created_at(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    inventory: Inventory,
) -> None:
    """Pending kaydın `started_at`'i yoktur; karar `created_at` üzerindedir."""
    fresh = _stale_job(
        db_session,
        inventory.id,
        status=JobStatus.PENDING,
        created_at=datetime.now(UTC),
        started_at=None,
    )

    with pytest.raises(ActivePingJobConflictError):
        confirm(publish())

    assert {job.id: job.status for job in _jobs(db_session)} == {fresh.id: JobStatus.PENDING}


def test_stale_recovery_preserves_a_published_result(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    inventory: Inventory,
    artifacts: JobArtifactStore,
    settings: Settings,
) -> None:
    """Kurtarma, operatör incelemesi için duran sonucu silmez."""
    old = datetime.now(UTC) - timedelta(hours=1)
    stale = _stale_job(
        db_session, inventory.id, status=JobStatus.RUNNING, created_at=old, started_at=old
    )
    artifacts.create(stale.id)
    artifacts.write_result(stale.id, {"schema_version": 1, "job_id": stale.id})

    run = confirm(publish())

    assert run.status == "successful"
    assert _result(settings, stale.id)["job_id"] == stale.id


def test_stale_recovery_removes_an_unpublished_directory(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    inventory: Inventory,
    artifacts: JobArtifactStore,
    settings: Settings,
) -> None:
    old = datetime.now(UTC) - timedelta(hours=1)
    stale = _stale_job(
        db_session, inventory.id, status=JobStatus.PENDING, created_at=old, started_at=None
    )
    artifacts.create(stale.id)

    confirm(publish())

    assert not (settings.app_data_dir / "jobs" / stale.id).exists()


def test_unexpected_content_in_a_stale_directory_is_not_hidden(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    inventory: Inventory,
    artifacts: JobArtifactStore,
    settings: Settings,
    recorded_processes: list[list[str]],
) -> None:
    old = datetime.now(UTC) - timedelta(hours=1)
    stale = _stale_job(
        db_session, inventory.id, status=JobStatus.PENDING, created_at=old, started_at=None
    )
    artifacts.create(stale.id)
    foreign = settings.app_data_dir / "jobs" / stale.id / "beklenmeyen.bin"
    foreign.write_text("veri", encoding="utf-8")

    with pytest.raises(PingJobArtifactUnavailableError):
        confirm(publish())

    assert foreign.read_text(encoding="utf-8") == "veri"
    assert recorded_processes == []


def test_unique_index_race_rolls_back_and_leaves_no_artifact(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    inventory: Inventory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    recorded_processes: list[list[str]],
) -> None:
    """Ön sorgu yarışı kaçırsa bile asıl garanti partial unique index'tir.

    Ön kontrol bilinçli olarak devre dışı bırakılır: eşzamanlı iki isteğin
    aralarında geçen kısa pencere böyle deterministik biçimde kurulur.
    """
    now = datetime.now(UTC)
    active = _stale_job(
        db_session, inventory.id, status=JobStatus.RUNNING, created_at=now, started_at=now
    )
    monkeypatch.setattr(ping_confirm, "_release_stale_or_conflict", lambda *a, **k: None)

    with pytest.raises(ActivePingJobConflictError) as error:
        confirm(publish())

    assert error.value.details == {"job_id": active.id}
    assert [job.id for job in _jobs(db_session)] == [active.id]
    assert _job_dirs(settings) == []
    assert recorded_processes == []


def test_two_concurrent_confirms_never_leave_two_active_jobs(
    publish: Callable[..., str],
    confirm: Callable[..., PingRun],
    migrated_engine: Engine,
    db_session: Session,
    settings: Settings,
) -> None:
    """Farklı token'larla eşzamanlı iki istek tutarlı bir duruma yerleşir."""
    tokens = [publish(), publish()]
    barrier = Barrier(2)

    def _attempt(token: str) -> str:
        barrier.wait(timeout=10)
        with Session(migrated_engine, expire_on_commit=False) as session:
            try:
                confirm(token, session=session)
            except (ActivePingJobConflictError, PingJobArtifactUnavailableError):
                return "conflict"
            return "ran"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(
            future.result() for future in [pool.submit(_attempt, token) for token in tokens]
        )

    assert "ran" in results
    jobs = _jobs(db_session)
    assert [job for job in jobs if job.status in {JobStatus.PENDING, JobStatus.RUNNING}] == []
    # Yarışı kaybeden istek geride yetim bir artifact dizini bırakmaz.
    assert len(_job_dirs(settings)) == len(jobs)


# --- Transaction sınırları ----------------------------------------------------


def test_no_transaction_is_open_while_the_subprocess_runs(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ping timeout'u kadar süren bir yazma kilidi uygulamayı bloklardı."""
    observed: list[bool] = []

    def _fake_run(**kwargs: Any) -> ProcessOutcome:
        observed.append(db_session.in_transaction())
        stdout = "".join(f'{host} | SUCCESS => {{"ping": "pong"}}\n' for host in HOSTS)
        return ProcessOutcome(
            return_code=0,
            stdout_text=stdout,
            stderr_text="",
            timed_out=False,
            oversized_stream=None,
        )

    monkeypatch.setattr(ping_confirm, "run_ping_process", _fake_run)

    run = confirm(publish())

    assert observed == [False]
    assert run.status == "successful"


def _failing_commit(session: Session, monkeypatch: pytest.MonkeyPatch, *, at_call: int) -> None:
    """Belirtilen sıradaki ``commit`` çağrısını başarısız kılar."""
    real_commit = session.commit
    calls = {"count": 0}

    def _commit() -> None:
        calls["count"] += 1
        if calls["count"] == at_call:
            raise OperationalError("injected", None, Exception("injected"))
        real_commit()

    monkeypatch.setattr(session, "commit", _commit)


def test_first_transaction_commit_failure_leaves_no_job_or_artifact(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    recorded_processes: list[list[str]],
) -> None:
    # 1: aktif Job ön kontrolü, 2: T1, 3: T2, 4: T3.
    _failing_commit(db_session, monkeypatch, at_call=2)

    with pytest.raises(PingJobArtifactUnavailableError) as error:
        confirm(publish())

    assert error.value.code == "ping_artifact_unavailable"
    assert _jobs(db_session) == []
    assert _job_dirs(settings) == []
    assert recorded_processes == []


def test_second_transaction_commit_failure_stops_before_execution(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    recorded_processes: list[list[str]],
) -> None:
    """T2 arızasında pending Job ve boş dizin kalabilir; stale recovery toplar."""
    _failing_commit(db_session, monkeypatch, at_call=3)

    with pytest.raises(PingJobArtifactUnavailableError):
        confirm(publish())

    assert recorded_processes == []
    jobs = _jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].status is JobStatus.PENDING
    assert list((settings.app_data_dir / "jobs" / jobs[0].id).iterdir()) == []


def test_third_transaction_commit_failure_preserves_the_result(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yayımlanmış `result.json` terminal commit hatasında silinmez."""
    _failing_commit(db_session, monkeypatch, at_call=4)

    with pytest.raises(PingArtifactWriteFailedError) as error:
        confirm(publish())

    job_id = _details(error.value)["job_id"]
    assert error.value.code == "ping_artifact_write_failed"
    assert set(_details(error.value)) == {"job_id"}
    assert _result(settings, job_id)["status"] == "successful"


def test_finish_rowcount_zero_is_reported_without_deleting_the_result(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ping_confirm, "finish_job", lambda *args, **kwargs: False)

    with pytest.raises(PingArtifactWriteFailedError) as error:
        confirm(publish())

    job_id = _details(error.value)["job_id"]
    assert _result(settings, job_id)["job_id"] == job_id


class _FailingCreate(JobArtifactStore):
    def create(self, job_id: str) -> str:
        raise JobArtifactUnavailableError("injected")


class _FailingWrite(JobArtifactStore):
    def write_result(self, job_id: str, result: dict[str, Any]) -> str:
        raise JobArtifactUnavailableError("injected")


def test_artifact_directory_failure_rolls_back_the_job(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
    recorded_processes: list[list[str]],
) -> None:
    with pytest.raises(PingJobArtifactUnavailableError):
        confirm(publish(), artifacts_override=_FailingCreate(settings.app_data_dir))

    assert _jobs(db_session) == []
    assert _job_dirs(settings) == []
    assert recorded_processes == []


def test_result_write_failure_finalizes_the_job_as_failed(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
) -> None:
    with pytest.raises(PingArtifactWriteFailedError) as error:
        confirm(publish(), artifacts_override=_FailingWrite(settings.app_data_dir))

    jobs = _jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].status is JobStatus.FAILED
    assert jobs[0].artifact_path is None
    assert error.value.details == {"job_id": jobs[0].id}


# --- Execution arıza eşlemesi -------------------------------------------------


def test_timeout_produces_a_terminal_failed_job_and_safe_artifact(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
) -> None:
    settings.ping_timeout_seconds = 0.5

    with pytest.raises(PingTimeoutError) as error:
        confirm(publish(), behaviour="sleep", sleep_seconds=30)

    assert error.value.status_code == 504
    jobs = _jobs(db_session)
    assert jobs[0].status is JobStatus.FAILED
    document = _result(settings, jobs[0].id)
    assert {host["status"] for host in document["hosts"]} == {"no_result"}
    assert document["summary"]["no_result"] == 3


def test_output_limit_reports_only_the_stream_name(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
) -> None:
    settings.ping_max_output_bytes = 1024

    with pytest.raises(PingOutputTooLargeError) as error:
        confirm(publish(), behaviour="flood", size_bytes=500_000, sleep_seconds=30)

    assert error.value.status_code == 502
    assert error.value.details == {"stream": "stdout"}
    assert _jobs(db_session)[0].status is JobStatus.FAILED


def test_missing_ansible_binary_is_reported_as_unavailable(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
    tmp_path: Path,
) -> None:
    with pytest.raises(AnsibleUnavailableError) as error:
        confirm(publish(), command=[str(tmp_path / "boyle-bir-ikili-yok")])

    assert error.value.status_code == 503
    jobs = _jobs(db_session)
    assert jobs[0].status is JobStatus.FAILED
    assert _result(settings, jobs[0].id)["return_code"] is None


def test_unexpected_runner_exception_never_leaves_the_job_running(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beklenmeyen bir istisna Job'u `running` asılı bırakmaz.

    Aksi hâlde tek bir arıza, inventory'yi stale eşiği dolana kadar
    ping'lenemez hâle getirirdi.
    """
    workspaces: list[Path] = []
    real_mkdtemp = ping_confirm.tempfile.mkdtemp

    def _record(*args: Any, **kwargs: Any) -> str:
        created = str(real_mkdtemp(*args, **kwargs))
        workspaces.append(Path(created))
        return created

    def _explode(**_kwargs: Any) -> ProcessOutcome:
        raise RuntimeError("GIZLI_AYRINTI /srv/gizli/argv --key /root/.ssh/id_rsa")

    monkeypatch.setattr(ping_confirm.tempfile, "mkdtemp", _record)
    monkeypatch.setattr(ping_confirm, "run_ping_process", _explode)

    with pytest.raises(AnsibleUnavailableError) as error:
        confirm(publish())

    assert error.value.status_code == 503
    assert error.value.code == "ansible_unavailable"
    assert "GIZLI_AYRINTI" not in error.value.message
    assert "/srv/gizli" not in error.value.message
    assert error.value.details is None

    jobs = _jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].status is JobStatus.FAILED
    assert jobs[0].finished_at is not None

    document = _result(settings, jobs[0].id)
    assert document["status"] == "failed"
    assert {host["status"] for host in document["hosts"]} == {"no_result"}
    assert document["summary"]["no_result"] == 3
    assert "GIZLI_AYRINTI" not in json.dumps(document)
    assert "/root" not in json.dumps(document)

    # Temizlik korunur ve yeni ping stale beklemeden yapılabilir.
    assert workspaces and all(not path.exists() for path in workspaces)
    assert _preview_dirs(settings) == []
    monkeypatch.undo()
    assert confirm(publish()).status == "successful"


def test_base_exception_from_the_runner_is_not_swallowed(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`KeyboardInterrupt` bir execution arızası değildir; genel hataya çevrilmez."""

    def _interrupt(**_kwargs: Any) -> ProcessOutcome:
        raise KeyboardInterrupt

    monkeypatch.setattr(ping_confirm, "run_ping_process", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        confirm(publish())


@pytest.mark.parametrize("behaviour", ["garbage", "silent-failure"])
def test_invalid_output_is_reported_and_terminal(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
    behaviour: str,
) -> None:
    with pytest.raises(PingInvalidOutputError) as error:
        confirm(publish(), behaviour=behaviour)

    assert error.value.status_code == 502
    jobs = _jobs(db_session)
    assert jobs[0].status is JobStatus.FAILED
    assert {host["status"] for host in _result(settings, jobs[0].id)["hosts"]} == {"no_result"}


def test_known_hosts_outside_the_controlled_directory_blocks_execution(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    db_session: Session,
    settings: Settings,
    tmp_path: Path,
    recorded_processes: list[list[str]],
) -> None:
    """known_hosts yalnız `app-data/ssh` altında olabilir; aksi hâlde ping yok."""
    settings.ssh_known_hosts_path = tmp_path / "disarida" / "known_hosts"

    with pytest.raises(PingKnownHostsUnavailableError) as error:
        confirm(publish())

    assert error.value.status_code == 500
    assert error.value.code == "ping_known_hosts_unavailable"
    assert str(tmp_path) not in error.value.message
    assert recorded_processes == []
    assert _jobs(db_session) == []
    assert _job_dirs(settings) == []


# --- Artifact ve temizlik -----------------------------------------------------


def test_result_artifact_is_private_and_carries_only_safe_fields(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    settings: Settings,
    inventory: Inventory,
) -> None:
    run = confirm(publish(limit="web"), behaviour="leaky")

    directory = settings.app_data_dir / "jobs" / run.job_id
    result = directory / RESULT_FILENAME
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.stat().st_mode) == 0o600

    document = _result(settings, run.job_id)
    assert set(document) == {
        "schema_version",
        "job_id",
        "job_type",
        "status",
        "inventory_id",
        "project_id",
        "limit",
        "return_code",
        "started_at",
        "finished_at",
        "summary",
        "hosts",
    }
    assert document["schema_version"] == 1
    assert document["limit"] == "web"
    assert document["inventory_id"] == inventory.id
    assert document["project_id"] is None

    raw = result.read_text(encoding="utf-8")
    for leak in (
        "ansible_host",
        "127.0.0.1",
        "inventory-targets",
        str(settings.app_data_dir),
        inventory.path,
        "stdout",
        "stderr",
        "argv",
        "ansible.cfg",
    ):
        assert leak not in raw


def test_execution_workspace_and_claimed_preview_are_cleaned_up(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces: list[Path] = []
    real_mkdtemp = ping_confirm.tempfile.mkdtemp

    def _record(*args: Any, **kwargs: Any) -> str:
        created = str(real_mkdtemp(*args, **kwargs))
        workspaces.append(Path(created))
        return created

    monkeypatch.setattr(ping_confirm.tempfile, "mkdtemp", _record)

    confirm(publish())

    assert workspaces and all(not path.exists() for path in workspaces)
    assert _preview_dirs(settings) == []


def test_workspace_is_cleaned_up_after_a_failure(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces: list[Path] = []
    real_mkdtemp = ping_confirm.tempfile.mkdtemp

    def _record(*args: Any, **kwargs: Any) -> str:
        created = str(real_mkdtemp(*args, **kwargs))
        workspaces.append(Path(created))
        return created

    monkeypatch.setattr(ping_confirm.tempfile, "mkdtemp", _record)
    settings.ping_timeout_seconds = 0.5

    with pytest.raises(PingTimeoutError):
        confirm(publish(), behaviour="sleep", sleep_seconds=30)

    assert workspaces and all(not path.exists() for path in workspaces)
    assert _preview_dirs(settings) == []


def test_execution_snapshot_is_written_privately(
    confirm: Callable[..., PingRun],
    publish: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot yalnız sahibine açık bir dosyaya, 0700 dizinde yazılır."""
    observed: dict[str, int] = {}

    def _inspect(**kwargs: Any) -> ProcessOutcome:
        snapshot = Path(kwargs["snapshot_path"])
        observed["file"] = stat.S_IMODE(snapshot.stat().st_mode)
        observed["dir"] = stat.S_IMODE(snapshot.parent.stat().st_mode)
        observed["hosts"] = len(json.loads(snapshot.read_text("utf-8"))["all"]["hosts"])
        return ProcessOutcome(
            return_code=0,
            stdout_text="".join(f'{h} | SUCCESS => {{"ping":"pong"}}\n' for h in HOSTS),
            stderr_text="",
            timed_out=False,
            oversized_stream=None,
        )

    monkeypatch.setattr(ping_confirm, "run_ping_process", _inspect)

    confirm(publish())

    assert observed == {"file": 0o600, "dir": 0o700, "hosts": 3}


# --- HTTP sözleşmesi ----------------------------------------------------------


def _confirm_request(client: TestClient, inventory_id: int, **payload: Any) -> httpx.Response:
    return cast(httpx.Response, client.post(f"/api/inventories/{inventory_id}/ping", json=payload))


@pytest.fixture
def api(settings: Settings, client: TestClient) -> Iterator[TestClient]:
    settings.ansible_ad_hoc_command = stub_ping_command("mixed")
    yield client


def test_api_returns_the_full_run_contract(
    api: TestClient, publish: Callable[..., str], inventory: Inventory
) -> None:
    response = _confirm_request(api, inventory.id, preview_token=publish())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "job_id",
        "job_type",
        "status",
        "inventory_id",
        "project_id",
        "limit",
        "return_code",
        "started_at",
        "finished_at",
        "summary",
        "hosts",
    }
    assert body["job_type"] == "ping"
    assert body["status"] == "failed"
    assert body["return_code"] == 2
    assert body["inventory_id"] == inventory.id
    assert body["project_id"] is None
    assert body["limit"] is None
    assert [host["name"] for host in body["hosts"]] == ["db01", "web01", "web02"]
    assert body["summary"] == {
        "total": 3,
        "reachable": 2,
        "unreachable": 0,
        "failed": 1,
        "no_result": 0,
    }
    assert uuid.UUID(body["job_id"]).version == 4


def test_api_response_carries_no_token_snapshot_or_path(
    api: TestClient,
    publish: Callable[..., str],
    inventory: Inventory,
    settings: Settings,
) -> None:
    token = publish()

    body = _confirm_request(api, inventory.id, preview_token=token).text

    for leak in (
        token,
        token_digest(token),
        "ansible_host",
        "127.0.0.1",
        str(settings.app_data_dir),
        inventory.path,
        "artifact",
        "inventory-targets",
    ):
        assert leak not in body


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"preview_token": ""},
        {"preview_token": "GIZLI_TOKEN_" + "x" * 200},
        {"preview_token": "a" * 43, "limit": "all"},
        {"preview_token": "a" * 43, "timeout": 1},
        {"preview_token": "a" * 43, "forks": 50},
        {"preview_token": "a" * 43, "module": "shell"},
        {"preview_token": "a" * 43, "inventory_path": "/etc/hosts"},
    ],
)
def test_api_rejects_bodies_that_carry_execution_parameters(
    api: TestClient,
    inventory: Inventory,
    db_session: Session,
    settings: Settings,
    recorded_processes: list[list[str]],
    payload: dict[str, Any],
) -> None:
    response = _confirm_request(api, inventory.id, **payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"
    assert "GIZLI_TOKEN_" not in response.text
    assert recorded_processes == []
    assert _jobs(db_session) == []
    assert _job_dirs(settings) == []


def test_api_reports_an_unknown_token_as_preview_invalid(
    api: TestClient, inventory: Inventory, db_session: Session
) -> None:
    response = _confirm_request(api, inventory.id, preview_token="a" * 43)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ping_preview_invalid"
    assert response.json()["error"]["details"] == {"reason": "invalid"}
    assert _jobs(db_session) == []


def test_api_reports_a_fresh_active_job_as_conflict(
    api: TestClient,
    publish: Callable[..., str],
    inventory: Inventory,
    db_session: Session,
) -> None:
    now = datetime.now(UTC)
    active = _stale_job(
        db_session, inventory.id, status=JobStatus.RUNNING, created_at=now, started_at=now
    )

    response = _confirm_request(api, inventory.id, preview_token=publish())

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "job_already_running"
    assert body["details"] == {"job_id": active.id}


def test_api_reports_a_deleted_inventory_record_as_not_found(
    api: TestClient,
    publish: Callable[..., str],
    inventory: Inventory,
    db_session: Session,
) -> None:
    token = publish()
    db_session.delete(inventory)
    db_session.commit()

    response = _confirm_request(api, inventory.id, preview_token=token)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_api_maps_a_timeout_to_gateway_timeout(
    api: TestClient,
    publish: Callable[..., str],
    inventory: Inventory,
    settings: Settings,
) -> None:
    settings.ansible_ad_hoc_command = stub_ping_command("sleep", sleep_seconds=30)
    settings.ping_timeout_seconds = 0.5

    response = _confirm_request(api, inventory.id, preview_token=publish())

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "ping_timeout"
