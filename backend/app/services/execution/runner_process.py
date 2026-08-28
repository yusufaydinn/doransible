"""`ansible-runner` CLI child process katmanı (R1-V3C1B).

Bu modül **tek bir işi** yapar: dondurulmuş bir çalışma alanı üzerinde sabit
argümanlı bir `ansible-runner` sürecini sınırlar dâhilinde çalıştırır ve ham
sonucunu döndürür. Veritabanı, session, Job durumu, worker döngüsü, HTTP modeli
ve kalıcı artifact **burada yoktur**; olsalardı süreç sınırlarının kanıtı iş
akışı koduna karışırdı.

Sözleşme:

- Python ``ansible_runner`` API'si **import edilmez**. Runner, kendisini
  başlatan sürecin environment'ını miras alır (ADR-021 Kapı A); tek güvenilir
  sınır ayrı bir CLI process'idir ve o process'in environment'ı R1-V3C1A'nın
  :class:`RunnerEnvironment` sözlüğüyle **sıfırdan** kurulur.
- Komut bir **argüman listesidir**. ``shell=True`` yoktur, kullanıcı metni
  hiçbir aşamada shell string'e girmez (subprocess güvenlik sözleşmesi).
- Argüman kümesi ``--limit``, ``--tags``, ``--skip-tags``, ``--become`` ve
  extra-vars için **sabittir**: bunlar hiçbir kipte üretilmez. Tek koşullu
  argüman ``--cmdline=--check``'tir ve varlığı yalnız çağıranın verdiği
  :class:`~app.models.execution_mode.ExecutionMode`'a bağlıdır (R1-V3H1B2B):
  ``ExecutionMode.CHECK`` onu tam bir kez ekler, ``ExecutionMode.NORMAL``
  argv'yi bu tek argüman dışında birebir aynı bırakır. Kip zorunlu bir
  keyword-only parametredir; default'u ve bunun dışında bir "ek argüman"
  yolu yoktur.
- ``become=false`` yalnız **uygulamanın CLI seviyesinde ``--become``
  eklememesi** demektir. Güvenilir bir playbook kendi ``become:`` direktifini
  taşıyabilir; bu modül onu ne görür ne engeller (ADR-022 trusted-operator).
- Raw artifact alanı yalnız ``<job-run-dir>/raw``'dır, 0700'dür ve
  **çalıştırma bittiğinde her yolda silinir**. Kalıcı, güvenli sonuç bir
  sonraki dilimin (R1-V3C1C) işidir; bu katman diske kalıcı hiçbir şey
  bırakmaz.
- Ham stdout/stderr loglanmaz ve exception mesajına konmaz. Arızalar sabit,
  makine tarafından okunabilir ``details["reason"]`` kodlarıyla bildirilir;
  path, dosya içeriği veya çıktı parçası hata mesajına **yazılmaz**.

**Özgün ağaçlar hiç açılmaz.** Playbook doğrulaması yalnız *dondurulmuş*
project kökü içinde, descriptor-relative (``dir_fd`` + ``O_NOFOLLOW``) yapılır;
inventory olarak yalnız dondurulmuş ``inventory/hosts.yml`` kabul edilir.
Onaylanan içerik dondurulmuş kopyadır ve manifest yalnız onu doğrular.

**Project ve inventory aynı workspace'e bağlıdır.** İkisini ayrı ayrı doğrulamak
yetmez: her biri kendi başına geçerliyken *farklı* workspace'lerden gelebilir ve
o zaman bir planın project'i başka bir planın inventory'siyle çalıştırılırdı.
Bu yüzden doğrulanan şey iki yol değil, **tek bir düzendir**::

    <execution-plan-root>/<canonical UUID4>/
        project/
        inventory/hosts.yml
        manifest.json

Düzenin bütün parçaları zorunludur: project çocuğunun adı tam olarak
``project``, inventory çocuğunun adı tam olarak ``inventory``, dosyanın adı tam
olarak ``hosts.yml`` olmalı; ikisi de **aynı** workspace dizininin çocuğu
olmalı; workspace dizininin adı uygulamanın ürettiği canonical UUID4 olmalıdır.
Absolute yol içinde ``.``/``..`` kabul edilmez — lexical bir alias, aynı
dizinin iki farklı adla iki farklı workspace gibi görünmesine izin verirdi.
Workspace yerleşimi descriptor kontrolleriyle doğrulanır: workspace ve
çocukları ``O_NOFOLLOW`` ile açılır ve açılan descriptor ile isimdeki girdinin
aynı nesne olduğu ``fstat``/``lstat`` ile karşılaştırılır. Bu kontroller
**kernel seviyesinde TOCTOU koruması değildir**: doğrulama bitince
descriptor'lar kapanır ve ``ansible-runner`` argv'deki yolları daha sonra
kendisi yeniden açar. Dolayısıyla hostile bir concurrent writer'a karşı
"TOCTOU yoktur" garantisi verilmez. Sözleşme, uygulamanın sahip olduğu,
güvenilir ve dondurulmuş workspace'tir. DB executor, süreci başlatmadan hemen
önce manifesti yeniden doğrulamakla yükümlüdür.

**Manifest yeniden doğrulaması burada yoktur.** ``manifest.json`` yalnızca
**düzenin parçası** olarak aranır; okunmaz ve digest'i doğrulanmaz. Plan
kaydına ve beklenen digest'e sahip olan taraf DB executor dilimidir (R1-V3C1C);
bu modül plan kaydı olmadan bir bütünlük kontrolü **taklit etmez** — taklit
edilmiş bir kontrol, yapılmış bir kontrol sanılırdı.
"""

