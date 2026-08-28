"""Controller path browse servisi (R1-V3J0C).

API testleri HTTP sözleşmesini doğrular; bu dosya servis katmanının kendi
kararlarını HTTP olmadan ölçer. Bu, ``test_inventory_service.py`` ile aynı
route/service katman ayrımıdır.
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import Project
from app.services.browse import service as browse_service
from app.services.browse.service import (
    MAX_BROWSE_ENTRIES,
    BrowseDirectoryUnreadableError,
    BrowseInvalidScopeError,
    BrowseScope,
    EntryKind,
    list_controller_paths,
)
from app.services.projects.service import ProjectInactiveError
from app.services.security.paths import PathNotAllowedError
from tests.support import link_directory


def _add_project(session: Session, path: Path, *, is_active: bool = True) -> Project:
    path.mkdir(parents=True, exist_ok=True)
    project = Project(name="Web", path=str(path))
    project.is_active = is_active
    session.add(project)
    session.commit()
    return project


# --- Üç scope için happy path -------------------------------------------------


def test_project_scope_happy_path(db_session: Session, project_root: Path) -> None:
    (project_root / "web").mkdir()
    (project_root / "site.yml").write_text("- hosts: all", encoding="utf-8")

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.PROJECT,
        project_id=None,
        path=str(project_root),
        project_roots=[project_root],
        inventory_roots=[],
    )

    assert listing.scope is BrowseScope.PROJECT
    assert listing.current_path == str(project_root.resolve())
    assert listing.target_kind is EntryKind.DIRECTORY
    names = {entry.name: entry for entry in listing.entries}
    assert names["web"].kind is EntryKind.DIRECTORY
    assert names["web"].selectable is True
    assert names["site.yml"].kind is EntryKind.FILE
    assert names["site.yml"].selectable is False


def test_inventory_scope_happy_path(db_session: Session, inventory_root: Path) -> None:
    (inventory_root / "hosts.ini").write_text("[web]\nweb01\n", encoding="utf-8")
    (inventory_root / "group_vars").mkdir()

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.INVENTORY,
        project_id=None,
        path=str(inventory_root),
        project_roots=[],
        inventory_roots=[inventory_root],
    )

    names = {entry.name: entry for entry in listing.entries}
    assert names["hosts.ini"].kind is EntryKind.FILE
    assert names["hosts.ini"].selectable is True
    assert names["group_vars"].kind is EntryKind.DIRECTORY
    assert names["group_vars"].selectable is False


def test_project_inventory_scope_happy_path(db_session: Session, project_root: Path) -> None:
    project = _add_project(db_session, project_root / "web")
    (project_root / "web" / "inventories").mkdir()
    (project_root / "web" / "hosts.ini").write_text("[web]\nweb01\n", encoding="utf-8")

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.PROJECT_INVENTORY,
        project_id=project.id,
        path=None,
        project_roots=[project_root],
        inventory_roots=[],
    )

    # Tek kök (project'in kendi dizini) olduğu için sentetik katman atlanır;
    # `path=None` doğrudan project kökünü listeler.
    assert listing.current_path == str((project_root / "web").resolve())
    names = {entry.name for entry in listing.entries}
    assert names == {"inventories", "hosts.ini"}


# --- Yalnız doğrudan çocuklar; recursion yok ---------------------------------


def test_only_direct_children_are_listed(db_session: Session, project_root: Path) -> None:
    nested = project_root / "roles" / "web" / "tasks"
    nested.mkdir(parents=True)
    (nested / "main.yml").write_text("- name: noop", encoding="utf-8")

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.PROJECT,
        project_id=None,
        path=str(project_root),
        project_roots=[project_root],
        inventory_roots=[],
    )

    assert [entry.name for entry in listing.entries] == ["roles"]


# --- project_inventory: project kökünden çıkamama ----------------------------


def test_project_inventory_cannot_escape_its_own_root(
    db_session: Session, project_root: Path
) -> None:
    project = _add_project(db_session, project_root / "web")
    sibling = project_root / "baska-project"
    sibling.mkdir()

    with pytest.raises(PathNotAllowedError):
        list_controller_paths(
            db_session,
            scope=BrowseScope.PROJECT_INVENTORY,
            project_id=project.id,
            path=str(sibling),
            project_roots=[project_root],
            inventory_roots=[],
        )


def test_project_inventory_general_allowlist_does_not_substitute_project_root(
    db_session: Session, project_root: Path
) -> None:
    """Genel `project_root_allowlist` içindeki başka bir dizin bu project'e ait sayılmaz."""
    project_a = _add_project(db_session, project_root / "a")
    (project_root / "b").mkdir()

    with pytest.raises(PathNotAllowedError):
        list_controller_paths(
            db_session,
            scope=BrowseScope.PROJECT_INVENTORY,
            project_id=project_a.id,
            path=str(project_root / "b"),
            project_roots=[project_root],
            inventory_roots=[],
        )


