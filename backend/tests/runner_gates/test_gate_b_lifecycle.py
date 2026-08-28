"""Kapı B — Lifecycle ve containment ölçümü.

PRODUCTION KODU DEĞİLDİR (bkz. paket docstring'i).

ÖLÇÜM SINIRI — bu iki şey aynı şey DEĞİLDİR:

* **Controller-side process tree**: Runner child'ı, `ansible-playbook` süreci ve
  bunların controller üzerindeki descendant'ları. Bu tur YALNIZ bunu ölçer.
* **Uzak Ansible async workload**: `async`/`poll: 0` ile uzak host üzerinde
  başlatılan iş. Bu tur SSH/uzak host kullanmadığı için uzak async'in
  sonlandığı BU PROBE İLE KANITLANAMAZ ve kanıtlanmış sayılmamalıdır.

Üç senaryo ölçülür: job timeout, worker'a SIGTERM ve worker'a SIGKILL. Her
senaryoda normal descendant ile kendi session'ını açmaya çalışan adversarial
descendant ayrı ayrı izlenir.

"worker SIGTERM" senaryosu, worker sürecine SIGTERM göndermekten ibarettir.
**Cooperative bir FastAPI lifespan shutdown'ı değildir**; uygulamanın kendi
kapanış yolunu çalıştırma fırsatı bu probe'da ölçülmemiştir ve öyle
sunulmamalıdır.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.runner_gates import probe_support as ps

pytestmark = [
    pytest.mark.runner_gate,
    pytest.mark.skipif(not ps.IS_LINUX, reason=ps.NON_LINUX_SKIP_REASON),
]

# Check mode'un kapsam sınırı olarak kullanılıp kullanılamayacağını ölçen
# playbook'lar. `check_mode: false` taşıyan task, `--check` altında bile
# GERÇEKTEN çalışır; bu, "check mode kullanıyoruz" sınırının kendiliğinden
# yeterli olup olmadığını belirler.
CHECK_MATRIX_PLAIN = """
- name: gate-b check matrix (plain)
  hosts: probe
  gather_facts: false
  tasks:
    - name: uzun command task
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "import time; time.sleep(600)"
          - "{{ probe_marker }}-task"
      changed_when: false
"""

# Her vaka icin TEK expected matrisi. Rapor ve assertion ayni kaynaktan besilenir.
CHECK_MATRIX_EXPECTED: dict[str, dict[str, object]] = {
    "normal-mode": {"real_task_started": True, "runner_status": "timeout", "residue_after_run": 1},
    "check-mode": {
        "real_task_started": False,
        "runner_status": "successful",
        "residue_after_run": 0,
    },
    "check-mode-forced-real": {
        "real_task_started": True,
        "runner_status": "timeout",
        "residue_after_run": 1,
    },
}

CHECK_MATRIX_FORCED = """
- name: gate-b check matrix (check_mode false)
  hosts: probe
  gather_facts: false
  tasks:
    - name: check altinda bile gercekten calisan task
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "import time; time.sleep(600)"
          - "{{ probe_marker }}-task"
      changed_when: false
      check_mode: false
"""

# Task içinden arka planda, task bittikten sonra da yaşayacak bir descendant
# başlatır. `start_new_session` ile adversarial varyant kendi session/process
# group'unu açar; POSIX process-group tabanlı sonlandırmadan kaçmayı dener.
SPAWN_SCRIPT = """
import subprocess, sys

marker, mode = sys.argv[1], sys.argv[2]
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(900)", marker, mode],
    start_new_session=(mode == "setsid"),
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
)
"""

PLAYBOOK = """
- name: gate-b lifecycle probe
  hosts: probe
  gather_facts: false
  tasks:
    - name: normal descendant baslat
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "{{ probe_spawn_script }}"
          - "{{ probe_marker }}-normal"
          - "normal"
      changed_when: false

    - name: adversarial descendant baslat
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "{{ probe_spawn_script }}"
          - "{{ probe_marker }}-setsid"
          - "setsid"
      changed_when: false

    - name: uzun suren task
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "import time; time.sleep(600)"
          - "{{ probe_marker }}-longtask"
      changed_when: false
