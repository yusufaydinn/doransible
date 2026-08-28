"""Controller path browse servisi (R1-V3J0C).

Project ve Inventory formlarındaki manuel path alanının yanına eklenen
"Gözat…" dialogunun tek backend yüzeyidir. Bu modül:

- **Yazmaz.** Dosya oluşturmaz, silmez, yeniden adlandırmaz veya düzenlemez.
- **Okumaz.** Hiçbir dosyanın içeriği açılmaz; yalnızca dizin girdileri
  listelenir (``os.scandir``/``os.stat`` ötesine geçilmez).
- **Subprocess çalıştırmaz.** Shell veya ``ansible-*`` süreci başlatılmaz.
- **Recursive taramaz.** Her çağrı tam olarak tek bir dizinin doğrudan
  çocuklarını listeler.

Path güvenliği yeni bir kod yolu değildir: ``services.security.paths``
primitiflerinin (``normalize_filesystem_path``, ``ensure_within_allowed_roots``,
``ensure_existing_directory``) **aynı** kopyası kullanılır — GUVENLIK.md bölüm 4
ve ADR-015'in "güvenlik kritik kod kopyalanmaz" ilkesiyle tutarlı (MIMARI.md
bölüm 2, ``process.py`` gerekçesiyle aynı).

Üç scope, üç farklı sınırı temsil eder:

- ``project``: ``project_root_allowlist`` altında gezinilir, yalnız dizin
  seçilebilir.
- ``inventory``: ``inventory_root_allowlist`` altında gezinilir, yalnız
  normal dosya seçilebilir.
- ``project_inventory``: **yalnızca** seçili aktif project'in kendi
  doğrulanmış kökü altında gezinilir, yalnız normal dosya seçilebilir. Genel
  inventory allowlist'i bu sınırı **genişletmez** (ADR-015): boundary tek bir
  dizindir, o project'in kendi kökü.

Backend parent/breadcrumb hesaplamaz; frontend kendi navigasyon yığınını
tutar (R1-V3J0C kapsam kararı).
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.services.projects.service import (
    ProjectInactiveError,
    get_project,
    resolve_project_root,
)
from app.services.security.paths import (
    PathNotAllowedError,
    ensure_existing_directory,
    ensure_within_allowed_roots,
    normalize_filesystem_path,
)

#: Tek bir dizin listelemesinde döndürülecek azami girdi sayısı.
#:
#: Bu **gerçek** bir kaynak sınırıdır: ``_list_directory`` ``os.scandir``
#: iteratöründen en fazla ``MAX_BROWSE_ENTRIES + 1`` ham girdi çeker — milyonlarca
#: dosya içeren bir dizinin geri kalanı hiç okunmaz, belleğe alınmaz veya
#: sıralanmaz (AUDIT-FIX1 bulgu 1; eski uygulama tüm dizini önce
#: ``sorted(os.scandir(...))`` ile tüketip **sonra** kırpıyordu, bu da sınırı
#: yalnızca görünüşte bırakıyordu).
#:
#: Sınır ham (henüz sembolik bağlantı/özel dosya filtresinden geçmemiş) girdi
#: sayısına uygulanır. Bu yüzden ``truncated=True`` olsa bile döndürülen girdi
#: sayısı 500'den **az** olabilir: çekilen ilk 500 ham girdinin bir kısmı
#: symlink veya özel dosya olarak elenmiş olabilir.
#:
#: **Kesilen listenin hangi üyeleri taşıyacağına dair global bir alfabetik
#: garanti yoktur.** Sınır, dizin tamamı sıralanmadan (yani ``os.scandir``'ın
#: döndürdüğü ham, dosya sistemine/platforma özgü sırayla) uygulanır; yalnızca
#: **döndürülen** alt küme kendi içinde (dizin-önce, ada göre) sıralanır. 600
#: girdili bir dizinde "ilk 500" alfabetik olarak ilk 500 girdi olmak zorunda
#: değildir — hangi 500'ün geldiği dosya sisteminin numaralandırma sırasına
#: bağlıdır.
MAX_BROWSE_ENTRIES = 500


class BrowseScope(StrEnum):
    """Hangi allowlist'in ve hangi seçilebilir türün geçerli olduğu."""

    PROJECT = "project"
    INVENTORY = "inventory"
    PROJECT_INVENTORY = "project_inventory"