# --- Traversal ve allowlist dışı path ----------------------------------------


def test_traversal_out_of_allowed_root_is_rejected(
    db_session: Session, project_root: Path, tmp_path: Path
) -> None:
    (tmp_path / "disarida").mkdir()

    with pytest.raises(PathNotAllowedError):
        list_controller_paths(
            db_session,
            scope=BrowseScope.PROJECT,
            project_id=None,
            path=str(project_root / ".." / "disarida"),
            project_roots=[project_root],
            inventory_roots=[],
        )


def test_missing_and_existing_paths_outside_allowlist_produce_same_error(
    db_session: Session, project_root: Path, tmp_path: Path
) -> None:
    """Allowlist dışında var/yok farkı sızdırılmaz; ikisi de aynı hata sınıfı."""
    existing_outside = tmp_path / "gercek-ama-disarida"
    existing_outside.mkdir()
    missing_outside = tmp_path / "hic-var-olmayan"

    for candidate in (existing_outside, missing_outside):
        with pytest.raises(PathNotAllowedError) as exc_info:
            list_controller_paths(
                db_session,
                scope=BrowseScope.PROJECT,
                project_id=None,
                path=str(candidate),
                project_roots=[project_root],
                inventory_roots=[],
            )
        assert exc_info.value.status_code == 403
        assert exc_info.value.code == "path_not_allowed"


def test_empty_allowlist_is_fail_closed(db_session: Session, project_root: Path) -> None:
    with pytest.raises(PathNotAllowedError):
        list_controller_paths(
            db_session,
            scope=BrowseScope.PROJECT,
            project_id=None,
            path=None,
            project_roots=[],
            inventory_roots=[],
        )


# --- Symlink ve özel dosyaların atlanması ------------------------------------


def test_symlink_entries_are_dropped_regardless_of_target(
    db_session: Session, project_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "disarida"
    outside.mkdir()
    link_directory(project_root / "gorunuste-icerde", outside)
    inside = project_root / "gercek"
    inside.mkdir()
    link_directory(project_root / "ic-baglanti", inside)

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.PROJECT,
        project_id=None,
        path=str(project_root),
        project_roots=[project_root],
        inventory_roots=[],
    )

    names = {entry.name for entry in listing.entries}
    assert "gorunuste-icerde" not in names
    assert "ic-baglanti" not in names
    assert "gercek" in names


@pytest.mark.skipif(sys.platform == "win32", reason="FIFO POSIX'e özgüdür")
def test_special_files_are_skipped(db_session: Session, inventory_root: Path) -> None:
    os.mkfifo(inventory_root / "bir-fifo")
    (inventory_root / "gercek.ini").write_text("[web]\n", encoding="utf-8")

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.INVENTORY,
        project_id=None,
        path=str(inventory_root),
        project_roots=[],
        inventory_roots=[inventory_root],
    )

    names = {entry.name for entry in listing.entries}
    assert names == {"gercek.ini"}


# --- Bounded ve truncated sonuç ----------------------------------------------


def test_result_is_bounded_and_marks_truncated(db_session: Session, project_root: Path) -> None:
    for index in range(MAX_BROWSE_ENTRIES + 1):
        (project_root / f"dosya-{index:04d}.yml").write_text("- hosts: all", encoding="utf-8")

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.PROJECT,
        project_id=None,
        path=str(project_root),
        project_roots=[project_root],
        inventory_roots=[],
    )

    assert listing.truncated is True
    assert len(listing.entries) <= MAX_BROWSE_ENTRIES


