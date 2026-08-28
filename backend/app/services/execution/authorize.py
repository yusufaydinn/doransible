"""Plan claim'i ile PLAYBOOK Job rezervasyonunu bağlayan tek servis (R1-V3A).

R1-V2'de plan token'ı tek başına tüketilebiliyordu: claim commit ediliyor,
karşılığında hiçbir kalıcı iz kalmıyordu. Bu dilim o boşluğu kapatır — **bir
token'ın tek geçerli sonucu bir Job'dur.** İkisi aynı transaction'da kesinleşir;
biri olup diğeri olmaz.

Bu dilimde yine **hiçbir şey çalıştırılmaz**: `ansible-runner` çağrılmaz,
`ansible-playbook` başlatılmaz, SSH bağlantısı kurulmaz, alt süreç açılmaz,
artifact üretilmez ve arka plan işçisi yoktur. Üretilen Job ``pending``
durumunda durur; onu ``running`` yapan bir yol bu modülde **yoktur**. R1-V3D1'den
beri bu servisi dolaylı olarak çağıran bir public endpoint vardır
(``POST /api/projects/{project_id}/executions`` →
:func:`~app.services.execution.launch.launch_prepared_playbook_job`); sözleşme
değişmez, çünkü o yol da buradaki tek transaction'ı kullanır ve kendi claim
mantığını yazmaz.

Sıra bilinçlidir::

    token biçimi
    → atomik claim (UPDATE, commit **yok**)
    → dondurulmuş içeriğin yeniden doğrulanması
    → Job INSERT + flush
    → tek commit

**Neden claim önce, doğrulama sonra?** Doğrulamayı öne almak, aynı token'la
sınırsız sayıda "içerik hâlâ duruyor mu" sondası çekilmesine izin verirdi.
Claim'i öne almak ise pahalı doğrulamayı yalnızca gerçekten kazanan çağrı için
çalıştırır ve yarışı tek bir atomik ifadede çözer.

**Neden doğrulama başarısızlığında token tüketilir?** İçeriği değişmiş bir
workspace'in planı artık kullanıcının onayladığı planı temsil etmez. Bileti
yeniden claim edilebilir bırakmak, saldırganın içeriği değiştirip yeniden
denemesine kapı açardı; bu yüzden claim rollback edilir ama plan ayrı bir
ifadeyle ``expired`` yapılır — Job oluşmaz, token da geri gelmez.

**Neden Job hatasında token geri gelir?** Orada onaylanan içerik sağlamdır;
başarısız olan yalnızca rezervasyondur. Rollback claim'i de Job'u da geri alır:
plan ``prepared`` kalır, kullanıcı aynı token'la yeniden deneyebilir ve arkada
orphan bir Job kalmaz.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import ExecutionMode, Job, JobStatus, JobType
from app.services.execution.store import (
    ExecutionPlanInvalidError,
    claim_plan_row,
    expire_plan_by_token,
)
from app.services.execution.workspace import (
    WorkspaceIntegrityError,
    WorkspaceUnavailableError,
    WorkspaceUnsafeError,
    verify_frozen_workspace,
)

# Dondurulmuş içerik doğrulamasının fail-closed saydığı bütün arızalar. Üçü de
# aynı sonucu doğurur: Job yok, plan expired. Ayrım yapmak, dışarıdan "içerik
# değişti mi yoksa kayıp mı" sorusunu yanıtlanabilir kılardı.
_WORKSPACE_FAILURES = (
    WorkspaceIntegrityError,
    WorkspaceUnavailableError,
    WorkspaceUnsafeError,
)


@dataclass(frozen=True)
class AuthorizedPlaybookJob:
    """Claim edilmiş plana bağlanmış, ``pending`` PLAYBOOK Job'ı.

    Absolute path taşımaz; tüketici dondurulmuş içeriği ``workspace_id``
    üzerinden açar. Raw token bu nesnede de **bulunmaz**.
    """

    job_id: str
    plan_id: str
    workspace_id: str
    manifest_digest: str
    project_id: int
    inventory_id: int
    playbook_path: str
    requested_by: str
    # Job'un execution mode'u. Kaynağı çağıranın verdiği beklenen kip değil,
    # **claim edilen plan satırıdır** (R1-V3H1B1); değer Job'a yazılanın
    # aynısıdır.
    mode: ExecutionMode
    claimed_at: datetime


def claim_and_reserve_playbook_job(
    session: Session,
    *,
    token: str,
    project_id: int,
    inventory_id: int,
    playbook_path: str,
    fingerprint: str,
    mode: ExecutionMode,
    requested_by: str,
    workspace_root: Path,
    now: datetime | None = None,
) -> AuthorizedPlaybookJob:
    """Planı claim eder ve **aynı transaction'da** PLAYBOOK Job'ı rezerve eder.

    Job kimliği rezervasyondan **önce** üretilir (ping akışıyla aynı yaklaşım):
    ileride artifact dizini ile veritabanı satırının aynı kimliği taşıması ve
    rezervasyon başarısız olduğunda hangi dizinin temizleneceği ancak böyle
    kesinleşir.

    Job'un bağlayıcı alanları istekten değil **plandan** alınır: istemcinin
    gönderdiği project/inventory/playbook değerleri yalnızca claim koşulunda
    eşleşme için kullanılır, Job'a yazılan değerler kayıttan okunur. Böylece
    Job, kullanıcının onayladığı planın kendisini tarif eder.

    **Execution mode da bu kuralın istisnası değildir (R1-V3H1B1).** ``mode``
    parametresi yalnızca *beklenen* kiptir ve tek işi claim koşuluna girmektir;
    Job'a yazılan değer claim edilen satırdan okunur (``record.mode``). Caller'ın
    değerini Job'a kopyalamak, koşul ile yazılan değeri aynı kaynaktan besleyip
    kontrolü kendi kendini doğrulayan bir tekrara indirgerdi: eşleşme sağlansa
    da sağlanmasa da Job caller'ın istediği kipi taşırdı. ORM/DB varsayılanına
    bırakmak ise daha kötüsünü yapardı — plan ``normal`` için hazırlanmış olsa
    bile Job sessizce ``check`` olurdu ve ikisi ayrışırdı. Kip, planın kalıcı
    parçasıdır; Job onu **miras alır**, yeniden beyan etmez.

    Args:
        session: Aktif veritabanı session'ı.
        token: Hazırlama cevabında bir kez dönen raw plan token'ı.
        project_id: Beklenen project kimliği.
        inventory_id: Beklenen inventory kimliği.
        playbook_path: Beklenen, project köküne göreli playbook yolu.
        fingerprint: Beklenen girdi özeti.
        mode: Beklenen execution mode. Yalnız claim koşuluna girer; Job'a
            yazılan kip claim edilen plan satırından okunur.
        requested_by: Geçerli aktör (:attr:`Settings.local_actor`).
        workspace_root: ``app-data/execution-plans`` kökü.
        now: Test edilebilirlik için karar anı.

    Returns:
        Oluşturulan Job'u ve claim edilen planı tanıtan
        :class:`AuthorizedPlaybookJob`.

    Raises:
        ExecutionPlanInvalidError: Token biçimsiz, bilinmiyor, süresi geçmiş,
            bağlamla, kiple veya aktörle eşleşmiyor, kullanılmış ya da
            dondurulmuş içerik artık onaylanan içerik değil. Bütün durumlar tek
            kodla döner ve hata token, path veya digest içeriği taşımaz. Yanlış
            kiple yapılan deneme de diğer eşleşmezlikler gibi token'ı
            **tüketmez**.
        SQLAlchemyError: Job rezervasyonu veritabanı arızasıyla başarısız
            olursa. Bu yolda claim rollback edilir; token yeniden kullanılabilir
            kalır ve orphan Job oluşmaz.
    """
    moment = now or datetime.now(UTC)
    job_id = str(uuid.uuid4())

    try:
        record = claim_plan_row(
            session,
            token=token,
            project_id=project_id,
            inventory_id=inventory_id,
            playbook_path=playbook_path,
            fingerprint=fingerprint,
            mode=mode,
            requested_by=requested_by,
            now=moment,
        )
    except ExecutionPlanInvalidError:
        # Hiçbir satır eşleşmedi: token tüketilmedi. Session çağırana
        # kullanılabilir durumda bırakılır.
        session.rollback()
        raise

    plan_id = record.id
    workspace_id = record.workspace_id
    manifest_digest = record.manifest_digest
    plan_project_id = record.project_id
    plan_inventory_id = record.inventory_id
    plan_playbook_path = record.playbook_path
    plan_actor = record.requested_by
    # Kip claim edilen satırdan okunur; sütun tipi zaten `ExecutionMode` olduğu
    # için burada bir dönüşüm veya coercion yapılmaz.
    plan_mode = record.mode

    try:
        verify_frozen_workspace(workspace_root, workspace_id, expected_digest=manifest_digest)
    except _WORKSPACE_FAILURES as exc:
        # Claim geri alınır, ardından plan ayrı bir ifadeyle expired yapılır:
        # Job oluşmaz ve token yeniden kullanılamaz.
        session.rollback()
        expire_plan_by_token(session, token=token)
        raise _invalid_plan() from exc

    session.add(
        Job(
            id=job_id,
            job_type=JobType.PLAYBOOK,
            status=JobStatus.PENDING,
            # Kipin kaynağı caller da, sütun varsayılanı da değil; **plan
            # kaydıdır**. Job onaylanan planın kipini taşır.
            mode=plan_mode,
            execution_plan_id=plan_id,
            inventory_id=plan_inventory_id,
            project_id=plan_project_id,
            playbook_path=plan_playbook_path,
            # `limit` bu dilimde kapsam dışıdır ve plana da açıkça `null`
            # yazılır; Job da onu taşımaz.
            limit_pattern=None,
            requested_by=plan_actor,
            artifact_path=None,
            return_code=None,
            started_at=None,
            finished_at=None,
            created_at=moment,
        )
    )
    try:
        session.flush()
        # Tek commit: claim ile Job aynı anda kalıcı olur. Commit de aynı
        # `try` içindedir: bir kısıt ihlali flush yerine commit anında
        # yüzeye çıkabilir (deferred kontroller, dialect farkları) ve
        # yakalanmamış bir commit hatası session'ı çağırana kirli bırakırdı.
        session.commit()
    except SQLAlchemyError:
        # Rollback claim UPDATE'ini de Job INSERT'ini de geri alır: plan
        # `prepared` kalır, token yeniden kullanılabilir, orphan Job yoktur.
        session.rollback()
        raise

    return AuthorizedPlaybookJob(
        job_id=job_id,
        plan_id=plan_id,
        workspace_id=workspace_id,
        manifest_digest=manifest_digest,
        project_id=plan_project_id,
        inventory_id=plan_inventory_id,
        playbook_path=plan_playbook_path,
        requested_by=plan_actor,
        mode=plan_mode,
        claimed_at=moment,
    )


def _invalid_plan() -> ExecutionPlanInvalidError:
    """Dondurulmuş içerik doğrulaması için ortak, sızdırmayan hata.

    Mesaj ve ``details`` ne token'ın bir parçasını, ne workspace yolunu, ne de
    digest içeriğini taşır: hangi kontrolün takıldığı dışarıdan görünmez.
    """
    return ExecutionPlanInvalidError(
        "Hazırlanmış execution planı geçerli değil. Planı yeniden hazırlayın.",
        details={"reason": "invalid"},
    )