class EntryKind(StrEnum):
    """Bir dizin girdisinin çözümlenmiş türü. Symlink ayrı bir tür değildir;

    symlink girdileri listeye hiç girmez (bkz. :func:`_list_directory`).
    """

    DIRECTORY = "directory"
    FILE = "file"


#: Her scope'ta kullanıcının gerçekten **seçebileceği** girdi türü.
_TARGET_KIND: dict[BrowseScope, EntryKind] = {
    BrowseScope.PROJECT: EntryKind.DIRECTORY,
    BrowseScope.INVENTORY: EntryKind.FILE,
    BrowseScope.PROJECT_INVENTORY: EntryKind.FILE,
}


class BrowseInvalidScopeError(AppError):
    """``scope``/``project_id`` kombinasyonu geçersiz.

    ``project_inventory`` için ``project_id`` zorunludur; ``project`` ve
    ``inventory`` için verilemez. Hangi allowlist'in kastedildiği belirsiz
    bırakılan bir istek sessizce yorumlanmaz.
    """

    status_code = 422
    code = "browse_invalid_scope"


class BrowseDirectoryUnreadableError(AppError):
    """Dizin allowlist içinde ve mevcut ama listelenemedi (izin, I/O).

    ``path_not_found``/``path_not_a_directory`` ile karıştırılmaz: burada
    dizin gerçekten vardır, yalnızca okunamamıştır.
    """

    status_code = 500
    code = "browse_directory_unreadable"


@dataclass(frozen=True)
class BrowseEntry:
    """Listelenen tek bir dizin/dosya girdisi.

    ``path`` her zaman kanonik (zaten ``resolve()`` edilmiş) mutlak yoldur:
    listelenen dizinin kendisi normalize edilmiş olduğu ve girdi symlink
    değilse (symlink'ler tamamen elenir), ``dizin / ad`` doğrudan kanonik
    sonuçtur; girdi başına ayrı bir ``resolve()`` çağrısı gerekmez.
    """

    name: str
    path: str
    kind: EntryKind
    selectable: bool


@dataclass(frozen=True)
class BrowseListing:
    """Tek bir browse çağrısının sonucu."""

    scope: BrowseScope
    current_path: str | None
    target_kind: EntryKind
    entries: list[BrowseEntry]
    truncated: bool


