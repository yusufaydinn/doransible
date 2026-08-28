"""SSH hedefi ve gösterim host adı doğrulaması (T-204A).

Kritik gerekçe: **shell kullanmamak OpenSSH option injection'ını çözmez.**
Ansible hedefi ``ssh`` argv'sine ``--`` ayıracı olmadan ekler; lider ``-``
taşıyan bir değer orada seçenek olarak tüketilir. Ölçülen kanıt::

    $ ssh -F/yok/boyle/bir/dosya ornek.host true
    Can't open user config file /yok/boyle/bir/dosya: No such file or directory
"""

from __future__ import annotations

import pytest

from app.services.ansible.destinations import (
    MAX_DESTINATION_LENGTH,
    effective_destination,
    is_valid_display_hostname,
    is_valid_ssh_destination,
)

ACCEPTED = [
    pytest.param("web01", id="tek-etiket"),
    pytest.param("web01.prod.ornek", id="dns"),
    pytest.param("srv_01", id="alt-cizgi"),
    pytest.param("node-3", id="tire-ortada"),
    pytest.param("10.0.0.10", id="ipv4"),
    pytest.param("127.0.0.1", id="ipv4-loopback"),
    pytest.param("2001:db8::1", id="ipv6"),
    pytest.param("::1", id="ipv6-loopback"),
]

REJECTED = [
    # Seçenek enjeksiyonu — ölçüldü.
    pytest.param("-oProxyCommand=/bin/sh", id="proxycommand"),
    pytest.param("-F/tmp/config", id="alternatif-ssh-config"),
    pytest.param("-J jump.host", id="proxyjump"),
    pytest.param("-i/tmp/key", id="identity-file"),
    pytest.param("-lroot", id="login-name"),
    # `user@host` ansible_user'ı sessizce eziyordu — ölçüldü.
    pytest.param("root@127.0.0.1", id="user-at-host"),
    pytest.param("web01/../etc", id="path-benzeri"),
    pytest.param("web01\\evil", id="ters-slash"),
    pytest.param("web 01", id="bosluk"),
    pytest.param("web01\n[WARNING] sahte", id="yeni-satir-log-forging"),
    pytest.param("web01\x00", id="nul"),
    pytest.param("web01\x7f", id="delete-karakteri"),
    pytest.param('web"01', id="cift-tirnak"),
    pytest.param("web'01", id="tek-tirnak"),
    pytest.param("$(whoami)", id="komut-ikamesi"),
    pytest.param("web01`id`", id="backtick"),
    pytest.param("", id="bos"),
    pytest.param("-", id="yalniz-tire"),
    pytest.param("a" * (MAX_DESTINATION_LENGTH + 1), id="cok-uzun"),
    pytest.param("a" * 64 + ".ornek", id="cok-uzun-etiket"),
    pytest.param("web..prod", id="bos-etiket"),
    pytest.param("[::1]", id="koseli-parantezli-ipv6"),
]


@pytest.mark.parametrize("value", ACCEPTED)
def test_supported_destinations_are_accepted(value: str) -> None:
    assert is_valid_ssh_destination(value) is True


@pytest.mark.parametrize("value", REJECTED)
def test_unsafe_destinations_are_rejected(value: str) -> None:
    assert is_valid_ssh_destination(value) is False


@pytest.mark.parametrize("value", REJECTED)
def test_display_hostname_uses_the_same_contract(value: str) -> None:
    """Gösterim adı da aynı sözleşmeye tabidir.

    Gösterim adı snapshot'ta anahtar, çıktı ayrıştırmasında çapa ve API
    cevabında metindir; kontrol karakteri içeren bir ad log satırı bölebilir.
    """
    assert is_valid_display_hostname(value) is False


def test_effective_destination_prefers_ansible_host() -> None:
    """``ansible_host`` tanımlıysa gerçek hedef odur."""
    assert effective_destination("web01", {"ansible_host": "10.0.0.10"}) == "10.0.0.10"


def test_effective_destination_falls_back_to_inventory_hostname() -> None:
    """``ansible_host`` yoksa hedef inventory host adının kendisidir.

    Bu yüzden inventory host adı da hedef sözleşmesinden geçmek zorundadır.
    """
    assert effective_destination("web01.prod", {}) == "web01.prod"


def test_effective_destination_ignores_non_string_ansible_host() -> None:
    """Beklenmeyen tipte bir ``ansible_host`` hedefi ele geçiremez."""
    assert effective_destination("web01", {"ansible_host": 1234}) == "web01"
    assert effective_destination("web01", {"ansible_host": ""}) == "web01"
