"""Gerçek `ansible-inventory` ile uçtan uca parse (T-202).

Bu dosya, stub kullanan testlerin **kapatamadığı** tek boşluğu kapatır: altın
çıktı varsayımlarımızın Ansible'ın gerçek `--list` sözleşmesiyle uyuşup
uyuşmadığı.

Ansible, Windows'u control node olarak **desteklemez**: paket kurulabilir ama
CLI ve kütüphane POSIX'e özgü modüllere (`grp`, `os.get_blocking`) bağlıdır.
Bu yüzden testler platform kısıtı nedeniyle atlanabilir. Atlanan test koşmuş
test değildir; atlama gerekçesi açıkça yazılır ve raporlanır.

Beklentiler, ``test_inventory_parser.py`` içindeki altın çıktı testleriyle
**bilinçli olarak aynıdır**: altın çıktı yanlışsa bu testler onu yakalar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.inventories.parser import (
    InventoryParseFailedError,
    ParserLimits,
    normalize_inventory,
    run_inventory_parser,
)
from app.services.security.redaction import REDACTED
from tests.support import real_parser_available

pytestmark = pytest.mark.skipif(
    not real_parser_available(),
    reason=(
        "`ansible-inventory` bu platformda çalıştırılamıyor. "
        "Ansible, Windows'u control node olarak desteklemez; "
        "geliştirme makinesi Windows olduğu için atlanıyor."
    ),
)

INI_INVENTORY = """\
[web]
web01 ansible_host=10.0.0.10
web02 ansible_host=10.0.0.11

[db]
db01 ansible_host=10.0.0.20

[production:children]
web
db
"""

YAML_INVENTORY = """\
all:
  children:
    web:
      hosts:
        web01:
          ansible_host: 10.0.0.10
          http_port: 8080
"""

SECRET_INVENTORY = """\
[web]
web01 ansible_host=10.0.0.10 ansible_password=hunter2 api_token=ghp_gizli
"""


def _parse(inventory_path: Path, inventory_id: int = 1):  # type: ignore[no-untyped-def]
    raw = run_inventory_parser(
        inventory_path,
        command=["ansible-inventory"],
        limits=ParserLimits(),
    )
    return normalize_inventory(raw, inventory_id=inventory_id)


def test_real_ini_inventory_is_parsed(tmp_path: Path) -> None:
    inventory = tmp_path / "hosts.ini"
    inventory.write_text(INI_INVENTORY, encoding="utf-8")

    contents = _parse(inventory, inventory_id=7)

    groups = {group.name: group.hosts for group in contents.groups}
    assert contents.inventory_id == 7
    assert groups["web"] == ("web01", "web02")
    assert groups["db"] == ("db01",)
    assert groups["production"] == ("db01", "web01", "web02")
    hosts = {host.name: host for host in contents.hosts}
    assert hosts["web01"].groups == ("all", "production", "web")
    assert hosts["web01"].variables["ansible_host"] == "10.0.0.10"


def test_real_yaml_inventory_is_parsed(tmp_path: Path) -> None:
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(YAML_INVENTORY, encoding="utf-8")

    contents = _parse(inventory)

    assert [host.name for host in contents.hosts] == ["web01"]
    assert contents.hosts[0].variables["http_port"] == 8080
    assert "web" in contents.hosts[0].groups


def test_real_inventory_host_variables_are_redacted(tmp_path: Path) -> None:
    inventory = tmp_path / "hosts.ini"
    inventory.write_text(SECRET_INVENTORY, encoding="utf-8")

    contents = _parse(inventory)

    variables = contents.hosts[0].variables
    assert variables["ansible_password"] == REDACTED
    assert variables["api_token"] == REDACTED
    assert variables["ansible_host"] == "10.0.0.10"


def test_real_invalid_inventory_produces_a_safe_error(tmp_path: Path) -> None:
    inventory = tmp_path / "hosts.ini"
    inventory.write_text("[web\nweb01 =====\n:::\n", encoding="utf-8")

    with pytest.raises(InventoryParseFailedError) as exc_info:
        _parse(inventory)

    details = exc_info.value.details
    assert isinstance(details, dict)
    assert str(inventory) not in details["parser_message"]


def test_real_dynamic_inventory_script_is_not_executed(tmp_path: Path) -> None:
    """`script` eklentisi kapalıdır: çalıştırılabilir inventory çalıştırılmaz.

    Script çalışsaydı `marker` grubu görünürdü; parse hatası bekliyoruz.
    """
    inventory = tmp_path / "dynamic.sh"
    inventory.write_text('#!/bin/sh\necho \'{"marker": {"hosts": ["kacti"]}}\'\n', encoding="utf-8")
    inventory.chmod(0o700)

    with pytest.raises(InventoryParseFailedError):
        _parse(inventory)
