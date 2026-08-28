"""Güvenli inventory snapshot üretimi ve hostvar allowlist'i (T-204).

Kayıtlı bir inventory'yi ping'lemek, o dosyanın içeriğine güvenmek demektir:
``ansible_ssh_executable`` gibi değişkenler **controller üzerinde** hangi
ikilinin çalıştırılacağını seçer, ``ansible_ssh_common_args`` ise keyfi SSH
seçeneği (``ProxyCommand`` dâhil) enjekte eder. Bunları dokümantasyonla
"önermemek" ürünün "arbitrary shell execution yok" ilkesini karşılamaz.

Bu yüzden özgün inventory ping komutuna **hiç verilmez**. Onun yerine burada,
uygulamanın kendi ürettiği dondurulmuş bir snapshot kurulur ve yalnızca
**pozitif allowlist**'ten geçmiş bağlantı alanları taşınır. Bilinmeyen her
``ansible_*`` değişkeni fail-closed reddedilir: sessizce atmak, ping'in
kullanıcının gerçek playbook çalıştırmasından farklı koşullarda koşmasına yol
açar ve "gördüğün şey çalışacak olan şeydir" vaadini bozar.

Snapshot biçimi **JSON metnidir** ve ``.yml`` uzantısıyla yazılır. JSON, YAML'ın
alt kümesi olduğu için Ansible'ın ``yaml`` inventory eklentisi onu sorunsuz
ayrıştırır; böylece PyYAML'a doğrudan bağımlılık gerekmez.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from app.core.errors import AppError, ValidationFailedError
from app.services.ansible.destinations import (
    effective_destination,
    is_valid_display_hostname,
    is_valid_ssh_destination,
)
from app.services.security.paths import (
    ensure_existing_file,
    ensure_within_allowed_roots,
    normalize_filesystem_path,
)

# Ansible'ın kendi ürettiği örtük gruplar; snapshot'ta yeniden tanımlanmaz.
IMPLICIT_GROUPS = frozenset({"all", "ungrouped"})

# Snapshot'a taşınabilecek tek değişken kümesi. Her biri için "neden zaruri"
# ve "controller üzerindeki etkisi" ADR-018'de kayıtlıdır.
ALLOWED_VARIABLES = frozenset(
    {
        "ansible_host",
        "ansible_port",
        "ansible_user",
        "ansible_ssh_user",
        "ansible_ssh_private_key_file",
        "ansible_private_key_file",
        "ansible_python_interpreter",
    }
)

# Parola sınıfı. T-204'te desteklenmez: Credential service yoktur ve parolayı
# ikinci bir geçici dosyaya kopyalamak, ani süreç/host çökmesinde (SIGKILL)
# `finally` çalışmayacağı için düz metin kalıntı bırakabilir.
# Bu sınıfta değişken **adı bile** dışarı verilmez.
PASSWORD_VARIABLES = frozenset({"ansible_password", "ansible_ssh_pass"})

# `auto` benzeri sabitler dışında interpreter mutlak yol olmalıdır.
_INTERPRETER_KEYWORDS = frozenset({"auto", "auto_silent", "auto_legacy", "auto_legacy_silent"})
_INTERPRETER_FORBIDDEN = ("$", "`", ";", "&", "|", ">", "<", "\n", "\r")

MAX_USER_LENGTH = 64
MIN_PORT = 1
MAX_PORT = 65535


class InventoryUnsafeError(ValidationFailedError):
    """Inventory, T-204'te güvenle çalıştırılamayacak bir tanım içeriyor.

    ``details`` yalnızca host adı (veya güvenli ``host_index``) ve değişken
    **adını** taşır; değer hiçbir koşulda yer almaz.
    """

    code = "ping_inventory_unsafe"


@dataclass(frozen=True)
class SnapshotPlan:
    """Doğrulanmış host'lar ve grup topolojisi.

    ``hosts`` yalnızca allowlist'ten geçmiş bağlantı alanlarını taşır; kaynak
    inventory'deki diğer değişkenler burada **yoktur**.
    """

    hosts: dict[str, dict[str, Any]]
    group_hosts: dict[str, tuple[str, ...]]
    group_children: dict[str, tuple[str, ...]]

    def host_names(self) -> tuple[str, ...]:
        """Ada göre sıralı host adları."""
        return tuple(sorted(self.hosts))


def build_snapshot_plan(
    host_variables: dict[str, dict[str, Any]],
    direct_hosts: dict[str, set[str]],
    children: dict[str, set[str]],
    *,
    key_roots: Sequence[Path],
) -> SnapshotPlan:
    """Ham parser çıktısından güvenli snapshot planı üretir.

    Host'lar ada göre sıralı işlenir; böylece birden fazla ihlal olduğunda
    hangisinin bildirileceği deterministiktir.

    Args:
        host_variables: Ham (maskelenmemiş) hostvar haritası.
        direct_hosts: Grup → doğrudan host kümesi.
        children: Grup → alt grup kümesi.
        key_roots: Private key dosyaları için izin verilen kökler.

    Returns:
        Doğrulanmış :class:`SnapshotPlan`.

    Raises:
        InventoryUnsafeError: Desteklenmeyen bir değişken, geçersiz bir SSH
            hedefi veya izin verilmeyen bir anahtar yolu bulunursa.
    """
    hosts: dict[str, dict[str, Any]] = {}
    for index, host_name in enumerate(sorted(host_variables)):
        hosts[host_name] = _build_host_entry(
            host_name,
            index,
            host_variables[host_name],
            key_roots=key_roots,
        )

    # Grup üyeliğinde adı geçen ama `_meta.hostvars` içinde bulunmayan host'lar
    # da hedeflenebilir; onlar da doğrulamadan geçmelidir.
    for group_members in direct_hosts.values():
        for host_name in sorted(group_members):
            if host_name not in hosts:
                hosts[host_name] = _build_host_entry(host_name, len(hosts), {}, key_roots=key_roots)

    group_hosts = {
        group: tuple(sorted(members))
        for group, members in direct_hosts.items()
        if group not in IMPLICIT_GROUPS
    }
    group_children = {
        group: tuple(sorted(names))
        for group, names in children.items()
        if group not in IMPLICIT_GROUPS
    }
    return SnapshotPlan(hosts=hosts, group_hosts=group_hosts, group_children=group_children)


def render_full_snapshot(plan: SnapshotPlan) -> str:
    """Snapshot A: bütün host'lar ve grup topolojisi.

    Grup yapısı korunur çünkü grup, kesişim ve dışlama limitleri ancak bu
    topoloji üzerinde çözülebilir. Değişkenler bir kez ``all.hosts`` altında
    tanımlanır; grup girdileri host'a yalnızca ``null`` değerle **referans**
    verir.
    """
    children: dict[str, Any] = {}
    for group in sorted(set(plan.group_hosts) | set(plan.group_children)):
        entry: dict[str, Any] = {}
        members = plan.group_hosts.get(group, ())
        if members:
            entry["hosts"] = dict.fromkeys(members)
        sub_groups = tuple(
            name for name in plan.group_children.get(group, ()) if name not in IMPLICIT_GROUPS
        )
        if sub_groups:
            entry["children"] = dict.fromkeys(sub_groups)
        children[group] = entry or None

    document: dict[str, Any] = {"all": {"hosts": dict(sorted(plan.hosts.items()))}}
    if children:
        document["all"]["children"] = children
    return _render(document)


def render_target_snapshot(plan: SnapshotPlan, targets: Sequence[str]) -> str:
    """Snapshot B: yalnızca çözülmüş hedef host'lar.

    Grup topolojisi taşınmaz: hedefler zaten çözülmüştür ve ping komutuna
    ``--limit`` **hiç verilmez**. Böylece ``@dosya`` yüzeyi Phase 2'de yapısal
    olarak bulunmaz.
    """
    hosts = {name: plan.hosts[name] for name in sorted(set(targets))}
    return _render({"all": {"hosts": hosts}})


def revalidate_snapshot_private_keys(snapshot_text: str, *, key_roots: Sequence[Path]) -> None:
    """Execution öncesinde snapshot private key yollarını yeniden doğrular."""
    for host, variables in _snapshot_hosts(snapshot_text).items():
        for name in ("ansible_ssh_private_key_file", "ansible_private_key_file"):
            if name in variables:
                _validate_key_path(variables[name], host=host, variable=name, key_roots=key_roots)


def snapshot_host_names(snapshot_text: str) -> tuple[str, ...]:
    """Dondurulmuş snapshot'taki hedef host adlarını ada göre sıralı döndürür.

    Adlar hem çıktı ayrıştırmasında çapa hem de API cevabında metindir; bu
    yüzden gösterim sözleşmesi (ADR-018 Karar 5) burada **yeniden** uygulanır.
    Snapshot'ı uygulama üretmiş olsa da execution anındaki doğrulama, preview
    anındaki doğrulamanın kalıcı garantisi sayılmaz.

    Raises:
        InventoryUnsafeError: Snapshot beklenen dar yapıda değilse veya bir host
            adı gösterim sözleşmesini karşılamıyorsa.
    """
    hosts = _snapshot_hosts(snapshot_text)
    if not hosts:
        raise InventoryUnsafeError("Ping snapshot yapısı geçersiz.")
    for host in hosts:
        if not is_valid_display_hostname(host):
            raise InventoryUnsafeError("Ping snapshot yapısı geçersiz.")
    return tuple(sorted(hosts))


def snapshot_connection_values(snapshot_text: str) -> tuple[str, ...]:
    """Snapshot'taki bağlantı **değerlerini** metin olarak döndürür.

    Bunlar adres, port, kullanıcı adı, private key yolu ve interpreter'dır;
    yani preview planının bilinçli olarak dışarı vermediği hostvar değerleri
    (MIMARI.md bölüm 7). Ansible'ın bağlantı hata metinleri bu değerleri
    tekrarladığı için çağıran taraf onları maskeler.

    Uzun değerler önce döner: kısa bir değerin uzun bir değerin içini
    parçalaması engellenir.
    """
    values: set[str] = set()
    for variables in _snapshot_hosts(snapshot_text).values():
        for value in variables.values():
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                text = str(value)
                if text:
                    values.add(text)
    return tuple(sorted(values, key=len, reverse=True))


def _snapshot_hosts(snapshot_text: str) -> dict[str, dict[str, Any]]:
    """Snapshot'ın ``all.hosts`` haritasını yapısal doğrulamayla döndürür."""
    try:
        document = json.loads(snapshot_text)
        hosts = document["all"]["hosts"]
    except (ValueError, KeyError, TypeError) as exc:
        raise InventoryUnsafeError("Ping snapshot yapısı geçersiz.") from exc
    if not isinstance(hosts, dict):
        raise InventoryUnsafeError("Ping snapshot yapısı geçersiz.")
    for host, variables in hosts.items():
        if not isinstance(host, str) or not isinstance(variables, dict):
            raise InventoryUnsafeError("Ping snapshot yapısı geçersiz.")
    return hosts