from __future__ import annotations

import contextlib
import os
import stat
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings
from app.core.errors import AppError
from app.models.execution_mode import ExecutionMode
from app.services.ansible.process import (
    BoundedProcessObserver,
    CompositeProcessObserver,
    ProcessLaunchError,
    ProcessLimits,
    run_bounded_process,
)
from app.services.execution.runner_env import DIRECTORY_MODE, RunnerEnvironment
from app.services.execution.workspace import (
    INVENTORY_DIRNAME,
    INVENTORY_FILENAME,
    MANIFEST_FILENAME,
    PROJECT_DIRNAME,
)

# Raw artifact alanının Job dizini altındaki sabit adı. `ansible-runner`
# ``--artifact-dir/<ident>`` altına yazar; ölçüm ve temizlik bu tek kök
# üzerinden yapılır.
RAW_DIRNAME = "raw"

# Raw ağacı ölçülürken ve silinirken uygulanan yapısal sınırlar. Sınırsız bir
# gezinti, derin veya çok girdili bir ağaçla ölçümün kendisini bir yük hâline
# getirirdi.
MAX_RAW_ENTRIES = 200_000
MAX_RAW_DEPTH = 16

# Raw bütçesinin **süreç çalışırken** ölçülme aralığı. Sınır yalnız süreç
# bittikten sonra kontrol edilseydi, sınırı aşan bayt zaten diske yazılmış
# olurdu; bütçe ancak çalışma anında ölçülünce gerçek bir sınırdır.
RAW_POLL_SECONDS = 0.2

# Ölçüm thread'inin durdurulurken beklendiği süre. Cömerttir: büyük ama meşru
# bir raw ağacının son ölçümü bir poll aralığından uzun sürebilir. Süre yine de
# sonludur; bu sürede bitmeyen ölçüm fail-closed sayılır.
OBSERVER_JOIN_SECONDS = 5.0

# `ansible-runner run` alt komutunun sabit anahtarları.
RUN_SUBCOMMAND = "run"
CHECK_CMDLINE_ARGUMENT = "--cmdline=--check"

# Playbook relative path'inde kabul edilmeyen parçalar.
_REJECTED_SEGMENTS = frozenset({"", ".", ".."})


class RunnerProcessError(AppError):
    """Runner süreci güvenli biçimde çalıştırılamadı.

    Altyapı hatasıdır ve fail-closed'dır. ``details["reason"]`` yalnız sabit bir
    sebep kodu taşır: path, playbook içeriği, environment değeri veya alt süreç
    çıktısı hata mesajına **girmez**.
    """

    status_code = 500
    code = "runner_process_unavailable"


@dataclass(frozen=True)
class RunnerProcessLimits:
    """Tek bir runner çalıştırmasına uygulanan sınırlar.

    Üçü de **çalışma anında** uygulanır: timeout süreci sonlandırır, stdout
    sınırı aşıldığı anda süreç sonlandırılır, raw bütçesi aşıldığı anda süreç
    sonlandırma talebi alır.
    """

    timeout_seconds: float
    max_stdout_bytes: int
    max_raw_bytes: int

    @classmethod
    def from_settings(cls, settings: Settings) -> RunnerProcessLimits:
        """Doğrulanmış ayarlardan sınırları üretir.

        Ayar doğrulaması :class:`~app.core.config.Settings` içindedir; burada
        yeniden yorumlanmaz, yalnız taşınır.
        """
        return cls(
            timeout_seconds=settings.playbook_runner_timeout_seconds,
            max_stdout_bytes=settings.playbook_runner_max_stdout_bytes,
            max_raw_bytes=settings.playbook_runner_max_raw_bytes,
        )


