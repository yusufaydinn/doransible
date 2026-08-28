"""Hazırlanmış execution planlarının kalıcı deposu ve atomik claim'i (R1-V2).

Ping akışının :mod:`app.services.jobs.preview` deposu bilinçli olarak **yeniden
kullanılmaz ve değiştirilmez**: o depo ping'e özgüdür (tek snapshot dosyası,
inventory kimliği, ping operasyonu) ve tek kullanım garantisini dosya sistemi
``rename``'i üzerinden verir. Buradaki plan ise bir project ağacını, bir
inventory snapshot'ını ve bir playbook seçimini birlikte bağlar; tek kullanım
garantisi **veritabanındaki atomik UPDATE**'tir. İki yaşam döngüsünü tek koda
sıkıştırmak, ikisini de zayıflatırdı.

Token sözleşmesi:

- 256 bit CSPRNG (:func:`secrets.token_bytes`), padding'siz base64url.
- Raw token yalnızca hazırlama cevabında **bir kez** döner.
- Veritabanına yalnızca SHA-256 özeti yazılır; ``token_hash`` unique'tir.
- Token loglanmaz, hata mesajına veya ``details``'e girmez, URL'ye konmaz ve
  hiçbir telemetriye (prefix/substring dâhil) yazılmaz.

Claim sözleşmesi:

- Tek bir ``UPDATE ... WHERE status='prepared' AND expires_at > now AND
  <beklenen girdiler>`` ile yapılır. Karar ile değişiklik aynı ifadededir;
  "önce oku, sonra yaz" penceresi yoktur.
- Yanlış girdi (**başka** project/inventory/playbook/fingerprint/mode/aktör)
  hiçbir satırı eşleştirmez, dolayısıyla token'ı **tüketmez**.
- Süresi geçmiş veya daha önce claim edilmiş token eşleşmez.
- İki eşzamanlı claim'den tam olarak biri kazanır: ikinci UPDATE artık
  ``prepared`` olmayan satırı bulamaz.

**Bu modülde commit eden bir claim yoktur (R1-V3A).** :func:`claim_plan_row`
bilinçli olarak *commit etmez*: claim'in tek geçerli sonucu bir PLAYBOOK Job'ı
rezerve etmektir ve ikisi aynı transaction'da kesinleşmelidir. Önce claim'i
commit edip sonra Job yaratmak, arada bir arıza olduğunda tüketilmiş ama
karşılığı olmayan bir token bırakırdı. Transaction sınırını kuran tek yer
:mod:`app.services.execution.authorize` içindeki
:func:`claim_and_reserve_playbook_job`'dır; plan tüketen bağımsız bir servis
yüzeyi yoktur.

**Token'ı tüketen public bir endpoint R1-V3D1'den beri vardır**
(``POST /api/projects/{project_id}/executions``). Bu modülün sözleşmesi
değişmez: o yol da claim'i buradaki tek atomik UPDATE üzerinden yapar, kendi
"önce oku sonra yaz" penceresini açmaz ve token'ın karşılığı yine tek bir
``pending`` PLAYBOOK Job'dır; plan claim ve Job rezervasyonu tek transaction'dır.
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import (
    ExecutionMode,
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    Job,
    JobStatus,
    JobType,
)
from app.services.execution.workspace import (
    list_stale_staging,
    list_workspace_ids,
    read_maintenance_cursor,
    remove_workspace,
    workspace_age_seconds,
    workspace_exists,
    write_maintenance_cursor,
)

# 256 bit entropi. Base64url (padding'siz) karşılığı tam 43 karakterdir.
TOKEN_BYTES = 32
TOKEN_LENGTH = 43
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")

# Tek bir istekte toplanacak azami terk edilmiş kayıt. Temizlik asıl isteğin
# yanında çalışır; sınırsız olsaydı büyük bir birikim tek bir kullanıcının
# isteğini bekletirdi.
MAX_SWEEP_RECORDS = 20
MAX_RECONCILE_RECORDS = 500

# Bir reconciliation turunda incelenecek azami dizin ve tek sorgudaki azami
# aday sayısı. Sınır **hiçbir zaman** "geri kalanı orphan'dır" anlamına gelmez;
# pencerenin dışında kalan dizinler bir sonraki turda incelenir.
MAX_RECONCILE_WORKSPACES = 500
WORKSPACE_LOOKUP_CHUNK = 100

# Planı **aktif** bir PLAYBOOK Job'ına bağlayan correlated alt sorgu.
#
# Kuyrukta bekleyen veya çalışan bir Job'ın dondurulmuş workspace'i, planın
# TTL'si geçti diye silinemez: TTL bir biletin ne kadar süre *claim edilebilir*
# kaldığını söyler, claim edilmiş bir biletin işi ne kadar sürebileceğini
# değil. Silinseydi, çalışmakta olan bir execution'ın project ağacı ve
# inventory snapshot'ı altından çekilirdi; ``pending`` bir Job ise hiç
# başlayamadan, kullanıcının haberi olmadan çalıştırılamaz hâle gelirdi.
#
# Terminal Job koruma sağlamaz: çalıştırma bitmiştir, dondurulmuş içeriğin
# tutulması için bir sebep kalmaz. PING Job'ları da koruma sağlamaz; onların
# plan kaydı hiç olmaz (``ck_jobs_ping_has_no_execution_plan``) ve tür koşulu
# bu kuralın ileride gevşemesine karşı da açıkça yazılır.
_HAS_ACTIVE_PLAYBOOK_JOB = (
    select(Job.id)
    .where(
        Job.execution_plan_id == ExecutionPlanRecord.id,
        Job.job_type == JobType.PLAYBOOK,
        Job.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
    )
    .exists()
)


class ExecutionPlanInvalidError(AppError):
    """Token bilinmiyor, süresi geçmiş, bağlamla eşleşmiyor veya kullanılmış.

    Dört durum **tek** kodla döner. Ayrım yapmak, geçerli bir token'ın hangi
    project/inventory/playbook üçlüsüne ait olduğunu deneme yanılmayla
    öğrenilebilir kılardı.
    """

    status_code = 409
    code = "execution_plan_invalid"


@dataclass(frozen=True)
class PreparedPlan:
    """Yeni hazırlanmış plan.

    ``token`` yalnızca burada, süreç belleğinde bulunur ve çağıran onu tek bir
    cevaba yazar. Kayda yazılan değer özetidir.
    """

    plan_id: str
    token: str
    workspace_id: str
    manifest_digest: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class CleanupResult:
    """Bir temizlik turunun sonucu."""

    expired_records: int
    removed_workspaces: int
    orphan_workspaces: int
    stale_staging: int
    missing_workspaces: int


def generate_token() -> str:
    """256 bitlik, padding'siz base64url token üretir."""
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode("ascii")


