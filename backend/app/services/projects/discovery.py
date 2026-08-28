"""Playbook keşfi (T-103).

Bu modül bir project kökü altındaki **çalıştırılabilir playbook adaylarını**
bulur. Bilinçli olarak dar kapsamlıdır:

- Ansible semantiği doğrulanmaz; bu T-402'nin (syntax-check) işidir.
- YAML parse edilmez, dolayısıyla yeni bir dependency gerekmez ve kötü
  hazırlanmış YAML (anchor bombası vb.) parser'ı yormaz.
- Aday olup olmadığı, açıklanabilir ve ucuz yapısal kurallarla belirlenir.

Modülün veritabanı bağımlılığı yoktur: kök dizini ve limitleri alır, sonuç
döndürür. Project kaydı, aktiflik ve allowlist kararları servis katmanındadır.
"""

from __future__ import annotations

import os
import re
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings
from app.services.security.paths import path_comparison_key

PLAYBOOK_SUFFIXES = frozenset({".yml", ".yaml"})

# Ansible role'ünün iç dizinleri. Bunlar **yalnızca** gerçek `roles/<role>/`
# yapısının altındayken dışlanır. Project'in başka bir yerindeki aynı adlı
# dizin (örn. `playbooks/tasks/`) meşru olabilir ve budanmaz.
ROLE_SUBDIRECTORIES = frozenset(
    {"tasks", "handlers", "defaults", "vars", "meta", "templates", "files"}
)

# Inventory değişken dizinleri. Ansible bunları hem project kökünde hem
# inventory yanında hem de playbook dizininin yanında arar; bu yüzden derinlik
# kısıtı yoktur. Tanım gereği değişken taşırlar, playbook taşımazlar.
INVENTORY_VARIABLE_DIRECTORIES = frozenset({"group_vars", "host_vars"})

# Inventory dizinleri yalnızca **project kökünde** dışlanır. Daha derindeki
# `inventory` adlı bir dizin bu projeye özgü olabilir; gereksiz yere budanmaz.
TOP_LEVEL_INVENTORY_DIRECTORIES = frozenset({"inventory", "inventories"})

# Eklenti/kod dizinleri. Ansible bunları yalnızca project kökünde ve
# `roles/<role>/` altında arar; kapsam bilinçli olarak o iki konumla sınırlıdır.
PLUGIN_DIRECTORIES = frozenset(
    {
        "library",
        "module_utils",
        "filter_plugins",
        "lookup_plugins",
        "action_plugins",
        "callback_plugins",
        "vars_plugins",
    }
)

# İçeriği hiçbir konumda playbook olmayan gürültü dizinleri.
NOISE_DIRECTORIES = frozenset({"__pycache__", "node_modules"})

# Uzantısı doğru olsa da playbook olmayan, iyi bilinen dosya adları.
EXCLUDED_FILE_NAMES = frozenset(
    {
        "requirements.yml",
        "requirements.yaml",
        "galaxy.yml",
        "galaxy.yaml",
        "ansible-navigator.yml",
        "ansible-navigator.yaml",
        "molecule.yml",
        "molecule.yaml",
        "inventory.yml",
        "inventory.yaml",
        "hosts.yml",
        "hosts.yaml",
        "vault.yml",
        "vault.yaml",
    }
)

# Playbook'un ilk anlamlı satırı bir üst seviye dizi öğesi olmalıdır ("- ").
_BLANK_OR_COMMENT = re.compile(r"^\s*(?:#.*)?$")
_DOCUMENT_MARKER = re.compile(r"^(?:---|\.\.\.)\s*(?:#.*)?$")
_SEQUENCE_ITEM = re.compile(r"^-(?:\s|$)")

# Play seviyesinde bulunması beklenen anahtarlar.
_PLAY_KEY = re.compile(
    r"^\s*(?:-\s+)?(?:hosts|import_playbook|ansible\.builtin\.import_playbook)\s*:",
    re.MULTILINE,
)

