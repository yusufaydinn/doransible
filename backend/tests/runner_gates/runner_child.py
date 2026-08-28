"""Runner'ı ayrı bir child process'te çalıştıran probe helper'ı.

PRODUCTION KODU DEĞİLDİR (bkz. paket docstring'i).

Neden ayrı bir process: `ansible_runner.run()` Python API'si, kendisini çağıran
sürecin environment'ını miras alır ve `env/envvars` bu environment'ın ÜZERİNE
eklenir. API prosesi içinde thread'de çağrılırsa API'nin bütün environment'ı
(master key, DATABASE_URL, proxy, SSH_AUTH_SOCK, HOME) işe sızar. Bu yüzden
Runner, environment'ı allowlist ile sıfırdan kurulmuş ayrı bir child process'te
çalıştırılır (ADR-021 Kapı A).

Kullanım:
    python runner_child.py <config.json>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _observed_environment() -> dict[str, str]:
    """Bu child'ın GERÇEKTEN gördüğü environment."""
    return dict(os.environ)


def _observed_descriptors() -> dict[str, str]:
    """Bu child'a açık gelen file descriptor'ların hedefleri.

    Sentinel descriptor sızıntısını ölçmek için kullanılır.
    """
    targets: dict[str, str] = {}
    fd_dir = Path("/proc/self/fd")
    try:
        entries = sorted(fd_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return targets
    for entry in entries:
        try:
            targets[entry.name] = os.readlink(entry)
        except OSError:
            continue
    return targets


def _process_identity() -> dict[str, int]:
    """Child'ın process group / session kimliği."""
    return {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "pgid": os.getpgid(0),
        "sid": os.getsid(0),
    }


def main() -> int:
    config: dict[str, Any] = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

    # Child umask'i BAŞKA HİÇBİR ŞEYDEN ÖNCE daraltılır. Runner ve
    # `ansible-playbook` bu umask'i miras alır; ölçümde Runner'ın kendi
    # oluşturduğu dizinlere kısıtlayıcı mod UYGULAMADIĞI, umask'e tabi olduğu
    # görülmüştür (ADR-021 Kapı D).
    os.umask(config.get("umask", 0o077))

    report_path = config.get("self_report_path")
    if report_path:
        Path(report_path).write_text(
            json.dumps(
                {
                    "environment": _observed_environment(),
                    "descriptors": _observed_descriptors(),
                    "identity": _process_identity(),
                    "stdin_closed": _stdin_is_devnull(),
                    "umask": _current_umask(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    import ansible_runner

    run_kwargs: dict[str, Any] = {
        "private_data_dir": config["private_data_dir"],
        "playbook": config["playbook"],
        "inventory": config.get("inventory", "hosts.ini"),
        "settings": config.get("settings", {}),
        "quiet": True,
    }
    if config.get("envvars"):
        run_kwargs["envvars"] = config["envvars"]
    if config.get("extravars"):
        run_kwargs["extravars"] = config["extravars"]
    if config.get("cmdline"):
        # `--check` gibi ek `ansible-playbook` argümanları. Kapı B'nin
        # check-mode matrisi bunu kullanır.
        run_kwargs["cmdline"] = config["cmdline"]

    result = ansible_runner.run(**run_kwargs)

    status = str(result.status)
    rc = int(result.rc) if result.rc is not None else -1
    Path(config["result_path"]).write_text(
        json.dumps({"status": status, "rc": rc}),
        encoding="utf-8",
    )
    return 0


def _current_umask() -> str:
    """Yürürlükteki umask'i okur ve geri koyar."""
    current = os.umask(0o077)
    os.umask(current)
    return oct(current)


def _stdin_is_devnull() -> bool:
    """stdin'in /dev/null'a bağlı olup olmadığını ölçer."""
    try:
        return os.readlink("/proc/self/fd/0") == "/dev/null"
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())
