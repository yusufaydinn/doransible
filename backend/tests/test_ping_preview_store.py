"""Preview token, atomik yayımlama/claim ve dar temizlik (T-204A).

Bu dosya, T-204A denetiminde ölçülen symlink/TOCTOU açığının kapatıldığını da
doğrular: yayımlanmış bir dizin symlink ile değiştirildiğinde claim ve temizlik
dışarıya **hiç dokunmaz**. Testler Linux/POSIX'te atlanmaz; ping preview zaten
yalnızca orada çalışır.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.config import MINIMUM_CLAIM_STALE_SECONDS
from app.services.jobs.preview import (
    CLAIM_FILENAME,
    META_FILENAME,
    SNAPSHOT_FILENAME,
    TOKEN_LENGTH,
    PreviewNotFoundError,
    PreviewRecord,
    PreviewStore,
    PreviewStoreUnavailableError,
    token_digest,
)

SNAPSHOT = '{"all": {"hosts": {"web01": {"ansible_host": "10.0.0.10"}}}}\n'
ACTOR = "local-single-user"
INVENTORY_ID = 1

META: dict[str, Any] = {
    "schema_version": 1,
    "inventory_id": INVENTORY_ID,
    "requested_by": ACTOR,
    "limit": None,
    "host_count": 1,
    "host_key_policy": "strict",
    "operation": "ansible.builtin.ping",
}


@pytest.fixture
def store(tmp_path: Path) -> PreviewStore:
    """İzole bir preview deposu.

    TTL bilinçli olarak claim-stale eşiğinden **küçüktür**: aradaki boşluk,
    "normal TTL doldu ama claim edilmiş state hâlâ korunuyor" durumunun
    ölçülebilmesi için gereklidir.
    """
    return PreviewStore(
        tmp_path / "ping-previews",
        ttl_seconds=60.0,
        claim_stale_seconds=MINIMUM_CLAIM_STALE_SECONDS,
    )


def _published_dir(store: PreviewStore, token: str) -> Path:
    return store.root / token_digest(token)


def _claim(store: PreviewStore, token: str, **overrides: Any) -> PreviewRecord:
    """Varsayılan bağlamla claim eder."""
    arguments: dict[str, Any] = {
        "inventory_id": INVENTORY_ID,
        "requested_by": ACTOR,
    }
    arguments.update(overrides)
    return store.claim(token, **arguments)


def _state_dir(store: PreviewStore, record: PreviewRecord) -> Path:
    """Claim edilmiş dizin — yalnızca test gözlemi için.

    Üretim kodu bu path'i kurmaz; `PreviewRecord` bilinçli olarak path
    taşımaz ve state'e her erişim kök descriptor'ı üzerinden yapılır.
    """
    return store.root / record.directory_name


def _replace_with_symlink(entry: Path, target: Path) -> None:
    """Var olan bir dizini, dışarıyı gösteren bir symlink ile değiştirir."""
    for child in entry.iterdir():
        child.unlink()
    entry.rmdir()
    os.symlink(target, entry, target_is_directory=True)


# --- Token --------------------------------------------------------------------


def test_token_is_256_bit_base64url(store: PreviewStore) -> None:
    """43 karakterlik padding'siz base64url tam 32 bayta çözülür."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    assert len(token) == TOKEN_LENGTH
    padded = token + "=" * (-len(token) % 4)
    assert len(base64.urlsafe_b64decode(padded)) == 32


def test_tokens_are_unique(store: PreviewStore) -> None:
    tokens = {store.publish(meta=META, snapshot_text=SNAPSHOT)[0] for _ in range(20)}

    assert len(tokens) == 20


