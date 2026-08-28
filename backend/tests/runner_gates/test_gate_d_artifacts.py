"""Kapı D — Artifact güvenliği ölçümü.

PRODUCTION KODU DEĞİLDİR (bkz. paket docstring'i).

Sentetik secret'lar altı ayrı kanaldan işe sokulur ve Runner'ın ürettiği BÜTÜN
dosyalar binary-safe taranır. Amaç, "0700 yeterli" ve "`no_log` yeterli"
varsayımlarını ölçümle sınamaktır (ADR-021 Kapı D).

Buradaki secret değerlerin hiçbiri gerçek değildir; hepsi test içinde üretilmiş
sentetik dizelerdir ve hiçbir uzak sisteme gönderilmez.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.security.redaction import REDACTED, redact_text
from tests.runner_gates import probe_support as ps

pytestmark = [
    pytest.mark.runner_gate,
    pytest.mark.skipif(not ps.IS_LINUX, reason=ps.NON_LINUX_SKIP_REASON),
]

PLAYBOOK = """
- name: gate-d artifact probe
  hosts: probe
  gather_facts: false
  tasks:
    - name: stdout'a acikca sentetik secret bas
      ansible.builtin.debug:
        msg: "printed-secret {{ probe_stdout_secret }}"

    - name: task argumaninda sentetik secret
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "pass"
          - "{{ probe_taskarg_secret }}"
      changed_when: false

    - name: no_log ile sentetik secret
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "pass"
          - "{{ probe_nolog_secret }}"
      changed_when: false
      no_log: true

    - name: environment'tan okunan secret'i stdout'a bas
      ansible.builtin.debug:
        msg: "env-secret {{ lookup('ansible.builtin.env', 'AOPS_PROBE_ENV_SECRET') }}"
      when: probe_leak_env | bool

    - name: uzun suren task (timeout senaryosu icin)
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "import time; time.sleep(600)"
          - "{{ probe_marker }}-dwell"
      changed_when: false
      when: probe_dwell | bool

    - name: basarisiz task, argumaninda secret
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "import sys; sys.exit(3)"
          - "{{ probe_fail_secret }}"
      changed_when: false
      when: probe_fail | bool
"""

SECRETS: dict[str, str] = {
    "env": "AOPSD-ENVSECRET-1a2b3c4d",
    "extravar": "AOPSD-EXTRAVAR-2b3c4d5e",
    "taskarg": "AOPSD-TASKARG-3c4d5e6f",
    "stdout": "AOPSD-STDOUT-4d5e6f70",
    "nolog": "AOPSD-NOLOG-5e6f7081",
    "fail": "AOPSD-FAILARG-6f708192",
}


# ÖLÇÜLEN PLATFORM SÖZLEŞMESİ. Her senaryo icin TAM kume; uyelik kontrolu
# (`x in ...`) bilincli olarak YETERLI SAYILMAZ, cunku beklenmeyen YENI bir
# sizinti kanali sessizce gecerdi. Runner/platform davranisi degisirse test
# duser ve ADR-021 Kapi D yeniden olculur.
EXPECTED_LEAKS: dict[str, dict[str, list[str]]] = {
    "complete": {
        "env": ["artifacts/command", "artifacts/job_events", "artifacts/stdout", "env/envvars"],
        "extravar": ["env/extravars"],
        "taskarg": ["artifacts/job_events", "env/extravars"],
        "stdout": ["artifacts/job_events", "artifacts/stdout", "env/extravars"],
        "nolog": ["env/extravars"],
        "fail": ["env/extravars"],
    },
    "failed": {
        "env": ["artifacts/command", "artifacts/job_events", "artifacts/stdout", "env/envvars"],
        "extravar": ["env/extravars"],
        "taskarg": ["artifacts/job_events", "env/extravars"],
        "stdout": ["artifacts/job_events", "artifacts/stdout", "env/extravars"],
        "nolog": ["env/extravars"],
        "fail": ["artifacts/job_events", "artifacts/stdout", "env/extravars"],
    },
    "timeout": {
        "env": ["artifacts/command", "artifacts/job_events", "artifacts/stdout", "env/envvars"],
        "extravar": ["env/extravars"],
        "taskarg": ["artifacts/job_events", "env/extravars"],
        "stdout": ["artifacts/job_events", "artifacts/stdout", "env/extravars"],
        "nolog": ["env/extravars"],
        "fail": ["env/extravars"],
    },
}


def _classify(path: Path, pdd: Path) -> str:
    """Sızıntı bulunan dosyayı Kapı D'nin ölçüm kategorilerine ayırır."""
    rel = path.relative_to(pdd)
    parts = rel.parts
    if parts[0] == "artifacts":
        if "job_events" in parts:
            return "artifacts/job_events"
        return f"artifacts/{rel.name}"
    if parts[0] == "env":
        return f"env/{rel.name}"
    if parts[0] == "project":
        return "project"
    return "other/" + "/".join(parts[:-1]) if len(parts) > 1 else "other"