def list_controller_paths(
    session: Session,
    *,
    scope: BrowseScope,
    project_id: int | None,
    path: str | None,
    project_roots: Sequence[Path],
    inventory_roots: Sequence[Path],
) -> BrowseListing:
    """Bir dizini ya da sentetik kök seçiciyi listeler.

    Kontrol sırası GUVENLIK.md bölüm 4 ile birebir aynıdır: allowlist
    kontrolü **her zaman** varlık kontrolünden önce çalışır, bu yüzden
    allowlist dışındaki mevcut ve mevcut olmayan bir yol aynı generic 403'ü
    üretir (dosya sistemi sondası olunmaz).

    Args:
        session: Aktif veritabanı session'ı (yalnızca ``project_inventory``
            scope'unda project sorgusu için kullanılır).
        scope: Hangi allowlist'in ve seçilebilir türün geçerli olduğu.
        project_id: ``project_inventory`` scope'unda zorunlu, diğerlerinde
            verilemez.
        path: Listelenecek dizin. ``None`` ise "başlangıç görünümü" döner
            (bkz. modül docstring'i).
        project_roots: ``project`` scope ve ``project_inventory`` scope'unun
            iç doğrulaması için izin verilen project root'ları.
        inventory_roots: ``inventory`` scope için izin verilen root'lar.

    Returns:
        Deterministik sıralı (önce dizin, sonra dosya; kendi içinde ada göre)
        bir :class:`BrowseListing`.

    Raises:
        BrowseInvalidScopeError: ``scope``/``project_id`` kombinasyonu
            geçersizse.
        NotFoundError: ``project_inventory`` scope'unda project yoksa.
        ProjectInactiveError: Bağlı project pasifse.
        PathNotAllowedError: Path (ya da project'in kendi kökü) izin verilen
            alanın dışındaysa.
        ProjectPathUnavailableError: Project kökü artık mevcut değilse.
        InvalidPathError: ``path`` biçimsel olarak geçersizse.
        PathNotFoundError: Dizin mevcut değilse.
        PathIsNotADirectoryError: Path bir dizin değilse.
        BrowseDirectoryUnreadableError: Dizin okunamazsa.
    """
    target_kind = _TARGET_KIND[scope]
    boundary_roots = _resolve_boundary(
        session,
        scope=scope,
        project_id=project_id,
        project_roots=project_roots,
        inventory_roots=inventory_roots,
    )

    if path is None:
        listing = _initial_listing(scope, boundary_roots, target_kind)
        if listing is not None:
            return listing
        directory = boundary_roots[0]
    else:
        normalized = normalize_filesystem_path(path)
        ensure_within_allowed_roots(normalized, boundary_roots)
        directory = normalized

    ensure_existing_directory(directory)
    entries, truncated = _list_directory(directory, target_kind)

    return BrowseListing(
        scope=scope,
        current_path=str(directory),
        target_kind=target_kind,
        entries=entries,
        truncated=truncated,
    )


def _resolve_boundary(
    session: Session,
    *,
    scope: BrowseScope,
    project_id: int | None,
    project_roots: Sequence[Path],
    inventory_roots: Sequence[Path],
) -> tuple[Path, ...]:
    """Scope'a göre geçerli gezinme sınırını (bir ya da birden çok kök) döndürür.

    ``project_inventory`` için sınır **her zaman tek** bir dizindir: seçili
    project'in kendi kökü. Genel ``inventory_root_allowlist`` burada hiç
    devreye girmez ve genel ``project_root_allowlist`` yalnızca project'in
    kendi kaydını yeniden doğrulamak için **iç adımda** kullanılır — gezinme
    sınırının kendisini genişletmez.
    """
    if scope is BrowseScope.PROJECT_INVENTORY:
        if project_id is None:
            raise BrowseInvalidScopeError("project_inventory scope'u project_id gerektirir.")
        project = get_project(session, project_id)
        if not project.is_active:
            raise ProjectInactiveError(
                "Pasif project altında gezinilemez.",
                details={"project_id": project.id},
            )
        root = resolve_project_root(project, allowed_roots=project_roots)
        return (root,)

    if project_id is not None:
        raise BrowseInvalidScopeError(f"{scope.value} scope'unda project_id verilemez.")

    if scope is BrowseScope.PROJECT:
        return tuple(project_roots)
    return tuple(inventory_roots)


def _initial_listing(
    scope: BrowseScope,
    boundary_roots: tuple[Path, ...],
    target_kind: EntryKind,
) -> BrowseListing | None:
    """``path`` verilmediğinde gösterilecek başlangıç görünümünü üretir.

    - Sınır tek bir kökten oluşuyorsa (varsayılan yapılandırma ve her zaman
      ``project_inventory``) sentetik katman **atlanır**: ``None`` döner ve
      çağıran doğrudan o kökü listeler.
    - Birden fazla kök varsa her biri sentetik bir "dizin" girdisi olarak
      sunulur; ``current_path`` bunun gerçek bir dosya sistemi yolu değil bir
      seçim ekranı olduğunu belirtmek için ``None`` kalır.

    Raises:
        PathNotAllowedError: ``boundary_roots`` boşsa (fail-closed; yapılandırma
            hiçbir kök tanımlamıyor demektir).
    """
    if not boundary_roots:
        # `ensure_within_allowed_roots`'un boş allowlist mesajıyla aynı
        # sözleşme: normal akışta `settings.resolve_*_allowlist()` her zaman
        # en az bir kök döndürür, ama servis kendi başına da fail-closed
        # olmalıdır.
        raise PathNotAllowedError("İzin verilen root tanımlı değil; hiçbir path kabul edilemez.")

    if len(boundary_roots) == 1:
        return None

    entries = sorted(
        (
            BrowseEntry(
                name=str(root),
                path=str(root),
                kind=EntryKind.DIRECTORY,
                selectable=(target_kind == EntryKind.DIRECTORY),
            )
            for root in boundary_roots
        ),
        key=lambda entry: entry.name,
    )
    return BrowseListing(
        scope=scope,
        current_path=None,
        target_kind=target_kind,
        entries=entries,
        truncated=False,
    )


