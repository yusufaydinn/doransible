"""R1-V3C1B runner süreç katmanının sınırları.

Testlerin ortak kuralı: subprocess katmanı **taklit edilmez**. Her testte
gerçek bir işletim sistemi süreci başlar, gerçek argv'yi alır, gerçek
environment'ı görür ve gerçek stdout üretir. Bir sınırın "uygulandığını"
iddia eden ama süreci hiç başlatmayan bir test, tam da ölçmesi gereken şeyi
atlamış olurdu.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from app.core.config import EXECUTION_RUN_DIRNAME
from app.models.execution_mode import ExecutionMode
from app.services.ansible.process import ProcessLimits, run_bounded_process
from app.services.execution.normalize import OUTCOME_SUCCESSFUL, normalize_runner_output
from app.services.execution.runner_env import RunnerEnvironment, build_runner_environment
from app.services.execution.runner_process import (
    CHECK_CMDLINE_ARGUMENT,
    RAW_DIRNAME,
    RunnerProcessError,
    RunnerProcessLimits,
    RunnerProcessResult,
    _RawBudgetObserver,
    run_playbook_process,
)
from tests.support import link_directory

STUB = Path(__file__).resolve().parent / "runner_stub.py"

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

# Yalnız localhost; SSH, dış ağ ve gerçek credential yok.
INVENTORY = "all:\n  hosts:\n    probehost:\n      ansible_connection: local\n"

DEFAULT_LIMITS = RunnerProcessLimits(
    timeout_seconds=30.0, max_stdout_bytes=1_000_000, max_raw_bytes=10_000_000
)

# Parent sürece konan, child'da **hiçbir biçimde** görünmemesi gereken değerler.
SENTINEL_ENV = {
    "AOPS_TEST_MASTER_KEY": "AOPS-SENTINEL-MASTER-KEY-8f2a",
    "ANSIBLE_CONFIG": "/parent/should/not/leak/ansible.cfg",
    "SSH_AUTH_SOCK": "/parent/should/not/leak/agent.sock",
    "DATABASE_URL": "sqlite:////parent/should/not/leak/app.db",
}

# CLI'ye asla girmemesi gereken anahtarlar (ADR-022 trusted-operator: bu
# dilimde limit/tags/become/extra-vars **üretilmez**).
FORBIDDEN_ARGUMENTS = (
    "--limit",
    "--tags",
    "--skip-tags",
    "--become",
    "--become-user",
    "-e",
    "--extra-vars",
    "--forks",
    "-m",
    "-r",
)


def stub_command(behaviour: str, **options: object) -> list[str]:
    """Sahte runner CLI'sini çağıran bir **argüman listesi** üretir."""
    command = [sys.executable, str(STUB), "--behaviour", behaviour]
    for name, value in options.items():
        command.extend([f"--{name.replace('_', '-')}", str(value)])
    return command


