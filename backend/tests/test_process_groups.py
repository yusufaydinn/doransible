"""POSIX process-group supervision regresyonları (T-204B1)."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import cast

import pytest

from app.services.ansible import process as process_module
from app.services.ansible.process import (
    ProcessLimits,
    ProcessSupervisor,
    run_bounded_process,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process-group testi")

TREE_SCRIPT = """\
import os, subprocess, sys, time
grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
child = subprocess.Popen([
    sys.executable, "-c",
    "import subprocess,sys,time; "
    "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
    "print(p.pid,flush=True); time.sleep(60)"
], stdout=subprocess.PIPE, text=True)
grandchild = child.stdout.readline().strip()
print(f"{child.pid} {grand.pid} {grandchild}", flush=True)
if sys.argv[1] == "output":
    print("x" * 200000, flush=True)
time.sleep(60)
"""

PARENT_EXIT_SCRIPT = """\
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", sys.argv[1]])
print(child.pid, flush=True)
"""

# PID'lerini stdout'a değil bir dosyaya yazan ağaç. Gözlemci arızası yolunda
# çıktı toplanmaz; ölçüm yapılabilmesi için PID'lerin süreçten bağımsız bir
# yerde durması gerekir.
TREE_PID_FILE_SCRIPT = """\
import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(sys.argv[1]).write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return False
    return stat_path.read_text(encoding="utf-8").split()[2] != "Z"


def _cleanup_pid(pid: int | None) -> None:
    if pid is not None and _alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize(
    ("mode", "limits", "reason"),
    [
        ("timeout", ProcessLimits(timeout_seconds=0.3, max_output_bytes=1_000_000), "timeout"),
        ("output", ProcessLimits(timeout_seconds=10, max_output_bytes=1024), "output"),
    ],
)
def test_descendants_are_killed_on_bounded_failure(
    tmp_path: Path, mode: str, limits: ProcessLimits, reason: str
) -> None:
    outcome = run_bounded_process(
        [sys.executable, "-c", TREE_SCRIPT, mode],
        work_dir=tmp_path,
        environment={"PATH": os.environ["PATH"]},
        limits=limits,
    )
    pids = [int(value) for value in outcome.stdout_text.splitlines()[0].split()]
    assert len(pids) == 3
    assert not any(_alive(pid) for pid in pids), reason
    assert outcome.timed_out is (mode == "timeout")
    assert outcome.oversized_stream == ("stdout" if mode == "output" else None)


def test_parent_exits_but_child_sleeps(tmp_path: Path) -> None:
    child_pid: int | None = None
    timeout = 0.4
    started = time.monotonic()
    try:
        outcome = run_bounded_process(
            [
                sys.executable,
                "-c",
                PARENT_EXIT_SCRIPT,
                "import time; time.sleep(60)",
            ],
            work_dir=tmp_path,
            environment={"PATH": os.environ["PATH"]},
            limits=ProcessLimits(timeout_seconds=timeout, max_output_bytes=100_000),
        )
        elapsed = time.monotonic() - started
        child_pid = int(outcome.stdout_text.splitlines()[0])

        assert outcome.timed_out
        assert not _alive(child_pid)
        assert elapsed < timeout + process_module.TERMINATE_GRACE_SECONDS + 1.0
    finally:
        _cleanup_pid(child_pid)


def test_parent_exits_then_child_exceeds_output(tmp_path: Path) -> None:
    child_pid: int | None = None
    try:
        outcome = run_bounded_process(
            [
                sys.executable,
                "-c",
                PARENT_EXIT_SCRIPT,
                "import sys,time; time.sleep(0.1); "
                "sys.stdout.write('x' * 200000); sys.stdout.flush(); time.sleep(60)",
            ],
            work_dir=tmp_path,
            environment={"PATH": os.environ["PATH"]},
            limits=ProcessLimits(timeout_seconds=5, max_output_bytes=1024),
        )
        child_pid = int(outcome.stdout_text.splitlines()[0])

        assert not outcome.timed_out
        assert outcome.oversized_stream == "stdout"
        assert not _alive(child_pid)
    finally:
        _cleanup_pid(child_pid)


def test_parent_exits_child_finishes_within_deadline(tmp_path: Path) -> None:
    child_pid: int | None = None
    started = time.monotonic()
    try:
        outcome = run_bounded_process(
            [
                sys.executable,
                "-c",
                PARENT_EXIT_SCRIPT,
                "import time; time.sleep(0.2)",
            ],
            work_dir=tmp_path,
            environment={"PATH": os.environ["PATH"]},
            limits=ProcessLimits(timeout_seconds=2, max_output_bytes=100_000),
        )
        elapsed = time.monotonic() - started
        child_pid = int(outcome.stdout_text.splitlines()[0])

        assert not outcome.timed_out
        assert outcome.oversized_stream is None
        assert elapsed >= 0.15
        assert elapsed < 2
        assert not _alive(child_pid)
    finally:
        _cleanup_pid(child_pid)


def test_concurrent_termination_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(60)",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == b"ready\n"
    supervisor = ProcessSupervisor(process)
    real_killpg = os.killpg
    signals: list[tuple[int, int]] = []

    def _record_killpg(process_group: int, signal_number: int) -> None:
        signals.append((process_group, signal_number))
        real_killpg(process_group, signal_number)

    monkeypatch.setattr(process_module.os, "killpg", _record_killpg)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: supervisor.terminate(), range(3)))
    assert process.returncode is not None
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]