class _FakeDirEntry:
    """`os.DirEntry`'nin bu servis tarafından kullanılan yüzeyini taklit eder."""

    def __init__(self, index: int) -> None:
        self.name = f"sahte-{index:06d}.yml"

    def is_symlink(self) -> bool:
        return False

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return False

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return True


class _CountingScandirIterator:
    """`os.scandir()` sonucunu taklit eden ve çekilen girdi sayısını sayan sahte iterator.

    AUDIT-FIX1 bulgu 1'i (gerçek bounded tarama) kanıtlamak için kullanılır:
    yüz binlerce dosya içeren gerçek bir dizin kurmadan, servisin iteratörden
    **gerçekten** en fazla ``MAX_BROWSE_ENTRIES + 1`` girdi çektiğini —
    dizinde bundan çok daha fazlası olsa bile — doğrudan ölçer.

    Sonsuza kadar girdi üretmez: ``_SAFETY_CEILING`` gerçek sınırın (500) çok
    üzerinde ama sonludur. Servis iteratörü sınırlamıyorsa test sonsuz
    döngüye girip donmak yerine ``pulled`` beklenenden büyük bir değerle
    başarısız olur.
    """

    _SAFETY_CEILING = 5_000

    def __init__(self) -> None:
        self.pulled = 0

    def __enter__(self) -> _CountingScandirIterator:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def __iter__(self) -> _CountingScandirIterator:
        return self

    def __next__(self) -> _FakeDirEntry:
        if self.pulled >= self._SAFETY_CEILING:
            raise StopIteration
        entry = _FakeDirEntry(self.pulled)
        self.pulled += 1
        return entry


def test_list_directory_never_pulls_more_than_max_plus_one_raw_entries(
    db_session: Session, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUDIT-FIX1 bulgu 1: sınır gerçek bir kaynak sınırıdır, görünüşte değil.

    Eski uygulama ``sorted(os.scandir(...))`` ile **bütün** dizini önce
    tüketip belleğe alıyor, sınırı ancak ondan **sonra** uyguluyordu. Bu test
    servisin `os.scandir` sonucundan gerçekten en fazla
    ``MAX_BROWSE_ENTRIES + 1`` (501) ham girdi çektiğini — dizin binlerce
    girdi sunsa bile — doğrudan iteratörü sayarak kanıtlar. Yalnızca response
    uzunluğunu ölçmek yetmez: response uzunluğu 500 olsa bile, sınırlanmamış
    bir uygulama arkada milyonlarca girdiyi taramış/sıralamış olabilir.
    """
    counting_iterator = _CountingScandirIterator()
    monkeypatch.setattr(os, "scandir", lambda path: counting_iterator)

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.PROJECT,
        project_id=None,
        path=str(project_root),
        project_roots=[project_root],
        inventory_roots=[],
    )

    assert counting_iterator.pulled == MAX_BROWSE_ENTRIES + 1
    assert listing.truncated is True
    assert len(listing.entries) == MAX_BROWSE_ENTRIES


def test_list_directory_pulls_exactly_the_available_count_when_under_the_limit(
    db_session: Session, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Limit altındaki bir dizinde iteratör tükenene kadar (501'e değil) çekilir."""

    class _FiniteCountingIterator(_CountingScandirIterator):
        _SAFETY_CEILING = 10

    counting_iterator = _FiniteCountingIterator()
    monkeypatch.setattr(os, "scandir", lambda path: counting_iterator)

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.PROJECT,
        project_id=None,
        path=str(project_root),
        project_roots=[project_root],
        inventory_roots=[],
    )

    assert counting_iterator.pulled == 10
    assert listing.truncated is False
    assert len(listing.entries) == 10


def test_result_under_limit_is_not_truncated(db_session: Session, project_root: Path) -> None:
    (project_root / "tek.yml").write_text("- hosts: all", encoding="utf-8")

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.PROJECT,
        project_id=None,
        path=str(project_root),
        project_roots=[project_root],
        inventory_roots=[],
    )

    assert listing.truncated is False


# --- Pasif veya bulunamayan project -------------------------------------------


