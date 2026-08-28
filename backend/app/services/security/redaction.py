"""Secret redaction (GUVENLIK.md bölüm 9).

Bu modül, kullanıcıya veya loglara gitmeden önce secret görünümlü değerleri
maskeler. İki bağımsız sinyal kullanılır:

1. **Anahtar adı** — ``ansible_password``, ``api_token`` gibi adlar.
2. **Değerin biçimi** — private key blokları, ``$ANSIBLE_VAULT`` başlıkları,
   ``Bearer ...`` token'ları, ``password=...`` gibi gömülü atamalar.

İkisi ayrı tutulur çünkü tek başına hiçbiri yeterli değildir: masum adlı bir
değişken private key taşıyabilir, secret adlı bir değişken de yapılandırılmış
bir dict içinde saklanabilir.

Redaction **tek savunma katmanı değildir**; secret değerler zaten gereksiz
yere okunmamalı ve loglanmamalıdır (GUVENLIK.md bölüm 9).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "***"

# Anahtar adında geçtiğinde değerin tamamı maskelenen parçalar. Liste bilinçli
# olarak **geniş** tutulmuştur: gereksiz yere maskelenen bir değişken can
# sıkıcıdır, sızan bir parola ise olaydır. Karşılaştırma küçük harfe indirgenmiş
# anahtar üzerinde substring olarak yapılır.
SECRET_KEY_HINTS: frozenset[str] = frozenset(
    {
        "apikey",
        "api_key",
        "access_key",
        "auth",
        "authorization",
        "credential",
        "pass",  # password, passwd, passphrase, ansible_ssh_pass ...
        "private_key",
        "privatekey",
        "pwd",
        "secret",
        "session_key",
        "ssh_key",
        "token",
        "vault",
    }
)

# Değerin kendisi secret olduğunu ele veren biçimler. Desenler hem "bu değerin
# tamamını maskele" kararında hem de metin içi maskelemede kullanılır.
#
# Atama deseninde bilinçli olarak `\b` **kullanılmaz**: `ansible_password=...`
# içinde alt çizgi bir kelime karakteri olduğu için `\bpassword\b` eşleşmez ve
# desen tam da yakalaması gereken Ansible değişkenlerini kaçırırdı.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?(?:-----END[A-Z ]*PRIVATE KEY-----|$)",
    re.DOTALL,
)
_VAULT_HEADER = re.compile(r"\$ANSIBLE_VAULT\s*;[^\s]*(?:\s+[0-9a-fA-F]+)*")
_BEARER_TOKEN = re.compile(r"bearer\s+\S+", re.IGNORECASE)

# Atama deseni, anahtar adı sinyaliyle **aynı** listeden türetilir. İki ayrı
# liste tutmak, birinin güncellenip diğerinin unutulduğu sessiz bir boşluk
# üretirdi. Uzun parçalar önce denenir ki `api_key` yerine `key` eşleşmesin.
_KEY_HINT_ALTERNATION = "|".join(
    re.escape(hint) for hint in sorted(SECRET_KEY_HINTS, key=len, reverse=True)
)
_INLINE_ASSIGNMENT = re.compile(
    rf"[\w.\-]*(?:{_KEY_HINT_ALTERNATION})[\w.\-]*\s*[=:]\s*\S+",
    re.IGNORECASE,
)

_VALUE_PATTERNS = (_PRIVATE_KEY, _VAULT_HEADER, _BEARER_TOKEN, _INLINE_ASSIGNMENT)


def is_secret_key(key: str) -> bool:
    """Anahtar adı secret taşıdığını düşündürüyor mu."""
    lowered = key.lower()
    return any(hint in lowered for hint in SECRET_KEY_HINTS)


def looks_like_secret_value(value: str) -> bool:
    """Değerin kendisi bilinen bir secret biçimine uyuyor mu.

    Private key blokları, Ansible Vault başlıkları, ``Bearer`` token'ları ve
    ``password=...`` gibi gömülü atamalar yakalanır.
    """
    return any(pattern.search(value) for pattern in _VALUE_PATTERNS)


def redact_text(text: str) -> str:
    """Serbest metin içindeki secret parçalarını **yerinde** maskeler.

    :func:`redact_value`'dan farkı: orada bir değişkenin değeri secret ise
    tamamı atılır. Burada amaç hata mesajı veya log satırıdır; metnin
    açıklayıcı kısmı korunmalı, yalnızca secret parçalar çıkarılmalıdır.
    Aksi hâlde "anlaşılır hata" ilkesi ile "secret sızdırma" ilkesi karşı
    karşıya gelir; yerinde maskeleme ikisini birden sağlar.

    Args:
        text: Maskelenecek serbest metin.

    Returns:
        Secret parçaları :data:`REDACTED` ile değiştirilmiş metin.
    """
    for pattern in _VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_value(value: Any) -> Any:
    """Tek bir değeri, iç içe yapıları da dolaşarak maskeler.

    Yapı korunur: dict anahtarları ve liste uzunlukları değişmez, yalnızca
    secret görünümlü **değerler** :data:`REDACTED` ile değiştirilir. Böylece
    kullanıcı hangi değişkenin var olduğunu görür ama içeriğini görmez.

    Args:
        value: Herhangi bir JSON-benzeri değer.

    Returns:
        Maskelenmiş kopya. Girdi değiştirilmez.
    """
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, str):
        return REDACTED if looks_like_secret_value(value) else value
    if isinstance(value, (bytes, bytearray)):
        # Metin olmayan içerik kullanıcıya gösterilmez; içeriğine bakmadan maskelenir.
        return REDACTED
    if isinstance(value, Sequence):
        return [redact_value(item) for item in value]
    return value


def redact_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Bir sözlüğü anahtar ve değer sinyallerini birlikte kullanarak maskeler.

    Anahtar secret görünüyorsa **değerin tamamı** maskelenir; iç içe bir dict
    veya liste olsa bile içine girilmez. Aksi hâlde değer özyinelemeli olarak
    dolaşılır.

    Args:
        mapping: Maskelenecek sözlük.

    Returns:
        Aynı anahtarlara sahip, değerleri maskelenmiş yeni sözlük.
    """
    redacted: dict[str, Any] = {}
    for raw_key, value in mapping.items():
        key = str(raw_key)
        redacted[key] = REDACTED if is_secret_key(key) else redact_value(value)
    return redacted
