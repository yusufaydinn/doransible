"""Job artifact deposu symlink, atomiklik ve cleanup regresyonları."""

from __future__ import annotations

import errno
import json
import os
import stat
import uuid
from pathlib import Path

import pytest

from app.services.jobs import artifacts
from app.services.jobs.artifacts import (
    RESULT_FILENAME,
    JobArtifactPreservedError,
    JobArtifactStore,
    JobArtifactUnavailableError,
)


@pytest.fixture
def job_id() -> str:
    return str(uuid.uuid4())


def test_create_and_atomic_result_permissions(tmp_path: Path, job_id: str) -> None:
    store = JobArtifactStore(tmp_path / "data")
    assert store.create(job_id) == f"jobs/{job_id}"
    result_path = store.write_result(job_id, {"status": "ok"})
    path = tmp_path / "data" / result_path
    assert json.loads(path.read_text()) == {"status": "ok"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not [entry for entry in path.parent.iterdir() if entry.name.startswith(".result-")]


def test_symlink_root_and_job_directory_fail_closed(tmp_path: Path, job_id: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    os.symlink(outside, data / "jobs", target_is_directory=True)
    with pytest.raises(JobArtifactUnavailableError):
        JobArtifactStore(data).create(job_id)
    assert list(outside.iterdir()) == []


def test_result_symlink_is_replaced_not_followed(tmp_path: Path, job_id: str) -> None:
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    os.symlink(outside, tmp_path / "data" / "jobs" / job_id / RESULT_FILENAME)
    store.write_result(job_id, {"safe": True})
    assert outside.read_text() == "keep"


def test_existing_temporary_symlink_is_not_followed(
    tmp_path: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    temporary = tmp_path / "data" / "jobs" / job_id / f".result-{'a' * 32}.tmp"
    os.symlink(outside, temporary)
    monkeypatch.setattr(artifacts.secrets, "token_hex", lambda _size: "a" * 32)
    with pytest.raises(JobArtifactUnavailableError):
        store.write_result(job_id, {"safe": True})
    assert outside.read_text() == "keep"


def test_unexpected_content_blocks_cleanup(tmp_path: Path, job_id: str) -> None:
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)
    foreign = tmp_path / "data" / "jobs" / job_id / "foreign.bin"
    foreign.write_text("keep")
    with pytest.raises(JobArtifactUnavailableError):
        store.cleanup(job_id)
    assert foreign.read_text() == "keep"


def test_cleanup_of_a_missing_directory_is_an_error_by_default(tmp_path: Path, job_id: str) -> None:
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)
    (tmp_path / "data" / "jobs" / job_id).rmdir()

    with pytest.raises(JobArtifactUnavailableError):
        store.cleanup(job_id)


@pytest.mark.parametrize("prepare", ["no-root", "no-directory"])
def test_cleanup_with_missing_ok_is_a_no_op(tmp_path: Path, job_id: str, prepare: str) -> None:
    """Stale kurtarma, hiç oluşturulmamış bir dizin için hata üretmemelidir."""
    store = JobArtifactStore(tmp_path / "data")
    if prepare == "no-directory":
        store.create(job_id)
        (tmp_path / "data" / "jobs" / job_id).rmdir()

    store.cleanup(job_id, missing_ok=True)


def test_published_result_is_reported_as_preserved_not_as_io_failure(
    tmp_path: Path, job_id: str
) -> None:
    """Korunan sonuç ile gerçek arıza ayrı sınıflardır (T-204B2)."""
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)
    store.write_result(job_id, {"safe": True})

    with pytest.raises(JobArtifactPreservedError):
        store.cleanup(job_id, missing_ok=True)


def test_unexpected_content_is_not_reported_as_preserved(tmp_path: Path, job_id: str) -> None:
    """Beklenmeyen içerik gizlenmez: `preserved` değil, arızadır."""
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)
    (tmp_path / "data" / "jobs" / job_id / "beklenmeyen.bin").write_text("veri")

    with pytest.raises(JobArtifactUnavailableError) as error:
        store.cleanup(job_id, missing_ok=True)
    assert not isinstance(error.value, JobArtifactPreservedError)


def test_cleanup_preserves_published_result(tmp_path: Path, job_id: str) -> None:
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)
    store.write_result(job_id, {"safe": True})
    directory = tmp_path / "data" / "jobs" / job_id
    result = directory / RESULT_FILENAME
    original = result.read_bytes()
    (directory / f".result-{'b' * 32}.tmp").write_text("partial")

    with pytest.raises(JobArtifactUnavailableError):
        store.cleanup(job_id)

    assert result.read_bytes() == original
    assert directory.exists()


@pytest.mark.parametrize("with_temporary", [False, True])
def test_cleanup_removes_only_unpublished_directory(
    tmp_path: Path, job_id: str, with_temporary: bool
) -> None:
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)
    directory = tmp_path / "data" / "jobs" / job_id
    if with_temporary:
        temporary = directory / f".result-{'b' * 32}.tmp"
        temporary.write_text("partial")
        os.chmod(temporary, 0o600)

    store.cleanup(job_id)

    assert not directory.exists()


def test_post_rename_directory_fsync_failure_preserves_result(
    tmp_path: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)
    real_fsync = os.fsync

    def _fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "directory fsync")
        real_fsync(descriptor)

    monkeypatch.setattr(artifacts.os, "fsync", _fail_directory_fsync)
    with pytest.raises(JobArtifactUnavailableError):
        store.write_result(job_id, {"safe": True})

    directory = tmp_path / "data" / "jobs" / job_id
    result = directory / RESULT_FILENAME
    assert json.loads(result.read_text()) == {"safe": True}
    with pytest.raises(JobArtifactUnavailableError):
        store.cleanup(job_id)
    assert json.loads(result.read_text()) == {"safe": True}


def test_directory_swap_during_cleanup_fails_closed(
    tmp_path: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("keep")
    real_stat = os.stat
    matching_calls = 0

    def _swap_on_revalidation(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal matching_calls
        if path == job_id and dir_fd is not None and not follow_symlinks:
            matching_calls += 1
            if matching_calls == 2:
                os.rename(job_id, f"{job_id}.moved", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                os.symlink(outside, job_id, dir_fd=dir_fd)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(artifacts.os, "stat", _swap_on_revalidation)
    with pytest.raises(JobArtifactUnavailableError):
        store.cleanup(job_id)
    assert marker.read_text() == "keep"


@pytest.mark.parametrize("failure_errno", [errno.EACCES, errno.EIO])
def test_write_permission_or_io_failure_leaves_no_result(
    tmp_path: Path,
    job_id: str,
    monkeypatch: pytest.MonkeyPatch,
    failure_errno: int,
) -> None:
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)
    real_open = os.open

    def _fail_temporary_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if isinstance(path, str) and path.startswith(".result-"):
            raise OSError(failure_errno, "injected")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(artifacts.os, "open", _fail_temporary_open)
    with pytest.raises(JobArtifactUnavailableError):
        store.write_result(job_id, {"safe": True})
    assert list((tmp_path / "data" / "jobs" / job_id).iterdir()) == []


def test_eio_during_publish_leaves_no_half_result(
    tmp_path: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobArtifactStore(tmp_path / "data")
    store.create(job_id)

    def _eio(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(os, "rename", _eio)
    with pytest.raises(JobArtifactUnavailableError):
        store.write_result(job_id, {"safe": True})
    directory = tmp_path / "data" / "jobs" / job_id
    assert not (directory / RESULT_FILENAME).exists()
    assert list(directory.iterdir()) == []
