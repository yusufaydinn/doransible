"""Ansible alt süreçleri için ortak sınırlı çalıştırma katmanı.

Bu modül T-202'de `inventory parser` için yazılan sınırlandırma makinesinin
domain'den bağımsız hâlidir (ADR-017). T-204'te ping akışı da aynı sınırlara
ihtiyaç duyduğu için kod **kopyalanmadı, çıkarıldı**: iki yerde iki farklı
environment allowlist'i veya iki farklı çıktı sınırı oluşması, ADR-015'in
bizzat cezalandırdığı hata sınıfıdır ve burada güvenlik kritik koddur.

Sağlanan sınırlar:

- Komut **argüman listesidir**; hiçbir aşamada shell string'e çevrilmez
  (GUVENLIK.md bölüm 5, subprocess güvenlik sözleşmesi).
- ``stdin`` kapalıdır: parola soran bir alt süreç askıda kalmaz.
- Üst sürecin environment'ı **aktarılmaz**; yalnızca sayılı değişken geçer.
- stdout ve stderr pipe'lardan okunur ve **diske hiç yazılmaz**. Her akışın
  kendi üst sınırı vardır ve sınır aşıldığı **anda** süreç sonlandırılır.
- Timeout **ayrı** bir korumadır ve boyut sınırının yerine geçmez.

Bu modül domain hatası üretmez. Arızaları nötr :class:`ProcessLaunchError` ve
:class:`ProcessOutcome` alanlarıyla bildirir; HTTP karşılığına çeviren taraf
çağıran domain servisidir.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from app.services.security.redaction import redact_text

# stderr için **gerçek** üst sınır. Okuma sırasında kırpma değil, süreç
# çalışırken uygulanan bir sınırdır: aşıldığı anda süreç sonlandırılır.
# Ansible'ın hata metinleri kilobaytlar mertebesindedir; 64 KiB fazlasıyla
# yeterlidir ve bunu aşan bir süreç yanlış davranıyordur.
MAX_STDERR_BYTES = 65_536

# Pipe'lardan tek seferde okunacak bayt.
CHUNK_BYTES = 65_536

# `terminate()` sonrası `kill()`'e geçmeden önce tanınan süre.
TERMINATE_GRACE_SECONDS = 5.0

# Alt sürece aktarılan environment değişkenleri. Tam ortam kopyalanmaz:
# kullanıcının `ANSIBLE_*` ayarları, proxy değişkenleri ve secret'ları
# alt sürece sızmamalıdır. `HOME`/`USERPROFILE` bilinçli olarak aktarılmaz;
# böylece `~/.ansible.cfg` okunmaz.
#
# DİKKAT: `HOME`'u aktarmamak SSH kullanıcı yapılandırmasını izole ettiğinin
# kanıtı **değildir** — OpenSSH kullanıcı dizinini passwd veritabanından da
# bulabilir. SSH izolasyonu ayrıca ve açıkça kurulur (T-204B).
PRESERVED_ENV_NAMES = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "LANG",
    "LC_ALL",
)

# Mutlak yol görünümlü parçalar (Windows sürücü harfi veya POSIX kökü).
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s'\"]+")
_WHITESPACE = re.compile(r"\s+")

# Alt sürecin **kendisinin** çöktüğünü gösteren imza. Bu, kullanıcının
# dosyasıyla ilgili bir sorun değildir: kurulum bozuk, yorumlayıcı uyumsuz veya
# platform desteklenmiyordur.
PYTHON_TRACEBACK = re.compile(r"Traceback \(most recent call last\)")

# Traceback çerçeveleri ve yorumlayıcı iç yolları kullanıcıya hiç gösterilmez.
_TRACEBACK_FRAME = re.compile(
    r'File "[^"]*", line \d+(?:, in \S+)?|<frozen [^>]+>|\^{2,}',
)


class ProcessLaunchError(RuntimeError):
    """Alt süreç hiç başlatılamadı (dosya yok, izin yok, geçersiz ikili).

    Bilinçli olarak domain hatası **değildir**: hangi HTTP kodunun uygun
    olduğuna çağıran servis karar verir.
    """


@dataclass(frozen=True)
class ProcessLimits:
    """Alt sürece uygulanan sınırlar.

    ``max_output_bytes`` **gerçek zamanlı** bir üst sınırdır: stdout okundukça
    sayılır ve sınır aşıldığı anda alt süreç sonlandırılır. Sürecin doğal
    olarak bitmesi beklenmez.

    Çıktı hiçbir aşamada diske yazılmaz; pipe'lardan sınırlı biçimde belleğe
    alınır. Dolayısıyla sınır aynı anda hem bellek hem disk sınırıdır.

    ``timeout_seconds`` ayrı bir korumadır ve **boyut sınırının yerine
    geçmez**: hiç çıktı üretmeden asılı kalan bir süreci sonlandırır.
    """

    timeout_seconds: float = 30.0
    max_output_bytes: int = 5_000_000


@dataclass(frozen=True)
class ProcessOutcome:
    """Sınırlar dâhilinde toplanmış alt süreç sonucu."""

    return_code: int
    stdout_text: str
    stderr_text: str
    timed_out: bool
    oversized_stream: str | None


class BoundedProcessObserver(Protocol):
    """Süreç çalışırken **pipe dışında** bir sınırı uygulayan gözlemci.

    Bu modül yalnız stdout/stderr baytlarını sayabilir; bazı sınırlar
    (örneğin runner'ın diske yazdığı raw artifact bütçesi) süreç çalışırken
    başka bir yüzeyden ölçülmelidir. Gözlemci o ölçümü yapar ve sınırı aştığını
    gördüğü anda kendisine verilen ``request_termination`` çağrısını yapar;
    sinyal gönderme, grup sonlandırma ve reap işleri **yine tek sahibinde**
    (:class:`ProcessSupervisor`) kalır.

    Varsayılan olarak gözlemci **yoktur**: gözlemcisiz çağrılar (ping, inventory
    parser) bu protokolden hiç etkilenmez.
    """

    def start(self, request_termination: Callable[[], None]) -> None:
        """Ölçümü başlatır. Süreç başlatıldıktan hemen sonra çağrılır."""
        ...

    def stop(self) -> None:
        """Ölçümü durdurur. Süreç sonlandıktan sonra **her yolda** çağrılır."""
        ...


class CompositeProcessObserver:
    """Birden çok gözlemciyi **tek** bir gözlemci gibi çalıştırır.

    Bir çalıştırmanın birden fazla, birbirinden bağımsız sınırı olabilir:
    runner'ın diske yazdığı raw bütçesi süreç katmanının kendi iç ölçümüdür,
    Job kirası ise dışarıdan verilen bir yaşam döngüsü gözlemidir. İkisinden
    birini seçmek zorunda kalmak, diğerinin sınırının o çalıştırmada hiç
    uygulanmaması demek olurdu.

    :func:`collect_bounded_output` yine **tek** bir gözlemci görür; bileşik
    olduğu bilgisi ona sızmaz. Sözleşme üç noktada sıkıdır:

    - *Kısmi başlatma arızası.* Bir gözlemci başlatılamazsa o ana kadar
      başlatılmış olanlar **ters sırada** durdurulur ve asıl hata yeniden
      yükseltilir. Yarım başlatılmış bir zincir, kimsenin durdurmadığı bir
      ölçüm thread'i bırakırdı.
    - *Durdurmada arıza.* Bir gözlemcinin ``stop``'u hata verse de kalanlar
      yine durdurulur; hatalardan **ilki** en sonda yeniden yükseltilir.
      Yükseltilen hata çağıranın alt süreci sahipsiz bırakmamasını sağlar:
      :func:`collect_bounded_output` onu görünce süreci sonlandırır ve reap
      eder.
    - *Sahiplik.* Bileşik gözlemci de sinyal göndermez ve süreç beklemez;
      yalnız aldığı ``request_termination`` çağrısını olduğu gibi aktarır.
    """

    def __init__(self, *observers: BoundedProcessObserver) -> None:
        self._observers = observers
        self._started: list[BoundedProcessObserver] = []

    def start(self, request_termination: Callable[[], None]) -> None:
        """Gözlemcileri sırayla başlatır; arızada başlamış olanları geri alır."""
        for observer in self._observers:
            try:
                observer.start(request_termination)
            except BaseException:
                # Geri alma sırasındaki ikincil bir hata, başlatmayı düşüren
                # **asıl** hatayı gölgelememelidir: çağıranın gördüğü hata,
                # sürecin neden başlatılamadığını söyleyen hatadır.
                with contextlib.suppress(BaseException):
                    self._stop_started()
                raise
            self._started.append(observer)

    def stop(self) -> None:
        """Başlatılmış gözlemcileri ters sırada durdurur.

        Çağrı idempotenttir: başlatılmamış bir gözlemci durdurulmaz ve ikinci
        bir ``stop`` hiçbir şey yapmaz.
        """
        self._stop_started()

    def _stop_started(self) -> None:
        """Hepsini durdurur, sonra ilk hatayı yükseltir."""
        first_error: BaseException | None = None
        while self._started:
            observer = self._started.pop()
            try:
                observer.stop()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def build_base_environment(work_dir: Path) -> dict[str, str]:
    """Ansible alt süreçleri için daraltılmış ortak environment üretir.

    - Yalnızca sürecin çalışması için gereken değişkenler aktarılır.
    - ``ANSIBLE_CONFIG`` çalışma dizinindeki **boş** dosyaya sabitlenir; böylece
      kullanıcının ve repository'nin ``ansible.cfg`` dosyaları okunmaz.
    - ``ANSIBLE_HOME`` ve ``ANSIBLE_LOCAL_TEMP`` geçici dizine bağlanır; süreç
      kullanıcının ev dizinine hiçbir şey yazmaz.

    Inventory eklentisi seçimi gibi domain'e özgü değişkenleri çağıran taraf
    ekler; burada varsayılan bir eklenti kümesi **bilinçli olarak** yoktur.
    """
    environment = {name: os.environ[name] for name in PRESERVED_ENV_NAMES if name in os.environ}
    environment.update(
        {
            "ANSIBLE_CONFIG": str(work_dir / "ansible.cfg"),
            "ANSIBLE_HOME": str(work_dir / "ansible-home"),
            "ANSIBLE_LOCAL_TEMP": str(work_dir / "ansible-tmp"),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_FORCE_COLOR": "0",
            "ANSIBLE_DEPRECATION_WARNINGS": "False",
            "ANSIBLE_RETRY_FILES_ENABLED": "False",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONWARNINGS": "ignore",
        }
    )
    return environment


def write_empty_ansible_config(work_dir: Path) -> Path:
    """Çalışma dizinine boş bir ``ansible.cfg`` yazar ve yolunu döndürür.

    ``ANSIBLE_CONFIG`` bu dosyaya sabitlendiği için Ansible'ın cwd/ev dizini
    tabanlı yapılandırma keşfi devre dışı kalır.
    """
    config_path = work_dir / "ansible.cfg"
    config_path.write_text("", encoding="utf-8")
    return config_path


def run_bounded_process(
    arguments: Sequence[str],
    *,
    work_dir: Path,
    environment: dict[str, str],
    limits: ProcessLimits,
    observer: BoundedProcessObserver | None = None,
) -> ProcessOutcome:
    """Bir alt süreci sınırlar dâhilinde çalıştırır.

    Args:
        arguments: Çalıştırılacak **argüman listesi**; shell kullanılmaz.
        work_dir: Sürecin çalışma dizini (izole geçici dizin olmalıdır).
        environment: Daraltılmış environment; üst süreçten miras alınmaz.
        limits: Timeout ve stdout boyutu sınırları.
        observer: Pipe dışı bir sınırı süreç çalışırken uygulayan isteğe bağlı
            gözlemci (:class:`BoundedProcessObserver`). Varsayılan ``None``:
            gözlemcisiz çağrıların davranışı değişmez.

    Returns:
        Toplanmış :class:`ProcessOutcome`.

    Raises:
        ProcessLaunchError: Süreç hiç başlatılamadıysa.
    """
    started_at = time.monotonic()
    try:
        process = subprocess.Popen(  # noqa: S603 - argüman listesi, shell=False
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=work_dir,
            env=environment,
            shell=False,
            # Ansible/SSH bir süreç ağacı oluşturabilir. POSIX'te yeni session,
            # bütün ağacı güvenli biçimde tek process group olarak sonlandırır.
            start_new_session=os.name == "posix",
            # Tamponsuz ham akış: `read()` tek syscall yapar ve hazır olan
            # baytı döndürür, böylece sınır anında ölçülebilir.
            bufsize=0,
        )
    except OSError as exc:
        # FileNotFoundError, PermissionError, WinError 193 (geçersiz ikili) ...
        raise ProcessLaunchError(str(exc)) from exc

    return collect_bounded_output(process, limits, started_at=started_at, observer=observer)


class ProcessSupervisor:
    """Süreç bekleme, grup sonlandırma ve leader reap işlemlerinin tek sahibi."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        started_at: float | None = None,
    ) -> None:
        self.process = process
        self._started_at = started_at if started_at is not None else time.monotonic()
        self._owner_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._termination_requested = threading.Event()
        self._reaped = False
        self._process_group: int | None = None
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                process_group = os.getpgid(process.pid)
                if process_group == process.pid and process_group != os.getpgrp():
                    self._process_group = process_group

    def request_termination(self) -> None:
        """Reader thread'lerinden sinyal/reap yapmadan termination talep eder."""
        with self._state_lock:
            if not self._reaped:
                self._termination_requested.set()

    def wait(self, timeout_seconds: float) -> bool:
        """Tek sahip olarak completion/termination'ı koordine eder.

        Returns:
            Süreç timeout nedeniyle sonlandırıldıysa ``True``.
        """
        with self._owner_lock:
            if self._reaped:
                return False
            deadline = self._started_at + timeout_seconds
            while True:
                if self._termination_requested.is_set():
                    self._terminate_owned()
                    return False

                if os.name == "posix":
                    if self._leader_exited_without_reaping():
                        process_group = self._process_group
                        if process_group is not None and not self._finalize_group_owned(
                            process_group
                        ):
                            # Leader zombie olarak unreaped kalır; yaşayan
                            # descendant doğal bitiş, reader talebi veya ilk
                            # genel deadline'a kadar izlenmeye devam eder.
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                self.request_termination()
                                self._terminate_owned()
                                return True
                            self._termination_requested.wait(min(0.01, remaining))
                            continue

                        # Grup gerçekten finalize edildikten sonra reader
                        # talebiyle atomik yarış çözülür ve ancak o zaman PGID
                        # sinyal için geçersizleştirilip leader reap edilir.
                        with self._state_lock:
                            if self._reaped:
                                return False
                            terminate = self._termination_requested.is_set()
                            if not terminate:
                                self._process_group = None
                        if terminate:
                            self._terminate_owned()
                        else:
                            self._reap_owned()
                        return False
                else:
                    # Non-POSIX'te de Popen.wait yalnız coordinator tarafından
                    # çağrılır; kısa dilimler reader talebine cevap verir.
                    try:
                        self.process.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
                    else:
                        self._mark_reaped()
                        return False

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.request_termination()
                    self._terminate_owned()
                    return True
                self._termination_requested.wait(min(0.01, remaining))

    def _finalize_group_owned(self, process_group: int) -> bool:
        """Grubun canlı üyesi kalmadığını SIGSTOP fence ile kesinleştirir."""
        if _process_group_has_live_members(process_group) is not False:
            return False
        try:
            os.killpg(process_group, signal.SIGSTOP)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False

        # İlk tarama ile reap arasında yeni üye doğması ancak taramada kaçan
        # canlı bir üye üzerinden mümkündür. SIGSTOP fence o üyeyi user-space'e
        # dönmeden durdurur; ikinci tarama onu zombie olmayan üye olarak görür.
        state = _process_group_has_live_members(process_group)
        if state is False:
            return True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process_group, signal.SIGCONT)
        return False

    def terminate(self) -> None:
        """Dış çağrıda tek termination dizisini başlatır veya mevcut olana katılır."""
        self.request_termination()
        if not self._owner_lock.acquire(blocking=False):
            return
        try:
            if not self._reaped:
                self._terminate_owned()
        finally:
            self._owner_lock.release()

    def _leader_exited_without_reaping(self) -> bool:
        """POSIX leader durumunu PID/PGID yeniden kullanımına izin vermeden gözler."""
        try:
            status = os.waitid(
                os.P_PID,
                self.process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            # Süreç dışarıda reap edilmişse sayısal PGID artık güvenilir
            # değildir; onu derhal geçersiz kıl ve bir daha sinyal gönderme.
            with self._state_lock:
                self._process_group = None
                self._reaped = True
            return True
        return status is not None

    def _terminate_owned(self) -> None:
        process_group = self._process_group
        if os.name == "posix" and process_group is not None:
            # Leader, son grup sinyali tamamlanana kadar reap edilmez. Session
            # leader PID'si dolayısıyla PGID bu pencere içinde yeniden
            # kullanılamaz.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process_group, signal.SIGTERM)
            deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
            while _process_group_has_live_members(process_group) is not False:
                if time.monotonic() >= deadline:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(process_group, signal.SIGKILL)
                    break
                time.sleep(0.05)
            with self._state_lock:
                self._process_group = None
            self._reap_owned()
            return

        # İzole bir POSIX grubu doğrulanamadıysa veya platform non-POSIX ise
        # yalnız parent üzerinde önceki terminate→kill davranışı korunur.
        with contextlib.suppress(OSError):
            self.process.terminate()
        try:
            self.process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                self.process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                self.process.wait(timeout=TERMINATE_GRACE_SECONDS)
        self._mark_reaped()

    def _reap_owned(self) -> None:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError, ChildProcessError):
            self.process.wait(timeout=TERMINATE_GRACE_SECONDS)
        self._mark_reaped()

    def _mark_reaped(self) -> None:
        with self._state_lock:
            self._process_group = None
            self._reaped = True


