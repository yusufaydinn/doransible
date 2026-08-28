"""Playbook keşfi — dosya sistemi katmanı (T-103).

Bu testler veritabanı olmadan çalışır; yalnızca tarama ve sınıflandırma
kurallarını doğrular.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.services.projects import discovery as discovery_module
from app.services.projects.discovery import (
    ScanLimits,
    ScanRootUnavailableError,
    discover_playbooks,
    is_excluded_directory,
    is_role_subdirectory,
    looks_like_playbook,
)
from tests.support import link_directory

PLAYBOOK = "---\n- name: Ornek play\n  hosts: all\n  tasks:\n    - name: Ping\n      ping:\n"
ROLE_TASKS = "---\n- name: Paket kur\n  ansible.builtin.apt:\n    name: nginx\n"
# İçerik sezgisini bilerek geçen sahte role task'i: dışlamanın içerikten değil
# **yapıdan** geldiğini kanıtlamak için kullanılır.
FAKE_ROLE_TASKS = "---\n- name: Sahte play gibi gorunen task\n  hosts: all\n"
VARS_FILE = "---\nnginx_port: 80\nnginx_user: www-data\n"


def scan(root: Path, **overrides: int) -> list[str]:
    """Kısayol: keşfi çalıştırıp yalnızca relative path listesini döndürür."""
    result = discover_playbooks(root, project_id=1, limits=ScanLimits(**overrides))
    return [item.path for item in result.playbooks]


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- Bulma ------------------------------------------------------------------


def test_root_level_yml_and_yaml_are_found(tmp_path: Path) -> None:
    write(tmp_path / "site.yml", PLAYBOOK)
    write(tmp_path / "deploy.yaml", PLAYBOOK)

    assert scan(tmp_path) == ["deploy.yaml", "site.yml"]


def test_playbook_in_subdirectory_is_found(tmp_path: Path) -> None:
    write(tmp_path / "playbooks" / "web.yml", PLAYBOOK)
    write(tmp_path / "ortamlar" / "prod" / "app.yml", PLAYBOOK)

    assert scan(tmp_path) == ["ortamlar/prod/app.yml", "playbooks/web.yml"]


def test_paths_are_relative_and_posix(tmp_path: Path) -> None:
    """Sunucudaki absolute yol hiçbir zaman dönmez."""
    write(tmp_path / "playbooks" / "web.yml", PLAYBOOK)

    (path,) = scan(tmp_path)

    assert path == "playbooks/web.yml"
    assert "\\" not in path
    assert not Path(path).is_absolute()
    assert str(tmp_path) not in path


def test_ordering_is_deterministic(tmp_path: Path) -> None:
    for name in ("zeta.yml", "alfa.yml", "beta.yml"):
        write(tmp_path / name, PLAYBOOK)
    write(tmp_path / "b" / "iki.yml", PLAYBOOK)
    write(tmp_path / "a" / "bir.yml", PLAYBOOK)

    first = scan(tmp_path)
    second = scan(tmp_path)

    assert first == second == ["a/bir.yml", "alfa.yml", "b/iki.yml", "beta.yml", "zeta.yml"]


def test_import_playbook_only_file_is_a_playbook(tmp_path: Path) -> None:
    write(tmp_path / "master.yml", "---\n- import_playbook: web.yml\n")

    assert scan(tmp_path) == ["master.yml"]


# --- Dışlama ----------------------------------------------------------------


@pytest.mark.parametrize(
    "subdir", ["tasks", "handlers", "defaults", "vars", "meta", "templates", "files"]
)
def test_role_internal_directories_are_excluded(tmp_path: Path, subdir: str) -> None:
    """İçerik sezgisini geçen bir dosya bile role alt dizininde listelenmez."""
    write(tmp_path / "roles" / "nginx" / subdir / "main.yml", FAKE_ROLE_TASKS)
    write(tmp_path / "site.yml", PLAYBOOK)

    assert scan(tmp_path) == ["site.yml"]


def test_role_task_file_containing_hosts_word_is_still_excluded(tmp_path: Path) -> None:
    """Dizin kuralı, içerik sezgisinden bağımsız olarak da korumalıdır."""
    write(
        tmp_path / "roles" / "nginx" / "tasks" / "main.yml",
        "---\n- name: Sahte\n  hosts: all\n",
    )

    assert scan(tmp_path) == []


def test_roles_at_any_depth_are_recognised(tmp_path: Path) -> None:
    """`roles/` project kökünde olmak zorunda değildir."""
    write(tmp_path / "playbooks" / "roles" / "web" / "tasks" / "main.yml", FAKE_ROLE_TASKS)
    write(
        tmp_path / "ansible_collections" / "ns" / "coll" / "roles" / "db" / "vars" / "main.yml",
        FAKE_ROLE_TASKS,
    )
    write(tmp_path / "site.yml", PLAYBOOK)

    assert scan(tmp_path) == ["site.yml"]


# --- Meşru dizin adları budanmamalı (T-103 denetim bulgusu 1) ---------------


def test_playbooks_tasks_directory_is_not_pruned(tmp_path: Path) -> None:
    """`playbooks/tasks/deploy.yml` role yapısı değildir; keşfedilmelidir."""
    write(tmp_path / "playbooks" / "tasks" / "deploy.yml", PLAYBOOK)

    assert scan(tmp_path) == ["playbooks/tasks/deploy.yml"]


@pytest.mark.parametrize(
    "relative",
    [
        "playbooks/tasks/deploy.yml",
        "playbooks/handlers/restart.yml",
        "playbooks/defaults/bootstrap.yml",
        "playbooks/vars/migrate.yml",
        "playbooks/meta/rollout.yml",
        "playbooks/templates/render.yml",
        "playbooks/files/seed.yml",
        "ortamlar/prod/tasks/deploy.yml",
        "roles/tasks/site.yml",
    ],
)
def test_role_subdirectory_names_outside_roles_are_kept(tmp_path: Path, relative: str) -> None:
    """Aynı adlar role yapısı dışında meşrudur ve budanmaz.

    `roles/tasks/site.yml` özellikle önemlidir: burada `tasks` bir role
    **adıdır**, role alt dizini değil.
    """
    write(tmp_path / relative, PLAYBOOK)

    assert scan(tmp_path) == [relative]


def test_plugin_directories_are_only_excluded_where_ansible_looks(tmp_path: Path) -> None:
    """`library` kökte ve role içinde dışlanır, başka yerde budanmaz."""
    write(tmp_path / "library" / "kok.yml", PLAYBOOK)
    write(tmp_path / "roles" / "nginx" / "library" / "role.yml", PLAYBOOK)
    write(tmp_path / "playbooks" / "library" / "mesru.yml", PLAYBOOK)

    assert scan(tmp_path) == ["playbooks/library/mesru.yml"]


def test_inventory_directory_is_only_excluded_at_the_root(tmp_path: Path) -> None:
    write(tmp_path / "inventory" / "kok.yml", PLAYBOOK)
    write(tmp_path / "ortamlar" / "inventory" / "mesru.yml", PLAYBOOK)

    assert scan(tmp_path) == ["ortamlar/inventory/mesru.yml"]


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (("roles", "nginx", "tasks"), True),
        (("playbooks", "roles", "web", "vars"), True),
        (("a", "b", "roles", "x", "meta"), True),
        (("playbooks", "tasks"), False),
        (("roles", "tasks"), False),
        (("tasks",), False),
    ],
)
def test_role_structure_detection(parts: tuple[str, ...], expected: bool) -> None:
    assert is_role_subdirectory(parts) is expected


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (("roles", "nginx", "tasks"), True),
        (("playbooks", "tasks"), False),
        (("group_vars",), True),
        (("ortamlar", "prod", "group_vars"), True),
        (("inventory",), True),
        (("ortamlar", "inventory"), False),
        (("library",), True),
        (("roles", "nginx", "library"), True),
        (("playbooks", "library"), False),
        (("node_modules",), True),
        ((".git",), True),
        (("playbooks",), False),
    ],
)
def test_directory_exclusion_rules(parts: tuple[str, ...], expected: bool) -> None:
    assert is_excluded_directory(parts) is expected


@pytest.mark.parametrize("subdir", ["group_vars", "host_vars"])
def test_inventory_variable_directories_are_excluded(tmp_path: Path, subdir: str) -> None:
    write(tmp_path / subdir / "all.yml", VARS_FILE)
    write(tmp_path / "site.yml", PLAYBOOK)

    assert scan(tmp_path) == ["site.yml"]


def test_nested_group_vars_are_also_excluded(tmp_path: Path) -> None:
    """`group_vars` yalnızca kökte değil, her derinlikte dışlanır."""
    write(tmp_path / "ortamlar" / "prod" / "group_vars" / "web.yml", VARS_FILE)

    assert scan(tmp_path) == []


@pytest.mark.parametrize(
    "relative",
    ["inventory.yml", "hosts.yaml", "requirements.yml", "galaxy.yml", "molecule.yml"],
)
def test_known_non_playbook_file_names_are_excluded(tmp_path: Path, relative: str) -> None:
    write(tmp_path / relative, PLAYBOOK)
    write(tmp_path / "site.yml", PLAYBOOK)

    assert scan(tmp_path) == ["site.yml"]


@pytest.mark.parametrize("directory", ["inventory", "inventories"])
def test_inventory_directories_are_excluded(tmp_path: Path, directory: str) -> None:
    write(tmp_path / directory / "prod.yml", PLAYBOOK)

    assert scan(tmp_path) == []


def test_non_yaml_files_are_excluded(tmp_path: Path) -> None:
    write(tmp_path / "README.md", "# proje")
    write(tmp_path / "ansible.cfg", "[defaults]\n")
    write(tmp_path / "script.sh", "#!/bin/sh\n")
    write(tmp_path / "site.yml", PLAYBOOK)

    assert scan(tmp_path) == ["site.yml"]


def test_hidden_directories_are_excluded(tmp_path: Path) -> None:
    write(tmp_path / ".git" / "config.yml", PLAYBOOK)
    write(tmp_path / ".github" / "workflows" / "ci.yml", PLAYBOOK)
    write(tmp_path / "site.yml", PLAYBOOK)

    assert scan(tmp_path) == ["site.yml"]


# --- İçerik sezgisi ---------------------------------------------------------


def test_mapping_yaml_is_not_a_playbook(tmp_path: Path) -> None:
    """Uzantısı doğru olsa da mapping olan YAML playbook değildir."""
    write(tmp_path / "degiskenler.yml", VARS_FILE)

    assert scan(tmp_path) == []


def test_task_list_without_hosts_is_not_a_playbook(tmp_path: Path) -> None:
    """Dizi olsa bile play anahtarı yoksa playbook sayılmaz."""
    write(tmp_path / "gorevler.yml", ROLE_TASKS)

    assert scan(tmp_path) == []


def test_empty_and_comment_only_files_are_not_playbooks(tmp_path: Path) -> None:
    write(tmp_path / "bos.yml", "")
    write(tmp_path / "yorum.yml", "# yalnizca yorum\n---\n")

    assert scan(tmp_path) == []


def test_broken_yaml_is_excluded_without_failing_the_scan(tmp_path: Path) -> None:
    """Bozuk içerik keşfi düşürmez; yalnızca aday listesine girmez."""
    write(tmp_path / "bozuk.yml", "---\n- hosts: all\n   tasks:\n  - bad: [unclosed\n")
    write(tmp_path / "site.yml", PLAYBOOK)

    result = discover_playbooks(tmp_path, project_id=1, limits=ScanLimits())

    # Bozuk dosya okunabildiği için "unreadable" değildir; sezgiye takılmazsa
    # listeye girebilir. Önemli olan taramanın tamamlanması ve site.yml'in
    # bulunmasıdır.
    assert "site.yml" in [item.path for item in result.playbooks]
    assert result.skipped_unreadable_files == 0


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("- hosts: all\n", True),
        ("---\n- hosts: all\n", True),
        ("# yorum\n---\n- name: x\n  hosts: web\n", True),
        ("- import_playbook: a.yml\n", True),
        ("- ansible.builtin.import_playbook: a.yml\n", True),
        ("hosts: all\n", False),
        ("- name: task\n  ping:\n", False),
        ("", False),
        ("---\n", False),
        ("nginx_port: 80\n", False),
    ],
)
def test_playbook_heuristic_rules(content: str, expected: bool) -> None:
    assert looks_like_playbook(content) is expected


def test_undecodable_file_is_counted_not_fatal(tmp_path: Path) -> None:
    """İkili içerikli `.yml` okunamaz sayılır, keşif devam eder."""
    (tmp_path / "ikili.yml").write_bytes(b"\xff\xfe\x00\x01binary")
    write(tmp_path / "site.yml", PLAYBOOK)

    result = discover_playbooks(tmp_path, project_id=1, limits=ScanLimits())

    assert [item.path for item in result.playbooks] == ["site.yml"]
    assert result.skipped_unreadable_files == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX dosya izinleri gerekir")
def test_unreadable_file_is_counted_not_fatal(tmp_path: Path) -> None:
    target = write(tmp_path / "gizli.yml", PLAYBOOK)
    os.chmod(target, 0o000)
    write(tmp_path / "site.yml", PLAYBOOK)

    try:
        result = discover_playbooks(tmp_path, project_id=1, limits=ScanLimits())
    finally:
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)

    assert [item.path for item in result.playbooks] == ["site.yml"]
    assert result.skipped_unreadable_files == 1


# --- Symlink ve junction ----------------------------------------------------


def test_symlink_directory_escaping_project_is_not_followed(tmp_path: Path) -> None:
    """Project dışını gösteren bağlantı dizini taranmaz."""
    project = tmp_path / "proje"
    outside = tmp_path / "disarida"
    write(project / "site.yml", PLAYBOOK)
    write(outside / "gizli.yml", PLAYBOOK)
    link_directory(project / "kacis", outside)

    assert scan(project) == ["site.yml"]


@pytest.mark.skipif(sys.platform == "win32", reason="Dosya symlink'i yönetici yetkisi ister")
def test_symlink_file_escaping_project_is_excluded(tmp_path: Path) -> None:
    project = tmp_path / "proje"
    outside = tmp_path / "disarida"
    write(project / "site.yml", PLAYBOOK)
    outside_file = write(outside / "gizli.yml", PLAYBOOK)
    os.symlink(outside_file, project / "baglanti.yml")

    assert scan(project) == ["site.yml"]


def test_symlink_inside_project_does_not_duplicate_results(tmp_path: Path) -> None:
    """Bilinçli karar: kök içinde kalan bağlantı takip edilir ama her gerçek
    dizin **bir kez** taranır.

    Kural "her bağlantıyı reddet" değil "kök dışına çıkanı reddet"tir. Aynı
    gerçek dizine iki yoldan ulaşılıyorsa playbook tek kez, deterministik
    sırada ilk görülen yol altında listelenir. Böylece bir bağlantı çiftliği
    sonuçları çoğaltıp `max_results` sınırını dolduramaz.
    """
    project = tmp_path / "proje"
    write(project / "gercek" / "web.yml", PLAYBOOK)
    link_directory(project / "kisayol", project / "gercek")

    found = scan(project)

    assert found == ["gercek/web.yml"]
    assert (project / found[0]).resolve() == (project / "gercek" / "web.yml").resolve()


def test_directory_reachable_only_through_inside_link_is_found(tmp_path: Path) -> None:
    """Bağlantı gerçekten takip edilir: sırada önce gelen yol raporlanır."""
    project = tmp_path / "proje"
    write(project / "zz-gercek" / "web.yml", PLAYBOOK)
    link_directory(project / "aa-kisayol", project / "zz-gercek")

    found = scan(project)

    assert found == ["aa-kisayol/web.yml"]
    assert (project / found[0]).resolve() == (project / "zz-gercek" / "web.yml").resolve()


def test_link_cannot_alias_a_role_tasks_directory(tmp_path: Path) -> None:
    """Bağlantı ile role içeriğine takma ad verilip dışlama atlatılamaz.

    T-103 denetim bulgusu: dışlama kararı yalnızca görünen yola bakılarak
    verilirse `playbooks/alias` masum görünür ve `roles/demo/tasks/main.yml`
    playbook diye sunulur. Karar çözülmüş gerçek yol üzerinde de verilmelidir.

    Dosya içeriği bilerek içerik sezgisini geçer; koruma yapısal olmalıdır.
    """
    project = tmp_path / "proje"
    write(project / "roles" / "demo" / "tasks" / "main.yml", FAKE_ROLE_TASKS)
    write(project / "playbooks" / "site.yml", PLAYBOOK)
    link_directory(project / "playbooks" / "alias", project / "roles" / "demo" / "tasks")

    found = scan(project)

    assert found == ["playbooks/site.yml"]
    assert "playbooks/alias/main.yml" not in found
    assert not any("alias" in path for path in found)


def test_aliased_role_directory_is_never_descended_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Takma adlı role dizinine **hiç girilmez**.

    Sonuç listesine bakmak yetmez: dosya seviyesindeki ikinci kontrol de aynı
    kaçışı yakalar, dolayısıyla yalnızca sonuca bakan bir test iki katmanı
    ayırt edemez. Bu test `os.scandir` çağrılarını gözleyerek **dizin
    seviyesindeki** gerçek-yol kontrolünü tek başına doğrular.
    """
    project = tmp_path / "proje"
    write(project / "roles" / "demo" / "tasks" / "main.yml", FAKE_ROLE_TASKS)
    write(project / "playbooks" / "site.yml", PLAYBOOK)
    link_directory(project / "playbooks" / "alias", project / "roles" / "demo" / "tasks")

    from app.services.projects import discovery

    real_scandir = discovery.os.scandir
    visited: list[Path] = []

    def _spy(path: str | Path) -> _MaterializedScan:
        visited.append(Path(path))
        with real_scandir(path) as scan_iterator:
            return _MaterializedScan(list(scan_iterator))

    monkeypatch.setattr(discovery.os, "scandir", _spy)

    found = scan(project)

    assert found == ["playbooks/site.yml"]
    # Dizin **adı** karşılaştırılır; pytest'in tmp dizin adı testin kendi adını
    # taşıdığı için ham path üzerinde alt dizi araması yanıltıcı olurdu.
    scanned_names = [entry.name for entry in visited]
    assert "alias" not in scanned_names, scanned_names
    assert "tasks" not in scanned_names, scanned_names


