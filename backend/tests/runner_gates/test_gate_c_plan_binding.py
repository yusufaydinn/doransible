"""Kapı C — Plan/onay bağı için tehdit modeli ölçümü.

PRODUCTION KODU DEĞİLDİR (bkz. paket docstring'i).

Bu tur production generic plan store'u IMPLEMENT ETMEZ. Burada yalnız,
aday çözümler arasında seçim yapmayı mümkün kılan ölçülebilir gerçekler
saptanır:

* Yalnız seçili playbook dosyasının digest'i, playbook'un davranışını belirleyen
  role/vars/template/include dosyalarındaki değişikliği yakalar mı?
* İçerik manifest'i bunları yakalar mı ve maliyeti nedir?
* Manifest, project kökünden dışarı çıkan symlink'i görebilir mi?
* Plan ile worker başlangıcı arasındaki pencerede yapılan değişiklik,
  worker başlangıcında yeniden doğrulama ile yakalanır mı?

Ping `PreviewStore` production koduna DOKUNULMAZ; buradaki hiçbir şey onu
kullanmaz veya değiştirmez.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.runner_gates import probe_support as ps

pytestmark = pytest.mark.runner_gate


@dataclass(frozen=True)
class Manifest:
    """Bir project ağacının içerik manifest'i."""

    digest: str
    file_count: int
    total_bytes: int
    escaping_symlinks: list[str]
    all_symlinks: list[str]
    seconds: float


class ManifestLimitExceeded(Exception):
    """Manifest sınırı aşıldı; iş sessizce kısaltılmaz, REDDEDİLİR."""


# PROVISIONAL güvenlik sınırları. Bu değerler bir performans ölçümünden
# TÜRETİLMEMİŞTİR; saldırı yüzeyini sınırlamak için seçilmiş ilk değerlerdir ve
# gerçek sınır testleriyle doğrulanana kadar geçicidir (ADR-021 Kapı C).
PROVISIONAL_MAX_FILES = 10_000
PROVISIONAL_MAX_BYTES = 100 * 1024 * 1024


# Hash fonksiyonu ENJEKTE EDİLEBİLİR. İki test-only ihtiyaç için:
#   1. Büyük dosyanın hash'e HİÇ girmediğini kanıtlamak (çağrı kaydı).
#   2. Tarama ortasında deterministik olarak durdurup dosya değiştirmek.
# Production kodu değildir.
HashFile = Callable[[Path, int], tuple[str, int]]


def _hash_file(path: Path, remaining: int) -> tuple[str, int]:
    """Dosyayı hash'ler; (digest, okunan_bayt) döndürür.

    Okunan bayt bütçeye bağlanır: dosya `stat` ile ölçüldükten sonra BÜYÜRSE
    bile toplam sınır aşıldığı anda fail-closed durulur.
    """
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            read += len(chunk)
            if read > remaining:
                raise ManifestLimitExceeded(
                    f"{path.name}: okuma sirasinda bayt butcesi asildi "
                    f"(okunan {read} > kalan {remaining})"
                )
            digest.update(chunk)
    return digest.hexdigest(), read


def playbook_only_digest(playbook: Path) -> str:
    """Yalnız seçili playbook dosyasının digest'i (aday 2'nin zayıf biçimi)."""
    return _hash_file(playbook, PROVISIONAL_MAX_BYTES)[0]


