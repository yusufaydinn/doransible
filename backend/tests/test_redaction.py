"""Secret redaction (T-202, GUVENLIK.md bölüm 9)."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.security.redaction import (
    REDACTED,
    is_secret_key,
    looks_like_secret_value,
    redact_mapping,
    redact_text,
    redact_value,
)

PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEA\n-----END OPENSSH PRIVATE KEY-----"
)
VAULT_BLOB = "$ANSIBLE_VAULT;1.1;AES256\n33633462386236316...\n"


@pytest.mark.parametrize(
    "key",
    [
        "ansible_password",
        "ansible_become_password",
        "ansible_ssh_pass",
        "api_token",
        "API_TOKEN",
        "apiKey",
        "aws_access_key",
        "vault_password_file",
        "authorization",
        "db_credential",
        "session_key",
        "ansible_ssh_private_key_file",
        "my_secret_thing",
        "user_pwd",
    ],
)
def test_secret_key_names_are_detected(key: str) -> None:
    assert is_secret_key(key) is True


@pytest.mark.parametrize(
    "key",
    ["ansible_host", "ansible_port", "ansible_user", "datacenter", "role", "http_port"],
)
def test_ordinary_key_names_are_not_flagged(key: str) -> None:
    assert is_secret_key(key) is False


@pytest.mark.parametrize(
    "value",
    [
        PRIVATE_KEY,
        VAULT_BLOB,
        "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def",
        "bearer sk-1234567890",
        "Authorization: Bearer sk-live-xyz",
        "psql://kullanici?password=hunter2",
        "token = ghp_abcdefghijklmnop",
    ],
)
def test_secret_shaped_values_are_detected(value: str) -> None:
    assert looks_like_secret_value(value) is True


@pytest.mark.parametrize(
    "value",
    ["10.0.0.10", "web01.example.com", "/etc/ansible", "22", "ubuntu", "bearer"],
)
def test_ordinary_values_are_not_flagged(value: str) -> None:
    assert looks_like_secret_value(value) is False


def test_secret_key_masks_the_whole_value() -> None:
    """Anahtar secret ise değere hiç bakılmaz; iç yapı da açığa çıkmaz."""
    result = redact_mapping({"ansible_password": "hunter2", "ansible_host": "10.0.0.10"})

    assert result == {"ansible_password": REDACTED, "ansible_host": "10.0.0.10"}


def test_secret_shaped_value_is_masked_under_an_innocent_key() -> None:
    """Masum adlı bir değişken private key taşıyabilir."""
    result = redact_mapping({"notes": PRIVATE_KEY})

    assert result == {"notes": REDACTED}


def test_nested_dictionaries_are_redacted() -> None:
    payload = {
        "app": {
            "database": {"host": "db01", "password": "hunter2"},
            "cache": {"url": "redis://db01:6379"},
        }
    }

    result = redact_mapping(payload)

    assert result == {
        "app": {
            "database": {"host": "db01", "password": REDACTED},
            "cache": {"url": "redis://db01:6379"},
        }
    }


def test_nested_lists_are_redacted() -> None:
    payload = {"servers": [{"name": "web01", "api_key": "abc"}, {"name": "web02"}]}

    result = redact_mapping(payload)

    assert result == {"servers": [{"name": "web01", "api_key": REDACTED}, {"name": "web02"}]}


def test_secret_key_masks_a_nested_structure_without_descending() -> None:
    """Secret anahtarın altındaki yapı da görünmez; yalnızca değerleri değil."""
    result = redact_mapping({"vault": {"inner": {"deep": "gizli"}, "list": [1, 2]}})

    assert result == {"vault": REDACTED}


def test_secret_shaped_value_inside_a_list_is_masked() -> None:
    result = redact_mapping({"keys": [PRIVATE_KEY, "masum"]})

    assert result == {"keys": [REDACTED, "masum"]}


def test_structure_is_preserved() -> None:
    """Kullanıcı hangi değişkenin var olduğunu görür, içeriğini görmez."""
    payload = {"ansible_host": "10.0.0.10", "api_token": "abc", "tags": ["web", "prod"]}

    result = redact_mapping(payload)

    assert set(result) == set(payload)
    assert result["tags"] == ["web", "prod"]


def test_non_string_scalars_are_preserved() -> None:
    result = redact_mapping({"port": 22, "enabled": True, "weight": 1.5, "none": None})

    assert result == {"port": 22, "enabled": True, "weight": 1.5, "none": None}


def test_binary_values_are_masked_without_inspection() -> None:
    assert redact_value(b"\x00\x01gizli") == REDACTED


def test_input_is_not_mutated() -> None:
    payload = {"inner": {"password": "hunter2"}}

    redact_mapping(payload)

    assert payload == {"inner": {"password": "hunter2"}}


def test_non_string_keys_are_stringified() -> None:
    """Parser çıktısı JSON'dan gelir ama savunma tip varsayımına dayanmamalıdır."""
    payload: dict[Any, Any] = {1: "bir"}

    assert redact_mapping(payload) == {"1": "bir"}


# --- Metin içi maskeleme (hata mesajları, loglar) ------------------------------


def test_text_redaction_keeps_the_explanation() -> None:
    """Hata metninin açıklayıcı kısmı korunur, yalnızca secret çıkarılır."""
    text = "ERROR! Unable to parse inventory: ansible_password=hunter2 satirinda"

    result = redact_text(text)

    assert "hunter2" not in result
    assert "Unable to parse inventory" in result
    assert REDACTED in result


def test_underscore_prefixed_variable_names_are_matched() -> None:
    """`ansible_password=` içindeki alt çizgi maskelemeyi engellememelidir.

    `\\bpassword\\b` kullanan bir desen tam da yakalaması gereken Ansible
    değişkenlerini kaçırırdı; bu test o regresyonu kapatır.
    """
    for text in (
        "ansible_password=hunter2",
        "ansible_become_pass: hunter2",
        "app.api_token = ghp_abc",
        "X-Authorization: sk-live",
    ):
        assert "hunter2" not in redact_text(text)
        assert "ghp_abc" not in redact_text(text)
        assert "sk-live" not in redact_text(text)


def test_text_redaction_masks_private_key_blocks() -> None:
    text = f"onceki satir\n{PRIVATE_KEY}\nsonraki satir"

    result = redact_text(text)

    assert "b3BlbnNzaC1rZXktdjEA" not in result
    assert "onceki satir" in result
    assert "sonraki satir" in result


def test_text_redaction_masks_vault_blobs() -> None:
    result = redact_text(f"deger: {VAULT_BLOB}")

    assert "33633462386236316" not in result


def test_text_redaction_masks_bearer_tokens() -> None:
    result = redact_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc")

    assert "eyJhbGciOiJIUzI1NiJ9.abc" not in result


def test_text_redaction_leaves_ordinary_text_untouched() -> None:
    text = "Unable to parse <path> as an inventory source"

    assert redact_text(text) == text