def _run_case(
    tmp_path: Path,
    *,
    case: str,
    fail: bool,
    dwell: bool,
    job_timeout: int,
) -> dict[str, object]:
    marker = ps.new_marker(f"gated-{case}")
    workspace = tmp_path / case
    workspace.mkdir()
    pdd = ps.make_private_data_dir(workspace)
    ps.write_project_file(pdd, "gate_d.yml", PLAYBOOK)

    result_path = workspace / "result.json"
    config_path = workspace / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "private_data_dir": str(pdd),
                "playbook": "gate_d.yml",
                "settings": {"job_timeout": job_timeout, "suppress_ansible_output": True},
                "envvars": {"AOPS_PROBE_ENV_SECRET": SECRETS["env"]},
                "extravars": {
                    "probe_python": sys.executable,
                    "probe_marker": marker,
                    "probe_extravar_secret": SECRETS["extravar"],
                    "probe_taskarg_secret": SECRETS["taskarg"],
                    "probe_stdout_secret": SECRETS["stdout"],
                    "probe_nolog_secret": SECRETS["nolog"],
                    "probe_fail_secret": SECRETS["fail"],
                    "probe_fail": fail,
                    "probe_dwell": dwell,
                    "probe_leak_env": True,
                },
                "result_path": str(result_path),
            }
        ),
        encoding="utf-8",
    )

    env = ps.build_isolated_environment(workspace=workspace, venv_bin=ps.venv_bin_dir())
    child_script = Path(__file__).parent / "runner_child.py"

    try:
        subprocess.run(  # noqa: S603
            [sys.executable, str(child_script), str(config_path)],
            env=env,
            close_fds=True,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=job_timeout + 120,
            check=False,
            cwd=str(workspace),
        )
    finally:
        ps.terminate_marker_processes(marker, grace=5.0)

    status: str | None = None
    if result_path.exists():
        try:
            status = str(json.loads(result_path.read_text(encoding="utf-8"))["status"])
        except (OSError, ValueError, KeyError):
            status = None

    # Bütün ağacı binary-safe tara.
    matrix: dict[str, list[str]] = {}
    for label, value in SECRETS.items():
        hits = ps.find_bytes_in_tree(pdd, value)
        matrix[label] = sorted({_classify(hit, pdd) for hit in hits})

    total_files = len(ps.iter_all_files(pdd))
    artifacts_root = pdd / "artifacts"
    artifacts_mode = ps.mode_of(artifacts_root) if artifacts_root.exists() else None

    # Runner'ın oluşturduğu BÜTÜN dosya ve dizinlerin gerçek izinleri.
    # Politika: dizinler en fazla 0700, normal dosyalar en fazla 0600.
    loose_dirs: list[str] = []
    loose_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(pdd):
        for name in dirnames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                continue
            if entry.lstat().st_mode & 0o077:
                loose_dirs.append(f"{entry.relative_to(pdd)}={ps.mode_of(entry)}")
        for name in filenames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                continue
            if entry.lstat().st_mode & 0o177:
                loose_files.append(f"{entry.relative_to(pdd)}={ps.mode_of(entry)}")

    fact_cache_files = sorted(
        str(path.relative_to(pdd)) for path in pdd.glob("artifacts/*/fact_cache/*")
    )

    return {
        "case": case,
        "runner_status": status,
        "fact_cache_files": fact_cache_files,
        "fact_cache_modes": {
            str(path.relative_to(pdd)): ps.mode_of(path)
            for path in sorted(pdd.glob("artifacts/*/fact_cache/*"))
        },
        "file_count": total_files,
        "directory_modes": {
            "private_data_dir": ps.mode_of(pdd),
            "artifacts": artifacts_mode,
        },
        "entries_looser_than_0700_dirs": sorted(loose_dirs),
        "entries_looser_than_0600_files": sorted(loose_files),
        "marker_residue_after_cleanup": [p.pid for p in ps.scan_marker_processes(marker)],
        "leak_matrix": matrix,
    }


