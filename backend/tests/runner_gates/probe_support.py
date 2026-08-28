"""Kapı A-D probe'ları için ortak ölçüm yardımcıları.

PRODUCTION KODU DEĞİLDİR (bkz. paket docstring'i).

Buradaki iki tasarım kuralı ölçümün güvenli kalması içindir:

1. Süreç işlemleri YALNIZCA komut satırında benzersiz bir marker taşıyan
   süreçler üzerinde yapılır. Marker her testte yeniden üretilir; bu yüzden
   probe, kendi başlatmadığı hiçbir sürece sinyal göndermez.
2. Environment, parent'tan filtrelenerek değil SIFIRDAN allowlist ile kurulur.
   Runner kendisini başlatan sürecin environment'ını miras aldığı için
   (ADR-021 Kapı A), tek güvenilir sınır child process'in kendi environment'ıdır.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")

MARKER_PREFIX = "AOPSGATE"

# Kapı A, B ve D probe'ları süreç kimliğini ve descriptor tablosunu `/proc`
# üzerinden okur; bu Linux'a özgüdür. Proje Linux **ve** macOS'u hedeflediği
# için (docs/gelistirme-ortami.md), bu probe'lar macOS'ta atlanır ve
# ATLANAN TEST KOŞMUŞ TEST SAYILMAZ: kapı kanıtı yalnız Linux'ta üretilmiştir.
# Kapı C'nin saf filesystem testleri her iki platformda da çalışır.
IS_LINUX = sys.platform.startswith("linux")

NON_LINUX_SKIP_REASON = (
    "Kapı A/B/D probe'ları süreç ağacını ve descriptor tablosunu /proc üzerinden "
    "ölçer; /proc yalnız Linux'ta vardır. macOS'ta bu kapılar ÖLÇÜLMEMİŞ sayılır."
)


@dataclass(frozen=True)
class ProcInfo:
    """`/proc` üzerinden okunan tek bir sürecin kimliği."""

    pid: int
    ppid: int
    pgid: int
    sid: int
    cmdline: str


def new_marker(label: str) -> str:
    """Test başına benzersiz, komut satırında aranabilir marker üretir."""
    return f"{MARKER_PREFIX}-{label}-{uuid.uuid4().hex}"


def _read_stat(pid: int) -> tuple[int, int, int] | None:
    """`/proc/<pid>/stat` içinden (ppid, pgid, sid) okur.

    `comm` alanı boşluk ve parantez içerebildiği için son ')' karakterinden
    sonrası ayrıştırılır.
    """
    try:
        raw = (PROC / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 1 :].split()
    if len(fields) < 4:
        return None
    try:
        return int(fields[1]), int(fields[2]), int(fields[3])
    except ValueError:
        return None


def _cmdline(pid: int) -> str | None:
    try:
        raw = (PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def scan_marker_processes(marker: str) -> list[ProcInfo]:
    """Komut satırında `marker` geçen bütün canlı süreçleri döndürür."""
    found: list[ProcInfo] = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmdline = _cmdline(pid)
        if cmdline is None or marker not in cmdline:
            continue
        stat = _read_stat(pid)
        if stat is None:
            continue
        ppid, pgid, sid = stat
        found.append(ProcInfo(pid=pid, ppid=ppid, pgid=pgid, sid=sid, cmdline=cmdline))
    return found


def wait_for_marker_processes(
    marker: str, *, minimum: int = 1, timeout: float = 20.0
) -> list[ProcInfo]:
    """Marker'lı en az `minimum` süreç görünene kadar bekler."""
    deadline = time.monotonic() + timeout
    seen: list[ProcInfo] = []
    while time.monotonic() < deadline:
        seen = scan_marker_processes(marker)
        if len(seen) >= minimum:
            return seen
        time.sleep(0.05)
    return seen


def wait_until_gone(marker: str, *, timeout: float) -> tuple[bool, float]:
    """Marker'lı süreçlerin yok olmasını bekler.

    (hepsi_gitti, geçen_süre) döndürür.
    """
    start = time.monotonic()
    deadline = start + timeout
    while time.monotonic() < deadline:
        if not scan_marker_processes(marker):
            return True, time.monotonic() - start
        time.sleep(0.05)
    return not scan_marker_processes(marker), time.monotonic() - start


