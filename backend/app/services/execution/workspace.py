"""Dondurulmuş execution workspace'i (R1-V2).

Bir planı onaya hazırlamak, o an gördüğün içeriği **sabitlemek** demektir.
Kullanıcının okuduğu plan ile ileride çalıştırılacak içerik arasındaki bağ,
ancak içerik kopyalanıp dondurulursa kurulabilir: aksi hâlde onay ile
çalıştırma arasında project ağacı değişebilir ve onaylanan şeyden başka bir şey
çalışır (ADR-021 Kapı C).

Bu modül **hiçbir şey çalıştırmaz**: yalnızca kopyalar, doğrular, özetler ve
temizler.

Güvenlik sözleşmesi:

- Bütün dosya sistemi işlemleri **descriptor-relative**'dir (``dir_fd`` +
  ``O_NOFOLLOW``). Path metnini çözüp ardından normal ``open`` yapmak güvenlik
  kanıtı sayılmaz: çözme ile kullanma arasındaki pencerede girdi değiştirilebilir.
- Her girdi önce ``lstat`` ile sınıflandırılır, dosya açıldıktan sonra
  ``fstat`` ile **yeniden** doğrulanır; arada yapılan bir değiş-tokuş yakalanır.
- Symlink fail-closed reddedilir. FIFO, socket, device ve diğer özel dosya
  türleri de reddedilir: kopyalamak sürecin bloke olmasına veya cihaz okumasına
  yol açardı.
- Kök ve bütün dizinler 0700, bütün normal dosyalar 0600 olur. Kaynaktaki izin
  bitleri taşınmaz; execute biti dondurulmuş içeriğe **hiç** girmez.
- İçerik önce ``staging-<32 hex>`` altında hazırlanır, fsync edilir ve ancak
  sonra atomik ``rename`` ile opaque workspace adına yayımlanır. Yarım bir
  kopya hiçbir zaman geçerli bir workspace olarak görünmez.
- Hata durumunda staging kalıntısı temizlenir.
- Sınırlar okuma sırasında uygulanır ve aşıldığında **hata** üretir; sessiz
  truncation yoktur.

Manifest, kopyanın **gerçekte yazdığı** baytlar üzerinden hesaplanır. Kaynak
kopyalama sırasında değişse bile digest, dondurulmuş çıktının kendisini anlatır;
plan hiçbir zaman özgün ağacın manifest'ine bağlanmaz.

**Platform sınırı.** Güvenli primitive'ler yalnızca POSIX'te vardır. Bulunmazsa
zayıf bir fallback ile devam **edilmez**; hazırlama fail-closed biçimde
``execution_workspace_unavailable`` üretir (ADR-017'deki "Windows control node
desteklenmez" sınırıyla tutarlı).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.errors import AppError

# Dondurulmuş içeriğin düzeni.
PROJECT_DIRNAME = "project"
INVENTORY_DIRNAME = "inventory"
# JSON metni `.yml` uzantısıyla yazılır: JSON, YAML'ın alt kümesidir ve
# Ansible'ın `yaml` inventory eklentisi onu ayrıştırır (ping snapshot'ıyla aynı
# yaklaşım).
INVENTORY_FILENAME = "hosts.yml"
MANIFEST_FILENAME = "manifest.json"

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600

# Provisional sınırlar (R1-V2). Ölçüme değil, "makul bir Ansible project'i bu
# sınırların çok altındadır" kabulüne dayanır; bu yüzden görünür sabitlerdir ve
# ileride ayara taşınabilir. Aşıldığında kopya **reddedilir**: eksik bir ağacı
# dondurup planı onaya sunmak, kullanıcıya olmayan bir içeriği onaylatırdı.
MAX_WORKSPACE_ENTRIES = 10_000
MAX_WORKSPACE_BYTES = 100 * 1024 * 1024
MAX_WORKSPACE_DEPTH = 32

COPY_CHUNK_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_FROZEN_INVENTORY_BYTES = 5_000_000

MANIFEST_SCHEMA_VERSION = 1

STAGING_PREFIX = "staging-"

# Bakım imleci. Adı ne workspace ne de staging desenine uyar: listelemeler onu
# görmez, `remove_workspace` ona dokunamaz. İçeriği zaten aynı 0700 kökte dizin
# adı olarak duran bir workspace kimliğinden ibarettir; token, path veya
# kullanıcı verisi taşımaz.
MAINTENANCE_CURSOR_FILENAME = ".maintenance-cursor"
_CURSOR_TEMP_FILENAME = ".maintenance-cursor.tmp"
CURSOR_SCHEMA_VERSION = 1
MAX_CURSOR_BYTES = 4096

_WORKSPACE_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_STAGING_PATTERN = re.compile(r"^staging-[0-9a-f]{32}$")

# Descriptor-relative çalışmak için gereken syscall'lar. Biri bile yoksa güvenli
# akış kurulamaz ve zayıf bir fallback'e düşülmez.
_REQUIRED_DIR_FD_FUNCTIONS = (os.open, os.mkdir, os.rename, os.stat, os.unlink, os.rmdir)


class WorkspaceUnavailableError(AppError):
    """Dondurulmuş workspace hazırlanamadı, okunamadı veya temizlenemedi.

    Altyapı hatasıdır; kullanıcıya dosya sistemi ayrıntısı gösterilmez.
    """

    status_code = 500
    code = "execution_workspace_unavailable"


class WorkspaceIntegrityError(AppError):
    """Dondurulmuş workspace, dondurulduğu andaki içerikten farklı.

    Eksik, fazla, değişmiş, izni değiştirilmiş veya symlink'e dönüştürülmüş bir
    girdi; manifest dosyasının kendisinde yapılan bir değişiklik; ya da yeniden
    hesaplanan digest'in kayıttaki digest ile eşleşmemesi bu hatayı üretir.

    ``details`` yalnızca makine tarafından okunabilir bir sebep taşır: hangi
    dosyanın, hangi digest'in veya hangi yolun sorunlu olduğu **yazılmaz**.
    Aksi hâlde doğrulama, dondurulmuş içeriği dışarıdan sorgulanabilir bir
    sonda hâline gelirdi.
    """

    status_code = 409
    code = "execution_workspace_modified"


class WorkspaceUnsafeError(AppError):
    """Project ağacı güvenle dondurulamıyor.

    Girdi biçimsel olarak geçerlidir; reddedilme sebebi **kaydın dünyasıdır**
    (symlink, özel dosya, sınır aşımı), bu yüzden 422 değil 409 döner.

    ``details["reason"]`` makine tarafından okunabilir sebebi taşır. Hangi
    dosyanın sorunlu olduğu bilinçli olarak **yazılmaz**: dizin içeriğini
    dışarıdan sorgulanabilir bir sonda hâline getirmemek için.
    """

    status_code = 409
    code = "execution_workspace_unsafe"


class _RootMissingError(Exception):
    """Workspace kökü henüz yok. Yalnızca modül içinde kullanılır."""


@dataclass(frozen=True)
class ManifestEntry:
    """Dondurulmuş içerikteki tek bir girdi.

    ``path`` workspace köküne göreli POSIX yoldur; sunucudaki absolute yol
    manifest'e de girmez.
    """

    path: str
    entry_type: str
    mode: int
    sha256: str | None


@dataclass(frozen=True)
class FrozenWorkspace:
    """Yayımlanmış bir dondurulmuş workspace'in özeti.

    Absolute path taşımaz: kök her zaman çalışma anındaki ayarlardan türetilir.
    """

    workspace_id: str
    manifest_digest: str
    entry_count: int
    total_bytes: int
    frozen_at: datetime


class _Budget:
    """Kopyalama sırasında uygulanan girdi ve bayt sınırı.

    Sınır **iş yapılırken** uygulanır: bayt sayacı her okunan parçada artar, bu
    yüzden 100 MB'lık bir dosyayı önce diske yazıp sonra fark etmek gerekmez.
    """

    def __init__(self, *, max_entries: int, max_bytes: int) -> None:
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self.entries = 0
        self.total_bytes = 0

    def count_entry(self) -> None:
        self.entries += 1
        if self.entries > self._max_entries:
            raise WorkspaceUnsafeError(
                "Project ağacı dondurulamayacak kadar çok girdi içeriyor.",
                details={"reason": "too_many_entries", "limit": self._max_entries},
            )

    def count_bytes(self, size: int) -> None:
        self.total_bytes += size
        if self.total_bytes > self._max_bytes:
            raise WorkspaceUnsafeError(
                "Project ağacı dondurulamayacak kadar büyük.",
                details={"reason": "too_large", "limit": self._max_bytes},
            )


def secure_filesystem_available() -> bool:
    """Descriptor-relative güvenli primitive'ler bu platformda var mı."""
    if os.name != "posix":
        return False
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        return False
    if os.listdir not in os.supports_fd:
        return False
    return all(function in os.supports_dir_fd for function in _REQUIRED_DIR_FD_FUNCTIONS)