def _process_group_has_live_members(process_group: int) -> bool | None:
    """Linux /proc üzerinden zombie olmayan grup üyesi var mı belirler.

    Session leader bilerek reap edilmediği için ``killpg(pgid, 0)`` grup boş
    olsa da leader zombie'sini görür. ``None`` gözlemin güvenilir olmadığını
    belirtir; hiçbir çağıran bunu "grup boş" olarak yorumlamaz.
    """
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    uncertain = False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            state = fields[0]
            member_group = int(fields[2])
        except FileNotFoundError:
            # Listelemeden sonra yok olan süreç artık yeni descendant üretemez.
            continue
        except (OSError, IndexError, ValueError):
            uncertain = True
            continue
        if member_group == process_group and state != "Z":
            return True
    return None if uncertain else False


class _BoundedStreamReader(threading.Thread):
    """Bir pipe'ı üst sınırla okur ve sınır aşılır aşılmaz haber verir.

    Okuma ayrı thread'de yapılır çünkü iki pipe aynı anda dolabilir: tek
    thread'de sırayla okumak, diğer akış işletim sisteminin pipe tamponunu
    doldurduğunda kilitlenme üretirdi.

    Sınır aşıldığında okuma durur ve geri çağrı alt süreci sonlandırır; bu,
    "önce her şeyi al, sonra ölç" yaklaşımının aksine sınırı **gerçek zamanlı**
    kılar.
    """

    def __init__(
        self,
        stream: IO[bytes],
        limit: int,
        on_limit_exceeded: Callable[[], None],
    ) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._on_limit_exceeded = on_limit_exceeded
        self._chunks: list[bytes] = []
        self._size = 0
        self.limit_exceeded = False

    def run(self) -> None:
        """Sınıra ulaşana veya akış kapanana kadar okur."""
        try:
            while True:
                chunk = self._stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                self._chunks.append(chunk)
                self._size += len(chunk)
                if self._size > self._limit:
                    self.limit_exceeded = True
                    self._on_limit_exceeded()
                    break
        except (OSError, ValueError):
            # Süreç sonlandırıldığında pipe kapanabilir; bu bir hata değildir.
            pass

    def text(self) -> str:
        """Toplanan baytları sınıra kırpılmış metin olarak döndürür."""
        return b"".join(self._chunks)[: self._limit].decode("utf-8", errors="replace")