@pytest.mark.parametrize(
    ("case", "fail", "dwell", "job_timeout"),
    [
        ("complete", False, False, 120),
        ("failed", True, False, 120),
        ("timeout", False, True, 8),
    ],
)
def test_gate_d_secret_leakage_into_runner_artifacts(
    tmp_path: Path,
    case: str,
    fail: bool,
    dwell: bool,
    job_timeout: int,
) -> None:
    """Altı kanaldan giren sentetik secret'ların hangi artifact'lara düştüğünü ölçer.

    Bu test bir güvenlik iddiasını doğrulamaz; ham Runner çıktısının gerçekte ne
    içerdiğini KAYDEDER. Sonuçlar ADR-021 Kapı D'ye işlenir.
    """
    report = _run_case(tmp_path, case=case, fail=fail, dwell=dwell, job_timeout=job_timeout)
    print(f"\nGATE-D MEASUREMENT [{case}] " + json.dumps(report, indent=2))

    expected_status = {"complete": "successful", "failed": "failed", "timeout": "timeout"}[case]
    assert report["runner_status"] == expected_status

    matrix: dict[str, list[str]] = report["leak_matrix"]  # type: ignore[assignment]

    # 1. SIZINTI MATRISI TAM ESITLIKLE baglanir. Beklenmeyen yeni bir kanal da
    #    testi dusurur; uyelik kontrolu bunu kacirirdi.
    assert matrix == EXPECTED_LEAKS[case], (
        f"[{case}] sizinti matrisi degisti.\nbeklenen={EXPECTED_LEAKS[case]}\n"
        f"olculen={matrix}\nADR-021 Kapi D yeniden olculmelidir."
    )

    # 2. Dizinler: umask 0077 + urunun 0700 kurmasiyla hicbiri gevsek degil.
    assert report["entries_looser_than_0700_dirs"] == []

    # 3. Fact cache: assertion VACUOUS OLMAMALI. Once dosyanin gercekten
    #    olustugunu, sonra gevsek dosya kumesinin TAM OLARAK o dosyalardan
    #    ibaret oldugunu ve modunun tam 0644 oldugunu dogrula.
    fact_cache_files: list[str] = report["fact_cache_files"]  # type: ignore[assignment]
    fact_cache_modes: dict[str, str] = report["fact_cache_modes"]  # type: ignore[assignment]
    loose_files: list[str] = report["entries_looser_than_0600_files"]  # type: ignore[assignment]

    assert fact_cache_files != [], (
        "Beklenen fact cache dosyasi olusmadi; 0644 bulgusu artik olculmuyor "
        "ve ADR-021 Kapi D yeniden degerlendirilmelidir."
    )
    assert loose_files != [], "Gevsek dosya kumesi bos; fact cache bulgusu artik gecerli degil"
    assert sorted(loose_files) == sorted(f"{name}=0o644" for name in fact_cache_files), (
        f"Gevsek dosya kumesi yalnizca fact cache olmali.\nolculen={sorted(loose_files)}\n"
        f"fact_cache={fact_cache_files}"
    )
    for name in fact_cache_files:
        assert fact_cache_modes[name] == "0o644", f"{name} modu {fact_cache_modes[name]}"

    # 4. Fact cache disinda gevsek dosya olmamali.
    non_fact_cache = [entry for entry in loose_files if "/fact_cache/" not in entry]
    assert non_fact_cache == [], f"fact_cache disinda gevsek dosya: {non_fact_cache}"

    # 5. Probe kendi surecini birakmamali.
    assert report["marker_residue_after_cleanup"] == []