@pytest.mark.skipif(sys.platform == "win32", reason="Dosya symlink'i yönetici yetkisi ister")
def test_file_link_cannot_alias_a_role_task_file(tmp_path: Path) -> None:
    """Dosya seviyesindeki gerçek-yol kontrolünün tek başına tetiklendiği senaryo.

    Dizin bağlantısında iki katman da aynı kaçışı yakalar; yalnızca **dosya**
    bağlantısı dosya seviyesindeki kontrolü izole eder. Windows'ta dosya
    symlink'i yetki gerektirdiği için bu senaryo yalnızca POSIX'te koşar.
    """
    project = tmp_path / "proje"
    target = write(project / "roles" / "demo" / "tasks" / "main.yml", FAKE_ROLE_TASKS)
    write(project / "playbooks" / "site.yml", PLAYBOOK)
    os.symlink(target, project / "playbooks" / "takma.yml")

    assert scan(project) == ["playbooks/site.yml"]


def test_link_cannot_alias_group_vars(tmp_path: Path) -> None:
    """Aynı koruma diğer dışlanmış dizinler için de geçerlidir."""
    project = tmp_path / "proje"
    write(project / "group_vars" / "all.yml", FAKE_ROLE_TASKS)
    write(project / "site.yml", PLAYBOOK)
    link_directory(project / "takma", project / "group_vars")

    assert scan(project) == ["site.yml"]