# Dizin için okuma amaçlı bir dosya tanıtıcısı açılabiliyor mu. POSIX'te
# `O_DIRECTORY` vardır; Windows'ta yoktur ve dizin `os.open` ile açılamaz.
DIRECTORY_REFERENCE_SUPPORTED = hasattr(os, "O_DIRECTORY")


class ScanRootUnavailableError(Exception):
    """Tarama kökü tarama başında veya sırasında kullanılamaz hâle geldi.

    Servis katmanı bunu domain hatasına çevirir; modül bilinçli olarak
    HTTP veya project semantiğini bilmez.
    """


@dataclass(frozen=True)
class ScanLimits:
    """Kötü hazırlanmış veya çok büyük dizin ağaçlarına karşı keşif sınırları.

    Sınırlar hata değil **kırpma** üretir: sonuç ``truncated=True`` ile
    işaretlenir, böylece kullanıcı listenin eksik olabileceğini görür.
    """

    max_depth: int = 12
    max_entries: int = 20_000
    max_results: int = 500
    read_bytes: int = 65_536

    @classmethod
    def from_settings(cls, settings: Settings) -> ScanLimits:
        """Ayarlardan limit seti üretir."""
        return cls(
            max_depth=settings.playbook_scan_max_depth,
            max_entries=settings.playbook_scan_max_entries,
            max_results=settings.playbook_scan_max_results,
            read_bytes=settings.playbook_scan_read_bytes,
        )


@dataclass(frozen=True)
class DiscoveredPlaybook:
    """Bulunmuş bir playbook adayı.

    ``path`` her zaman project köküne **göreli** ve POSIX ayraçlıdır;
    sunucudaki absolute yol dışarı verilmez (GUVENLIK.md bölüm 3).
    """

    path: str
    name: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True)
class PlaybookScanResult:
    """Bir keşif çalışmasının sonucu."""

    project_id: int
    playbooks: list[DiscoveredPlaybook]
    skipped_unreadable_files: int
    skipped_unreadable_directories: int
    truncated: bool
    scanned_at: datetime


@dataclass(frozen=True)
class _RootIdentity:
    """Tarama kökünün dosya sistemi kimliği.

    Path metni tek başına yetmez: aynı yol altındaki dizin silinip yerine
    yenisi konabilir. ``st_dev``/``st_ino`` ikilisi bunu ayırt eder ve
    Windows NTFS dâhil desteklenen platformlarda anlamlıdır.

    Kimliğin **nasıl okunduğu** kadar önemlidir: yalnızca path üzerinden
    alınan bir başlangıç kimliği güvenilir değildir, çünkü silinen bir inode
    anında yeniden kullanılabilir (ext4'te tipik davranıştır). Bu yüzden
    başlangıç kimliği açık bir dizin referansı üzerinden ``fstat`` ile okunur;
    bkz. :func:`_root_anchor`.
    """

    device: int
    inode: int
    key: str

    def matches(self, other: _RootIdentity) -> bool:
        """İki kimliğin aynı dosya sistemi nesnesini gösterip göstermediği.

        ``st_ino`` üretmeyen (0 döndüren) bir dosya sisteminde kimlik
        karşılaştırması anlamsızdır; o durumda kanonik path karşılaştırmasına
        düşülür. Bu, korumayı zayıflatan bilinen tek durumdur ve
        ``inode_supported`` ile dışarıdan görülebilir.
        """
        if self.key != other.key:
            return False
        if not self.inode_supported or not other.inode_supported:
            return True
        return (self.device, self.inode) == (other.device, other.inode)

    @property
    def inode_supported(self) -> bool:
        """Platform bu nesne için gerçek bir inode üretebildi mi."""
        return self.inode != 0


