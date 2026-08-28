"""Path normalizasyonu (T-101, GUVENLIK.md bölüm 4)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.services.security.paths import (
    MAX_PATH_LENGTH,
    InvalidPathError,
    PathIsNotADirectoryError,
    PathIsNotAFileError,
    PathNotAllowedError,
    PathNotFoundError,
    ensure_existing_directory,
    ensure_existing_file,
    ensure_within_allowed_roots,
    normalize_filesystem_path,
    path_comparison_key,
)
from tests.support import link_directory


def test_absolute_path_is_returned_resolved(tmp_path: Path) -> None:
    target = tmp_path / "ansible-projects" / "web"
    target.mkdir(parents=True)

    assert normalize_filesystem_path(str(target)) == target.resolve()


def test_parent_references_are_collapsed(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    noisy = tmp_path / "a" / ".." / "b"

    assert normalize_filesystem_path(str(noisy)) == (tmp_path / "b").resolve()


def test_redundant_separators_and_dot_segments_are_removed(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    noisy = f"{tmp_path}{'/'}{'.'}{'/'}web{'/'}"

    assert normalize_filesystem_path(noisy) == (tmp_path / "web").resolve()


def test_surrounding_whitespace_is_ignored(tmp_path: Path) -> None:
    assert normalize_filesystem_path(f"  {tmp_path}  ") == tmp_path.resolve()


def test_home_shortcut_is_expanded() -> None:
    assert normalize_filesystem_path("~") == Path.home().resolve()


def test_traversal_is_resolved_not_preserved(tmp_path: Path) -> None:
    """`..` sonuca taşınmaz; böylece allowlist kontrolü aldatılamaz.

    Normalizasyon tek başına yetki kontrolü değildir; izin verilen root
    kontrolü T-102 kapsamındadır. Buradaki garanti, karşılaştırmanın
    kanonik path üzerinde yapılabilmesidir.
    """
    escaped = tmp_path / "projects" / ".." / ".." / "disarida"

    result = normalize_filesystem_path(str(escaped))

    assert ".." not in result.parts
    assert result == (tmp_path.parent / "disarida").resolve()


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_empty_path_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidPathError, match="boş"):
        normalize_filesystem_path(raw)


@pytest.mark.parametrize("raw", ["relative/path", "./projects", "projects"])
def test_relative_path_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidPathError, match="absolute"):
        normalize_filesystem_path(raw)


def test_null_byte_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidPathError, match="geçersiz karakter"):
        normalize_filesystem_path(f"{tmp_path}\x00/etc/passwd")


def test_excessively_long_path_is_rejected() -> None:
    with pytest.raises(InvalidPathError, match=str(MAX_PATH_LENGTH)):
        normalize_filesystem_path("/" + "a" * (MAX_PATH_LENGTH + 1))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows case-insensitive davranışı")
def test_comparison_key_folds_case_on_windows() -> None:
    assert path_comparison_key("C:\\Projeler\\Web") == path_comparison_key("c:\\projeler\\web")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ayraç normalizasyonu")
def test_comparison_key_normalises_separators_on_windows() -> None:
    assert path_comparison_key("C:/Projeler/Web") == path_comparison_key("C:\\Projeler\\Web")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX case-sensitive davranışı")
def test_comparison_key_preserves_case_on_posix() -> None:
    assert path_comparison_key("/srv/Projeler") != path_comparison_key("/srv/projeler")


def test_comparison_key_is_stable_for_identical_input(tmp_path: Path) -> None:
    normalized = normalize_filesystem_path(str(tmp_path))

    assert path_comparison_key(normalized) == path_comparison_key(str(normalized))


def test_error_is_an_api_validation_error() -> None:
    """Hata standart 422 zarfına eşlenebilmelidir."""
    error = InvalidPathError("test")

    assert error.status_code == 422
    assert error.code == "invalid_path"


# --- Allowlist kontrolü (T-102, GUVENLIK.md bölüm 4) -------------------------


def test_path_inside_root_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "kok"
    inner = root / "web"
    inner.mkdir(parents=True)

    assert ensure_within_allowed_roots(inner.resolve(), [root]) == inner.resolve()


def test_root_itself_is_allowed(tmp_path: Path) -> None:
    assert ensure_within_allowed_roots(tmp_path.resolve(), [tmp_path]) == tmp_path.resolve()


def test_path_outside_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "kok"
    root.mkdir()

    with pytest.raises(PathNotAllowedError):
        ensure_within_allowed_roots((tmp_path / "disarida").resolve(), [root])


def test_shared_prefix_sibling_is_rejected(tmp_path: Path) -> None:
    """`/x/ansible` root'u `/x/ansible-evil` yolunu kapsamaz.

    Bu, `startswith` ile yapılan bir kontrolün kaçıracağı senaryodur.
    """
    root = tmp_path / "ansible"
    root.mkdir()

    with pytest.raises(PathNotAllowedError):
        ensure_within_allowed_roots((tmp_path / "ansible-evil").resolve(), [root])


def test_parent_of_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "kok"
    root.mkdir()

    with pytest.raises(PathNotAllowedError):
        ensure_within_allowed_roots(tmp_path.resolve(), [root])


def test_empty_allowlist_is_fail_closed(tmp_path: Path) -> None:
    """Root tanımlı değilse hiçbir path kabul edilmez."""
    with pytest.raises(PathNotAllowedError, match="tanımlı değil"):
        ensure_within_allowed_roots(tmp_path.resolve(), [])


def test_any_matching_root_is_sufficient(tmp_path: Path) -> None:
    first = tmp_path / "bir"
    second = tmp_path / "iki"
    first.mkdir()
    second.mkdir()

    assert ensure_within_allowed_roots(second.resolve(), [first, second]) == second.resolve()


def test_symlinked_root_is_resolved_before_comparison(tmp_path: Path) -> None:
    """Root'un kendisi bir bağlantıysa da karşılaştırma kanonik yapılır."""
    real_root = tmp_path / "gercek-kok"
    real_root.mkdir()
    (real_root / "web").mkdir()
    link_root = link_directory(tmp_path / "baglanti-kok", real_root)

    candidate = normalize_filesystem_path(str(real_root / "web"))

    assert ensure_within_allowed_roots(candidate, [link_root]) == candidate


