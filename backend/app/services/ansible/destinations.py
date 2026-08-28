"""SSH hedefi ve inventory host adı doğrulaması (T-204).

**Shell kullanmamak OpenSSH option injection'ını tek başına çözmez.**

Argüman listesiyle çalıştırmak *bizim* sürecimizin kabuğunu devre dışı bırakır;
ancak çağrılan program (``ssh``) kendi argv ayrıştırmasını yapar ve orada lider
``-`` bir seçenektir. Ansible'ın SSH connection plugin'i hedefi komutun sonuna
``--`` ayıracı **olmadan** ekler (``_build_command``, ``other_args``), yani
``ansible_host`` değeri doğrudan ``ssh`` argv'sine düşer.

Ölçülen kanıt::

    $ ssh -F/yok/boyle/bir/dosya ornek.host true
    Can't open user config file /yok/boyle/bir/dosya: No such file or directory

Değer bir seçenek olarak **tüketildi**. Aynı değer ``ansible_host`` üzerinden
verildiğinde de Ansible onu olduğu gibi geçirdi.

İkinci ölçülen sorun ``user@host``: ``ansible_host: "root@127.0.0.1"`` ile
birlikte ``ansible_user: "deploy"`` verildiğinde bağlantı ``root`` olarak
kuruldu — hedefin içindeki kullanıcı ``-o User`` ayarını sessizce ezdi.

Bu yüzden hedef, karakter yasaklarıyla değil **pozitif bir sözleşmeyle**
kabul edilir: geçerli bir DNS adı, IPv4 veya IPv6 olmalıdır. Yeni bir
dependency gerekmez; ``ipaddress`` standart kütüphanededir.
"""

from __future__ import annotations

import ipaddress
import re

MAX_DESTINATION_LENGTH = 255
MAX_LABEL_LENGTH = 63

# Tek bir DNS etiketi. İlk ve son karakterin alfanümerik olma zorunluluğu,
# lider `-` (seçenek görünümlü değer) ihtimalini de kapatır.
# Alt çizgi bilinçli olarak kabul edilir: inventory alias'larında yaygındır.
_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?$")

# Hiçbir koşulda kabul edilmeyen karakterler. Kontrol karakterleri ayrıca
# taranır: log forging ve tek satırlık hata metinlerinin bölünmesi engellenir.
_FORBIDDEN_CHARACTERS = frozenset({"@", "/", "\\", '"', "'", "`", "$", "%"})


def is_valid_ssh_destination(value: str) -> bool:
    """Değer, ``ssh`` hedefi olarak güvenle kullanılabilir mi.

    Kabul edilenler: geçerli DNS adı, IPv4 adresi, IPv6 adresi.

    Reddedilenler arasında lider ``-``, ``user@host`` biçimi, path benzeri
    değerler, boşluk ve kontrol karakterleri bulunur.
    """
    if not _passes_common_checks(value):
        return False
    return _is_ip_address(value) or _is_dns_name(value)


def is_valid_display_hostname(value: str) -> bool:
    """Değer, inventory host adı olarak güvenle kullanılabilir mi.

    Gösterim adı snapshot'ta sözlük anahtarı, çıktı ayrıştırmasında çapa ve
    API cevabında görünen metindir. Bu yüzden hedefle **aynı** sözleşmeye
    tabidir: kontrol karakteri içeren bir ad log satırı bölebilir, boşluk
    içeren bir ad çıktı çapasını kaydırabilir.
    """
    return is_valid_ssh_destination(value)


def effective_destination(host_name: str, variables: dict[str, object]) -> str:
    """Bir host için gerçekte ``ssh``'e gidecek hedefi döndürür.

    ``ansible_host`` tanımlıysa hedef odur; tanımlı değilse inventory host
    adının kendisi hedef olarak kullanılır. İkisi de aynı doğrulamadan geçmek
    zorundadır, çünkü hangisinin kullanılacağı inventory içeriğine bağlıdır.
    """
    raw = variables.get("ansible_host")
    if isinstance(raw, str) and raw:
        return raw
    return host_name


def _passes_common_checks(value: str) -> bool:
    """Uzunluk, yasaklı karakter ve kontrol karakteri kontrolleri."""
    if not value or len(value) > MAX_DESTINATION_LENGTH:
        return False
    if value[0] == "-":
        # Ölçüldü: ssh bunu seçenek olarak tüketiyor.
        return False
    if any(char in _FORBIDDEN_CHARACTERS for char in value):
        return False
    if any(char.isspace() for char in value):
        return False
    return all(ord(char) >= 0x20 and ord(char) != 0x7F for char in value)


def _is_ip_address(value: str) -> bool:
    """Değer geçerli bir IPv4 veya IPv6 adresi mi.

    Köşeli parantezli IPv6 yazımı (``[::1]``) bilinçli olarak kabul edilmez:
    tek bir kanonik biçim, snapshot ve çıktı eşleştirmesini belirsizlikten
    kurtarır.
    """
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _is_dns_name(value: str) -> bool:
    """Değer geçerli bir DNS adı mı (etiketlere bölünerek doğrulanır)."""
    labels = value.split(".")
    if any(len(label) > MAX_LABEL_LENGTH for label in labels):
        return False
    return all(_DNS_LABEL.fullmatch(label) for label in labels)