def looks_like_playbook(text: str) -> bool:
    """Dosya içeriğinin playbook olma ihtimalini yapısal kurallarla değerlendirir.

    İki kural birlikte aranır:

    1. Yorum, boş satır ve ``---``/``...`` belge işaretleri atlandıktan sonra
       ilk anlamlı satır bir üst seviye dizi öğesi olmalıdır. Playbook üst
       seviyede play listesidir; ``group_vars`` ve ``defaults`` gibi dosyalar
       ise mapping'dir ve bu kuralda elenir.
    2. İçerikte play seviyesinde ``hosts:`` veya ``import_playbook:`` anahtarı
       geçmelidir. Role task dosyaları dizi olsa da bu anahtarları taşımaz.

    Bu, uzantıya güvenmemek için yeterli ve ucuz bir ayırt ediciliktir; Ansible
    semantiğinin doğrulaması değildir (T-402). İçerik sezgisi tek başına
    güvenlik sınırı da değildir: yapısal dizin kuralları ondan bağımsız çalışır.

    Args:
        text: Dosyanın baştan okunmuş metni.

    Returns:
        Aday playbook ise ``True``.
    """
    for line in text.splitlines():
        if _BLANK_OR_COMMENT.match(line) or _DOCUMENT_MARKER.match(line):
            continue
        if not _SEQUENCE_ITEM.match(line):
            return False
        break
    else:
        # Anlamlı satır yok: boş veya yalnızca yorumdan oluşan dosya.
        return False

    return _PLAY_KEY.search(text) is not None


def is_role_subdirectory(parts: tuple[str, ...]) -> bool:
    """Verilen göreli dizin yolu gerçek bir ``roles/<role>/<subdir>`` mi.

    ``roles`` her derinlikte olabilir (project kökü, ``ansible_collections``
    altı, ``playbooks/roles`` vb.); önemli olan yapının kendisidir.

    Örnekler::

        ("roles", "nginx", "tasks")            -> True
        ("playbooks", "roles", "web", "vars")  -> True
        ("playbooks", "tasks")                 -> False
        ("roles", "tasks")                     -> False   (adı "tasks" olan role)
    """
    return len(parts) >= 3 and parts[-3].lower() == "roles"


def is_excluded_directory(parts: tuple[str, ...]) -> bool:
    """Göreli yolu verilen dizinin taranmayacağını belirler.

    Kurallar bilinçli olarak **konuma duyarlıdır**; aynı adı taşıyan her dizin
    körlemesine budanmaz:

    ==============================  ====================================
    Dizin adı                       Nerede dışlanır
    ==============================  ====================================
    ``tasks``, ``handlers``, ...    Yalnızca ``roles/<role>/`` altında
    ``group_vars``, ``host_vars``   Her derinlikte
    ``inventory``, ``inventories``  Yalnızca project kökünde
    Eklenti dizinleri               Project kökü veya ``roles/<role>/``
    ``__pycache__``, ...            Her derinlikte
    ``.`` ile başlayanlar           Her derinlikte
    ==============================  ====================================

    Args:
        parts: Project köküne göreli yol bileşenleri.

    Returns:
        Dizin budanacaksa ``True``.
    """
    if not parts:
        return False

    name = parts[-1].lower()
    depth = len(parts)

    if parts[-1].startswith("."):
        return True
    if name in NOISE_DIRECTORIES:
        return True
    if name in INVENTORY_VARIABLE_DIRECTORIES:
        return True
    if name in TOP_LEVEL_INVENTORY_DIRECTORIES:
        return depth == 1
    if name in ROLE_SUBDIRECTORIES:
        return is_role_subdirectory(parts)
    if name in PLUGIN_DIRECTORIES:
        return depth == 1 or is_role_subdirectory(parts)
    return False


def path_has_excluded_directory(parts: tuple[str, ...]) -> bool:
    """Yol zincirindeki **herhangi bir** dizin bileşeni dışlanıyor mu.

    Tek bir seviyeye bakmak yetmez: bir bağlantı, dışlanmış bir dizinin
    *içine* de yönelmiş olabilir.
    """
    return any(is_excluded_directory(parts[:index]) for index in range(1, len(parts) + 1))