@dataclass(frozen=True)
class RunnerProcessResult:
    """Bir runner çalıştırmasının **ham** süreç sonucu.

    Bilinçli olarak yalnız süreç katmanına aittir: Job durumu, outcome kodu ve
    kullanıcıya dönecek hiçbir alan taşımaz. stdout metni normalize edilmemiş
    runner JSON'udur ve bu katmanda **hiçbir yere loglanmaz**; onu güvenli bir
    şemaya çeviren taraf :mod:`app.services.execution.normalize`'dır.
    """

    return_code: int
    stdout_text: str
    stderr_text: str
    timed_out: bool
    oversized_stream: str | None
    raw_limit_exceeded: bool
    started_at: datetime
    finished_at: datetime


def build_runner_arguments(
    *,
    command: Sequence[str],
    run_dir: Path,
    frozen_project_root: Path,
    frozen_inventory_path: Path,
    raw_dir: Path,
    job_id: str,
    playbook_path: str,
    mode: ExecutionMode,
) -> list[str]:
    """Sabit `ansible-runner` argv'sini, yalnız doğrulanmış kipe göre kurar.

    Sıra ve anahtar kümesi `ansible-runner` 2.4.3 CLI'sine göre sabittir.
    Fonksiyon çağıran tarafından genişletilebilecek bir "ek argüman" parametresi
    **almaz**: böyle bir parametre, ``--limit``/``--tags``/``--become``
    yasağını tek satırlık bir çağrıyla delerdi.

    ``--cmdline`` bilinçli olarak ``=`` biçimindedir: ayrı argüman olarak
    verilseydi ``--check`` argparse tarafından değer değil seçenek sayılırdı.

    ``mode`` zorunlu bir keyword-only parametredir ve default'u yoktur: kipi
    atlayan bir çağrı, eksik bir bağlamı sessizce ``check`` sayardı.
    ``ExecutionMode.CHECK`` için :data:`CHECK_CMDLINE_ARGUMENT` tam bir kez
    eklenir; ``ExecutionMode.NORMAL`` için argv bu tek argüman dışında birebir
    aynı kalır. Karşılaştırma bilinçli olarak **kimlik** (``is``) iledir: bu
    enum bir ``StrEnum`` olduğu için düz bir ``"check"``/``"normal"`` metni
    eşitlik testinde üyeyle eşit görünür; kimlik testi böyle bir metni,
    ``None``'ı ya da başka bir nesneyi sessizce ``check`` sanan bir fail-open
    yola düşürmez. Kabul edilen tek iki değer bu ikisidir; başka her şey
    ``runner_mode_invalid`` sebebiyle reddedilir ve sebep bu fonksiyonun
    kendisinden ötesine (raw dizini, child process) hiç ulaşmaz — çağıran onu
    süreç ve raw dizini açılmadan **önce** çağırır.
    """
    arguments = [
        *command,
        RUN_SUBCOMMAND,
        str(run_dir),
        "--project-dir",
        str(frozen_project_root),
        "--inventory",
        str(frozen_inventory_path),
        "--artifact-dir",
        str(raw_dir),
        "--ident",
        job_id,
        "--json",
        "--omit-env-files",
        "-p",
        playbook_path,
    ]
    if mode is ExecutionMode.CHECK:
        arguments.append(CHECK_CMDLINE_ARGUMENT)
    elif mode is not ExecutionMode.NORMAL:
        raise _unavailable("runner_mode_invalid")
    return arguments


