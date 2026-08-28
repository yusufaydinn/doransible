"""Sabit ping argv'si, output parser ve gerçek unreachable testi."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.ansible.ping_execution import (
    PingInvalidOutputError,
    build_ping_command,
    parse_ping_output,
    run_ping_process,
)
from app.services.ansible.ssh import build_ssh_arguments, prepare_known_hosts
from tests.support import real_parser_available


def test_command_is_fixed_and_has_no_limit(tmp_path: Path) -> None:
    command = build_ping_command(["ansible"], tmp_path / "snapshot.yml", forks=7, connect_timeout=4)
    assert command == [
        "ansible",
        "all",
        "-i",
        str(tmp_path / "snapshot.yml"),
        "-m",
        "ping",
        "--forks",
        "7",
        "-T",
        "4",
    ]
    assert "--limit" not in command


def test_parser_normalizes_all_states_and_no_result() -> None:
    stdout = """\
web01 | SUCCESS => {
  "ping": "pong"
}
web02 | UNREACHABLE! => {"msg": "connection refused"}
web03 | FAILED! => {"msg": "token=secret /root/key"}
"""
    results = parse_ping_output(stdout, ["web01", "web02", "web03", "web04"])
    assert [item.status for item in results] == ["reachable", "unreachable", "failed", "no_result"]
    assert "secret" not in results[2].message
    assert "/root" not in results[2].message


@pytest.mark.parametrize(
    "stdout",
    [
        'other | SUCCESS => {"ping":"pong"}',
        'web01 | SUCCESS => {"ping":"pong"}\nweb01 | SUCCESS => {"ping":"pong"}',
        "web01 | SUCCESS => {bad}",
    ],
)
def test_parser_rejects_unknown_duplicate_or_bad_json(stdout: str) -> None:
    with pytest.raises(PingInvalidOutputError):
        parse_ping_output(stdout, ["web01"])


# --- Ansible-core 2.19 ad-hoc tanı bloğu ---------------------------------------
#
# Aşağıdaki fixture, gerçek `ansible-core 2.19.11` ile ölçülmüş çıktının
# güvenli/redakte edilmiş bir örneğidir (adres RFC 5737 belgeleme bloğundan,
# `198.51.100.6`). Canlı bulguda beş host'tan dördü erişilebilir, biri
# (`ubuntu-demo-6`) kapalıdır; ad-hoc ping rc=4 ile döner ve dört `SUCCESS`
# bloğundan sonra bu tanı bloğu, ardından tek `UNREACHABLE!` bloğu gelir.

_DIAGNOSTIC_HOSTS = (
    "ubuntu-demo-2",
    "ubuntu-demo-3",
    "ubuntu-demo-4",
    "ubuntu-demo-5",
    "ubuntu-demo-6",
)

_ANSIBLE_219_MIXED_STDOUT = """\
ubuntu-demo-2 | SUCCESS => {
    "ansible_facts": {
        "discovered_interpreter_python": "/usr/bin/python3.12"
    },
    "changed": false,
    "ping": "pong"
}
ubuntu-demo-4 | SUCCESS => {
    "ansible_facts": {
        "discovered_interpreter_python": "/usr/bin/python3.12"
    },
    "changed": false,
    "ping": "pong"
}
ubuntu-demo-3 | SUCCESS => {
    "ansible_facts": {
        "discovered_interpreter_python": "/usr/bin/python3.12"
    },
    "changed": false,
    "ping": "pong"
}
ubuntu-demo-5 | SUCCESS => {
    "ansible_facts": {
        "discovered_interpreter_python": "/usr/bin/python3.12"
    },
    "changed": false,
    "ping": "pong"
}
[ERROR]: Task failed: ssh: connect to host 198.51.100.6 port 22: Connection refused
Origin: <adhoc 'ping' task>

{'action': 'ping', 'args': {}, 'timeout': 0, 'async_val': 0, 'poll': 15}