def terminate_marker_processes(marker: str, *, grace: float = 5.0) -> list[int]:
    """Marker'lı süreçleri sonlandırır ve sonlandırılan PID'leri döndürür.

    Güvenlik kuralı: sinyal göndermeden hemen önce cmdline yeniden okunur ve
    marker hâlâ orada değilse sinyal gönderilmez. PID yeniden kullanımı olsa
    bile ilgisiz bir sürece dokunulmaz.
    """
    signalled: list[int] = []
    self_pid = os.getpid()

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for proc in scan_marker_processes(marker):
            if proc.pid == self_pid:
                continue
            current = _cmdline(proc.pid)
            if current is None or marker not in current:
                continue
            try:
                os.kill(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                continue
            if proc.pid not in signalled:
                signalled.append(proc.pid)
        gone, _ = wait_until_gone(marker, timeout=grace if sig == signal.SIGTERM else 2.0)
        if gone:
            break

    return signalled


# Ansible'ın gerçek kullanıcı home'una yazan temp/kontrol yüzeyleri. Her biri
# AYRI bir seçenektir; birini bağlamak diğerini kapatmaz.
CONTROLLED_TEMP_KEYS: tuple[str, ...] = (
    "ANSIBLE_LOCAL_TEMP",
    "ANSIBLE_REMOTE_TEMP",
    "ANSIBLE_REMOTE_TMP",
    "ANSIBLE_ASYNC_DIR",
    "ANSIBLE_SSH_CONTROL_PATH_DIR",
)

# `build_isolated_environment()` çıktısının TAM anahtar kümesi. Testler bunu
# exact eşitlikle bağlar; sayı saymak yerine küme karşılaştırılır.
EXPECTED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "ANSIBLE_HOST_KEY_CHECKING",
        "ANSIBLE_RETRY_FILES_ENABLED",
        "ANSIBLE_HOME",
        "ANSIBLE_CONFIG",
        *CONTROLLED_TEMP_KEYS,
    }
)


def controlled_temp_paths(workspace: Path) -> dict[str, Path]:
    """`build_isolated_environment()` ile aynı yolları döndürür (assertion için)."""
    return {
        "ANSIBLE_LOCAL_TEMP": workspace / "tmp" / "ansible-local",
        "ANSIBLE_REMOTE_TEMP": workspace / "ansible-remote-tmp",
        "ANSIBLE_REMOTE_TMP": workspace / "ansible-remote-tmp",
        "ANSIBLE_ASYNC_DIR": workspace / "ansible-async",
        "ANSIBLE_SSH_CONTROL_PATH_DIR": workspace / "ansible-cp",
    }


CONTROLLED_ANSIBLE_CFG = """# Kapı A probe'unun kontrollü ansible.cfg dosyası.
# Bilinçli olarak BOŞTUR: hiçbir ayar geçersiz kılınmaz, Ansible varsayılanları
# geçerli olur. Varlığının amacı, ANSIBLE_CONFIG'i sabitleyerek project
# içindeki ansible.cfg'nin ve ~/.ansible.cfg'nin okunmasını engellemektir.
[defaults]
"""


def passwd_home() -> Path:
    """Hedef kullanıcının **passwd kaydındaki** home dizini.

    `os.environ["HOME"]` ile AYNI ŞEY DEĞİLDİR ve bu ayrım ölçümün merkezindedir:
    Ansible'ın `remote_tmp`/`async_dir`/`control_path_dir` varsayılanlarındaki
    `~` işareti environment HOME'dan değil passwd kaydından çözülür (ölçüldü).
    Bu yüzden kaçak taraması daima passwd home'a göre yapılır; HOME başka bir
    değere override edilmiş olsa bile gerçek kaçak yakalanabilsin.

    `pwd` modülü POSIX'e özgüdür ve Windows'ta yoktur. Module-level import,
    Kapı A/B/D'nin "açık gerekçeyle skip" davranışına ulaşılamadan collection'ı
    `ModuleNotFoundError` ile kırardı; bu yüzden import fonksiyon içindedir ve
    Linux dışında fallback yol üretmek yerine açık hata verilir.
    """
    if not IS_LINUX:
        raise RuntimeError("passwd_home yalnız Linux runner-gate probe'unda kullanılabilir.")
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