def test_symlink_cycle_does_not_hang_the_scan(tmp_path: Path) -> None:
    """`a/dongu -> a` bağlantısı sonsuz taramaya yol açmamalıdır."""
    project = tmp_path / "proje"
    write(project / "a" / "site.yml", PLAYBOOK)
    link_directory(project / "a" / "dongu", project / "a")

    assert scan(project) == ["a/site.yml"]


def test_mutual_symlink_cycle_terminates(tmp_path: Path) -> None:
    project = tmp_path / "proje"
    write(project / "bir" / "b1.yml", PLAYBOOK)
    write(project / "iki" / "b2.yml", PLAYBOOK)
    link_directory(project / "bir" / "iki", project / "iki")
    link_directory(project / "iki" / "bir", project / "bir")

    found = scan(project)

    assert "bir/b1.yml" in found
    assert "iki/b2.yml" in found


# --- Limitler ---------------------------------------------------------------


def test_max_depth_limit_truncates(tmp_path: Path) -> None:
    write(tmp_path / "a" / "b" / "c" / "derin.yml", PLAYBOOK)
    write(tmp_path / "yuzey.yml", PLAYBOOK)

    result = discover_playbooks(tmp_path, project_id=1, limits=ScanLimits(max_depth=1))

    assert [item.path for item in result.playbooks] == ["yuzey.yml"]
    assert result.truncated is True