def collect_bounded_output(
    process: subprocess.Popen[bytes],
    limits: ProcessLimits,
    *,
    started_at: float | None = None,
    observer: BoundedProcessObserver | None = None,
) -> ProcessOutcome:
    """Alt sürecin çıktısını sınırlar dâhilinde toplar.

    Hem sınır aşımında hem timeout'ta süreç sonlandırılır; iki durumda da
    sürecin kendiliğinden bitmesi **beklenmez**.

    ``observer`` verilirse süreç başlar başlamaz başlatılır ve süreç sonlandığı
    anda — hangi yoldan sonlanırsa sonlansın — durdurulur. Gözlemci sinyal
    göndermez; yalnız supervisor'ın ``request_termination`` çağrısını yapar.

    Gözlemcinin (veya reader thread'lerinin) **kendisi** arıza verirse alt süreç
    arka planda bırakılmaz: süreç grubuyla birlikte sonlandırılır, reap edilir,
    pipe'lar kapatılır ve hata yeniden yükseltilir. Yutulan bir gözlemci
    hatası, ölçülmediği hâlde çalışmaya devam eden bir süreç bırakırdı.
    """
    stdout_stream = process.stdout
    stderr_stream = process.stderr
    if stdout_stream is None or stderr_stream is None:  # pragma: no cover - PIPE garantisi
        raise ProcessLaunchError("Alt süreç çıktısı okunamadı.")

    supervisor = ProcessSupervisor(process, started_at=started_at)
    stdout_reader = _BoundedStreamReader(
        stdout_stream, limits.max_output_bytes, supervisor.request_termination
    )
    stderr_reader = _BoundedStreamReader(
        stderr_stream, MAX_STDERR_BYTES, supervisor.request_termination
    )
    readers = (stdout_reader, stderr_reader)
    streams = (stdout_stream, stderr_stream)

    try:
        stdout_reader.start()
        stderr_reader.start()
        if observer is not None:
            observer.start(supervisor.request_termination)
        try:
            timed_out = supervisor.wait(limits.timeout_seconds)
        finally:
            if observer is not None:
                observer.stop()
    except BaseException:
        # `BaseException`: `KeyboardInterrupt`/`SystemExit` de bir alt süreci
        # sahipsiz bırakmamalıdır. Temizlik yapılır ama hata **yutulmaz**.
        supervisor.terminate()
        _release_readers(readers, streams)
        raise

    _release_readers(readers, streams)

    oversized: str | None = None
    if stdout_reader.limit_exceeded:
        oversized = "stdout"
    elif stderr_reader.limit_exceeded:
        oversized = "stderr"

    return ProcessOutcome(
        return_code=process.returncode if process.returncode is not None else -1,
        stdout_text=stdout_reader.text(),
        stderr_text=stderr_reader.text(),
        timed_out=timed_out,
        oversized_stream=oversized,
    )