"""


@dataclass
class Scenario:
    """Tek bir lifecycle senaryosunun ölçüm çıktısı."""

    name: str
    helper_pid: int
    ansible_playbook_pids: list[int]
    tree_before: list[ps.ProcInfo]
    identities: dict[str, dict[str, int] | None]
    helper_alive_after: bool
    ansible_playbook_alive_after: bool
    residue_after_event: list[ps.ProcInfo]
    seconds_to_settle: float
    runner_status: str | None
    residue_after_cleanup: list[ps.ProcInfo]
    cleaned_pids: list[int]


def _children_of(pid: int) -> list[int]:
    """`/proc` üzerinden doğrudan çocuk PID'leri."""
    kids: list[int] = []
    for entry in ps.PROC.iterdir():
        if not entry.name.isdigit():
            continue
        stat = ps._read_stat(int(entry.name))
        if stat is not None and stat[0] == pid:
            kids.append(int(entry.name))
    return kids


def _alive(pid: int) -> bool:
    return (ps.PROC / str(pid)).exists()


def _identity_of(pid: int) -> dict[str, int] | None:
    """Tek bir sürecin (ppid, pgid, sid) kimliği."""
    stat = ps._read_stat(pid)
    if stat is None:
        return None
    return {"pid": pid, "ppid": stat[0], "pgid": stat[1], "sid": stat[2]}


def _describe(procs: list[ps.ProcInfo]) -> list[dict[str, object]]:
    return [
        {
            "pid": p.pid,
            "ppid": p.ppid,
            "pgid": p.pgid,
            "sid": p.sid,
            "tag": p.cmdline.rsplit("-", 1)[-1],
        }
        for p in procs
    ]


def _build_workspace(tmp_path: Path, marker: str, job_timeout: int) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pdd = ps.make_private_data_dir(workspace)
    (pdd / "project" / "gate_b.yml").write_text(PLAYBOOK, encoding="utf-8")

    result_path = workspace / "result.json"
    config_path = workspace / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "private_data_dir": str(pdd),
                "playbook": "gate_b.yml",
                "settings": {"job_timeout": job_timeout, "suppress_ansible_output": True},
                "extravars": {
                    "probe_python": sys.executable,
                    "probe_spawn_script": SPAWN_SCRIPT,
                    "probe_marker": marker,
                },
                "result_path": str(result_path),
            }
        ),
        encoding="utf-8",
    )
    return workspace, config_path, result_path


def _run_scenario(
    tmp_path: Path,
    *,
    name: str,
    job_timeout: int,
    disruption: str,
    settle_timeout: float,
) -> Scenario:
    """Senaryoyu çalıştırır, süreç ağacını ve kalıntıyı ölçer, temizler."""
    marker = ps.new_marker(name)
    workspace, config_path, result_path = _build_workspace(tmp_path, marker, job_timeout)
    env = ps.build_isolated_environment(workspace=workspace, venv_bin=ps.venv_bin_dir())
    child_script = Path(__file__).parent / "runner_child.py"

    helper = subprocess.Popen(  # noqa: S603
        [sys.executable, str(child_script), str(config_path)],
        env=env,
        close_fds=True,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(workspace),
    )

    try:
        # Her iki descendant ve uzun task görünene kadar bekle.
        tree_before = ps.wait_for_marker_processes(marker, minimum=3, timeout=90.0)
        ansible_pids = _children_of(helper.pid)
        # Sonlandırma stratejisi seçimi için: helper ve `ansible-playbook`
        # hangi process group/session'da? Task süreçleri onlarla aynı grupta mı?
        identities = {"helper": _identity_of(helper.pid)}
        for pid in ansible_pids:
            identities[f"ansible_playbook:{pid}"] = _identity_of(pid)

        if disruption == "timeout":
            # Runner'ın kendi job_timeout'u devreye girsin; helper kendiliğinden biter.
            try:
                helper.wait(timeout=job_timeout + 60)
            except subprocess.TimeoutExpired:
                pass
        elif disruption == "sigterm":
            os.kill(helper.pid, signal.SIGTERM)
            try:
                helper.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        elif disruption == "sigkill":
            os.kill(helper.pid, signal.SIGKILL)
            try:
                helper.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass

        # Kalıntının kendiliğinden yok olup olmadığını sınırlı süre bekle.
        settled, seconds = ps.wait_until_gone(marker, timeout=settle_timeout)
        residue = ps.scan_marker_processes(marker)
        helper_alive = _alive(helper.pid) and helper.poll() is None
        playbook_alive = any(_alive(pid) for pid in ansible_pids)

        status: str | None = None
        if result_path.exists():
            try:
                status = str(json.loads(result_path.read_text(encoding="utf-8"))["status"])
            except (OSError, ValueError, KeyError):
                status = None

        assert settled == (not residue)
    finally:
        # Zorunlu cleanup: yalnız bu testin marker'ını taşıyan süreçler.
        cleaned = ps.terminate_marker_processes(marker, grace=5.0)
        if helper.poll() is None:
            helper.kill()
        helper.wait(timeout=10)

    time.sleep(0.2)
    residue_after_cleanup = ps.scan_marker_processes(marker)

    return Scenario(
        name=name,
        helper_pid=helper.pid,
        ansible_playbook_pids=ansible_pids,
        tree_before=tree_before,
        identities=identities,
        helper_alive_after=helper_alive,
        ansible_playbook_alive_after=playbook_alive,
        residue_after_event=residue,
        seconds_to_settle=seconds,
        runner_status=status,
        residue_after_cleanup=residue_after_cleanup,
        cleaned_pids=cleaned,
    )


