"""Inventory parser: subprocess sınırları ve normalizasyon (T-202).

Subprocess davranışları **gerçek süreçlerle** ölçülür; yalnızca Ansible'ın
INI/YAML çözümlemesi yerine denetlenebilir bir stub konur (bkz.
``tests/inventory_parser_stub.py``). Gerçek `ansible-inventory` ile çalışan
testler ``test_inventory_parser_real.py`` içindedir.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from app.services.inventories.parser import (
    ENABLED_INVENTORY_PLUGINS,
    MAX_STDERR_BYTES,
    InventoryParseFailedError,
    InventoryParserInvalidOutputError,
    InventoryParserOutputTooLargeError,
    InventoryParserUnavailableError,
    InventoryParseTimeoutError,
    ParserLimits,
    _collect_bounded_output,
    _sanitize_parser_output,
    build_command,
    build_environment,
    normalize_inventory,
    run_inventory_parser,
)
from app.services.security.redaction import REDACTED
from tests.support import make_settings, stub_parser_command

# `ansible-inventory --list` sözleşmesine göre elle hazırlanmış altın çıktı.
# Kaynak inventory (INI):
#
#     [web]
#     web01 ansible_host=10.0.0.10
#     web02 ansible_host=10.0.0.11
#     [db]
#     db01 ansible_host=10.0.0.20
#     [production:children]
#     web
#     db
INI_OUTPUT: dict[str, Any] = {
    "_meta": {
        "hostvars": {
            "web01": {"ansible_host": "10.0.0.10"},
            "web02": {"ansible_host": "10.0.0.11"},
            "db01": {"ansible_host": "10.0.0.20"},
        }
    },
    "all": {"children": ["ungrouped", "production"]},
    "production": {"children": ["web", "db"]},
    "web": {"hosts": ["web01", "web02"]},
    "db": {"hosts": ["db01"]},
}

# Kaynak inventory (YAML):
#
#     all:
#       children:
#         web:
#           hosts:
#             web01: {ansible_host: 10.0.0.10}
YAML_OUTPUT: dict[str, Any] = {
    "_meta": {"hostvars": {"web01": {"ansible_host": "10.0.0.10", "http_port": 8080}}},
    "all": {"children": ["ungrouped", "web"]},
    "web": {"hosts": ["web01"]},
}

# Sınır aşıldıktan sonra stub'ın açık kalacağı süre. Testler bundan **önce**
# bitmelidir; bitmiyorsa sınır gerçek zamanlı uygulanmıyor demektir.
HANG_SECONDS = 30

# Boyut testlerinde timeout bilinçli olarak cömerttir: hatayı üreten şeyin
# timeout değil **boyut sınırı** olduğu böyle kanıtlanır.
GENEROUS_TIMEOUT_SECONDS = 25.0


@pytest.fixture
def inventory_file(tmp_path: Path) -> Path:
    """Stub'ın varlığını doğruladığı gerçek bir inventory dosyası."""
    target = tmp_path / "hosts.ini"
    target.write_text("[web]\nweb01\n", encoding="utf-8")
    return target


def _payload_file(tmp_path: Path, payload: dict[str, Any]) -> Path:
    target = tmp_path / "payload.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _run(inventory_file: Path, command: list[str], **limit_kwargs: Any) -> str:
    return run_inventory_parser(
        inventory_file, command=command, limits=ParserLimits(**limit_kwargs)
    )


# --- Komut kurulumu: shell değil, argüman listesi ------------------------------


def test_command_is_an_argument_list(tmp_path: Path) -> None:
    """Komut bir liste olarak kurulur; hiçbir yerde string birleştirilmez."""
    arguments = build_command(["ansible-inventory"], tmp_path / "hosts.ini")

    assert isinstance(arguments, list)
    assert all(isinstance(item, str) for item in arguments)
    assert arguments == [
        "ansible-inventory",
        "--list",
        "--inventory",
        str(tmp_path / "hosts.ini"),
    ]