ubuntu-demo-6 | UNREACHABLE! => {
    "changed": false,
    "msg": "ssh: connect to host 198.51.100.6 port 22: Connection refused",
    "unreachable": true
}
"""


def test_parser_accepts_ansible_219_diagnostic_between_host_blocks() -> None:
    """rc=4 + 4×SUCCESS + tanı bloğu + 1×UNREACHABLE hâlâ normal bir sonuçtur."""
    results = parse_ping_output(_ANSIBLE_219_MIXED_STDOUT, _DIAGNOSTIC_HOSTS)

    by_host = {item.host: item for item in results}
    assert {item.status for host, item in by_host.items() if host != "ubuntu-demo-6"} == {
        "reachable"
    }
    assert by_host["ubuntu-demo-6"].status == "unreachable"
    assert len(results) == 5
    statuses = [item.status for item in results]
    assert statuses.count("reachable") == 4
    assert statuses.count("unreachable") == 1
    assert statuses.count("failed") == 0
    assert statuses.count("no_result") == 0
    # Tanı bloğu bir sonuç değildir: ne ayrı bir host, ne de bir mesaj olarak
    # geri dönmez.
    assert "[ERROR]" not in by_host["ubuntu-demo-6"].message
    assert "Origin" not in by_host["ubuntu-demo-6"].message


def test_parser_accepts_two_consecutive_diagnostic_blocks() -> None:
    """İki ardışık UNREACHABLE host, her biri kendi tanı bloğuyla kabul edilir."""
    stdout = """\
h1 | SUCCESS => {"ping": "pong"}
[ERROR]: Task failed: ssh: connect to host 198.51.100.5 port 22: Connection refused
Origin: <adhoc 'ping' task>

{'action': 'ping', 'args': {}, 'timeout': 0, 'async_val': 0, 'poll': 15}

h2 | UNREACHABLE! => {"changed": false, "msg": "connection refused", "unreachable": true}
[ERROR]: Task failed: ssh: connect to host 198.51.100.6 port 22: Connection refused
Origin: <adhoc 'ping' task>

{'action': 'ping', 'args': {}, 'timeout': 0, 'async_val': 0, 'poll': 15}

