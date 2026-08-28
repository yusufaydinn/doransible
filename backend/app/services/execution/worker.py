"""Varsayılan **kapalı**, tek eşzamanlılıklı arka plan playbook worker'ı (R1-V3C2C).

Bu modül, R1-V3C1C2B2B'den beri var olan tek atımlık
:func:`~app.services.execution.executor.execute_next_playbook_job` çağrısını
tekrarlayan **ilk** şeydir. Yaptığı iş bundan ibarettir: bir döngü, bir kapanış
sinyali ve periyodik bir janitor tetikleyicisi. Burada yeni bir güvenlik kararı,
yeni bir hata kodu, yeni bir Job geçişi, argv üretimi, path çözümü, public
endpoint, UI ve **kullanıcı iptali** yoktur; hepsi alt katmanlarda ve orada
kalır.

**Kapalı doğar.** Worker'ı ayağa kaldıran tek şey ``playbook_worker_enabled``
ayarıdır ve varsayılanı ``False``'tur. Ayar kapalıyken bu modülden ne bir
thread ne de tek bir executor çağrısı doğar; modülü import etmek de hiçbir yan
etki üretmez (modül seviyesinde thread, engine, session veya global mutable
worker yoktur).

**Eşzamanlılık birdir ve yapıyla garanti edilir.** Aynı anda birden çok
çalıştırma olmaması bir sayaçla değil, döngünün biçimiyle sağlanır: ``execute_
next_playbook_job``'ı çağıran **tek bir** thread vardır, o thread çağrıyı
**senkron** yapar ve çağrı dönmeden bir sonraki denemeye geçmez.

**Janitor ayrı bir thread'dir ve bu eşzamanlılığı artırmaz.** Periyodik crash
run temizliği execution döngüsünün içinde çalışsaydı, saatlerce süren tek bir
playbook ``execution_run_janitor_interval_seconds`` için hiçbir üst sınır
bırakmazdı: temizlik o çalıştırma bitene kadar beklerdi ve belgelenen orphan
retention süresi sessizce aşılırdı. Bu yüzden janitor'ın kendi (daemon
**olmayan**) thread'i ve kendi monotonic zamanlaması vardır. İkisinin işi
ayrıktır: janitor Job acquire etmez, executor çağırmaz ve Job durumuna
dokunmaz; yalnız :func:`~app.services.execution.reconcile.sweep_stale_execution_runs`
çağırır. Dolayısıyla aktif playbook sayısı hâlâ birdir. İki thread'i de aynı
worker sahiplenir: :meth:`PlaybookWorker.start` ikisini birlikte açar,
:meth:`PlaybookWorker.stop` ikisini birlikte bekler.

**Başarısız bir başlangıç hiçbir çalıştırma denemesi üretmez.** İki thread aynı
anda başlatılamaz; biri ötekinden önce başlar ve işletim sistemi onu hemen
çalıştırabilir. Bu yüzden entrypoint'ler ilk yan etkilerinden önce bir
*yayımlanma bariyeri* bekler ve bariyer :meth:`PlaybookWorker.start`'ın son
adımıdır. Bariyer olmasaydı, ikinci ``Thread.start`` düşerken ilk thread çoktan
bir Job acquire etmiş olabilirdi: ``start`` çağırana başarısız dönerken arkada
bir çalıştırma sürerdi. Güvence simetriktir — hangi thread ikinci açılırsa
açılsın, yayım tamamlanmadan ne executor ne de janitor yan etkisi doğar.

**Boşta beklemek meşguliyet döngüsü değildir.** Alınacak iş yokken döngü
``stop`` event'i üzerinde ``playbook_worker_poll_seconds`` kadar bekler. Bekleme
hedefinin bir ``sleep`` değil bir event olması bilinçlidir: kapanış talebi
beklemeyi **anında** uyandırır, yani shutdown bir poll aralığı kadar gecikmez.

**Arıza döngüyü sessizce öldürmez ve tight loop üretmez.** Executor'ın sözleşme
dışı bir arızası (veritabanı düştü, disk doldu) yakalanır, sayılır ve döngü
sınırlı bir üstel gecikmeyle (:data:`MAX_FAILURE_BACKOFF_SECONDS` tavanıyla)
devam eder. İkisi de gereklidir: hatayı yutup çıkmak kuyruğu sessizce
durdururdu, hatayı yutup hemen yeniden denemek ise arızalı bir veritabanına
saniyede binlerce kez giden bir döngü üretirdi.

**Log satırı sabittir.** Yazılan metin bir sabittir; exception nesnesi,
``exc_info``, path, Job kimliği, token, digest veya environment içeriği log'a
**girmez**. Bir arızanın metni işletim sisteminin hata dizesini, DSN'i veya bir
workspace yolunu taşır ve log, bunların en kolay okunabildiği yerdir.

**Kapanış, "thread'i unutmak" değildir.** Worker thread'leri bilinçli olarak
*daemon değildir* ve :meth:`PlaybookWorker.stop` ikisini de gerçekten bekler.
``True`` yalnız ikisinin de bittiği **kanıtlandığında** döner; kanıtlanamayan
bir thread'in referansı bırakılmaz, böylece bir sonraki ``stop`` aynı gerçek
thread'i yeniden bekleyebilir. Referansı erken düşürmek, ikinci bir ``stop``
çağrısının "bekleyecek bir şey yok" diye okuyup hâlâ çalışan bir worker için
``True`` dönmesi olurdu.
Kapanış anında bir child process çalışıyorsa, o child'a sonlandırma talebini
:class:`ShutdownProcessObserver` taşır: gözlemci sinyal göndermez ve reap
etmez, yalnız :class:`~app.services.ansible.process.ProcessSupervisor`'ın
verdiği ``request_termination`` callback'ini çağırır. Sinyal, process-group
temizliği ve reap tek sahibinde kalır; ``stop`` ancak o zincir tamamlanıp
executor döndükten sonra geri döner. Daemon bir thread'i "nasılsa süreçle
birlikte ölür" diye geride bırakmak, reap edilmemiş bir child'ı kapanmış
saymak olurdu.

**Bu bir iptal özelliği değildir.** Gözlemci yalnız *sürecin kendi kapanışına*
bağlıdır; kullanıcıdan gelen bir iptal isteği, ona ait bir Job durumu veya bir
route bu modülde yoktur.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.services.execution.executor import (
    ExecutionOutcome,
    execute_next_playbook_job,
)
from app.services.execution.reconcile import sweep_stale_execution_runs

#: Her çağrıda **yeni** bir Session üreten çağrılabilir (executor ile aynı tür).
SessionFactory = Callable[[], Session]

_logger = logging.getLogger(__name__)

# Kapanış gözlemcisinin izleme turu. Gözlemci ``stop`` event'ini sınırlı
# aralıklarla yoklar; kendi ``stop``'u geldiğinde iterasyonun sonunda çıkar.
# Süre kısa tutulur: kapanış talebi ile child'a giden sonlandırma isteği
# arasındaki gecikmenin üst sınırı budur.
SHUTDOWN_WATCH_TICK_SECONDS = 0.25

# İzleme thread'inin durdurulurken beklendiği süre. Thread yalnız bir event
# bekler, yani normalde bir tur içinde biter; süre yine de sonludur.
SHUTDOWN_OBSERVER_JOIN_SECONDS = 5.0

# Worker'ın **iki** thread'inin kapanışta beklendiği toplam azami süre.
# Cömerttir: bu süre yalnızca "kapanış talebi kondu" ile "child sonlandı, reap
# edildi ve Job terminal yazıldı" arasını kapsar, çalıştırmanın kendi süresini
# değil. Bütçe thread başına değil ortaktır; ayrı bütçeler çağıranın verdiği
# süreyi sessizce ikiye katlardı.
WORKER_JOIN_SECONDS = 120.0

# Ardışık arızalarda gecikmenin tavanı. Üstel büyüme sınırsız bırakılırsa
# worker, arıza geçtikten sonra saatlerce uyur hâle gelirdi.
MAX_FAILURE_BACKOFF_SECONDS = 60.0

# Üstel gecikmenin taşmaması için sayaç tavanı; tavandan sonra gecikme zaten
# `MAX_FAILURE_BACKOFF_SECONDS`'e sabitlenmiştir.
_MAX_COUNTED_FAILURES = 20

# Log metinleri **sabittir**: arızanın kendi metni taşınmaz (modül docstring'i).
_LOG_EXECUTION_FAILED = "playbook worker execution attempt failed"
_LOG_JANITOR_FAILED = "playbook worker execution-run janitor failed"
_LOG_STOP_TIMED_OUT = "playbook worker did not stop within the join budget"


class ShutdownProcessObserver:
    """Kapanış talebini çalışan bir child process'e taşıyan gözlemci.

    :class:`~app.services.ansible.process.BoundedProcessObserver` protokolünü
    karşılar: süreç başlar başlamaz :meth:`start`, süreç sonlandığında — hangi
    yoldan sonlanırsa sonlansın — :meth:`stop` çağrılır.

    Var olma sebebi dar bir boşluktur: ``execute_next_playbook_job`` blocking
    çalışırken worker thread'i kendi ``stop`` bayrağını **okuyamaz**, çünkü
    child'ın bitmesini bekliyordur. Kapanış talebini o thread'e "iletmenin"
    yolu, talebi child'a taşıyıp çalıştırmanın normal yoldan bitmesini
    sağlamaktır.

    **Sahiplik.** Gözlemci sinyal göndermez, process group'a dokunmaz, süreç
    beklemez ve reap etmez; yalnız kendisine verilen ``request_termination``
    çağrısını yapar. Sinyalin ve reap'in tek sahibi
    :class:`~app.services.ansible.process.ProcessSupervisor`'dır.

    **Tek kullanımlıktır.** Her execution denemesi için yeni bir gözlemci
    üretilir; ikinci bir :meth:`start` :class:`RuntimeError` yükseltir. Aksi
    hâlde aynı ``stop`` event'ini izleyen iki thread doğabilirdi. :meth:`stop`
    ise idempotenttir: durdurmayı iki kez istemek bir hata değildir.
    """

    def __init__(
        self,
        stop: threading.Event,
        *,
        tick_seconds: float = SHUTDOWN_WATCH_TICK_SECONDS,
        join_seconds: float = SHUTDOWN_OBSERVER_JOIN_SECONDS,
    ) -> None:
        """Gözlemciyi kurar; hiçbir thread başlatmaz ve hiçbir şey talep etmez.

        Args:
            stop: Worker'ın kapanış event'i. Gözlemci bu event'i **okur**;
                hiçbir koşulda set etmez.
            tick_seconds: İzleme turu; pozitif ve sonlu olmalıdır.
            join_seconds: İzleme thread'inin durdurulurken beklendiği süre;
                pozitif ve sonlu olmalıdır.

        Raises:
            ValueError: Süreler pozitif/sonlu değilse. Reddedilen değer hata
                mesajına **yazılmaz**.
        """
        self._stop = stop
        self._tick = _require_interval(tick_seconds)
        self._join = _require_interval(join_seconds)
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._request_termination: Callable[[], None] | None = None

        #: Child'a sonlandırma talebi **gitti mi**. Yalnız gözlemlenebilirlik
        #: içindir; bir karar girdisi değildir.
        self.termination_requested = False
        #: İzleme beklenen sürede durmadı ya da beklenmedik biçimde düştü.
        self.watch_failed = False

    def start(self, request_termination: Callable[[], None]) -> None:
        """İzlemeyi başlatır; kapanış çoktan istenmişse **hemen** talep eder.

        Erken kontrol bilinçlidir: kapanış talebi child başlatılmadan hemen önce
        gelmiş olabilir. O aralıkta bir thread açıp beklemeye başlamak, zaten
        kapanmakta olan bir süreçte yeni bir çalıştırmayı tam süresince ayakta
        tutardı.

        Args:
            request_termination: Supervisor'ın sonlandırma talebi.

        Raises:
            RuntimeError: Gözlemci daha önce başlatılmışsa.
        """
        if self._started:
            raise RuntimeError("Kapanış gözlemcisi tek kullanımlıktır.")
        self._started = True
        self._request_termination = request_termination

        if self._stop.is_set():
            self._demand_termination()
            return

        thread = threading.Thread(target=self._run, name="playbook-shutdown-observer", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """İzlemeyi durdurur ve thread'i **sınırlı** süre bekler.

        Çağrı idempotenttir ve hiçbir zaman sonlandırma talep etmez: ``stop``
        süreç zaten sonlandıktan sonra çağrılır ve o noktada talep etmenin bir
        karşılığı yoktur.

        Thread verilen sürede bitmezse :attr:`watch_failed` işaretlenir; sonuç
        gizlenmez ama çağıranın kapanışını da süresiz bloklamaz.
        """
        self._done.set()
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        thread.join(timeout=self._join)
        if thread.is_alive():
            self.watch_failed = True

    def _run(self) -> None:
        """Kapanış event'ini kendi ``stop``'u gelene kadar sınırlı turlarla izler.

        İki event birlikte beklenemediği için turlar sınırlıdır: her turda
        kapanış event'i ``tick`` kadar beklenir, sonra kendi ``done`` bayrağı
        okunur. Alternatif — kapanış event'ini süresiz beklemek — durdurulamayan
        ve dolayısıyla sızan bir thread üretirdi.

        Beklenmeyen bir arızada sonlandırma **talep edilmez**: ``Event.wait``
        arızası nedeniyle canlı ve doğru çalışan bir execution'ı kesmek, iki
        hatanın daha pahalı olanı olurdu. Arıza yalnız işaretlenir; kapanış o
        yolda child'ın kendi bitişini bekler ve gecikme
        :meth:`PlaybookWorker.stop`'un join bütçesinde görünür hâle gelir.
        """
        try:
            while not self._done.is_set():
                if self._stop.wait(self._tick):
                    self._demand_termination()
                    return
        except BaseException:
            self.watch_failed = True

    def _demand_termination(self) -> None:
        """Supervisor'dan sonlandırma **ister**; sinyal göndermez."""
        terminate = self._request_termination
        if terminate is None:  # pragma: no cover - `start` her zaman doldurur
            return
        self.termination_requested = True
        terminate()


