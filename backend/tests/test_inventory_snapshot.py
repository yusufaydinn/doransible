"""Hostvar allowlist ve güvenli snapshot üretimi (T-204A)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.ansible.inventory_snapshot import (
    InventoryUnsafeError,
    SnapshotPlan,
    build_snapshot_plan,
    render_full_snapshot,
    render_target_snapshot,
    revalidate_snapshot_private_keys,
)

# Controller üzerinde çalıştırılacak ikiliyi veya SSH seçeneklerini seçen
# değişkenler. Hepsi fail-closed reddedilir.
CONTROLLER_EXECUTION_VARIABLES = [
    "ansible_ssh_executable",
    "ansible_scp_executable",
    "ansible_sftp_executable",
    "ansible_shell_executable",
    "ansible_become_exe",
    "ansible_ssh_args",
    "ansible_ssh_common_args",
    "ansible_ssh_extra_args",
    "ansible_sftp_extra_args",
    "ansible_scp_extra_args",
    "ansible_network_os",
    "ansible_ssh_pipelining",
]


def _plan(
    host_variables: dict[str, dict[str, Any]],
    *,
    key_roots: list[Path] | None = None,
    direct_hosts: dict[str, set[str]] | None = None,
    children: dict[str, set[str]] | None = None,
) -> SnapshotPlan:
    return build_snapshot_plan(
        host_variables,
        direct_hosts if direct_hosts is not None else {},
        children if children is not None else {},
        key_roots=key_roots or [],
    )


# --- Pozitif allowlist --------------------------------------------------------


def test_allowed_connection_fields_are_carried(secrets_root: Path) -> None:
    key = secrets_root / "id_ed25519"
    key.write_text("anahtar", encoding="utf-8")

    plan = _plan(
        {
            "web01": {
                "ansible_host": "10.0.0.10",
                "ansible_port": 2222,
                "ansible_user": "deploy",
                "ansible_ssh_private_key_file": str(key),
                "ansible_python_interpreter": "/usr/bin/python3",
            }
        },
        key_roots=[secrets_root],
    )

    assert plan.hosts["web01"] == {
        "ansible_host": "10.0.0.10",
        "ansible_port": 2222,
        "ansible_user": "deploy",
        "ansible_ssh_private_key_file": str(key),
        "ansible_python_interpreter": "/usr/bin/python3",
    }


def test_non_ansible_user_variables_are_dropped_silently() -> None:
    """Uygulama verisi bağlantı semantiğini etkilemez; hata da üretmez."""
    plan = _plan({"web01": {"ansible_host": "10.0.0.10", "http_port": 8080, "app": {"a": 1}}})

    assert plan.hosts["web01"] == {"ansible_host": "10.0.0.10"}


def test_ansible_connection_ssh_is_accepted_but_not_carried() -> None:
    """`ssh` kabul edilir ama snapshot'a **yazılmaz**.

    Varsayılan zaten ssh'tir; tek bir kanonik yol bırakmak, başka bir
    connection plugin'inin kazara devreye girmesini imkânsız kılar.
    """
    plan = _plan({"web01": {"ansible_connection": "ssh", "ansible_host": "10.0.0.10"}})

    assert plan.hosts["web01"] == {"ansible_host": "10.0.0.10"}


# --- Fail-closed reddetmeler --------------------------------------------------


@pytest.mark.parametrize("variable", CONTROLLER_EXECUTION_VARIABLES)
def test_controller_execution_variables_are_rejected(variable: str) -> None:
    with pytest.raises(InventoryUnsafeError) as exc_info:
        _plan({"web01": {variable: "/bin/sh"}})

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "ping_inventory_unsafe"
    assert exc_info.value.details == {"host": "web01", "variable": variable}


@pytest.mark.parametrize("value", ["local", "winrm", "docker", "community.docker.docker", ""])
def test_non_ssh_connection_is_rejected(value: str) -> None:
    """`ansible_connection=local` controller üzerinde çalıştırma denemesidir."""
    with pytest.raises(InventoryUnsafeError) as exc_info:
        _plan({"web01": {"ansible_connection": value}})

    assert exc_info.value.details == {"host": "web01", "variable": "ansible_connection"}


@pytest.mark.parametrize("variable", ["ansible_foo", "ansible_gelecek_knob", "ansible_"])
def test_unknown_ansible_variables_are_rejected(variable: str) -> None:
    """Bilinmeyen bir knob sessizce atılmaz.

    Sessiz atma, ping'i kullanıcının gerçek playbook çalıştırmasından farklı
    koşullara sokardı.
    """
    with pytest.raises(InventoryUnsafeError) as exc_info:
        _plan({"web01": {variable: "deger"}})

    assert exc_info.value.details == {"host": "web01", "variable": variable}


@pytest.mark.parametrize(
    "variable", ["ansible_become", "ansible_become_user", "ansible_become_method"]
)
def test_become_variables_are_rejected(variable: str) -> None:
    """Ping become gerektirmez; become alanları taşınmaz."""
    with pytest.raises(InventoryUnsafeError):
        _plan({"web01": {variable: "true"}})


# --- Parola sınıfı ------------------------------------------------------------


@pytest.mark.parametrize("variable", ["ansible_password", "ansible_ssh_pass"])
def test_password_variables_are_rejected_without_naming_them(variable: str) -> None:
    """Parola desteklenmez ve **adı bile** dışarı verilmez.

    Credential service yoktur; parolayı ikinci bir geçici dosyaya kopyalamak
    ani süreç/host çökmesinde düz metin kalıntı bırakabilirdi.
    """
    with pytest.raises(InventoryUnsafeError) as exc_info:
        _plan({"web01": {variable: "hunter2"}})

    rendered = f"{exc_info.value.message} {exc_info.value.details}"
    assert exc_info.value.code == "ping_inventory_unsafe"
    assert exc_info.value.details is None
    assert variable not in rendered
    assert "hunter2" not in rendered
    assert "credential" in rendered.lower()


def test_password_value_never_reaches_a_snapshot() -> None:
    """Parola içeren inventory hiçbir snapshot üretemez."""
    with pytest.raises(InventoryUnsafeError):
        _plan({"web01": {"ansible_host": "10.0.0.10", "ansible_password": "hunter2"}})


# --- SSH hedefi ---------------------------------------------------------------


def test_option_like_ansible_host_is_rejected() -> None:
    with pytest.raises(InventoryUnsafeError) as exc_info:
        _plan({"web01": {"ansible_host": "-oProxyCommand=/bin/sh"}})

    assert exc_info.value.details == {"host": "web01", "variable": "ansible_host"}


def test_user_at_host_in_ansible_host_is_rejected() -> None:
    with pytest.raises(InventoryUnsafeError) as exc_info:
        _plan({"web01": {"ansible_host": "root@10.0.0.10", "ansible_user": "deploy"}})

    assert exc_info.value.details == {"host": "web01", "variable": "ansible_host"}


def test_option_like_inventory_hostname_is_rejected_without_ansible_host() -> None:
    """`ansible_host` yoksa etkin hedef inventory host adıdır."""
    with pytest.raises(InventoryUnsafeError) as exc_info:
        _plan({"-F/tmp/config": {}})

    details = exc_info.value.details
    assert isinstance(details, dict)
    assert details["variable"] == "inventory_hostname"


def test_malicious_display_hostname_is_reported_by_index_not_by_name() -> None:
    """Zararlı gösterim adı hata detayında **basılmaz**."""
    with pytest.raises(InventoryUnsafeError) as exc_info:
        _plan({"web01\n[WARNING] sahte satir": {"ansible_host": "10.0.0.10"}})

    details = exc_info.value.details
    assert isinstance(details, dict)
    assert details == {"host_index": 0, "variable": "inventory_hostname"}
    assert "sahte" not in f"{exc_info.value.message} {details}"


def test_safe_ansible_host_does_not_rescue_a_malicious_hostname() -> None:
    """Gösterim adı geçersizse `ansible_host` güvenli olsa da reddedilir."""
    with pytest.raises(InventoryUnsafeError):
        _plan({"web 01": {"ansible_host": "10.0.0.10"}})


# --- Private key yolu ---------------------------------------------------------


def test_private_key_outside_the_allowlist_is_rejected(secrets_root: Path, tmp_path: Path) -> None:
    """Anahtar yolu controller üzerinde dosya okutur; kök dışı reddedilir."""
    outside = tmp_path / "baska" / "id_rsa"
    outside.parent.mkdir()
    outside.write_text("anahtar", encoding="utf-8")

    with pytest.raises(InventoryUnsafeError) as exc_info:
        _plan(
            {"web01": {"ansible_ssh_private_key_file": str(outside)}},
            key_roots=[secrets_root],
        )

    details = exc_info.value.details
    assert isinstance(details, dict)
    assert details == {"host": "web01", "variable": "ansible_ssh_private_key_file"}
    assert str(outside) not in f"{exc_info.value.message} {details}"


def test_missing_private_key_is_rejected(secrets_root: Path) -> None:
    with pytest.raises(InventoryUnsafeError):
        _plan(
            {"web01": {"ansible_ssh_private_key_file": str(secrets_root / "yok")}},
            key_roots=[secrets_root],
        )


def test_private_key_path_is_not_leaked_in_the_error(secrets_root: Path) -> None:
    """Reddedilen anahtarın yolu hata metnine yazılmaz."""
    with pytest.raises(InventoryUnsafeError) as exc_info:
        _plan(
            {"web01": {"ansible_ssh_private_key_file": "/root/.ssh/id_rsa"}},
            key_roots=[secrets_root],
        )

    assert "/root/.ssh" not in f"{exc_info.value.message} {exc_info.value.details}"


def _execution_snapshot(variable: str | None = None, value: Any = None) -> str:
    variables = {} if variable is None else {variable: value}
    return json.dumps({"all": {"hosts": {"web01": variables}}})


@pytest.mark.parametrize(
    "variable",
    ["ansible_private_key_file", "ansible_ssh_private_key_file"],
)
def test_execution_revalidation_accepts_existing_allowlisted_key(
    secrets_root: Path, variable: str
) -> None:
    key = secrets_root / "id_ed25519"
    key.write_text("private-material", encoding="utf-8")

    revalidate_snapshot_private_keys(
        _execution_snapshot(variable, str(key)),
        key_roots=[secrets_root],
    )


def test_execution_revalidation_rejects_key_deleted_after_preview(
    secrets_root: Path,
) -> None:
    key = secrets_root / "id_ed25519"
    key.write_text("private-material", encoding="utf-8")
    snapshot = _execution_snapshot("ansible_ssh_private_key_file", str(key))
    key.unlink()

    with pytest.raises(InventoryUnsafeError):
        revalidate_snapshot_private_keys(snapshot, key_roots=[secrets_root])


def test_execution_revalidation_rejects_key_replaced_by_outside_symlink(
    secrets_root: Path, tmp_path: Path
) -> None:
    key = secrets_root / "id_ed25519"
    key.write_text("original", encoding="utf-8")
    snapshot = _execution_snapshot("ansible_private_key_file", str(key))
    outside = tmp_path / "outside-key"
    outside.write_text("outside-secret", encoding="utf-8")
    key.unlink()
    key.symlink_to(outside)

    with pytest.raises(InventoryUnsafeError) as exc_info:
        revalidate_snapshot_private_keys(snapshot, key_roots=[secrets_root])

    rendered = f"{exc_info.value.message} {exc_info.value.details}"
    assert str(key) not in rendered
    assert str(outside) not in rendered
    assert "outside-secret" not in rendered


def test_execution_revalidation_rejects_directory_instead_of_key(
    secrets_root: Path,
) -> None:
    key = secrets_root / "id_ed25519"
    key.mkdir()

    with pytest.raises(InventoryUnsafeError):
        revalidate_snapshot_private_keys(
            _execution_snapshot("ansible_ssh_private_key_file", str(key)),
            key_roots=[secrets_root],
        )


@pytest.mark.parametrize(
    "snapshot",
    [
        "not-json",
        "{}",
        json.dumps({"all": {"hosts": []}}),
        json.dumps({"all": {"hosts": {"web01": []}}}),
    ],
)
def test_execution_revalidation_rejects_malformed_snapshot(snapshot: str) -> None:
    with pytest.raises(InventoryUnsafeError):
        revalidate_snapshot_private_keys(snapshot, key_roots=[])


def test_execution_revalidation_accepts_snapshot_without_key() -> None:
    revalidate_snapshot_private_keys(
        _execution_snapshot(),
        key_roots=[],
    )


# --- Port ve interpreter ------------------------------------------------------


@pytest.mark.parametrize("value", [0, 65536, -1, "abc", True, 3.5])
def test_invalid_ports_are_rejected(value: Any) -> None:
    with pytest.raises(InventoryUnsafeError):
        _plan({"web01": {"ansible_port": value}})


def test_string_port_is_normalised_to_an_integer() -> None:
    plan = _plan({"web01": {"ansible_port": "2222"}})

    assert plan.hosts["web01"]["ansible_port"] == 2222


@pytest.mark.parametrize(
    "value",
    ["/bin/sh -c id", "python3", "$(which python3)", "/usr/bin/python3;id", ""],
)
def test_unsafe_interpreters_are_rejected(value: str) -> None:
    with pytest.raises(InventoryUnsafeError):
        _plan({"web01": {"ansible_python_interpreter": value}})


@pytest.mark.parametrize("value", ["auto", "auto_silent", "/usr/bin/python3"])
def test_safe_interpreters_are_accepted(value: str) -> None:
    plan = _plan({"web01": {"ansible_python_interpreter": value}})

    assert plan.hosts["web01"]["ansible_python_interpreter"] == value


# --- Snapshot biçimi ----------------------------------------------------------


def test_full_snapshot_keeps_group_topology() -> None:
    """Snapshot A grup yapısını korur; grup/kesişim limitleri buna bağlıdır."""
    plan = _plan(
        {"web01": {"ansible_host": "10.0.0.10"}, "db01": {"ansible_host": "10.0.0.20"}},
        direct_hosts={
            "web": {"web01"},
            "db": {"db01"},
            "production": set(),
            "all": {"web01", "db01"},
        },
        children={"production": {"web", "db"}},
    )

    document = json.loads(render_full_snapshot(plan))

    assert set(document["all"]["hosts"]) == {"web01", "db01"}
    assert document["all"]["hosts"]["web01"] == {"ansible_host": "10.0.0.10"}
    assert document["all"]["children"]["web"]["hosts"] == {"web01": None}
    assert document["all"]["children"]["production"]["children"] == {
        "db": None,
        "web": None,
    }
    # Örtük gruplar yeniden tanımlanmaz.
    assert "all" not in document["all"]["children"]


def test_target_snapshot_carries_only_targets_and_no_groups() -> None:
    """Snapshot B yalnızca hedefleri taşır; `--limit` Phase 2'de kullanılmaz."""
    plan = _plan(
        {
            "web01": {"ansible_host": "10.0.0.10"},
            "web02": {"ansible_host": "10.0.0.11"},
        },
        direct_hosts={"web": {"web01", "web02"}},
    )

    document = json.loads(render_target_snapshot(plan, ["web01"]))

    assert document == {"all": {"hosts": {"web01": {"ansible_host": "10.0.0.10"}}}}
    assert "children" not in document["all"]


def test_snapshot_rendering_is_deterministic() -> None:
    """Aynı girdi aynı baytları üretir; digest karşılaştırılabilir olur."""
    variables = {
        "web02": {"ansible_port": 22, "ansible_host": "10.0.0.11"},
        "web01": {"ansible_host": "10.0.0.10", "ansible_port": 22},
    }

    first = render_target_snapshot(_plan(variables), ["web02", "web01"])
    second = render_target_snapshot(_plan(variables), ["web01", "web02"])

    assert first == second


def test_hosts_named_only_in_groups_are_also_validated() -> None:
    """`_meta.hostvars` dışında kalan grup üyeleri de doğrulanır."""
    with pytest.raises(InventoryUnsafeError):
        _plan({}, direct_hosts={"web": {"-oProxyCommand=/bin/sh"}})