def discover_playbooks(
    project_root: Path,
    *,
    project_id: int,
    limits: ScanLimits,
) -> PlaybookScanResult:
    """Project kökü altındaki playbook adaylarını bulur.

    **Symlink ve junction yaklaşımı.** Her dizin girdisi koşulsuz ``resolve()``
    edilir ve gerçek hedefinin project kökü içinde kaldığı doğrulanır. Bu,
    ``is_symlink()`` sorgusuna güvenmekten daha sağlamdır; Windows
    junction'ları ``os.path.islink`` tarafından bağlantı sayılmaz ama
    ``resolve()`` onları da çözer. Kök dışına çıkan bağlantı ne takip edilir ne
    de listelenir.

    **Dışlama kararı hem görünen hem gerçek yol üzerinde verilir.** Aksi hâlde
    ``playbooks/alias -> roles/demo/tasks`` gibi bir bağlantı, görünen yolu
    masum olduğu için role içeriğini playbook diye sunardı.

    **Döngü ve tekrar koruması.** Girilen her dizinin çözülmüş yolu bir kümede
    tutulur; aynı gerçek dizine ikinci kez girilmez. Bu hem symlink
    döngülerini sonlandırır hem de bir bağlantı çiftliğinin sonuçları
    çoğaltmasını (ve ``max_results`` sınırını doldurmasını) engeller. Aynı
    gerçek dizine iki yoldan ulaşılabiliyorsa deterministik sırada ilk görülen
    yol raporlanır.

    **Kök, tarama boyunca açık bir dizin referansıyla sabitlenir.** Kimlik
    karşılaştırmasının anlamlı olması için başlangıç kimliğinin okunduğu nesne
    tarama süresince yaşamalıdır; ayrıntı :func:`_root_anchor` içindedir.

    Args:
        project_root: Normalize edilmiş, doğrulanmış project kökü.
        project_id: Sonuca yazılacak project kaydının kimliği.
        limits: Keşif sınırları.

    Returns:
        Deterministik sıralı ``PlaybookScanResult``.

    Raises:
        ScanRootUnavailableError: Kök taranamıyorsa veya tarama sırasında
            silinmiş/değiştirilmişse.
    """
    try:
        root_real = project_root.resolve(strict=True)
    except OSError as exc:
        raise ScanRootUnavailableError("Project kökü çözümlenemedi.") from exc

    with _root_anchor(root_real) as root_identity:
        result = _scan_tree(root_real, project_id=project_id, limits=limits)
        # Doğrulama referans **hâlâ açıkken** yapılır. Referans bırakıldığı an
        # inode serbest kalır ve aradaki boşlukta yeniden kullanılabilir;
        # o durumda karşılaştırma yine yanıltıcı olurdu.
        _reassert_root(project_root, root_identity)
    return result