def run_playbook_process(
    *,
    command: Sequence[str],
    runner_environment: RunnerEnvironment,
    job_id: str,
    frozen_project_root: Path,
    frozen_inventory_path: Path,
    playbook_path: str,
    mode: ExecutionMode,
    limits: RunnerProcessLimits,
    observer: BoundedProcessObserver | None = None,
) -> RunnerProcessResult:
    """Dondurulmuş çalışma alanı üzerinde runner sürecini sınırlarla çalıştırır.

    Args:
        command: ``settings.ansible_runner_command`` — bir **argüman listesi**.
        runner_environment: R1-V3C1A'nın ürettiği çalışma alanı ve child
            environment'ı. Child yalnız ``runner_environment.environment``
            anahtarlarını görür; parent environment'ı miras alınmaz.
        job_id: Job'un canonical UUID4 kimliği. ``--ident`` budur ve
            ``runner_environment.run_dir`` adının aynısı olmalıdır.
        frozen_project_root: **Dondurulmuş** project kökü
            (``<workspace>/project``).
        frozen_inventory_path: Aynı workspace'in dondurulmuş
            ``inventory/hosts.yml`` dosyası.
        playbook_path: Dondurulmuş project köküne **relative** playbook yolu.
        mode: Doğrulanmış execution kipi. Zorunludur ve default'u yoktur; bu
            fonksiyon kipi **yeniden yorumlamaz**, yalnız
            :func:`build_runner_arguments`'a olduğu gibi aktarır — kipin argv'ye
            nasıl çevrildiği tek bir yerde durur.
        limits: Timeout, stdout ve raw bütçesi.
        observer: Süreç çalışırken **pipe dışında** bir sınırı uygulayan isteğe
            bağlı yaşam döngüsü gözlemcisi (örneğin Job kirasını yenileyen
            :class:`~app.services.execution.lease.PlaybookLeaseObserver`). Tür
            bilinçli olarak generic :class:`BoundedProcessObserver`'dır: bu
            katman veritabanı, session ve Job durumu bilmez. Verilirse iç raw
            bütçe gözlemcisiyle **birlikte** çalışır; raw sınırı hiçbir koşulda
            devre dışı kalmaz. Varsayılan ``None`` mevcut davranışı birebir
            korur.

    Returns:
        Ham :class:`RunnerProcessResult`.

    Raises:
        RunnerProcessError: Kimlik/çalışma alanı tutarsızsa, project ile
            inventory aynı dondurulmuş workspace'e bağlı değilse, playbook yolu
            güvenli değilse, ``mode`` ``ExecutionMode.CHECK``/``NORMAL``
            dışında bir değerse (``runner_mode_invalid``, raw alanı ve child
            hiç oluşmadan), raw alanı kurulamazsa ya da süreç hiç
            başlatılamazsa. Sebep sabit bir koddur; path yazılmaz.
    """
    _require_canonical_job_id(job_id)
    if runner_environment.run_dir.name != job_id:
        raise _unavailable("run_dir_job_id_mismatch")

    segments = _require_safe_playbook_segments(playbook_path)
    # Yerleşim **süreç başlamadan** doğrulanır ve descriptor'lar doğrulama biter
    # bitmez kapanır. Runner argv'deki yolları yeniden açacağından bu kontrol
    # trusted frozen workspace sözleşmesine dayanır; hostile-writer TOCTOU
    # koruması değildir (modül docstring'i).
    with _bind_frozen_workspace(frozen_project_root, frozen_inventory_path) as (
        binding,
        project_fd,
    ):
        _require_frozen_playbook(project_fd, segments)

    arguments = build_runner_arguments(
        command=command,
        run_dir=runner_environment.run_dir,
        frozen_project_root=binding.project_root,
        frozen_inventory_path=binding.inventory_path,
        raw_dir=runner_environment.run_dir / RAW_DIRNAME,
        job_id=job_id,
        playbook_path=playbook_path,
        mode=mode,
    )

    started_at = datetime.now(UTC)
    with (
        _open_run_directory(runner_environment.run_dir) as run_fd,
        # Raw alanı bu bağlamın **kendi** ürünüdür ve çıkışta — başarı, rc
        # hatası, timeout, sınır aşımı ve başlatma arızası dâhil her yolda —
        # yine bu bağlam tarafından silinir.
        _open_raw_directory(run_fd) as raw_fd,
    ):
        raw_observer = _RawBudgetObserver(raw_fd, limits.max_raw_bytes)
        # Dışarıdan bir gözlemci geldiğinde raw bütçesi **yerini ona bırakmaz**:
        # ikisi bileşik bir gözlemcide birlikte çalışır. Gözlemci verilmediğinde
        # zincir hiç kurulmaz; gözlemcisiz çağrının yolu değişmez.
        process_observer: BoundedProcessObserver = (
            raw_observer if observer is None else CompositeProcessObserver(raw_observer, observer)
        )
        try:
            outcome = run_bounded_process(
                arguments,
                work_dir=runner_environment.run_dir,
                environment=runner_environment.environment,
                limits=ProcessLimits(
                    timeout_seconds=limits.timeout_seconds,
                    max_output_bytes=limits.max_stdout_bytes,
                ),
                observer=process_observer,
            )
        except ProcessLaunchError as exc:
            # `ProcessLaunchError` mesajı işletim sisteminin hata metnidir ve
            # komut yolunu taşır; **aktarılmaz**.
            raise _unavailable("runner_launch_failed") from exc

    return RunnerProcessResult(
        return_code=outcome.return_code,
        stdout_text=outcome.stdout_text,
        stderr_text=outcome.stderr_text,
        timed_out=outcome.timed_out,
        oversized_stream=outcome.oversized_stream,
        raw_limit_exceeded=raw_observer.limit_exceeded,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


# --- Raw bütçesi -------------------------------------------------------------


class _BudgetExceededError(Exception):
    """Raw ağacının yapısal sınırı aşıldı (girdi sayısı veya derinlik)."""


class _RawBudgetObserver:
    """Raw artifact bütçesini **süreç çalışırken** ölçen gözlemci.

    Ölçüm ayrı bir thread'de yapılır ve sınır aşıldığı anda supervisor'dan
    sonlandırma **talep edilir**; sinyal gönderme ve reap işleri gözlemcide
    değil, tek sahibinde kalır (:class:`ProcessSupervisor`).

    Ağacın kendisi de sınırlıdır: girdi sayısı veya derinlik tavanı aşılırsa
    ölçüm sonsuza kadar gezmez, bunu bütçe aşımı sayar ve süreci durdurur.

    **Ölçülemeyen bir ağaç, bütçesi aşılmamış bir ağaç değildir.** Raw alanı bu
    modülün kendi ürünüdür ve süreç bitmeden silinmez; dolayısıyla kökün veya
    bir alt dizinin okunamaması normal bir yarış değil, ölçümün kaybıdır. Böyle
    bir durumda sınır **ihlal edilmiş sayılır** ve süreç durdurulur: aksi hâlde
    dizin izni değiştirilerek ya da ölçümü düşüren bir yapı kurularak bütçe
    tamamen devre dışı bırakılabilirdi. Tolere edilen tek yarış, ölçüm sırasında
    **tek bir girdinin** kaybolmasıdır.
    """

    def __init__(self, raw_fd: int, max_bytes: int) -> None:
        self._raw_fd = raw_fd
        self._max_bytes = max_bytes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._request_termination: Callable[[], None] | None = None
        self.limit_exceeded = False

    def start(self, request_termination: Callable[[], None]) -> None:
        """Ölçüm thread'ini başlatır."""
        self._request_termination = request_termination
        thread = threading.Thread(target=self._run, daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Ölçümü durdurur ve **son bir kez** ölçer.

        Son ölçüm gereklidir: süreç, son periyodik ölçümden sonra ve
        sonlanmadan önce de yazabilir. Bu, çalışma anındaki ölçümün yerine
        geçmez; onun üstüne eklenir.

        Ölçüm thread'i verilen süre içinde bitmezse sonuç fail-closed sayılır:
        tamamlanamayan bir ölçüm, bütçeye uyulduğunun kanıtı değildir.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=OBSERVER_JOIN_SECONDS)
            if thread.is_alive():
                self.limit_exceeded = True
        if self.limit_exceeded:
            return
        try:
            self.limit_exceeded = self._exceeds_budget()
        except BaseException:
            # Son ölçüm de yapılamadıysa sonuç fail-closed'dır. Hata
            # yükseltilmez: yükseltilseydi tamamlanmış bir çalıştırmanın sonucu
            # bir ölçüm arızası yüzünden bütünüyle kaybolurdu ve arıza zaten
            # ``limit_exceeded`` ile bildiriliyor.
            self.limit_exceeded = True

    def _run(self) -> None:
        try:
            while True:
                if self._exceeds_budget():
                    self._demand_termination()
                    return
                if self._stop.wait(RAW_POLL_SECONDS):
                    return
        except BaseException:
            # Thread'in sessizce ölmesi süreci **sınırsız** bırakırdı: gözlemci
            # yaşamadığı sürece kimse raw alanını ölçmez. Beklenmeyen arıza da
            # bu yüzden sonlandırma talebine çevrilir. Hata yeniden
            # yükseltilmez; taşıdığı metin (path, işletim sistemi mesajı)
            # thread excepthook'u üzerinden stderr'e yazılırdı ve arıza zaten
            # ``limit_exceeded`` ile sonuca taşınıyor.
            self._demand_termination()

    def _demand_termination(self) -> None:
        """Sınır ihlalini kaydeder ve supervisor'dan sonlandırma ister."""
        self.limit_exceeded = True
        terminate = self._request_termination
        if terminate is not None:
            terminate()

    def _exceeds_budget(self) -> bool:
        try:
            return _measure_tree(self._raw_fd, depth=0, budget=_EntryBudget()) > self._max_bytes
        except _BudgetExceededError:
            return True
        except OSError:
            # Ölçüm yapılamıyorsa bütçe **aşılmış sayılır** (sınıf docstring'i):
            # okunamayan bir kök veya alt dizin, ölçümün kendisini devre dışı
            # bırakmanın en kolay yoludur.
            return True


class _EntryBudget:
    """Gezilen girdi sayısını sınırlar."""

    def __init__(self) -> None:
        self.remaining = MAX_RAW_ENTRIES

    def consume(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise _BudgetExceededError


def _measure_tree(dir_fd: int, *, depth: int, budget: _EntryBudget) -> int:
    """Descriptor'a göre ağacın normal dosya baytlarını toplar.

    Symlink **izlenmez** ve boyutu sayılmaz: izlenseydi ölçüm ve dolayısıyla
    bütçe, ağacın dışındaki bir dosyaya bağlanabilirdi.
    """
    if depth > MAX_RAW_DEPTH:
        raise _BudgetExceededError
    total = 0
    for name in os.listdir(dir_fd):
        budget.consume()
        try:
            status = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            # Tolere edilen **tek** yarış: ölçüm sırasında gerçekten kaybolan
            # bir girdi. Diğer bütün `OSError`'lar yukarı taşınır ve çağıran
            # tarafından sınır ihlali sayılır.
            continue
        if stat.S_ISREG(status.st_mode):
            total += status.st_size
        elif stat.S_ISDIR(status.st_mode):
            try:
                child_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd
                )
            except FileNotFoundError:
                continue
            # `PermissionError` ve diğer arızalar **sessizce atlanmaz**: bir alt
            # dizinin ölçülememesi, altındaki baytların bütçeden düşmesi
            # demekti; bütçe böylece tek bir `chmod` ile devre dışı kalırdı.
            try:
                total += _measure_tree(child_fd, depth=depth + 1, budget=budget)
            finally:
                os.close(child_fd)
    return total


# --- Çalışma alanı -----------------------------------------------------------


@contextlib.contextmanager
def _open_run_directory(run_dir: Path) -> Iterator[int]:
    """Job çalışma dizinini doğrulayarak açar.

    Dizin R1-V3C1A tarafından 0700 olarak açılmıştır; burada oluşturulmaz,
    yalnız **hâlâ** beklenen nesne olduğu doğrulanır.
    """
    try:
        run_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _unavailable("run_dir_unavailable") from exc
    try:
        status = os.fstat(run_fd)
        if not stat.S_ISDIR(status.st_mode):
            raise _unavailable("run_dir_not_a_directory")
        if stat.S_IMODE(status.st_mode) != DIRECTORY_MODE:
            raise _unavailable("run_dir_not_private")
        yield run_fd
    finally:
        os.close(run_fd)


@contextlib.contextmanager
def _open_raw_directory(run_fd: int) -> Iterator[int]:
    """Raw alanını Job dizinine göre **yeni** olarak açar ve çıkışta siler.

    ``exist_ok`` yoktur: aynı adda duran bir girdi — dizin, symlink, ne olursa
    olsun — hata üretir. Önceki bir çalıştırmadan kalan raw içerik yeni bir
    ölçüme karışamaz.

    Silme sorumluluğu bilinçli olarak buradadır: alanı **yalnız kendi
    oluşturduğu** bağlam kaldırır. Dışarıda bir ``finally`` ile silinseydi,
    "zaten vardı" hatasıyla düşen bir çağrı da kendisinin olmayan bir dizini
    silerdi.
    """
    try:
        os.mkdir(RAW_DIRNAME, DIRECTORY_MODE, dir_fd=run_fd)
    except FileExistsError as exc:
        raise _unavailable("raw_dir_already_exists") from exc
    except OSError as exc:
        raise _unavailable("raw_dir_unavailable") from exc

    try:
        raw_fd = os.open(RAW_DIRNAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=run_fd)
    except OSError as exc:
        raise _unavailable("raw_dir_unavailable") from exc
    try:
        # `mkdir` mode'u umask ile maskelenir; izin açıkça sabitlenip
        # ardından doğrulanır.
        os.fchmod(raw_fd, DIRECTORY_MODE)
        status = os.fstat(raw_fd)
        if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) != DIRECTORY_MODE:
            raise _unavailable("raw_dir_not_private")
        yield raw_fd
    except OSError as exc:
        raise _unavailable("raw_dir_unavailable") from exc
    finally:
        os.close(raw_fd)
        _remove_raw_directory(run_fd)


