"""`ansible` ad-hoc komutu yerine geçen, davranışı denetlenebilir sahte süreç.

Neden gerekli: gerçek bir ping'in *reachable* dönmesi çalışan bir SSH hedefi
ister; timeout, çıktı sınırı ve bozuk çıktı gibi arıza yolları ise gerçek
Ansible ile deterministik biçimde üretilemez.

Bu stub **subprocess katmanını atlamaz**: gerçek bir işletim sistemi süreci
olarak çalışır, servisin ürettiği gerçek argümanları alır, snapshot'ı gerçekten
okur ve gerçek stdout/stderr üretir. Yani argüman aktarımı, environment
daraltması, timeout ve boyut sınırı gerçekten ölçülür; yalnız SSH bağlantısı
taklit edilir.

Gerçek `ansible` ile çalışan kapalı-port testi `test_ping_confirm_real.py`
içindedir ve atlanmaz.

Kullanım::

    command = [sys.executable, str(STUB), "--behaviour", "success"]

Servis bu listeye ``all -i <snapshot> -m ping --forks N -T N`` ekler.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main() -> int:
    """Stub'ı çalıştırır ve seçilen davranışı üretir."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--behaviour", required=True)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--size-bytes", type=int, default=0)
    # Servisin eklediği gerçek argümanlar.
    parser.add_argument("-i", dest="inventory")
    parser.add_argument("-m", dest="module")
    parser.add_argument("-T", dest="connect_timeout")
    parser.add_argument("--forks")
    options, extra = parser.parse_known_args()

    behaviour = options.behaviour
    hosts = _hosts(options.inventory)

    if behaviour == "echo-arguments":
        sys.stdout.write(json.dumps({"argv": sys.argv[1:], "extra": extra}))
        return 0

    if behaviour == "success":
        for host in hosts:
            sys.stdout.write(f'{host} | SUCCESS => {{\n  "ping": "pong"\n}}\n')
        return 0

    if behaviour == "unreachable":
        for host in hosts:
            sys.stdout.write(
                f'{host} | UNREACHABLE! => {{"changed": false, '
                f'"msg": "Failed to connect to the host via ssh", "unreachable": true}}\n'
            )
        return 4

    if behaviour == "mixed":
        for index, host in enumerate(hosts):
            if index % 2 == 0:
                sys.stdout.write(f'{host} | SUCCESS => {{"ping": "pong"}}\n')
            else:
                sys.stdout.write(f'{host} | FAILED! => {{"msg": "module failure"}}\n')
        return 2

    if behaviour == "partial":
        # Yalnız ilk host için sonuç üretir: kalanlar `no_result` olmalıdır.
        if hosts:
            sys.stdout.write(f'{hosts[0]} | SUCCESS => {{"ping": "pong"}}\n')
        return 0

    if behaviour == "leaky":
        # Mesajda secret ve mutlak yol taşır; redaction ölçülür.
        for host in hosts:
            sys.stdout.write(
                f'{host} | UNREACHABLE! => {{"msg": "password=hunter2 '
                f'/root/.ssh/id_rsa okunamadi"}}\n'
            )
        return 4

    if behaviour == "echo-destination":
        # Gerçek OpenSSH hatası hedefi metne koyar; maskeleme burada ölçülür.
        variables = _host_variables(options.inventory)
        for host in hosts:
            address = variables.get(host, {}).get("ansible_host", host)
            port = variables.get(host, {}).get("ansible_port", 22)
            sys.stdout.write(
                f'{host} | UNREACHABLE! => {{"msg": "ssh: connect to host '
                f'{address} port {port}: Connection refused"}}\n'
            )
        return 4

    if behaviour == "ansible-2-19-mixed":
        # Ansible-core 2.19.11 ile ölçülen gerçek ad-hoc ping çıktı biçimi
        # (bkz. `test_ping_execution.py`): bilinen host'lardan sonuncusu
        # UNREACHABLE olur ve hemen önünde 2.19'un kanonik
        # [ERROR]/Origin/dict tanı bloğu basılır.
        variables = _host_variables(options.inventory)
        unreachable_host = hosts[-1] if hosts else None
        for host in hosts:
            if host == unreachable_host:
                address = variables.get(host, {}).get("ansible_host", host)
                port = variables.get(host, {}).get("ansible_port", 22)
                msg = (
                    "Task failed: Failed to connect to the host via ssh: "
                    f"ssh: connect to host {address} port {port}: Connection refused"
                )
                sys.stdout.write(
                    f"[ERROR]: {msg}\n"
                    "Origin: <adhoc 'ping' task>\n"
                    "\n"
                    "{'action': 'ping', 'args': {}, 'timeout': 0, 'async_val': 0, "
                    "'poll': 15}\n"
                    "\n"
                    f'{host} | UNREACHABLE! => {{"changed": false, "msg": "{msg}", '
                    '"unreachable": true}\n'
                )
            else:
                sys.stdout.write(f'{host} | SUCCESS => {{"ping": "pong"}}\n')
        return 4

    if behaviour == "garbage":
        for host in hosts:
            sys.stdout.write(f'{host} | SUCCESS => {{"ping": "pong"}}\n')
        sys.stdout.write("bu bir sonuc blogu degil\n")
        return 0

    if behaviour == "silent-failure":
        sys.stderr.write("ERROR! bir seyler ters gitti\n")
        return 2

    if behaviour == "sleep":
        time.sleep(options.sleep_seconds)
        for host in hosts:
            sys.stdout.write(f'{host} | SUCCESS => {{"ping": "pong"}}\n')
        return 0

    if behaviour == "flood":
        chunk = "x" * 65536
        written = 0
        try:
            while written < options.size_bytes:
                sys.stdout.write(chunk)
                sys.stdout.flush()
                written += len(chunk)
        except (OSError, ValueError):
            return 0
        time.sleep(options.sleep_seconds)
        return 0

    sys.stderr.write(f"[stub] bilinmeyen davranis: {behaviour}\n")
    return 2


def _host_variables(snapshot: str | None) -> dict[str, dict[str, object]]:
    """Snapshot'taki bağlantı değişkenlerini okur."""
    if not snapshot:
        return {}
    try:
        document = json.loads(Path(snapshot).read_text(encoding="utf-8"))
        return dict(document["all"]["hosts"])
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _hosts(snapshot: str | None) -> list[str]:
    """Snapshot'taki hedef host adlarını okur.

    Stub'ın hedefleri uydurmaması bilinçlidir: parser yalnızca **beklenen**
    host kümesini kabul eder ve testin bunu doğrulaması gerekir.
    """
    if not snapshot:
        return []
    try:
        document = json.loads(Path(snapshot).read_text(encoding="utf-8"))
        return sorted(document["all"]["hosts"])
    except (OSError, ValueError, KeyError, TypeError):
        return []


if __name__ == "__main__":
    sys.exit(main())
