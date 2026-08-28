"""Ansible host pattern (limit) doğrulaması (T-204).

Ansible'ın ``--limit`` seçeneği masum bir string filtresi **değildir**; iki
somut saldırı yüzeyi taşır:

1. **Dosya okuma.** ``InventoryManager.subset()`` ``@`` önekini "Unix style
   @filename" olarak yorumlar ve dosyayı okur. ``--limit @/etc/passwd``
   çağrısında dosyanın satırları hata metnine geri yazılır. Komut argüman
   listesi olarak kurulsa ve shell hiç kullanılmasa bile bu yüzey açıktır:
   ayrıştırmayı yapan bizim kabuğumuz değil, Ansible'ın kendisidir.
2. **Var/yok sondası.** ``--limit @/olmayan`` çağrısı "Unable to find limit
   file" üretir; bu, keyfi bir path için varlık bilgisi sızdırır.

Karakter allowlist'i tek başına yetmez. Ölçülen davranışlar:

- ``web[01`` (kapanmayan köşeli parantez) **sessizce** ``web`` ve ``01``
  parçalarına bölünür, hiçbiriyle eşleşmez ve boş küme üretir.
- ``!`` tek başına Ansible'ı ``rc=250`` ve bir Python traceback'i ile çökertir.
- ``:``, ``,,`` ve ``all::`` **sessizce tüm host'ları** seçer.

Bu yüzden doğrulama yapısaldır: uzunluk → karakter kümesi → tokenizasyon →
token biçimi. Reddedilen her değer tek bir hata koduyla döner; Ansible'ın
metni kullanıcıya hiç gösterilmez.
"""

from __future__ import annotations

import re
from typing import NoReturn

from app.core.errors import ValidationFailedError

MAX_LIMIT_LENGTH = 256

# Ansible pattern'lerinde anlamlı olan ve güvenli bulunan karakterler.
# Bilinçli olarak dışarıda kalanlar:
#   @    -> @dosya sözdizimi (dosya okuma yüzeyi)
#   / \  -> path benzeri değerlerin hiçbir meşru host pattern'inde işi yoktur
#   ~    -> kullanıcı tanımlı regex; büyük host listesinde öngörülemez maliyet
#   boşluk -> `split_host_pattern` boşlukta da böler; kullanıcının tek pattern
#             sandığı girdi sessizce ikinci bir pattern'e dönüşür
_ALLOWED_CHARACTERS = re.compile(r"^[A-Za-z0-9._\-*\[\]:,!&]+$")

# Köşeli parantez içi aralık: `web[01:20]` veya tek indeks `web[3]`.
_BRACKET_CONTENT = re.compile(r"^[A-Za-z0-9]+(?::[A-Za-z0-9]+)?$")

# Bir token'ın başında bulunabilecek küme işleçleri.
_TOKEN_PREFIXES = ("!", "&")


class InvalidLimitPatternError(ValidationFailedError):
    """Limit pattern'i biçimsel veya yapısal olarak kabul edilebilir değil.

    Hata metni bilinçli olarak **geneldir**: hangi alt kuralın ihlal edildiğini
    ayrıntılandırmak, kullanıcıya fayda sağlamadan reddetme mantığını haritalar.
    """

    code = "ping_invalid_limit"


def validate_limit_pattern(raw: str | None) -> str | None:
    """Kullanıcı limitini doğrular ve kanonik hâlini döndürür.

    Sözleşme **her katmanda aynıdır**:

    - Alan hiç verilmemişse veya ``None`` ise sonuç ``None``'dır: tüm inventory
      hedeflenir.
    - Boş veya yalnızca boşluktan oluşan bir değer **hatadır**. Boş string
      göndermek, alanı hiç göndermemekten farklı bir kullanıcı eylemidir; onu
      sessizce "tüm filo" hâline çevirmek, muhtemel bir yazım hatasını en geniş
      etkiye dönüştürürdü.

    Args:
        raw: Kullanıcının girdiği ham limit değeri.

    Returns:
        Doğrulanmış pattern veya limit istenmemişse ``None``.

    Raises:
        InvalidLimitPatternError: Değer boş, çok uzun, izin verilmeyen karakter
            içeriyor veya yapısal olarak bozuksa.
    """
    if raw is None:
        return None

    if raw.strip() != raw or not raw:
        # Baştaki/sondaki boşluk da bir ayraçtır; sessizce kırpmak yerine
        # reddedilir ki gönderilen değer ile çalıştırılan değer aynı olsun.
        _reject()

    if len(raw) > MAX_LIMIT_LENGTH:
        _reject()

    if not _ALLOWED_CHARACTERS.fullmatch(raw):
        _reject()

    tokens = _tokenize(raw)
    inclusive_count = 0
    for token in tokens:
        if not token:
            # `:`, `,,`, `all::` gibi değerler burada yakalanır. Ansible bunları
            # sessizce "tüm host'lar" olarak yorumlardı.
            _reject()
        body = token
        prefix = ""
        if token[0] in _TOKEN_PREFIXES:
            prefix = token[0]
            body = token[1:]
        if not body:
            # Tek başına `!` veya `&`. Ansible bu girdide traceback ile çöker.
            _reject()
        if any(char in body for char in _TOKEN_PREFIXES):
            # `web!db` gibi değerler; işleç yalnızca token başında anlamlıdır.
            _reject()
        _validate_brackets(body)
        if prefix != "!":
            inclusive_count += 1

    if inclusive_count == 0:
        # Yalnızca dışlamadan oluşan bir pattern hiçbir host seçmez; kullanıcı
        # büyük ihtimalle kapsayıcı bir terim yazmayı unutmuştur.
        _reject()

    return raw


def _tokenize(pattern: str) -> list[str]:
    """Pattern'i ``,`` ve ``:`` ayraçlarına göre böler; ``[...]`` korunur.

    Köşeli parantez içindeki ``:`` bir ayraç değil aralık işaretidir
    (``web[01:20]``), bu yüzden derinlik takip edilir.

    Raises:
        InvalidLimitPatternError: Parantezler dengesiz veya iç içeyse.
    """
    tokens: list[str] = []
    current: list[str] = []
    depth = 0

    for char in pattern:
        if char == "[":
            if depth:
                _reject()
            depth += 1
            current.append(char)
        elif char == "]":
            if not depth:
                _reject()
            depth -= 1
            current.append(char)
        elif char in ",:" and depth == 0:
            tokens.append("".join(current))
            current = []
        else:
            current.append(char)

    if depth:
        # `web[01` — Ansible bunu sessizce `web` ve `01` diye bölerdi.
        _reject()

    tokens.append("".join(current))
    return tokens


def _validate_brackets(body: str) -> None:
    """Token içindeki köşeli parantez bloklarının biçimini doğrular."""
    depth = 0
    content: list[str] = []
    for char in body:
        if char == "[":
            depth += 1
            content = []
        elif char == "]":
            depth -= 1
            if not _BRACKET_CONTENT.fullmatch("".join(content)):
                _reject()
        elif depth:
            content.append(char)


def _reject() -> NoReturn:
    """Tek ve genel bir ret üretir."""
    raise InvalidLimitPatternError(
        "Limit deseni geçersiz. Host adı, grup adı veya bunların "
        "`,` `:` `:&` `:!` ile birleştirilmiş hâlini kullanın."
    )