def test_termination_after_early_exit_is_harmless() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    supervisor = ProcessSupervisor(process)
    assert not supervisor.wait(5)
    assert process.returncode == 0
    supervisor.terminate()
    supervisor.terminate()


def test_finalized_group_is_fenced_and_never_signaled_after_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completion ile reader talebinin kilit sırası deterministik ölçülür."""
    reap_started = Event()
    reader_requested = Event()

    class FakeProcess:
        pid = 424_242
        returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            reap_started.set()
            assert reader_requested.wait(timeout=2)
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            raise AssertionError("reap başladıktan sonra terminate çağrılamaz")

        def kill(self) -> None:
            raise AssertionError("reap başladıktan sonra kill çağrılamaz")

    fake = FakeProcess()
    monkeypatch.setattr(process_module.os, "getpgid", lambda _pid: fake.pid)
    monkeypatch.setattr(process_module.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(
        process_module.os,
        "waitid",
        lambda *_args: SimpleNamespace(si_pid=fake.pid),
    )
    group_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_module.os,
        "killpg",
        lambda pgid, signum: group_signals.append((pgid, signum)),
    )
    supervisor = ProcessSupervisor(cast("subprocess.Popen[bytes]", fake))

    def _reader_request() -> None:
        assert reap_started.wait(timeout=2)
        supervisor.request_termination()
        reader_requested.set()

    reader = Thread(target=_reader_request)
    reader.start()
    assert not supervisor.wait(2)
    reader.join(timeout=2)
    supervisor.terminate()

    assert not reader.is_alive()
    assert fake.returncode == 0
    assert group_signals == [(fake.pid, signal.SIGSTOP)]


# --- Gözlemci yaşam döngüsü --------------------------------------------------


class _StartFailingObserver:
    """``start()`` arıza veren gözlemci.

    Arızayı **ağaç kurulduktan sonra** üretir: süreç henüz doğmadan atılan bir
    hata, "child arkada kalmadı" iddiasını boş bir tautolojiye çevirirdi.
    """

    def __init__(self, pid_file: Path) -> None:
        self._pid_file = pid_file
        self.stop_calls = 0

    def start(self, request_termination: object) -> None:
        del request_termination
        deadline = time.monotonic() + 10.0
        while not self._pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise RuntimeError("gozlemci baslatilamadi")

    def stop(self) -> None:
        self.stop_calls += 1


class _StopFailingObserver:
    """``stop()`` arıza veren gözlemci."""

    def __init__(self) -> None:
        self.started = False

    def start(self, request_termination: object) -> None:
        del request_termination
        self.started = True

    def stop(self) -> None:
        raise RuntimeError("gozlemci durdurulamadi")


def _wait_until_dead(pids: list[int], *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(_alive(pid) for pid in pids):
        time.sleep(0.02)


def test_an_observer_start_failure_leaves_no_running_child(tmp_path: Path) -> None:
    """Gözlemci başlatılamazsa child ve torunu arka planda bırakılmaz.

    Gözlemcisi olmayan bir süreç ölçülmeyen bir süreçtir; hatayı yutup devam
    etmek sınırsız bir çalıştırma bırakırdı. Hata yutulmaz da: çağıran, süreç
    sonucu yerine arızayı görür.
    """
    pid_file = tmp_path / "pids.txt"
    observer = _StartFailingObserver(pid_file)
    pids: list[int] = []
    try:
        with pytest.raises(RuntimeError):
            run_bounded_process(
                [sys.executable, "-c", TREE_PID_FILE_SCRIPT, str(pid_file)],
                work_dir=tmp_path,
                environment={"PATH": os.environ["PATH"]},
                limits=ProcessLimits(timeout_seconds=60, max_output_bytes=100_000),
                observer=observer,
            )

        pids = [int(value) for value in pid_file.read_text(encoding="utf-8").split()]
        assert len(pids) == 2
        _wait_until_dead(pids, timeout=process_module.TERMINATE_GRACE_SECONDS + 2.0)
        assert not any(_alive(pid) for pid in pids)
        # `start()` düştüğünde `stop()` çağrılmaz: hiç başlamamış bir ölçümün
        # durdurulacak bir tarafı yoktur.
        assert observer.stop_calls == 0
    finally:
        for pid in pids:
            _cleanup_pid(pid)


def test_an_observer_stop_failure_still_reaps_the_child(tmp_path: Path) -> None:
    """``stop()`` arızası da child'ı sahipsiz bırakmaz ve sessizce yutulmaz.

    Süreç doğrudan burada açılır: ancak elde bir ``Popen`` varken "reap edildi
    mi" ve "pipe'lar kapandı mı" sorularının ikisi de ölçülebilir.
    """
    del tmp_path
    observer = _StopFailingObserver()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        bufsize=0,
    )
    with pytest.raises(RuntimeError):
        process_module.collect_bounded_output(
            process,
            ProcessLimits(timeout_seconds=0.3, max_output_bytes=100_000),
            observer=observer,
        )

    assert observer.started is True
    assert process.returncode is not None
    assert not _alive(process.pid)
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


def test_successful_process_is_unchanged(tmp_path: Path) -> None:
    outcome = run_bounded_process(
        [sys.executable, "-c", "print('ok')"],
        work_dir=tmp_path,
        environment={"PATH": os.environ["PATH"]},
        limits=ProcessLimits(timeout_seconds=5, max_output_bytes=100),
    )
    assert outcome.return_code == 0
    assert outcome.stdout_text == "ok\n"
    assert not outcome.timed_out
    assert outcome.oversized_stream is None