def test_token_is_never_stored_in_plaintext(store: PreviewStore) -> None:
    """Diskte yalnızca SHA-256 özeti bulunur; token'ın kendisi hiçbir yerde yok."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    directory = _published_dir(store, token)
    assert directory.name == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert token not in directory.name
    for path in directory.iterdir():
        assert token not in path.name
        assert token not in path.read_text(encoding="utf-8")


def test_meta_does_not_contain_the_token(store: PreviewStore) -> None:
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    meta = json.loads((_published_dir(store, token) / META_FILENAME).read_text("utf-8"))
    assert "token" not in json.dumps(meta)
    assert token not in json.dumps(meta)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "kisa",
        "a" * (TOKEN_LENGTH - 1),
        "a" * (TOKEN_LENGTH + 1),
        "a" * 42 + "!",
        "a" * 42 + "/",
        "../../../etc/passwd",
        "a" * 42 + "\n",
    ],
)
def test_malformed_tokens_are_rejected(store: PreviewStore, token: str) -> None:
    """Biçimsiz token, bilinmeyen token ile **aynı** cevabı alır."""
    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "ping_preview_invalid"
    assert exc_info.value.details == {"reason": "invalid"}


def test_unknown_token_reports_invalid_not_already_used(store: PreviewStore) -> None:
    """`already_used` garantisi verilmez.

    Kullanılmış token'ın dizini silindiği için "kullanılmış" ile "hiç var
    olmamış" ayırt edilemez; kalıcı bir tombstone tutmak yalnızca yeni bir
    kalıcı iz üretirdi.
    """
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    store.discard(_claim(store, token))

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token)

    assert exc_info.value.details == {"reason": "invalid"}


# --- Yayımlama ----------------------------------------------------------------


def test_publish_writes_snapshot_and_meta_with_tight_permissions(
    store: PreviewStore,
) -> None:
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    directory = _published_dir(store, token)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    for name in (META_FILENAME, SNAPSHOT_FILENAME):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert (directory / SNAPSHOT_FILENAME).read_text(encoding="utf-8") == SNAPSHOT


def test_publish_records_the_snapshot_digest(store: PreviewStore) -> None:
    """Digest, claim sırasında snapshot'ın bütünlüğünü doğrulamayı sağlar."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    meta = json.loads((_published_dir(store, token) / META_FILENAME).read_text("utf-8"))
    assert meta["snapshot_sha256"] == hashlib.sha256(SNAPSHOT.encode()).hexdigest()


def test_publish_is_atomic_and_leaves_no_building_directory(
    store: PreviewStore,
) -> None:
    """Yayımlama tamamlandığında hazırlık dizini kalmaz."""
    store.publish(meta=META, snapshot_text=SNAPSHOT)

    names = [path.name for path in store.root.iterdir()]
    assert len(names) == 1
    assert not names[0].startswith("building-")