def real_runner_available() -> bool:
    """Gerçek ``ansible-runner`` bu platformda çalıştırılabiliyor mu.

    "Kurulu mu" değil "çalışıyor mu" sorulur: Ansible Windows'u control node
    olarak desteklemez ve paket kurulu olsa bile CLI başlatılamaz.
    """
    try:
        completed = subprocess.run(
            ["ansible-runner", "--version"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


# --- Fixture'lar -------------------------------------------------------------


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    """`runner_env`'in beklediği biçimde, önceden var olan 0700 execution kökü."""
    root = tmp_path / "app-data" / EXECUTION_RUN_DIRNAME
    root.mkdir(parents=True)
    root.chmod(0o700)
    return root


@pytest.fixture
def plan_root(tmp_path: Path) -> Path:
    """``app-data/execution-plans`` karşılığı: dondurulmuş workspace'lerin kökü."""
    root = tmp_path / "app-data" / "execution-plans"
    root.mkdir(parents=True)
    return root


def freeze_workspace(plan_root: Path, *, name: str | None = None) -> Path:
    """R1-V2'nin ürettiği düzende bir dondurulmuş workspace kurar.

    Düzen gerçeğin aynısıdır: ``<plan-root>/<uuid4>/{project, inventory/hosts.yml,
    manifest.json}``. Testler bu yardımcıyı kullanır çünkü bağ doğrulaması iki
    yolun **birlikte** oluşturduğu düzeni ölçer; tek tek kurulan yollar o düzeni
    hiç kurmazdı.
    """
    workspace = plan_root / (name if name is not None else str(uuid.uuid4()))
    project = workspace / "project"
    (project / "plays").mkdir(parents=True)
    (project / "site.yml").write_text(PLAYBOOK, encoding="utf-8")
    (project / "plays" / "nested.yml").write_text("- hosts: all\n  tasks: []\n", encoding="utf-8")
    inventory = workspace / "inventory"
    inventory.mkdir()
    (inventory / "hosts.yml").write_text(INVENTORY, encoding="utf-8")
    # Manifest yalnız **düzenin parçası** olarak aranır; içeriği bu katmanda
    # okunmaz ve doğrulanmaz.
    (workspace / "manifest.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
    return workspace


@pytest.fixture
def frozen_workspace(plan_root: Path) -> Path:
    """Geçerli, tam bir dondurulmuş workspace."""
    return freeze_workspace(plan_root)


@pytest.fixture
def frozen_project(frozen_workspace: Path) -> Path:
    """Dondurulmuş project ağacı (zararsız debug/assert playbook'u)."""
    return frozen_workspace / "project"


@pytest.fixture
def frozen_inventory(frozen_workspace: Path) -> Path:
    """Aynı workspace'in dondurulmuş ``inventory/hosts.yml`` dosyası."""
    return frozen_workspace / "inventory" / "hosts.yml"


@pytest.fixture
def job_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def environment(
    run_root: Path, job_id: str, frozen_project: Path, tmp_path: Path
) -> RunnerEnvironment:
    """R1-V3C1A'nın gerçek environment'ı; testte taklit edilmez."""
    return build_runner_environment(
        execution_run_root=run_root,
        job_id=job_id,
        frozen_project_root=frozen_project,
        ssh_policy="strict",
        known_hosts=tmp_path / "app-data" / "ssh" / "known_hosts",
    )


@pytest.fixture
def sentinel_parent_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parent sürece secret görünümlü değerler koyar."""
    for name, value in SENTINEL_ENV.items():
        monkeypatch.setenv(name, value)


def run_stub(
    *,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
    behaviour: str,
    playbook: str = "site.yml",
    mode: ExecutionMode = ExecutionMode.CHECK,
    limits: RunnerProcessLimits | None = None,
    report: Path | None = None,
    stub_options: dict[str, object] | None = None,
) -> RunnerProcessResult:
    """Sahte runner CLI'sini gerçek süreç olarak çalıştırır."""
    options = dict(stub_options or {})
    if report is not None:
        options["report"] = report
    return run_playbook_process(
        command=stub_command(behaviour, **options),
        runner_environment=environment,
        job_id=job_id,
        frozen_project_root=frozen_project,
        frozen_inventory_path=frozen_inventory,
        playbook_path=playbook,
        mode=mode,
        limits=limits or DEFAULT_LIMITS,
    )


# --- 1-7: argv, environment ve süreç sözleşmesi ------------------------------


def test_the_runner_runs_as_a_real_child_process_not_the_python_api(
    environment: RunnerEnvironment, job_id: str, frozen_project: Path, frozen_inventory: Path
) -> None:
    """Runner ayrı bir OS süreci olarak çalışır; in-process API kullanılmaz.

    Ölçüm dolaylı değil doğrudandır: child kendi PID'ini ve process group'unu
    rapor eder. PID test sürecininkinden farklıdır ve child kendi session'ının
    lideridir — yani süreç gerçekten ayrıdır (ADR-021 Kapı A).
    """
    report = environment.run_dir.parent / "report.json"
    result = run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="success",
        report=report,
    )

    observed = json.loads(report.read_text(encoding="utf-8"))
    assert observed["pid"] != os.getpid()
    assert observed["pgid"] == observed["pid"]
    assert observed["pgid"] != os.getpgid(0)
    assert result.return_code == 0


def test_importing_the_process_layer_never_loads_the_runner_python_api() -> None:
    """Süreç katmanını import etmek `ansible_runner`'ı yüklemez.

    Ölçüm **temiz** bir yorumlayıcıda yapılır: aynı test oturumunda başka bir
    testin (örneğin Kapı A-D probe'larının) modülü yüklemiş olması, bu katmanın
    onu yüklediği anlamına gelmezdi ve tam tersi bir sonuç üretirdi.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import app.services.execution.runner_process as module; "
            "assert module is not None; "
            "print('ansible_runner' in sys.modules or 'ansible' in sys.modules)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert completed.stdout.strip() == "False"


def test_the_child_receives_the_exact_fixed_argument_vector(
    environment: RunnerEnvironment, job_id: str, frozen_project: Path, frozen_inventory: Path
) -> None:
    """argv **tam eşitlikle** bağlanır; sıra ve anahtar kümesi sabittir."""
    report = environment.run_dir.parent / "report.json"
    run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="report-only",
        report=report,
    )

    observed = json.loads(report.read_text(encoding="utf-8"))["argv"]
    assert observed == [
        "--behaviour",
        "report-only",
        "--report",
        str(report),
        "run",
        str(environment.run_dir),
        "--project-dir",
        str(frozen_project),
        "--inventory",
        str(frozen_inventory),
        "--artifact-dir",
        str(environment.run_dir / RAW_DIRNAME),
        "--ident",
        job_id,
        "--json",
        "--omit-env-files",
        "-p",
        "site.yml",
        "--cmdline=--check",
    ]


def test_no_shell_is_ever_used_on_the_runner_path() -> None:
    """Süreç yolunda ``shell`` yalnız ``False`` olarak geçer.

    Kontrol AST üzerindedir: metin araması, ``shell=True``'yu yasakladığını
    söyleyen bir **docstring**'i ihlal sanardı.
    """
    backend = Path(__file__).resolve().parents[1]
    observed: list[bool] = []
    for relative in ("app/services/execution/runner_process.py", "app/services/ansible/process.py"):
        tree = ast.parse((backend / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "shell":
                    assert isinstance(keyword.value, ast.Constant), relative
                    observed.append(bool(keyword.value.value))

    # Yalnız "True yok" değil, "açıkça False var" da ölçülür: parametrenin hiç
    # geçmediği bir gelecek sürüm de bu testi sessizce geçirirdi.
    assert observed == [False]


def test_the_child_sees_only_the_runner_environment_keys(
    sentinel_parent_environment: None,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Child'ın environment'ı C1A sözlüğüne **tam eşittir**.

    Sayı saymak yerine küme karşılaştırılır: fazladan tek bir anahtar bile
    ölçülmüş Kapı A1 yüzeyini genişletirdi.
    """
    report = environment.run_dir.parent / "report.json"
    run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="report-only",
        report=report,
    )

    observed = json.loads(report.read_text(encoding="utf-8"))["environment"]
    assert set(observed) == set(environment.environment)
    assert observed == environment.environment


def test_parent_sentinel_secrets_never_reach_the_child(
    sentinel_parent_environment: None,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Parent'taki master key, agent socket ve DSN child'da hiç görünmez."""
    report = environment.run_dir.parent / "report.json"
    run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="report-only",
        report=report,
    )

    blob = report.read_text(encoding="utf-8")
    for name, value in SENTINEL_ENV.items():
        assert value not in blob, name
    assert "AOPS_TEST_MASTER_KEY" not in json.loads(blob)["environment"]


def test_check_mode_pins_the_check_cmdline_argument_exactly_once(
    environment: RunnerEnvironment, job_id: str, frozen_project: Path, frozen_inventory: Path
) -> None:
    """``ExecutionMode.CHECK`` argv'ye ``--cmdline=--check``'i tam bir kez ekler."""
    report = environment.run_dir.parent / "report.json"
    run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="report-only",
        mode=ExecutionMode.CHECK,
        report=report,
    )
    argv = json.loads(report.read_text(encoding="utf-8"))["argv"]
    assert argv.count(CHECK_CMDLINE_ARGUMENT) == 1


def test_normal_mode_produces_no_check_or_cmdline_argument(
    environment: RunnerEnvironment, job_id: str, frozen_project: Path, frozen_inventory: Path
) -> None:
    """``ExecutionMode.NORMAL`` argv'sinde ``--check``/``--cmdline`` hiç yoktur.

    Yalnız kip argümanı çıkarılır: NORMAL argv'si, CHECK argv'sinin son
    elemanı (``--cmdline=--check``) çıkarılmış hâliyle **birebir** eşleşir —
    ne sıra ne de başka bir alan değişir.
    """
    # `--report` iki çağrıda **kasten farklı** dosyalara işaret eder — aynı
    # rapor dosyasına iki çağrının yarışması istenmez. Karşılaştırma bu yüzden
    # servisin ürettiği kısımla sınırlıdır (``"run"``'dan itibaren); stub'ın
    # kendi ``--report`` öneki hiçbir kipte servisin ürettiği argv'nin parçası
    # değildir.
    check_report = environment.run_dir.parent / "check-report.json"
    run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="report-only",
        mode=ExecutionMode.CHECK,
        report=check_report,
    )
    check_argv = json.loads(check_report.read_text(encoding="utf-8"))["argv"]
    check_produced = check_argv[check_argv.index("run") :]

    normal_report = environment.run_dir.parent / "normal-report.json"
    run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="report-only",
        mode=ExecutionMode.NORMAL,
        report=normal_report,
    )
    normal_argv = json.loads(normal_report.read_text(encoding="utf-8"))["argv"]
    normal_produced = normal_argv[normal_argv.index("run") :]

    assert "--check" not in normal_produced
    assert "--cmdline=--check" not in normal_produced
    assert not any(item == "--cmdline" or item.startswith("--cmdline=") for item in normal_produced)
    assert normal_produced == check_produced[:-1]


@pytest.mark.parametrize(
    "offending",
    ["check", "normal", None, 42],
    ids=["str-check", "str-normal", "none", "other-object"],
)
def test_an_invalid_runtime_mode_is_rejected_before_any_process_or_raw_directory(
    offending: object,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Bilinmeyen bir kip fail-closed reddedilir; child ve raw alanı hiç doğmaz.

    Karşılaştırma bilinçli olarak **kimlik** (``is``) iledir, bu yüzden düz
    ``"check"``/``"normal"`` metinleri de reddedilir: :class:`ExecutionMode`
    bir ``StrEnum`` olduğundan bu metinler eşitlik testinde üyeyle eşit
    görünürdü ve fail-open bir yola düşerdi.
    """
    report = environment.run_dir.parent / "report.json"
    with pytest.raises(RunnerProcessError) as error:
        run_stub(
            environment=environment,
            job_id=job_id,
            frozen_project=frozen_project,
            frozen_inventory=frozen_inventory,
            behaviour="report-only",
            mode=offending,  # type: ignore[arg-type]
            report=report,
        )
    assert error.value.details == {"reason": "runner_mode_invalid"}
    assert not report.exists()
    assert not (environment.run_dir / RAW_DIRNAME).exists()


@pytest.mark.parametrize("forbidden", FORBIDDEN_ARGUMENTS)
def test_no_limit_tag_become_or_extra_vars_argument_is_produced(
    forbidden: str,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Yasaklı anahtarların hiçbiri argv'ye girmez."""
    report = environment.run_dir.parent / "report.json"
    run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="report-only",
        report=report,
    )
    argv = json.loads(report.read_text(encoding="utf-8"))["argv"]
    # Stub'ın kendi öneki hariç, servisin **ürettiği** kısım incelenir.
    produced = argv[argv.index("run") :]
    assert forbidden not in produced


# --- 8-10: sınırların çalışma anında uygulanması -----------------------------


def test_timeout_terminates_the_whole_process_group(
    environment: RunnerEnvironment, job_id: str, frozen_project: Path, frozen_inventory: Path
) -> None:
    """Timeout yalnız leader'ı değil, torun süreçleri de kapatır.

    Stub uzun uyuyan bir torun başlatır. Yalnız parent sonlandırılsaydı torun
    yaşamaya devam eder ve `--check` bir playbook'un arkasında kontrolsüz bir
    süreç bırakılırdı.
    """
    started = time.monotonic()
    result = run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="sleep",
        stub_options={"sleep_seconds": 60},
        limits=RunnerProcessLimits(
            timeout_seconds=1.0, max_stdout_bytes=1_000_000, max_raw_bytes=10_000_000
        ),
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert elapsed < 30.0
    assert result.finished_at >= result.started_at


def test_the_stdout_limit_cuts_the_process_while_it_still_runs(
    environment: RunnerEnvironment, job_id: str, frozen_project: Path, frozen_inventory: Path
) -> None:
    """Sınır aşıldığı anda süreç kesilir; doğal bitişi beklenmez."""
    started = time.monotonic()
    result = run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="flood-stdout",
        stub_options={"size_bytes": 4_000_000, "sleep_seconds": 60},
        limits=RunnerProcessLimits(
            timeout_seconds=60.0, max_stdout_bytes=200_000, max_raw_bytes=10_000_000
        ),
    )
    elapsed = time.monotonic() - started

    assert result.oversized_stream == "stdout"
    assert result.timed_out is False
    assert elapsed < 30.0
    assert len(result.stdout_text.encode("utf-8")) <= 200_000


def test_the_raw_budget_cuts_the_process_while_it_still_runs(
    environment: RunnerEnvironment, job_id: str, frozen_project: Path, frozen_inventory: Path
) -> None:
    """Raw bütçesi süreç çalışırken ölçülür ve aşıldığında süreci kestirir.

    Sınır yalnız süreç bittikten sonra kontrol edilseydi, sınırı aşan baytlar
    zaten diske yazılmış olurdu.
    """
    started = time.monotonic()
    result = run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="flood-raw",
        stub_options={"size_bytes": 300_000, "sleep_seconds": 60},
        limits=RunnerProcessLimits(
            timeout_seconds=60.0, max_stdout_bytes=1_000_000, max_raw_bytes=1_000_000
        ),
    )
    elapsed = time.monotonic() - started

    assert result.raw_limit_exceeded is True
    assert result.timed_out is False
    assert elapsed < 30.0