class PlaybookWorker:
    """``pending`` PLAYBOOK Job'larını **teker teker** çalıştıran arka plan döngüsü.

    Worker süreç ömrü boyunca tek bir canonical UUID4 :attr:`worker_id`
    kullanır: sahiplik kanıtı budur ve her denemede yeniden üretilmesi, bir
    önceki denemenin kirasını kendi kimliğiyle yenileyememesi anlamına gelirdi.

    Worker **iki** thread sahiplenir ve ikisini birlikte açıp birlikte bekler:
    Job'ları teker teker çalıştıran execution döngüsü ve yalnız periyodik crash
    run temizliğini yapan janitor. Janitor'ın ayrı olması eşzamanlılığı
    artırmaz (modül docstring'i); ayrı **olmaması** ise uzun bir çalıştırma
    boyunca temizliği tümüyle durdururdu.

    **Tek kullanımlıktır.** İkinci bir :meth:`start` :class:`RuntimeError`
    yükseltir; durdurulmuş bir worker yeniden başlatılamaz. İdempotent bir
    ``start``, aynı kimlikle iki döngü açmanın ve eşzamanlılık sözünü sessizce
    ikiye çıkarmanın yolu olurdu.
    """

    def __init__(self, *, session_factory: SessionFactory, settings: Settings) -> None:
        """Worker'ı kurar; thread başlatmaz ve veritabanına dokunmaz.

        Args:
            session_factory: Her çağrıda **yeni** bir
                :class:`~sqlalchemy.orm.Session` üreten çağrılabilir. Hazır bir
                session kabul edilmez: worker'ın thread'i çağıranınkinden
                farklıdır ve saatlerce sürebilen bir çalıştırma boyunca açık
                tutulan session bir bağlantıyı elde tutardı.
            settings: Doğrulanmış ayarlar. Bütün süreler, sınırlar ve kökler
                buradan gelir; worker hiçbirini yeniden yorumlamaz.
        """
        self._session_factory = session_factory
        self._settings = settings
        self.worker_id = str(uuid.uuid4())
        self._stop = threading.Event()
        # Yaşam döngüsü kilidi: `start` ile `stop`'un ve eşzamanlı `stop`
        # çağrılarının **aynı** thread referanslarını görmesini sağlar. Kilitsiz
        # bir kurulumda iki `stop` çağrısı aynı canlı thread'i farklı okuyabilir
        # ve biri kapanmış sayabilirdi.
        self._lifecycle = threading.Lock()
        # Yayımlanma bariyeri: iki thread de **tam** olarak başlatılıp
        # sahiplenilmeden hiçbir entrypoint yan etki üretmez. Gerekçesi
        # :meth:`start`'tadır.
        self._ready = threading.Event()
        self._execution: threading.Thread | None = None
        self._janitor: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        """Execution döngüsünü ve janitor'ı kendi thread'lerinde başlatır.

        İki thread de daemon **değildir** ve başlatma **atomiktir**: ya iki
        thread birlikte yayımlanır ve çalışmaya başlar, ya da hiçbiri tek bir
        yan etki üretmez.

        **Neden bir yayımlanma bariyeri var.** İki thread aynı anda
        başlatılamaz; biri ötekinden önce başlar ve işletim sistemi onu hemen
        çalıştırabilir. Bariyer olmasaydı ilk thread, ikinci ``Thread.start``
        düşmeden önce bir Job acquire edip ``execute_next_playbook_job``'ı
        çağırabilirdi: ``start`` çağırana **başarısız** dönerken arkada bir
        çalıştırma başlamış olurdu. Bu yüzden entrypoint'ler ilk yan
        etkilerinden önce :attr:`_ready`'yi bekler ve bariyer ``start``'ın son
        adımıdır — iki ``Thread.start`` da döndükten ve referanslar kilit
        altında kaydedildikten **sonra** açılır.

        Sıra bilinçli olarak simetriktir: hangi thread ikinci açılırsa açılsın,
        yayım tamamlanmadan ne executor ne de janitor yan etkisi doğar. Yalnız
        başlatma sırasını değiştirmek bu güvenceyi vermezdi.

        **Geri alım.** İkinci ``Thread.start`` düşerse durdurma talebi konur,
        bariyer yine de açılır, uyanan thread ``stop``'u görüp yan etki
        üretmeden çıkar ve ortak join bütçesiyle beklenir. Durduğu
        kanıtlanamayan bir thread'in referansı korunur; asıl arıza
        gölgelenmeden yukarı taşınır.

        **Bariyer her iki terminal yolda da açılır.** ``start``'ın yayımdan
        vazgeçebileceği üçüncü bir çıkış yoktur: ya son adım olarak
        :attr:`_ready` set edilir, ya da geri alım onu set eder. Bekleyen bir
        entrypoint bu yüzden mutlaka uyandırılır ve bariyerde bir timeout'a
        gerek kalmaz. Gecikmiş ama sonunda başarılı bir ``Thread.start`` da
        böylece doğru sonuçlanır: iki thread de yayımı görür ve çalışır.

        Raises:
            RuntimeError: Worker daha önce başlatılmışsa veya durdurulmuşsa.
            Exception: Thread oluşturulamazsa işletim sisteminin arızası
                yukarı taşınır; o yolda ne bir thread ne de bir çalıştırma
                denemesi geride kalır.
        """
        with self._lifecycle:
            if self._started or self._stop.is_set():
                raise RuntimeError("Playbook worker tek kullanımlıktır.")
            self._started = True
            execution = threading.Thread(target=self._run, name="playbook-worker", daemon=False)
            janitor = threading.Thread(
                target=self._run_janitor, name="playbook-worker-janitor", daemon=False
            )
            try:
                # Referans yalnız gerçekten başlamış bir thread için tutulur:
                # `start` düşerse geri alım tam olarak başlayanları bekler.
                execution.start()
                self._execution = execution
                janitor.start()
                self._janitor = janitor
                # Yayım: bundan **önce** hiçbir entrypoint iş yapmaz.
                self._ready.set()
            except BaseException:
                self._stop.set()
                # Bariyer geri alımda da açılır: kapalı bırakılsaydı başlamış
                # thread `stop`'u hiç göremez ve join bütçesi boşuna dolardı.
                self._ready.set()
                self._await_threads(time.monotonic() + WORKER_JOIN_SECONDS)
                raise

    def stop(self, *, join_seconds: float = WORKER_JOIN_SECONDS) -> bool:
        """Durdurma talebini koyar ve **iki** thread'in de bitmesini bekler.

        Talep konduğu anda üç şey olur: boşta bekleyen execution döngüsü hemen
        uyanır, janitor'ın uzun aralık beklemesi hemen kesilir ve çalışmakta
        olan bir child'ın gözlemcisi sonlandırma talep eder. Çağrı, executor o
        child'ı sonlandırıp reap ettikten ve Job'ı terminal yazdıktan sonra
        döner.

        Çağrı tekrarlanabilir ve eşzamanlı yapılabilir: bir kilit altında hep
        **aynı** gerçek thread'ler gözlemlenir. Bütçe dolduğunda canlı thread'in
        referansı düşürülmez, dolayısıyla ikinci bir ``stop`` aynı thread'i
        yeniden bekler; erken düşürülen bir referans, hâlâ çalışan bir worker
        için ``True`` dönmenin yolu olurdu.

        Args:
            join_seconds: **İki** thread için toplam beklenecek azami süre.

        Returns:
            İki thread de gerçekten bittiyse ``True``. ``False``, worker'ın
            **hâlâ çalışıyor olabileceği** anlamına gelir; sabit bir metinle
            loglanır ve çağıran bunu kapanmış saymamalıdır.
        """
        self._stop.set()
        with self._lifecycle:
            stopped = self._await_threads(time.monotonic() + join_seconds)
        if not stopped:
            _logger.warning(_LOG_STOP_TIMED_OUT)
        return stopped

    def _await_threads(self, deadline: float) -> bool:
        """Sahiplenilen thread'leri ortak bütçeyle bekler; **yalnız** öleni bırakır.

        Bütçe ikisi arasında paylaşılır (ortak bir deadline): thread başına ayrı
        bir bütçe vermek, çağıranın verdiği süreyi sessizce ikiye katlardı.

        Yalnız kilit altında çağrılır.
        """
        self._execution = _reap(self._execution, deadline)
        self._janitor = _reap(self._janitor, deadline)
        return self._execution is None and self._janitor is None

    def _await_publication(self) -> bool:
        """Yayımı bekler ve **çalışmaya başlanıp başlanmayacağını** söyler.

        Bekleme **süresizdir** ve bu bilinçlidir. Bariyeri açan iki terminal yol
        vardır — başarılı yayım ve geri alım — ve :meth:`start` bunların
        dışında dönmez; dolayısıyla bekleyen bir thread mutlaka uyandırılır.
        Buraya bir timeout koymak, süre dolduğunda entrypoint'in **sessizce**
        çıkması olurdu: gecikmiş ama sonunda başarılı bir ``Thread.start``
        ardından ``start`` başarıyla dönerken ilk thread çoktan ölmüş olurdu ve
        worker açık görünüp hiçbir Job işlemezdi. Timeout, takılmış bir
        ``Thread.start`` çağrısını da kurtarmaz — o çağrı zaten *çağıranı*
        bloklar; kurtardığını sandığı tek şey, öldürdüğü thread'dir.

        ``False`` yalnız **geri alımda** döner: ``start`` düşmüştür, bariyer
        açılmıştır ama ``stop`` talebi de konmuştur. Çağıran entrypoint hiçbir
        yan etki üretmeden çıkar; "başarısız bir başlangıç hiçbir çalıştırma
        denemesi üretmez" sözü burada uygulanır.
        """
        self._ready.wait()
        return not self._stop.is_set()

    def _run(self) -> None:
        """Execution döngüsü: yayımı bekle → dene → boşta/arızada sınırlı bekle.

        Bu thread yalnız Job çalıştırır. Periyodik temizlik burada **değildir**:
        blocking bir çalıştırmanın yanına konan janitor, temizlik aralığına üst
        sınır bırakmazdı (modül docstring'i).

        İlk iş yayımı beklemektir: yayım gelmeden **tek bir Job bile acquire
        edilmez** (gerekçesi :meth:`start`'tadır).
        """
        if not self._await_publication():
            return

        failures = 0

        while not self._stop.is_set():
            worked, healthy = self._attempt()
            failures = 0 if healthy else min(failures + 1, _MAX_COUNTED_FAILURES)

            if self._stop.is_set():
                return
            delay = self._delay(worked=worked, failures=failures)
            if delay > 0:
                # Bekleme hedefi ``stop`` event'idir: kapanış talebi beklemeyi
                # anında uyandırır ve shutdown bir poll aralığı geciktirmez.
                self._stop.wait(delay)

    def _run_janitor(self) -> None:
        """Janitor döngüsü: sınırlı bekle → süpür. Job'a **dokunmaz**.

        Zamanlama monotonic saatledir; duvar saati geri alındığında bir daha hiç
        çalışmayan bir zamanlayıcı üretmemek içindir. Bekleme hedefi ``stop``
        event'idir: kapanış talebi uzun bir aralığı **anında** keser.

        Turlar örtüşmez, çünkü tek bir thread vardır ve bir sonraki bekleme tur
        bittikten sonra kurulur. Bir sonraki tur, biteni değil **başlayanı**
        temel alır: aksi hâlde her turun süresi periyoda eklenir ve gerçek
        periyot sessizce büyürdü. Tur aralıktan uzun sürdüyse turlar birikmez;
        bir sonraki tur hemen başlar.

        Execution döngüsüyle aynı biçimde önce yayımı bekler: yayım gelmeden
        **tek bir süpürme turu bile** çalışmaz (gerekçesi :meth:`start`'tadır).
        """
        if not self._await_publication():
            return

        interval = self._settings.execution_run_janitor_interval_seconds
        next_sweep = time.monotonic() + interval

        while not self._stop.is_set():
            delay = next_sweep - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                return
            started = time.monotonic()
            self._sweep()
            next_sweep = max(started + interval, time.monotonic())

    def _attempt(self) -> tuple[bool, bool]:
        """En fazla bir Job işler.

        Returns:
            ``(worked, healthy)``. ``worked``, gerçekten bir Job'ın ele alındığını
            söyler ve döngünün beklemeden bir sonrakine geçmesini sağlar;
            ``healthy`` ise denemenin sözleşme içinde bittiğini söyler.
        """
        if self._stop.is_set():
            # Kapanış istendikten sonra **yeni Job alınmaz**. Kontrol burada,
            # acquire'dan önce durur: alınan bir Job'ı hemen kapanışa denk
            # getirmek, onu kirası dolana kadar `running` bırakmak olurdu.
            return False, True

        observer = ShutdownProcessObserver(self._stop)
        try:
            attempt = execute_next_playbook_job(
                session_factory=self._session_factory,
                settings=self._settings,
                worker_id=self.worker_id,
                lifecycle_observer=observer,
            )
        except Exception:
            # Sözleşme dışı arıza döngüyü öldürmez ve metni loglanmaz.
            _logger.warning(_LOG_EXECUTION_FAILED)
            return False, False
        finally:
            # Executor kendi yolunda gözlemciyi zaten durdurmuş olabilir; çağrı
            # idempotenttir. Hiç başlatılamamış bir gözlemci de burada kapanır.
            observer.stop()

        return attempt.outcome is not ExecutionOutcome.IDLE, True

    def _sweep(self) -> None:
        """Tek bir crash run janitor turunu çalıştırır; arızasını yutar.

        Janitor arızası döngüyü bitirmez ve tight loop üretmez: temizlik bir
        sonraki **sınırlı** aralıkta yeniden denenir, o arada Job çalıştırmak
        engellenmez ve arızanın metni loglanmaz (modül docstring'i). Sayaçlar
        bilinçli olarak okunmaz — hangi dizinin toplandığı bu katmanın konusu
        değildir.
        """
        try:
            sweep_stale_execution_runs(
                self._session_factory,
                execution_run_root=self._settings.resolve_execution_run_dir(),
                stale_seconds=self._settings.execution_run_stale_seconds,
            )
        except Exception:
            _logger.warning(_LOG_JANITOR_FAILED)

    def _delay(self, *, worked: bool, failures: int) -> float:
        """Bir sonraki denemeden önce beklenecek süreyi seçer.

        Üç durum vardır ve üçü de bilinçlidir:

        - *Arıza.* Sınırlı üstel gecikme uygulanır. Hemen yeniden denemek,
          arızalı bir veritabanına giden bir tight loop üretirdi.
        - *İş yapıldı.* Hiç beklenmez: kuyrukta bekleyen bir sonraki Job'ı bir
          poll aralığı geciktirmenin gerekçesi yoktur.
        - *Boşta.* ``playbook_worker_poll_seconds`` kadar beklenir.
        """
        if failures > 0:
            grown = self._settings.playbook_worker_poll_seconds * 2.0 ** (failures - 1)
            return min(grown, MAX_FAILURE_BACKOFF_SECONDS)
        if worked:
            return 0.0
        return self._settings.playbook_worker_poll_seconds


def _reap(thread: threading.Thread | None, deadline: float) -> threading.Thread | None:
    """Thread'i deadline'a kadar bekler; **bittiyse** ``None``, aksi hâlde kendisi.

    Dönüş değeri bilinçli olarak thread'in kendisidir: canlı bir thread'in
    referansı çağıranda kalmalıdır ki bir sonraki bekleme aynı thread'i
    gözlemleyebilsin.
    """
    if thread is None:
        return None
    thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return None if not thread.is_alive() else thread


def _require_interval(seconds: float) -> float:
    """Süreyi doğrular. ``NaN``/sonsuz/sıfır/negatif reddedilir.

    Sıfır veya negatif bir tur, izlemeyi meşguliyet döngüsüne çevirirdi; sonsuz
    bir değer ise izlemeyi tümüyle durdururdu.
    """
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Süre pozitif ve sonlu olmalıdır.")
    return seconds