def test_subprocess_is_invoked_without_a_shell(
    inventory_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alt süreç bir liste ile ve `shell=False` başlatılır.

    Davranışsal testler (boşluklu path, echo-arguments) bunu dolaylı olarak da
    gösterir; bu test sözleşmeyi doğrudan ölçer.
    """
    captured: dict[str, Any] = {}
    real_popen = subprocess.Popen

    def _capture(args: Any, **kwargs: Any) -> Any:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return real_popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _capture)
    payload = _payload_file(tmp_path, INI_OUTPUT)

    _run(inventory_file, stub_parser_command("payload", payload=payload))

    assert isinstance(captured["args"], list)
    assert not isinstance(captured["args"], str)
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL


def test_output_is_never_written_to_disk(
    inventory_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Çıktı pipe'lardan okunur; geçici dosyaya yazılmaz.

    Dosyaya yazılsaydı boyut sınırı yalnızca bellek/cevap sınırı olurdu; disk
    sınırsız büyüyebilirdi.
    """
    captured: dict[str, Any] = {}
    real_popen = subprocess.Popen

    def _capture(args: Any, **kwargs: Any) -> Any:
        captured["kwargs"] = kwargs
        return real_popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _capture)
    payload = _payload_file(tmp_path, INI_OUTPUT)

    _run(inventory_file, stub_parser_command("payload", payload=payload))

    assert captured["kwargs"]["stdout"] is subprocess.PIPE
    assert captured["kwargs"]["stderr"] is subprocess.PIPE


def test_arguments_reach_the_process_unsplit(inventory_file: Path, tmp_path: Path) -> None:
    """Alt süreç, path'i **tek** argüman olarak alır.

    Shell string birleştirmesi yapılsaydı boşluk içeren yol parçalanırdı.
    """
    spaced_directory = tmp_path / "envanter dizini"
    spaced_directory.mkdir()
    spaced_inventory = spaced_directory / "hosts.ini"
    spaced_inventory.write_text("[web]\nweb01\n", encoding="utf-8")

    raw = _run(spaced_inventory, stub_parser_command("echo-arguments"))

    argv = json.loads(raw)["argv"]
    assert "--inventory" in argv
    assert argv[argv.index("--inventory") + 1] == str(spaced_inventory)


def test_inventory_with_space_in_path_is_parsed(
    tmp_path: Path,
) -> None:
    """Boşluklu yol davranışsal olarak da doğru çalışır."""
    spaced_directory = tmp_path / "envanter dizini"
    spaced_directory.mkdir()
    inventory = spaced_directory / "hosts.ini"
    inventory.write_text("[web]\nweb01\n", encoding="utf-8")
    payload = _payload_file(tmp_path, INI_OUTPUT)

    raw = _run(inventory, stub_parser_command("payload", payload=payload))

    assert json.loads(raw)["web"]["hosts"] == ["web01", "web02"]


# --- Environment daraltması ---------------------------------------------------


def test_environment_only_enables_static_inventory_plugins(tmp_path: Path) -> None:
    """`script` eklentisi kapalıdır: dinamik inventory çalıştırılamaz."""
    environment = build_environment(tmp_path)

    assert environment["ANSIBLE_INVENTORY_ENABLED"] == ENABLED_INVENTORY_PLUGINS
    assert "script" not in environment["ANSIBLE_INVENTORY_ENABLED"]
    assert environment["ANSIBLE_INVENTORY_UNPARSED_FAILED"] == "True"


def test_environment_pins_ansible_config_to_an_isolated_file(tmp_path: Path) -> None:
    """Kullanıcının ansible.cfg dosyaları okunmaz."""
    environment = build_environment(tmp_path)

    assert environment["ANSIBLE_CONFIG"] == str(tmp_path / "ansible.cfg")
    assert "HOME" not in environment
    assert "USERPROFILE" not in environment