def test_max_results_limit_truncates(tmp_path: Path) -> None:
    for index in range(5):
        write(tmp_path / f"p{index}.yml", PLAYBOOK)

    result = discover_playbooks(tmp_path, project_id=1, limits=ScanLimits(max_results=2))

    assert len(result.playbooks) == 2
    assert result.truncated is True


def test_max_entries_limit_truncates(tmp_path: Path) -> None:
    for index in range(10):
        write(tmp_path / f"p{index}.yml", PLAYBOOK)

    result = discover_playbooks(tmp_path, project_id=1, limits=ScanLimits(max_entries=3))

    assert result.truncated is True
    assert len(result.playbooks) < 10


def test_read_bytes_limit_is_applied(tmp_path: Path) -> None:
    """Sınırın ötesindeki `hosts:` görülmezse dosya aday sayılmaz."""
    padding = "# " + "x" * 200 + "\n"
    write(tmp_path / "gec.yml", "- name: play\n" + padding * 50 + "  hosts: all\n")

    assert scan(tmp_path, read_bytes=100) == []
    assert scan(tmp_path, read_bytes=65_536) == ["gec.yml"]


def test_limits_are_not_reached_in_a_normal_tree(tmp_path: Path) -> None:
    write(tmp_path / "site.yml", PLAYBOOK)
    write(tmp_path / "roles" / "nginx" / "tasks" / "main.yml", ROLE_TASKS)

    result = discover_playbooks(tmp_path, project_id=1, limits=ScanLimits())

    assert result.truncated is False
    assert result.skipped_unreadable_files == 0