# Ansible'ın passwd home'una düşen varsayılanları. Bunlardan biri artifact'ta
# veya child environment'ında görünürse izolasyon delinmiştir.
PASSWD_HOME_DEFAULT_SUFFIXES: tuple[str, ...] = (
    ".ansible/tmp",
    ".ansible_async",
    ".ansible/cp",
)


def build_isolated_environment(
    *,
    workspace: Path,
    venv_bin: Path,
    pin_ansible_config: bool = True,
) -> dict[str, str]:
    """Parent environment'tan HİÇBİR ŞEY miras almayan environment kurar.

    Bu, ADR-021 Kapı A'nın önerdiği modeldir: `env/envvars` Runner tarafından
    mevcut environment'ın ÜZERİNE eklendiği için, tek güvenilir sınır child
    process'in environment'ının allowlist ile sıfırdan kurulmasıdır.

    `HOME`, `TMPDIR`, `ANSIBLE_HOME` ve `ANSIBLE_CONFIG` bilinçli olarak
    workspace altına sabitlenir. Parent'ın `HOME`'u ve parent'ın
    `ANSIBLE_CONFIG`'i **hiçbir koşulda** kullanılmaz.

    `pin_ansible_config=False` yalnız NEGATİF ölçüm içindir: `ANSIBLE_CONFIG`
    verilmediğinde Runner'ın project içindeki `ansible.cfg`'yi okuduğunu
    kanıtlamak için kullanılır. Production modeli daima `True`'dur.
    """
    home = workspace / "home"
    tmp = workspace / "tmp"
    ansible_home = workspace / "ansible-home"
    # Ansible'ın DÖRT ayrı temp/kontrol yüzeyi. Hepsi ayrı ayrı bağlanmazsa
    # varsayılanları gerçek kullanıcı home'una yazar (aşağıdaki nota bakın).
    local_temp = tmp / "ansible-local"  # controller-side local temp
    remote_tmp = workspace / "ansible-remote-tmp"  # hedef taraf temp
    async_dir = workspace / "ansible-async"  # async job metadata
    control_path_dir = workspace / "ansible-cp"  # SSH ControlPath soketleri
    for directory in (home, tmp, ansible_home, local_temp, remote_tmp, async_dir, control_path_dir):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)

    env: dict[str, str] = {
        # Runner `ansible-playbook`'u PATH üzerinden bulur; venv'in bin dizini
        # olmadan rc=127 üretir (ölçüldü).
        "PATH": f"{venv_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        # Runner varsayılan olarak ANSIBLE_HOST_KEY_CHECKING=False enjekte eder
        # (ölçüldü). Ürünün host key politikası bunu açıkça geri almalıdır.
        "ANSIBLE_HOST_KEY_CHECKING": "True",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        "ANSIBLE_LOCAL_TEMP": str(local_temp),
        # `~/.ansible` yerine kontrollü alan; collection/role önbelleği ve
        # geçici dosyalar workspace dışına çıkmaz.
        "ANSIBLE_HOME": str(ansible_home),
        # --- remote_tmp -------------------------------------------------
        # `remote_tmp` varsayılanı `~/.ansible/tmp`'dir ve `~` işareti
        # **$HOME'dan DEĞİL**, hedef kullanıcının passwd kaydından çözülür.
        # ÖLÇÜLDÜ: `HOME` workspace'e kurulmuş olmasına rağmen Ansible
        # `/home/<kullanıcı>/.ansible/tmp` yolunu kullandı. `ansible_connection
        # =local` altında "hedef" controller'ın kendisi olduğu için bu, gerçek
        # kullanıcı home'una yazmak demektir. `ANSIBLE_LOCAL_TEMP` bunu
        # KAPATMAZ; ayrı bir seçenektir.
        #
        # `ANSIBLE_REMOTE_TEMP` ve `ANSIBLE_REMOTE_TMP` **tek bir seçeneğin
        # (`remote_tmp`) iki env alias'ıdır**. Aralarındaki öncelik Ansible'ın
        # iç ayrıntısıdır; bu yüzden ikisi de AYNI değere sabitlenir. Çelişkili
        # iki farklı değer verilmez ve sonuç alias önceliğine bağlı kalmaz.
        "ANSIBLE_REMOTE_TEMP": str(remote_tmp),
        "ANSIBLE_REMOTE_TMP": str(remote_tmp),
        # `async_dir` varsayılanı `~/.ansible_async`. İlk dilimde `async`
        # reddedilecek olsa bile probe environment'ının varsayılan yola
        # kaçmasına izin verilmez.
        "ANSIBLE_ASYNC_DIR": str(async_dir),
        # `control_path_dir` varsayılanı `~/.ansible/cp`. Bu probe SSH
        # kullanmaz; yine de varsayılanın gerçek home'a düşmemesi için bağlanır.
        "ANSIBLE_SSH_CONTROL_PATH_DIR": str(control_path_dir),
    }

    if pin_ansible_config:
        cfg = workspace / "ansible.cfg"
        cfg.write_text(CONTROLLED_ANSIBLE_CFG, encoding="utf-8")
        cfg.chmod(0o600)
        env["ANSIBLE_CONFIG"] = str(cfg)

    # Bilinçli olarak `extra`/override parametresi YOKTUR (fail-closed). Böyle
    # bir parametre, ileride bir çağrının `ANSIBLE_CONFIG`, `PATH` veya
    # `ANSIBLE_REMOTE_TMP` gibi güvenlik anahtarlarını sessizce ezmesine izin
    # verirdi. Yeni bir anahtar gerekiyorsa buraya ve `EXPECTED_ENV_KEYS`'e
    # açıkça eklenmeli; çağrı tarafından enjekte edilememeli.
    return env