def test_an_unmeasurable_raw_subtree_counts_as_a_budget_violation(
    environment: RunnerEnvironment, job_id: str, frozen_project: Path, frozen_inventory: Path
) -> None:
    """Okunamayan bir raw alt dizini "bütçe aşılmadı" sayılmaz.

    Alt dizin sessizce atlansaydı, tek bir ``chmod 000`` bütçeyi tamamen devre
    dışı bırakır ve sınırsız yazan bir süreç ölçülmeden çalışmaya devam ederdi.
    """
    if os.geteuid() == 0:  # pragma: no cover - CI kullanıcısına bağlı
        pytest.skip("root için dizin izinleri ölçümü kısıtlamaz.")

    started = time.monotonic()
    try:
        result = run_stub(
            environment=environment,
            job_id=job_id,
            frozen_project=frozen_project,
            frozen_inventory=frozen_inventory,
            behaviour="unreadable-raw",
            stub_options={"sleep_seconds": 60},
            limits=RunnerProcessLimits(
                timeout_seconds=60.0, max_stdout_bytes=1_000_000, max_raw_bytes=10_000_000
            ),
        )
        elapsed = time.monotonic() - started

        assert result.raw_limit_exceeded is True
        assert result.timed_out is False
        assert elapsed < 30.0
    finally:
        # İzni geri ver: 0000 bir dizin, en iyi çaba ile çalışan raw
        # temizliğinin (ve pytest'in tmp temizliğinin) kaldıramayacağı bir
        # kalıntıdır.
        for locked in (environment.run_dir / RAW_DIRNAME).rglob("locked"):
            locked.chmod(0o700)