def _report(scenario: Scenario) -> None:
    print(
        f"\nGATE-B MEASUREMENT [{scenario.name}] "
        + json.dumps(
            {
                "helper_pid": scenario.helper_pid,
                "ansible_playbook_pids": scenario.ansible_playbook_pids,
                "tree_before_event": _describe(scenario.tree_before),
                "controller_identities": scenario.identities,
                "runner_status": scenario.runner_status,
                "helper_alive_after_event": scenario.helper_alive_after,
                "ansible_playbook_alive_after_event": scenario.ansible_playbook_alive_after,
                "residue_after_event": _describe(scenario.residue_after_event),
                "residue_count_after_event": len(scenario.residue_after_event),
                "seconds_waited_for_settle": round(scenario.seconds_to_settle, 2),
                "cleanup_signalled_pids": scenario.cleaned_pids,
                "residue_after_cleanup": _describe(scenario.residue_after_cleanup),
            },
            indent=2,
        )
    )


@pytest.mark.parametrize(
    ("name", "job_timeout", "disruption", "expected_status", "expect_playbook_alive"),
    [
        # "worker SIGTERM", worker'a SIGTERM göndermektir. Cooperative bir
        # FastAPI lifespan shutdown'ı DEĞİLDİR ve öyle sunulmamalıdır: burada
        # worker'ın kendi kapanış yolunu çalıştırma fırsatı ölçülmüyor.
        ("timeout", 8, "timeout", "timeout", False),
        ("worker-sigterm", 300, "sigterm", "canceled", False),
        ("worker-sigkill", 300, "sigkill", None, True),
    ],
)
def test_gate_b_controller_process_tree_containment(
    tmp_path: Path,
    name: str,
    job_timeout: int,
    disruption: str,
    expected_status: str | None,
    expect_playbook_alive: bool,
) -> None:
    """Üç lifecycle olayında controller process tree'nin akıbetini ölçer.

    Assertion'lar ÖLÇÜLEN GÜVENSİZ DAVRANIŞI sabitler. Runner veya platform
    ileride bu davranışı düzeltirse test DÜŞER; böylece ADR-021 Kapı B sonucu
    sessizce eskimez, yeniden değerlendirilmesi zorlanır.
    """
    scenario = _run_scenario(
        tmp_path,
        name=name,
        job_timeout=job_timeout,
        disruption=disruption,
        settle_timeout=10.0,
    )
    _report(scenario)

    # 1. Ölçümün anlamlı olması için beklenen süreçler gerçekten oluşmalıydı.
    assert len(scenario.tree_before) >= 3, (
        "Olay oncesi 3 marker'li surec (normal, setsid, longtask) bekleniyordu; "
        f"olculen: {_describe(scenario.tree_before)}"
    )
    assert scenario.ansible_playbook_pids != [], "ansible-playbook sureci bulunamadi"

    # 2. Runner'ın raporladığı status ölçülmüş olmalı.
    assert scenario.runner_status == expected_status, (
        f"[{name}] runner status beklenen={expected_status} olculen={scenario.runner_status}"
    )

    # 3. Helper her senaryoda ölmeli; `ansible-playbook`'un akıbeti senaryoya bağlı.
    assert scenario.helper_alive_after is False
    assert scenario.ansible_playbook_alive_after is expect_playbook_alive, (
        f"[{name}] ansible-playbook yasam durumu beklenen={expect_playbook_alive} "
        f"olculen={scenario.ansible_playbook_alive_after}"
    )

    # 4. GHOST WORKLOAD: ölçülen güvensiz sonuç. Üç senaryoda da 3 süreç kaldı.
    assert len(scenario.residue_after_event) == 3, (
        f"[{name}] olay sonrasi 3 ghost surec bekleniyordu, "
        f"olculen {len(scenario.residue_after_event)}: "
        f"{_describe(scenario.residue_after_event)}. Davranis degistiyse ADR-021 "
        "Kapi B yeniden degerlendirilmelidir."
    )

    # 5. Probe kendi ürettiği her şeyi temizlemiş olmalı.
    assert scenario.residue_after_cleanup == [], (
        f"Probe kendi surecini temizleyemedi; kalinti: {_describe(scenario.residue_after_cleanup)}"
    )