def _remove_raw_directory(run_fd: int) -> None:
    """Raw alanını descriptor'a göre siler.

    Yalnız Job dizininin ``raw`` çocuğuna dokunulur: geniş ``rmtree``, glob ve
    çözülmemiş path kullanılmaz. Symlink **izlenmez**, ``unlink`` edilir.

    En iyi çaba ile çalışır: silinemeyen bir kalıntı gerçek çalıştırma sonucunu
    bastırmaz. Kalıntının toplanması bir sonraki dilimdeki janitor'ın işidir;
    bu katman SIGKILL/host çökmesi sonrası temizlik **garantisi vermez**.
    """
    with contextlib.suppress(OSError, _BudgetExceededError):
        _remove_entry(run_fd, RAW_DIRNAME, depth=0, budget=_EntryBudget())


def _remove_entry(parent_fd: int, name: str, *, depth: int, budget: _EntryBudget) -> None:
    """Tek bir girdiyi (gerekirse ağacıyla) descriptor'a göre siler."""
    budget.consume()
    if depth > MAX_RAW_DEPTH:
        raise _BudgetExceededError
    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(status.st_mode):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent_fd)
        return

    try:
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError:
        return
    try:
        for child in os.listdir(child_fd):
            _remove_entry(child_fd, child, depth=depth + 1, budget=budget)
    finally:
        os.close(child_fd)
    with contextlib.suppress(OSError):
        os.rmdir(name, dir_fd=parent_fd)