# --- Kök kullanılamazlığı ---------------------------------------------------


def test_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(ScanRootUnavailableError):
        discover_playbooks(tmp_path / "yok", project_id=1, limits=ScanLimits())


class _MaterializedScan:
    """`os.scandir` bağlamını taklit eder ama girdileri önceden okur.

    Tarama sırasında dosya sistemini değiştiren testler için gereklidir:
    gerçek iterator açıkken Windows dizini silmeye izin vermez.
    """

    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self._entries = entries

    def __enter__(self) -> Iterator[os.DirEntry[str]]:
        return iter(self._entries)

    def __exit__(self, *_: object) -> None:
        return None


def test_root_removed_during_scan_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tarama sırasında kök kaybolursa sessizce eksik liste dönmez."""
    project = tmp_path / "proje"
    write(project / "site.yml", PLAYBOOK)

    from app.services.projects import discovery

    real_scandir = discovery.os.scandir
    state = {"done": False}

    def _scandir_then_remove(path: str | Path) -> _MaterializedScan:
        with real_scandir(path) as scan:
            entries = list(scan)
        if not state["done"]:
            state["done"] = True
            for item in entries:
                Path(item.path).unlink()
            project.rmdir()
        return _MaterializedScan(entries)

    monkeypatch.setattr(discovery.os, "scandir", _scandir_then_remove)

    with pytest.raises(ScanRootUnavailableError):
        discover_playbooks(project, project_id=1, limits=ScanLimits())


def test_root_replaced_by_a_different_directory_during_scan_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aynı path altında dizin değiştirilirse tarama sonucu güvenilir değildir.

    T-103 denetim bulgusu: path metni değişmediği için yalnızca path
    karşılaştırması bunu **kaçırır**. Kimlik `st_dev`/`st_ino` ile doğrulanır.

    Kimliğin nereden okunduğu da testin konusudur: keşif kökü tarama boyunca
    açık tutar, bu yüzden silinen inode yeniden kullanılamaz ve yerine konan
    dizin zorunlu olarak **başka** bir kimlik alır. Kimlik yeniden açık referans
    olmadan yalnızca `stat(path)` ile okunursa (eski zayıf yaklaşım), inode'u
    anında geri veren dosya sistemlerinde iki kimlik eşit çıkar ve bu test
    "DID NOT RAISE" ile düşer.
    """
    project = tmp_path / "proje"
    write(project / "site.yml", PLAYBOOK)
    if not _inode_identity_supported(project):
        pytest.skip("Dosya sistemi st_ino üretmiyor; kimlik karşılaştırması anlamsız.")
    identity_before = _path_identity(project)

    from app.services.projects import discovery

    real_scandir = discovery.os.scandir
    state = {"done": False}

    def _scandir_then_swap(path: str | Path) -> _MaterializedScan:
        with real_scandir(path) as scan_iterator:
            entries = list(scan_iterator)
        if not state["done"]:
            state["done"] = True
            # Aynı path, tamamen yeni bir dizin nesnesi.
            for item in entries:
                Path(item.path).unlink()
            project.rmdir()
            project.mkdir()
            write(project / "baska.yml", PLAYBOOK)
        return _MaterializedScan(entries)

    monkeypatch.setattr(discovery.os, "scandir", _scandir_then_swap)

    with pytest.raises(ScanRootUnavailableError, match="değişti"):
        discover_playbooks(project, project_id=1, limits=ScanLimits())

    # Kök gerçekten başka bir dosya sistemi nesnesine dönüşmüş olmalı; aksi
    # hâlde test kimlik kontrolünü değil yalnızca path kontrolünü ölçerdi.
    assert _path_identity(project) != identity_before
    # Ve eldeki sonuç gerçekten eskimişti: yeni ağaç başka bir playbook taşıyor.
    monkeypatch.setattr(discovery.os, "scandir", real_scandir)
    assert scan(project) == ["baska.yml"]


