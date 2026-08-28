"""Public çalıştırma yüzeyinin dar domain servisi (R1-V3D1).

Bu modül **yeni bir yetkilendirme mantığı kurmaz**. Atomik claim + Job
rezervasyonu (:func:`claim_and_reserve_playbook_job`) olduğu gibi kalır; burada
yapılan tek şey, bir HTTP route'unun çağırabileceği kadar **dar** bir yüzey
tanımlamaktır. Onu çağıran route artık vardır
(``POST /api/projects/{project_id}/executions``) ve bu yüzeyin darlığı o
route'un sözleşmesidir; route kendi fingerprint, claim veya transaction
mantığını yazmaz.

Yüzeyin dar olması iki somut kısıttır:

1. *İstemci fingerprint gönderemez.* Özet çağırandan alınsaydı, "beklenen
   girdi" iddiası istemcinin kendi beyanı olurdu ve claim koşulunun bağladığı
   şey plan değil, isteğin kendisi hâline gelirdi. Özet bu yüzden
   :func:`input_fingerprint` ile **sunucuda** kurulur.
2. *İstemci çalıştırma parametresi gönderemez.* ``connection``, ``become``,
   ``limit``, ``tags`` ve ``skip_tags`` imzada hiç yoktur ve planın kendi
   sabitlerinden (:mod:`app.services.execution.plan`) okunur; bu alanların
   bir "varsayılan" değil **sabit** olması gerekir, aksi hâlde ileride sessizce
   ezilebilirlerdi.

``mode`` bu kuralın **istisnasıdır** (R1-V3H2A): istemci artık kip
söyleyebilir, ama söylediği değer Job'a yazılan değil yalnızca **beklenen**
kiptir — özete ve alt servisin claim koşuluna ayrı bir alan olarak geçer. Check
için hazırlanmış bir bilet ancak check ile çalıştırmayla eşleşir, normal için
hazırlanmış bir bilet ancak normal ile; yanlış kiple gelen istek hiçbir satırı
eşleştirmez ve token **tüketilmez**. Dönen :class:`AuthorizedPlaybookJob`
üzerindeki ``mode`` de çağıranın verdiği bu beklenti değil, **claim edilen
plan satırının** kipidir; façade kipi Job'a kendisi yazmaz.

``project_id``, ``inventory_id``, ``playbook_path`` ve ``host_key_policy``
özete girer; dolayısıyla hazırlama anındaki politika ile çalıştırma anındaki
politika ayrıştığında token hiçbir satırı eşleştirmez ve **tüketilmez**.

``requested_by`` çağırandan alınır: route bunu istekten değil
:attr:`Settings.local_actor` değerinden verir (hazırlama yolundaki sözleşmenin
aynısı).

Bu façade dondurulmuş içerik doğrulamasını ve transaction sınırını **tekrar
yazmaz**; alt servisin sözleşmesi neyse odur. Tek istisnası arıza sınırıdır:
bir ``SQLAlchemyError``'ı public-safe bir 503'e çevirmeden önce savunma amaçlı
**koşulsuz** rollback yapar, böylece claim ve Job rezervasyonunun hiçbir
aşamasında açık/failed bir transaction dışarı çıkmaz.

Runner, subprocess, SSH, worker ve artifact katmanlarına hiç dokunmaz: burada
üretilen Job ``pending`` durumunda durur.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import ExecutionMode
from app.services.execution.authorize import (
    AuthorizedPlaybookJob,
    claim_and_reserve_playbook_job,
)
from app.services.execution.plan import (
    PLAN_BECOME,
    PLAN_CONNECTION,
    PLAN_LIMIT,
    PLAN_SKIP_TAGS,
    PLAN_TAGS,
)
from app.services.execution.store import input_fingerprint


class ExecutionLaunchUnavailableError(AppError):
    """Rezervasyon veritabanı arızası yüzünden yapılamadı.

    :class:`ExecutionPlanInvalidError`'dan bilinçli olarak **ayrıdır**: orada
    reddedilen isteğin kendisidir ve tekrar denemek aynı sonucu verir; burada
    reddedilen hiçbir şey yoktur, yalnızca depolama şu an cevap verememiştir.
    İkisini tek koda katlamak, geçici bir arızayı "planınız geçersiz" diye
    gösterip kullanıcıyı gereksizce yeniden hazırlamaya iterdi.

    Dışarı verilen mesaj sabittir: veritabanı hata metnini, token'ı, workspace
    yolunu, digest'i veya Job kimliğini taşımaz. Özgün arıza ``__cause__``
    üzerinden zincirlenir ve orada kalır.
    """

    status_code = 503
    code = "execution_launch_unavailable"


_UNAVAILABLE_MESSAGE = "Çalıştırma şu anda başlatılamıyor. Lütfen biraz sonra tekrar deneyin."


def launch_prepared_playbook_job(
    session: Session,
    *,
    token: str,
    mode: ExecutionMode,
    project_id: int,
    inventory_id: int,
    playbook_path: str,
    requested_by: str,
    workspace_root: Path,
    host_key_policy: str,
) -> AuthorizedPlaybookJob:
    """Hazırlanmış planı tüketip ``pending`` PLAYBOOK Job'ını rezerve eder.

    Beklenen girdi özeti çağırandan **alınmaz**, burada kurulur: sabit plan
    parametreleri ile isteğin bağlamı (project, inventory, playbook, host key
    politikası, beklenen ``mode``) birleştirilir. Ardından atomik claim +
    rezervasyon servisi çağrılır; transaction sınırı, dondurulmuş içerik
    doğrulaması ve rollback davranışı o servise aittir.

    Args:
        session: Aktif veritabanı session'ı.
        token: Hazırlama cevabında bir kez dönen raw plan token'ı.
        mode: İstemcinin beklediği çalıştırma kipi (R1-V3H2A). Yalnız
            fingerprint'e ve claim koşuluna girer; dönen nesnenin ``mode``
            alanı bundan değil claim edilen plan satırından gelir.
        project_id: Beklenen project kimliği.
        inventory_id: Beklenen inventory kimliği.
        playbook_path: Beklenen, project köküne göreli playbook yolu.
        requested_by: Geçerli aktör (:attr:`Settings.local_actor`).
        workspace_root: ``app-data/execution-plans`` kökü.
        host_key_policy: Yürürlükteki SSH host key politikası
            (:attr:`Settings.ssh_host_key_policy`).

    Returns:
        Oluşturulan Job'u ve claim edilen planı tanıtan
        :class:`AuthorizedPlaybookJob`. Raw token bu nesnede **bulunmaz**.

    Raises:
        ExecutionPlanInvalidError: Token biçimsiz, bilinmiyor, süresi geçmiş,
            bağlamla/aktörle/politikayla eşleşmiyor, kullanılmış ya da
            dondurulmuş içerik artık onaylanan içerik değil. Hata olduğu gibi
            dışarı geçer; sızdırmayan tek kodlu sözleşmesi korunur.
        ExecutionLaunchUnavailableError: Rezervasyon veritabanı arızasıyla
            düştü. Hata public-safe cevaba çevrilmeden **önce** savunma amaçlı
            koşulsuz bir rollback yapılır: plan ``prepared`` kalır, token
            yeniden kullanılabilir, orphan Job oluşmaz ve session çağırana
            açık/failed transaction ile dönmez.
    """
    fingerprint = input_fingerprint(
        project_id=project_id,
        inventory_id=inventory_id,
        playbook_path=playbook_path,
        mode=mode,
        connection=PLAN_CONNECTION,
        become=PLAN_BECOME,
        limit=PLAN_LIMIT,
        tags=PLAN_TAGS,
        skip_tags=PLAN_SKIP_TAGS,
        host_key_policy=host_key_policy,
    )

    try:
        return claim_and_reserve_playbook_job(
            session,
            token=token,
            project_id=project_id,
            inventory_id=inventory_id,
            playbook_path=playbook_path,
            fingerprint=fingerprint,
            mode=mode,
            requested_by=requested_by,
            workspace_root=workspace_root,
        )
    except SQLAlchemyError as exc:
        # Yalnız veritabanı arızası daraltılır. `ExecutionPlanInvalidError` ve
        # diğer domain hataları bilinçli olarak yakalanmaz: onları da 503'e
        # katlamak, reddedilmiş bir isteği "sonra tekrar deneyin" diye
        # gösterirdi.
        #
        # Rollback **koşulsuzdur**. Alt servisin her arızada rollback ettiğini
        # varsaymak yanlış olurdu: Job flush'ı ve final commit kendi rollback
        # bloklarına sahiptir, ama claim UPDATE/SELECT'inin kendisi SQL
        # seviyesinde düşerse hata o blokların hiçbirine uğramadan yukarı
        # çıkar ve transaction açık kalır. Arızayı burada public-safe bir
        # cevaba çevirip transaction'ı açık bırakmak, daraltılmış hatanın
        # sınırını isteğin dışına taşırdı: session çağırana tanımsız bir
        # transaction sınırıyla döner, açık kalan yazma kilidi sonraki isteği
        # bekletir ve (arızanın çıktığı aşamaya göre) transaction failed
        # durumdaysa bir sonraki sorgu doğrudan düşerdi. İkinci bir rollback
        # ise zararsızdır.
        session.rollback()
        raise ExecutionLaunchUnavailableError(
            _UNAVAILABLE_MESSAGE,
            details={"reason": "unavailable"},
        ) from exc