def test_normalization_and_allowlist_together_block_symlink_escape(tmp_path: Path) -> None:
    """Root içindeki bağlantı dışarıyı gösteriyorsa iki adım birlikte engeller."""
    root = tmp_path / "kok"
    root.mkdir()
    outside = tmp_path / "disarida"
    outside.mkdir()
    link = link_directory(root / "kacis", outside)

    candidate = normalize_filesystem_path(str(link))

    assert candidate == outside.resolve()
    with pytest.raises(PathNotAllowedError):
        ensure_within_allowed_roots(candidate, [root])


def test_path_not_allowed_error_maps_to_403() -> None:
    error = PathNotAllowedError("test")

    assert error.status_code == 403
    assert error.code == "path_not_allowed"


# --- Varlık kontrolü ---------------------------------------------------------


def test_existing_directory_passes(tmp_path: Path) -> None:
    assert ensure_existing_directory(tmp_path.resolve()) == tmp_path.resolve()


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathNotFoundError) as exc_info:
        ensure_existing_directory((tmp_path / "yok").resolve())

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "path_not_found"


def test_file_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "site.yml"
    target.write_text("- hosts: all", encoding="utf-8")

    with pytest.raises(PathIsNotADirectoryError) as exc_info:
        ensure_existing_directory(target.resolve())

    assert exc_info.value.code == "path_not_a_directory"


def test_existing_file_passes(tmp_path: Path) -> None:
    target = tmp_path / "hosts.ini"
    target.write_text("[web]\nweb01\n", encoding="utf-8")

    assert ensure_existing_file(target.resolve()) == target.resolve()


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathNotFoundError) as exc_info:
        ensure_existing_file((tmp_path / "yok.ini").resolve())

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "path_not_found"


def test_directory_is_rejected_as_file(tmp_path: Path) -> None:
    with pytest.raises(PathIsNotAFileError) as exc_info:
        ensure_existing_file(tmp_path.resolve())

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "path_not_a_file"