PLAIN_PLAYBOOK = """
- name: gate-b plain task probe
  hosts: probe
  gather_facts: false
  tasks:
    - name: arka plana is atmayan siradan task
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "import time; time.sleep(600)"
          - "{{ probe_marker }}-plain"
      changed_when: false
"""


def test_gate_b_plain_task_survives_job_timeout(tmp_path: Path) -> None:
    """Arka plana iş atmayan SIRADAN bir task job_timeout'tan sağ çıkıyor mu?

    Bu ölçüm, hangi kapsam daraltmalarının ELENDİĞİNİ belirler. Adversarial
    olmayan, arka plana hiçbir şey atmayan bir task bile timeout sonrasında
    yaşamaya devam ediyorsa, "playbook'ları iyi davrananlarla sınırlarız"
    türünden bir kapsam kuralı Kapı B'yi kapatmaz.

    Bu, "hiçbir kapsam daraltması mümkün değildir" DEMEK DEĞİLDİR. Ölçümle
    elenen kapsamlar yalnız şunlardır: (a) "kısa süren / iyi davranan playbook"
    — sıradan task da kaçtığı için; (b) "yalnız `--check` veriyoruz" —
    `check_mode: false` ile delinebildiği için (bkz.
    `test_gate_b_check_mode_scope_matrix`). `check_mode: false`, `async`,
    `local_action` ve plugin dizinlerini fail-closed reddeden enforce
    edilebilir bir içerik kapsamı hâlâ açık bir seçenektir; bu turda
    uygulanmadığı için Kapı B OPEN kalır.
    """
    marker = ps.new_marker("plain-timeout")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pdd = ps.make_private_data_dir(workspace)
    (pdd / "project" / "gate_b_plain.yml").write_text(PLAIN_PLAYBOOK, encoding="utf-8")

    result_path = workspace / "result.json"
    config_path = workspace / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "private_data_dir": str(pdd),
                "playbook": "gate_b_plain.yml",
                "settings": {"job_timeout": 8, "suppress_ansible_output": True},
                "extravars": {"probe_python": sys.executable, "probe_marker": marker},
                "result_path": str(result_path),
            }
        ),
        encoding="utf-8",
    )

    env = ps.build_isolated_environment(workspace=workspace, venv_bin=ps.venv_bin_dir())
    child_script = Path(__file__).parent / "runner_child.py"
    helper = subprocess.Popen(  # noqa: S603
        [sys.executable, str(child_script), str(config_path)],
        env=env,
        close_fds=True,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(workspace),
    )

    try:
        before = ps.wait_for_marker_processes(marker, minimum=1, timeout=90.0)
        assert before != [], "Uzun task sureci hic olusmadi; olcum anlamsiz"
        try:
            helper.wait(timeout=90)
        except subprocess.TimeoutExpired:
            pass
        settled, seconds = ps.wait_until_gone(marker, timeout=10.0)
        residue = ps.scan_marker_processes(marker)
        status: str | None = None
        if result_path.exists():
            try:
                status = str(json.loads(result_path.read_text(encoding="utf-8"))["status"])
            except (OSError, ValueError, KeyError):
                status = None
    finally:
        cleaned = ps.terminate_marker_processes(marker, grace=5.0)
        if helper.poll() is None:
            helper.kill()
        helper.wait(timeout=10)

    time.sleep(0.2)
    after_cleanup = ps.scan_marker_processes(marker)

    print(
        "\nGATE-B MEASUREMENT [plain-task-timeout] "
        + json.dumps(
            {
                "runner_status": status,
                "task_before_event": _describe(before),
                "settled_by_itself": settled,
                "seconds_waited": round(seconds, 2),
                "residue_after_event": _describe(residue),
                "residue_count_after_event": len(residue),
                "cleanup_signalled_pids": cleaned,
                "residue_after_cleanup": _describe(after_cleanup),
            },
            indent=2,
        )
    )

    assert status == "timeout", f"Runner status beklenen=timeout olculen={status}"
    assert settled is False, "Sıradan task timeout sonrasi kendiliginden sonlanmis"
    assert len(residue) == 1, (
        "Arka plana is atmayan siradan bir task'in timeout'tan sag ciktigi olculmustu; "
        f"simdi {len(residue)} kalinti var. ADR-021 Kapi B yeniden degerlendirilmelidir."
    )
    assert after_cleanup == [], f"Probe temizleyemedi: {_describe(after_cleanup)}"