def rel_of(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def content_manifest(
    root: Path,
    *,
    max_files: int = PROVISIONAL_MAX_FILES,
    max_bytes: int = PROVISIONAL_MAX_BYTES,
    hash_file: HashFile | None = None,
) -> Manifest:
    """Project ağacının tamamının deterministik içerik manifest'i.

    Her girdi için relatif path, dosya modu ve içerik hash'i bağlanır. Symlink
    İZLENMEZ; hedef dizesi manifest'e girer (böylece symlink swap digest'i
    değiştirir) ve kökten dışarı çıkanlar ayrıca raporlanır.

    DİKKAT — `os.walk(followlinks=False)` symlink olan DİZİNLERİ `dirnames`
    içinde bırakır ve içine inmez; yalnız `filenames` taranırsa dizin
    symlink'leri BÜTÜNÜYLE GÖRÜNMEZ olur. Ansible bu dizinleri (örneğin bir
    role) izleyebildiği için `dirnames` de açıkça denetlenir.

    SINIR SEMANTİĞİ — fail-closed ve **iş yapılmadan önce**:

    * Dosya sayısı sınırı her girdi işlenmeden ÖNCE denetlenir.
    * `stat` boyutu toplam bütçeyi aşacaksa dosya **hash edilmeden** reddedilir;
      dev bir dosya hash fonksiyonuna hiç girmez.
    * Hash sırasında dosya büyürse okunan bayt da bütçeye sayılır.
    * Sınır aşıldığında `ManifestLimitExceeded` yükselir ve **digest üretilmez**.
    """
    hasher: HashFile = hash_file or _hash_file
    started = time.monotonic()
    entries: list[str] = []
    escaping: list[str] = []
    symlinks: list[str] = []
    total_bytes = 0
    resolved_root = root.resolve()

    def reserve_entry(what: str) -> None:
        if len(entries) + 1 > max_files:
            raise ManifestLimitExceeded(
                f"dosya sayisi siniri asildi: {len(entries) + 1} > {max_files} ({what})"
            )

    def record_symlink(path: Path) -> None:
        rel = rel_of(path, root)
        reserve_entry(rel)
        target = os.readlink(path)
        symlinks.append(rel)
        try:
            resolved = (path.parent / target).resolve()
        except OSError:
            escaping.append(rel)
        else:
            if not resolved.is_relative_to(resolved_root):
                escaping.append(rel)
        entries.append(f"{rel}\0symlink\0{target}")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        # Dizin symlink'leri: os.walk içine inmez, bu yüzden burada ölçülür.
        for name in list(dirnames):
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                record_symlink(candidate)
                dirnames.remove(name)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                record_symlink(path)
                continue
            if not path.is_file():
                continue
            rel = rel_of(path, root)
            reserve_entry(rel)
            stat = path.stat()
            # HASH ETMEDEN ÖNCE: stat boyutu bütçeyi aşıyorsa reddet.
            if total_bytes + stat.st_size > max_bytes:
                raise ManifestLimitExceeded(
                    f"{rel}: bayt siniri asildi (stat {stat.st_size}, "
                    f"toplam {total_bytes}, sinir {max_bytes}) - dosya hash EDILMEDI"
                )
            file_digest, read = hasher(path, max_bytes - total_bytes)
            total_bytes += read
            entries.append(f"{rel}\0{stat.st_mode & 0o777:o}\0{file_digest}")

    digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return Manifest(
        digest=digest,
        file_count=len(entries),
        total_bytes=total_bytes,
        escaping_symlinks=sorted(escaping),
        all_symlinks=sorted(symlinks),
        seconds=time.monotonic() - started,
    )


def naive_file_only_manifest(root: Path) -> list[str]:
    """R0-D3'teki İLK hâlin eşdeğeri: yalnız `filenames` taranır.

    Dizin symlink'lerini kaçırdığını göstermek için karşılaştırma amacıyla
    tutulur; production adayı DEĞİLDİR.
    """
    found: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                found.append(rel_of(path, root))
    return sorted(found)


def _build_project(root: Path) -> Path:
    """Playbook'un davranışını 6 ayrı dosyaya dağıtan gerçekçi bir project."""
    (root / "group_vars").mkdir(parents=True)
    (root / "roles" / "probe" / "tasks").mkdir(parents=True)
    (root / "roles" / "probe" / "defaults").mkdir(parents=True)
    (root / "roles" / "probe" / "templates").mkdir(parents=True)
    (root / "tasks").mkdir(parents=True)

    playbook = root / "site.yml"
    playbook.write_text(
        "- name: site\n"
        "  hosts: probe\n"
        "  gather_facts: false\n"
        "  roles:\n"
        "    - probe\n"
        "  tasks:\n"
        "    - ansible.builtin.include_tasks: tasks/extra.yml\n",
        encoding="utf-8",
    )
    (root / "group_vars" / "all.yml").write_text("paket: nginx\n", encoding="utf-8")
    (root / "roles" / "probe" / "tasks" / "main.yml").write_text(
        "- name: rol taski\n  ansible.builtin.debug:\n    msg: 'zararsiz'\n",
        encoding="utf-8",
    )
    (root / "roles" / "probe" / "defaults" / "main.yml").write_text(
        "hedef_durum: present\n", encoding="utf-8"
    )
    (root / "roles" / "probe" / "templates" / "conf.j2").write_text(
        "durum={{ hedef_durum }}\n", encoding="utf-8"
    )
    (root / "tasks" / "extra.yml").write_text(
        "- name: ek task\n  ansible.builtin.debug:\n    msg: 'ek'\n",
        encoding="utf-8",
    )
    return playbook


MUTATIONS: dict[str, str] = {
    "role_task": "roles/probe/tasks/main.yml",
    "role_default": "roles/probe/defaults/main.yml",
    "template": "roles/probe/templates/conf.j2",
    "group_vars": "group_vars/all.yml",
    "included_task": "tasks/extra.yml",
}


def test_gate_c_playbook_only_digest_misses_dependent_files(tmp_path: Path) -> None:
    """Yalnız playbook digest'i, davranışı belirleyen 5 dosyanın değişimini kaçırır.

    Bu ölçüm, "seçili playbook dosyasının digest'ini almak yeterlidir"
    varsayımını ADR-021 Kapı C için çürütür.
    """
    root = tmp_path / "project"
    root.mkdir()
    playbook = _build_project(root)

    plan_playbook_digest = playbook_only_digest(playbook)
    plan_manifest = content_manifest(root)

    missed_by_playbook_digest: list[str] = []
    caught_by_manifest: list[str] = []

    for label, relative in MUTATIONS.items():
        target = root / relative
        original = target.read_text(encoding="utf-8")
        # Plan onaylandıktan SONRA yapılan, davranışı değiştiren düzenleme.
        target.write_text(original + "# plan sonrasi degisiklik\n", encoding="utf-8")

        if playbook_only_digest(playbook) == plan_playbook_digest:
            missed_by_playbook_digest.append(label)
        if content_manifest(root).digest != plan_manifest.digest:
            caught_by_manifest.append(label)

        target.write_text(original, encoding="utf-8")

    print(
        "\nGATE-C MEASUREMENT [digest-scope] "
        + json.dumps(
            {
                "mutations_tested": sorted(MUTATIONS),
                "missed_by_playbook_only_digest": sorted(missed_by_playbook_digest),
                "caught_by_content_manifest": sorted(caught_by_manifest),
                "manifest_file_count": plan_manifest.file_count,
                "manifest_total_bytes": plan_manifest.total_bytes,
                "manifest_seconds": round(plan_manifest.seconds, 4),
            },
            indent=2,
        )
    )

    assert sorted(missed_by_playbook_digest) == sorted(MUTATIONS)
    assert sorted(caught_by_manifest) == sorted(MUTATIONS)
    # Manifest, geri alma sonrası ilk değere dönmeli (deterministik olmalı).
    assert content_manifest(root).digest == plan_manifest.digest


def test_gate_c_symlink_matrix(tmp_path: Path) -> None:
    """Dosya symlink'i, DİZİN symlink'i ve kök içi symlink ayrı ayrı ölçülür.

    Kritik bulgu: yalnız `filenames` tarayan naif yürüyüş, project kökünden
    dışarı çıkan bir DİZİN symlink'ini bütünüyle kaçırır. Ansible bir role'ü
    dizin symlink'i üzerinden izleyebildiği için bu, onaylanan içerik ile
    yürütülen içeriğin ayrışmasına yol açar.
    """
    root = tmp_path / "project"
    root.mkdir()
    _build_project(root)

    outside_file = tmp_path / "outside-secret.txt"
    outside_file.write_text("disarida\n", encoding="utf-8")
    outside_dir = tmp_path / "outside-role"
    (outside_dir / "tasks").mkdir(parents=True)
    (outside_dir / "tasks" / "main.yml").write_text(
        "- name: disaridan gelen task\n  ansible.builtin.debug:\n    msg: 'disari'\n",
        encoding="utf-8",
    )

    (root / "roles" / "probe" / "files").mkdir(parents=True)
    (root / "roles" / "probe" / "files" / "escape.txt").symlink_to(outside_file)
    (root / "roles" / "probe" / "files" / "benign.txt").symlink_to(root / "group_vars" / "all.yml")
    # Kökten dışarı çıkan DİZİN symlink'i — bir role olarak kullanılabilir.
    (root / "roles" / "escaped_role").symlink_to(outside_dir, target_is_directory=True)

    manifest = content_manifest(root)
    naive = naive_file_only_manifest(root)

    print(
        "\nGATE-C MEASUREMENT [symlink-matrix] "
        + json.dumps(
            {
                "all_symlinks": manifest.all_symlinks,
                "escaping_symlinks": manifest.escaping_symlinks,
                "naive_file_only_walker_saw": naive,
                "directory_symlink_missed_by_naive_walker": ("roles/escaped_role" not in naive),
            },
            indent=2,
        )
    )

    assert manifest.escaping_symlinks == [
        "roles/escaped_role",
        "roles/probe/files/escape.txt",
    ]
    assert "roles/probe/files/benign.txt" in manifest.all_symlinks
    assert "roles/probe/files/benign.txt" not in manifest.escaping_symlinks
    # R0-D3'teki hâl dizin symlink'ini kaçırıyordu; bu ölçüm onu sabitler.
    assert "roles/escaped_role" not in naive


def test_gate_c_symlink_swap_changes_manifest(tmp_path: Path) -> None:
    """Symlink hedefinin değiştirilmesi manifest digest'ini değiştirmeli."""
    root = tmp_path / "project"
    root.mkdir()
    _build_project(root)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("bir\n", encoding="utf-8")
    second.write_text("iki\n", encoding="utf-8")

    (root / "roles" / "probe" / "files").mkdir(parents=True)
    link = root / "roles" / "probe" / "files" / "swap.txt"
    link.symlink_to(first)
    before = content_manifest(root)

    link.unlink()
    link.symlink_to(second)
    after = content_manifest(root)

    print(
        "\nGATE-C MEASUREMENT [symlink-swap] "
        + json.dumps(
            {
                "digest_before": before.digest[:16],
                "digest_after": after.digest[:16],
                "changed": before.digest != after.digest,
            },
            indent=2,
        )
    )

    assert before.digest != after.digest


def test_gate_c_manifest_rejects_limit_overrun(tmp_path: Path) -> None:
    """Sınır aşımında manifest sessizce kısaltmaz, REDDEDER."""
    root = tmp_path / "project"
    root.mkdir()
    _build_project(root)
    for index in range(50):
        (root / f"extra{index}.yml").write_text("x\n", encoding="utf-8")

    with pytest.raises(ManifestLimitExceeded):
        content_manifest(root, max_files=10)

    big = root / "big.bin"
    big.write_bytes(b"a" * 5000)
    with pytest.raises(ManifestLimitExceeded):
        content_manifest(root, max_bytes=1000)
    # Sınır aşımında digest ÜRETİLMEZ; istisna yükselir.

    print("\nGATE-C MEASUREMENT [limits] " + json.dumps({"rejects_overrun": True}))


def test_gate_c_manifest_is_not_atomic_under_concurrent_change(tmp_path: Path) -> None:
    """Manifest atomik DEĞİLDİR — deterministik olarak kanıtlanır.

    Sıra (timing sleep'i YOK, iki `threading.Event` ile kilitlenir):

    1. Manifest taraması ilk dosyayı (`aaa-first.yml`) hash eder.
    2. Hash hook'u `first_hashed` event'ini set eder ve `mutation_done`
       event'ini BEKLER — tarama bu noktada durur.
    3. Mutator thread `first_hashed`'i görür, ZATEN HASH EDİLMİŞ dosyayı
       değiştirir ve `mutation_done`'ı set eder.
    4. Tarama devam eder ve `during` manifest'ini döndürür.

    Böylece mutation'ın manifest DÖNMEDEN ÖNCE tamamlandığı garanti edilir.
    `during`, dosyanın ESKİ içeriğini taşır; yani manifest döndüğü anda
    filesystem'in gerçek hâlini temsil ETMEZ.
    """
    root = tmp_path / "project"
    root.mkdir()
    _build_project(root)

    victim = root / "aaa-first.yml"
    victim.write_text("ilk-icerik\n", encoding="utf-8")
    baseline = content_manifest(root)

    first_hashed = threading.Event()
    mutation_done = threading.Event()
    hashed_order: list[str] = []

    def blocking_hash(path: Path, remaining: int) -> tuple[str, int]:
        result = _hash_file(path, remaining)
        hashed_order.append(rel_of(path, root))
        if path == victim:
            # Kurban dosya hash EDİLDİ; şimdi taramayı durdur ve değiştirilmesini bekle.
            first_hashed.set()
            assert mutation_done.wait(timeout=30), "mutator zamaninda calismadi"
        return result

    def mutate() -> None:
        assert first_hashed.wait(timeout=30), "hash hook'u tetiklenmedi"
        victim.write_text("tarama-sirasinda-degisti\n", encoding="utf-8")
        mutation_done.set()

    worker = threading.Thread(target=mutate)
    worker.start()
    during = content_manifest(root, hash_file=blocking_hash)
    worker.join(timeout=30)
    after = content_manifest(root)

    print(
        "\nGATE-C MEASUREMENT [manifest-not-atomic] "
        + json.dumps(
            {
                "victim_hashed_first": hashed_order[0] if hashed_order else None,
                "mutation_completed_before_manifest_returned": mutation_done.is_set(),
                "during_equals_baseline_stale": during.digest == baseline.digest,
                "during_equals_after": during.digest == after.digest,
                "baseline_equals_after": baseline.digest == after.digest,
            },
            indent=2,
        )
    )

    assert worker.is_alive() is False
    assert hashed_order[0] == "aaa-first.yml", f"kurban ilk hash edilmedi: {hashed_order[:3]}"
    # Mutation, manifest dönmeden ÖNCE bitti (hook onu beklemeden devam etmedi).
    assert mutation_done.is_set() is True
    # `during` eski içeriği taşıyor: dönüş anındaki filesystem'i temsil etmiyor.
    assert during.digest == baseline.digest, "during, mutation oncesi hali tasimali"
    assert during.digest != after.digest, "during ile after ayni; atomik olmama gosterilemedi"
    assert baseline.digest != after.digest


def test_gate_c_oversized_file_is_rejected_without_hashing(tmp_path: Path) -> None:
    """Bütçeyi aşan tek bir dev dosya hash fonksiyonuna HİÇ girmemeli."""
    root = tmp_path / "project"
    root.mkdir()
    _build_project(root)
    big = root / "zzz-big.bin"
    big.write_bytes(b"a" * 200_000)

    hashed: list[str] = []

    def recording_hash(path: Path, remaining: int) -> tuple[str, int]:
        hashed.append(rel_of(path, root))
        return _hash_file(path, remaining)

    with pytest.raises(ManifestLimitExceeded) as excinfo:
        content_manifest(root, max_bytes=50_000, hash_file=recording_hash)

    print(
        "\nGATE-C MEASUREMENT [oversized-file] "
        + json.dumps(
            {
                "error": str(excinfo.value),
                "hashed_files": hashed,
                "big_file_hashed": "zzz-big.bin" in hashed,
            },
            indent=2,
        )
    )

    assert "zzz-big.bin" not in hashed, "dev dosya hash fonksiyonuna girdi"
    assert "hash EDILMEDI" in str(excinfo.value)


def test_gate_c_file_count_limit_rejects_before_hashing_whole_tree(tmp_path: Path) -> None:
    """Çok sayıda entry senaryosu, ağacın tamamı hash edilmeden reddedilmeli."""
    root = tmp_path / "project"
    root.mkdir()
    _build_project(root)
    for index in range(200):
        (root / f"pad{index:04d}.yml").write_text(f"pad {index}\n", encoding="utf-8")

    total_files = sum(1 for _ in root.rglob("*") if _.is_file())
    hashed: list[str] = []

    def recording_hash(path: Path, remaining: int) -> tuple[str, int]:
        hashed.append(rel_of(path, root))
        return _hash_file(path, remaining)

    with pytest.raises(ManifestLimitExceeded):
        content_manifest(root, max_files=10, hash_file=recording_hash)

    print(
        "\nGATE-C MEASUREMENT [file-count-limit] "
        + json.dumps(
            {
                "files_on_disk": total_files,
                "files_hashed_before_reject": len(hashed),
                "whole_tree_hashed": len(hashed) >= total_files,
            },
            indent=2,
        )
    )

    assert len(hashed) < total_files, "agacin tamami hash edildi"
    assert len(hashed) <= 10


def test_gate_c_file_growing_during_hash_is_rejected(tmp_path: Path) -> None:
    """`stat` sonrası büyüyen dosya, okunan bayt bütçesiyle yakalanmalı."""
    root = tmp_path / "project"
    root.mkdir()
    _build_project(root)
    target = root / "zzz-grow.bin"
    target.write_bytes(b"a" * 1000)

    def growing_hash(path: Path, remaining: int) -> tuple[str, int]:
        if path == target:
            # stat'tan sonra dosya büyüdü; gerçek okuma bütçeyi aşacak.
            target.write_bytes(b"a" * (remaining + 5000))
        return _hash_file(path, remaining)

    with pytest.raises(ManifestLimitExceeded) as excinfo:
        content_manifest(root, max_bytes=20_000, hash_file=growing_hash)

    print(
        "\nGATE-C MEASUREMENT [file-grew-during-hash] "
        + json.dumps({"error": str(excinfo.value)}, indent=2)
    )
    assert "butcesi asildi" in str(excinfo.value)


def test_gate_c_revalidation_at_worker_start_catches_queue_window(tmp_path: Path) -> None:
    """Plan claim'i ile worker başlangıcı arasındaki pencere yakalanabiliyor mu?

    Sıra: plan üretilir → kullanıcı onaylar (claim) → iş kuyrukta bekler →
    worker başlar. Bu pencerede dosya değişirse, worker başlangıcındaki yeniden
    doğrulama bunu yakalamalıdır.
    """
    root = tmp_path / "project"
    root.mkdir()
    _build_project(root)

    plan_manifest = content_manifest(root)  # plan anı
    # ... kullanıcı onayı, kuyruk penceresi ...
    (root / "roles" / "probe" / "tasks" / "main.yml").write_text(
        "- name: degistirilmis rol taski\n"
        "  ansible.builtin.command:\n"
        "    argv: ['/bin/true', 'kuyruk-penceresinde-eklendi']\n",
        encoding="utf-8",
    )
    worker_start_manifest = content_manifest(root)  # worker başlangıcı

    print(
        "\nGATE-C MEASUREMENT [queue-window] "
        + json.dumps(
            {
                "plan_digest": plan_manifest.digest[:16],
                "worker_start_digest": worker_start_manifest.digest[:16],
                "mismatch_detected": plan_manifest.digest != worker_start_manifest.digest,
            },
            indent=2,
        )
    )

    assert plan_manifest.digest != worker_start_manifest.digest


def test_gate_c_manifest_cost_on_larger_tree(tmp_path: Path) -> None:
    """Manifest'in boyut/dosya sayısı sınırlarını belirlemek için maliyet ölçümü."""
    root = tmp_path / "project"
    root.mkdir()
    _build_project(root)

    for index in range(2000):
        directory = root / "roles" / f"generated{index // 100}" / "tasks"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"task{index}.yml").write_text(
            f"- name: uretilmis {index}\n  ansible.builtin.debug:\n    msg: '{index}'\n",
            encoding="utf-8",
        )

    manifest = content_manifest(root)

    print(
        "\nGATE-C MEASUREMENT [cost] "
        + json.dumps(
            {
                "file_count": manifest.file_count,
                "total_bytes": manifest.total_bytes,
                "seconds": round(manifest.seconds, 4),
            },
            indent=2,
        )
    )

    assert manifest.file_count > 2000


