"""PLAYBOOK Job'ın atomik alınması, lease heartbeat'i ve terminal sonucu.

R1-V3C1C1A (acquire + heartbeat), R1-V3C1C1B (finish) ve R1-V3C2A (kirası
gerçekten dolmuş ``running`` satırların uzlaştırılması).

Bu modül **yalnızca veritabanı durum makinesidir**. Alt süreç açmaz,
`ansible-runner` çağırmaz, dosya sistemine dokunmaz, artifact **yazmaz** ve bir
worker döngüsü içermez: sonucu yalnızca *kaydeder*. İçeriye alınan tek
bağımlılıklar SQLAlchemy ve ORM modelleridir; runner, workspace, normalize,
artifact store, FastAPI ve frontend katmanları buradan **import edilmez**.

Dört geçiş tanımlanır:

- :func:`acquire_pending_playbook_job` — ``pending → running``. Kararı ve
  değişikliği tek bir koşullu ``UPDATE`` verir; "önce oku, sonra yaz" penceresi
  kazanan tarafı belirlemez. Aday seçimi için bir ``SELECT`` yapılır ama o
  seçim bir **hak** değil yalnızca bir adaydır: iki worker aynı satırı görse de
  ``WHERE ... status = 'pending'`` koşulunu yalnız biri karşılar.
- :func:`heartbeat_playbook_job` — kirayı **yalnız sahibi** ve **yalnız kira
  dolmadan** uzatabilir. Ölmüş bir kirayı yeniden canlandırmak, satırı stale
  kabul edip devralmış bir başka worker'ın altından işi çekerdi; bu yüzden
  ``lease_expires_at > now`` koşulun parçasıdır ve eşitlik yeterli değildir.
- :func:`finish_playbook_job` — ``running → successful | failed``. Sonuç
  alanlarının tamamı ve kiranın boşaltılması **aynı** koşullu ``UPDATE``'tedir;
  koşul ``status = 'running' AND worker_id = <çağıran>`` olduğu için yanlış
  sahip, terminal satır veya kaybedilmiş yarış ikinci kez sonuç yazamaz.
- :func:`reconcile_stale_playbook_jobs` — ``running → failed``, yalnız kirası
  **gerçekten dolmuş** satırlar için. Çökmüş bir worker'ın arkasında bıraktığı
  satırı kapatan tek yetki kira süresidir; "bu satırın sahibi ben değilim"
  tek başına stale kanıtı **değildir**, çünkü aynı veritabanına bakan başka
  bir canlı süreç olabilir.

Sonuç sözleşmesi üç ayrı sonucu **birbirine karıştırmaz**:

- iş yok veya yarış kaybedildi (:attr:`AcquireOutcome.IDLE`),
- Job'ın plan bağı geçersiz ve Job kapatıldı
  (:attr:`AcquireOutcome.BINDING_INVALID`),
- gerçek veritabanı arızası (:class:`sqlalchemy.exc.SQLAlchemyError` yükselir).

İlk ikisini tek bir ``None``'a indirgemek, çağıranın "bekle" ile "hemen bir
sonraki işe bak" arasındaki farkı görememesine; üçüncüsünü onlara katmak ise
disk arızasının sessizce "iş yok" diye okunmasına yol açardı.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import Row, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionMode,
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    Job,
    JobStatus,
    JobType,
)

# Plan bağı doğrulanamayan Job'ın makine tarafından okunabilir sebebi.
# `Job.error_code` sözleşmesine uyar: serbest metin değildir ve path, token,
# digest veya environment içeriği taşımaz.
ERROR_EXECUTION_BINDING_INVALID = "execution_binding_invalid"

# Kirası dolmuş `running` bir satırın uzlaştırma sırasında kapatılma sebebi
# Public hata kodu sözleşmesine ve `Job.error_code` alanına uyar: serbest
# metin değildir ve path, worker kimliği, token, digest veya environment
# içeriği taşımaz.
#
# Kod bu recovery alanına aittir ve :data:`FINISH_ERROR_CODES` içinde bilinçli
# olarak **yer almaz**: bir çalıştırmanın sonucunu değil, sonucun hiç
# öğrenilemediğini kaydeder. Normal finish allowlist'ine eklenseydi, çalışan bir
# worker sıradan bir sonuç olarak "restart kesti" yazabilir ve gerçekten kesilen
# execution'lar ile normal biten execution'lar aynı kayıtta karışırdı.
ERROR_INTERRUPTED_BY_RESTART = "interrupted_by_restart"

# Bir çalıştırma sonucunun taşıyabileceği **bütün** hata kodları
# Public Job hata kodu sözleşmesi. Liste burada yeniden yazılır;
# :mod:`app.services.execution.normalize` veya runner katmanından **import
# edilmez**. Bu modül bir veritabanı durum makinesidir ve o katmanlara
# bağlanması, çalıştırma yolunun buradan geçtiği izlenimini verirdi.
#
# Allowlist bir biçim kontrolü değil, bir **içerik** kontrolüdür: ``error_code``
# API'ye çıkacak sabit bir sözlüktür ve path, token, digest veya environment
# içeriği taşımamalıdır. Serbest metne izin veren bir alan bunların hepsini
# taşıyabilir; hata mesajı olarak yazılmış tek bir workspace yolu, sonucun
# okunabildiği her yerde görünür olurdu.
#
# :data:`ERROR_EXECUTION_BINDING_INVALID` bilinçli olarak **listede değildir**:
# o kod bir çalıştırma sonucu değil, çalıştırmanın hiç başlamadığının
# kaydıdır ve yalnızca acquire yolu (:func:`_fail_binding`) yazar. Buradan da
# kabul edilseydi, hiç çalışmamış bir Job'ın çalışmış gibi bitirilmesi mümkün
# olurdu.
FINISH_ERROR_CODES: frozenset[str] = frozenset(
    {
        "runner_start_failed",
        "runner_timeout",
        "runner_failed",
        # Doğrulanmış bir Ansible sonucunda task failure/unreachable host
        # bulundu. `runner_failed` listede **kalır**: eski satırlar onu taşır ve
        # kanıtı eksik kalan başarısızlıklar bugün de onu yazar.
        "playbook_failed",
        "runner_output_invalid",
        "runner_no_hosts",
        "workspace_unavailable",
        "workspace_integrity_failed",
        "result_limit_exceeded",
    }
)

# Terminal sonucun kabul edilen **tek** iki durumu. ``canceled`` bilinçli olarak
# dışarıdadır: iptal, bu fonksiyonun modellediği "çalıştırma bitti" geçişi
# değildir ve kendi yolunu gerektirir.
_TERMINAL_STATUSES = frozenset({JobStatus.SUCCESSFUL, JobStatus.FAILED})

# Yayımlanmış sonucun app-data köküne göreli **tek** geçerli konumu. Biçim
# :class:`app.services.jobs.artifacts.JobArtifactStore` sözleşmesiyle aynıdır
# ama o modül import edilmez: burada dosya sistemine dokunulmaz, yalnızca
# kaydedilecek dizgi doğrulanır.
_ARTIFACT_TEMPLATE = "jobs/{job_id}/result.json"

# Kiranın üst sınırı. Sınırsız bir lease iki yönden zararlıdır: çökmüş bir
# worker'ın bıraktığı satır saatlerce devralınamaz hâle gelir ve devasa bir
# değer tarih aritmetiğini taşırma hatasına sürükler. Sınır bir politika
# değil, girdi doğrulamasının makul üst ucudur.
MAX_LEASE_SECONDS = 86_400.0

# `ExecutionPlanRecord.manifest_digest` tam 64 küçük harfli hex olmalıdır.
# Büyük harfli veya kırpılmış bir digest, ileride dondurulmuş içerikle
# karşılaştırılacak değerin sessizce eşleşmemesi demektir.
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


class AcquireOutcome(StrEnum):
    """Bir acquire denemesinin üç olası sonucu."""

    #: Job bu worker'a geçti; :attr:`AcquireResult.context` doludur.
    ACQUIRED = "acquired"
    #: Alınacak aday yok ya da atomik geçişi başka bir worker kazandı.
    IDLE = "idle"
    #: Aday bulundu ama plan bağı geçersizdi; Job terminal `failed` yapıldı.
    BINDING_INVALID = "binding_invalid"


@dataclass(frozen=True, slots=True)
class AcquiredPlaybookJob:
    """Bir worker'a geçmiş PLAYBOOK Job'ının değişmez tanımı.

    Nesne bilinçli olarak **taşımadıkları** ile tanımlanır: raw plan token'ı,
    token özeti, absolute path, environment ve hiçbir credential burada
    bulunmaz. Dondurulmuş içeriğe erişim kökten değil, opaque ``workspace_id``
    üzerinden çözülür; kökü nesneye yazmak, kökü değişen bir kurulumda eski bir
    dizinin "yetkilendirilmiş" sayılmasına yol açardı.
    """

    job_id: str
    execution_plan_id: str
    workspace_id: str
    manifest_digest: str
    project_id: int
    inventory_id: int
    playbook_path: str
    requested_by: str
    #: Doğrulanmış execution mode. Değer Job satırından gelir ve bağlam ancak
    #: :func:`_binding_is_valid` onu plan kaydındaki kiple **eşit** bulduktan
    #: sonra kurulur; iki kaynak arasında seçim yapmak, plan ile Job'ın
    #: ayrıştığı bir satırda hangisinin çalıştırılacağını sessizce belirlerdi.
    #: Varsayılanı yoktur: mode'u atlayan bir çağrı, kipi eksik bir bağlamı
    #: sessizce ``check`` sayardı.
    mode: ExecutionMode
    worker_id: str


@dataclass(frozen=True, slots=True)
class AcquireResult:
    """Acquire denemesinin sonucu ve — yalnız kazanıldıysa — bağlamı."""

    outcome: AcquireOutcome
    context: AcquiredPlaybookJob | None = None

    def __post_init__(self) -> None:
        if (self.context is not None) is not (self.outcome is AcquireOutcome.ACQUIRED):
            raise ValueError("Execution context yalnız kazanılmış acquire sonucunda bulunur.")


_IDLE = AcquireResult(AcquireOutcome.IDLE)
_BINDING_INVALID = AcquireResult(AcquireOutcome.BINDING_INVALID)


def acquire_pending_playbook_job(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: float,
    now: datetime | None = None,
) -> AcquireResult:
    """En eski ``pending`` PLAYBOOK Job'ını bu worker adına ``running`` yapar.

    Sıra şöyledir::

        girdi doğrulaması (veritabanına dokunmadan)
        → deterministic aday seçimi
        → plan bağının okunması, okuma transaction'ının kapatılması
        → plan bağının doğrulanması
        → koşullu UPDATE (pending → running) + tek commit

    **Yarış sözleşmesi.** Aday ``SELECT``'i bir rezervasyon değildir. Geçiş,
    en az ``id``, ``job_type`` ve ``status = 'pending'`` koşullarını taşıyan tek
    bir ``UPDATE`` ile yapılır; iki worker aynı adayı görse bile ikincisinin
    ``UPDATE``'i hiçbir satır etkilemez ve kaybeden taraf **hiçbir** execution
    context üretmeden :attr:`AcquireOutcome.IDLE` alır. Sahiplik, başlangıç ve
    kira alanları (``worker_id``, ``started_at``, ``heartbeat_at``,
    ``lease_expires_at``) aynı ifadede yazılır: ayrı bir ikinci ``UPDATE``,
    arada bir arıza olduğunda sahibi olmayan bir ``running`` satır bırakırdı —
    veritabanı zaten böyle bir satırı ``ck_jobs_running_playbook_has_lease``
    ile reddeder.

    **Plan bağı.** Job'ın yetkilendirildiği plan gerçekten mevcut, ``claimed``
    ve Job ile aynı project/inventory/playbook/aktör dörtlüsünü taşıyor
    olmalıdır; Job ``limit_pattern`` taşımamalı, planın ``workspace_id``'si
    canonical UUID4 ve ``manifest_digest``'i tam 64 küçük harfli hex olmalıdır.
    Planın **TTL'si bilinçli olarak kontrol edilmez**: TTL, bir onay biletinin
    ne kadar süre *claim edilebilir* kaldığını söyler. Bilet bir kez claim
    edilip Job üretmişse yetkilendirme çoktan gerçekleşmiştir; sonradan geçen
    bir TTL yüzünden onaylanmış işi düşürmek, kuyrukta bekleyen her işi
    kullanıcının haberi olmadan iptal ederdi.

    Bağ geçersizse Job **ne çalıştırılır ne de pending bırakılır**: tek
    transaction içinde terminal ``failed`` yapılır (bkz.
    :data:`ERROR_EXECUTION_BINDING_INVALID`) ve execution context dönmez.
    Pending bırakmak, aynı bozuk satırın her turda yeniden seçilip kuyruğu
    kalıcı olarak tıkaması demekti — global aktif PLAYBOOK sınırı 1 olduğu için
    bu, bütün playbook execution'ının durması anlamına gelirdi.

    Args:
        session: Aktif veritabanı session'ı. Fonksiyon çıkışında — başarı,
            no-op veya hata — açık transaction bırakılmaz.
        worker_id: Bu worker'ın canonical UUID4 kimliği.
        lease_seconds: Pozitif kira süresi; ``now`` üzerine eklenir.
        now: Test edilebilirlik için karar anı; timezone-aware olmalıdır.

    Returns:
        Kazanıldıysa bağlamı taşıyan, aksi hâlde bağlamsız bir
        :class:`AcquireResult`.

    Raises:
        ValueError: ``worker_id`` canonical UUID4 değilse, ``now`` naive ise
            veya ``lease_seconds`` pozitif/sonlu değilse. Bu yolda veritabanına
            **hiç** dokunulmaz.
        SQLAlchemyError: Aday/plan okuması **veya** geçiş veritabanı arızasıyla
            düşerse. Her iki yolda da rollback edilir ve hata olduğu gibi
            yeniden yükselir; "iş yok" ya da "bağ geçersiz" ile karıştırılmaz.
    """
    worker = _require_uuid4(worker_id, "Worker kimliği canonical UUID4 olmalıdır.")
    lease = _require_lease(lease_seconds)
    moment = _require_moment(now)

    # Okuma da bir transaction açar ve okuma da düşebilir. İki `SELECT` bu
    # yüzden yazma yolundakiyle aynı korumadadır: arızada session rollback
    # edilir ve hata olduğu gibi yükselir. Bir okuma hatasını "iş yok" veya
    # "bağ geçersiz" sonucuna çevirmek, disk arızasını boş bir kuyruk gibi
    # gösterir; rollback'siz bırakmak ise çağırana açık/failed bir transaction
    # devrederdi.
    try:
        candidate = session.execute(
            select(
                Job.id,
                Job.execution_plan_id,
                Job.project_id,
                Job.inventory_id,
                Job.playbook_path,
                Job.requested_by,
                Job.limit_pattern,
                Job.mode,
            )
            .where(Job.job_type == JobType.PLAYBOOK, Job.status == JobStatus.PENDING)
            # `id` ikincil sıradır: aynı `created_at` taşıyan iki satırda seçimin
            # sürücünün satır sırasına kalmaması gerekir.
            .order_by(Job.created_at, Job.id)
            .limit(1)
        ).first()

        plan = None
        if candidate is not None and candidate.execution_plan_id is not None:
            plan = session.execute(
                select(
                    ExecutionPlanRecord.status,
                    ExecutionPlanRecord.project_id,
                    ExecutionPlanRecord.inventory_id,
                    ExecutionPlanRecord.playbook_path,
                    ExecutionPlanRecord.requested_by,
                    ExecutionPlanRecord.workspace_id,
                    ExecutionPlanRecord.manifest_digest,
                    ExecutionPlanRecord.mode,
                ).where(ExecutionPlanRecord.id == candidate.execution_plan_id)
            ).first()
    except SQLAlchemyError:
        session.rollback()
        raise

    # Okuma transaction'ı karar verilir verilmez kapatılır. Aday `SELECT`'i bir
    # rezervasyon olmadığı için okuma kilidini yazma boyunca tutmanın hiçbir
    # doğruluk katkısı yoktur; iki worker'ın okuma kilitlerini tutarken
    # birbirinin yazmasını beklemesi ise ikisini birden düşürürdü. Geçişi
    # belirleyen tek şey aşağıdaki koşullu `UPDATE`'tir.
    session.rollback()

    if candidate is None:
        return _IDLE

    if plan is None or not _binding_is_valid(candidate, plan):
        return _fail_binding(session, job_id=candidate.id, now=moment)

    # Bağlam değerleri **commit'ten önce** düz Python değerleri olarak elde
    # edilir. ORM nesnesi taşımak, commit sonrası bir attribute erişiminin
    # sessizce yeni bir transaction açmasına yol açardı.
    context = AcquiredPlaybookJob(
        job_id=candidate.id,
        execution_plan_id=candidate.execution_plan_id,
        workspace_id=plan.workspace_id,
        manifest_digest=plan.manifest_digest,
        project_id=plan.project_id,
        inventory_id=plan.inventory_id,
        playbook_path=plan.playbook_path,
        requested_by=plan.requested_by,
        # Kip Job satırından okunur. Eşitliği yukarıda doğrulandığı için iki
        # kaynak arasında fark yoktur; farkın olabildiği tek durumda bağlam
        # zaten kurulmaz. Job'ı seçmek, çalıştırılacak satırın kendi kipini
        # taşımasını sağlar: bağlam ile satır arasına, ileride ikisinin
        # ayrışabileceği bir dolaylılık girmez.
        mode=candidate.mode,
        worker_id=worker,
    )

    try:
        result = session.execute(
            update(Job)
            .where(
                Job.id == candidate.id,
                Job.job_type == JobType.PLAYBOOK,
                Job.status == JobStatus.PENDING,
            )
            .values(
                status=JobStatus.RUNNING,
                worker_id=worker,
                started_at=moment,
                heartbeat_at=moment,
                lease_expires_at=moment + lease,
            )
            .execution_options(synchronize_session=False)
        )
        if _rowcount(result) != 1:
            # Yarışı başka bir worker kazandı: bu çağrı hiçbir şey değiştirmedi.
            session.rollback()
            return _IDLE
        # Commit de aynı `try` içindedir: bir kısıt ihlali her zaman `execute`
        # anında görünmez ve yakalanmamış bir commit hatası session'ı çağırana
        # kirli bırakırdı.
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise

    return AcquireResult(AcquireOutcome.ACQUIRED, context)


def heartbeat_playbook_job(
    session: Session,
    *,
    job_id: str,
    worker_id: str,
    lease_seconds: float,
    now: datetime | None = None,
) -> bool:
    """Sahibi olan worker'ın kirasını tek koşullu ``UPDATE`` ile yeniler.

    Yenileme yalnızca dört koşul birlikte sağlandığında gerçekleşir: satır bir
    PLAYBOOK Job'ıdır, ``running``'dir, ``worker_id`` **tam olarak** çağıranın
    kimliğidir ve mevcut ``lease_expires_at`` hâlâ ``now``'dan büyüktür.

    Sınır **kesindir**: ``lease_expires_at == now`` yenilenmez. Kirası dolmuş
    bir satır, stale recovery'nin devralmaya hak kazandığı satırdır; onu
    canlandırmak, aynı işin iki worker tarafından sahiplenilmesine kapı açardı.
    Aynı gerekçeyle ``pending`` ve terminal satırlar da yenilenemez: birinin
    henüz sahibi yoktur, diğeri artık kimseye ait değildir.

    Args:
        session: Aktif veritabanı session'ı. Başarı ve no-op sonrasında açık
            transaction bırakılmaz.
        job_id: Yenilenecek Job'ın canonical UUID4 kimliği.
        worker_id: Kirayı elinde tuttuğunu iddia eden worker.
        lease_seconds: Pozitif kira süresi; ``now`` üzerine eklenir.
        now: Test edilebilirlik için karar anı; timezone-aware olmalıdır.

    Returns:
        Kira yenilendiyse ``True``; hiçbir satır etkilenmediyse ``False``.
        ``False`` bir hata değil, açık bir no-op bildirimidir: sahipliğini
        kaybetmiş bir worker'ın yapması gereken, işi bırakmaktır.

    Raises:
        ValueError: Kimlikler canonical UUID4 değilse, ``now`` naive ise veya
            ``lease_seconds`` pozitif/sonlu değilse. Veritabanına **hiç**
            dokunulmaz.
        SQLAlchemyError: Yenileme veritabanı arızasıyla düşerse. Rollback
            edilir ve hata yeniden yükselir; sessizce ``False`` dönülmez.
    """
    job = _require_uuid4(job_id, "Job kimliği canonical UUID4 olmalıdır.")
    worker = _require_uuid4(worker_id, "Worker kimliği canonical UUID4 olmalıdır.")
    lease = _require_lease(lease_seconds)
    moment = _require_moment(now)

    try:
        result = session.execute(
            update(Job)
            .where(
                Job.id == job,
                Job.job_type == JobType.PLAYBOOK,
                Job.status == JobStatus.RUNNING,
                Job.worker_id == worker,
                Job.lease_expires_at > moment,
            )
            .values(heartbeat_at=moment, lease_expires_at=moment + lease)
            .execution_options(synchronize_session=False)
        )
        if _rowcount(result) != 1:
            session.rollback()
            return False
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise
    return True


def finish_playbook_job(
    session: Session,
    *,
    job_id: str,
    worker_id: str,
    status: JobStatus,
    return_code: int | None,
    error_code: str | None,
    artifact_path: str | None,
    result_truncated: bool,
    now: datetime | None = None,
) -> bool:
    """Sahibi olan worker'ın Job'ını tek koşullu ``UPDATE`` ile terminal yapar.

    Geçiş yalnızca üç koşul birlikte sağlandığında gerçekleşir: satır bir
    PLAYBOOK Job'ıdır, ``running``'dir ve ``worker_id`` **tam olarak**
    çağıranın kimliğidir. Sonuç alanlarının tamamı (durum, ``finished_at``,
    ``return_code``, ``error_code``, ``artifact_path``, ``result_truncated``) ve
    kiranın boşaltılması (``worker_id``, ``heartbeat_at``, ``lease_expires_at``
    → ``NULL``) **aynı** ifadededir. Ayrı bir ikinci ``UPDATE``, arada bir arıza
    olduğunda kirası hâlâ duran terminal bir satır bırakırdı; veritabanı zaten
    böyle bir satırı ``ck_jobs_idle_playbook_has_no_lease`` ile reddeder.

    **Kira süresi koşula girmez.** Dolmuş bir kira, sahibinin sonucu yazmasını
    engellememelidir: yazılacak şey geçmişte tamamlanmış bir çalıştırmanın
    sonucudur ve onu düşürmek, bilinen bir sonucu bilinmeyene çevirirdi. Aynı
    nedenle burada takeover veya kira canlandırma da **yoktur**: satırı başka
    bir süreç devraldıysa ``worker_id`` artık çağıranın değildir, satırı bir
    başkası terminalize ettiyse ``status`` artık ``running`` değildir; her iki
    hâlde de ``UPDATE`` sıfır satır etkiler.

    Args:
        session: Aktif veritabanı session'ı. Fonksiyon çıkışında — başarı,
            no-op veya hata — açık transaction bırakılmaz.
        job_id: Bitirilecek Job'ın canonical UUID4 kimliği.
        worker_id: Sonucu yazan, satırın sahibi olduğunu iddia eden worker.
        status: :attr:`JobStatus.SUCCESSFUL` veya :attr:`JobStatus.FAILED`.
        return_code: Başarıda tam olarak ``0``; başarısızlıkta ``int`` veya
            ``None`` (``bool`` kabul edilmez).
        error_code: Başarıda ``None``; başarısızlıkta
            :data:`FINISH_ERROR_CODES` üyesi.
        artifact_path: ``jobs/<job_id>/result.json`` ya da — yalnız
            başarısızlıkta — ``None``.
        result_truncated: Gerçek ``bool``; başarıda ``False`` olmak zorundadır.
        now: Test edilebilirlik için karar anı; timezone-aware olmalıdır.

    Returns:
        Sonuç yazıldıysa ``True``; hiçbir satır etkilenmediyse ``False``.
        ``False`` bir hata değil, açık bir no-op bildirimidir: sahipliğini
        kaybetmiş bir worker'ın yapması gereken, sonucu **yeniden yazmaya
        çalışmamaktır**.

    Raises:
        ValueError: Kimlikler canonical UUID4 değilse, ``status`` terminal
            değilse, ``now`` naive ise veya sonuç alanları aşağıdaki
            invariantları ihlal ediyorsa. Bu yolda veritabanına **hiç**
            dokunulmaz.
        SQLAlchemyError: ``UPDATE`` veya commit veritabanı arızasıyla düşerse.
            Rollback edilir ve hata yeniden yükselir; sessizce ``False``
            dönülmez.
    """
    job = _require_uuid4(job_id, "Job kimliği canonical UUID4 olmalıdır.")
    worker = _require_uuid4(worker_id, "Worker kimliği canonical UUID4 olmalıdır.")
    terminal = _require_terminal_status(status)
    _require_result_fields(
        job_id=job,
        status=terminal,
        return_code=return_code,
        error_code=error_code,
        artifact_path=artifact_path,
        result_truncated=result_truncated,
    )
    moment = _require_moment(now)

    try:
        result = session.execute(
            update(Job)
            .where(
                Job.id == job,
                Job.job_type == JobType.PLAYBOOK,
                Job.status == JobStatus.RUNNING,
                Job.worker_id == worker,
            )
            .values(
                status=terminal,
                finished_at=moment,
                return_code=return_code,
                error_code=error_code,
                artifact_path=artifact_path,
                result_truncated=result_truncated,
                worker_id=None,
                heartbeat_at=None,
                lease_expires_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if _rowcount(result) != 1:
            session.rollback()
            return False
        # Commit de aynı `try` içindedir: bir kısıt ihlali her zaman `execute`
        # anında görünmez ve yakalanmamış bir commit hatası session'ı çağırana
        # kirli bırakırdı.
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise
    return True


def reconcile_stale_playbook_jobs(session: Session, *, now: datetime | None = None) -> int:
    """Kirası dolmuş ``running`` PLAYBOOK Job'larını terminal ``failed`` yapar.

    Çökmüş, SIGKILL almış veya restart edilmiş bir worker'ın arkasında bıraktığı
    satır kimseye ait değildir ama veritabanında hâlâ ``running`` görünür. Global
    aktif PLAYBOOK sınırı 1 olduğu için böyle tek bir satır bütün playbook
    kuyruğunu süresiz tıkar; kullanıcıya da bitmeyen bir çalıştırma olarak
    görünür.

    **Stale kararının tek yetkisi kira süresidir**: ``lease_expires_at <= now``.
    ``UPDATE``'in diğer iki koşulu (``job_type``, ``status``) hangi satırların bu
    yaşam döngüsüne ait olduğunu söyler — ``pending``, terminal ve PING satırların
    kirası veritabanı kısıtları gereği zaten ``NULL``'dur, dolayısıyla o koşullar
    aynı invariantın ikinci savunmasıdır. Bir satırın "dolu" sayılmasına yol açan
    başka hiçbir gerekçe **yoktur**. Özellikle "satırın ``worker_id``'si açılıştaki
    yeni worker'ın kimliğinden farklı" tek başına **stale kanıtı sayılmaz**:
    aynı veritabanına bakan ikinci bir canlı backend süreci olabilir ve onun
    çalışan Job'ını kapatmak, hâlâ süren bir execution'ın kaydını yalan hâle
    getirirdi. Kirası gelecekte olan bir satıra bu yüzden hiç dokunulmaz.

    Sınır **kesindir**: ``lease_expires_at == now`` stale kabul edilir. Bu,
    :func:`heartbeat_playbook_job`'ın aynı sınırdaki "yenilenemez" sözleşmesinin
    diğer yarısıdır; sınır iki tarafta farklı olsaydı tam o anda duran bir satır
    ya iki kez sahiplenilir ya da hiç kimse tarafından kapatılamazdı.

    Geçiş tek koşullu bir ``UPDATE`` ve tek bir commit'tir. Önce aday ``SELECT``
    edip sonra koşulsuz yazmak, okuma ile yazma arasındaki pencerede kirasını
    yenilemiş **canlı** bir Job'ı kapatırdı: koşul ifadenin içinde durduğu için
    o satır ``UPDATE`` anında artık eşleşmez ve dokunulmadan kalır.

    Satır ``pending``'e **döndürülmez**. Onaylanmış tek bir çalıştırma isteğini
    kullanıcının haberi olmadan ikinci kez çalıştırmak, ürünün "AI/otomasyon
    onaysız execution üretmez" kuralının doğrudan ihlalidir; yeniden çalıştırma
    kararı kullanıcınındır.

    Args:
        session: Aktif veritabanı session'ı. Fonksiyon çıkışında — başarı, boş
            sonuç veya hata — açık transaction bırakılmaz.
        now: Test edilebilirlik için karar anı; timezone-aware olmalıdır.
            Verilmezse UTC şimdisi kullanılır.

    Returns:
        Bu çağrıda gerçekten terminal yapılan satır sayısı. Hiçbir stale satır
        yoksa ``0``; ikinci bir çağrı da ``0`` döner, çünkü kapatılan satır
        artık ``running`` değildir.

    Raises:
        ValueError: ``now`` naive ise veya UTC offset'i çözülemiyorsa. Bu yolda
            veritabanına **hiç** dokunulmaz.
        SQLAlchemyError: ``UPDATE`` veya commit veritabanı arızasıyla düşerse.
            Rollback edilir ve hata olduğu gibi yeniden yükselir; arıza "0 satır
            uzlaştırıldı" diye gizlenmez.
    """
    moment = _require_moment(now)

    try:
        result = session.execute(
            update(Job)
            .where(
                Job.job_type == JobType.PLAYBOOK,
                Job.status == JobStatus.RUNNING,
                Job.lease_expires_at <= moment,
            )
            .values(
                status=JobStatus.FAILED,
                error_code=ERROR_INTERRUPTED_BY_RESTART,
                finished_at=moment,
                return_code=None,
                artifact_path=None,
                result_truncated=False,
                # `started_at` bilinçli olarak yazılmaz: çalıştırmanın gerçekten
                # başladığı an kaydın bir parçasıdır ve kesilme onu geçersiz
                # kılmaz. Sahiplik alanları ise aynı ifadede boşaltılır; terminal
                # bir satırda duran kirayı `ck_jobs_idle_playbook_has_no_lease`
                # zaten reddeder.
                worker_id=None,
                heartbeat_at=None,
                lease_expires_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        reconciled = _rowcount(result)
        # Commit koşulsuzdur ve aynı `try` içindedir: boş bir ``UPDATE``'in
        # açtığı transaction da kapatılmalıdır ve bir kısıt ihlali her zaman
        # `execute` anında görünmez.
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise
    return reconciled


def _require_terminal_status(status: JobStatus) -> JobStatus:
    """Yalnız ``JobStatus.SUCCESSFUL``/``FAILED`` **üyesini** kabul eder.

    Tür kontrolü küme üyeliğinden ayrı ve ondan **önce** durur.
    :class:`JobStatus` bir :class:`~enum.StrEnum` olduğu için ham ``"successful"``
    dizgisi üyeye eşit sayılır ve tek başına bir ``in`` kontrolünü geçerdi. O
    yol, durumun tip sisteminden değil çağıranın elindeki serbest metinden
    gelmesine izin verirdi: bir yerde yapılan yazım hatası veya dışarıdan gelen
    bir değer, ``JobStatus`` genişletildiğinde sessizce yeni bir anlam kazanır
    ve sözleşmenin dışında kalırdı.

    Reddedilen değer hata mesajına yazılmaz.
    """
    if not isinstance(status, JobStatus) or status not in _TERMINAL_STATUSES:
        raise ValueError("Job yalnız successful veya failed olarak bitirilebilir.")
    return status


def _require_result_fields(
    *,
    job_id: str,
    status: JobStatus,
    return_code: int | None,
    error_code: str | None,
    artifact_path: str | None,
    result_truncated: bool,
) -> None:
    """Sonuç alanlarının durum başına invariantını doğrular.

    Doğrulama **veritabanına dokunmadan** yapılır: geçersiz bir sonuç, kısmen
    yazılıp sonra geri alınacak bir satır değil, hiç başlamamış bir geçiş
    olmalıdır.

    İki tür kontrol vardır. Birincisi *tutarlılık*: ``successful`` bir sonuç
    sıfır olmayan bir ``return_code``, bir hata kodu veya kırpılmış bir sonuç
    taşıyamaz — böyle bir satır, kaydın kendisiyle çelişen bir "başarı"
    olurdu. İkincisi *içerik*: hata kodu sabit sözlükten, artifact path'i ise
    **tam olarak** bu Job'a ait tek geçerli konumdan gelmelidir.

    ``bool`` bir ``int`` alt sınıfı olduğu için ``return_code`` ayrıca tür
    olarak reddedilir: ``True`` sessizce ``1``, ``False`` sessizce ``0`` diye
    yazılırdı ve ikincisi başarısız bir çalıştırmayı başarı gibi gösterirdi.
    """
    if not isinstance(result_truncated, bool):
        raise ValueError("result_truncated gerçek bir bool olmalıdır.")
    if isinstance(return_code, bool) or not isinstance(return_code, int | None):
        raise ValueError("return_code int veya None olmalıdır.")

    # Path karşılaştırması **eşitliktir**, bir desen eşleşmesi değil. Absolute
    # path, `..` bileşeni, başka bir Job kimliği ve başka bir dosya adı böylece
    # tek bir kontrolle reddedilir; `job_id` de bu noktada canonical UUID4
    # olarak doğrulanmıştır, dolayısıyla beklenen dizgi traversal taşıyamaz.
    expected_artifact = _ARTIFACT_TEMPLATE.format(job_id=job_id)

    if status is JobStatus.SUCCESSFUL:
        if return_code != 0:
            raise ValueError("Başarılı sonuç yalnız sıfır return_code taşıyabilir.")
        if error_code is not None:
            raise ValueError("Başarılı sonuç hata kodu taşıyamaz.")
        if artifact_path != expected_artifact:
            raise ValueError("Başarılı sonuç bu Job'a ait yayımlanmış sonucu göstermelidir.")
        if result_truncated:
            # Kırpılmış bir sonuç, kaydın eksik olduğunun kabulüdür; onu
            # `successful` diye yazmak eksik bir sonucu tam sonuç gibi
            # gösterirdi. Doğru temsil `failed` + `result_limit_exceeded`'dır.
            raise ValueError("Kırpılmış sonuç başarılı olarak kaydedilemez.")
        return

    if error_code not in FINISH_ERROR_CODES:
        raise ValueError("Başarısız sonuç bilinen bir hata kodu taşımalıdır.")
    if artifact_path is not None and artifact_path != expected_artifact:
        raise ValueError("Sonuç yalnız bu Job'a ait yayımlanmış sonucu gösterebilir.")


def _fail_binding(session: Session, *, job_id: str, now: datetime) -> AcquireResult:
    """Bağı geçersiz Job'ı tek transaction'da terminal ``failed`` yapar.

    Koşul yine ``pending``'dir: bu arada başka bir worker Job'ı almışsa hiçbir
    satır etkilenmez ve sonuç sıradan bir kaybedilmiş yarıştır. Sahiplik
    alanları açıkça boşaltılır — terminal bir satırda duran kira,
    ``ck_jobs_idle_playbook_has_no_lease`` tarafından zaten reddedilir.
    """
    try:
        result = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.job_type == JobType.PLAYBOOK,
                Job.status == JobStatus.PENDING,
            )
            .values(
                status=JobStatus.FAILED,
                error_code=ERROR_EXECUTION_BINDING_INVALID,
                finished_at=now,
                return_code=None,
                artifact_path=None,
                result_truncated=False,
                worker_id=None,
                heartbeat_at=None,
                lease_expires_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if _rowcount(result) != 1:
            session.rollback()
            return _IDLE
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise
    return _BINDING_INVALID


def _binding_is_valid(candidate: Row[Any], plan: Row[Any]) -> bool:
    """Job ile planın aynı yetkilendirmeyi tarif edip etmediğini söyler.

    Tek bir ``bool`` döner: hangi alanın uyuşmadığı çağırana bildirilmez ve
    hiçbir yere yazılmaz. Ayrım yapmak, geçersiz bir satırın hangi bağlama ait
    olduğunu deneme yanılmayla öğrenilebilir kılardı.
    """
    plan_status: ExecutionPlanStatus = plan.status
    job_project: int | None = candidate.project_id
    job_inventory: int = candidate.inventory_id
    job_playbook: str | None = candidate.playbook_path
    job_actor: str = candidate.requested_by
    job_limit: str | None = candidate.limit_pattern
    job_mode: ExecutionMode = candidate.mode
    plan_mode: ExecutionMode = plan.mode
    workspace_id: str = plan.workspace_id
    manifest_digest: str = plan.manifest_digest
    return (
        plan_status is ExecutionPlanStatus.CLAIMED
        and job_limit is None
        and job_mode == plan_mode
        and job_project == plan.project_id
        and job_inventory == plan.inventory_id
        and job_playbook == plan.playbook_path
        and job_actor == plan.requested_by
        and _is_uuid4(workspace_id)
        and _DIGEST_PATTERN.fullmatch(manifest_digest) is not None
    )


def _require_moment(now: datetime | None) -> datetime:
    """Karar anını UTC'ye normalize eder; naive değeri reddeder.

    Naive bir değer, yerel saat ile UTC arasındaki farkı sessizce kiraya
    yazardı: saat farkı kadar erken dolan (veya hiç dolmayan) bir kira, stale
    recovery'yi tümüyle yanıltır.
    """
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Karar anı timezone-aware olmalıdır.")
    return now.astimezone(UTC)


def _require_lease(lease_seconds: float) -> timedelta:
    """Kira süresini doğrular. ``NaN``/sonsuz/sıfır/negatif reddedilir.

    Sıfır uzunluklu bir kira, yazıldığı anda dolmuş sayılırdı ve
    ``ck_jobs_running_playbook_lease_outlives_heartbeat`` tarafından zaten
    reddedilirdi; kontrol veritabanına gitmeden burada yapılır.
    """
    if not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("Lease süresi pozitif ve sonlu olmalıdır.")
    if lease_seconds > MAX_LEASE_SECONDS:
        raise ValueError("Lease süresi izin verilen üst sınırı aşıyor.")
    return timedelta(seconds=lease_seconds)


def _require_uuid4(value: str, message: str) -> str:
    """Kimliğin canonical UUID4 olduğunu doğrular; değeri hataya yazmaz."""
    if not _is_uuid4(value):
        raise ValueError(message)
    return value


def _is_uuid4(value: str) -> bool:
    """Değer, uygulamanın ürettiği kanonik UUID4 biçiminde mi?"""
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _rowcount(result: object) -> int:
    """``UPDATE`` sonucunun etkilenen satır sayısı."""
    return int(getattr(result, "rowcount", 0))
