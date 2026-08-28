"""`ansible-runner` CLI'si yerine geçen, davranışı denetlenebilir sahte süreç.

Neden gerekli: timeout, stdout sınırı, raw bütçesi ve process-group
sonlandırma yolları gerçek Ansible ile deterministik biçimde üretilemez.

Bu stub **subprocess katmanını atlamaz**: gerçek bir işletim sistemi süreci
olarak çalışır, servisin ürettiği gerçek argv'yi alır, gerçek environment'ı
görür, gerçek stdout üretir ve gerçekten ``--artifact-dir`` altına yazar. Yani
argüman aktarımı, environment daraltması, timeout, çıktı sınırı ve raw bütçesi
gerçekten ölçülür; yalnız Ansible'ın kendisi taklit edilir.

Gerçek `ansible-runner` 2.4.3 ile çalışan localhost testi
``test_runner_process.py`` içindedir ve atlanmaz.

Kullanım::

    command = [sys.executable, str(STUB), "--behaviour", "success"]

Servis bu listeye ``run <pdd> --project-dir ... -p <playbook>
--cmdline=--check`` ekler.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _parse() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--behaviour", required=True)
    parser.add_argument("--report", default=None)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--size-bytes", type=int, default=0)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--leak-text", default="")
    # Servisin eklediği gerçek argümanlar.
    parser.add_argument("--artifact-dir", dest="artifact_dir")
    parser.add_argument("--ident", dest="ident")
    parser.add_argument("--project-dir", dest="project_dir")
    parser.add_argument("--inventory", dest="inventory")
    parser.add_argument("-p", dest="playbook")
    return parser.parse_known_args()


def _write_report(options: argparse.Namespace) -> None:
    """Child'ın **gerçekten** gördüğü argv, environment ve cwd'yi kaydeder."""
    if not options.report:
        return
    Path(options.report).write_text(
        json.dumps(
            {
                "argv": sys.argv[1:],
                "environment": dict(os.environ),
                "cwd": os.getcwd(),
                "pid": os.getpid(),
                "pgid": os.getpgid(0),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _raw_dir(options: argparse.Namespace) -> Path:
    """`ansible-runner` ile aynı yer: ``<artifact-dir>/<ident>``."""
    root = Path(options.artifact_dir)
    return root / options.ident if options.ident else root


def _emit(event: str, **event_data: object) -> None:
    sys.stdout.write(json.dumps({"event": event, "event_data": event_data}) + "\n")
    sys.stdout.flush()


def main() -> int:  # noqa: C901 - davranış tablosu; her dal tek satırlık
    options, _extra = _parse()
    _write_report(options)
    behaviour = options.behaviour

    if behaviour == "report-only":
        return 0

    if behaviour == "success":
        _emit("playbook_on_task_start", task="probe task")
        _emit("runner_on_ok", host="probehost", task="probe task", res={"changed": False})
        _emit("playbook_on_stats", ok={"probehost": 1}, processed={"probehost": 1})
        return 0

    if behaviour == "sleep":
        # Uzun uykuyu bir torun süreç de paylaşır: process-group sonlandırma
        # yalnız leader'ı değil bütün ağacı kapatmalıdır.
        child = subprocess.Popen(  # noqa: S603 - argüman listesi, shell=False
            [sys.executable, "-c", f"import time; time.sleep({options.sleep_seconds})"],
        )
        try:
            time.sleep(options.sleep_seconds)
        finally:
            child.kill()
        return 0

    if behaviour == "flood-stdout":
        chunk = "x" * 65_536
        written = 0
        while written < options.size_bytes:
            sys.stdout.write(chunk)
            sys.stdout.flush()
            written += len(chunk)
        # Sınır aşıldıktan sonra da yaşamaya devam eder: süreci kesen şey
        # sürecin kendi bitişi değil, sınırın uygulanması olmalıdır.
        time.sleep(options.sleep_seconds)
        return 0

    if behaviour == "flood-raw":
        raw = _raw_dir(options)
        raw.mkdir(parents=True, exist_ok=True)
        index = 0
        deadline = time.monotonic() + options.sleep_seconds
        while time.monotonic() < deadline:
            (raw / f"blob-{index}").write_bytes(b"x" * options.size_bytes)
            index += 1
            time.sleep(0.01)
        return 0

    if behaviour == "unreadable-raw":
        # Ölçülemeyen bir alt ağaç: bütçe gözlemcisi bunu sınır ihlali saymalı.
        raw = _raw_dir(options)
        locked = raw / "locked"
        locked.mkdir(parents=True, exist_ok=True)
        (locked / "artifact").write_bytes(b"x" * 1024)
        locked.chmod(0o000)
        time.sleep(options.sleep_seconds)
        return 0

    if behaviour == "churn-raw":
        # Yazılıp hemen silinen girdiler: ölçüm sırasında kaybolan bir girdi
        # gerçek bir yarıştır ve tolere edilmelidir.
        raw = _raw_dir(options)
        raw.mkdir(parents=True, exist_ok=True)
        index = 0
        deadline = time.monotonic() + options.sleep_seconds
        while time.monotonic() < deadline:
            blob = raw / f"gecici-{index}"
            blob.write_bytes(b"x" * options.size_bytes)
            blob.unlink()
            index += 1
        _emit("playbook_on_stats", ok={"probehost": 1}, processed={"probehost": 1})
        return 0

    if behaviour == "write-raw":
        raw = _raw_dir(options)
        (raw / "nested" / "deeper").mkdir(parents=True, exist_ok=True)
        (raw / "artifact").write_bytes(b"x" * options.size_bytes)
        (raw / "nested" / "deeper" / "artifact").write_bytes(b"x" * options.size_bytes)
        _emit("playbook_on_stats", ok={"probehost": 1}, processed={"probehost": 1})
        return int(options.exit_code)

    if behaviour == "leak":
        # Ansible'ın gerçekten yaptığı şey: bağlantı değerini task adına ve
        # hata metnine geri yazar. Sızıntı testleri bunu taklit etmek yerine
        # **üretir**; maskeleme ancak gerçekten sızdıran bir çıktıda ölçülebilir.
        leak = options.leak_text
        sys.stderr.write(f"fatal: [{leak}]: UNREACHABLE! => {leak}\n")
        sys.stderr.flush()
        _emit("playbook_on_task_start", task=f"connect as {leak}")
        _emit("runner_on_ok", host="probehost", task=f"connect as {leak}", res={"changed": False})
        _emit("playbook_on_stats", ok={"probehost": 1}, processed={"probehost": 1})
        return 0

    if behaviour == "invalid-json":
        sys.stdout.write("bu bir JSON nesnesi degil\n")
        return 0

    if behaviour == "no-terminal-event":
        _emit("playbook_on_task_start", task="probe task")
        return int(options.exit_code)

    raise SystemExit(f"bilinmeyen davranis: {behaviour}")


if __name__ == "__main__":
    sys.exit(main())