def token_digest(token: str) -> str:
    """Token'ın SHA-256 özeti; veritabanına yazılan tek değer budur."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def input_fingerprint(
    *,
    project_id: int,
    inventory_id: int,
    playbook_path: str,
    mode: ExecutionMode,
    connection: str,
    become: bool,
    limit: str | None,
    tags: str | None,
    skip_tags: str | None,
    host_key_policy: str,
) -> str:
    """Planın bağlandığı değişmez girdilerin özeti.

    Claim anında yeniden hesaplanıp karşılaştırılır: aynı token, başka bir
    çalıştırma parametresi kümesiyle kullanılamaz. Özet, ileride yeni bir alan
    eklendiğinde eski token'ların otomatik olarak eşleşmemesini de sağlar.

    ``mode`` özetin **parçasıdır** (R1-V3H1B1): check mode için hazırlanmış bir
    bilet, aynı project/inventory/playbook üçlüsüyle bile normal mode
    çalıştırmanın beklediği özeti üretemez. Kip iki bağımsız yerde bağlanır —
    burada ve :func:`claim_plan_row`'un okunabilir ``mode`` sütun koşulunda; ne
    biri diğerinin yerine geçer ne de birinin gevşemesi tek başına yanlış kiple
    çalıştırmaya yeter.

    Parametre bilinçli olarak :class:`~app.models.execution_mode.ExecutionMode`
    ister, ham ``str`` değil: serbest bir dizge kabul edilseydi ``"Check"`` veya
    ``"chk"`` gibi bir yazım hatası sessizce **başka** bir özet üretir ve
    kullanıcıya "planınız geçersiz" dedirtirdi. ``mode.value`` de açıkça
    yazılır; canonical gövdeye enum'un ``repr``'i değil, sözleşmedeki değerin
    kendisi girer.
    """
    canonical = json.dumps(
        {
            "project_id": project_id,
            "inventory_id": inventory_id,
            "playbook_path": playbook_path,
            "mode": mode.value,
            "connection": connection,
            "become": become,
            "limit": limit,
            "tags": tags,
            "skip_tags": skip_tags,
            "host_key_policy": host_key_policy,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def store_prepared_plan(
    session: Session,
    *,
    project_id: int,
    inventory_id: int,
    playbook_path: str,
    fingerprint: str,
    mode: ExecutionMode,
    requested_by: str,
    workspace_id: str,
    manifest_digest: str,
    ttl_seconds: float,
    now: datetime | None = None,
) -> PreparedPlan:
    """Hazırlanmış planı kaydeder ve raw token'ı **bir kez** döndürür.

    ``requested_by`` sunucunun kendi ayarından (:attr:`Settings.local_actor`)
    gelir, istekten değil: aktör istemci tarafından seçilebilseydi "aktör bağı"
    yalnızca bir alan kopyalaması olurdu. Değer claim koşulunun parçasıdır ve
    API cevabına çıkmaz.

    ``mode`` zorunludur ve satıra **açıkça** yazılır (R1-V3H1B1). Sütunun bir
    ORM/DB varsayılanı vardır ama buradan ona güvenilmez: varsayılana
    yaslanmak, kipi hiç düşünmeyen bir çağrının sessizce ``check`` yazmasını
    doğru davranış gibi gösterirdi. Yazılacak kipin ne olduğu çağıranın
    bilerek verdiği bir karar olmalıdır; ileride ``normal`` açıldığında
    unutulan bir çağrı sessizce yanlış kipte plan üretmemelidir.

    Yazılan değer aynı zamanda claim koşulunun okunabilir yarısıdır: bilet
    ``fingerprint`` üzerinden zaten kipe bağlıdır, ama satırın kendisi de
    hangi kip için hazırlandığını doğrudan söyler.
    """
    moment = now or datetime.now(UTC)
    expires_at = moment + timedelta(seconds=ttl_seconds)
    token = generate_token()
    record = ExecutionPlanRecord(
        id=str(uuid.uuid4()),
        token_hash=token_digest(token),
        project_id=project_id,
        inventory_id=inventory_id,
        playbook_path=playbook_path,
        input_fingerprint=fingerprint,
        mode=mode,
        requested_by=requested_by,
        workspace_id=workspace_id,
        manifest_digest=manifest_digest,
        status=ExecutionPlanStatus.PREPARED,
        created_at=moment,
        expires_at=expires_at,
    )
    session.add(record)
    session.commit()
    return PreparedPlan(
        plan_id=record.id,
        token=token,
        workspace_id=workspace_id,
        manifest_digest=manifest_digest,
        created_at=moment,
        expires_at=expires_at,
    )


def claim_plan_row(
    session: Session,
    *,
    token: str,
    project_id: int,
    inventory_id: int,
    playbook_path: str,
    fingerprint: str,
    mode: ExecutionMode,
    requested_by: str,
    now: datetime,
) -> ExecutionPlanRecord:
    """Planı atomik olarak ``prepared → claimed`` yapar ve **commit etmez**.

    Beklenen bağlamın tamamı — token özeti, durum, geçerlilik, project,
    inventory, playbook, girdi özeti, **execution mode** ve aktör — tek
    UPDATE'in ``WHERE`` koşulundadır. Karar ile değişiklik aynı ifadede olduğu
    için "önce oku, sonra yaz" penceresi yoktur ve iki eşzamanlı çağrıdan tam
    olarak biri kazanır.

    Yanlış bir bağlamla (**başka** bir aktör veya **başka bir kip** dâhil)
    gelen istek hiçbir satırı eşleştirmez ve token'ı **tüketmez**: kullanıcının
    elindeki geçerli plan yanlış bir denemeyle kaybolmaz. Aynı bilet, doğru
    kiple sonradan hâlâ claim edilebilir.

    ``mode`` koşula ``fingerprint``'e **ek olarak** girer. İkisi aynı bilgiyi
    iki bağımsız biçimde bağlar: özet kipi kriptografik olarak mühürler,
    sütun ise onu doğrudan okunabilir ve sorgulanabilir tutar. Özet
    hesaplamasındaki bir gerileme tek başına yanlış kiple çalıştırmaya
    yetmesin diye ikisi birden aranır.

    Kip ayrımı **ayrı bir hata kodu üretmez**: yanlış kip de diğer bütün
    eşleşmezlikler gibi tek ve genel ``execution_plan_invalid`` ile döner.
    Ayrı bir kod, elindeki token'ın hangi kip için hazırlandığını deneme
    yanılmayla öğrenilebilir kılan bir oracle olurdu.

    Commit **bilinçli olarak yapılmaz**. Bu fonksiyon tek başına çağrılabilir
    bir "planı tüket" servisi değildir; çağıranın (bkz.
    :func:`app.services.execution.authorize.claim_and_reserve_playbook_job`)
    aynı transaction içinde Job'u da rezerve etmesi beklenir.

    Özgün project veya inventory dosyası burada **hiç açılmaz**.

    Raises:
        ExecutionPlanInvalidError: Token biçimsiz, bilinmiyor, süresi geçmiş,
            bağlamla eşleşmiyor ya da kullanılmış.
    """
    if not _TOKEN_PATTERN.fullmatch(token):
        # Biçimsiz token hiçbir sorguya girmez.
        raise _invalid_plan()

    digest = token_digest(token)
    result = session.execute(
        update(ExecutionPlanRecord)
        .where(
            ExecutionPlanRecord.token_hash == digest,
            ExecutionPlanRecord.status == ExecutionPlanStatus.PREPARED,
            ExecutionPlanRecord.expires_at > now,
            ExecutionPlanRecord.project_id == project_id,
            ExecutionPlanRecord.inventory_id == inventory_id,
            ExecutionPlanRecord.playbook_path == playbook_path,
            ExecutionPlanRecord.input_fingerprint == fingerprint,
            ExecutionPlanRecord.mode == mode,
            ExecutionPlanRecord.requested_by == requested_by,
        )
        .values(status=ExecutionPlanStatus.CLAIMED, claimed_at=now)
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) != 1:
        raise _invalid_plan()

    record = session.execute(
        select(ExecutionPlanRecord).where(ExecutionPlanRecord.token_hash == digest)
    ).scalar_one()
    # Özet karşılaştırması sabit zamanlıdır; satır zaten digest ile bulundu ama
    # karşılaştırmayı da zamanlama sızıntısına açık bırakmamak ucuzdur.
    if not hmac.compare_digest(record.token_hash, digest):  # pragma: no cover - savunma
        raise _invalid_plan()
    return record


def expire_plan_by_token(session: Session, *, token: str) -> None:
    """Planı, hangi durumda olursa olsun ``expired`` yapar ve commit eder.

    Dondurulmuş içeriği kaybolmuş veya değişmiş bir plan için kullanılır.
    Token bu noktada **tüketilmiş sayılır**: içeriği artık onaylanan içerik
    olmayan bir bileti yeniden claim edilebilir bırakmak fail-open olurdu.

    Çağıran, claim UPDATE'ini rollback ettikten **sonra** çağırır; bu yüzden
    koşul ``prepared`` ile sınırlanmaz.
    """
    if not _TOKEN_PATTERN.fullmatch(token):  # pragma: no cover - savunma
        return
    session.execute(
        update(ExecutionPlanRecord)
        .where(
            ExecutionPlanRecord.token_hash == token_digest(token),
            ExecutionPlanRecord.status != ExecutionPlanStatus.EXPIRED,
        )
        .values(status=ExecutionPlanStatus.EXPIRED)
        .execution_options(synchronize_session=False)
    )
    session.commit()


def sweep_expired_plans(
    session: Session,
    *,
    workspace_root: Path,
    now: datetime | None = None,
    limit: int = MAX_SWEEP_RECORDS,
) -> CleanupResult:
    """Süresi geçmiş planların workspace'lerini toplar (bounded).

    Kayıt silinmez, ``expired`` işaretlenir: ``claimed_at`` korunur ve "bu plan
    bir kez onaya hazırlandı" izi kaybolmaz. İşaretlenen kayıt bir sonraki
    turda yeniden ele alınmaz, böylece tekrar iş yapılmaz.

    **Aktif bir PLAYBOOK Job'ına bağlı plan, TTL'si geçmiş olsa bile
    korunur** (bkz. :data:`_HAS_ACTIVE_PLAYBOOK_JOB`): ne workspace'i silinir ne
    de ``expired`` işaretlenir. Koruma doğrudan seçim sorgusunun ``WHERE``
    koşulundadır ve ``LIMIT``'ten **önce** uygulanır. Sınırlı bir sayfa çekip
    korumayı Python'da uygulamak iki ayrı hata üretirdi: aday listesi Job
    okumasıyla arasındaki pencerede değişebilir, ve korunan en eski plan
    sayfanın tek yerini işgal ederek arkasındaki gerçekten atıl planların
    süresiz temizlenmeden kalmasına yol açardı.

    Tek tek girdilerdeki hatalar yutulur: temizlik, asıl isteğin başarısını
    engellememelidir.
    """
    moment = now or datetime.now(UTC)
    records = list(
        session.execute(
            select(ExecutionPlanRecord)
            .where(
                ExecutionPlanRecord.expires_at <= moment,
                ExecutionPlanRecord.status != ExecutionPlanStatus.EXPIRED,
                ~_HAS_ACTIVE_PLAYBOOK_JOB,
            )
            .order_by(ExecutionPlanRecord.expires_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    removed = 0
    for record in records:
        try:
            removed += int(remove_workspace(workspace_root, record.workspace_id))
        except OSError:  # pragma: no cover - savunma amaçlı
            continue
        record.status = ExecutionPlanStatus.EXPIRED
    if records:
        session.commit()
    return CleanupResult(
        expired_records=len(records),
        removed_workspaces=removed,
        orphan_workspaces=0,
        stale_staging=0,
        missing_workspaces=0,
    )


def reconcile_execution_plans(
    session: Session,
    *,
    workspace_root: Path,
    staging_stale_seconds: float,
    now: datetime | None = None,
) -> CleanupResult:
    """Crash sonrası veritabanı ile disk arasındaki tutarsızlıkları giderir.

    Dört sınıf ele alınır:

    - **Süresi geçmiş kayıtlar:** workspace'leri silinir, kayıt ``expired``.
    - **Workspace'i olmayan kayıt** (satır yazıldı, dizin kayboldu veya
      yayımlanamadı): kayıt ``expired`` yapılır — içeriği olmayan bir onay
      claim edilebilir kalmaz.
    - **Kaydı olmayan workspace** (dizin yayımlandı, satır yazılamadı) ve
      **yarım kalmış staging**: yaş eşiğini aşmışsa silinir.
    - **Expired kaydın hâlâ duran workspace'i:** ilk silme denemesi başarısız
      olmuşsa yeniden denenir.

    **Orphan kararı asla bir sayfalama sonucundan çıkarılmaz.** Bir dizin,
    ancak veritabanında o ``workspace_id``'ye ait **hiçbir** kayıt bulunmadığı
    doğrudan sorgulanarak kanıtlandığında orphan sayılır. Sınırlı bir kayıt
    listesini "gerisi orphan'dır" diye okumak, 500'üncü satırdan sonra gelen
    geçerli bir planın dondurulmuş içeriğini silerdi; kullanıcı elindeki
    token'ın neden aniden geçersizleştiğini de hiçbir yerde göremezdi.

    Yaş eşiği yalnızca **kaydı olmayan** dizinlere uygulanır: yayımlama ile
    satırın yazılması arasındaki kısa pencerede hazırlanmakta olan bir planın
    içeriği silinmemelidir. Kaydı bulunan ve ``expired`` olan bir dizinde eşiğe
    gerek yoktur; kimliği satırla zaten kanıtlanmıştır.

    Tur başına yapılan iş bounded'dır (:data:`MAX_RECONCILE_RECORDS`,
    :data:`MAX_RECONCILE_WORKSPACES`). Artan iş gerçekten bir sonraki tura
    kalır: incelenecek dizin penceresi kök altında tutulan kalıcı bir bakım
    imleciyle her turda **ileri** kayar ve liste bitince başa döner. Sınırı
    "hep ilk N dizin" diye uygulamak, ilk pencere dolu olduğunda arkadaki
    orphan ve silinememiş expired dizinleri süresiz aç bırakırdı; dondurulmuş
    workspace project ve inventory içeriği taşıdığı için bu kabul edilemez.

    Temizlik yalnızca kökün **doğrulanmış doğrudan çocuklarında** çalışır:
    adı uygulamanın ürettiği biçime uymayan hiçbir girdiye dokunulmaz, symlink
    izlenmez ve hiçbir noktada geniş kapsamlı bir silme kullanılmaz.
    """
    moment = now or datetime.now(UTC)
    expired = sweep_expired_plans(
        session, workspace_root=workspace_root, now=moment, limit=MAX_RECONCILE_RECORDS
    )

    live = list(
        session.execute(
            select(ExecutionPlanRecord)
            .where(ExecutionPlanRecord.status != ExecutionPlanStatus.EXPIRED)
            .order_by(ExecutionPlanRecord.expires_at)
            .limit(MAX_RECONCILE_RECORDS)
        )
        .scalars()
        .all()
    )

    missing = 0
    for record in live:
        if workspace_exists(workspace_root, record.workspace_id):
            continue
        record.status = ExecutionPlanStatus.EXPIRED
        missing += 1
    if missing:
        session.commit()

    orphans, retried = _collect_unreferenced_workspaces(
        session,
        workspace_root=workspace_root,
        staging_stale_seconds=staging_stale_seconds,
        now=moment,
    )

    staging = 0
    stale_staging = list_stale_staging(
        workspace_root, now=moment, stale_seconds=staging_stale_seconds
    )
    for name in stale_staging:
        staging += int(remove_workspace(workspace_root, name))

    return CleanupResult(
        expired_records=expired.expired_records,
        removed_workspaces=expired.removed_workspaces + retried,
        orphan_workspaces=orphans,
        stale_staging=staging,
        missing_workspaces=missing,
    )


def _collect_unreferenced_workspaces(
    session: Session,
    *,
    workspace_root: Path,
    staging_stale_seconds: float,
    now: datetime,
) -> tuple[int, int]:
    """Kaydı olmayan ve expired kayda ait workspace'leri toplar.

    Karar **dizin başına** verilir: adayların durumu tek bir toplu sorguyla
    okunur ve yalnız üç sonuçtan biri temizliğe yol açar — kayıt yok (orphan,
    yaş eşiğine tabi) veya kayıt ``expired`` (başarısız silmenin tekrarı).
    ``prepared``/``claimed`` bir kayda bağlı dizin, kayıt hangi sayfada olursa
    olsun **korunur**.

    İncelenen pencere kalıcı bir imleçle ilerletilir (bkz. :func:`_select_window`);
    her turda aynı ilk dizinlere bakılmaz.

    Returns:
        ``(orphan_sayısı, tekrar_denenen_silme_sayısı)``.
    """
    candidates = list_workspace_ids(workspace_root)
    if not candidates:
        return 0, 0

    window, next_cursor = _select_window(candidates, read_maintenance_cursor(workspace_root))
    statuses = _workspace_statuses(session, window)
    orphans = 0
    retried = 0
    for workspace_id in window:
        status = statuses.get(workspace_id)
        if status is None:
            age = workspace_age_seconds(workspace_root, workspace_id, now=now)
            if age is None or age <= staging_stale_seconds:
                # Az önce yayımlanmış olabilir: kaydı henüz yazılmamış bir
                # workspace silinmez.
                continue
            orphans += int(remove_workspace(workspace_root, workspace_id))
            continue
        if status is ExecutionPlanStatus.EXPIRED:
            retried += int(remove_workspace(workspace_root, workspace_id))

    # İmleç turun **sonunda** ilerletilir: tur yarıda kesilirse bir sonraki tur
    # aynı pencereyi yeniden inceler. Bu yalnızca iş tekrarıdır; her silme
    # kararı zaten o anki veritabanı durumundan yeniden türetilir.
    write_maintenance_cursor(workspace_root, next_cursor)
    return orphans, retried


def _select_window(candidates: list[str], cursor: str | None) -> tuple[list[str], str | None]:
    """Sıralı aday listesinden bu turun penceresini ve bir sonraki imleci seçer.

    Adaylar her turda yeniden listelenip sıralandığı için imleç tek bir workspace
    adından ibarettir: pencere o addan **kesinlikle sonra** gelen adla başlar.
    Liste tükendiğinde imleç sıfırlanır ve bir sonraki tur baştan devam eder.
    Böylece ilerleme rastlantıya değil sıraya bağlıdır: ``N`` dizin, en çok
    ``ceil(N / MAX_RECONCILE_WORKSPACES)`` turda tamamen incelenir ve hiçbir
    dizin süresiz atlanamaz.

    İmleç okunamaz, bozuk veya listeden düşmüş bir adı gösteriyorsa baştan
    başlanır: en kötü sonuç, daha önce incelenmiş dizinlerin yeniden
    incelenmesidir. İmleç bir dizinin silinip silinmeyeceğini belirlemediği için
    kaybı veya bozulması **hiçbir zaman** yanlış bir silmeye yol açamaz.
    """
    start = bisect.bisect_right(candidates, cursor) if cursor is not None else 0
    if start >= len(candidates):
        # İmleç listenin sonunu geçmiş (aradaki dizinler silinmiş olabilir).
        start = 0
    window = candidates[start : start + MAX_RECONCILE_WORKSPACES]
    exhausted = start + len(window) >= len(candidates)
    return window, None if exhausted else window[-1]


def _workspace_statuses(
    session: Session, workspace_ids: list[str]
) -> dict[str, ExecutionPlanStatus]:
    """Verilen workspace adlarının veritabanındaki durumlarını okur.

    Sorgu adaylarla sınırlıdır ve parçalara bölünür: tek bir ``IN`` listesinin
    sınırsız büyümesi, sürücüye göre parametre sınırına takılabilir.
    """
    found: dict[str, ExecutionPlanStatus] = {}
    for index in range(0, len(workspace_ids), WORKSPACE_LOOKUP_CHUNK):
        chunk = workspace_ids[index : index + WORKSPACE_LOOKUP_CHUNK]
        rows = session.execute(
            select(ExecutionPlanRecord.workspace_id, ExecutionPlanRecord.status).where(
                ExecutionPlanRecord.workspace_id.in_(chunk)
            )
        ).all()
        for workspace_id, status in rows:
            found[workspace_id] = status
    return found


def reconcile_quietly(
    session: Session,
    *,
    workspace_root: Path,
    staging_stale_seconds: float,
) -> CleanupResult | None:
    """Açılış reconciliation'ı; altyapı hatasında uygulamayı durdurmaz.

    Reconciliation bir bakım işidir: veritabanı henüz migrate edilmemişse veya
    disk geçici olarak okunamıyorsa uygulamanın hiç açılmaması, temizlenmemiş
    bir workspace'ten daha kötüdür. Güvenlik kararları buna bağlı değildir;
    kayıp workspace claim anında yeniden fail-closed kontrol edilir.
    """
    try:
        return reconcile_execution_plans(
            session,
            workspace_root=workspace_root,
            staging_stale_seconds=staging_stale_seconds,
        )
    except (SQLAlchemyError, OSError, AppError):
        session.rollback()
        return None


def _invalid_plan() -> ExecutionPlanInvalidError:
    """Bilinmeyen, biçimsiz, süresi geçmiş veya kullanılmış plan için ortak hata.

    Mesaj ve ``details`` token'ın hiçbir parçasını taşımaz.
    """
    return ExecutionPlanInvalidError(
        "Hazırlanmış execution planı geçerli değil. Planı yeniden hazırlayın.",
        details={"reason": "invalid"},
    )