def freeze_workspace(
    root: Path,
    *,
    project_root: Path,
    inventory_snapshot: str,
    now: datetime | None = None,
) -> FrozenWorkspace:
    """Project ağacını ve normalize inventory snapshot'ını dondurur.

    Args:
        root: ``app-data/execution-plans`` kökü.
        project_root: Doğrulanmış, normalize edilmiş project dizini.
        inventory_snapshot: Uygulamanın kendi ürettiği güvenli inventory
            snapshot metni. Ham inventory dosyası **kopyalanmaz**.
        now: Test edilebilirlik için dondurma anı.

    Returns:
        Yayımlanmış :class:`FrozenWorkspace`.

    Raises:
        WorkspaceUnsafeError: Ağaçta symlink, özel dosya veya sınır aşımı varsa.
        WorkspaceUnavailableError: Kopya yazılamazsa veya güvenli primitive'ler
            bu platformda yoksa.
    """
    _require_secure_filesystem()
    moment = now or datetime.now(UTC)
    workspace_id = str(uuid.uuid4())
    budget = _Budget(max_entries=MAX_WORKSPACE_ENTRIES, max_bytes=MAX_WORKSPACE_BYTES)

    with _root_descriptor(root, create=True) as root_fd:
        digest = _stage_and_publish(
            root_fd,
            workspace_id=workspace_id,
            project_root=project_root,
            inventory_snapshot=inventory_snapshot,
            budget=budget,
            moment=moment,
        )

    return FrozenWorkspace(
        workspace_id=workspace_id,
        manifest_digest=digest,
        entry_count=budget.entries,
        total_bytes=budget.total_bytes,
        frozen_at=moment,
    )