def test_half_written_state_is_never_visible_as_a_preview(
    store: PreviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Yayımlama sırasında hata olursa geçerli bir preview adı oluşmaz."""
    real_rename = os.rename

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("rename engellendi")

    monkeypatch.setattr(os, "rename", _fail)
    with pytest.raises(PreviewStoreUnavailableError) as exc_info:
        store.publish(meta=META, snapshot_text=SNAPSHOT)

    monkeypatch.setattr(os, "rename", real_rename)
    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "ping_preview_unavailable"
    # Ne yayımlanmış ne de yarım bir state kaldı.
    assert list(store.root.iterdir()) == []


def test_snapshot_a_is_not_part_of_the_published_state(store: PreviewStore) -> None:
    """Kalıcı state yalnızca hedef snapshot'ını taşır.

    Snapshot A (bütün inventory + grup topolojisi) yalnızca üretim sırasındaki
    geçici workdir'de bulunur ve oraya kopyalanmaz.
    """
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    names = {path.name for path in _published_dir(store, token).iterdir()}
    assert names == {META_FILENAME, SNAPSHOT_FILENAME}
    assert "inventory-all.yml" not in names


# --- Claim --------------------------------------------------------------------


def test_claim_returns_the_verified_frozen_snapshot(store: PreviewStore) -> None:
    """Record, path değil **doğrulanmış içerik** taşır."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    record = _claim(store, token)

    assert record.meta["inventory_id"] == INVENTORY_ID
    assert record.meta["requested_by"] == ACTOR
    assert record.snapshot_text == SNAPSHOT
    assert not hasattr(record, "path")
    claim_file = _state_dir(store, record) / CLAIM_FILENAME
    assert claim_file.is_file()
    assert stat.S_IMODE(claim_file.stat().st_mode) == 0o600


def test_claim_is_single_use(store: PreviewStore) -> None:
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    _claim(store, token)

    with pytest.raises(PreviewNotFoundError):
        _claim(store, token)


def test_only_one_of_two_concurrent_claims_wins(store: PreviewStore) -> None:
    """Atomik `rename`: iki eşzamanlı claim'den yalnızca biri kazanır."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    def _attempt() -> bool:
        try:
            _claim(store, token)
        except PreviewNotFoundError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _attempt(), range(2)))

    assert sorted(results) == [False, True]


def test_expired_token_is_rejected_and_cleaned(store: PreviewStore) -> None:
    now = datetime.now(UTC)
    token, expires_at = store.publish(meta=META, snapshot_text=SNAPSHOT, now=now)

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token, now=expires_at + timedelta(seconds=1))

    assert exc_info.value.details == {"reason": "expired"}
    # Süresi geçmiş state claim sırasında toplanır.
    assert list(store.root.iterdir()) == []


def test_claim_at_the_expiry_boundary_is_rejected(store: PreviewStore) -> None:
    """Tam son kullanma anında token artık geçerli değildir."""
    now = datetime.now(UTC)
    token, expires_at = store.publish(meta=META, snapshot_text=SNAPSHOT, now=now)

    with pytest.raises(PreviewNotFoundError):
        _claim(store, token, now=expires_at)


def test_discard_removes_the_claimed_state(store: PreviewStore) -> None:
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    record = _claim(store, token)

    store.discard(record)

    assert not _state_dir(store, record).exists()
    assert list(store.root.iterdir()) == []


# --- Claim bağlaması ve içerik doğrulaması ------------------------------------


def test_claim_rejects_a_token_bound_to_another_inventory(store: PreviewStore) -> None:
    """Token, planın üretildiği inventory'ye bağlıdır."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token, inventory_id=INVENTORY_ID + 1)

    assert exc_info.value.details == {"reason": "mismatch"}
    assert list(store.root.iterdir()) == []


def test_claim_rejects_a_token_bound_to_another_actor(store: PreviewStore) -> None:
    """Token, planı isteyen aktöre bağlıdır."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token, requested_by="baska-aktor")

    assert exc_info.value.details == {"reason": "mismatch"}


def test_a_mismatched_token_is_consumed_and_not_reusable(store: PreviewStore) -> None:
    """Eşleşmeyen bir claim state'i tüketir; token tekrar kullanılamaz."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    with pytest.raises(PreviewNotFoundError):
        _claim(store, token, inventory_id=INVENTORY_ID + 1)

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token)
    assert exc_info.value.details == {"reason": "invalid"}


def test_tampered_snapshot_fails_the_digest_check(store: PreviewStore) -> None:
    """Snapshot baytı değişirse claim mismatch üretir.

    Digest'i yalnızca yazmak yetmez; onay ile çalıştırma arasında dondurulmuş
    içeriğin değişmediği claim sırasında **kanıtlanmalıdır**.
    """
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    snapshot = _published_dir(store, token) / SNAPSHOT_FILENAME
    snapshot.write_text(SNAPSHOT.replace("web01", "saldirgan01"), encoding="utf-8")

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token)

    assert exc_info.value.details == {"reason": "mismatch"}
    assert list(store.root.iterdir()) == []


def test_a_single_flipped_byte_is_detected(store: PreviewStore) -> None:
    """Uzunluğu değiştirmeyen tek baytlık bir değişiklik de yakalanır."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    snapshot = _published_dir(store, token) / SNAPSHOT_FILENAME
    snapshot.write_text(SNAPSHOT.replace("10.0.0.10", "10.0.0.11"), encoding="utf-8")

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token)

    assert exc_info.value.details == {"reason": "mismatch"}


def test_tampered_meta_digest_fails_the_check(store: PreviewStore) -> None:
    """Meta'daki digest değiştirilirse de mismatch döner."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    meta_file = _published_dir(store, token) / META_FILENAME
    payload = json.loads(meta_file.read_text(encoding="utf-8"))
    payload["snapshot_sha256"] = "0" * 64
    meta_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token)

    assert exc_info.value.details == {"reason": "mismatch"}


