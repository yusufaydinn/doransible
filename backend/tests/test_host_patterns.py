"""Limit (host pattern) doğrulaması (T-204A).

Buradaki ret vakalarının çoğu, gerçek `ansible-core 2.19` üzerinde ölçülen
davranışlardan türetilmiştir: karakter allowlist'i tek başına yetmez çünkü
Ansible malformed pattern'leri ya **sessizce** yanlış yorumlar ya da çöker.
"""

from __future__ import annotations

import pytest

from app.services.ansible.host_patterns import (
    MAX_LIMIT_LENGTH,
    InvalidLimitPatternError,
    validate_limit_pattern,
)

ACCEPTED = [
    "all",
    "web01",
    "webservers",
    "web*",
    "web,db",
    "web:db",
    "web:&stage",
    "web:!web03",
    "production:!web02",
    "web[01:20]",
    "web[3]",
    "srv-01.prod",
    "srv_01",
]

REJECTED = [
    pytest.param("", id="bos"),
    pytest.param("   ", id="yalnizca-bosluk"),
    pytest.param(" web", id="onde-bosluk"),
    pytest.param("web ", id="sonda-bosluk"),
    pytest.param("web db", id="ortada-bosluk"),
    # Ölçüldü: `@dosya` içeriği okunur ve hata metnine geri yazılır.
    pytest.param("@/etc/passwd", id="at-dosya"),
    pytest.param("web,@hosts.txt", id="at-dosya-ikinci-token"),
    pytest.param("/etc/passwd", id="path"),
    pytest.param("c:\\hosts", id="windows-path"),
    pytest.param("~^web", id="regex"),
    # Ölçüldü: sessizce `web` ve `01` diye bölünüyordu.
    pytest.param("web[01", id="kapanmayan-parantez"),
    pytest.param("web01]", id="kapanmayan-kapanis"),
    pytest.param("web[[01]]", id="ic-ice-parantez"),
    pytest.param("web[a b]", id="parantez-icinde-bosluk"),
    # Ölçüldü: Ansible rc=250 ve traceback ile çöküyordu.
    pytest.param("!", id="yalniz-unlem"),
    pytest.param("&", id="yalniz-ampersan"),
    # Ölçüldü: üçü de sessizce TÜM host'ları seçiyordu.
    pytest.param("all::", id="cift-iki-nokta"),
    pytest.param(":", id="yalniz-ayrac"),
    pytest.param(",,", id="yalniz-virgul"),
    pytest.param("web!db", id="islec-token-ortasinda"),
    pytest.param("!web01", id="yalnizca-dislama"),
    pytest.param("!web01:!web02", id="yalnizca-coklu-dislama"),
    pytest.param("web\x00db", id="nul"),
    pytest.param("web\ndb", id="yeni-satir"),
    pytest.param("a" * (MAX_LIMIT_LENGTH + 1), id="cok-uzun"),
]


@pytest.mark.parametrize("pattern", ACCEPTED)
def test_supported_patterns_are_accepted(pattern: str) -> None:
    """MVP'de desteklenen desenler değiştirilmeden döner."""
    assert validate_limit_pattern(pattern) == pattern


@pytest.mark.parametrize("pattern", REJECTED)
def test_unsafe_or_malformed_patterns_are_rejected(pattern: str) -> None:
    with pytest.raises(InvalidLimitPatternError) as exc_info:
        validate_limit_pattern(pattern)

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "ping_invalid_limit"


def test_missing_limit_means_whole_inventory() -> None:
    """Alanın hiç verilmemesi ile boş metin **farklı** eylemlerdir.

    ``None`` tüm inventory demektir; boş metin ise hatadır. Boş metni sessizce
    "tüm filo"ya çevirmek muhtemel bir yazım hatasını en geniş etkiye
    dönüştürürdü.
    """
    assert validate_limit_pattern(None) is None


def test_rejection_message_does_not_echo_the_input() -> None:
    """Hata metni kullanıcının girdisini geri yazmaz (log forging, sızıntı)."""
    with pytest.raises(InvalidLimitPatternError) as exc_info:
        validate_limit_pattern("@/srv/gizli/anahtar")

    rendered = f"{exc_info.value.message} {exc_info.value.details}"
    assert "/srv/gizli/anahtar" not in rendered
    assert exc_info.value.details is None


def test_maximum_length_is_accepted() -> None:
    """Sınır değerin kendisi reddedilmez."""
    pattern = "a" * MAX_LIMIT_LENGTH

    assert validate_limit_pattern(pattern) == pattern
