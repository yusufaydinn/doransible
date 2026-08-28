"""Dondurulmuş execution workspace'i (R1-V2) ve yeniden doğrulaması (R1-V3A).

Merkez iddia: **dondurulmuş kopya, kaynağın sonraki hâlinden bağımsızdır.**
Kopya symlink ve özel dosya taşımaz, izinleri daraltılmıştır, sınırları sessizce
kırpmaz ve başarısız bir dondurma diskte kalıntı bırakmaz.

R1-V3A bunun aynadaki karşılığını ekler: dondurulan içerik, tüketilmeden önce
diskteki gerçek baytlardan yeniden özetlenip kayıttaki digest ile karşılaştırılır
ve en küçük fark fail-closed reddedilir.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.execution import workspace as ws
from app.services.execution.workspace import (
    WorkspaceIntegrityError,
    WorkspaceUnavailableError,
    WorkspaceUnsafeError,
    freeze_workspace,
    list_stale_staging,
    list_workspace_ids,
    read_frozen_inventory,
    read_manifest,
    remove_workspace,
    verify_frozen_workspace,
    workspace_exists,
    workspace_inventory_path,
    workspace_project_root,
)

SNAPSHOT = '{\n  "all": {\n    "hosts": {\n      "web01": {}\n    }\n  }\n}\n'

pytestmark = pytest.mark.skipif(
    not ws.secure_filesystem_available(),
    reason="Descriptor-relative dosya sistemi primitive'leri bu platformda yok (ADR-017).",
)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "execution-plans"
    root.mkdir()
    return root


@pytest.fixture
def source_project(tmp_path: Path) -> Path:
    """Küçük ama tipik bir project ağacı."""
    root = tmp_path / "proje"
    (root / "playbooks").mkdir(parents=True)
    (root / "roles" / "web" / "tasks").mkdir(parents=True)
    (root / "site.yml").write_text("---\n- hosts: all\n", encoding="utf-8")
    (root / "playbooks" / "web.yml").write_text("---\n- hosts: web\n", encoding="utf-8")
    (root / "roles" / "web" / "tasks" / "main.yml").write_text("---\n- debug:\n", encoding="utf-8")
    return root


def _entries(root: Path) -> list[str]:
    return sorted(item.name for item in root.iterdir())


def test_freeze_copies_project_and_normalized_inventory(
    workspace_root: Path, source_project: Path
) -> None:
    """Dondurulmuş workspace project ağacını ve snapshot'ı taşır."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )

    project_copy = workspace_project_root(workspace_root, frozen.workspace_id)
    assert (project_copy / "site.yml").read_text(encoding="utf-8") == "---\n- hosts: all\n"
    assert (project_copy / "roles" / "web" / "tasks" / "main.yml").exists()
    assert read_frozen_inventory(workspace_root, frozen.workspace_id) == SNAPSHOT
    # Ham inventory dosyası kopyalanmaz; yalnız normalize snapshot dondurulur.
    assert _entries(workspace_root / frozen.workspace_id) == [
        "inventory",
        "manifest.json",
        "project",
    ]


def test_frozen_permissions_are_narrow(workspace_root: Path, source_project: Path) -> None:
    """Kök ve dizinler 0700, bütün normal dosyalar 0600."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )

    base = workspace_root / frozen.workspace_id
    assert stat.S_IMODE(workspace_root.stat().st_mode) == 0o700
    for path in [base, *base.rglob("*")]:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), path


def test_manifest_is_deterministic_and_content_sensitive(
    workspace_root: Path, source_project: Path
) -> None:
    """Aynı içerik aynı digest'i, tek bayt farkı başka digest'i üretir."""
    first = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    second = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    assert first.manifest_digest == second.manifest_digest
    assert first.workspace_id != second.workspace_id

    (source_project / "site.yml").write_text("---\n- hosts: all\n#\n", encoding="utf-8")
    third = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    assert third.manifest_digest != first.manifest_digest


def test_manifest_lists_relative_paths_and_hashes(
    workspace_root: Path, source_project: Path
) -> None:
    """Manifest göreli yol, tür, normalize mode ve içerik özeti taşır."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )

    manifest = read_manifest(workspace_root, frozen.workspace_id)
    assert manifest["digest"] == frozen.manifest_digest
    paths = {entry["path"]: entry for entry in manifest["entries"]}
    assert "project/site.yml" in paths
    assert paths["project/site.yml"]["type"] == "file"
    assert paths["project/site.yml"]["mode"] == "0600"
    assert len(paths["project/site.yml"]["sha256"]) == 64
    assert paths["project"]["type"] == "dir"
    assert paths["project"]["sha256"] is None
    # Absolute path hiçbir manifest girdisinde bulunmaz.
    assert all(not entry["path"].startswith("/") for entry in manifest["entries"])
    assert str(source_project) not in str(manifest)