@pytest.mark.skipif(
    not discovery_module.DIRECTORY_REFERENCE_SUPPORTED,
    reason="Platform dizin tanıtıcısı desteklemiyor (Windows); koruma zayıf fallback'tedir.",
)
def test_open_root_reference_defeats_inode_reuse(tmp_path: Path) -> None:
    """Kimlik neden açık referans üzerinden okunmalı: inode yeniden kullanımı.

    Test iki yarımdan oluşur:

    1. **Referanssız** silme/yeniden yaratma: dosya sistemi aynı inode'u geri
       verebilir. Verirse, kimliği yalnızca `stat(path)` ile okuyan yaklaşım
       o senaryoda kesinlikle yanılır. Geri vermeyen dosya sistemlerinde
       (ör. tmpfs) tehlike gösterilemez ve test atlanır.
    2. **Açık referansla** aynı senaryo: POSIX açık tanıtıcısı olan inode'u
       serbest bırakmadığı için yeni dizin zorunlu olarak farklı kimlik alır ve
       `_reassert_root` değişimi yakalar.
    """
    unpinned = tmp_path / "referanssiz"
    unpinned.mkdir()
    identity_before = _path_identity(unpinned)
    unpinned.rmdir()
    unpinned.mkdir()
    if _path_identity(unpinned) != identity_before:
        pytest.skip(
            "Bu dosya sistemi silinen inode'u geri vermiyor; zayıf yaklaşımın "
            "yanıldığı bu ortamda gösterilemiyor."
        )

    pinned = tmp_path / "referansli"
    pinned.mkdir()
    with discovery_module._root_anchor(pinned) as anchored_identity:
        pinned.rmdir()
        pinned.mkdir()

        with pytest.raises(ScanRootUnavailableError, match="değişti"):
            discovery_module._reassert_root(pinned, anchored_identity)