def test_the_digest_comparison_runs_on_the_real_claim_path(
    store: PreviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Karşılaştırma gerçekten claim yolunda çağrılır.

    Doğrulamanın "var ama çağrılmıyor" olmadığını kanıtlar: sabit doğru dönen
    bir karşılaştırma, bozulmuş snapshot'ı kabul ettirebilmelidir.
    """
    calls: list[tuple[str, str]] = []
    import app.services.jobs.preview as preview_module

    def _spy(left: str, right: str) -> bool:
        calls.append((left, right))
        return True

    monkeypatch.setattr(preview_module.hmac, "compare_digest", _spy)

    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    snapshot = _published_dir(store, token) / SNAPSHOT_FILENAME
    tampered = SNAPSHOT.replace("10.0.0.10", "10.0.0.11")
    snapshot.write_text(tampered, encoding="utf-8")

    record = _claim(store, token)

    assert len(calls) == 1
    assert calls[0][0] != calls[0][1]
    assert record.snapshot_text == tampered


@pytest.mark.parametrize(
    "missing",
    [
        "schema_version",
        "created_at",
        "expires_at",
        "inventory_id",
        "requested_by",
        "limit",
        "host_count",
        "host_key_policy",
        "operation",
        "snapshot_sha256",
    ],
)
def test_meta_missing_a_required_field_never_yields_a_record(
    store: PreviewStore, missing: str
) -> None:
    """Eksik meta güvenilir bir record üretmez."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    meta_file = _published_dir(store, token) / META_FILENAME
    payload = json.loads(meta_file.read_text(encoding="utf-8"))
    del payload[missing]
    meta_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token)

    assert exc_info.value.details == {"reason": "mismatch"}
    assert list(store.root.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("created_at", "not-a-timestamp"),
        ("expires_at", "2026-07-31T12:00:00"),
        ("inventory_id", True),
        ("requested_by", ""),
        ("limit", []),
        ("host_count", True),
        ("host_key_policy", "no"),
        ("operation", "ansible.builtin.command"),
        ("snapshot_sha256", "not-a-digest"),
    ],
)
def test_meta_with_an_invalid_field_never_yields_a_record(
    store: PreviewStore, field: str, value: Any
) -> None:
    """Zorunlu alanın yalnızca varlığı değil türü ve değeri de doğrulanır."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    meta_file = _published_dir(store, token) / META_FILENAME
    payload = json.loads(meta_file.read_text(encoding="utf-8"))
    payload[field] = value
    meta_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token)

    assert exc_info.value.details == {"reason": "mismatch"}
    assert list(store.root.iterdir()) == []


def test_meta_host_count_must_match_the_frozen_snapshot(store: PreviewStore) -> None:
    """Geçerli türde fakat snapshot ile tutarsız host sayısı reddedilir."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    meta_file = _published_dir(store, token) / META_FILENAME
    payload = json.loads(meta_file.read_text(encoding="utf-8"))
    payload["host_count"] = 2
    meta_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token)

    assert exc_info.value.details == {"reason": "mismatch"}
    assert list(store.root.iterdir()) == []


def test_corrupt_meta_never_yields_a_record(store: PreviewStore) -> None:
    """Ayrıştırılamayan meta da record üretmez."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    (_published_dir(store, token) / META_FILENAME).write_text("{bozuk", encoding="utf-8")

    with pytest.raises(PreviewNotFoundError) as exc_info:
        _claim(store, token)

    assert exc_info.value.details == {"reason": "mismatch"}


def test_mismatch_cleanup_failure_is_reported_as_unavailable(
    store: PreviewStore,
) -> None:
    """Mismatch temizlenemezse cancel'ın yutacağı bir 409 üretilmez."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    directory = _published_dir(store, token)
    (directory / "beklenmeyen.bin").write_text("veri", encoding="utf-8")
    meta_file = directory / META_FILENAME
    payload = json.loads(meta_file.read_text(encoding="utf-8"))
    payload["inventory_id"] = INVENTORY_ID + 1
    meta_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PreviewStoreUnavailableError) as exc_info:
        _claim(store, token)

    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "ping_preview_unavailable"
    assert (next(store.root.iterdir()) / "beklenmeyen.bin").is_file()