def _render(document: dict[str, Any]) -> str:
    """Snapshot belgesini kararlı sıralı JSON metnine çevirir.

    ``sort_keys`` deterministikliği sağlar: aynı girdi aynı baytları üretir ve
    snapshot digest'i karşılaştırılabilir olur.
    """
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _build_host_entry(
    host_name: str,
    host_index: int,
    variables: dict[str, Any],
    *,
    key_roots: Sequence[Path],
) -> dict[str, Any]:
    """Tek bir host için allowlist'ten geçmiş değişken sözlüğünü üretir."""
    safe_name = host_name if is_valid_display_hostname(host_name) else None
    if safe_name is None:
        # Ad geçersiz: hata detayında **basılmaz**, güvenli bir indeks verilir.
        _unsafe(
            "Inventory host adı desteklenen biçimde değil.",
            host_index=host_index,
            variable="inventory_hostname",
        )

    entry: dict[str, Any] = {}
    for name in sorted(variables):
        value = variables[name]
        if name in PASSWORD_VARIABLES:
            _unsafe_credential()
        if name == "ansible_connection":
            _validate_connection(value, host=safe_name)
            # Snapshot'a yazılmaz: varsayılan zaten `ssh`'tir ve tek bir
            # kanonik yol bırakmak, ileride başka bir plugin'in kazara
            # devreye girmesini imkânsız kılar.
            continue
        if name in ALLOWED_VARIABLES:
            entry[name] = _validate_allowed(name, value, host=safe_name, key_roots=key_roots)
            continue
        if name.startswith("ansible_"):
            # Bilinmeyen veya yasaklı bağlantı knob'u. Sessizce atmak, ping'i
            # kullanıcının gerçek çalıştırmasından farklı koşullara sokardı.
            _unsafe(
                "Inventory, ping için desteklenmeyen bir Ansible bağlantı değişkeni içeriyor.",
                host=safe_name,
                variable=name,
            )
        # `ansible_` ile başlamayan kullanıcı değişkenleri uygulama verisidir;
        # bağlantı semantiğini etkilemez ve sessizce kopyalanmaz.

    destination = effective_destination(safe_name, entry)
    if not is_valid_ssh_destination(destination):
        variable = "ansible_host" if "ansible_host" in entry else "inventory_hostname"
        _unsafe(
            "Inventory'deki SSH hedefi desteklenen biçimde değil.",
            host=safe_name,
            variable=variable,
        )
    return entry


