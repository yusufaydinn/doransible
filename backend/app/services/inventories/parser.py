"""Inventory parse (T-202).

Inventory dosyaları **Ansible'ın kendi parser'ıyla** okunur; INI/YAML
söz dizimi burada yeniden uygulanmaz. Çağrı ayrı bir süreç olarak yapılır
(`ansible-inventory --list -i <path>`, ADR-017):

- Süreç sınırı, inventory eklentilerinin uygulama süreci içinde kod
  çalıştırmasını engeller.
- Timeout ve çıktı boyutu ancak ayrı süreçte anlamlı biçimde uygulanabilir.
- `ansible-core` kurulu değilse uygulamanın geri kalanı çalışmaya devam eder.

Komut **argüman listesi** olarak kurulur; shell kullanılmaz (GUVENLIK.md
bölüm 5, subprocess güvenlik sözleşmesi).

Ham `ansible-inventory` JSON'u dışarı verilmez; bu modül onu kararlı sıralı ve
maskelenmiş bir domain yapısına çevirir.

Sınırlı alt süreç makinesi (environment daraltması, gerçek zamanlı çıktı
sınırı, timeout, `terminate→kill`) :mod:`app.services.ansible.process` içine
**çıkarılmıştır**: T-204 ping akışı da aynı sınırlara ihtiyaç duyar ve güvenlik
kritik kodun iki kopyası zamanla birbirinden ayrışırdı.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from app.core.config import Settings
from app.core.errors import AppError, ValidationFailedError
from app.services.ansible.process import (
    MAX_STDERR_BYTES,
    ProcessLaunchError,
    ProcessLimits,
    build_base_environment,
    collect_bounded_output,
    contains_python_traceback,
    run_bounded_process,
    sanitize_output,
    write_empty_ansible_config,
)
from app.services.security.redaction import redact_mapping

# Parser hata çıktısından kullanıcıya gösterilecek azami metin.
PARSER_MESSAGE_MAX_LENGTH = 400

# Yalnızca statik dosya eklentileri. `script` bilinçli olarak dışarıdadır:
# dinamik inventory, çalıştırılabilir bir dosyayı çağırmak demektir ve ürünün
# "arbitrary shell execution yok" ilkesine aykırıdır (ADR-009).
ENABLED_INVENTORY_PLUGINS = "ini,yaml"

# Uygulamanın kendi ürettiği snapshot dosyaları için kullanılan daha dar küme.
# Snapshot biçimini biz yazdığımız için `ini` eklentisine ihtiyaç yoktur ve
# açık bırakılması zararlıdır: `ini` eklentisi JSON metnini ayrıştırmaya
# çalışıp başarısız olmadan önce paylaşılan inventory nesnesine hayalet bir
# host (`{`) ekleyebilir.
YAML_ONLY_INVENTORY_PLUGINS = "yaml"

# Testler ve çağıranlar için geri uyumlu ad; gerçek sınır process katmanındadır.
__all__ = [
    "ENABLED_INVENTORY_PLUGINS",
    "MAX_STDERR_BYTES",
    "PARSER_MESSAGE_MAX_LENGTH",
    "YAML_ONLY_INVENTORY_PLUGINS",
    "InventoryContents",
    "InventoryGroup",
    "InventoryHost",
    "InventoryParseFailedError",
    "InventoryParseTimeoutError",
    "InventoryParserInvalidOutputError",
    "InventoryParserOutputTooLargeError",
    "InventoryParserUnavailableError",
    "ParsedInventory",
    "ParserLimits",
    "build_command",
    "build_environment",
    "load_parser_output",
    "normalize_inventory",
    "run_inventory_parser",
]


class InventoryParserUnavailableError(AppError):
    """`ansible-inventory` çalıştırılabilir değil.

    En sık sebep `ansible-core`'un kurulu olmamasıdır. Ansible, Windows'u
    control node olarak desteklemez; orada da bu hata beklenen davranıştır.
    """

    status_code = 503
    code = "inventory_parser_unavailable"


class InventoryParseTimeoutError(AppError):
    """Parser verilen süre içinde tamamlanmadı."""

    status_code = 504
    code = "inventory_parse_timeout"


class InventoryParserOutputTooLargeError(AppError):
    """Parser, kabul edilen sınırdan büyük çıktı üretti."""

    status_code = 502
    code = "inventory_parse_output_too_large"


class InventoryParserInvalidOutputError(AppError):
    """Parser çıktısı beklenen JSON sözleşmesine uymuyor."""

    status_code = 502
    code = "inventory_parse_invalid_output"


class InventoryParseFailedError(ValidationFailedError):
    """Inventory dosyası Ansible tarafından ayrıştırılamadı.

    Bu, altyapı değil **içerik** hatasıdır; kullanıcı dosyayı düzeltmelidir.
    """

    code = "inventory_parse_failed"


@dataclass(frozen=True)
class ParserLimits(ProcessLimits):
    """Parser sürecine uygulanan sınırlar.

    Alanlar :class:`~app.services.ansible.process.ProcessLimits` ile aynıdır;
    bu sınıf yalnızca ayarlardan üretim kolaylığı ekler.
    """

    @classmethod
    def from_settings(cls, settings: Settings) -> ParserLimits:
        """Ayarlardan sınır seti üretir."""
        return cls(
            timeout_seconds=settings.inventory_parse_timeout_seconds,
            max_output_bytes=settings.inventory_parse_max_output_bytes,
        )


@dataclass(frozen=True)
class InventoryGroup:
    """Bir inventory grubu ve etkin host listesi."""

    name: str
    hosts: tuple[str, ...]


@dataclass(frozen=True)
class InventoryHost:
    """Tek bir host; ait olduğu gruplar ve maskelenmiş değişkenleri."""

    name: str
    groups: tuple[str, ...]
    variables: dict[str, Any]


@dataclass(frozen=True)
class InventoryContents:
    """Bir inventory dosyasının normalize edilmiş içeriği.

    Sıralama deterministiktir: gruplar ve host'lar ada göre sıralıdır. Aynı
    dosya için aynı cevap üretilir; istemci sıralamaya güvenebilir.
    """

    inventory_id: int
    groups: tuple[InventoryGroup, ...]
    hosts: tuple[InventoryHost, ...]


@dataclass(frozen=True)
class ParsedInventory:
    """Ham `ansible-inventory --list` çıktısının yapısal hâli.

    **Bu yapıdaki değişken değerleri MASKELENMEMİŞTİR.** Yalnızca uygulama
    süreci içinde, güvenli snapshot üretimi gibi gerçek değere ihtiyaç duyan
    adımlarda kullanılır. API cevabına, log'a veya artifact'e giden yol
    :func:`normalize_inventory` üzerinden geçer ve orada maskeleme uygulanır
    (GUVENLIK.md bölüm 9).
    """

    host_variables: dict[str, dict[str, Any]]
    direct_hosts: dict[str, set[str]]
    children: dict[str, set[str]]


def build_command(
    command: Sequence[str],
    inventory_path: Path,
    *,
    limit: str | None = None,
) -> list[str]:
    """Parser komutunu **argüman listesi** olarak kurar.

    Shell string birleştirmesi kullanılmaz (GUVENLIK.md bölüm 5): path'te
    boşluk veya shell metakarakteri bulunması davranışı değiştirmez.

    ``limit`` yalnızca **doğrulanmış** bir host pattern'i olmalıdır
    (:mod:`app.services.ansible.host_patterns`). Ansible'ın ``--limit``
    seçeneği ``@dosya`` sözdizimini destekler ve doğrulanmamış bir değer
    controller üzerinde dosya okuma yüzeyi açar.
    """
    arguments = [*command, "--list", "--inventory", str(inventory_path)]
    if limit is not None:
        arguments += ["--limit", limit]
    return arguments


def build_environment(
    work_dir: Path,
    *,
    inventory_plugins: str = ENABLED_INVENTORY_PLUGINS,
) -> dict[str, str]:
    """Parser alt süreci için daraltılmış ve güvenli bir environment üretir.

    Ortak kısım :func:`~app.services.ansible.process.build_base_environment`
    içindedir. Buraya yalnızca inventory'ye özgü kararlar eklenir:

    - ``ANSIBLE_INVENTORY_ENABLED`` yalnızca statik eklentilere izin verir;
      `script` eklentisi devre dışıdır.
    - Ayrıştırılamayan kaynak sessizce boş sonuç değil **hata** üretir.
    """
    environment = build_base_environment(work_dir)
    environment.update(
        {
            "ANSIBLE_INVENTORY_ENABLED": inventory_plugins,
            "ANSIBLE_INVENTORY_UNPARSED_FAILED": "True",
            "ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED": "True",
        }
    )
    return environment


def run_inventory_parser(
    inventory_path: Path,
    *,
    command: Sequence[str],
    limits: ParserLimits,
    limit: str | None = None,
    inventory_plugins: str = ENABLED_INVENTORY_PLUGINS,
) -> str:
    """`ansible-inventory --list` çalıştırır ve ham JSON metnini döndürür.

    Uygulanan sınırlar :mod:`app.services.ansible.process` içindedir:

    - Komut argüman listesidir; ``shell=False``.
    - ``stdin`` kapatılıdır: parola soran bir alt süreç askıda kalmaz.
    - Çalışma dizini boş bir geçici dizindir; kullanıcı dizinindeki
      ``ansible.cfg`` veya ``group_vars`` yanlışlıkla devreye giremez.
    - stdout ve stderr **pipe'lardan okunur ve diske hiç yazılmaz**. Her
      akışın kendi üst sınırı vardır ve sınır aşıldığı **anda** süreç
      sonlandırılır; sürecin doğal olarak bitmesi beklenmez.
    - Timeout ayrı bir korumadır: hiç çıktı üretmeden asılı kalan süreci
      sonlandırır. Boyut sınırının yerine **geçmez**.

    Args:
        inventory_path: Doğrulanmış, normalize edilmiş inventory dosyası.
        command: Parser komutu (varsayılan ``["ansible-inventory"]``).
        limits: Timeout ve çıktı boyutu sınırları.
        limit: Doğrulanmış host pattern'i; verilirse ``--limit`` eklenir.
        inventory_plugins: Etkin inventory eklentileri. Uygulamanın kendi
            ürettiği snapshot dosyaları için ``yaml`` kullanılır.

    Returns:
        Parser'ın ürettiği ham JSON metni.

    Raises:
        InventoryParserUnavailableError: Komut çalıştırılamadı veya çöktü.
        InventoryParseTimeoutError: Süre aşıldı.
        InventoryParseFailedError: Parser sıfırdan farklı çıkış kodu döndürdü.
        InventoryParserOutputTooLargeError: stdout veya stderr sınırı aşıldı.
    """
    with tempfile.TemporaryDirectory(prefix="ansibleops-inventory-") as raw_work_dir:
        work_dir = Path(raw_work_dir)
        # Boş config: kullanıcının ansible.cfg dosyalarının okunmasını engeller.
        write_empty_ansible_config(work_dir)
        arguments = build_command(command, inventory_path, limit=limit)

        try:
            outcome = run_bounded_process(
                arguments,
                work_dir=work_dir,
                environment=build_environment(work_dir, inventory_plugins=inventory_plugins),
                limits=limits,
            )
        except ProcessLaunchError as exc:
            raise InventoryParserUnavailableError(
                "Inventory parser çalıştırılamadı. `ansible-core` kurulu olmalıdır."
            ) from exc

    if outcome.oversized_stream is not None:
        raise InventoryParserOutputTooLargeError(
            "Inventory parser kabul edilen sınırdan çok çıktı üretti; işlem durduruldu.",
            details={"stream": outcome.oversized_stream},
        )
    if outcome.timed_out:
        raise InventoryParseTimeoutError(
            "Inventory parse işlemi zaman aşımına uğradı ve durduruldu."
        )
    if outcome.return_code != 0:
        _raise_for_failed_run(outcome.stderr_text)

    return outcome.stdout_text


def _raise_for_failed_run(stderr_text: str) -> NoReturn:
    """Sıfırdan farklı çıkış kodunu doğru hata sınıfına ayırır.

    İki farklı arıza aynı çıkış koduyla gelir ve **aynı sayılmamalıdır**:

    - **Parser'ın kendisi çöktü.** Kurulum bozuk, yorumlayıcı uyumsuz veya
      platform desteklenmiyor. Kullanıcının inventory dosyasında bir sorun
      yoktur; "dosyanız ayrıştırılamadı" demek yanıltıcı olurdu. Bu durumda
      stderr **hiç** gösterilmez: içinde yorumlayıcı iç yapısı ve modül yolları
      bulunur.
    - **Inventory içeriği ayrıştırılamadı.** Kullanıcı dosyayı düzeltebilir;
      temizlenmiş bir açıklama gösterilir.

    Raises:
        InventoryParserUnavailableError: Parser çöktüyse.
        InventoryParseFailedError: Inventory içeriği ayrıştırılamadıysa.
    """
    if contains_python_traceback(stderr_text):
        raise InventoryParserUnavailableError(
            "Inventory parser çalıştırılamadı. `ansible-core` kurulumu bu "
            "platformda kullanılabilir durumda değil."
        )
    raise InventoryParseFailedError(
        "Inventory dosyası ayrıştırılamadı.",
        details={"parser_message": _sanitize_parser_output(stderr_text)},
    )


def load_parser_output(raw_output: str) -> ParsedInventory:
    """Ham `ansible-inventory --list` JSON'unu yapısal hâle çevirir.

    Dönen değişken değerleri **maskelenmemiştir**; bu fonksiyon yalnızca
    gerçek değere ihtiyaç duyan uygulama içi adımlar (güvenli snapshot üretimi)
    için vardır. Kullanıcıya giden gösterim :func:`normalize_inventory`
    üzerinden üretilir.

    Raises:
        InventoryParserInvalidOutputError: Çıktı JSON nesnesi değilse.
    """
    data = _load_json_object(raw_output)
    host_variables = _extract_host_variables(data)
    direct_hosts, children = _extract_group_topology(data)
    return ParsedInventory(
        host_variables=host_variables,
        direct_hosts=direct_hosts,
        children=children,
    )


def normalize_inventory(raw_output: str, *, inventory_id: int) -> InventoryContents:
    """Ham `ansible-inventory --list` JSON'unu domain yapısına çevirir.

    Ham JSON **dışarı verilmez**. Ansible'ın çıktısı grup merkezlidir ve
    ``_meta.hostvars`` içinde ayrı bir değişken haritası taşır; burada host
    merkezli, kararlı sıralı ve maskelenmiş bir gösterime dönüştürülür.

    Grup üyeliği ``children`` kenarları izlenerek geçişli hesaplanır: bir host
    ``web`` grubundaysa ``web``'in üst gruplarına da aittir. Döngüsel
    ``children`` tanımları sonlu sayıda ziyaretle ele alınır.

    Args:
        raw_output: Parser'ın ürettiği JSON metni.
        inventory_id: Cevaba yazılacak inventory kaydının kimliği.

    Returns:
        Kararlı sıralı ``InventoryContents``.

    Raises:
        InventoryParserInvalidOutputError: Çıktı JSON değilse veya beklenen
            sözleşmeye uymuyorsa.
    """
    parsed = load_parser_output(raw_output)
    effective = effective_group_hosts(parsed)

    host_groups: dict[str, set[str]] = {name: set() for name in parsed.host_variables}
    for group, members in effective.items():
        for host in members:
            host_groups.setdefault(host, set()).add(group)

    groups = tuple(
        InventoryGroup(name=group, hosts=tuple(sorted(effective[group])))
        for group in sorted(effective)
    )
    hosts = tuple(
        InventoryHost(
            name=host,
            groups=tuple(sorted(host_groups[host])),
            variables=redact_mapping(parsed.host_variables.get(host, {})),
        )
        for host in sorted(host_groups)
    )
    return InventoryContents(inventory_id=inventory_id, groups=groups, hosts=hosts)


def effective_group_hosts(parsed: ParsedInventory) -> dict[str, set[str]]:
    """Her grup için alt gruplardan gelenler dâhil etkin host kümesini üretir."""
    return {
        group: _collect_group_hosts(group, parsed.direct_hosts, parsed.children)
        for group in parsed.direct_hosts
    }


def _load_json_object(raw_output: str) -> dict[str, Any]:
    """Parser çıktısını JSON nesnesi olarak çözer."""
    try:
        data = json.loads(raw_output)
    except (json.JSONDecodeError, ValueError) as exc:
        raise InventoryParserInvalidOutputError(
            "Inventory parser beklenen JSON çıktısını üretmedi."
        ) from exc
    if not isinstance(data, dict):
        raise InventoryParserInvalidOutputError(
            "Inventory parser beklenen JSON çıktısını üretmedi."
        )
    return data


def _extract_host_variables(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``_meta.hostvars`` bölümünü güvenli biçimde okur.

    Eksik veya beklenen tipte olmayan bölümler hata değil boş sonuç üretir;
    tek bir host'un bozuk değişken haritası bütün cevabı düşürmez.
    """
    meta = data.get("_meta")
    if not isinstance(meta, dict):
        return {}
    hostvars = meta.get("hostvars")
    if not isinstance(hostvars, dict):
        return {}
    return {
        str(host): dict(variables)
        for host, variables in hostvars.items()
        if isinstance(variables, dict)
    }