def test_claim_reports_eio_as_unavailable(
    store: PreviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename EIO, bilinmeyen token gibi 409 sınıfına düşmez."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)

    def _fail_with_eio(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "girdi/cikti hatasi")

    monkeypatch.setattr(os, "rename", _fail_with_eio)

    with pytest.raises(PreviewStoreUnavailableError) as exc_info:
        _claim(store, token)

    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "ping_preview_unavailable"


# --- Symlink ve TOCTOU saldırıları --------------------------------------------


def test_claim_refuses_a_published_name_replaced_by_a_symlink(
    store: PreviewStore, tmp_path: Path
) -> None:
    """Ölçülen açık: dış dizini gösteren `<digest>` symlink'i.

    Eski path-tabanlı uygulama symlink'i `.claimed-<uuid>` adına taşıyor, dış
    hedefe `claim.json` yazıyor ve temizleyemiyordu. Descriptor-relative akışta
    hiçbir adım symlink'i izlemez.
    """
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    published = _published_dir(store, token)
    victim = tmp_path / "kurban"
    victim.mkdir()
    (victim / "onemli.txt").write_text("silinmemeli", encoding="utf-8")
    # Saldırgan geçerli görünen dosyaları dış hedefe koyar.
    (victim / META_FILENAME).write_text(
        (published / META_FILENAME).read_text(encoding="utf-8"), encoding="utf-8"
    )
    (victim / SNAPSHOT_FILENAME).write_text(SNAPSHOT, encoding="utf-8")
    _replace_with_symlink(published, victim)

    with pytest.raises(PreviewStoreUnavailableError) as exc_info:
        _claim(store, token)

    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "ping_preview_unavailable"
    assert not (victim / CLAIM_FILENAME).exists()
    assert (victim / "onemli.txt").read_text(encoding="utf-8") == "silinmemeli"
    assert sorted(path.name for path in victim.iterdir()) == sorted(
        [META_FILENAME, SNAPSHOT_FILENAME, "onemli.txt"]
    )


def test_discard_does_not_touch_an_external_target_after_a_swap(
    store: PreviewStore, tmp_path: Path
) -> None:
    """Claim'den **sonra** ad symlink ile değiştirilse bile dışarı korunur."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    record = _claim(store, token)
    victim = tmp_path / "kurban"
    victim.mkdir()
    (victim / "onemli.txt").write_text("silinmemeli", encoding="utf-8")
    (victim / META_FILENAME).write_text("{}", encoding="utf-8")
    _replace_with_symlink(_state_dir(store, record), victim)

    with pytest.raises(PreviewStoreUnavailableError):
        store.discard(record)

    assert (victim / "onemli.txt").read_text(encoding="utf-8") == "silinmemeli"
    assert (victim / META_FILENAME).is_file()


def test_a_symlinked_meta_file_is_never_read(store: PreviewStore, tmp_path: Path) -> None:
    """Bilinen ada konmuş symlink izlenmez."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    published = _published_dir(store, token)
    outside = tmp_path / "disarida-meta.json"
    outside.write_text(json.dumps({**META, "snapshot_sha256": "0" * 64}), encoding="utf-8")
    (published / META_FILENAME).unlink()
    os.symlink(outside, published / META_FILENAME)

    with pytest.raises(PreviewStoreUnavailableError):
        _claim(store, token)

    assert outside.is_file()


def test_a_symlinked_snapshot_file_is_never_read(store: PreviewStore, tmp_path: Path) -> None:
    """Snapshot symlink'i digest doğrulamasında da izlenmez."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    published = _published_dir(store, token)
    outside = tmp_path / "disarida.yml"
    outside.write_text(SNAPSHOT, encoding="utf-8")
    (published / SNAPSHOT_FILENAME).unlink()
    os.symlink(outside, published / SNAPSHOT_FILENAME)

    with pytest.raises(PreviewStoreUnavailableError):
        _claim(store, token)

    assert outside.read_text(encoding="utf-8") == SNAPSHOT


def test_cleanup_removes_a_symlink_entry_without_following_it(
    store: PreviewStore, tmp_path: Path
) -> None:
    """Bilinen ada konmuş symlink'in kendisi silinir; hedefi silinmez."""
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    published = _published_dir(store, token)
    outside = tmp_path / "hedef.txt"
    outside.write_text("korunmali", encoding="utf-8")
    (published / CLAIM_FILENAME).unlink(missing_ok=True)
    os.symlink(outside, published / CLAIM_FILENAME)

    store.sweep(now=datetime.now(UTC) + timedelta(days=1))

    assert not published.exists()
    assert outside.read_text(encoding="utf-8") == "korunmali"


def test_a_symlinked_root_fails_closed(tmp_path: Path) -> None:
    """Kökün kendisi symlink ise depo hiç çalışmaz."""
    real = tmp_path / "gercek-kok"
    real.mkdir()
    link = tmp_path / "ping-previews"
    os.symlink(real, link, target_is_directory=True)
    store = PreviewStore(link, ttl_seconds=60.0, claim_stale_seconds=MINIMUM_CLAIM_STALE_SECONDS)

    with pytest.raises(PreviewStoreUnavailableError) as exc_info:
        store.publish(meta=META, snapshot_text=SNAPSHOT)

    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "ping_preview_unavailable"
    assert list(real.iterdir()) == []


def test_a_directory_swapped_between_check_and_use_is_detected(
    store: PreviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Açık descriptor ile isimdeki girdi ayrışırsa işlem durur.

    `O_NOFOLLOW` yalnızca **açma anındaki** symlink'i reddeder. Bu test,
    açmadan sonra yapılan gerçek bir değiş-tokuşu simüle eder: descriptor eski
    inode'u tutarken ad başka bir dizine bağlanır.
    """
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    decoy = store.root.parent / "sahte"
    decoy.mkdir()
    real_open = os.open
    swapped = False

    def _open_then_swap(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)
        if not swapped and isinstance(path, str) and ".claimed-" in path and flags & os.O_DIRECTORY:
            swapped = True
            target = store.root / path
            os.rename(target, store.root.parent / "kenara")
            os.rename(decoy, target)
        return descriptor

    monkeypatch.setattr(os, "open", _open_then_swap)

    with pytest.raises(PreviewStoreUnavailableError):
        _claim(store, token)

    assert swapped


# --- Süpürme ------------------------------------------------------------------


def test_sweep_removes_expired_unclaimed_previews(store: PreviewStore) -> None:
    now = datetime.now(UTC)
    store.publish(meta=META, snapshot_text=SNAPSHOT, now=now)

    removed = store.sweep(now=now + timedelta(seconds=store.ttl_seconds + 1))

    assert removed == 1
    assert list(store.root.iterdir()) == []


def test_sweep_keeps_a_live_preview(store: PreviewStore) -> None:
    now = datetime.now(UTC)
    store.publish(meta=META, snapshot_text=SNAPSHOT, now=now)

    assert store.sweep(now=now + timedelta(seconds=1)) == 0
    assert len(list(store.root.iterdir())) == 1


def test_sweep_does_not_remove_an_active_claimed_state(store: PreviewStore) -> None:
    """Claim edilmiş state normal preview TTL'siyle **silinemez**.

    Aksi hâlde başka bir isteğin süpürücüsü, o an çalışan bir execution'ın
    dondurulmuş snapshot'ını silebilirdi (T-204B garantisi).
    """
    now = datetime.now(UTC)
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT, now=now)
    record = _claim(store, token, now=now)

    removed = store.sweep(now=now + timedelta(seconds=store.ttl_seconds + 60))

    assert removed == 0
    state = _state_dir(store, record)
    assert state.is_dir()
    assert (state / SNAPSHOT_FILENAME).read_text(encoding="utf-8") == SNAPSHOT


def test_sweep_removes_a_stale_claimed_state(store: PreviewStore) -> None:
    """Ayrı ve daha yüksek claim eşiği aşıldığında artık toplanır."""
    now = datetime.now(UTC)
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT, now=now)
    record = _claim(store, token, now=now)

    removed = store.sweep(now=now + timedelta(seconds=MINIMUM_CLAIM_STALE_SECONDS + 60))

    assert removed == 1
    assert not _state_dir(store, record).exists()


def test_sweep_removes_a_crashed_building_directory(store: PreviewStore) -> None:
    """Yayımlanamadan çökmüş hazırlık artığı dar biçimde toplanır."""
    store.publish(meta=META, snapshot_text=SNAPSHOT)
    building = store.root / f"building-{'a' * 32}"
    building.mkdir()
    (building / META_FILENAME).write_text("{}", encoding="utf-8")
    old = datetime.now(UTC).timestamp() - MINIMUM_CLAIM_STALE_SECONDS - 60
    os.utime(building, (old, old))

    store.sweep()

    assert not building.exists()


def test_sweep_never_follows_symlinks_or_touches_foreign_entries(
    store: PreviewStore, tmp_path: Path
) -> None:
    """Süpürme yalnızca uygulamanın ürettiği ad biçimlerini hedefler."""
    store.root.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "kurban"
    victim.mkdir()
    (victim / "onemli.txt").write_text("silinmemeli", encoding="utf-8")

    foreign_dir = store.root / "rastgele-dizin"
    foreign_dir.mkdir()
    (store.root / "not-a-digest").mkdir()
    (store.root / "README.txt").write_text("dosya", encoding="utf-8")
    (store.root / "..gizli").mkdir()
    link = store.root / ("b" * 64)
    os.symlink(victim, link, target_is_directory=True)

    store.sweep(now=datetime.now(UTC) + timedelta(days=1))

    assert foreign_dir.is_dir()
    assert (store.root / "not-a-digest").is_dir()
    assert (store.root / "README.txt").is_file()
    assert (store.root / "..gizli").is_dir()
    assert link.is_symlink()
    assert (victim / "onemli.txt").read_text(encoding="utf-8") == "silinmemeli"


def test_cleanup_preserves_a_directory_with_unexpected_content(
    store: PreviewStore,
) -> None:
    """Beklenmeyen içerik sessizce yok edilmez.

    `shutil.rmtree` yerine bilinen dosya adlarını silip `rmdir` denenir; dizin
    boşalmazsa korunur ve fark edilebilir kalır.
    """
    now = datetime.now(UTC)
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT, now=now)
    directory = _published_dir(store, token)
    (directory / "beklenmeyen.bin").write_text("veri", encoding="utf-8")

    removed = store.sweep(now=now + timedelta(seconds=store.ttl_seconds + 1))

    assert removed == 0
    assert (directory / "beklenmeyen.bin").is_file()


def test_discard_raises_when_cleanup_cannot_complete(store: PreviewStore) -> None:
    """Temizlenemeyen state sessizce başarı gösteremez.

    Beklenmeyen bir dosya `rmdir`'i engellediğinde dizin güvenlik gereği
    korunur; ancak işlem başarılı sayılmaz, aksi hâlde diskte kalan claim
    edilmiş state fark edilmezdi.
    """
    token, _ = store.publish(meta=META, snapshot_text=SNAPSHOT)
    record = _claim(store, token)
    (_state_dir(store, record) / "beklenmeyen.bin").write_text("veri", encoding="utf-8")

    with pytest.raises(PreviewStoreUnavailableError) as exc_info:
        store.discard(record)

    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "ping_preview_unavailable"
    assert _state_dir(store, record).is_dir()


def test_sweep_survives_a_missing_root(tmp_path: Path) -> None:
    """Kök henüz yoksa süpürme hata üretmez."""
    store = PreviewStore(
        tmp_path / "yok",
        ttl_seconds=1.0,
        claim_stale_seconds=MINIMUM_CLAIM_STALE_SECONDS,
    )

    assert store.sweep() == 0


# --- Yapılandırma --------------------------------------------------------------


def test_claim_stale_below_the_safe_minimum_is_rejected(tmp_path: Path) -> None:
    """Alt sınırın altındaki değer sessizce yükseltilmez, **reddedilir**.

    Sessiz bir clamp, operatörün ayarladığını sandığı politika ile gerçekte
    uygulanan politikayı ayırır ve bunu fark ettirecek hiçbir iz bırakmaz.
    """
    with pytest.raises(ValueError, match="claim_stale_seconds"):
        PreviewStore(
            tmp_path / "p",
            ttl_seconds=10.0,
            claim_stale_seconds=MINIMUM_CLAIM_STALE_SECONDS - 1,
        )


def test_claim_stale_at_the_safe_minimum_is_accepted(tmp_path: Path) -> None:
    store = PreviewStore(
        tmp_path / "p",
        ttl_seconds=10.0,
        claim_stale_seconds=MINIMUM_CLAIM_STALE_SECONDS,
    )

    assert store.sweep() == 0