@pytest.mark.parametrize(
    ("name", "playbook_body", "cmdline"),
    [
        ("normal-mode", CHECK_MATRIX_PLAIN, None),
        ("check-mode", CHECK_MATRIX_PLAIN, "--check"),
        ("check-mode-forced-real", CHECK_MATRIX_FORCED, "--check"),
    ],
    ids=["normal-mode", "check-mode", "check-mode-forced-real"],
)
def test_gate_b_check_mode_scope_matrix(
    tmp_path: Path,
    name: str,
    playbook_body: str,
    cmdline: str | None,
) -> None:
    """`--check` bir kapsam sınırı olarak güvenilir mi?

    Üç durum ayrı ayrı ölçülür. Beklenti VARSAYILMAZ; gerçek sonuç kaydedilir.
    `check_mode: false` taşıyan bir task `--check` altında da gerçek süreç
    başlatıyorsa, "check mode kullanıyoruz" tek başına bir containment sınırı
    değildir ve Kapı B bu yolla kapatılamaz.
    """
    marker = ps.new_marker(f"checkmatrix-{name}")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pdd = ps.make_private_data_dir(workspace)
    (pdd / "project" / "matrix.yml").write_text(playbook_body, encoding="utf-8")

    result_path = workspace / "result.json"
    config_path = workspace / "config.json"
    config: dict[str, object] = {
        "private_data_dir": str(pdd),
        "playbook": "matrix.yml",
        "settings": {"job_timeout": 25, "suppress_ansible_output": True},
        "extravars": {"probe_python": sys.executable, "probe_marker": marker},
        "result_path": str(result_path),
    }
    if cmdline:
        config["cmdline"] = cmdline
    config_path.write_text(json.dumps(config), encoding="utf-8")

    env = ps.build_isolated_environment(workspace=workspace, venv_bin=ps.venv_bin_dir())
    child_script = Path(__file__).parent / "runner_child.py"
    helper = subprocess.Popen(  # noqa: S603
        [sys.executable, str(child_script), str(config_path)],
        env=env,
        close_fds=True,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(workspace),
    )

    try:
        observed = ps.wait_for_marker_processes(marker, minimum=1, timeout=30.0)
        real_task_started = len(observed) >= 1
        helper_finished = True
        try:
            helper.wait(timeout=90)
        except subprocess.TimeoutExpired:
            helper_finished = False
        status: str | None = None
        if result_path.exists():
            try:
                status = str(json.loads(result_path.read_text(encoding="utf-8"))["status"])
            except (OSError, ValueError, KeyError):
                status = None
        residue_after = ps.scan_marker_processes(marker)
    finally:
        ps.terminate_marker_processes(marker, grace=5.0)
        if helper.poll() is None:
            helper.kill()
        helper.wait(timeout=10)

    time.sleep(0.2)
    residue_after_cleanup = ps.scan_marker_processes(marker)

    print(
        f"\nGATE-B MEASUREMENT [check-matrix:{name}] "
        + json.dumps(
            {
                "cmdline": cmdline,
                "real_task_process_started": real_task_started,
                "observed": _describe(observed),
                "runner_status": status,
                "residue_after_run": len(residue_after),
                "expected": CHECK_MATRIX_EXPECTED[name],
                "helper_finished": helper_finished,
                "residue_after_cleanup": _describe(residue_after_cleanup),
            },
            indent=2,
        )
    )

    expected = CHECK_MATRIX_EXPECTED[name]

    # Helper kendiliğinden bitmiş olmalı; hâlâ çalışıyorsa ölçüm güvenilmezdir.
    assert helper_finished is True, f"[{name}] helper beklenmedik sekilde hala calisiyor"
    # Her vakada Runner bir sonuç yazmış olmalı; yazamamak açık test hatasıdır.
    assert status is not None, f"[{name}] result.json uretilemedi"

    assert real_task_started is expected["real_task_started"], (
        f"[{name}] gercek task sureci beklenen={expected['real_task_started']} "
        f"olculen={real_task_started}"
    )
    assert status == expected["runner_status"], (
        f"[{name}] runner status beklenen={expected['runner_status']} olculen={status}"
    )
    assert len(residue_after) == expected["residue_after_run"], (
        f"[{name}] calisma sonrasi kalinti beklenen={expected['residue_after_run']} "
        f"olculen={len(residue_after)}: {_describe(residue_after)}"
    )
    assert residue_after_cleanup == [], f"cleanup sonrasi kalinti: {residue_after_cleanup}"
