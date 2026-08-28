"""Ansible SSH izolasyon politikası (T-204B1)."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from app.core.config import SSH_HOST_KEY_POLICIES
from app.core.errors import AppError


class SSHPolicyUnavailableError(AppError):
    status_code = 500
    code = "ssh_policy_unavailable"


def prepare_known_hosts(app_data_dir: Path, configured: Path | None = None) -> Path:
    """Known-hosts dosyasını app-data/ssh altında 0600 hazırlar."""
    ssh_dir = app_data_dir / "ssh"
    candidate = configured or ssh_dir / "known_hosts"
    try:
        app_root = app_data_dir.resolve()
        resolved_parent = candidate.parent.resolve()
        if resolved_parent != (app_root / "ssh"):
            raise SSHPolicyUnavailableError("Known-hosts yolu güvenli değil.")
        app_data_dir.mkdir(parents=True, exist_ok=True)
        ssh_dir.mkdir(exist_ok=True)
        root_fd = os.open(ssh_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _require_same_inode(os.fstat(root_fd), os.stat(ssh_dir, follow_symlinks=False))
            os.fchmod(root_fd, 0o700)
            descriptor = os.open(
                candidate.name,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_fd,
            )
            try:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                _require_same_inode(
                    os.fstat(descriptor),
                    os.stat(candidate.name, dir_fd=root_fd, follow_symlinks=False),
                )
            finally:
                os.close(descriptor)
            os.fsync(root_fd)
            _require_same_inode(os.fstat(root_fd), os.stat(ssh_dir, follow_symlinks=False))
        finally:
            os.close(root_fd)
    except SSHPolicyUnavailableError:
        raise
    except OSError as exc:
        raise SSHPolicyUnavailableError("Known-hosts dosyası hazırlanamadı.") from exc
    return candidate


def _require_same_inode(opened: os.stat_result, named: os.stat_result) -> None:
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise OSError("SSH policy girdisi değiştirildi.")


def build_ssh_arguments(*, policy: str, known_hosts: Path, work_dir: Path) -> list[str]:
    """Tam ve sabit OpenSSH izolasyon argv parçalarını döndürür."""
    if policy not in SSH_HOST_KEY_POLICIES:
        raise ValueError("Desteklenmeyen host key politikası.")
    strict = "yes" if policy == "strict" else "accept-new"
    return [
        "-F",
        "/dev/null",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"IdentityFile={work_dir / 'no-identity'}",
        "-o",
        "IdentityAgent=none",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        f"StrictHostKeyChecking={strict}",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "BatchMode=yes",
        "-o",
        "PreferredAuthentications=publickey",
    ]


def render_ansible_ssh_args(arguments: list[str]) -> str:
    """Güvenilir argv parçalarını Ansible'ın string ayarına kayıpsız çevirir."""
    rendered = " ".join(shlex.quote(argument) for argument in arguments)
    if shlex.split(rendered) != arguments:  # pragma: no cover - shlex garantisi
        raise ValueError("SSH argümanları kayıpsız gösterilemedi.")
    return rendered