def _validate_connection(value: Any, *, host: str) -> None:
    """``ansible_connection`` yalnızca tam olarak ``ssh`` ise kabul edilir."""
    if value != "ssh":
        _unsafe(
            "Inventory, ping için desteklenmeyen bir bağlantı türü tanımlıyor. "
            "T-204 yalnızca SSH bağlantısını destekler.",
            host=host,
            variable="ansible_connection",
        )


def _validate_allowed(
    name: str,
    value: Any,
    *,
    host: str,
    key_roots: Sequence[Path],
) -> Any:
    """Allowlist'teki bir değişkenin değerini doğrular."""
    if name == "ansible_host":
        if not isinstance(value, str) or not is_valid_ssh_destination(value):
            _unsafe(
                "Inventory'deki SSH hedefi desteklenen biçimde değil.",
                host=host,
                variable=name,
            )
        return value

    if name == "ansible_port":
        return _validate_port(value, host=host, variable=name)

    if name in {"ansible_user", "ansible_ssh_user"}:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_USER_LENGTH
            or not is_valid_display_hostname(value)
        ):
            # Kullanıcı adı `-o User="..."` argümanına gider; tırnak, boşluk ve
            # kontrol karakteri içermemelidir.
            _unsafe(
                "Inventory'deki SSH kullanıcı adı desteklenen biçimde değil.",
                host=host,
                variable=name,
            )
        return value

    if name in {"ansible_ssh_private_key_file", "ansible_private_key_file"}:
        return _validate_key_path(value, host=host, variable=name, key_roots=key_roots)

    return _validate_interpreter(value, host=host, variable=name)