def _list_directory(
    directory: Path,
    target_kind: EntryKind,
) -> tuple[list[BrowseEntry], bool]:
    """Bir dizinin doğrudan çocuklarını **bounded** biçimde listeler.

    Kurallar:

    - **Gerçek bounded tarama (AUDIT-FIX1 bulgu 1).** ``os.scandir``
      iteratöründen ``itertools.islice`` ile en fazla
      ``MAX_BROWSE_ENTRIES + 1`` ham girdi çekilir; iteratör bu noktadan
      sonra **hiç ilerletilmez**. Dizinin geri kalanı — milyonlarca girdi
      olsa bile — ne okunur ne belleğe alınır ne sıralanır. 501. girdinin
      çekilip çekilemediği ``truncated``'ı belirler; işlenen alt küme yalnızca
      ilk ``MAX_BROWSE_ENTRIES`` ham girdidir.
    - **Symlink girdileri tamamen atılır** — nereye işaret ettikleri
      önemsizdir; ne gezilebilir ne "var" görünürler. Bu, discovery.py'nin
      resolve-edip-boundary-içinde-mi-diye-bakma yaklaşımından **bilinçli
      olarak daha basittir**: burada recursion olmadığı için aynı sağlamlık
      gerekmez, symlink'i baştan eleyen kural hem yeterli hem denetlenebilir.
    - FIFO, socket, device gibi normal olmayan girdiler de atlanır.
    - Stat/erişim hatası veren tek bir girdi bütün listelemeyi düşürmez;
      yalnızca o girdi atlanır.
    - Döndürülen alt küme kendi içinde (dizin-önce, ada göre) sıralanır; bu
      **görüntüleme** sıralamasıdır, dizindeki bütün girdiler üzerinde global
      bir alfabetik "ilk N" garantisi değildir (bkz. :data:`MAX_BROWSE_ENTRIES`
      docstring'i).

    Raises:
        BrowseDirectoryUnreadableError: Dizinin kendisi açılamazsa/okunamazsa
            (kök açma **veya** iterasyon sırasında).
    """
    try:
        with os.scandir(directory) as scan:
            raw_batch = list(itertools.islice(scan, MAX_BROWSE_ENTRIES + 1))
    except OSError as exc:
        raise BrowseDirectoryUnreadableError("Dizin listelenemedi.") from exc

    truncated = len(raw_batch) > MAX_BROWSE_ENTRIES
    raw_entries = raw_batch[:MAX_BROWSE_ENTRIES]

    collected: list[BrowseEntry] = []
    for item in raw_entries:
        try:
            if item.is_symlink():
                continue
            is_directory = item.is_dir(follow_symlinks=False)
            is_regular_file = item.is_file(follow_symlinks=False)
        except OSError:
            # Yarışan silme veya erişilemeyen girdi: sessizce atlanır, bütün
            # listelemeyi düşürmez.
            continue

        if is_directory:
            kind = EntryKind.DIRECTORY
        elif is_regular_file:
            kind = EntryKind.FILE
        else:
            # FIFO, socket, device vb. — hiçbir scope'ta anlamlı değildir.
            continue

        collected.append(
            BrowseEntry(
                name=item.name,
                path=str(directory / item.name),
                kind=kind,
                selectable=(kind == target_kind),
            )
        )

    collected.sort(key=lambda entry: (entry.kind != EntryKind.DIRECTORY, entry.name))
    return collected, truncated
