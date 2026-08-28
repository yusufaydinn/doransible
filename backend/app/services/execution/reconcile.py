"""Crash'ten artakalan execution run dizinlerinin toplanması (R1-V3C2B).

Bir worker SIGKILL alır, süreç çökerse veya makine kapanırsa executor'ın kendi
``finally`` yolu **hiç çalışmaz** ve ``app-data/execution-runs/<job-uuid>/``
ağacı diskte kalır. Bu modül o kalıntıyı toplayan tek yoldur.

Sözleşme, ne yaptığından çok **neyi yapmadığıyla** tanımlıdır:

- Silme yalnız :func:`app.services.execution.runner_env.remove_execution_run_directory`
  ile yapılır. Bu modülde ikinci bir silme biçimi — ``shutil.rmtree``, glob,
  çözülmüş path üzerinden özyineleme, ``unlink`` — **yoktur**. İkinci bir biçim,
  tam da denetlenen descriptor-relative sınırın yanından dolaşan yol olurdu.
- Hedef çağıran tarafından seçilemez: aday olabilecek tek şey kökün **doğrudan**
  çocuğu olan, canonical UUID4 adlı, gerçek bir dizindir. Kökün kendisine,
  komşu girdilere ve bir symlink'in gösterdiği hedefe hiçbir koşulda
  dokunulmaz.
- Silme bir ada değil, listelenen **nesneye** bağlıdır: her aday kendi
  :class:`~app.services.execution.runner_env.RunDirectoryIdentity` kimliğiyle
  — ``device``, ``inode`` ve ``changed_ns`` (``st_ctime_ns``) — taşınır ve
  remover o kimliği silme öncesinde yeniden doğrular. Üçüncü alan inode yeniden
  kullanımını ayırt etmeyi güçlendirir, uyuşmazlıkta fail-closed davranılır.
  Aradan aynı canonical adla yeni bir dizin oluşturulmuşsa —
  ki bu, bir sonraki denemenin çalışma alanı olabilir — janitor ona dokunmaz.
- Artifact dizinleri, dondurulmuş workspace'ler ve Job satırları bu modülün
  konusu **değildir**. Veritabanı yalnızca *okunur* ve yalnız tek bir soru
  için: hangi PLAYBOOK Job'ları şu anda ``running``?

**Neden lease'e bakılmaz.** Kirası dolmuş bir satırı terminal yapmak
:func:`app.services.execution.job_state.reconcile_stale_playbook_jobs`'ın
işidir ve açılışta **önce** o çalışır. Janitor gördüğü her ``running`` PLAYBOOK
Job'ının dizinini, kirasının durumuna bakmadan korur: iki bileşen aynı kararı
kendi başına verirse, birinin canlı saydığı bir execution'ın çalışma alanı
diğeri tarafından altından silinebilirdi. Koruma kararının tek sahibi Job
durumudur; kira kararının tek sahibi C2A'dır.

**Sıra ve transaction sınırı.** Kök önce listelenir, sonra veritabanı okunur,
sonra session tamamen kapatılır ve **ancak ondan sonra** silme başlar. Üç
gerekçesi vardır:

1. Listeleme ile veritabanı okuması arasında başlayan bir Job'ın dizini listede
   hiç bulunmaz — yani yeni başlamış bir çalıştırma aday olamaz. Ters sıra bu
   satırı yalnızca "genç" olduğu için korurdu.
2. Kök doğrulaması ve girdi sınırı (global fail-closed hatalar) veritabanına
   dokunulmadan önce yüzeye çıkar.
3. Pahalı olabilecek descriptor-relative temizlik boyunca açık bir SQLite
   transaction veya connection tutulmaz (ADR-019 Karar 6/4).

Sonuç, serbest metin değil **sayaçtır**: hangi Job'ın dizininin silindiği,
kökün nerede olduğu ve içeride ne bulunduğu sonuca yazılmaz.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Job, JobStatus, JobType
from app.services.execution.runner_env import (
    RunnerEnvironmentError,
    list_execution_run_directories,
    remove_execution_run_directory,
)

SessionFactory = Callable[[], Session]


@dataclass(frozen=True, slots=True)
class ExecutionRunSweepResult:
    """Bir janitor turunun **sayısal** sonucu.

    Nesne bilinçli olarak yalnız sayı taşır: Job kimliği, path, dizin içeriği
    veya hata metni **yoktur**. Sonuç ileride loglanacak ve muhtemelen API'ye
    çıkacak bir değerdir; içine konan tek bir workspace yolu, kalıntının
    okunabildiği her yerde görünür olurdu.
    """

    #: Bu turda gerçekten kaldırılan çalışma dizini sayısı.
    removed: int
    #: ``running`` bir PLAYBOOK Job'ına ait olduğu için korunanlar.
    preserved_active: int
    #: Yaşı eşiği aşmadığı için korunanlar.
    preserved_young: int
    #: Aday bile olmayan doğrudan girdiler (canonical olmayan ad, symlink,
    #: dosya, FIFO, socket ...). Hiçbiri açılmaz ve silinmez.
    preserved_unexpected: int
    #: Güvenli kaldırma denendi ama fail-closed düştü; girdi yerinde kaldı.
    cleanup_failed: int


def sweep_stale_execution_runs(
    session_factory: SessionFactory,
    *,
    execution_run_root: Path,
    stale_seconds: float,
    now: datetime | None = None,
) -> ExecutionRunSweepResult:
    """Terk edilmiş execution run dizinlerini sınırlı ve fail-closed toplar.

    Sıra sabittir::

        girdi doğrulaması (veritabanına ve dosya sistemine dokunmadan)
        → kökün sınırlı, symlink izlemeyen listelenmesi
        → aktif PLAYBOOK Job kimliklerinin kısa ömürlü session'da okunması
        → session/transaction kapanışı
        → aday başına güvenli kaldırma

    Bir aday yalnız **üç koşul birlikte** sağlanırsa kaldırılır: canonical UUID4
    adlı gerçek bir dizindir, ``running`` bir PLAYBOOK Job'ına ait değildir ve
    yaşı eşiği **aşar**. Yaş sınırı kesindir: ``age == stale_seconds`` korunur.
    Eşitlikte silmek, eşiğin "bu süre boyunca dokunulmaz" sözünü bir saniyelik
    yuvarlama farkına bırakırdı. Gelecekte duran bir ``mtime`` de korunur —
    negatif bir yaş, saat kayması ya da elle kurcalanmış bir zaman damgasıdır ve
    hiçbiri silme gerekçesi değildir.

    Args:
        session_factory: **Yeni** bir Session üreten fabrika. Hazır veya uzun
            ömürlü bir Session kabul edilmez: bu fonksiyon session'ın ömrünü
            kendi belirler ve temizlik başlamadan onu kapatır.
        execution_run_root: ``app-data/execution-runs`` kökü.
        stale_seconds: Pozitif ve sonlu eşik.
        now: Test edilebilirlik için karar anı; timezone-aware olmalıdır.

    Returns:
        Turun :class:`ExecutionRunSweepResult` sayaçları.

    Raises:
        ValueError: ``stale_seconds`` pozitif/sonlu değilse veya ``now`` naive
            ise. Bu yolda ne veritabanına ne dosya sistemine dokunulur.
        RunnerEnvironmentError: Kök relative/yanlış adlı/symlink/yanlış izinli
            ise, okunamıyorsa ya da doğrudan girdi sayısı sınırı aşıyorsa.
            Hepsi silme başlamadan önce yükselir ve hiçbir aday etkilenmez.
        SQLAlchemyError: Aktif Job okuması veya session kapanışı arızalanırsa.
            Arıza boş bir aktif kümeye **çevrilmez**: öyle olsaydı bir disk
            hatası, çalışan bir Job'ın alanının silinmesine yol açardı.
    """
    threshold = _require_stale_seconds(stale_seconds)
    moment = _require_moment(now)

    # Kök doğrulaması ve girdi sınırı burada, yani veritabanına dokunulmadan ve
    # tek bir silme denenmeden önce yüzeye çıkar.
    listing = list_execution_run_directories(execution_run_root)
    active = _active_playbook_job_ids(session_factory)

    removed = 0
    preserved_active = 0
    preserved_young = 0
    cleanup_failed = 0
    for entry in listing.candidates:
        if entry.job_id in active:
            preserved_active += 1
            continue
        if moment.timestamp() - entry.modified_at <= threshold:
            preserved_young += 1
            continue
        try:
            # Hedef burada yeniden **kökten** türetilir ve primitive kendi
            # canonical/doğrudan-çocuk/symlink kontrollerini yeniden yapar:
            # listeleme ile silme arasında yerine symlink konmuş bir aday, o
            # kontrollere takılır ve dış hedef açılmaz.
            #
            # Ad ise tek başına yetmez: aynı canonical adla **yeni ve gerçek**
            # bir dizin oluşturulmuş olabilir ve o dizin bu turun kararına hiç
            # konu olmamıştır. Bu yüzden silme, listelenen **nesnenin**
            # kimliğine bağlanır; uyuşmazsa primitive fail-closed düşer ve
            # replacement açılmadan, içine inilmeden korunur.
            if remove_execution_run_directory(
                execution_run_root,
                entry.job_id,
                missing_ok=True,
                expected_identity=entry.identity,
            ):
                removed += 1
            # ``False``: aday aradan kendiliğinden kayboldu. Ne silindi (biz
            # silmedik), ne korundu (duran bir şey yok), ne de bir arıza. Bu
            # nadir yarışta sayaçların toplamı aday sayısından küçük kalır.
        except (RunnerEnvironmentError, OSError):
            # Tek bir adayın fail-closed reddi turu bitirmez: kalan adaylar yine
            # değerlendirilir. Hata sayılır, girdi **yerinde bırakılır** ve
            # sebebi sonuca taşınmaz.
            cleanup_failed += 1

    return ExecutionRunSweepResult(
        removed=removed,
        preserved_active=preserved_active,
        preserved_young=preserved_young,
        preserved_unexpected=listing.unexpected,
        cleanup_failed=cleanup_failed,
    )


def _active_playbook_job_ids(session_factory: SessionFactory) -> frozenset[str]:
    """``running`` PLAYBOOK Job kimliklerinin kısa ömürlü snapshot'ı.

    Sorgu **yalnız duruma** bakar; ``lease_expires_at`` koşula girmez (modül
    docstring'i). Okuma bittiği anda transaction kapatılır ve session
    ``close()`` ile bırakılır: snapshot'tan sonra açık kalan bir connection,
    dosya sistemi temizliği boyunca tutulurdu.

    Raises:
        SQLAlchemyError: Sorgu veya kapanış arızalanırsa. Yutulmaz ve boş kümeye
            çevrilmez; çağıran hiçbir şey silmeden düşer.
    """
    with contextlib.closing(session_factory()) as session:
        try:
            rows = session.execute(
                select(Job.id).where(
                    Job.job_type == JobType.PLAYBOOK,
                    Job.status == JobStatus.RUNNING,
                )
            ).scalars()
            active = frozenset(rows)
            session.rollback()
        except SQLAlchemyError:
            session.rollback()
            raise
    return active


def _require_stale_seconds(stale_seconds: float) -> float:
    """Eşiği doğrular. ``NaN``/sonsuz/sıfır/negatif reddedilir.

    Sıfır veya negatif bir eşik, yeni oluşturulmuş bir çalışma alanını da stale
    sayardı: çalışan bir Job'ın alanını altından silmenin en kısa yolu budur.
    """
    if not math.isfinite(stale_seconds) or stale_seconds <= 0:
        raise ValueError("Stale eşiği pozitif ve sonlu olmalıdır.")
    return stale_seconds


def _require_moment(now: datetime | None) -> datetime:
    """Karar anını UTC'ye normalize eder; naive değeri reddeder.

    Kontrol :mod:`app.services.execution.job_state`'teki eşiyle aynı gerekçeye
    dayanır ama oradan **import edilmez**: bu modül bir dosya sistemi
    janitor'ıdır ve Job durum makinesine bağlanması, ileride buradan bir Job
    geçişi yazılmasının ilk adımı olurdu. Naive bir değer, yerel saat ile UTC
    arasındaki farkı yaş hesabına taşır; saat farkı kadar "yaşlı" görünen bir
    dizin, çalışan bir Job'ın alanı olabilir.
    """
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Karar anı timezone-aware olmalıdır.")
    return now.astimezone(UTC)