# --- Path doğrulaması --------------------------------------------------------


@dataclass(frozen=True)
class _WorkspaceBinding:
    """Aynı dondurulmuş workspace yerleşiminde doğrulanmış yollar.

    Alanlar çağırandan gelen metinler değil, doğrulanmış workspace dizininden
    sabit adlarla yeniden kurulmuş yollardır: argv'ye giren şey budur. Runner
    bu yolları daha sonra yeniden açtığı için nesne kimliği çalıştırma boyunca
    descriptor ile sabitlenmiş değildir.
    """

    workspace_dir: Path
    project_root: Path
    inventory_path: Path


@contextlib.contextmanager
def _bind_frozen_workspace(
    frozen_project_root: Path, frozen_inventory_path: Path
) -> Iterator[tuple[_WorkspaceBinding, int]]:
    """Project ve inventory'nin **aynı** dondurulmuş workspace'e bağlı olduğunu kanıtlar.

    Önce düzen metinsel olarak doğrulanır (adlar, ortak parent, canonical UUID4,
    alias'sız absolute yol), sonra aynı düzen descriptor'larla yeniden kurulur:
    workspace ``O_NOFOLLOW`` ile açılır, çocukları yine ``O_NOFOLLOW`` ile ve
    isimdeki girdiyle aynı nesne oldukları doğrulanarak açılır. Metin
    doğrulaması tek başına yeterli sayılmaz; ikisi birlikte uygulanır.

    Yields:
        Doğrulanmış :class:`_WorkspaceBinding` ve açık project dizini
        descriptor'ı. Playbook yerleşim kontrolü bu descriptor'a göre yapılır;
        descriptor doğrulama sonunda kapanır ve runner yolu daha sonra yeniden
        açar. Bu nedenle hostile concurrent writer'a karşı TOCTOU garantisi
        verilmez.

    Raises:
        RunnerProcessError: Düzenin herhangi bir parçası doğrulanamazsa. Sebep
            **tek** ve sabittir; hangi parçanın uymadığı bildirilmez, path
            yazılmaz.
    """
    workspace_dir = _require_workspace_layout(frozen_project_root, frozen_inventory_path)
    try:
        workspace_fd = os.open(workspace_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _binding_invalid() from exc
    try:
        # Manifest yalnız **düzenin parçası** olarak aranır: okunmaz ve digest'i
        # doğrulanmaz (modül docstring'i).
        _require_regular_file(workspace_fd, MANIFEST_FILENAME)
        with _bound_child_directory(workspace_fd, INVENTORY_DIRNAME) as inventory_fd:
            _require_regular_file(inventory_fd, INVENTORY_FILENAME)
        with _bound_child_directory(workspace_fd, PROJECT_DIRNAME) as project_fd:
            yield (
                _WorkspaceBinding(
                    workspace_dir=workspace_dir,
                    project_root=workspace_dir / PROJECT_DIRNAME,
                    inventory_path=workspace_dir / INVENTORY_DIRNAME / INVENTORY_FILENAME,
                ),
                project_fd,
            )
    finally:
        os.close(workspace_fd)


def _require_workspace_layout(frozen_project_root: Path, frozen_inventory_path: Path) -> Path:
    """Düzeni metinsel olarak doğrular ve ortak workspace dizinini döndürür.

    Yalnız son iki adın doğru olması yetmez (``/tmp/x/inventory/hosts.yml`` da
    o testi geçerdi): workspace dizininin adı uygulamanın ürettiği canonical
    UUID4 olmalı ve iki yol **aynı** workspace dizininin altında bulunmalıdır.
    """
    for path in (frozen_project_root, frozen_inventory_path):
        if not path.is_absolute():
            raise _binding_invalid()
        # `..` ile kurulmuş bir alias, aynı dizini iki farklı workspace gibi
        # gösterebilir; `.` de karşılaştırılan metni değiştirir.
        if any(part in _REJECTED_SEGMENTS for part in path.parts):
            raise _binding_invalid()

    if frozen_project_root.name != PROJECT_DIRNAME:
        raise _binding_invalid()
    if frozen_inventory_path.name != INVENTORY_FILENAME:
        raise _binding_invalid()
    if frozen_inventory_path.parent.name != INVENTORY_DIRNAME:
        raise _binding_invalid()

    workspace_dir = frozen_project_root.parent
    if frozen_inventory_path.parent.parent != workspace_dir:
        raise _binding_invalid()
    if not _is_canonical_uuid4(workspace_dir.name):
        raise _binding_invalid()
    return workspace_dir


@contextlib.contextmanager
def _bound_child_directory(parent_fd: int, name: str) -> Iterator[int]:
    """Alt dizini ``O_NOFOLLOW`` ile açar ve isimdeki girdiyle aynı nesne olduğunu doğrular."""
    try:
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise _binding_invalid() from exc
    try:
        try:
            opened = os.fstat(child_fd)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise _binding_invalid() from exc
        if not stat.S_ISDIR(opened.st_mode):
            raise _binding_invalid()
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise _binding_invalid()
        yield child_fd
    finally:
        os.close(child_fd)


def _require_regular_file(dir_fd: int, name: str) -> None:
    """Girdinin, descriptor'a göre açılabilen **normal** bir dosya olduğunu kanıtlar.

    ``O_NOFOLLOW`` symlink'i ``ELOOP`` ile düşürür, ``O_NONBLOCK`` yerine
    konmuş bir FIFO'nun açmayı bloke etmesini engeller ve ``fstat`` açılan
    nesnenin gerçekten normal dosya olduğunu doğrular.
    """
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        raise _binding_invalid() from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _binding_invalid()
    finally:
        os.close(descriptor)


def _require_safe_playbook_segments(playbook_path: str) -> list[str]:
    """Playbook yolunun metinsel olarak güvenli olduğunu doğrular ve parçalarını döndürür."""
    if not playbook_path:
        raise _unavailable("playbook_path_empty")
    if playbook_path.startswith("/") or Path(playbook_path).is_absolute():
        raise _unavailable("playbook_path_absolute")

    segments = playbook_path.split("/")
    for segment in segments:
        if segment in _REJECTED_SEGMENTS:
            raise _unavailable("playbook_path_unsafe_segment")
        # Ters bölü POSIX'te geçerli bir dosya adı karakteridir ama meşru bir
        # playbook adında bulunmaz; ayırıcı sanılan bir parçayı baştan reddetmek
        # doğrulama ile çalıştırmanın aynı yolu görmesini garanti eder.
        if "\\" in segment or "\x00" in segment:
            raise _unavailable("playbook_path_unsafe_segment")
        # `-p <değer>`: `-` ile başlayan bir değer argparse tarafından seçenek
        # sanılırdı.
        if segment.startswith("-"):
            raise _unavailable("playbook_path_unsafe_segment")
    return segments


def _require_frozen_playbook(project_fd: int, segments: Sequence[str]) -> None:
    """Playbook'un dondurulmuş project içinde normal bir dosya olduğunu kanıtlar.

    Kontrol **süreç başlamadan önce** ve bağlanmış project descriptor'ına göre
    yapılır: her ara parça ``O_NOFOLLOW`` ile açılır, son parça ``lstat`` ile
    normal dosya olarak doğrulanır. Böylece ne ``..`` ile ağacın dışına
    çıkılabilir ne de bir symlink dondurulmuş ağacın dışındaki bir dosyayı
    runner'a okutabilir.

    Metin doğrulaması tek başına yeterli sayılmaz; ikisi birlikte uygulanır
    (bkz. :func:`_require_safe_playbook_segments`).
    """
    open_fds: list[int] = []
    current_fd = project_fd
    try:
        for directory in segments[:-1]:
            try:
                current_fd = os.open(
                    directory,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise _unavailable("playbook_not_regular_file") from exc
            open_fds.append(current_fd)
        try:
            status = os.stat(segments[-1], dir_fd=current_fd, follow_symlinks=False)
        except OSError as exc:
            raise _unavailable("playbook_not_regular_file") from exc
        if not stat.S_ISREG(status.st_mode):
            raise _unavailable("playbook_not_regular_file")
    finally:
        # Yalnız **burada açılanlar** kapatılır; project descriptor'ının sahibi
        # bağ bağlamıdır.
        for descriptor in open_fds:
            os.close(descriptor)


# --- Ortak -------------------------------------------------------------------


def _require_canonical_job_id(job_id: str) -> None:
    """``--ident`` yalnız uygulamanın ürettiği canonical UUID4 olabilir."""
    if not _is_canonical_uuid4(job_id):
        raise _unavailable("job_id_not_canonical")


def _is_canonical_uuid4(value: str) -> bool:
    """Metin, uygulamanın ürettiği **canonical** UUID4 gösterimi mi.

    Yalnız ayrıştırılabilir olması yetmez: büyük harfli, süslü parantezli veya
    tiresiz bir gösterim aynı kimliği farklı bir dizin adıyla temsil ederdi.
    """
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _binding_invalid() -> RunnerProcessError:
    """Dondurulmuş workspace bağı doğrulanamadı.

    Sebep bilinçli olarak **tek** koddur: hangi parçanın uymadığını bildirmek,
    hata mesajını dosya sistemi düzenini dışarıdan sorgulayan bir sonda hâline
    getirirdi.
    """
    return _unavailable("frozen_workspace_binding_invalid")


def _unavailable(reason: str) -> RunnerProcessError:
    """Ortak, sızdırmayan altyapı hatası."""
    return RunnerProcessError("Runner süreci çalıştırılamadı.", details={"reason": reason})