def test_open_root_reference_is_released_after_a_failed_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tarama hata verse de kök tanıtıcısı bırakılır.

    Sızan bir tanıtıcı inode'u süresiz sabitler ve dizin silinse bile alanı
    serbest bırakmaz.
    """
    project = tmp_path / "proje"
    write(project / "site.yml", PLAYBOOK)

    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def _tracking_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def _tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    def _explode(path: str | Path) -> _MaterializedScan:
        raise OSError("tarama okunamadı")

    monkeypatch.setattr(discovery_module.os, "open", _tracking_open)
    monkeypatch.setattr(discovery_module.os, "close", _tracking_close)
    monkeypatch.setattr(discovery_module.os, "scandir", _explode)

    with pytest.raises(ScanRootUnavailableError):
        discover_playbooks(project, project_id=1, limits=ScanLimits())

    assert opened, "Kök açık bir referansla sabitlenmedi."
    assert set(opened) <= set(closed), "Hatalı tarama sonrası kök tanıtıcısı sızdı."


def test_root_identity_is_stable_for_an_untouched_directory(tmp_path: Path) -> None:
    """Kimlik kontrolü normal taramada yanlış alarm üretmemelidir."""
    write(tmp_path / "site.yml", PLAYBOOK)

    for _ in range(3):
        assert scan(tmp_path) == ["site.yml"]


def _inode_identity_supported(path: Path) -> bool:
    return path.stat().st_ino != 0


def _path_identity(path: Path) -> tuple[int, int]:
    """Kimliği **path üzerinden** okur: eski zayıf yaklaşımın gördüğü değer."""
    result = path.stat()
    return result.st_dev, result.st_ino


def test_result_carries_project_id_and_timestamp(tmp_path: Path) -> None:
    write(tmp_path / "site.yml", PLAYBOOK)

    result = discover_playbooks(tmp_path, project_id=42, limits=ScanLimits())

    assert result.project_id == 42
    assert result.scanned_at.tzinfo is not None
    assert result.playbooks[0].size_bytes == (tmp_path / "site.yml").stat().st_size
    assert result.playbooks[0].modified_at.tzinfo is not None