def test_gate_d_no_log_does_not_protect_environment_secrets(tmp_path: Path) -> None:
    """`no_log` ve 0700, environment secret'ını ham artifact'tan korumaz.

    ADR-021 Kapı D'nin dayanağı budur: Runner, çocuğa verdiği environment'ın
    TAMAMINI `artifacts/<uuid>/command` dosyasına düz metin yazar. `no_log`
    yalnız task sonuçlarının stdout/event gösterimini etkiler; bu dosyayı
    etkilemez.
    """
    report = _run_case(tmp_path, case="nolog-env", fail=False, dwell=False, job_timeout=120)
    matrix: dict[str, list[str]] = report["leak_matrix"]  # type: ignore[assignment]

    assert "artifacts/command" in matrix["env"], (
        "Environment secret'i command dosyasinda bulunamadi; Kapi D'nin dayanagi "
        f"yeniden olculmelidir. Olculen: {matrix}"
    )

    print("\nGATE-D CONCLUSION " + json.dumps({"leak_matrix": matrix}, indent=2))


# Normalize sonuç ADAYI — production kodu değildir, sözleşmenin ölçülebilir
# prototipidir. İki katmanlı savunma:
#
#   1. ALLOWLIST (birincil): ham `stdout`, ham event payload'ı, task argümanları
#      ve `msg` alanı sonuca HİÇ kopyalanmaz.
#   2. REDAKSİYON (ikincil): allowlist'te kalan ve proje/kullanıcı kontrolündeki
#      STRING alanlar (`task`, `host`) production `redact_text()` ile geçirilir.
#      Bunlar serbest metindir ve proje sahibi içlerine credential yazabilir.
#
# `redact_text` desen tabanlıdır (private key blokları, vault başlıkları,
# `Bearer ...`, `parola=...` biçimli atamalar). Rastgele bir dizeyi yakalayamaz;
# bu yüzden birincil savunma allowlist'tir, redaksiyon tek başına yeterli
# sayılmaz.
NORMALIZED_EVENT_FIELDS = ("host", "task", "changed", "failed", "skipped")

# Proje sahibinin yazdığı, görüntülenen task adının içine credential gömdüğü
# gerçekçi senaryo. `redact_text` bunu atama deseni olarak yakalar.
NORMALIZE_EXTRA_TASK = """
    - name: "deploy ansible_password={{ probe_taskname_secret }}"
      ansible.builtin.debug:
        msg: "task adinda secret var"
"""

# Ek task, BAŞARISIZ task'tan ÖNCE eklenir; sona eklenirse playbook ondan önce
# düşeceği için hiç çalışmaz ve ölçüm vacuous olurdu.
_FAIL_TASK_MARKER = "    - name: basarisiz task, argumaninda secret"
assert _FAIL_TASK_MARKER in PLAYBOOK
NORMALIZE_PLAYBOOK = PLAYBOOK.replace(
    _FAIL_TASK_MARKER, NORMALIZE_EXTRA_TASK.strip("\n") + "\n\n" + _FAIL_TASK_MARKER
)

NORMALIZE_SECRETS: dict[str, str] = {
    **SECRETS,
    "taskname": "AOPSD-TASKNAME-708192a3",
}


