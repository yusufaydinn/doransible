"""`ansible-inventory` yerine geçen, davranışı denetlenebilir sahte parser.

Neden gerekli: gerçek `ansible-core` bir **control node** aracıdır ve Windows'ta
çalışmaz (`grp`, `os.get_blocking` gibi POSIX'e özgü modüllere bağlıdır). Ayrıca
timeout, çıktı boyutu, geçersiz JSON ve "parser yok" gibi arıza senaryoları
gerçek parser'la deterministik biçimde üretilemez.

Bu stub **subprocess katmanını atlamaz**: gerçek bir işletim sistemi süreci
olarak çalışır, gerçek argümanları alır, gerçek stdout/stderr üretir. Yani
timeout, boyut sınırı, argüman listesi ve environment daraltması gerçekten
ölçülür; yalnızca INI/YAML söz dizimini çözen Ansible kodu taklit edilir.

Gerçek parser ile çalışan testler `test_inventory_parser_real.py` içindedir ve
`ansible-inventory` kullanılamayan platformlarda açıkça atlanır.

Kullanım::

    command = [sys.executable, str(STUB), "--behaviour", "payload",
               "--payload", str(json_dosyasi)]

Servis bu listeye ``--list --inventory <path>`` ekler.
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
    parser.add_argument("--payload")
    parser.add_argument("--size-bytes", type=int, default=0)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    # Servisin eklediği gerçek argümanlar.
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--inventory")
    parser.add_argument("--limit")
    options = parser.parse_args()

    behaviour = options.behaviour

    if behaviour == "payload":
        # Verilen JSON'u aynen basar; inventory dosyasının varlığı da doğrulanır.
        if not options.inventory or not Path(options.inventory).is_file():
            sys.stderr.write(f"[stub] inventory bulunamadi: {options.inventory}\n")
            return 1
        sys.stdout.write(Path(options.payload or "").read_text(encoding="utf-8"))
        return 0

    if behaviour == "echo-arguments":
        # Alınan argümanları JSON olarak basar: shell birleştirmesi yapılsaydı
        # argümanlar farklı parçalanırdı.
        sys.stdout.write(json.dumps({"argv": sys.argv[1:]}))
        return 0

    if behaviour == "echo-environment":
        sys.stdout.write(json.dumps({"env": dict(_ansible_environment())}))
        return 0

    if behaviour == "sleep":
        time.sleep(options.sleep_seconds)
        sys.stdout.write("{}")
        return 0

    if behaviour == "huge":
        sys.stdout.write('{"_meta": {"hostvars": {}}, "padding": "')
        chunk = "x" * 65536
        written = 0
        while written < options.size_bytes:
            sys.stdout.write(chunk)
            written += len(chunk)
        sys.stdout.write('"}')
        return 0

    if behaviour == "huge-then-hang":
        # Sınırı aşacak kadar yazar, akışı boşaltır ve **açık kalır**.
        # Sınır gerçek zamanlı uygulanmıyorsa çağıran taraf bu uykuyu bekler.
        _flood(sys.stdout, options.size_bytes)
        time.sleep(options.sleep_seconds)
        return 0

    if behaviour == "stderr-flood-then-hang":
        _flood(sys.stderr, options.size_bytes)
        time.sleep(options.sleep_seconds)
        return 0

    if behaviour == "invalid-json":
        sys.stdout.write("bu JSON degil <<<")
        return 0

    if behaviour == "json-array":
        sys.stdout.write("[1, 2, 3]")
        return 0

    if behaviour == "crash-on-limit":
        # Phase 1 (limitsiz) başarılı, Phase 1b (--limit) çöker. T-204A'da
        # limit çözümlemesinin **limit hatası** olarak sınıflandırıldığını,
        # "parser çöktü" (503) olarak değil, ölçmeyi sağlar.
        if options.limit is None:
            sys.stdout.write(
                json.dumps(
                    {
                        "_meta": {"hostvars": {"web01": {"ansible_host": "10.0.0.10"}}},
                        "all": {"children": ["web"]},
                        "web": {"hosts": ["web01"]},
                    }
                )
            )
            return 0
        sys.stderr.write(
            "Traceback (most recent call last):\n"
            '  File "/opt/venv/lib/ansible/cli/inventory.py", line 1, in <module>\n'
            "IndexError: string index out of range\n"
        )
        return 250

    if behaviour == "crash":
        # Parser'ın kendisinin çöktüğü durum: gerçek `ansible-inventory`
        # Windows'ta tam olarak böyle davranır (control node desteklenmez).
        sys.stderr.write(
            "Traceback (most recent call last):\n"
            '  File "<frozen runpy>", line 198, in _run_module_as_main\n'
            '  File "/opt/venv/lib/ansible/cli/__init__.py", line 52, in <module>\n'
            "    check_blocking_io()\n"
            "    ^^^^^^^^^^^^^^^^^^\n"
            "AttributeError: module 'os' has no attribute 'get_blocking'\n"
        )
        return 1

    if behaviour == "fail":
        sys.stderr.write(
            "[WARNING]: Unable to parse "
            f"{options.inventory} as an inventory source\n"
            "ERROR! ansible_password=hunter2 gizli deger\n"
        )
        return 1

    sys.stderr.write(f"[stub] bilinmeyen davranis: {behaviour}\n")
    return 2


def _flood(stream: object, size_bytes: int) -> None:
    """Verilen akışa en az ``size_bytes`` kadar veri yazar ve boşaltır.

    Yazma, çağıran taraf okumayı bıraktığında bloke olabilir; bu beklenen
    davranıştır. Süreç sonlandırıldığında yazma hatası yutulur.
    """
    chunk = "x" * 65536
    written = 0
    try:
        while written < size_bytes:
            stream.write(chunk)  # type: ignore[attr-defined]
            stream.flush()  # type: ignore[attr-defined]
            written += len(chunk)
    except (OSError, ValueError):
        pass


def _ansible_environment() -> dict[str, str]:
    """Yalnızca ANSIBLE_/HOME ile ilgili değişkenleri döndürür."""
    import os

    interesting = ("ANSIBLE_", "HOME", "USERPROFILE", "AWS_", "SECRET", "TOKEN")
    return {
        name: value
        for name, value in os.environ.items()
        if any(name.startswith(prefix) or name == prefix for prefix in interesting)
    }


if __name__ == "__main__":
    sys.exit(main())