def test_a_measurement_failure_in_the_observer_thread_still_terminates(
    monkeypatch: pytest.MonkeyPatch,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Ölçüm thread'indeki beklenmeyen hata süreci sınırsız bırakmaz.

    Thread sessizce ölseydi raw alanını kimse ölçmezdi ve süreç, bütçesi hiç
    uygulanmadan timeout'a kadar çalışırdı.
    """

    def _explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("olcum yapilamadi")

    monkeypatch.setattr("app.services.execution.runner_process._measure_tree", _explode)

    started = time.monotonic()
    result = run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="sleep",
        stub_options={"sleep_seconds": 60},
        limits=RunnerProcessLimits(
            timeout_seconds=60.0, max_stdout_bytes=1_000_000, max_raw_bytes=10_000_000
        ),
    )
    elapsed = time.monotonic() - started

    assert result.raw_limit_exceeded is True
    assert result.timed_out is False
    assert elapsed < 30.0


def test_an_unmeasurable_raw_root_counts_as_a_budget_violation() -> None:
    """Kök raw descriptor'ı okunamıyorsa bütçe **aşılmış** sayılır.

    Raw alanı bu katmanın kendi ürünüdür ve süreç bitmeden silinmez; kökün
    ölçülememesi normal bir yarış değil, ölçümün tamamen kaybıdır.
    """
    observer = _RawBudgetObserver(-1, 1_000_000)
    observer.stop()
    assert observer.limit_exceeded is True


def test_a_failing_final_measurement_does_not_lose_the_run_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Son ölçümdeki beklenmeyen hata ihlale çevrilir, çalıştırma sonucunu silmez."""

    def _explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("olcum yapilamadi")

    monkeypatch.setattr("app.services.execution.runner_process._measure_tree", _explode)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        observer = _RawBudgetObserver(descriptor, 1_000_000)
        observer.stop()
    finally:
        os.close(descriptor)
    assert observer.limit_exceeded is True


def test_entries_vanishing_during_measurement_do_not_break_the_run(
    environment: RunnerEnvironment, job_id: str, frozen_project: Path, frozen_inventory: Path
) -> None:
    """Ölçüm sırasında kaybolan girdi tolere edilir; kilitlenme veya yanlış ihlal olmaz.

    Bu, fail-closed davranışın **tolere edilen tek** yarışıdır: dosyayı yazıp
    hemen silen bir süreç bütçeyi aşmıyordur.
    """
    started = time.monotonic()
    result = run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="churn-raw",
        stub_options={"size_bytes": 4096, "sleep_seconds": 2},
        limits=RunnerProcessLimits(
            timeout_seconds=60.0, max_stdout_bytes=1_000_000, max_raw_bytes=10_000_000
        ),
    )
    elapsed = time.monotonic() - started

    assert result.raw_limit_exceeded is False
    assert result.timed_out is False
    assert result.return_code == 0
    assert elapsed < 30.0


# --- 11: raw temizliği -------------------------------------------------------