def _validate_port(value: Any, *, host: str, variable: str) -> int:
    """Port değerini tam sayıya çevirir ve aralığını doğrular."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _unsafe("Inventory'deki SSH portu geçersiz.", host=host, variable=variable)
    try:
        port = int(value)
    except ValueError:
        port = 0
    if port < MIN_PORT or port > MAX_PORT:
        _unsafe("Inventory'deki SSH portu geçersiz.", host=host, variable=variable)
    return port


def _validate_key_path(
    value: Any,
    *,
    host: str,
    variable: str,
    key_roots: Sequence[Path],
) -> str:
    """Private key yolunu doğrular.

    Bu değer **controller üzerinde dosya okutur**. Doğrulanmadan geçirilirse
    ``/root/.ssh/id_rsa`` gibi bir yol denenerek varlık ve okunabilirlik
    bilgisi sızdırılabilir. Bu yüzden yol normalize edilir, izin verilen
    köklerin altında olmak zorundadır ve mevcut normal bir dosya olmalıdır.

    Yolun kendisi hata mesajına veya ``details``'e **yazılmaz**.
    """
    if not isinstance(value, str) or not value:
        _unsafe(
            "Inventory'deki private key tanımı desteklenen biçimde değil.",
            host=host,
            variable=variable,
        )
    try:
        resolved = normalize_filesystem_path(value)
        ensure_within_allowed_roots(resolved, key_roots)
        ensure_existing_file(resolved)
    except AppError as exc:
        raise InventoryUnsafeError(
            "Inventory'deki private key dosyası izin verilen secrets kökü altında bulunamadı.",
            details={"host": host, "variable": variable},
        ) from exc
    return str(resolved)


def _validate_interpreter(value: Any, *, host: str, variable: str) -> str:
    """Uzak Python interpreter değerini doğrular.

    ``ansible_connection`` yalnızca ``ssh`` olabildiği için bu değer **uzak**
    hostta çalışır, controller'da değil. Yine de kabuk metakarakteri içeren bir
    değer uzak tarafta beklenmedik bir komuta dönüşebilir; biçim daraltılır.
    """
    if not isinstance(value, str) or not value:
        _unsafe(
            "Inventory'deki Python interpreter tanımı geçersiz.",
            host=host,
            variable=variable,
        )
    if value in _INTERPRETER_KEYWORDS:
        return value
    if not value.startswith("/") or any(token in value for token in _INTERPRETER_FORBIDDEN):
        _unsafe(
            "Inventory'deki Python interpreter tanımı geçersiz.",
            host=host,
            variable=variable,
        )
    if any(char.isspace() for char in value):
        _unsafe(
            "Inventory'deki Python interpreter tanımı geçersiz.",
            host=host,
            variable=variable,
        )
    return value


def _unsafe(
    message: str,
    *,
    host: str | None = None,
    host_index: int | None = None,
    variable: str | None = None,
) -> NoReturn:
    """Değer sızdırmayan bir ``ping_inventory_unsafe`` üretir."""
    details: dict[str, Any] = {}
    if host is not None:
        details["host"] = host
    if host_index is not None:
        details["host_index"] = host_index
    if variable is not None:
        details["variable"] = variable
    raise InventoryUnsafeError(message, details=details or None)


def _unsafe_credential() -> NoReturn:
    """Parola sınıfı için ad ve değer sızdırmayan genel ret üretir.

    Bir secret'ın hangi host'ta ve hangi değişkende bulunduğunu bildirmek bile
    gereksiz bilgidir; mesaj bilinçli olarak yalnızca yöntemi anlatır.
    """
    raise InventoryUnsafeError(
        "Bu inventory desteklenmeyen bir credential yöntemi içeriyor. "
        "T-204'te yalnızca izin verilen secrets kökü altındaki doğrulanmış "
        "private key dosyası desteklenir."
    )