h3 | UNREACHABLE! => {"changed": false, "msg": "connection refused", "unreachable": true}
"""
    results = parse_ping_output(stdout, ["h1", "h2", "h3"])
    assert [item.status for item in results] == ["reachable", "unreachable", "unreachable"]


@pytest.mark.parametrize(
    "corruption",
    [
        "wrong-origin",
        "no-blank-line",
        "not-followed-by-unreachable",
        "dict-wrong-action",
        "dict-extra-key",
        "dict-nonempty-args",
        "dict-not-a-literal",
        "truncated",
    ],
)
def test_parser_rejects_malformed_diagnostic_block(corruption: str) -> None:
    """Yalnız kanonik tanı yapısı kabul edilir; sapan her varyant reddedilir."""
    lines = {
        "wrong-origin": (
            'h1 | SUCCESS => {"ping": "pong"}\n'
            "[ERROR]: Task failed: connection refused\n"
            "Origin: <playbook task>\n"
            "\n"
            "{'action': 'ping', 'args': {}, 'timeout': 0, 'async_val': 0, 'poll': 15}\n"
            "\n"
            'h2 | UNREACHABLE! => {"msg": "connection refused"}\n'
        ),
        "no-blank-line": (
            'h1 | SUCCESS => {"ping": "pong"}\n'
            "[ERROR]: Task failed: connection refused\n"
            "Origin: <adhoc 'ping' task>\n"
            "{'action': 'ping', 'args': {}, 'timeout': 0, 'async_val': 0, 'poll': 15}\n"
            "\n"
            'h2 | UNREACHABLE! => {"msg": "connection refused"}\n'
        ),
        "not-followed-by-unreachable": (
            'h1 | SUCCESS => {"ping": "pong"}\n'
            "[ERROR]: Task failed: connection refused\n"
            "Origin: <adhoc 'ping' task>\n"
            "\n"
            "{'action': 'ping', 'args': {}, 'timeout': 0, 'async_val': 0, 'poll': 15}\n"
            "\n"
            'h2 | SUCCESS => {"ping": "pong"}\n'
        ),
        "dict-wrong-action": (
            'h1 | SUCCESS => {"ping": "pong"}\n'
            "[ERROR]: Task failed: connection refused\n"
            "Origin: <adhoc 'ping' task>\n"
            "\n"
            "{'action': 'shell', 'args': {}, 'timeout': 0, 'async_val': 0, 'poll': 15}\n"
            "\n"
            'h2 | UNREACHABLE! => {"msg": "connection refused"}\n'
        ),
        "dict-extra-key": (
            'h1 | SUCCESS => {"ping": "pong"}\n'
            "[ERROR]: Task failed: connection refused\n"
            "Origin: <adhoc 'ping' task>\n"
            "\n"
            "{'action': 'ping', 'args': {}, 'timeout': 0, 'async_val': 0, "
            "'poll': 15, 'evil': 'payload'}\n"
            "\n"
            'h2 | UNREACHABLE! => {"msg": "connection refused"}\n'
        ),
        "dict-nonempty-args": (
            'h1 | SUCCESS => {"ping": "pong"}\n'
            "[ERROR]: Task failed: connection refused\n"
            "Origin: <adhoc 'ping' task>\n"
            "\n"
            "{'action': 'ping', 'args': {'data': 'x'}, 'timeout': 0, "
            "'async_val': 0, 'poll': 15}\n"
            "\n"
            'h2 | UNREACHABLE! => {"msg": "connection refused"}\n'
        ),
        "dict-not-a-literal": (
            'h1 | SUCCESS => {"ping": "pong"}\n'
            "[ERROR]: Task failed: connection refused\n"
            "Origin: <adhoc 'ping' task>\n"
            "\n"
            "not a dict at all\n"
            "\n"
            'h2 | UNREACHABLE! => {"msg": "connection refused"}\n'
        ),
        "truncated": (
            'h1 | SUCCESS => {"ping": "pong"}\n'
            "[ERROR]: Task failed: connection refused\n"
            "Origin: <adhoc 'ping' task>\n"
        ),
    }[corruption]
    with pytest.raises(PingInvalidOutputError):
        parse_ping_output(lines, ["h1", "h2"])


def test_parser_still_rejects_arbitrary_foreign_text_between_host_blocks() -> None:
    """Tanı bloğuyla ilgisiz keyfi metin, host blokları arasında yine reddedilir."""
    stdout = (
        'h1 | SUCCESS => {"ping": "pong"}\n'
        "bu bir tanı bloğu değil, rastgele bir log satırı\n"
        'h2 | SUCCESS => {"ping": "pong"}\n'
    )
    with pytest.raises(PingInvalidOutputError):
        parse_ping_output(stdout, ["h1", "h2"])


def test_real_closed_port_is_unreachable(tmp_path: Path) -> None:
    assert os.name == "posix"
    assert real_parser_available(), "Linux doğrulama ortamında ansible zorunludur"
    work = tmp_path / "work"
    work.mkdir()
    snapshot = work / "targets.yml"
    snapshot.write_text(
        '{"all":{"hosts":{"closed-host":{"ansible_host":"127.0.0.1","ansible_port":1}}}}\n'
    )
    known = prepare_known_hosts(tmp_path / "data")
    ssh_args = build_ssh_arguments(policy="strict", known_hosts=known, work_dir=work)
    outcome = run_ping_process(
        command=["ansible"],
        snapshot_path=snapshot,
        work_dir=work,
        ssh_arguments=ssh_args,
        forks=1,
        connect_timeout=1,
        timeout_seconds=10,
        max_output_bytes=100_000,
    )
    results = parse_ping_output(outcome.stdout_text, ["closed-host"])
    assert results[0].status == "unreachable"
    assert "closed-host" not in outcome.stderr_text