def _scan_tree(
    root_real: Path,
    *,
    project_id: int,
    limits: ScanLimits,
) -> PlaybookScanResult:
    """Kök altındaki ağacı gezip aday playbook'ları toplar.

    Kök kimliği doğrulaması bu fonksiyonun sorumluluğu **değildir**; çağıran
    onu açık bir kök referansı altında yapar.

    Raises:
        ScanRootUnavailableError: Kökün kendisi taranamıyorsa.
    """
    found: list[DiscoveredPlaybook] = []
    skipped_files = 0
    skipped_directories = 0
    truncated = False
    entries_seen = 0
    visited_directories = {path_comparison_key(root_real)}
    queue: deque[tuple[Path, int]] = deque([(root_real, 0)])

    while queue:
        current, depth = queue.popleft()
        try:
            with os.scandir(current) as scan:
                entries = sorted(scan, key=lambda item: item.name)
        except OSError as exc:
            if current == root_real:
                raise ScanRootUnavailableError("Project kökü taranamadı.") from exc
            # Tarama sırasında kaybolan veya okunamayan alt dizin: keşfi
            # durdurmaz, sayılır.
            skipped_directories += 1
            continue

        for entry in entries:
            entries_seen += 1
            if entries_seen > limits.max_entries:
                truncated = True
                queue.clear()
                break

            child = Path(entry.path)
            try:
                real = child.resolve(strict=True)
            except OSError:
                # Kırık bağlantı veya yarışan silme.
                continue
            if not _is_inside(real, root_real):
                # Kök dışına çıkan symlink/junction: takip edilmez, listelenmez.
                continue

            logical_parts = child.relative_to(root_real).parts
            real_parts = real.relative_to(root_real).parts

            try:
                is_directory = entry.is_dir(follow_symlinks=True)
                is_regular_file = entry.is_file(follow_symlinks=True)
            except OSError:
                skipped_files += 1
                continue

            if is_directory:
                # Karar hem görünen hem çözülmüş yol üzerinde verilir; bağlantı
                # ile dışlanmış bir dizine takma ad verilemez.
                if is_excluded_directory(logical_parts) or path_has_excluded_directory(real_parts):
                    continue
                if depth + 1 > limits.max_depth:
                    truncated = True
                    continue
                directory_key = path_comparison_key(real)
                if directory_key in visited_directories:
                    continue
                visited_directories.add(directory_key)
                queue.append((child, depth + 1))
                continue

            if not is_regular_file:
                continue

            # Dosya bağlantısı da dışlanmış bir dizini hedefleyebilir.
            if path_has_excluded_directory(real_parts[:-1]):
                continue

            try:
                playbook = _evaluate_file(child, real, root_real, limits.read_bytes)
            except _UnreadableFileError:
                skipped_files += 1
                continue
            if playbook is None:
                continue

            if len(found) >= limits.max_results:
                truncated = True
                queue.clear()
                break
            found.append(playbook)

    return PlaybookScanResult(
        project_id=project_id,
        playbooks=sorted(found, key=lambda item: item.path),
        skipped_unreadable_files=skipped_files,
        skipped_unreadable_directories=skipped_directories,
        truncated=truncated,
        scanned_at=datetime.now(UTC),
    )


class _UnreadableFileError(Exception):
    """Aday dosya okunamadı veya metin olarak çözülemedi."""


def _evaluate_file(
    child: Path,
    real: Path,
    root_real: Path,
    read_bytes: int,
) -> DiscoveredPlaybook | None:
    """Tek bir dosyayı değerlendirir.

    Returns:
        Aday ise ``DiscoveredPlaybook``, playbook değilse ``None``.

    Raises:
        _UnreadableFileError: Dosya okunamadıysa veya UTF-8 metin değilse.
    """
    if child.suffix.lower() not in PLAYBOOK_SUFFIXES:
        return None
    if child.name.lower() in EXCLUDED_FILE_NAMES:
        return None

    try:
        stat_result = real.stat()
        with real.open("rb") as handle:
            head = handle.read(read_bytes)
        text = head.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Okunamayan veya metin olmayan aday listeye **girmez** ve sayılır;
        # tek bir bozuk dosya bütün keşfi düşürmez.
        raise _UnreadableFileError(child.name) from exc

    if not looks_like_playbook(text):
        return None

    relative = child.relative_to(root_real).as_posix()
    return DiscoveredPlaybook(
        path=relative,
        name=relative,
        size_bytes=stat_result.st_size,
        modified_at=datetime.fromtimestamp(stat_result.st_mtime, UTC),
    )


def _is_inside(candidate: Path, root: Path) -> bool:
    """``candidate`` kökün kendisi mi yoksa altında mı."""
    return candidate == root or candidate.is_relative_to(root)


