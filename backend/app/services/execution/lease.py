"""Çalışan bir playbook süreci boyunca Job kirasının yenilenmesi (R1-V3C1C2B1).

Bu modül **tek bir işi** yapar: uzun süren bir child process çalışırken, o
Job'ın kirasını (`lease`) ayrı ve **kısa ömürlü** veritabanı session'larıyla
yeniler; kirayı kaybettiği anda supervisor'dan sürecin sonlandırılmasını
**talep eder**. Burada süreç başlatma, sinyal gönderme, wait/reap, artifact,
normalize, Job'ı bitirme, worker döngüsü, API ve UI **yoktur**.

Neden ayrı bir gözlemci:

- Çalıştırmayı yürüten thread `ansible-runner` sürecinin bitmesini bekler; o
  thread aynı zamanda periyodik bir ``UPDATE`` yapamaz.
- Kira yenilenmezse satır *stale* olur ve başka bir worker aynı işi devralmaya
  hak kazanır. Kirasını kaybetmiş bir worker'ın süreci çalıştırmaya devam
  etmesi, aynı playbook'un iki kez koşması demektir; bu yüzden kira kaybı
  **sonlandırma talebine** çevrilir.

Sözleşmenin sert kısımları:

- **Session thread'ler arasında paylaşılmaz.** SQLAlchemy :class:`Session`
  thread-safe değildir. Bu yüzden gözlemciye bir session değil, bir *session
  factory* verilir: her heartbeat kendi session'ını açar, işini bitirir ve
  session'ı **kapatır**. Uzun süren çalıştırma boyunca açık tutulan tek bir
  session, hem paylaşım hatasına hem de bütün çalıştırma boyunca tutulan bir
  bağlantıya (SQLite'ta bir yazma kilidine) yol açardı.
- **Fail-closed.** Heartbeat ``False`` dönerse kira kaybedilmiş sayılır;
  heartbeat beklenmedik biçimde arıza verirse (veritabanı düştü, factory
  patladı, session kapanamadı) sahiplik **kanıtlanamamış** sayılır. İki durumda
  da süreç sonlandırma talebi gider: ölçülemeyen bir kira, geçerli bir kira
  değildir.
- **Sızdırmaz.** Hiçbir arıza log'a, stderr'e veya kalıcı bir alana yazılmaz.
  Thread'den sızan bir exception, işletim sisteminin hata metnini, DSN'i veya
  path'i thread excepthook'u üzerinden stderr'e dökerdi. Gözlemcinin dışarıya
  verdiği tek şey iki **boolean**'dır: :attr:`~PlaybookLeaseObserver.lease_lost`
  ve :attr:`~PlaybookLeaseObserver.heartbeat_failed`.
- **Sinyal sahipliği gözlemcide değildir.** Gözlemci yalnız kendisine verilen
  ``request_termination`` çağrısını yapar; sinyal gönderme, process-group
  sonlandırma ve reap işleri tek sahibinde
  (:class:`~app.services.ansible.process.ProcessSupervisor`) kalır.

Karara bağlanan şey burada **kararlaştırılmaz**: Job'ı `failed` yapmak, hata
kodu üretmek ve sonucu yazmak bir sonraki dilimin (R1-V3C1C2B2) işidir. Bu
modül yalnız gözlemi üretir.
"""

from __future__ import annotations

import math
import threading
import uuid
from collections.abc import Callable
from contextlib import closing

from sqlalchemy.orm import Session

from app.services.execution.job_state import heartbeat_playbook_job

# Heartbeat thread'i durdurulurken beklenen süre. Cömerttir: yolda olan tek bir
# ``UPDATE``'in dönmesi bir heartbeat aralığından uzun sürebilir. Süre yine de
# sonludur; bu sürede bitmeyen bir thread'in sonucu beklenmez.
LEASE_OBSERVER_JOIN_SECONDS = 5.0

#: Her heartbeat için **yeni** bir session üreten çağrılabilir.
SessionFactory = Callable[[], Session]


