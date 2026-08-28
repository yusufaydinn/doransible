"""Sabit SSH izolasyonu ve environment allowlist testleri."""

from __future__ import annotations

import shlex
import stat
from pathlib import Path

import pytest

from app.services.ansible.ping_execution import build_ping_environment
from app.services.ansible.ssh import (
    SSHPolicyUnavailableError,
    build_ssh_arguments,
    prepare_known_hosts,
)


@pytest.mark.parametrize(("policy", "value"), [("strict", "yes"), ("accept_new", "accept-new")])
def test_complete_ssh_policy_round_trips(tmp_path: Path, policy: str, value: str) -> None:
    known = prepare_known_hosts(tmp_path / "data")
    work = tmp_path / "work"
    work.mkdir()
    arguments = build_ssh_arguments(policy=policy, known_hosts=known, work_dir=work)
    environment = build_ping_environment(work, arguments)
    assert shlex.split(environment["ANSIBLE_SSH_ARGS"]) == arguments
    rendered = " ".join(arguments)
    for required in (
        "-F /dev/null",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        f"UserKnownHostsFile={known}",
        "GlobalKnownHostsFile=/dev/null",
        f"StrictHostKeyChecking={value}",
        "ControlMaster=no",
        "ControlPath=none",
        "ProxyCommand=none",
        "ProxyJump=none",
        "BatchMode=yes",
        "PreferredAuthentications=publickey",
    ):
        assert required in rendered
    assert not (work / "no-identity").exists()
    assert stat.S_IMODE(known.stat().st_mode) == 0o600
    assert stat.S_IMODE(known.parent.stat().st_mode) == 0o700


def test_known_hosts_outside_app_data_is_rejected(tmp_path: Path) -> None:
    data = tmp_path / "data"
    outside = tmp_path / "outside" / "known_hosts"
    outside.parent.mkdir()
    with pytest.raises(SSHPolicyUnavailableError):
        prepare_known_hosts(data, outside)


def test_known_hosts_symlinks_are_not_followed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    ssh_dir = data / "ssh"
    outside = tmp_path / "outside"
    outside.mkdir()
    data.mkdir()
    ssh_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SSHPolicyUnavailableError):
        prepare_known_hosts(data)

    ssh_dir.unlink()
    ssh_dir.mkdir()
    marker = outside / "keep"
    marker.write_text("keep")
    (ssh_dir / "known_hosts").symlink_to(marker)
    with pytest.raises(SSHPolicyUnavailableError):
        prepare_known_hosts(data)
    assert marker.read_text() == "keep"


def test_parent_secrets_and_proxy_are_not_in_execution_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("SSH_AUTH_SOCK", "HOME", "USERPROFILE", "HTTPS_PROXY", "ANSIBLE_FOO", "SECRET_X"):
        monkeypatch.setenv(name, "secret")
    work = tmp_path / "work"
    work.mkdir()
    environment = build_ping_environment(work, [])
    for name in ("SSH_AUTH_SOCK", "HOME", "USERPROFILE", "HTTPS_PROXY", "ANSIBLE_FOO", "SECRET_X"):
        assert name not in environment
    assert environment["ANSIBLE_INVENTORY_ENABLED"] == "yaml"