def test_frozen_copy_survives_source_mutation(workspace_root: Path, source_project: Path) -> None:
    """Kaynak dondurma sonrasında değişse de kopya ve digest değişmez."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    before = read_manifest(workspace_root, frozen.workspace_id)

    (source_project / "site.yml").write_text("---\n- hosts: hepsi\n", encoding="utf-8")
    (source_project / "yeni.yml").write_text("---\n- hosts: all\n", encoding="utf-8")

    project_copy = workspace_project_root(workspace_root, frozen.workspace_id)
    assert (project_copy / "site.yml").read_text(encoding="utf-8") == "---\n- hosts: all\n"
    assert not (project_copy / "yeni.yml").exists()
    assert read_manifest(workspace_root, frozen.workspace_id) == before


def test_frozen_copy_survives_source_deletion(workspace_root: Path, source_project: Path) -> None:
    """Kaynak tümüyle silinse bile dondurulmuş içerik okunabilir."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )

    for path in sorted(source_project.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    source_project.rmdir()

    project_copy = workspace_project_root(workspace_root, frozen.workspace_id)
    assert (project_copy / "site.yml").exists()
    assert read_frozen_inventory(workspace_root, frozen.workspace_id) == SNAPSHOT


def test_inventory_path_is_derived_from_fixed_names(
    workspace_root: Path, source_project: Path
) -> None:
    """Inventory yolu kök + opaque kimlik + sabit adlardan türetilir."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )

    path = workspace_inventory_path(workspace_root, frozen.workspace_id)

    assert path == workspace_root / frozen.workspace_id / "inventory" / "hosts.yml"
    assert path.parent.parent == workspace_project_root(workspace_root, frozen.workspace_id).parent
    assert path.read_text(encoding="utf-8") == SNAPSHOT


def test_inventory_path_refuses_a_forged_workspace_id(workspace_root: Path) -> None:
    """Uydurulmuş bir workspace adı path işlemine dönüşmez."""
    with pytest.raises(WorkspaceUnavailableError):
        workspace_inventory_path(workspace_root, "../../etc")


def test_inventory_path_refuses_a_symlinked_snapshot(
    workspace_root: Path, source_project: Path, tmp_path: Path
) -> None:
    """Snapshot yerine konmuş bir bağlantı izlenmez; yol hiç dönmez."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    outside = tmp_path / "disaridaki-hosts.yml"
    outside.write_text(SNAPSHOT, encoding="utf-8")
    target = workspace_root / frozen.workspace_id / "inventory" / "hosts.yml"
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(WorkspaceUnavailableError):
        workspace_inventory_path(workspace_root, frozen.workspace_id)


# --- Yeniden doğrulama (R1-V3A) ---------------------------------------------


def test_verification_accepts_untouched_content(workspace_root: Path, source_project: Path) -> None:
    """Dokunulmamış workspace, dondurma anındaki digest'i yeniden üretir."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )

    verify_frozen_workspace(
        workspace_root, frozen.workspace_id, expected_digest=frozen.manifest_digest
    )


def test_verification_never_reopens_the_source_tree(
    workspace_root: Path, source_project: Path
) -> None:
    """Özgün ağaç silinse bile doğrulama geçer: bakılan tek şey kopyadır."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )

    for path in sorted(source_project.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    source_project.rmdir()

    verify_frozen_workspace(
        workspace_root, frozen.workspace_id, expected_digest=frozen.manifest_digest
    )


def _add_private_file(workspace: Path) -> None:
    """Dondurulmuş ağaca 0600 izinli, fazladan bir dosya ekler."""
    added = workspace / "project" / "eklenen.yml"
    added.write_text("---\n", encoding="utf-8")
    added.chmod(0o600)


def _tamper_manifest_entry(workspace: Path) -> None:
    """Yalnız ``manifest.json``'ı bozar; dondurulmuş baytlara dokunmaz."""
    path = workspace / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["entries"][-1]["sha256"] = "0" * 64
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _tamper_manifest_digest(workspace: Path) -> None:
    """Manifest'in kendi digest satırını değiştirir."""
    path = workspace / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["digest"] = "0" * 64
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


TAMPERS: dict[str, tuple[Callable[[Path], object], str]] = {
    "changed_file": (
        lambda workspace: (workspace / "project" / "site.yml").write_text(
            "---\n- hosts: baska\n", encoding="utf-8"
        ),
        "content_digest_mismatch",
    ),
    "changed_inventory": (
        lambda workspace: (workspace / "inventory" / "hosts.yml").write_text(
            SNAPSHOT + "\n", encoding="utf-8"
        ),
        "content_digest_mismatch",
    ),
    # Eklenen dosya, dondurulmuş kopyanın izin biçimine **birebir** uyar: aksi
    # hâlde ret izin kontrolünden gelir ve "fazladan girdi digest'i değiştirir"
    # iddiası ölçülmemiş olurdu.
    "added_file": (_add_private_file, "content_digest_mismatch"),
    "removed_file": (
        lambda workspace: (workspace / "project" / "site.yml").unlink(),
        "content_digest_mismatch",
    ),
    "symlink": (
        lambda workspace: (workspace / "project" / "kisayol.yml").symlink_to(
            workspace / "project" / "site.yml"
        ),
        "symlink",
    ),
    "special_file": (
        lambda workspace: os.mkfifo(workspace / "project" / "boru", 0o600),
        "special_file",
    ),
    "file_mode": (
        lambda workspace: (workspace / "project" / "site.yml").chmod(0o644),
        "mode_mismatch",
    ),
    "directory_mode": (
        lambda workspace: (workspace / "project").chmod(0o755),
        "mode_mismatch",
    ),
    "extra_root_entry": (
        lambda workspace: (workspace / "fazladan").mkdir(mode=0o700),
        "unexpected_layout",
    ),
    "extra_inventory_entry": (
        lambda workspace: (workspace / "inventory" / "fazladan.yml").write_text(
            "---\n", encoding="utf-8"
        ),
        "unexpected_layout",
    ),
    "manifest_entry": (_tamper_manifest_entry, "manifest_mismatch"),
    "manifest_digest": (_tamper_manifest_digest, "manifest_mismatch"),
    "manifest_unreadable": (
        lambda workspace: (workspace / "manifest.json").write_text("{bozuk", encoding="utf-8"),
        "manifest_unreadable",
    ),
}


@pytest.mark.parametrize("tamper", sorted(TAMPERS))
def test_verification_refuses_every_kind_of_change(
    workspace_root: Path, source_project: Path, tamper: str
) -> None:
    """Eksik, fazla, değişmiş, symlink, özel dosya ve izin farkı fail-closed reddedilir.

    Manifest vakaları ayrıca şunu gösterir: digest **diskteki baytlardan**
    yeniden hesaplandığı için, yalnız ``manifest.json``'ı düzenlemek doğrulamayı
    geçmez. Manifest'e körü körüne güvenen bir kontrol, dosyayı değiştirebilen
    birinin digest satırını da değiştirebileceğini görmezden gelirdi.
    """
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    mutate, expected_reason = TAMPERS[tamper]
    mutate(workspace_root / frozen.workspace_id)

    with pytest.raises(WorkspaceIntegrityError) as exc_info:
        verify_frozen_workspace(
            workspace_root, frozen.workspace_id, expected_digest=frozen.manifest_digest
        )

    assert exc_info.value.details == {"reason": expected_reason}
    assert exc_info.value.status_code == 409
    # Hata ne path ne de digest içeriği taşır.
    assert str(workspace_root) not in exc_info.value.message
    assert frozen.manifest_digest not in exc_info.value.message


def test_verification_refuses_a_digest_from_another_plan(
    workspace_root: Path, source_project: Path
) -> None:
    """İçerik sağlam olsa bile beklenen digest tutmuyorsa doğrulama düşer."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )

    with pytest.raises(WorkspaceIntegrityError) as exc_info:
        verify_frozen_workspace(workspace_root, frozen.workspace_id, expected_digest="0" * 64)

    assert exc_info.value.details == {"reason": "content_digest_mismatch"}


def test_verification_of_a_missing_workspace_is_unavailable(
    workspace_root: Path, source_project: Path
) -> None:
    """Kaybolmuş workspace bütünlük ihlali değil, erişilemezliktir."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    assert remove_workspace(workspace_root, frozen.workspace_id) is True

    with pytest.raises(WorkspaceUnavailableError):
        verify_frozen_workspace(
            workspace_root, frozen.workspace_id, expected_digest=frozen.manifest_digest
        )


@pytest.mark.parametrize("target_is_directory", [True, False])
def test_symlink_is_rejected(
    workspace_root: Path, source_project: Path, tmp_path: Path, target_is_directory: bool
) -> None:
    """Ağaçtaki symlink fail-closed reddedilir; hedefi kopyalanmaz."""
    outside = tmp_path / "disarisi"
    if target_is_directory:
        outside.mkdir()
        (outside / "gizli.yml").write_text("---\n- hosts: all\n", encoding="utf-8")
    else:
        outside.write_text("gizli", encoding="utf-8")
    os.symlink(outside, source_project / "kisayol")

    with pytest.raises(WorkspaceUnsafeError) as exc_info:
        freeze_workspace(workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT)

    assert exc_info.value.details == {"reason": "symlink"}
    assert exc_info.value.status_code == 409
    # Hata hiçbir path sızdırmaz.
    assert str(outside) not in exc_info.value.message
    assert _entries(workspace_root) == []


def test_special_file_is_rejected(workspace_root: Path, source_project: Path) -> None:
    """FIFO gibi özel dosyalar kopyalanmaz; okuma denemesi bile yapılmaz."""
    os.mkfifo(source_project / "boru")

    with pytest.raises(WorkspaceUnsafeError) as exc_info:
        freeze_workspace(workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT)

    assert exc_info.value.details == {"reason": "special_file"}
    assert _entries(workspace_root) == []


def test_symlinked_project_root_is_rejected(
    workspace_root: Path, source_project: Path, tmp_path: Path
) -> None:
    """Kaynak kökün kendisi symlink ise kopyalama hiç başlamaz."""
    link = tmp_path / "proje-link"
    os.symlink(source_project, link, target_is_directory=True)

    with pytest.raises(WorkspaceUnsafeError) as exc_info:
        freeze_workspace(workspace_root, project_root=link, inventory_snapshot=SNAPSHOT)

    assert exc_info.value.details == {"reason": "unreadable_root"}


def test_entry_limit_fails_closed(
    workspace_root: Path, source_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Girdi sınırı aşılırsa kopya reddedilir; eksik ağaç yayımlanmaz."""
    monkeypatch.setattr(ws, "MAX_WORKSPACE_ENTRIES", 2)

    with pytest.raises(WorkspaceUnsafeError) as exc_info:
        freeze_workspace(workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT)

    assert exc_info.value.details == {"reason": "too_many_entries", "limit": 2}
    assert _entries(workspace_root) == []


def test_byte_limit_fails_closed(
    workspace_root: Path, source_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bayt sınırı okuma sırasında uygulanır ve sessiz truncation yapılmaz."""
    (source_project / "buyuk.yml").write_text("x" * 4096, encoding="utf-8")
    monkeypatch.setattr(ws, "MAX_WORKSPACE_BYTES", 1024)

    with pytest.raises(WorkspaceUnsafeError) as exc_info:
        freeze_workspace(workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT)

    assert exc_info.value.details == {"reason": "too_large", "limit": 1024}
    assert _entries(workspace_root) == []


def test_depth_limit_fails_closed(
    workspace_root: Path, source_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aşırı derin ağaç reddedilir."""
    monkeypatch.setattr(ws, "MAX_WORKSPACE_DEPTH", 1)

    with pytest.raises(WorkspaceUnsafeError) as exc_info:
        freeze_workspace(workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT)

    assert exc_info.value.details == {"reason": "too_deep", "limit": 1}
    assert _entries(workspace_root) == []


def test_failed_freeze_leaves_no_staging_residue(
    workspace_root: Path, source_project: Path
) -> None:
    """Başarısız dondurma ne staging ne de yarım workspace bırakır."""
    os.mkfifo(source_project / "boru")

    with pytest.raises(WorkspaceUnsafeError):
        freeze_workspace(workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT)

    assert list(workspace_root.iterdir()) == []
    assert list_workspace_ids(workspace_root) == []


def test_identity_check_detects_swapped_entry(tmp_path: Path) -> None:
    """Açık descriptor ile isimdeki girdi farklıysa takas yakalanır.

    Kopyalama sırasındaki dizin/dosya takasına karşı koruma budur: ``O_NOFOLLOW``
    açma anında symlink'i reddeder, kimlik karşılaştırması ise açmadan **sonra**
    yapılan değiş-tokuşu yakalar.
    """
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    child_fd = os.open("a", os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
    try:
        # İsim aynı kalır ama arkasındaki nesne değişir.
        os.rename(second, first)
        with pytest.raises(OSError):
            ws._assert_same_entry(child_fd, parent_fd, "a")
    finally:
        os.close(child_fd)
        os.close(parent_fd)


def test_remove_workspace_does_not_follow_symlinks(workspace_root: Path, tmp_path: Path) -> None:
    """Kök altına konmuş bağlantı silinir, gösterdiği dış hedef korunur."""
    outside = tmp_path / "disarisi"
    outside.mkdir()
    (outside / "onemli.txt").write_text("veri", encoding="utf-8")
    fake_id = "11111111-1111-4111-8111-111111111111"
    os.symlink(outside, workspace_root / fake_id, target_is_directory=True)

    assert workspace_exists(workspace_root, fake_id) is False
    assert remove_workspace(workspace_root, fake_id) is True
    assert (outside / "onemli.txt").exists()


def test_remove_workspace_refuses_unknown_names(workspace_root: Path) -> None:
    """Adı uygulamanın ürettiği biçimlere uymayan girdiye dokunulmaz."""
    (workspace_root / "elle-konmus").mkdir()

    assert remove_workspace(workspace_root, "elle-konmus") is False
    assert remove_workspace(workspace_root, "../disari") is False
    assert (workspace_root / "elle-konmus").exists()


def test_remove_workspace_deletes_published_content(
    workspace_root: Path, source_project: Path
) -> None:
    """Yayımlanmış workspace içeriğiyle birlikte silinir."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )

    assert remove_workspace(workspace_root, frozen.workspace_id) is True
    assert workspace_exists(workspace_root, frozen.workspace_id) is False
    assert list(workspace_root.iterdir()) == []


def test_stale_staging_needs_an_age_threshold(workspace_root: Path) -> None:
    """Taze staging korunur, yaşlanmış olan toplanır."""
    staging = workspace_root / f"{ws.STAGING_PREFIX}{'a' * 32}"
    staging.mkdir()
    now = datetime.now(UTC)

    assert list_stale_staging(workspace_root, now=now, stale_seconds=900) == []
    later = now + timedelta(seconds=1800)
    assert list_stale_staging(workspace_root, now=later, stale_seconds=900) == [staging.name]


def test_invalid_workspace_id_is_refused(workspace_root: Path) -> None:
    """Uydurulmuş bir workspace adı path işlemine dönüşmez."""
    with pytest.raises(WorkspaceUnavailableError):
        workspace_project_root(workspace_root, "../../etc")
    assert workspace_exists(workspace_root, "../../etc") is False


def test_maintenance_cursor_round_trips(workspace_root: Path) -> None:
    """Yazılan imleç okunur; yokken okuma ``None`` döner."""
    assert ws.read_maintenance_cursor(workspace_root) is None

    workspace_id = "1b4e28ba-2fa1-4d3b-a3f5-ccee0d6c1f9e"
    assert ws.write_maintenance_cursor(workspace_root, workspace_id) is True
    assert ws.read_maintenance_cursor(workspace_root) == workspace_id

    # Liste bitince imleç sıfırlanır: bir sonraki tur baştan başlar.
    assert ws.write_maintenance_cursor(workspace_root, None) is True
    assert ws.read_maintenance_cursor(workspace_root) is None


def test_maintenance_cursor_permissions_are_narrow(workspace_root: Path) -> None:
    """İmleç de 0600 yazılır ve listelemelerde workspace sayılmaz."""
    assert ws.write_maintenance_cursor(workspace_root, "1b4e28ba-2fa1-4d3b-a3f5-ccee0d6c1f9e")
    cursor = workspace_root / ws.MAINTENANCE_CURSOR_FILENAME

    assert stat.S_IMODE(cursor.stat().st_mode) == ws.FILE_MODE
    assert ws.list_workspace_ids(workspace_root) == []
    # Adı bilinen biçimlerden hiçbirine uymadığı için silme yoluna da giremez.
    assert remove_workspace(workspace_root, ws.MAINTENANCE_CURSOR_FILENAME) is False
    assert cursor.exists()


@pytest.mark.parametrize(
    "content",
    [
        "{bu json değil",
        '{"version": 1, "after": "../../etc"}',
        '{"version": 99, "after": "1b4e28ba-2fa1-4d3b-a3f5-ccee0d6c1f9e"}',
        '["liste"]',
        "",
    ],
)
def test_corrupt_cursor_reads_as_none(workspace_root: Path, content: str) -> None:
    """Bozuk imleç fail-closed yok sayılır; asla path işlemine dönüşmez."""
    (workspace_root / ws.MAINTENANCE_CURSOR_FILENAME).write_text(content, encoding="utf-8")
    assert ws.read_maintenance_cursor(workspace_root) is None


def test_oversized_cursor_reads_as_none(workspace_root: Path) -> None:
    """Şişirilmiş imleç okunmaz: bakım işi sınırsız bellek harcamaz."""
    payload = "a" * (ws.MAX_CURSOR_BYTES + 1)
    (workspace_root / ws.MAINTENANCE_CURSOR_FILENAME).write_text(payload, encoding="utf-8")
    assert ws.read_maintenance_cursor(workspace_root) is None


def test_symlinked_cursor_is_never_followed(workspace_root: Path, tmp_path: Path) -> None:
    """İmleç yerine konmuş symlink ne okunur ne de üzerinden yazılır."""
    outside = tmp_path / "kok-disi.txt"
    outside.write_text('{"version": 1, "after": "1b4e28ba-2fa1-4d3b-a3f5-ccee0d6c1f9e"}\n')
    (workspace_root / ws.MAINTENANCE_CURSOR_FILENAME).symlink_to(outside)

    # Okuma symlink'i izlemez: dışarıdaki geçerli içerik imleç sayılmaz.
    assert ws.read_maintenance_cursor(workspace_root) is None

    # Yazma da izlemez: `rename` girdinin kendisini değiştirir, hedefe dokunmaz.
    other = "2c5f39cb-3fb2-4e4c-b4f6-ddff1e7d2a0f"
    assert ws.write_maintenance_cursor(workspace_root, other) is True
    assert ws.read_maintenance_cursor(workspace_root) == other

    assert (workspace_root / ws.MAINTENANCE_CURSOR_FILENAME).is_symlink() is False
    assert outside.exists()
    assert "1b4e28ba" in outside.read_text(encoding="utf-8")


def test_cursor_write_refuses_a_forged_name(workspace_root: Path) -> None:
    """Uydurulmuş bir imleç değeri yazılmaz."""
    assert ws.write_maintenance_cursor(workspace_root, "../../etc") is False
    assert (workspace_root / ws.MAINTENANCE_CURSOR_FILENAME).exists() is False


def test_cursor_is_not_written_before_the_root_exists(tmp_path: Path) -> None:
    """Kök yoksa imleç yazma sessizce başarısız olur; dizin ağacı üretmez."""
    missing = tmp_path / "yok"
    assert ws.write_maintenance_cursor(missing, "1b4e28ba-2fa1-4d3b-a3f5-ccee0d6c1f9e") is False
    assert ws.read_maintenance_cursor(missing) is None
    assert missing.exists() is False
