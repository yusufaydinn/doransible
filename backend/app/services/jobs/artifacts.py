"""Descriptor-relative ve atomik Job artifact deposu (T-204B1)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.core.errors import AppError

RESULT_FILENAME = "result.json"
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_TEMP = re.compile(r"^\.result-[0-9a-f]{32}\.tmp$")


class JobArtifactUnavailableError(AppError):
    status_code = 500
    code = "job_artifact_unavailable"


class JobArtifactPreservedError(JobArtifactUnavailableError):
    """Dizin **yayımlanmış** bir sonuç taşıdığı için korunmuştur.

    :class:`JobArtifactUnavailableError` alt sınıfıdır; eski çağıranların
    davranışı değişmez. Ayrı sınıf olmasının tek sebebi, stale Job kurtarmasının
    "görünür sonucu koru" (beklenen) durumunu gerçek bir I/O arızasından ayırt
    edebilmesidir (T-204B2).
    """


class _RootMissingError(OSError):
    """Artifact kökü hiç oluşturulmamış. Yalnızca modül içinde kullanılır."""


class JobArtifactStore:
    """`app-data/jobs/<uuid>/result.json` deposu."""

    def __init__(self, app_data_dir: Path) -> None:
        self._app_data = app_data_dir
        self._root = app_data_dir / "jobs"

    def create(self, job_id: str) -> str:
        """Job dizinini 0700 oluşturur ve göreli artifact path döndürür."""
        name = _job_name(job_id)
        with self._root_fd(create=True) as root_fd:
            try:
                os.mkdir(name, 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
                with _directory(root_fd, name) as job_fd:
                    os.fchmod(job_fd, 0o700)
            except OSError as exc:
                raise JobArtifactUnavailableError("Job artifact dizini oluşturulamadı.") from exc
        return f"jobs/{name}"

    def write_result(self, job_id: str, result: dict[str, Any]) -> str:
        """Sonucu private geçici dosyadan atomik rename ile yayımlar."""
        name = _job_name(job_id)
        content = json.dumps(result, sort_keys=True, ensure_ascii=True) + "\n"
        temporary = f".result-{secrets.token_hex(16)}.tmp"
        try:
            with self._root_fd(create=False) as root_fd, _directory(root_fd, name) as job_fd:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=job_fd,
                )
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        os.fchmod(handle.fileno(), 0o600)
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.rename(temporary, RESULT_FILENAME, src_dir_fd=job_fd, dst_dir_fd=job_fd)
                    os.fsync(job_fd)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(temporary, dir_fd=job_fd)
                    raise
        except (OSError, ValueError) as exc:
            raise JobArtifactUnavailableError("Job sonucu yazılamadı.") from exc
        return f"jobs/{name}/{RESULT_FILENAME}"

    def cleanup(self, job_id: str, *, missing_ok: bool = False) -> None:
        """Yalnız yayımlanmamış temp/boş dizini temizler; sonucu daima korur.

        Args:
            job_id: Canonical UUID4 Job kimliği.
            missing_ok: ``True`` ise hiç oluşturulmamış bir kök veya Job dizini
                sessizce no-op'tur. Varsayılan ``False``; T-204B1 çağıranlarının
                davranışı değişmez.

        Raises:
            JobArtifactPreservedError: Dizinde yayımlanmış ``result.json`` var.
                Görünür sonuç asla silinmez.
            JobArtifactUnavailableError: Beklenmeyen içerik, symlink veya I/O
                arızası. Bu durumlar gizlenmez; dizin korunur.
        """
        name = _job_name(job_id)
        try:
            with self._root_fd(create=False, missing_ok=missing_ok) as root_fd:
                try:
                    with _directory(root_fd, name) as job_fd:
                        entries = os.listdir(job_fd)
                        if RESULT_FILENAME in entries:
                            raise JobArtifactPreservedError(
                                "Job artifact dizini yayımlanmış sonucu koruyor."
                            )
                        if any(not _TEMP.fullmatch(entry) for entry in entries):
                            raise JobArtifactUnavailableError("Job artifact dizini temizlenemedi.")
                        for entry in entries:
                            os.unlink(entry, dir_fd=job_fd)
                        _same_entry(job_fd, root_fd, name)
                except FileNotFoundError:
                    if missing_ok:
                        return
                    raise
                os.rmdir(name, dir_fd=root_fd)
                os.fsync(root_fd)
        except _RootMissingError:
            return
        except JobArtifactUnavailableError:
            raise
        except OSError as exc:
            raise JobArtifactUnavailableError("Job artifact dizini temizlenemedi.") from exc

    @contextlib.contextmanager
    def _root_fd(self, *, create: bool, missing_ok: bool = False) -> Iterator[int]:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise JobArtifactUnavailableError("Job artifact deposu desteklenmiyor.")
        if create:
            try:
                self._app_data.mkdir(parents=True, exist_ok=True)
                self._root.mkdir(exist_ok=True)
            except OSError as exc:
                raise JobArtifactUnavailableError("Job artifact kökü hazırlanamadı.") from exc
        try:
            descriptor = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError as exc:
            if missing_ok:
                raise _RootMissingError("Job artifact kökü yok.") from exc
            raise JobArtifactUnavailableError("Job artifact kökü açılamadı.") from exc
        except OSError as exc:
            raise JobArtifactUnavailableError("Job artifact kökü açılamadı.") from exc
        try:
            _same_path(descriptor, self._root)
            os.fchmod(descriptor, 0o700)
            yield descriptor
            _same_path(descriptor, self._root)
        finally:
            os.close(descriptor)


def _job_name(value: str) -> str:
    if not _UUID4.fullmatch(value):
        raise ValueError("Job id canonical UUID4 olmalıdır.")
    parsed = uuid.UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("Job id canonical UUID4 olmalıdır.")
    return value


@contextlib.contextmanager
def _directory(parent_fd: int, name: str) -> Iterator[int]:
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        _same_entry(descriptor, parent_fd, name)
        yield descriptor
    finally:
        os.close(descriptor)


def _same_entry(child_fd: int, parent_fd: int, name: str) -> None:
    opened = os.fstat(child_fd)
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise OSError("Job artifact girdisi değiştirildi.")


def _same_path(descriptor: int, path: Path) -> None:
    opened = os.fstat(descriptor)
    named = os.stat(path, follow_symlinks=False)
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise OSError("Job artifact kökü değiştirildi.")