def venv_bin_dir() -> Path:
    """Testi çalıştıran yorumlayıcının bin dizini."""
    return Path(sys.executable).parent


def mode_of(path: Path) -> str:
    """Dosya/dizin izin bitlerini okunur biçimde döndürür."""
    return oct(path.lstat().st_mode & 0o777)


def write_local_inventory(private_data_dir: Path, *, host: str = "probehost") -> Path:
    """Yalnız localhost'a, `ansible_connection=local` ile bağlanan inventory.

    Dış network ve SSH kullanılmaz.
    """
    inventory_dir = private_data_dir / "inventory"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    inventory_dir.chmod(0o700)
    path = inventory_dir / "hosts.ini"
    path.write_text(
        f"[probe]\n{host} ansible_connection=local\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def make_private_data_dir(root: Path) -> Path:
    """Boş bir Runner `private_data_dir` iskeleti kurar.

    Dizinler 0700 ile AÇIKÇA kurulur. Runner kendi oluşturduğu dizinlere
    kısıtlayıcı mod uygulamaz; yalnız umask'e tabidir (ADR-021 Kapı D). Bu
    yüzden izin sınırını ürün kurmak zorundadır.
    """
    pdd = root / "pdd"
    for directory in (pdd, pdd / "project", pdd / "env"):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    write_local_inventory(pdd)
    return pdd


def write_project_file(private_data_dir: Path, name: str, content: str) -> Path:
    """Project dosyasını 0600 ile yazar.

    Ürün, işe giren dosyaların iznini kendisi kurar; umask'e güvenilmez.
    """
    path = private_data_dir / "project" / name
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def iter_all_files(root: Path) -> list[Path]:
    """`root` altındaki bütün normal dosyaları döndürür (symlink izlemez)."""
    files: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.is_file() and not candidate.is_symlink():
                files.append(candidate)
    return files


def find_bytes_in_tree(root: Path, needle: str) -> list[Path]:
    """Ağaçtaki bütün dosyaları binary-safe tarar; eşleşen dosyaları döndürür.

    Metin kabul edip decode etmez: Runner event dosyaları ve geçici modül
    dosyaları binary olabilir.
    """
    target = needle.encode("utf-8")
    hits: list[Path] = []
    for path in iter_all_files(root):
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if target in blob:
            hits.append(path)
    return hits


def force_remove(path: Path) -> None:
    """Cleanup: ağacı kalıntısız siler."""
    shutil.rmtree(path, ignore_errors=True)
