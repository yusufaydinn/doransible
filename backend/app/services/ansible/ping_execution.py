"""Sabit Ansible ping argv'si, dar environment ve güvenli çıktı parser'ı."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.errors import AppError
from app.services.ansible.process import (
    ProcessLimits,
    ProcessOutcome,
    build_base_environment,
    run_bounded_process,
    sanitize_output,
    write_empty_ansible_config,
)
from app.services.ansible.ssh import render_ansible_ssh_args

_HEADER = re.compile(
    r"^(?P<host>[^\s|]+) \| (?P<state>SUCCESS|UNREACHABLE!|FAILED!) => (?P<body>\{.*)$"
)

# Ansible-core 2.19, ad-hoc bir task bağlantı hatasıyla başarısız olduğunda bu
# sonucu anons etmeden **önce** kendi tanısal bloğunu yazar (ölçülmüş biçim,
# bkz. `test_ping_execution.py`):
#
#   [ERROR]: Task failed: <ileti>
#   Origin: <adhoc 'ping' task>
#   <boş satır>
#   {'action': 'ping', 'args': {}, 'timeout': 0, 'async_val': 0, 'poll': 15}
#
# `Origin` satırı, komutumuz her zaman sabit `-m ping` ad-hoc çağrısı olduğu
# için deterministiktir. Bu blok host sonucu **değildir**; saklanmaz ve
# kullanıcıya döndürülmez.
_DIAGNOSTIC_ERROR_PREFIX = "[ERROR]: Task failed:"
_DIAGNOSTIC_ORIGIN_LINE = "Origin: <adhoc 'ping' task>"
_DIAGNOSTIC_TASK_KEYS = frozenset({"action", "args", "timeout", "async_val", "poll"})


class PingInvalidOutputError(AppError):
    status_code = 502
    code = "ping_invalid_output"


@dataclass(frozen=True)
class PingHostResult:
    host: str
    status: str
    message: str


def build_ping_command(
    command: Sequence[str], snapshot_path: Path, *, forks: int, connect_timeout: int
) -> list[str]:
    """Semantiği sabit ping komutunu argv olarak kurar."""
    return [
        *command,
        "all",
        "-i",
        str(snapshot_path),
        "-m",
        "ping",
        "--forks",
        str(forks),
        "-T",
        str(connect_timeout),
    ]


def build_ping_environment(work_dir: Path, ssh_arguments: list[str]) -> dict[str, str]:
    """Parent secret/proxy/ANSIBLE değişkenlerini taşımayan environment."""
    environment = build_base_environment(work_dir)
    environment.update(
        {
            "ANSIBLE_INVENTORY_ENABLED": "yaml",
            "ANSIBLE_INVENTORY_UNPARSED_FAILED": "True",
            "ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED": "True",
            "ANSIBLE_SSH_ARGS": render_ansible_ssh_args(ssh_arguments),
        }
    )
    return environment


def run_ping_process(
    *,
    command: Sequence[str],
    snapshot_path: Path,
    work_dir: Path,
    ssh_arguments: list[str],
    forks: int,
    connect_timeout: int,
    timeout_seconds: float,
    max_output_bytes: int,
) -> ProcessOutcome:
    """Dondurulmuş snapshot üzerinde sınırlı ping sürecini çalıştırır."""
    write_empty_ansible_config(work_dir)
    return run_bounded_process(
        build_ping_command(command, snapshot_path, forks=forks, connect_timeout=connect_timeout),
        work_dir=work_dir,
        environment=build_ping_environment(work_dir, ssh_arguments),
        limits=ProcessLimits(timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes),
    )


def _is_ping_task_literal(line: str) -> bool:
    """Satırın sabit ping ad-hoc task'ının Python dict repr'i olup olmadığını doğrular.

    Yapısal olarak doğrulanır: yalnızca ``ast.literal_eval`` ile çözülebilen,
    ``action`` alanı ``"ping"`` ve ``args`` alanı boş olan, bilinen alan
    kümesini aşmayan bir dict kabul edilir. Sayısal alanların (``timeout``,
    ``poll`` vb.) tam değerleri kasıtlı olarak sabitlenmez; bunlar Ansible'ın
    iç varsayılanlarıdır ve sürüm yamalarıyla değişebilir.
    """
    try:
        payload = ast.literal_eval(line)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("action") != "ping" or payload.get("args") != {}:
        return False
    return set(payload) <= _DIAGNOSTIC_TASK_KEYS


def _match_diagnostic_block(lines: Sequence[str], index: int) -> int | None:
    """Ansible-core 2.19 ad-hoc ping bağlantı tanı bloğunu tanır.

    Blok tam olarak dört satırdan oluşur (``[ERROR]: Task failed: ...``,
    ``Origin: <adhoc 'ping' task>``, boş satır, task dict'i) ve yalnızca
    hemen ardından — varsa aradaki boş satırlar atlanarak — bilinen bir
    ``UNREACHABLE!`` host başlığı geliyorsa kabul edilir; bu blok yalnızca o
    host'un bağlantı hatasını anons eder. Eşleşme bulunamazsa çağıran bunu
    yabancı metin olarak reddeder.

    Args:
        lines: Bütün stdout satırları.
        index: Şu an incelenen satırın indeksi.

    Returns:
        Bloktan sonraki ilk satırın indeksi, eşleşme yoksa ``None``.
    """
    if index + 3 >= len(lines):
        return None
    if not lines[index].startswith(_DIAGNOSTIC_ERROR_PREFIX):
        return None
    if lines[index + 1] != _DIAGNOSTIC_ORIGIN_LINE:
        return None
    if lines[index + 2].strip():
        return None
    if not _is_ping_task_literal(lines[index + 3]):
        return None

    after = index + 4
    while after < len(lines) and not lines[after].strip():
        after += 1
    if after >= len(lines):
        return None
    next_header = _HEADER.match(lines[after])
    if next_header is None or next_header.group("state") != "UNREACHABLE!":
        return None
    return index + 4


def parse_ping_output(stdout: str, expected_hosts: Sequence[str]) -> tuple[PingHostResult, ...]:
    """Ansible JSON bloklarını bilinen host kümesine göre normalize eder."""
    expected = set(expected_hosts)
    if len(expected) != len(expected_hosts):
        raise ValueError("Beklenen host listesi yinelenemez.")
    found: dict[str, PingHostResult] = {}
    lines = stdout.splitlines()
    index = 0
    saw_preamble = False
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        match = _HEADER.match(lines[index])
        if match is None:
            # Ansible-core 2.19 ilk host sonucundan önce kendi tanısal
            # bağlamını yazabilir; bu, hiç host görülmemişken herhangi bir
            # yabancı metni tolere eden mevcut preamble davranışıyla zaten
            # yutulur.
            if not found:
                saw_preamble = True
                index += 1
                continue
            # Host sonuçları arasında ise yalnız kanonik ad-hoc ping tanı
            # bloğu kabul edilir (bkz. `_match_diagnostic_block`); bu blok
            # sonuç değildir, saklanmaz ve kullanıcıya döndürülmez. Başka her
            # yabancı metin çıktıyı geçersiz kılar.
            diagnostic_end = _match_diagnostic_block(lines, index)
            if diagnostic_end is not None:
                index = diagnostic_end
                continue
            raise PingInvalidOutputError("Ping çıktısı beklenen biçimde değil.")
        host = match.group("host")
        if host not in expected or host in found:
            raise PingInvalidOutputError("Ping çıktısı bilinmeyen veya yinelenen host içeriyor.")
        body_lines = [match.group("body")]
        payload: Any = None
        while index < len(lines):
            try:
                payload = json.loads("\n".join(body_lines))
                break
            except ValueError:
                index += 1
                if index >= len(lines):
                    break
                body_lines.append(lines[index])
        if not isinstance(payload, dict):
            raise PingInvalidOutputError("Ping çıktısındaki JSON geçersiz.")
        state = match.group("state")
        status = {
            "SUCCESS": "reachable",
            "UNREACHABLE!": "unreachable",
            "FAILED!": "failed",
        }[state]
        raw_message = payload.get("msg", payload.get("ping", status))
        message = sanitize_output(str(raw_message), max_length=400)
        found[host] = PingHostResult(host=host, status=status, message=message)
        index += 1
    if saw_preamble and not found:
        raise PingInvalidOutputError("Ping çıktısında host sonucu bulunamadı.")
    return tuple(
        found.get(host, PingHostResult(host=host, status="no_result", message=""))
        for host in expected_hosts
    )