@contextmanager
def _root_anchor(root_real: Path) -> Iterator[_RootIdentity]:
    """Tarama süresince kökü açık tutar ve başlangıç kimliğini oradan okur.

    Açık referans yalnızca kimliği okumak için değil, **inode'u sabitlemek**
    için tutulur. POSIX'te açık bir tanıtıcısı olan inode, dizin silinse bile
    serbest bırakılmaz; dolayısıyla aynı path'e yeni bir dizin konduğunda dosya
    sistemi zorunlu olarak **başka** bir inode tahsis eder. Değişim böylece
    path üzerinden yeniden okunan kimlikle güvenilir biçimde ayırt edilir.

    Başlangıç kimliğini yalnızca ``stat(path)`` ile okumak bu garantiyi vermez:
    silinen inode anında yeniden kullanılabilir (ext4'te gözlenen davranıştır)
    ve kök değişmiş olmasına rağmen iki kimlik eşit çıkar.

    Yields:
        Kökün açık referans üzerinden okunmuş kimliği.

    Raises:
        ScanRootUnavailableError: Kök açılamıyorsa veya kimliği okunamıyorsa.
    """
    if not DIRECTORY_REFERENCE_SUPPORTED:
        # Windows: dizin `os.open` ile açılamaz, bu yüzden inode sabitlenemez ve
        # başlangıç kimliği path üzerinden okunur. Koruma orada zayıftır: kök
        # silinip yeniden yaratılırsa NTFS aynı dosya kimliğini geri verebilir.
        # Bilinçli bir platform sınırıdır; ürünün hedef çalışma ortamı
        # Linux/macOS'tur (ADR-017'deki aynı sınır).
        try:
            yield _read_root_identity(root_real)
        except OSError as exc:
            raise ScanRootUnavailableError("Project kökü çözümlenemedi.") from exc
        return

    try:
        descriptor = os.open(root_real, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise ScanRootUnavailableError("Project kökü açılamadı.") from exc

    try:
        stat_result = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise ScanRootUnavailableError("Project kökünün kimliği okunamadı.") from exc

    try:
        yield _identity_from_stat(stat_result, root_real)
    finally:
        # Tarama hata verse de referans bırakılır; açık tanıtıcı sızdırmak
        # inode'u süresiz sabitler.
        os.close(descriptor)


def _read_root_identity(root_real: Path) -> _RootIdentity:
    """Kökün dosya sistemi kimliğini path üzerinden okur."""
    return _identity_from_stat(root_real.stat(), root_real)


def _identity_from_stat(stat_result: os.stat_result, root_real: Path) -> _RootIdentity:
    """``stat`` sonucunu kanonik path anahtarıyla birlikte kimliğe çevirir."""
    return _RootIdentity(
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        key=path_comparison_key(root_real),
    )


def _reassert_root(project_root: Path, expected: _RootIdentity) -> None:
    """Tarama bittikten sonra kökün hâlâ **aynı nesne** olduğunu doğrular.

    Path metnini karşılaştırmak yetmez: aynı yol altındaki dizin silinip
    yerine yenisi konabilir; o durumda tarama sonucu artık başka bir ağacı
    anlatır. Bu yüzden ``st_dev``/``st_ino`` kimliği karşılaştırılır.

    ``expected`` :func:`_root_anchor` tarafından açık bir referans üzerinden
    okunmuş olmalıdır; çağrı da o referans hâlâ açıkken yapılmalıdır. Aksi
    hâlde karşılaştırma inode yeniden kullanımına açıktır.

    Raises:
        ScanRootUnavailableError: Kök yok olduysa, dizin olmaktan çıktıysa,
            başka bir yola çözülüyorsa veya farklı bir dosya sistemi nesnesine
            dönüştüyse.
    """
    try:
        current = project_root.resolve(strict=True)
        if not current.is_dir():
            raise ScanRootUnavailableError("Project kökü tarama sırasında dizin olmaktan çıktı.")
        actual = _read_root_identity(current)
    except OSError as exc:
        raise ScanRootUnavailableError("Project kökü tarama sırasında kayboldu.") from exc

    if not expected.matches(actual):
        raise ScanRootUnavailableError("Project kökü tarama sırasında değişti.")