def test_inactive_project_is_rejected(db_session: Session, project_root: Path) -> None:
    project = _add_project(db_session, project_root / "web", is_active=False)

    with pytest.raises(ProjectInactiveError) as exc_info:
        list_controller_paths(
            db_session,
            scope=BrowseScope.PROJECT_INVENTORY,
            project_id=project.id,
            path=None,
            project_roots=[project_root],
            inventory_roots=[],
        )
    assert exc_info.value.details == {"project_id": project.id}


def test_unknown_project_is_rejected(db_session: Session, project_root: Path) -> None:
    with pytest.raises(NotFoundError):
        list_controller_paths(
            db_session,
            scope=BrowseScope.PROJECT_INVENTORY,
            project_id=4242,
            path=None,
            project_roots=[project_root],
            inventory_roots=[],
        )


# --- scope / project_id kombinasyon hataları ---------------------------------


def test_project_inventory_scope_requires_project_id(
    db_session: Session, project_root: Path
) -> None:
    with pytest.raises(BrowseInvalidScopeError):
        list_controller_paths(
            db_session,
            scope=BrowseScope.PROJECT_INVENTORY,
            project_id=None,
            path=None,
            project_roots=[project_root],
            inventory_roots=[],
        )


@pytest.mark.parametrize("scope", [BrowseScope.PROJECT, BrowseScope.INVENTORY])
def test_project_and_inventory_scope_reject_project_id(
    db_session: Session, project_root: Path, inventory_root: Path, scope: BrowseScope
) -> None:
    with pytest.raises(BrowseInvalidScopeError):
        list_controller_paths(
            db_session,
            scope=scope,
            project_id=1,
            path=None,
            project_roots=[project_root],
            inventory_roots=[inventory_root],
        )


# --- Başlangıç görünümü: tek kök vs sentetik kök seçici ----------------------


def test_initial_view_with_single_root_lists_it_directly(
    db_session: Session, project_root: Path
) -> None:
    (project_root / "web").mkdir()

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.PROJECT,
        project_id=None,
        path=None,
        project_roots=[project_root],
        inventory_roots=[],
    )

    assert listing.current_path == str(project_root.resolve())
    assert [entry.name for entry in listing.entries] == ["web"]


def test_initial_view_with_multiple_roots_is_synthetic(db_session: Session, tmp_path: Path) -> None:
    first = tmp_path / "bir"
    second = tmp_path / "iki"
    first.mkdir()
    second.mkdir()

    listing = list_controller_paths(
        db_session,
        scope=BrowseScope.PROJECT,
        project_id=None,
        path=None,
        project_roots=[first, second],
        inventory_roots=[],
    )

    assert listing.current_path is None
    paths = {entry.path for entry in listing.entries}
    assert paths == {str(first.resolve()), str(second.resolve())}
    assert all(entry.selectable for entry in listing.entries)
    assert listing.truncated is False


# --- Okunamayan dizin ---------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="chmod POSIX izin modelidir")
def test_unreadable_directory_is_reported(db_session: Session, project_root: Path) -> None:
    locked = project_root / "kilitli"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(BrowseDirectoryUnreadableError):
            list_controller_paths(
                db_session,
                scope=BrowseScope.PROJECT,
                project_id=None,
                path=str(locked),
                project_roots=[project_root],
                inventory_roots=[],
            )
    finally:
        locked.chmod(0o700)


# --- Endpoint yazmaz, subprocess çalıştırmaz, dosya içeriği okumaz -----------


def test_service_module_never_touches_subprocess_or_file_contents() -> None:
    """Statik bir kaynak-kodu koruması: bu regresyonun testi çok geç olurdu.

    Modül; ``subprocess``, kabuk çağrısı veya dosya içeriği okuma (``open``,
    ``read_text``, ``read_bytes``) hiç **import etmemeli veya çağırmamalıdır**.
    Yalnızca ``os.scandir``/``os.stat`` seviyesinde metadata okunur.
    """
    source = inspect.getsource(browse_service)

    for forbidden in ("subprocess", "os.system", "open(", ".read_text(", ".read_bytes("):
        assert forbidden not in source, f"'{forbidden}' browse servisinde bulunmamalı"