def normalize_job_events(private_data_dir: Path) -> list[dict[str, object]]:
    """Ham Runner event'lerinden yalnız allowlist'teki alanları çıkarır.

    `task` ve `host` proje kontrollü serbest metindir; **redakte edilmeden
    taşınmazlar**.
    """
    normalized: list[dict[str, object]] = []
    events_root = private_data_dir / "artifacts"
    for events_dir in sorted(events_root.glob("*/job_events")):
        for event_file in sorted(events_dir.glob("*.json")):
            try:
                raw = json.loads(event_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            data = raw.get("event_data") or {}
            if not isinstance(data, dict):
                continue
            raw_result = data.get("res")
            result: dict[str, object] = raw_result if isinstance(raw_result, dict) else {}
            allowed = ("event", *NORMALIZED_EVENT_FIELDS)
            entry: dict[str, object] = {
                "event": str(raw.get("event", "")),
                # Proje kontrollü stringler: redaksiyondan geçirilir.
                "host": redact_text(str(data.get("host", ""))) or None,
                "task": redact_text(str(data.get("task", ""))) or None,
                "changed": bool(result.get("changed", False)),
                "failed": bool(result.get("failed", False)),
                "skipped": bool(result.get("skipped", False)),
            }
            normalized.append({k: v for k, v in entry.items() if k in allowed})
    return normalized


def test_gate_d_normalized_result_prototype_contains_no_synthetic_secret(
    tmp_path: Path,
) -> None:
    """Normalize sonuçta YEDİ sentetik secret'ın HİÇBİRİ bulunmamalı.

    Ham artifact'ın secret taşıdığı her secret için AYRI AYRI doğrulanır
    (`any(...)` yeterli sayılmaz); ardından normalize sonucun hiçbirini
    taşımadığı doğrulanır. Yedincisi, görüntülenen task adının içine gömülmüş
    bir credential'dır ve redaksiyonun gerçekten çalıştığını kanıtlar.
    """
    marker = ps.new_marker("gated-normalize")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pdd = ps.make_private_data_dir(workspace)
    ps.write_project_file(pdd, "gate_d.yml", NORMALIZE_PLAYBOOK)

    result_path = workspace / "result.json"
    config_path = workspace / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "private_data_dir": str(pdd),
                "playbook": "gate_d.yml",
                "settings": {"job_timeout": 120, "suppress_ansible_output": True},
                "envvars": {"AOPS_PROBE_ENV_SECRET": NORMALIZE_SECRETS["env"]},
                "extravars": {
                    "probe_python": sys.executable,
                    "probe_marker": marker,
                    "probe_extravar_secret": NORMALIZE_SECRETS["extravar"],
                    "probe_taskarg_secret": NORMALIZE_SECRETS["taskarg"],
                    "probe_stdout_secret": NORMALIZE_SECRETS["stdout"],
                    "probe_nolog_secret": NORMALIZE_SECRETS["nolog"],
                    "probe_fail_secret": NORMALIZE_SECRETS["fail"],
                    "probe_taskname_secret": NORMALIZE_SECRETS["taskname"],
                    "probe_fail": True,
                    "probe_dwell": False,
                    "probe_leak_env": True,
                },
                "result_path": str(result_path),
            }
        ),
        encoding="utf-8",
    )

    env = ps.build_isolated_environment(workspace=workspace, venv_bin=ps.venv_bin_dir())
    try:
        subprocess.run(  # noqa: S603
            [sys.executable, str(Path(__file__).parent / "runner_child.py"), str(config_path)],
            env=env,
            close_fds=True,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
            cwd=str(workspace),
        )
    finally:
        ps.terminate_marker_processes(marker, grace=5.0)

    raw_hits = {
        label: [str(path.relative_to(pdd)) for path in ps.find_bytes_in_tree(pdd, value)]
        for label, value in NORMALIZE_SECRETS.items()
    }
    normalized = normalize_job_events(pdd)
    serialized = json.dumps(normalized, ensure_ascii=False)
    normalized_hits = {label: (value in serialized) for label, value in NORMALIZE_SECRETS.items()}

    print(
        "\nGATE-D MEASUREMENT [normalize-prototype] "
        + json.dumps(
            {
                "normalized_event_count": len(normalized),
                "normalized_fields": ["event", *NORMALIZED_EVENT_FIELDS],
                "raw_hit_counts": {k: len(v) for k, v in raw_hits.items()},
                "normalized_result_has_secret": normalized_hits,
                "redaction_marker_present": REDACTED in serialized,
            },
            indent=2,
        )
    )

    assert normalized != [], "Normalize sonuc bos; test bos sonucla gecmemeli"

    # 1. HER secret ham agacta GERCEKTEN bulunmali (aksi hâlde test vacuous olur).
    missing_from_raw = [label for label, hits in raw_hits.items() if not hits]
    assert missing_from_raw == [], (
        f"Ham artifact'ta bulunamayan sentetik secret'lar: {missing_from_raw}. "
        "Test bir sey kanitlamiyor; senaryo yeniden kurulmalidir."
    )

    # 2. Normalize sonucta HICBIRI olmamali.
    leaked = [label for label, hit in normalized_hits.items() if hit]
    assert leaked == [], f"Normalize sonuca sizan sentetik secret'lar: {leaked}"

    # 3. Redaksiyon GERCEKTEN calismis olmali: task adindaki credential
    #    maskelenmis olarak gorunmeli. Alanin sessizce dusurulmesiyle
    #    karistirilmamasi icin REDACTED isareti aranir.
    assert REDACTED in serialized, (
        "Redaksiyon isareti yok; task adindaki credential maskelenmemis ya da "
        "alan sessizce dusurulmus olabilir."
    )