def test_parent_environment_is_not_forwarded(
    inventory_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uygulama sürecindeki secret'lar alt sürece sızmaz."""
    monkeypatch.setenv("SECRET_MASTER_KEY", "cok-gizli")
    monkeypatch.setenv("ANSIBLE_INVENTORY_ENABLED", "script,ini,yaml")

    raw = _run(inventory_file, stub_parser_command("echo-environment"))

    environment = json.loads(raw)["env"]
    assert "SECRET_MASTER_KEY" not in environment
    assert environment["ANSIBLE_INVENTORY_ENABLED"] == ENABLED_INVENTORY_PLUGINS
    assert os.environ["ANSIBLE_INVENTORY_ENABLED"] == "script,ini,yaml"


# --- Arıza senaryoları --------------------------------------------------------


def test_missing_parser_produces_an_explainable_error(inventory_file: Path, tmp_path: Path) -> None:
    """`ansible-core` yoksa açıklanabilir bir 503 üretilir."""
    with pytest.raises(InventoryParserUnavailableError) as exc_info:
        _run(inventory_file, [str(tmp_path / "hic-olmayan-parser")])

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "inventory_parser_unavailable"
    assert "ansible-core" in exc_info.value.message


def test_timeout_stops_a_hanging_parser(inventory_file: Path) -> None:
    with pytest.raises(InventoryParseTimeoutError) as exc_info:
        _run(
            inventory_file,
            stub_parser_command("sleep", sleep_seconds=10),
            timeout_seconds=1.0,
        )

    assert exc_info.value.status_code == 504
    assert exc_info.value.code == "inventory_parse_timeout"


def test_oversized_output_is_rejected(inventory_file: Path) -> None:
    with pytest.raises(InventoryParserOutputTooLargeError) as exc_info:
        _run(
            inventory_file,
            stub_parser_command("huge", size_bytes=400_000),
            max_output_bytes=100_000,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "inventory_parse_output_too_large"


def test_oversized_stdout_stops_the_process_without_waiting_for_it_to_finish(
    inventory_file: Path,
) -> None:
    """Sınır **gerçek zamanlı** uygulanır: süreç doğal olarak bitmeden durdurulur.

    Stub sınırı aşacak kadar yazar, sonra uzun süre açık kalır. Sınır yalnızca
    süreç bittikten sonra ölçülseydi bu çağrı `HANG_SECONDS` kadar bekler ve
    timeout hatası üretirdi; boyut hatası değil.
    """
    started = time.monotonic()

    with pytest.raises(InventoryParserOutputTooLargeError) as exc_info:
        _run(
            inventory_file,
            stub_parser_command("huge-then-hang", size_bytes=400_000, sleep_seconds=HANG_SECONDS),
            max_output_bytes=100_000,
            timeout_seconds=GENEROUS_TIMEOUT_SECONDS,
        )

    elapsed = time.monotonic() - started
    assert exc_info.value.status_code == 502
    assert exc_info.value.details == {"stream": "stdout"}
    assert elapsed < HANG_SECONDS, (
        f"Süreç doğal bitişi beklendi ({elapsed:.1f}s); sınır gerçek zamanlı değil."
    )


def test_stderr_flood_stops_the_process_without_waiting(inventory_file: Path) -> None:
    """stderr de gerçek bir üst sınıra tabidir; yalnızca okurken kırpılmaz.

    Sınır sürece uygulanmasaydı akış diskte/bellekte sınırsız büyür ve çağrı
    stub'ın uykusunu beklerdi.
    """
    started = time.monotonic()

    with pytest.raises(InventoryParserOutputTooLargeError) as exc_info:
        _run(
            inventory_file,
            stub_parser_command(
                "stderr-flood-then-hang",
                size_bytes=4 * MAX_STDERR_BYTES,
                sleep_seconds=HANG_SECONDS,
            ),
            max_output_bytes=5_000_000,
            timeout_seconds=GENEROUS_TIMEOUT_SECONDS,
        )

    elapsed = time.monotonic() - started
    assert exc_info.value.details == {"stream": "stderr"}
    assert elapsed < HANG_SECONDS, (
        f"Süreç doğal bitişi beklendi ({elapsed:.1f}s); stderr sınırı gerçek zamanlı değil."
    )


def test_collected_output_never_exceeds_the_limit_in_memory(
    inventory_file: Path,
) -> None:
    """Toplanan metin sınırın üstüne çıkmaz; sınır aşıldığında okuma durur."""
    process = subprocess.Popen(
        stub_parser_command("huge", size_bytes=400_000)
        + ["--list", "--inventory", str(inventory_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    outcome = _collect_bounded_output(process, ParserLimits(max_output_bytes=100_000))

    assert outcome.oversized_stream == "stdout"
    assert len(outcome.stdout_text.encode("utf-8")) <= 100_000


def test_output_within_the_limit_is_accepted(inventory_file: Path) -> None:
    """Sınır, geçerli büyük çıktıları da reddetmemelidir."""
    raw = _run(
        inventory_file,
        stub_parser_command("huge", size_bytes=100_000),
        max_output_bytes=5_000_000,
    )

    assert json.loads(raw)["_meta"] == {"hostvars": {}}


def test_parser_failure_is_reported_as_a_content_error(inventory_file: Path) -> None:
    with pytest.raises(InventoryParseFailedError) as exc_info:
        _run(inventory_file, stub_parser_command("fail"))

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "inventory_parse_failed"


def test_crashing_parser_is_not_reported_as_a_content_error(
    inventory_file: Path,
) -> None:
    """Parser'ın kendisi çökerse kullanıcıya "dosyan bozuk" denmez.

    Bozuk kurulum, uyumsuz yorumlayıcı veya desteklenmeyen platform (Ansible
    Windows'ta control node değildir) altyapı sorunudur; kullanıcının inventory
    dosyasında bir kusur yoktur.
    """
    with pytest.raises(InventoryParserUnavailableError) as exc_info:
        _run(inventory_file, stub_parser_command("crash"))

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "inventory_parser_unavailable"


def test_crashing_parser_does_not_leak_a_traceback(inventory_file: Path) -> None:
    """Çağrı yığını, modül yolları ve iç hata metni kullanıcıya gösterilmez."""
    with pytest.raises(InventoryParserUnavailableError) as exc_info:
        _run(inventory_file, stub_parser_command("crash"))

    rendered = f"{exc_info.value.message} {exc_info.value.details}"
    for leak in ("Traceback", "runpy", "check_blocking_io", "AttributeError", "line 52"):
        assert leak not in rendered


def test_traceback_frames_are_stripped_from_sanitized_output() -> None:
    """Sanitizer, traceback çerçevelerini içerik hatasında da temizler."""
    raw = (
        "ERROR! Unable to parse /srv/hosts.ini\n"
        '  File "/opt/venv/lib/ansible/cli.py", line 52, in <module>\n'
        "    check_blocking_io()\n"
    )

    sanitized = _sanitize_parser_output(raw)

    assert 'File "' not in sanitized
    assert "line 52" not in sanitized
    assert "/srv/hosts.ini" not in sanitized
    assert "Unable to parse" in sanitized


def test_parser_failure_message_hides_paths_and_secrets(
    inventory_file: Path,
) -> None:
    """Hata metni anlaşılırdır ama yol ve secret sızdırmaz."""
    with pytest.raises(InventoryParseFailedError) as exc_info:
        _run(inventory_file, stub_parser_command("fail"))

    details = exc_info.value.details
    assert isinstance(details, dict)
    message = details["parser_message"]
    assert str(inventory_file) not in message
    assert str(inventory_file.parent) not in message
    assert "hunter2" not in message
    assert "<path>" in message
    assert "inventory source" in message


# --- Çıktı sözleşmesi ---------------------------------------------------------


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(InventoryParserInvalidOutputError) as exc_info:
        normalize_inventory("bu JSON degil", inventory_id=1)

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "inventory_parse_invalid_output"


def test_json_that_is_not_an_object_is_rejected() -> None:
    with pytest.raises(InventoryParserInvalidOutputError):
        normalize_inventory("[1, 2, 3]", inventory_id=1)


def test_invalid_json_from_a_real_process_is_rejected(inventory_file: Path) -> None:
    raw = _run(inventory_file, stub_parser_command("invalid-json"))

    with pytest.raises(InventoryParserInvalidOutputError):
        normalize_inventory(raw, inventory_id=1)


# --- Normalizasyon ------------------------------------------------------------


def test_ini_style_output_is_normalized() -> None:
    contents = normalize_inventory(json.dumps(INI_OUTPUT), inventory_id=7)

    assert contents.inventory_id == 7
    groups = {group.name: group.hosts for group in contents.groups}
    assert groups["web"] == ("web01", "web02")
    assert groups["db"] == ("db01",)
    # Üst gruplar alt gruplardan gelen host'ları kapsar.
    assert groups["production"] == ("db01", "web01", "web02")
    assert groups["all"] == ("db01", "web01", "web02")
    assert groups["ungrouped"] == ()


def test_host_group_membership_is_transitive() -> None:
    contents = normalize_inventory(json.dumps(INI_OUTPUT), inventory_id=1)

    hosts = {host.name: host.groups for host in contents.hosts}
    assert hosts["web01"] == ("all", "production", "web")
    assert hosts["db01"] == ("all", "db", "production")


def test_yaml_style_output_is_normalized() -> None:
    contents = normalize_inventory(json.dumps(YAML_OUTPUT), inventory_id=3)

    assert [group.name for group in contents.groups] == ["all", "ungrouped", "web"]
    assert [host.name for host in contents.hosts] == ["web01"]
    assert contents.hosts[0].variables == {"ansible_host": "10.0.0.10", "http_port": 8080}


def test_ordering_is_stable_regardless_of_input_order() -> None:
    """Aynı içerik farklı sırayla gelse de aynı cevap üretilir."""
    shuffled = {
        "web": {"hosts": ["web02", "web01"]},
        "db": {"hosts": ["db01"]},
        "production": {"children": ["db", "web"]},
        "all": {"children": ["production", "ungrouped"]},
        "_meta": {"hostvars": {"web02": {}, "db01": {}, "web01": {}}},
    }

    first = normalize_inventory(json.dumps(INI_OUTPUT), inventory_id=1)
    second = normalize_inventory(json.dumps(shuffled), inventory_id=1)

    assert [group.name for group in first.groups] == [group.name for group in second.groups]
    assert [group.hosts for group in first.groups] == [group.hosts for group in second.groups]
    assert [host.name for host in first.hosts] == [host.name for host in second.hosts]


def test_host_variables_are_redacted() -> None:
    payload = {
        "_meta": {
            "hostvars": {
                "web01": {
                    "ansible_host": "10.0.0.10",
                    "ansible_password": "hunter2",
                    "app": {"db": {"token": "abc"}, "port": 5432},
                }
            }
        },
        "all": {"children": ["web"]},
        "web": {"hosts": ["web01"]},
    }

    contents = normalize_inventory(json.dumps(payload), inventory_id=1)

    assert contents.hosts[0].variables == {
        "ansible_host": "10.0.0.10",
        "ansible_password": REDACTED,
        "app": {"db": {"token": REDACTED}, "port": 5432},
    }


def test_cyclic_children_do_not_cause_infinite_recursion() -> None:
    """Bozuk bir `children` döngüsü taramayı sonsuza götürmez."""
    payload = {
        "_meta": {"hostvars": {"web01": {}}},
        "a": {"children": ["b"], "hosts": ["web01"]},
        "b": {"children": ["a"]},
    }

    contents = normalize_inventory(json.dumps(payload), inventory_id=1)

    groups = {group.name: group.hosts for group in contents.groups}
    assert groups["a"] == ("web01",)
    assert groups["b"] == ("web01",)


def test_missing_meta_section_is_tolerated() -> None:
    payload = {"all": {"children": ["web"]}, "web": {"hosts": ["web01"]}}

    contents = normalize_inventory(json.dumps(payload), inventory_id=1)

    assert [host.name for host in contents.hosts] == ["web01"]
    assert contents.hosts[0].variables == {}


def test_unexpected_entry_types_are_ignored_not_fatal() -> None:
    """Tek bir bozuk girdi bütün cevabı düşürmez."""
    payload = {
        "_meta": {"hostvars": {"web01": {"ansible_host": "10.0.0.10"}, "bozuk": "dict degil"}},
        "web": {"hosts": ["web01", 42, None]},
        "kirik": "dict degil",
    }

    contents = normalize_inventory(json.dumps(payload), inventory_id=1)

    groups = {group.name: group.hosts for group in contents.groups}
    assert groups["web"] == ("42", "web01")
    assert groups["kirik"] == ()


def test_parser_limits_are_read_from_settings() -> None:
    settings = make_settings(
        inventory_parse_timeout_seconds=5.0, inventory_parse_max_output_bytes=1234
    )

    limits = ParserLimits.from_settings(settings)

    assert limits.timeout_seconds == 5.0
    assert limits.max_output_bytes == 1234


def test_stub_parser_is_a_real_process() -> None:
    """Stub gerçekten ayrı bir süreçtir; testler subprocess katmanını atlamaz."""
    assert Path(stub_parser_command("payload")[1]).is_file()
    assert stub_parser_command("payload")[0] == sys.executable