class PlaybookLeaseObserver:
    """Çalışan bir süreç boyunca Job kirasını yenileyen gözlemci.

    :class:`~app.services.ansible.process.BoundedProcessObserver` protokolünü
    karşılar: süreç başlar başlamaz :meth:`start`, süreç sonlandığında — hangi
    yoldan sonlanırsa sonlansın — :meth:`stop` çağrılır.

    **Tek kullanımlıktır.** Bir gözlemci tek bir çalıştırmaya aittir: ikinci bir
    :meth:`start` veya durdurulduktan sonra yapılan bir :meth:`start`
    :class:`RuntimeError` yükseltir. İdempotent bir ``start``, aynı Job için iki
    heartbeat thread'i açmanın veya durdurulmuş bir gözlemciyi sessizce
    diriltmenin yolu olurdu; ikisi de kirayı gözlemcinin bildirdiğinden daha
    uzun süre canlı tutardı. :meth:`stop` ise idempotenttir: durdurmayı iki kez
    istemek bir hata değildir.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        job_id: str,
        worker_id: str,
        heartbeat_seconds: float,
        lease_seconds: float,
    ) -> None:
        """Gözlemciyi kurar; hiçbir thread başlatmaz ve veritabanına dokunmaz.

        Args:
            session_factory: Her heartbeat için **yeni** bir
                :class:`~sqlalchemy.orm.Session` üreten çağrılabilir. Hazır bir
                session bilinçli olarak kabul edilmez: o session çağıranın
                thread'ine aittir.
            job_id: Kirası yenilenecek Job'un canonical UUID4 kimliği.
            worker_id: Kirayı elinde tuttuğunu iddia eden worker'ın canonical
                UUID4 kimliği.
            heartbeat_seconds: İki heartbeat arasındaki pozitif aralık.
            lease_seconds: Her heartbeat'te yazılacak pozitif kira süresi.

        Raises:
            ValueError: Kimlikler canonical UUID4 değilse, süreler pozitif/sonlu
                değilse ya da heartbeat aralığı kiradan kısa değilse. Kirasından
                seyrek atan bir heartbeat, yenileme başarılı olsa bile kirayı
                düzenli olarak süresi geçmiş bırakırdı. Reddedilen değerler hata
                mesajına **yazılmaz**.
        """
        self._session_factory = session_factory
        self._job_id = _require_uuid4(job_id, "Job kimliği canonical UUID4 olmalıdır.")
        self._worker_id = _require_uuid4(worker_id, "Worker kimliği canonical UUID4 olmalıdır.")
        self._heartbeat_seconds = _require_interval(heartbeat_seconds)
        self._lease_seconds = _require_interval(lease_seconds)
        if self._heartbeat_seconds >= self._lease_seconds:
            raise ValueError("Heartbeat aralığı kira süresinden kısa olmalıdır.")

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._request_termination: Callable[[], None] | None = None

        #: Kira **kaybedildi**: heartbeat hiçbir satırı etkilemedi. Satır artık
        #: bu worker'ın değil, `running` değil ya da kirası çoktan dolmuş.
        self.lease_lost = False
        #: Heartbeat **yapılamadı**: session açılamadı, ``UPDATE`` arıza verdi
        #: ya da thread beklenen sürede durmadı. Kiranın canlı olduğu
        #: kanıtlanamamıştır; ölçülemeyen kira geçerli kira sayılmaz.
        self.heartbeat_failed = False

    def start(self, request_termination: Callable[[], None]) -> None:
        """Heartbeat thread'ini başlatır.

        İlk heartbeat bir aralık **beklenmeden** yapılır: acquire ile sürecin
        başlaması arasında geçen zaman kiradan yenmiştir ve ilk yenilemeyi bir
        aralık geciktirmek o payı büyütürdü.

        Args:
            request_termination: Supervisor'ın sonlandırma talebi. Gözlemci
                sinyal göndermez, süreci beklemez ve reap etmez.

        Raises:
            RuntimeError: Gözlemci daha önce başlatılmışsa veya durdurulmuşsa.
        """
        if self._started or self._stop.is_set():
            raise RuntimeError("Lease gözlemcisi tek kullanımlıktır.")
        self._started = True
        self._request_termination = request_termination
        thread = threading.Thread(target=self._run, daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Heartbeat'i durdurur ve thread'i **sınırlı** süre bekler.

        Durdurma isteği konduktan sonra yeni bir heartbeat **başlamaz**: her tur
        veritabanına gitmeden önce durdurma bayrağını okur.

        Thread verilen sürede bitmezse sonuç fail-closed sayılır:
        :attr:`heartbeat_failed` işaretlenir. Yolda kalmış bir ``UPDATE``'in
        sonucu beklenmez ve okunmaz; kiranın canlı olduğu bu yolda
        kanıtlanamamıştır. Thread daemon olduğu için süreci ayakta tutmaz.

        Burada sonlandırma **talep edilmez**: :meth:`stop` süreç zaten
        sonlandıktan sonra çağrılır ve o noktada talep etmenin bir karşılığı
        yoktur. Çağrı idempotenttir.
        """
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        thread.join(timeout=LEASE_OBSERVER_JOIN_SECONDS)
        if thread.is_alive():
            self.heartbeat_failed = True

    def _run(self) -> None:
        """Heartbeat döngüsü: hemen bir kez, sonra sınırlı aralıklarla.

        Döngü gövdesi bütünüyle korunur. Thread'den sızan bir exception
        stderr'e hata metni dökerdi ve — daha kötüsü — kirayı kimse yenilemediği
        hâlde süreç çalışmaya devam ederdi.
        """
        try:
            while not self._stop.is_set():
                if not self._beat():
                    return
                if self._stop.wait(self._heartbeat_seconds):
                    return
        except BaseException:
            self._fail()

    def _beat(self) -> bool:
        """Tek bir heartbeat yapar; döngünün sürüp sürmeyeceğini söyler.

        Returns:
            Kira yenilendiyse ``True``. Kira kaybedildiyse veya heartbeat arıza
            verdiyse ``False``: iki durumda da sonlandırma talep edilmiştir ve
            döngü devam etmez. Kaybedilmiş bir kirayı yeniden denemek, satırı
            devralmış olabilecek başka bir worker'ın altından işi çekmeye
            çalışmak olurdu.
        """
        try:
            renewed = self._renew()
        except BaseException:
            # Hata **taşınmaz**: metni DSN, path veya sürücü ayrıntısı içerir.
            # Dışarıya çıkan tek şey `heartbeat_failed` bayrağıdır.
            self._fail()
            return False
        if not renewed:
            self.lease_lost = True
            self._demand_termination()
            return False
        return True

    def _renew(self) -> bool:
        """Kısa ömürlü bir session açar, kirayı yeniler ve session'ı kapatır.

        Session bu thread'e aittir ve heartbeat biter bitmez kapanır: uzun
        çalıştırma boyunca açık tutulan bir session bağlantıyı (SQLite'ta yazma
        kilidini) çalıştırma süresince elde tutardı.
        """
        with closing(self._session_factory()) as session:
            return heartbeat_playbook_job(
                session,
                job_id=self._job_id,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
            )

    def _fail(self) -> None:
        """Heartbeat arızasını kaydeder ve sonlandırma talep eder."""
        self.heartbeat_failed = True
        self._demand_termination()

    def _demand_termination(self) -> None:
        """Supervisor'dan sonlandırma **ister**; sinyal göndermez."""
        terminate = self._request_termination
        if terminate is not None:
            terminate()


def _require_interval(seconds: float) -> float:
    """Süreyi doğrular. ``NaN``/sonsuz/sıfır/negatif reddedilir.

    Sıfır veya negatif bir aralık, döngüyü veritabanına kesintisiz yazan bir
    meşguliyet döngüsüne çevirirdi; sonsuz bir değer ise heartbeat'i tümüyle
    durdururdu.
    """
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Süre pozitif ve sonlu olmalıdır.")
    return seconds


def _require_uuid4(value: str, message: str) -> str:
    """Kimliğin canonical UUID4 olduğunu doğrular; değeri hataya yazmaz.

    Kontrol burada, thread başlamadan yapılır. Aşağı katmana bırakılsaydı
    geçersiz bir kimlik ancak thread içinde bir ``ValueError`` olarak görünür ve
    sıradan bir heartbeat arızasından ayırt edilemezdi.
    """
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(message) from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(message)
    return value