def _release_readers(readers: Sequence[_BoundedStreamReader], streams: Sequence[IO[bytes]]) -> None:
    """Reader thread'lerini sınırlı süreyle bekler ve pipe'ları kapatır.

    Hem normal hem arıza yolunda çalışır: kapatılmayan bir pipe descriptor'ı
    sızdırır, beklenmeyen bir thread ise okuduğu baytları kimsenin görmediği bir
    tampona yazmaya devam ederdi.
    """
    join_deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    for reader in readers:
        # Hiç başlatılamamış bir thread'i `join` etmek `RuntimeError` üretir;
        # burada asıl hatayı gölgelememesi için başlatılmış olanlar beklenir.
        if reader.ident is None:
            continue
        reader.join(timeout=max(0.0, join_deadline - time.monotonic()))
    for stream in streams:
        with contextlib.suppress(OSError):
            stream.close()


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    """Alt süreci önce nazikçe, gerekirse zorla sonlandırır.

    Birden çok thread'den çağrılabilir; zaten bitmiş bir süreç için yapılan
    çağrı zararsızdır.
    """
    ProcessSupervisor(process).terminate()


def contains_python_traceback(text: str) -> bool:
    """Metin, alt sürecin kendisinin çöktüğünü gösteren imzayı taşıyor mu."""
    return PYTHON_TRACEBACK.search(text) is not None


def sanitize_output(raw: str, *, max_length: int) -> str:
    """Alt süreç hata çıktısını kullanıcıya gösterilebilir hâle getirir.

    Ansible'ın hata metinleri mutlak sunucu yollarını, bazen dosya içeriğini ve
    Python çağrı yığını parçalarını tekrarlar. Bu yüzden çıktı ham gösterilmez:

    1. Mutlak yol görünümlü her parça ``<path>`` ile değiştirilir.
    2. Traceback çerçeveleri ve yorumlayıcı iç işaretleri silinir.
    3. Secret biçimleri (private key, Vault, Bearer, ``password=...``)
       maskelenir.
    4. Boşluklar tek satıra indirgenir ve metin kırpılır.

    Maskeleme bilinçli olarak agresiftir: fazladan silinen bir kelime zararsız,
    sızan bir dizin yapısı veya çağrı yığını değildir (GUVENLIK.md bölüm 3).
    """
    text = _ABSOLUTE_PATH.sub("<path>", raw)
    text = _TRACEBACK_FRAME.sub("", text)
    text = redact_text(text)
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > max_length:
        text = text[:max_length].rstrip() + "…"
    return text