def _stage_and_publish(
    root_fd: int,
    *,
    workspace_id: str,
    project_root: Path,
    inventory_snapshot: str,
    budget: _Budget,
    moment: datetime,
) -> str:
    """İçeriği staging'de hazırlar ve atomik ``rename`` ile yayımlar."""
    staging = f"{STAGING_PREFIX}{secrets.token_hex(16)}"
    try:
        os.mkdir(staging, DIRECTORY_MODE, dir_fd=root_fd)
    except OSError as exc:
        raise WorkspaceUnavailableError("Execution workspace hazırlanamadı.") from exc
    try:
        with _child_directory(root_fd, staging) as staging_fd:
            # `mkdir` mode'u umask ile maskelenir; izin bitleri claim anında
            # yeniden doğrulandığı için burada açıkça sabitlenir.
            os.fchmod(staging_fd, DIRECTORY_MODE)
            entries = _freeze_contents(
                staging_fd,
                project_root=project_root,
                inventory_snapshot=inventory_snapshot,
                budget=budget,
            )
            digest = _manifest_digest(entries)
            _write_private_file(
                staging_fd,
                MANIFEST_FILENAME,
                _render_manifest(entries, digest=digest, frozen_at=moment),
            )
            os.fsync(staging_fd)
        os.rename(staging, workspace_id, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
    except BaseException:
        # Yarım kalan hazırlık diskte bırakılmaz. Temizlik hatası asıl hatayı
        # gölgelemesin diye yutulur; kalıntı yine de reconciliation tarafından
        # toplanır.
        with contextlib.suppress(OSError, WorkspaceUnavailableError):
            _remove_child_tree(root_fd, staging)
        raise
    return digest


def workspace_project_root(root: Path, workspace_id: str) -> Path:
    """Dondurulmuş project ağacının yolu.

    Bu path yalnızca **uygulamanın kendi ürettiği**, 0700 kök altında duran ve
    symlink içermediği kopyalama sırasında kanıtlanmış bir ağaca işaret eder;
    API'ye hiçbir zaman verilmez.

    Raises:
        WorkspaceUnavailableError: Workspace adı geçersizse veya dizin güvenli
            biçimde açılamıyorsa (fail-closed).
    """
    _require_workspace_id(workspace_id)
    with _open_workspace(root, workspace_id) as workspace_fd:
        with _child_directory(workspace_fd, PROJECT_DIRNAME):
            pass
    return root / workspace_id / PROJECT_DIRNAME


def workspace_inventory_path(root: Path, workspace_id: str) -> Path:
    """Dondurulmuş inventory snapshot dosyasının yolu.

    Sözleşme :func:`workspace_project_root` ile aynıdır: yol çağırandan gelen
    bir metinden değil, kök + opaque ``workspace_id`` + sabit adlardan
    (``inventory/hosts.yml``) türetilir. Dönmeden önce girdinin gerçekten
    symlink olmayan normal bir dosya olduğu descriptor-relative doğrulanır; bu
    yol API'ye hiçbir zaman verilmez.

    Raises:
        WorkspaceUnavailableError: Workspace adı geçersizse, dizin güvenli
            biçimde açılamıyorsa veya dondurulmuş inventory normal bir dosya
            olarak bulunamıyorsa (fail-closed).
    """
    _require_workspace_id(workspace_id)
    with _open_workspace(root, workspace_id) as workspace_fd:
        try:
            with _child_directory(workspace_fd, INVENTORY_DIRNAME) as inventory_fd:
                _require_regular_entry(inventory_fd, INVENTORY_FILENAME)
        except OSError as exc:
            raise WorkspaceUnavailableError("Dondurulmuş inventory bulunamadı.") from exc
    return root / workspace_id / INVENTORY_DIRNAME / INVENTORY_FILENAME


def _require_regular_entry(dir_fd: int, name: str) -> None:
    """Girdinin symlink izlenmeden açılabilen **normal** bir dosya olduğunu kanıtlar.

    ``O_NOFOLLOW`` symlink'i ``ELOOP`` ile düşürür, ``O_NONBLOCK`` yerine
    konmuş bir FIFO'nun açmayı bloke etmesini engeller ve ``fstat`` açılan
    nesnenin gerçekten normal dosya olduğunu doğrular.
    """
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("dondurulmuş inventory normal dosya değil")
    finally:
        os.close(descriptor)


def read_frozen_inventory(root: Path, workspace_id: str) -> str:
    """Dondurulmuş inventory snapshot metnini okur.

    Özgün inventory dosyası bu noktadan sonra **hiç açılmaz**: plan da manifest
    de yalnızca dondurulmuş içerikten üretilir.
    """
    _require_workspace_id(workspace_id)
    with _open_workspace(root, workspace_id) as workspace_fd:
        try:
            with _child_directory(workspace_fd, INVENTORY_DIRNAME) as inventory_fd:
                return _read_private_file(
                    inventory_fd, INVENTORY_FILENAME, MAX_FROZEN_INVENTORY_BYTES
                )
        except (OSError, ValueError) as exc:
            raise WorkspaceUnavailableError("Dondurulmuş inventory okunamadı.") from exc


def read_manifest(root: Path, workspace_id: str) -> dict[str, Any]:
    """Workspace manifest'ini okur (test ve reconciliation için)."""
    _require_workspace_id(workspace_id)
    with _open_workspace(root, workspace_id) as workspace_fd:
        try:
            raw = _read_private_file(workspace_fd, MANIFEST_FILENAME, MAX_MANIFEST_BYTES)
        except (OSError, ValueError) as exc:
            raise WorkspaceUnavailableError("Workspace manifest'i okunamadı.") from exc
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise WorkspaceUnavailableError("Workspace manifest'i geçersiz.")
    return document


def verify_frozen_workspace(root: Path, workspace_id: str, *, expected_digest: str) -> None:
    """Dondurulmuş içeriğin hâlâ onaylanan içerik olduğunu kanıtlar.

    Digest, **diskteki gerçek baytlardan yeniden hesaplanır**. İçerideki
    ``manifest.json`` bir kanıt değil, bir kopyadır: ona bakıp "digest tutuyor"
    demek, dosyayı değiştirebilen birinin digest satırını da değiştirebileceğini
    görmezden gelmek olurdu. Bu yüzden sıra tersidir — önce içerik özetlenir,
    sonra hem kayıttaki digest hem de manifest dosyasının kendisi bu özete karşı
    doğrulanır. Böylece yalnızca ``manifest.json``'ın değiştirildiği durum da
    yakalanır.

    Doğrulama, dondurmanın aynadaki karşılığıdır ve aynı sınırlarla çalışır:
    bütün erişimler descriptor-relative'dir (``dir_fd`` + ``O_NOFOLLOW``),
    girdi ve bayt sayısı :class:`_Budget` ile sınırlıdır, symlink ve özel dosya
    fail-closed reddedilir.

    **Özgün project ağacı ve özgün inventory dosyası bu fonksiyonda hiç
    açılmaz.** Doğrulanan tek şey dondurulmuş kopyadır; hazırlamadan sonra
    özgün dosyaların değişmesi bu sonucu etkilemez.

    Args:
        root: ``app-data/execution-plans`` kökü.
        workspace_id: Doğrulanacak workspace'in opaque adı.
        expected_digest: Plan **kaydında** saklanan manifest digest'i.

    Raises:
        WorkspaceUnavailableError: Workspace yoksa, adı geçersizse veya güvenli
            biçimde açılamıyorsa.
        WorkspaceIntegrityError: İçerik, izinler veya manifest dosyası
            dondurulduğu andaki hâlinden farklıysa.
    """
    _require_workspace_id(workspace_id)
    budget = _Budget(max_entries=MAX_WORKSPACE_ENTRIES, max_bytes=MAX_WORKSPACE_BYTES)
    with _open_workspace(root, workspace_id) as workspace_fd:
        _require_mode(workspace_fd, DIRECTORY_MODE)
        _require_exact_children(
            workspace_fd, {PROJECT_DIRNAME, INVENTORY_DIRNAME, MANIFEST_FILENAME}
        )
        entries = _observed_entries(workspace_fd, budget=budget)
        observed = _manifest_digest(entries)
        if not hmac.compare_digest(observed, expected_digest):
            raise _integrity_error("content_digest_mismatch")
        _verify_manifest_document(workspace_fd, entries=entries, digest=observed)


def _observed_entries(workspace_fd: int, *, budget: _Budget) -> list[ManifestEntry]:
    """Dondurulmuş içeriğin **şu andaki** manifest girdilerini üretir.

    Sıra ve biçim :func:`_freeze_contents` ile birebir aynıdır; aksi hâlde
    değişmemiş bir workspace bile farklı bir digest üretirdi.
    """
    entries = [ManifestEntry(PROJECT_DIRNAME, "dir", DIRECTORY_MODE, None)]
    with _open_verified_child(workspace_fd, PROJECT_DIRNAME) as project_fd:
        entries.extend(
            _verify_directory(project_fd, prefix=f"{PROJECT_DIRNAME}/", budget=budget, depth=1)
        )

    entries.append(ManifestEntry(INVENTORY_DIRNAME, "dir", DIRECTORY_MODE, None))
    with _open_verified_child(workspace_fd, INVENTORY_DIRNAME) as inventory_fd:
        _require_exact_children(inventory_fd, {INVENTORY_FILENAME})
        budget.count_entry()
        entries.append(
            ManifestEntry(
                f"{INVENTORY_DIRNAME}/{INVENTORY_FILENAME}",
                "file",
                FILE_MODE,
                _digest_regular_file(inventory_fd, INVENTORY_FILENAME, budget=budget),
            )
        )
    return entries


def _verify_directory(
    dir_fd: int, *, prefix: str, budget: _Budget, depth: int
) -> list[ManifestEntry]:
    """Bir dizinin şu andaki girdilerini descriptor-relative özetler."""
    if depth > MAX_WORKSPACE_DEPTH:
        raise _integrity_error("too_deep")

    try:
        names = sorted(os.listdir(dir_fd))
    except OSError as exc:
        raise WorkspaceUnavailableError("Dondurulmuş workspace okunamadı.") from exc

    entries: list[ManifestEntry] = []
    for name in names:
        budget.count_entry()
        try:
            status = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            # Doğrulama sırasında kaybolan girdi de bir değişikliktir.
            raise _integrity_error("unreadable_entry") from exc

        relative = f"{prefix}{name}"
        if stat.S_ISLNK(status.st_mode):
            raise _integrity_error("symlink")
        if stat.S_ISDIR(status.st_mode):
            entries.append(ManifestEntry(relative, "dir", DIRECTORY_MODE, None))
            with _open_verified_child(dir_fd, name) as child_fd:
                entries.extend(
                    _verify_directory(
                        child_fd, prefix=f"{relative}/", budget=budget, depth=depth + 1
                    )
                )
            continue
        if not stat.S_ISREG(status.st_mode):
            raise _integrity_error("special_file")
        entries.append(
            ManifestEntry(relative, "file", FILE_MODE, _digest_regular_file(dir_fd, name, budget))
        )
    return entries


def _digest_regular_file(dir_fd: int, name: str, budget: _Budget) -> str:
    """Normal bir dosyanın SHA-256 özetini bounded biçimde hesaplar.

    Dosya ``O_NOFOLLOW`` ile açılır ve açıldıktan **sonra** ``fstat`` ile hem
    normal dosya olduğu hem de izin bitleri yeniden doğrulanır: ``lstat`` ile
    açma arasında yapılan bir değiş-tokuş burada yakalanır.
    """
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        raise _integrity_error("unreadable_entry") from exc

    hasher = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as reader:
        opened = os.fstat(reader.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise _integrity_error("special_file")
        if stat.S_IMODE(opened.st_mode) != FILE_MODE:
            raise _integrity_error("mode_mismatch")
        while True:
            chunk = reader.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            budget.count_bytes(len(chunk))
            hasher.update(chunk)
    return hasher.hexdigest()


@contextlib.contextmanager
def _open_verified_child(parent_fd: int, name: str) -> Iterator[int]:
    """Alt dizini güvenli biçimde açar ve izin bitlerini doğrular."""
    try:
        with _child_directory(parent_fd, name) as child_fd:
            _require_mode(child_fd, DIRECTORY_MODE)
            yield child_fd
    except OSError as exc:
        raise _integrity_error("unreadable_entry") from exc


def _require_mode(dir_fd: int, expected: int) -> None:
    """Açık descriptor'ın izin bitleri beklenen değerde mi."""
    if stat.S_IMODE(os.fstat(dir_fd).st_mode) != expected:
        raise _integrity_error("mode_mismatch")


def _require_exact_children(dir_fd: int, expected: set[str]) -> None:
    """Dizin **tam olarak** beklenen adları içermeli.

    Eksik girdi kadar fazla girdi de reddedilir: dondurulmuş bir ağaca sonradan
    eklenen dosya, kullanıcının onayladığı içeriğin parçası değildir.
    """
    try:
        found = set(os.listdir(dir_fd))
    except OSError as exc:
        raise WorkspaceUnavailableError("Dondurulmuş workspace okunamadı.") from exc
    if found != expected:
        raise _integrity_error("unexpected_layout")


def _verify_manifest_document(
    workspace_fd: int, *, entries: list[ManifestEntry], digest: str
) -> None:
    """Workspace içindeki ``manifest.json``'ın da değişmediğini doğrular.

    İçerik doğrulaması manifest dosyasını **kapsamaz** (manifest kendi
    digest'inin girdisi değildir), bu yüzden yalnızca manifest'in değiştirildiği
    durum ancak burada yakalanır.
    """
    try:
        raw = _read_private_file(workspace_fd, MANIFEST_FILENAME, MAX_MANIFEST_BYTES)
    except (OSError, ValueError) as exc:
        raise _integrity_error("manifest_unreadable") from exc

    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise _integrity_error("manifest_unreadable") from exc

    if not isinstance(document, dict):
        raise _integrity_error("manifest_mismatch")
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise _integrity_error("manifest_mismatch")
    stored = document.get("digest")
    if not isinstance(stored, str) or not hmac.compare_digest(stored, digest):
        raise _integrity_error("manifest_mismatch")
    if document.get("entries") != _manifest_payload(entries):
        raise _integrity_error("manifest_mismatch")


def _integrity_error(reason: str) -> WorkspaceIntegrityError:
    """Bütün bütünlük ihlalleri için ortak, sızdırmayan hata."""
    return WorkspaceIntegrityError(
        "Dondurulmuş execution içeriği değişmiş. Planı yeniden hazırlayın.",
        details={"reason": reason},
    )


def workspace_exists(root: Path, workspace_id: str) -> bool:
    """Workspace kök altında **gerçek bir dizin** olarak duruyor mu.

    Symlink izlenmez: kök altına konmuş bir bağlantı "var" saymaz.
    """
    if not _WORKSPACE_PATTERN.fullmatch(workspace_id):
        return False
    try:
        with _open_workspace(root, workspace_id):
            return True
    except (WorkspaceUnavailableError, _RootMissingError):
        return False


def list_workspace_ids(root: Path) -> list[str]:
    """Kökün doğrudan çocukları arasındaki geçerli workspace adları."""
    return _list_children(root, _WORKSPACE_PATTERN)


def list_stale_staging(root: Path, *, now: datetime, stale_seconds: float) -> list[str]:
    """Yaş eşiğini aşmış staging dizinlerinin adları.

    Yaş kontrolü zorunludur: o an **yazılmakta olan** bir staging'i silmek,
    başka bir isteğin hazırlığını ortadan kaldırırdı.
    """
    stale: list[str] = []
    try:
        with _root_descriptor(root, create=False) as root_fd:
            for name in _list_directory_names(root_fd, _STAGING_PATTERN):
                if _age_seconds(root_fd, name, now) > stale_seconds:
                    stale.append(name)
    except _RootMissingError:
        return []
    return stale


def workspace_age_seconds(root: Path, name: str, *, now: datetime) -> float | None:
    """Kök altındaki bir girdinin yaşı; girdi yoksa ``None``.

    Yaş da descriptor-relative okunur: adı bilinen bir girdinin yerine konmuş
    symlink izlenmez.
    """
    if not _is_known_shape(name):
        return None
    try:
        with _root_descriptor(root, create=False) as root_fd:
            try:
                os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError:
                return None
            return _age_seconds(root_fd, name, now)
    except _RootMissingError:
        return None


def remove_workspace(root: Path, name: str) -> bool:
    """Tek bir workspace veya staging dizinini kök altında siler.

    ``shutil.rmtree`` bilinçli olarak kullanılmaz. Silme yalnızca kök
    descriptor'ına göre, adı uygulamanın ürettiği biçimlerden birine uyan bir
    girdide yapılır; symlink izlenmez, kök dışına çıkılmaz ve unresolved path
    kullanılmaz.

    Returns:
        Dizin gerçekten silindiyse ``True``.
    """
    if not _is_known_shape(name):
        return False
    try:
        with _root_descriptor(root, create=False) as root_fd:
            try:
                return _remove_child_tree(root_fd, name)
            except OSError:
                return False
    except _RootMissingError:
        return False


def read_maintenance_cursor(root: Path) -> str | None:
    """Bakım turunun en son incelediği workspace adını okur.

    ``None`` "listenin başından başla" demektir ve **her zaman güvenlidir**:
    imleç yalnızca hangi pencerenin inceleneceğini belirler, bir dizinin silinip
    silinmeyeceğini asla belirlemez. Bu yüzden eksik, bozuk, okunamayan veya
    symlink bir imleç fail-closed biçimde yok sayılır; symlink izlenmez ve
    silinmez, yalnızca güvenilmez sayılır.
    """
    try:
        with _root_descriptor(root, create=False) as root_fd:
            try:
                raw = _read_private_file(root_fd, MAINTENANCE_CURSOR_FILENAME, MAX_CURSOR_BYTES)
            except (OSError, ValueError):
                return None
    except (_RootMissingError, WorkspaceUnavailableError):
        return None

    try:
        document = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict) or document.get("version") != CURSOR_SCHEMA_VERSION:
        return None
    after = document.get("after")
    if not isinstance(after, str) or not _WORKSPACE_PATTERN.fullmatch(after):
        return None
    return after


def write_maintenance_cursor(root: Path, after: str | None) -> bool:
    """Bakım imlecini atomik olarak günceller.

    Önce geçici bir dosyaya yazılır, fsync edilir ve ancak sonra ``rename`` ile
    yerine konur: yarım yazılmış bir imleç hiçbir zaman okunamaz. ``rename`` son
    bileşendeki symlink'i **izlemez**; girdinin kendisini değiştirir, gösterdiği
    hedefe dokunmaz. Kök dışına hiçbir şey yazılmaz ve silinmez.

    Returns:
        Yazılabildiyse ``True``. Yazılamazsa imleç ilerlemez: bir sonraki tur
        aynı pencereyi yeniden inceler; bu iş tekrarıdır, yanlış silme değildir.
    """
    if after is not None and not _WORKSPACE_PATTERN.fullmatch(after):
        return False
    payload = json.dumps(
        {"version": CURSOR_SCHEMA_VERSION, "after": after},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    try:
        with _root_descriptor(root, create=False) as root_fd:
            return _publish_cursor(root_fd, payload)
    except (_RootMissingError, WorkspaceUnavailableError):
        return False


def _publish_cursor(root_fd: int, payload: str) -> bool:
    """İmleci geçici dosya + atomik ``rename`` ile yayımlar."""
    try:
        # O_NONBLOCK: geçici adın yerine konmuş bir FIFO açma sırasında süreci
        # bloke edemesin. O_NOFOLLOW: symlink üzerinden yazılamasın.
        descriptor = os.open(
            _CURSOR_TEMP_FILENAME,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK,
            FILE_MODE,
            dir_fd=root_fd,
        )
    except OSError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("bakım imleci normal dosya değil")
            os.fchmod(handle.fileno(), FILE_MODE)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(
            _CURSOR_TEMP_FILENAME,
            MAINTENANCE_CURSOR_FILENAME,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        os.fsync(root_fd)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(_CURSOR_TEMP_FILENAME, dir_fd=root_fd)
        return False
    return True


# --- Dondurma ---------------------------------------------------------------


def _freeze_contents(
    staging_fd: int,
    *,
    project_root: Path,
    inventory_snapshot: str,
    budget: _Budget,
) -> list[ManifestEntry]:
    """Staging dizinine project ağacını ve inventory snapshot'ını yazar."""
    entries: list[ManifestEntry] = []

    try:
        os.mkdir(PROJECT_DIRNAME, DIRECTORY_MODE, dir_fd=staging_fd)
        os.mkdir(INVENTORY_DIRNAME, DIRECTORY_MODE, dir_fd=staging_fd)
    except OSError as exc:
        raise WorkspaceUnavailableError("Execution workspace hazırlanamadı.") from exc

    entries.append(ManifestEntry(PROJECT_DIRNAME, "dir", DIRECTORY_MODE, None))
    with (
        _source_directory(project_root) as source_fd,
        _child_directory(staging_fd, PROJECT_DIRNAME) as target_fd,
    ):
        os.fchmod(target_fd, DIRECTORY_MODE)
        entries.extend(
            _copy_directory(
                source_fd,
                target_fd,
                prefix=f"{PROJECT_DIRNAME}/",
                budget=budget,
                depth=1,
            )
        )

    entries.append(ManifestEntry(INVENTORY_DIRNAME, "dir", DIRECTORY_MODE, None))
    with _child_directory(staging_fd, INVENTORY_DIRNAME) as inventory_fd:
        os.fchmod(inventory_fd, DIRECTORY_MODE)
        budget.count_entry()
        budget.count_bytes(len(inventory_snapshot.encode("utf-8")))
        _write_private_file(inventory_fd, INVENTORY_FILENAME, inventory_snapshot)
        os.fsync(inventory_fd)
    entries.append(
        ManifestEntry(
            f"{INVENTORY_DIRNAME}/{INVENTORY_FILENAME}",
            "file",
            FILE_MODE,
            hashlib.sha256(inventory_snapshot.encode("utf-8")).hexdigest(),
        )
    )
    return entries


def _copy_directory(
    source_fd: int,
    target_fd: int,
    *,
    prefix: str,
    budget: _Budget,
    depth: int,
) -> list[ManifestEntry]:
    """Bir dizinin içeriğini descriptor-relative kopyalar.

    Girdiler ada göre sıralı işlenir: manifest deterministik olmalıdır ve
    dizin okuma sırası dosya sistemine göre değişir.
    """
    if depth > MAX_WORKSPACE_DEPTH:
        raise WorkspaceUnsafeError(
            "Project ağacı dondurulamayacak kadar derin.",
            details={"reason": "too_deep", "limit": MAX_WORKSPACE_DEPTH},
        )

    try:
        names = sorted(os.listdir(source_fd))
    except OSError as exc:
        raise WorkspaceUnavailableError("Project dizini okunamadı.") from exc

    entries: list[ManifestEntry] = []
    for name in names:
        budget.count_entry()
        try:
            status = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        except FileNotFoundError:
            # Kopyalama sırasında kaybolan girdi: dondurulmuş çıktı onu
            # içermez ve manifest de **yazdığımızı** anlatır.
            continue
        except OSError as exc:
            raise WorkspaceUnavailableError("Project girdisi okunamadı.") from exc

        relative = f"{prefix}{name}"
        if stat.S_ISLNK(status.st_mode):
            raise WorkspaceUnsafeError(
                "Project ağacı symlink içeriyor; dondurulmuş kopya symlink taşımaz.",
                details={"reason": "symlink"},
            )
        if stat.S_ISDIR(status.st_mode):
            entries.append(ManifestEntry(relative, "dir", DIRECTORY_MODE, None))
            entries.extend(
                _copy_child_directory(
                    source_fd,
                    target_fd,
                    name,
                    prefix=f"{relative}/",
                    budget=budget,
                    depth=depth,
                )
            )
            continue
        if not stat.S_ISREG(status.st_mode):
            raise WorkspaceUnsafeError(
                "Project ağacı normal dosya olmayan bir girdi içeriyor.",
                details={"reason": "special_file"},
            )
        digest = _copy_regular_file(source_fd, target_fd, name, budget=budget)
        if digest is None:
            continue
        entries.append(ManifestEntry(relative, "file", FILE_MODE, digest))
    return entries


def _copy_child_directory(
    source_fd: int,
    target_fd: int,
    name: str,
    *,
    prefix: str,
    budget: _Budget,
    depth: int,
) -> list[ManifestEntry]:
    """Alt dizini oluşturur ve içeriğini kopyalar."""
    try:
        os.mkdir(name, DIRECTORY_MODE, dir_fd=target_fd)
    except OSError as exc:
        raise WorkspaceUnavailableError("Execution workspace dizini oluşturulamadı.") from exc
    try:
        with (
            _child_directory(source_fd, name) as child_source,
            _child_directory(target_fd, name) as child_target,
        ):
            os.fchmod(child_target, DIRECTORY_MODE)
            return _copy_directory(
                child_source,
                child_target,
                prefix=prefix,
                budget=budget,
                depth=depth + 1,
            )
    except FileNotFoundError:
        # Kaynak alt dizin kopyalama sırasında kayboldu.
        return []
    except OSError as exc:
        raise WorkspaceUnavailableError("Project alt dizini kopyalanamadı.") from exc


def _copy_regular_file(
    source_fd: int,
    target_fd: int,
    name: str,
    *,
    budget: _Budget,
) -> str | None:
    """Tek bir normal dosyayı kopyalar ve **yazılan** baytların özetini döndürür.

    Dosya ``O_NOFOLLOW`` ile açılır ve açıldıktan sonra ``fstat`` ile yeniden
    normal dosya olduğu doğrulanır: ``lstat`` ile açma arasında yapılan bir
    değiş-tokuş burada yakalanır.

    Returns:
        SHA-256 özeti; dosya kopyalama sırasında kaybolduysa ``None``.
    """
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkspaceUnsafeError(
            "Project dosyası güvenli biçimde açılamadı.",
            details={"reason": "unreadable_entry"},
        ) from exc

    hasher = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as reader:
        opened = os.fstat(reader.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise WorkspaceUnsafeError(
                "Project ağacı normal dosya olmayan bir girdi içeriyor.",
                details={"reason": "special_file"},
            )
        try:
            target = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                FILE_MODE,
                dir_fd=target_fd,
            )
        except OSError as exc:
            raise WorkspaceUnavailableError("Dondurulmuş dosya oluşturulamadı.") from exc
        with os.fdopen(target, "wb") as writer:
            os.fchmod(writer.fileno(), FILE_MODE)
            while True:
                chunk = reader.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                # Sınır okuma sırasında uygulanır: aşan içerik diske tamamen
                # yazılmadan hata verilir ve staging temizlenir.
                budget.count_bytes(len(chunk))
                hasher.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    return hasher.hexdigest()


# --- Manifest ---------------------------------------------------------------


def _manifest_payload(entries: list[ManifestEntry]) -> list[dict[str, Any]]:
    """Manifest girdilerini kararlı, sıralı bir yapıya çevirir."""
    return [
        {
            "path": entry.path,
            "type": entry.entry_type,
            "mode": f"{entry.mode:04o}",
            "sha256": entry.sha256,
        }
        for entry in sorted(entries, key=lambda item: item.path)
    ]


def _manifest_digest(entries: list[ManifestEntry]) -> str:
    """Manifest digest'i: sıralı, deterministik ve içeriğe duyarlı.

    Tek bir baytın veya tek bir yolun değişmesi digest'i değiştirir; aynı
    dondurulmuş içerik her zaman aynı digest'i üretir.
    """
    canonical = json.dumps(
        _manifest_payload(entries),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _render_manifest(entries: list[ManifestEntry], *, digest: str, frozen_at: datetime) -> str:
    """Workspace içinde saklanan tam manifest metni.

    Tam manifest API'ye verilmez: kullanıcıya yalnızca digest gösterilir.
    """
    document = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "digest": digest,
        "frozen_at": frozen_at.isoformat(),
        "entries": _manifest_payload(entries),
    }
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


# --- Dosya sistemi yardımcıları ---------------------------------------------


def _require_secure_filesystem() -> None:
    """Güvenli primitive'ler yoksa fail-closed davranır."""
    if not secure_filesystem_available():
        raise WorkspaceUnavailableError(
            "Execution planı bu platformda güvenli biçimde hazırlanamıyor."
        )


def _require_workspace_id(workspace_id: str) -> None:
    """Workspace adı yalnızca uygulamanın ürettiği biçimde olabilir."""
    if not _WORKSPACE_PATTERN.fullmatch(workspace_id):
        raise WorkspaceUnavailableError("Execution workspace kimliği geçersiz.")


def _is_known_shape(name: str) -> bool:
    """Ad, uygulamanın ürettiği biçimlerden birine uyuyor mu."""
    return bool(_WORKSPACE_PATTERN.fullmatch(name) or _STAGING_PATTERN.fullmatch(name))


@contextlib.contextmanager
def _root_descriptor(root: Path, *, create: bool) -> Iterator[int]:
    """Workspace kökünü ``O_DIRECTORY | O_NOFOLLOW`` ile açar.

    Kökün kendisi bir symlink ise açma ``ELOOP`` ile başarısız olur ve işlem
    fail-closed biçimde ``execution_workspace_unavailable`` üretir.
    """
    _require_secure_filesystem()
    if create:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceUnavailableError("Execution workspace kökü hazırlanamadı.") from exc
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise _RootMissingError from exc
    except OSError as exc:
        raise WorkspaceUnavailableError(
            "Execution workspace kökü güvenli biçimde açılamadı."
        ) from exc
    try:
        with contextlib.suppress(OSError):
            os.fchmod(root_fd, DIRECTORY_MODE)
        yield root_fd
    finally:
        os.close(root_fd)


@contextlib.contextmanager
def _open_workspace(root: Path, workspace_id: str) -> Iterator[int]:
    """Yayımlanmış bir workspace dizinini güvenli biçimde açar."""
    try:
        with _root_descriptor(root, create=False) as root_fd:
            try:
                with _child_directory(root_fd, workspace_id) as workspace_fd:
                    yield workspace_fd
            except OSError as exc:
                raise WorkspaceUnavailableError("Execution workspace bulunamadı.") from exc
    except _RootMissingError as exc:
        raise WorkspaceUnavailableError("Execution workspace kökü bulunamadı.") from exc


@contextlib.contextmanager
def _source_directory(directory: Path) -> Iterator[int]:
    """Kaynak dizini ``O_DIRECTORY | O_NOFOLLOW`` ile açar.

    Kaynağın kendisi symlink ise kopyalama başlamadan reddedilir.
    """
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise WorkspaceUnsafeError(
            "Project dizini güvenli biçimde açılamadı.",
            details={"reason": "unreadable_root"},
        ) from exc
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _child_directory(parent_fd: int, name: str) -> Iterator[int]:
    """Alt dizini ``O_DIRECTORY | O_NOFOLLOW`` ile açar ve kimliğini doğrular."""
    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        _assert_same_entry(child_fd, parent_fd, name)
        yield child_fd
    finally:
        os.close(child_fd)


def _assert_same_entry(child_fd: int, parent_fd: int, name: str) -> None:
    """Açık descriptor ile isimdeki girdinin aynı nesne olduğunu doğrular."""
    opened = os.fstat(child_fd)
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise OSError("execution workspace girdisi değiştirildi")


def _write_private_file(dir_fd: int, name: str, content: str) -> None:
    """Dosyayı dizin descriptor'ına göre, 0600 izniyle ve fsync ederek yazar."""
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            FILE_MODE,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        raise WorkspaceUnavailableError("Execution workspace dosyası yazılamadı.") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), FILE_MODE)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _read_private_file(dir_fd: int, name: str, max_bytes: int) -> str:
    """Dosyayı dizin descriptor'ına göre, symlink izlemeden okur."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"{name} normal dosya değil")
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"{name} beklenenden büyük")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name} UTF-8 değil") from exc


def _list_children(root: Path, pattern: re.Pattern[str]) -> list[str]:
    """Kökün doğrudan alt **dizinleri** arasında desene uyan adlar."""
    try:
        with _root_descriptor(root, create=False) as root_fd:
            return _list_directory_names(root_fd, pattern)
    except _RootMissingError:
        return []


def _list_directory_names(root_fd: int, pattern: re.Pattern[str]) -> list[str]:
    """Desene uyan doğrudan alt dizin adları; symlink izlenmez."""
    try:
        names = os.listdir(root_fd)
    except OSError:  # pragma: no cover - kök yeni açıldı
        return []
    found: list[str] = []
    for name in sorted(names):
        if not pattern.fullmatch(name):
            continue
        try:
            status = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(status.st_mode):
            found.append(name)
    return found


def _age_seconds(root_fd: int, name: str, moment: datetime) -> float:
    """Girdinin son değişiklik zamanına göre yaşı."""
    try:
        status = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        return 0.0
    return moment.timestamp() - status.st_mtime


def _remove_child_tree(root_fd: int, name: str) -> bool:
    """Kök altındaki bir dizini içeriğiyle birlikte siler.

    Silme descriptor-relative ilerler ve symlink'i **izlemez**: bilinen bir ada
    konmuş bağlantının kendisi silinir, gösterdiği dış hedefe dokunulmaz.
    """
    try:
        status = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return False

    if not stat.S_ISDIR(status.st_mode):
        # Symlink veya beklenmeyen bir dosya: yalnızca girdinin kendisi silinir.
        try:
            os.unlink(name, dir_fd=root_fd)
        except OSError:
            return False
        return True

    try:
        with _child_directory(root_fd, name) as child_fd:
            _empty_directory(child_fd, depth=1)
    except OSError:
        return False
    try:
        os.rmdir(name, dir_fd=root_fd)
    except OSError:
        return False
    return True


def _empty_directory(dir_fd: int, *, depth: int) -> None:
    """Dizinin içeriğini descriptor-relative boşaltır."""
    if depth > MAX_WORKSPACE_DEPTH + 2:
        raise OSError("execution workspace beklenenden derin")
    for name in os.listdir(dir_fd):
        status = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISDIR(status.st_mode):
            with _child_directory(dir_fd, name) as child_fd:
                _empty_directory(child_fd, depth=depth + 1)
            os.rmdir(name, dir_fd=dir_fd)
            continue
        os.unlink(name, dir_fd=dir_fd)