@pytest.mark.parametrize(
    ("behaviour", "options", "limits"),
    [
        ("success", {}, None),
        ("write-raw", {"size_bytes": 1024, "exit_code": 2}, None),
        (
            "sleep",
            {"sleep_seconds": 60},
            RunnerProcessLimits(
                timeout_seconds=1.0, max_stdout_bytes=1_000_000, max_raw_bytes=10_000_000
            ),
        ),
        (
            "flood-stdout",
            {"size_bytes": 2_000_000, "sleep_seconds": 5},
            RunnerProcessLimits(
                timeout_seconds=60.0, max_stdout_bytes=200_000, max_raw_bytes=10_000_000
            ),
        ),
        (
            "flood-raw",
            {"size_bytes": 300_000, "sleep_seconds": 30},
            RunnerProcessLimits(
                timeout_seconds=60.0, max_stdout_bytes=1_000_000, max_raw_bytes=1_000_000
            ),
        ),
    ],
    ids=["success", "rc-failure", "timeout", "stdout-oversize", "raw-oversize"],
)
def test_the_raw_directory_is_removed_on_every_path(
    behaviour: str,
    options: dict[str, object],
    limits: RunnerProcessLimits | None,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Başarı, rc hatası, timeout ve iki sınır aşımında raw alanı geride kalmaz."""
    run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour=behaviour,
        limits=limits,
        stub_options=options,
    )
    assert not (environment.run_dir / RAW_DIRNAME).exists()
    # Job dizininin kendisi ve C1A'nın kurduğu alt dizinler **korunur**.
    assert environment.run_dir.is_dir()


def test_the_raw_directory_is_removed_when_the_process_cannot_start(
    environment: RunnerEnvironment, job_id: str, frozen_project: Path, frozen_inventory: Path
) -> None:
    """Süreç hiç başlayamadığında da raw alanı geride kalmaz."""
    with pytest.raises(RunnerProcessError) as error:
        run_playbook_process(
            command=[str(environment.run_dir / "bulunmayan-ikili")],
            runner_environment=environment,
            job_id=job_id,
            frozen_project_root=frozen_project,
            frozen_inventory_path=frozen_inventory,
            playbook_path="site.yml",
            mode=ExecutionMode.CHECK,
            limits=RunnerProcessLimits(
                timeout_seconds=30.0, max_stdout_bytes=1_000_000, max_raw_bytes=10_000_000
            ),
        )

    assert error.value.details == {"reason": "runner_launch_failed"}
    # Hata mesajı işletim sisteminin path taşıyan metnini aktarmaz.
    assert "bulunmayan-ikili" not in str(error.value)
    assert not (environment.run_dir / RAW_DIRNAME).exists()


def test_the_raw_directory_is_private_and_removed_with_its_whole_tree(
    environment: RunnerEnvironment, job_id: str, frozen_project: Path, frozen_inventory: Path
) -> None:
    """İç içe raw ağacı da bırakılmaz; silme yalnız ``raw`` çocuğuna dokunur."""
    marker = environment.run_dir / "home" / "dokunulmaz"
    marker.write_text("korunur", encoding="utf-8")

    run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="write-raw",
        stub_options={"size_bytes": 64},
    )

    assert not (environment.run_dir / RAW_DIRNAME).exists()
    assert marker.read_text(encoding="utf-8") == "korunur"


# --- 12-13: path doğrulaması -------------------------------------------------


@pytest.mark.parametrize(
    ("playbook", "reason"),
    [
        ("", "playbook_path_empty"),
        ("/etc/passwd", "playbook_path_absolute"),
        ("../site.yml", "playbook_path_unsafe_segment"),
        ("plays/../../site.yml", "playbook_path_unsafe_segment"),
        ("plays//nested.yml", "playbook_path_unsafe_segment"),
        ("./site.yml", "playbook_path_unsafe_segment"),
        ("-p", "playbook_path_unsafe_segment"),
        ("plays", "playbook_not_regular_file"),
        ("bulunmayan.yml", "playbook_not_regular_file"),
    ],
)
def test_unsafe_playbook_paths_are_rejected_before_the_process_starts(
    playbook: str,
    reason: str,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Reddediş süreç başlamadan olur ve sebep sabit bir koddur.

    Rapor dosyasının **hiç oluşmamış** olması, child'ın hiç başlamadığının
    doğrudan kanıtıdır.
    """
    report = environment.run_dir.parent / "report.json"
    with pytest.raises(RunnerProcessError) as error:
        run_stub(
            environment=environment,
            job_id=job_id,
            frozen_project=frozen_project,
            frozen_inventory=frozen_inventory,
            behaviour="report-only",
            playbook=playbook,
            report=report,
        )

    assert error.value.details == {"reason": reason}
    assert not report.exists()
    assert not (environment.run_dir / RAW_DIRNAME).exists()
    # Path hata mesajına yazılmaz.
    assert playbook not in str(error.value) or not playbook


def test_a_symlinked_playbook_is_rejected(
    tmp_path: Path,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Dondurulmuş ağacın dışına giden symlink kabul edilmez."""
    outside = tmp_path / "disarida.yml"
    outside.write_text("- hosts: all\n", encoding="utf-8")
    try:
        os.symlink(outside, frozen_project / "kacak.yml")
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform
        pytest.skip(f"Symlink oluşturulamadı: {exc}")

    with pytest.raises(RunnerProcessError) as error:
        run_stub(
            environment=environment,
            job_id=job_id,
            frozen_project=frozen_project,
            frozen_inventory=frozen_inventory,
            behaviour="report-only",
            playbook="kacak.yml",
        )
    assert error.value.details == {"reason": "playbook_not_regular_file"}


def test_a_symlinked_intermediate_directory_is_rejected(
    tmp_path: Path,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Ara dizin symlink'i de izlenmez: her parça ``O_NOFOLLOW`` ile açılır."""
    outside = tmp_path / "disarida"
    outside.mkdir()
    (outside / "site.yml").write_text("- hosts: all\n", encoding="utf-8")
    link_directory(frozen_project / "kacak", outside)

    with pytest.raises(RunnerProcessError) as error:
        run_stub(
            environment=environment,
            job_id=job_id,
            frozen_project=frozen_project,
            frozen_inventory=frozen_inventory,
            behaviour="report-only",
            playbook="kacak/site.yml",
        )
    assert error.value.details == {"reason": "playbook_not_regular_file"}


# --- Workspace bağı ----------------------------------------------------------


def expect_binding_rejection(
    *,
    environment: RunnerEnvironment,
    job_id: str,
    project: Path,
    inventory: Path,
    report: Path,
) -> None:
    """Bağ reddedilmeli ve süreç **hiç başlamamalıdır**.

    Rapor dosyasının oluşmamış olması child'ın hiç doğmadığının doğrudan
    kanıtıdır; raw alanının yokluğu ise çalıştırma hazırlığının da yapılmadığını
    gösterir. Sebep tek ve sabittir, path hiçbir yere yazılmaz.
    """
    with pytest.raises(RunnerProcessError) as error:
        run_playbook_process(
            command=stub_command("report-only", report=report),
            runner_environment=environment,
            job_id=job_id,
            frozen_project_root=project,
            frozen_inventory_path=inventory,
            playbook_path="site.yml",
            mode=ExecutionMode.CHECK,
            limits=DEFAULT_LIMITS,
        )

    assert error.value.details == {"reason": "frozen_workspace_binding_invalid"}
    assert not report.exists()
    assert not (environment.run_dir / RAW_DIRNAME).exists()
    assert str(project) not in str(error.value)
    assert str(inventory) not in str(error.value)


def test_a_project_and_inventory_from_the_same_workspace_are_accepted(
    environment: RunnerEnvironment,
    job_id: str,
    frozen_workspace: Path,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Aynı workspace'in iki çocuğu kabul edilir ve argv'ye **onlar** girer."""
    report = environment.run_dir.parent / "report.json"
    result = run_stub(
        environment=environment,
        job_id=job_id,
        frozen_project=frozen_project,
        frozen_inventory=frozen_inventory,
        behaviour="success",
        report=report,
    )

    argv = json.loads(report.read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--project-dir") + 1] == str(frozen_workspace / "project")
    assert argv[argv.index("--inventory") + 1] == str(frozen_workspace / "inventory" / "hosts.yml")
    assert result.return_code == 0


def test_an_inventory_from_another_workspace_is_rejected(
    plan_root: Path,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
) -> None:
    """İkisi de kendi başına geçerli ama **aynı** workspace'e ait değiller.

    Bu, bağ kontrolünün asıl sebebidir: yolları tek tek doğrulamak, bir planın
    project'ini başka bir planın inventory'siyle çalıştırmayı engellemezdi.
    """
    other = freeze_workspace(plan_root)
    expect_binding_rejection(
        environment=environment,
        job_id=job_id,
        project=frozen_project,
        inventory=other / "inventory" / "hosts.yml",
        report=environment.run_dir.parent / "report.json",
    )


def test_a_project_child_with_another_name_is_rejected(
    plan_root: Path, environment: RunnerEnvironment, job_id: str
) -> None:
    """Project çocuğunun adı tam olarak ``project`` olmalıdır."""
    workspace = freeze_workspace(plan_root)
    (workspace / "project").rename(workspace / "proje")
    expect_binding_rejection(
        environment=environment,
        job_id=job_id,
        project=workspace / "proje",
        inventory=workspace / "inventory" / "hosts.yml",
        report=environment.run_dir.parent / "report.json",
    )


@pytest.mark.parametrize(
    "name",
    ["hosts.ini", "inventory.yml", "HOSTS.YML"],
)
def test_only_the_expected_frozen_inventory_file_is_accepted(
    name: str,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_workspace: Path,
    frozen_project: Path,
) -> None:
    """Serbest bir inventory dosya adı kabul edilmez."""
    other = frozen_workspace / "inventory" / name
    other.write_text("all:\n  hosts: {}\n", encoding="utf-8")
    expect_binding_rejection(
        environment=environment,
        job_id=job_id,
        project=frozen_project,
        inventory=other,
        report=environment.run_dir.parent / "report.json",
    )


def test_an_inventory_directory_with_another_name_is_rejected(
    environment: RunnerEnvironment,
    job_id: str,
    frozen_workspace: Path,
    frozen_project: Path,
) -> None:
    """Inventory çocuğunun adı tam olarak ``inventory`` olmalıdır."""
    other = frozen_workspace / "envanter"
    other.mkdir()
    (other / "hosts.yml").write_text(INVENTORY, encoding="utf-8")
    expect_binding_rejection(
        environment=environment,
        job_id=job_id,
        project=frozen_project,
        inventory=other / "hosts.yml",
        report=environment.run_dir.parent / "report.json",
    )


@pytest.mark.parametrize(
    "name",
    ["x", "workspace", "3f2b7c1a9d4e4a6b8c1d5e7f9a0b2c3d", "3F2B7C1A-9D4E-4A6B-8C1D-5E7F9A0B2C3D"],
    ids=["short", "word", "no-dashes", "uppercase"],
)
def test_a_workspace_directory_that_is_not_a_canonical_uuid4_is_rejected(
    name: str, plan_root: Path, environment: RunnerEnvironment, job_id: str
) -> None:
    """Doğru son iki ada sahip sahte bir düzen bağ sayılmaz.

    ``/tmp/x/inventory/hosts.yml`` ile gerçek bir workspace'i ayıran şey, kökün
    adının uygulamanın ürettiği canonical UUID4 olmasıdır.
    """
    workspace = freeze_workspace(plan_root, name=name)
    expect_binding_rejection(
        environment=environment,
        job_id=job_id,
        project=workspace / "project",
        inventory=workspace / "inventory" / "hosts.yml",
        report=environment.run_dir.parent / "report.json",
    )


def test_a_lexical_parent_alias_is_rejected(
    environment: RunnerEnvironment,
    job_id: str,
    frozen_workspace: Path,
    frozen_inventory: Path,
) -> None:
    """``..`` ile kurulmuş bir alias, aynı dizini başka bir yol gibi gösterir."""
    expect_binding_rejection(
        environment=environment,
        job_id=job_id,
        project=frozen_workspace / "inventory" / ".." / "project",
        inventory=frozen_inventory,
        report=environment.run_dir.parent / "report.json",
    )


def test_a_relative_frozen_path_is_rejected(
    environment: RunnerEnvironment,
    job_id: str,
    frozen_inventory: Path,
) -> None:
    """Relative yol sürecin çalışma dizinine göre çözülürdü; kabul edilmez."""
    expect_binding_rejection(
        environment=environment,
        job_id=job_id,
        project=Path("project"),
        inventory=frozen_inventory,
        report=environment.run_dir.parent / "report.json",
    )


def test_a_workspace_without_a_manifest_is_rejected(
    plan_root: Path, environment: RunnerEnvironment, job_id: str
) -> None:
    """Düzenin parçası eksikse bağ kurulmaz.

    Manifest burada **okunmaz**: varlığı aranır. İçeriğin doğrulanması plan
    kaydına sahip olan dilimin işidir ve taklit edilmez.
    """
    workspace = freeze_workspace(plan_root)
    (workspace / "manifest.json").unlink()
    expect_binding_rejection(
        environment=environment,
        job_id=job_id,
        project=workspace / "project",
        inventory=workspace / "inventory" / "hosts.yml",
        report=environment.run_dir.parent / "report.json",
    )


@pytest.mark.parametrize(
    "linked",
    ["workspace", "project", "inventory"],
)
def test_a_symlinked_workspace_component_is_rejected(
    linked: str,
    plan_root: Path,
    environment: RunnerEnvironment,
    job_id: str,
) -> None:
    """Workspace, project veya inventory yerine konmuş bir bağlantı izlenmez."""
    real = freeze_workspace(plan_root)
    workspace = real
    if linked == "workspace":
        workspace = plan_root / str(uuid.uuid4())
        link_directory(workspace, real)
    else:
        target = real / linked
        target.rename(real / f"{linked}-gercek")
        link_directory(real / linked, real / f"{linked}-gercek")

    expect_binding_rejection(
        environment=environment,
        job_id=job_id,
        project=workspace / "project",
        inventory=workspace / "inventory" / "hosts.yml",
        report=environment.run_dir.parent / "report.json",
    )


def test_a_symlinked_inventory_file_is_rejected(
    tmp_path: Path,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_workspace: Path,
    frozen_project: Path,
) -> None:
    """``hosts.yml`` yerine konmuş bir symlink dışarıdaki bir dosyayı okutamaz."""
    outside = tmp_path / "disarida.yml"
    outside.write_text(INVENTORY, encoding="utf-8")
    inventory = frozen_workspace / "inventory" / "hosts.yml"
    inventory.unlink()
    try:
        os.symlink(outside, inventory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform
        pytest.skip(f"Symlink oluşturulamadı: {exc}")

    expect_binding_rejection(
        environment=environment,
        job_id=job_id,
        project=frozen_project,
        inventory=inventory,
        report=environment.run_dir.parent / "report.json",
    )


def test_the_original_project_and_inventory_are_never_opened(
    tmp_path: Path,
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Özgün ağaçlar okunamaz hâldeyken bile çalıştırma başarılıdır.

    Özgün project ve inventory 0000 iznine çekilir: onları açmayı deneyen
    herhangi bir kod yolu ``PermissionError`` üretirdi. Ayrıca child'ın argv'si
    özgün yolların **hiçbirini** taşımaz.
    """
    if os.geteuid() == 0:  # pragma: no cover - CI kullanıcısına bağlı
        pytest.skip("root için dosya izinleri erişimi kısıtlamaz.")

    original_project = tmp_path / "ozgun" / "project"
    original_project.mkdir(parents=True)
    (original_project / "site.yml").write_text("- hosts: all\n", encoding="utf-8")
    original_inventory = tmp_path / "ozgun" / "hosts.ini"
    original_inventory.write_text("[all]\nprobehost\n", encoding="utf-8")
    original_inventory.chmod(0o000)
    original_project.chmod(0o000)

    report = environment.run_dir.parent / "report.json"
    try:
        result = run_stub(
            environment=environment,
            job_id=job_id,
            frozen_project=frozen_project,
            frozen_inventory=frozen_inventory,
            behaviour="success",
            report=report,
        )
    finally:
        original_project.chmod(0o700)
        original_inventory.chmod(0o600)

    assert result.return_code == 0
    argv = " ".join(json.loads(report.read_text(encoding="utf-8"))["argv"])
    assert str(original_project) not in argv
    assert str(original_inventory) not in argv


# --- 14-15: katman sınırı ve geriye uyumluluk --------------------------------


def test_the_process_layer_imports_no_database_or_session_module() -> None:
    """Süreç katmanı DB'ye ve session'a **hiç** bağlanmaz.

    Kontrol AST üzerinden yapılır: metin araması yorum satırlarını da yakalar
    ve gerçek bir import ile bir cümleyi ayırt edemezdi.

    Tek istisna ``app.models.execution_mode``'dur (R1-V3H1B2B):
    :class:`~app.models.execution_mode.ExecutionMode` yalnız stdlib ve
    SQLAlchemy'nin sütun tipi tanımına bağlı, Session veya başka bir ORM
    modeli taşımayan bir değer tipidir (modülün kendi docstring'i); onu
    ``build_runner_arguments``'ın kip parametresi için almak DB/session
    sınırını **delmez**. Başka hiçbir ``app.models`` alt modülü — ``Job``,
    ``ExecutionPlanRecord``, Session'a bağlı hiçbir şey — bu istisnaya girmez.
    """
    forbidden_roots = ("sqlalchemy", "app.db", "app.models", "fastapi", "ansible_runner")
    allowed_leaf = "app.models.execution_mode"
    for module in ("runner_process", "normalize"):
        source = (
            Path(__file__).resolve().parents[1] / f"app/services/execution/{module}.py"
        ).read_text(encoding="utf-8")
        imported: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for name in imported:
            if name == allowed_leaf:
                continue
            assert not name.startswith(forbidden_roots), f"{module}: {name}"


def test_the_normalizer_touches_no_filesystem_or_process_module() -> None:
    """Normalizer saf bir dönüşüm katmanıdır: ``os``/``subprocess`` içermez."""
    source = (
        Path(__file__).resolve().parents[1] / "app/services/execution/normalize.py"
    ).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not {"os", "subprocess", "pathlib", "shutil"} & set(imported)


def test_a_bounded_process_without_an_observer_behaves_as_before(tmp_path: Path) -> None:
    """Gözlemci parametresi geriye uyumludur: verilmeyen çağrı değişmez.

    Ping ve inventory parser bu yolu kullanır; gözlemcinin varsayılan olarak
    kapalı olduğu doğrudan ölçülür.
    """
    outcome = run_bounded_process(
        [sys.executable, "-c", "print('merhaba')"],
        work_dir=tmp_path,
        environment={"PATH": os.environ.get("PATH", "")},
        limits=ProcessLimits(timeout_seconds=30.0, max_output_bytes=1_000_000),
    )
    assert outcome.return_code == 0
    assert outcome.stdout_text.strip() == "merhaba"
    assert outcome.timed_out is False
    assert outcome.oversized_stream is None


# --- Gerçek ansible-runner ---------------------------------------------------


@pytest.mark.skipif(
    not real_runner_available(),
    reason="ansible-runner bu platformda çalıştırılamıyor.",
)
def test_real_ansible_runner_executes_a_harmless_playbook_on_localhost(
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Gerçek `ansible-runner` 2.4.3 ile uçtan uca ölçüm.

    Subprocess katmanı **atlanmaz**: gerçek CLI, ürünün ürettiği gerçek argv ile
    çalışır. Inventory yalnız ``ansible_connection=local`` taşır; dış network,
    SSH ve gerçek credential kullanılmaz. Playbook yalnız zararsız
    ``debug``/``assert`` task'ları içerir.
    """
    result = run_playbook_process(
        command=["ansible-runner"],
        runner_environment=environment,
        job_id=job_id,
        frozen_project_root=frozen_project,
        frozen_inventory_path=frozen_inventory,
        playbook_path="site.yml",
        mode=ExecutionMode.CHECK,
        limits=RunnerProcessLimits(
            timeout_seconds=180.0, max_stdout_bytes=5_000_000, max_raw_bytes=50_000_000
        ),
    )

    assert result.return_code == 0
    assert result.timed_out is False
    assert result.oversized_stream is None
    assert result.raw_limit_exceeded is False
    # Raw alanı gerçek bir çalıştırmadan sonra da geride kalmaz.
    assert not (environment.run_dir / RAW_DIRNAME).exists()

    events = [json.loads(line) for line in result.stdout_text.splitlines() if line.strip()]
    assert [event["event"] for event in events][-1] == "playbook_on_stats"
    assert events[-1]["event_data"]["ok"] == {"probehost": 2}
    assert events[-1]["event_data"]["failures"] == {}
    assert events[-1]["event_data"]["processed"] == {"probehost": 1}


@pytest.mark.skipif(
    not real_runner_available(),
    reason="ansible-runner bu platformda çalıştırılamıyor.",
)
def test_a_real_runner_stream_normalizes_to_a_successful_result(
    environment: RunnerEnvironment,
    job_id: str,
    frozen_project: Path,
    frozen_inventory: Path,
) -> None:
    """Gerçek akış, sıkılaştırılmış normalizer'dan da ``successful`` çıkar.

    Sentetik akışlarla ölçülen kurallar (terminal event tek ve son olmalı,
    ``processed`` dolu ve tutarlı olmalı) gerçek `ansible-runner` 2.4.3 çıktısıyla
    da uyumlu olmalıdır; olmasaydı fail-closed sıkılaştırma her başarılı
    çalıştırmayı da reddederdi.
    """
    result = run_playbook_process(
        command=["ansible-runner"],
        runner_environment=environment,
        job_id=job_id,
        frozen_project_root=frozen_project,
        frozen_inventory_path=frozen_inventory,
        playbook_path="site.yml",
        mode=ExecutionMode.CHECK,
        limits=RunnerProcessLimits(
            timeout_seconds=180.0, max_stdout_bytes=5_000_000, max_raw_bytes=50_000_000
        ),
    )

    normalized = normalize_runner_output(
        job_id=job_id,
        stdout_text=result.stdout_text,
        return_code=result.return_code,
        timed_out=result.timed_out,
        oversized_stream=result.oversized_stream,
        raw_limit_exceeded=result.raw_limit_exceeded,
        known_hosts=("probehost",),
        connection_values=(),
        max_events=1000,
        max_result_bytes=1_000_000,
    )

    assert normalized.outcome == OUTCOME_SUCCESSFUL
    assert normalized.error_code is None
    assert normalized.recap["probehost"].ok == 2
    assert normalized.recap["probehost"].failures == 0
    assert [item.event for item in normalized.events] == [
        "playbook_on_task_start",
        "runner_on_ok",
        "playbook_on_task_start",
        "runner_on_ok",
    ]