ESCAPED_ROLE_PLAYBOOK = """
- name: gate-c escaped role probe
  hosts: probe
  gather_facts: false
  roles:
    - escaped_role
"""


def test_gate_c_ansible_follows_directory_symlink_out_of_project(tmp_path: Path) -> None:
    """Ansible, project kökünden dışarı çıkan DİZİN symlink'ini izliyor mu?

    Bu ölçüm, dizin symlink'inin teorik değil GERÇEK bir kaçış yolu olduğunu
    belirler: naif manifest onu görmez, Ansible ise izleyip çalıştırır. İkisi
    birlikte, onaylanan içerik ile yürütülen içeriğin ayrışması demektir.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pdd = ps.make_private_data_dir(workspace)
    project = pdd / "project"

    marker_path = workspace / "escaped-role-ran.txt"
    outside_role = tmp_path / "outside-role"
    (outside_role / "tasks").mkdir(parents=True)
    (outside_role / "tasks" / "main.yml").write_text(
        "- name: project disindaki role calisti\n"
        "  ansible.builtin.copy:\n"
        "    content: 'escaped-role-executed'\n"
        f"    dest: '{marker_path}'\n"
        "    mode: '0600'\n",
        encoding="utf-8",
    )

    (project / "roles").mkdir(parents=True, exist_ok=True)
    (project / "roles" / "escaped_role").symlink_to(outside_role, target_is_directory=True)
    (project / "site.yml").write_text(ESCAPED_ROLE_PLAYBOOK, encoding="utf-8")

    result_path = workspace / "result.json"
    config_path = workspace / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "private_data_dir": str(pdd),
                "playbook": "site.yml",
                "settings": {"job_timeout": 120, "suppress_ansible_output": True},
                "result_path": str(result_path),
            }
        ),
        encoding="utf-8",
    )

    env = ps.build_isolated_environment(workspace=workspace, venv_bin=ps.venv_bin_dir())
    subprocess.run(  # noqa: S603
        [sys.executable, str(Path(__file__).parent / "runner_child.py"), str(config_path)],
        env=env,
        close_fds=True,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        cwd=str(workspace),
    )

    followed = marker_path.exists()
    status = None
    if result_path.exists():
        try:
            status = json.loads(result_path.read_text(encoding="utf-8"))["status"]
        except (OSError, ValueError, KeyError):
            status = None

    print(
        "\nGATE-C MEASUREMENT [ansible-follows-directory-symlink] "
        + json.dumps(
            {
                "runner_status": status,
                "escaped_role_executed": followed,
                "naive_manifest_saw_symlink": "roles/escaped_role"
                in naive_file_only_manifest(project),
            },
            indent=2,
        )
    )

    assert followed is True, (
        "Ansible dizin symlink'ini izlemedi. Bu, ADR-021 Kapi C'deki "
        "'butun symlink'ler fail-closed reddedilir' gerekcesinin yeniden "
        "olculmesini gerektirir."
    )