def _extract_group_topology(
    data: dict[str, Any],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Gruplardan doğrudan host ve alt grup haritalarını çıkarır."""
    direct_hosts: dict[str, set[str]] = {}
    children: dict[str, set[str]] = {}

    for name, entry in data.items():
        if name == "_meta":
            continue
        group = str(name)
        direct_hosts.setdefault(group, set())
        children.setdefault(group, set())
        if not isinstance(entry, dict):
            continue
        direct_hosts[group].update(_string_items(entry.get("hosts")))
        for child in _string_items(entry.get("children")):
            children[group].add(child)
            direct_hosts.setdefault(child, set())
            children.setdefault(child, set())

    return direct_hosts, children


def _string_items(value: Any) -> list[str]:
    """Bir listedeki metin öğelerini döndürür; başka tipleri yok sayar."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int))]


def _collect_group_hosts(
    group: str,
    direct_hosts: dict[str, set[str]],
    children: dict[str, set[str]],
) -> set[str]:
    """Bir grubun alt gruplarından gelenler dâhil etkin host kümesi.

    Ziyaret edilen gruplar takip edilir; ``children`` döngüsü sonsuz özyineleme
    üretmez.
    """
    collected: set[str] = set()
    pending = [group]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        collected.update(direct_hosts.get(current, set()))
        pending.extend(children.get(current, set()))
    return collected


def _sanitize_parser_output(raw: str) -> str:
    """Parser hata çıktısını kullanıcıya gösterilebilir hâle getirir.

    Ayrıntı: :func:`~app.services.ansible.process.sanitize_output`.
    """
    return sanitize_output(raw, max_length=PARSER_MESSAGE_MAX_LENGTH)


# Geriye uyumlu ad: T-202 testleri sınır toplayıcıyı bu adla ölçer.
_collect_bounded_output = collect_bounded_output
