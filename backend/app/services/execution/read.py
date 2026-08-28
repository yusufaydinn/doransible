"""Yetkilendirilmiş PLAYBOOK Job'larının bounded, salt-okunur sorgusu (R1-V3D2A1).

Bu modül **hiçbir şey değiştirmez**. ``INSERT``, ``UPDATE``, ``DELETE`` ve
``commit`` yoktur; okunan satırlar dışarı çıkmadan önce değişmez dataclass'lere
dönüştürülür. Dosya sistemine, artifact deposuna, runner'a, worker'a ve HTTP
katmanına hiç dokunmaz: bu turda Job'ı **okuyan** bir public yol da yoktur
(``GET /api/jobs`` eklenmemiştir).

Görünürlük **fail-closed** bir yetkilendirme bağıdır: satır yalnız, onu üreten
onay biletiyle hâlâ tutarlıysa okunur. Yalnız ``execution_plan_id``'nin dolu
olmasına bakmak yeterli **değildir** — ``ck_jobs_active_playbook_is_authorized``
yalnız ``pending``/``running`` satırları kapsar, dolayısıyla terminal bir
PLAYBOOK satırı plan kimliğini taşıdığı hâlde project, inventory, playbook,
aktör veya ``limit_pattern`` bağı bozulmuş olabilir. Böyle bir satırı
"onaylanmış çalıştırma" diye göstermek, kullanıcının onayladığı işten başka bir
işi onun geçmişi gibi okutmak olurdu.

Planın kendi ``status``'u da tek başına yeterli **değildir**. Bilet
``claimed`` kaldığı sürece bilinen bir çalıştırmaya karşılık gelir, ama TTL
temizliği (:func:`~app.services.execution.store.sweep_expired_plans`) daha
önce claim edilmiş — ve gerçek bir Job'a bağlanmış — bir bileti, o Job'un
kuyruğunda aktif bir ``pending``/``running`` satır kalmadığı sürece yalnızca
süresi geçtiği için ``expired`` yapar; kayıt ve artifact silinmez, yalnız
plan satırı işaretlenir. Böyle bir satırı yalnız ``status != claimed`` diye
elemek, meşru ve **terminal** bir Job'ı temizliğin bir yan etkisiyle
geçmişten düşürürdü. Ayrım bu yüzden ``status``'a ek olarak ``claimed_at``'in
dolu olup olmadığına **ve** ``Job.status``'un terminal olup olmadığına bakar
(bkz. :func:`_authorized_statement`): hiç claim edilmeden süresi geçen bir
plan ``claimed_at``'i hiçbir zaman dolduramaz, ``pending``/``running`` bir
Job ise zaten terminal değildir ve — ``sweep_expired_plans`` aktif bir
PLAYBOOK Job'ına bağlı planı hiç dokunmadan atladığı, ``job_state``'in worker
tarafı da yalnız ``claimed`` bir planı çalıştırdığı için — normal yazma
yollarında hiçbir zaman gerçekleşmeyen, tutarsız bir durumu temsil eder;
böyle bir satır fail-closed elenir.

Bağın tamamı :func:`_authorized_statement` içinde, ``INNER JOIN``'ler +
``WHERE`` olarak durur (bkz. oradaki koşul listesi). Hiçbir koşul satır
alındıktan sonra Python'da uygulanmaz: "önce oku, sonra ele" yaklaşımı elenmesi
gereken satırları önce belleğe alır ve tek bir unutulmuş dalda dışarı
sızdırırdı. Plan tablosuna eklenen join yetkilendirme koşuludur; Project ve
Inventory'ye eklenenler (R1-V3J0B2) yalnızca ``project_name``/
``inventory_name``'i okumak içindir ve hiçbir koşulu **gevşetmez**.

Plan tablosu yalnız **koşul** olarak kullanılır; ``SELECT`` listesine hiçbir
alanı girmez. Token özeti, ``workspace_id``, ``manifest_digest``,
``input_fingerprint`` ve plan kimliği bu yüzden okunan satırda hiç bulunmaz —
okunmayan bir sütun yanlışlıkla dışarı verilemez.

Aynı bağı worker tarafında :func:`~app.services.execution.job_state` kendi
``_binding_is_valid``'i ile uygular. O fonksiyon buradan **import edilmez** ve
değiştirilmez: orası bir Job'ı çalıştırmaya alıp alamayacağına karar verir,
burası bir Job'ın okunabilir olup olmadığına. İki kararı tek koda bağlamak,
birinin gevşemesini sessizce diğerine taşırdı.

Aktörün kendisi **cevaba çıkmaz**. Filtre olarak kullanılan bir değeri geri
yansıtmak, çağıranın zaten bildiği bir şeyi tekrar etmekten ibaret olurdu; buna
karşılık aynı alanın ileride bir listede görünmesi, kimin hangi işi
çalıştırdığını okunabilir kılardı. Aynı gerekçeyle ``execution_plan_id``,
``workspace_id``, ``manifest_digest``, ``artifact_path``, ``worker_id`` ve kira
alanları da :class:`PlaybookJobSummary` içinde **yoktur**.

**Sıralama ve sayfalama.** Liste ``created_at DESC, id DESC`` ile sıralanır ve
keyset (cursor) sayfalaması kullanır. ``id`` ikincil anahtardır çünkü aynı
mikrosaniyeyi taşıyan iki satırda sürücünün satır sırası kararlı değildir;
kararsız bir sıra, ikinci sayfada satır tekrarına veya atlanan satıra yol
açardı. Offset tabanlı sayfalama da bilinçli olarak kullanılmaz: araya yeni bir
Job girdiğinde offset kayar ve aynı satır iki kez okunur.

**Zaman sözleşmesi.** Çağıranın verdiği cursor zamanı timezone-aware olmak
zorundadır ve UTC'ye çevrilir; naive bir değer sunucunun yerel saatini UTC ilan
etmek olurdu ve sayfa sınırını saat farkı kadar kaydırırdı. Veritabanından
**okunan** naive zaman ise ayrı bir durumdur: SQLite ``DateTime(timezone=True)``
sütunlarını tzinfo olmadan geri verir ve tek doğru yorum "DB UTC saklar"
sözleşmesidir. Bu varsayım yalnızca burada, okuma yönünde yapılır (bkz.
:func:`_stored_utc`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, Row, Select, and_, or_, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import (
    ExecutionMode,
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    Inventory,
    Job,
    JobStatus,
    JobType,
    Project,
)

# Bir sayfanın taşıyabileceği en fazla satır. Sınır bir tercih değil, sorgunun
# **sınırlı** olmasının kendisidir: sınırsız bir liste tek istekle bütün Job
# geçmişini belleğe alırdı.
MAX_PAGE_LIMIT = 100

# Varsayılan sayfa boyutu. İmzada tekrar edilir; sabit burada durur ki test ve
# çağıran aynı değeri iki yerden okumasın.
DEFAULT_PAGE_LIMIT = 25

# ``error_code`` alanının dışarı çıkabilecek **bütün** değerleri.
#
# Liste bir biçim değil bir **içerik** kontrolüdür. ``Job.error_code`` veritabanı
# tarafında serbest bir ``String(64)``'tür: doğrudan yazılmış bir satır oraya bir
# workspace yolu, bir token parçası veya bir exception metni koyabilir. Böyle bir
# değeri "zaten kaydedilmiş" diye dışarı taşımak, sonucun okunabildiği her yerde
# o metni görünür kılardı. Bilinmeyen her değer bu yüzden tek bir
# :data:`UNKNOWN_FAILURE` koduna daraltılır.
#
# Küme :data:`app.services.execution.job_state.FINISH_ERROR_CODES` ile aynı
# değildir ve ondan **import edilmez**: orası bir çalıştırmanın yazabileceği
# kodları tanımlar, burası bir okuyucunun görebileceği kodları. Recovery kodları
# (``execution_binding_invalid``, ``interrupted_by_restart``) hiçbir zaman
# finish yolundan yazılmaz ama kaydedilmiş satırlarda bulunur ve okunabilir
# olmalıdır; ikisini tek listeye bağlamak, yazma sözleşmesinin gevşemesini
# sessizce okuma yüzeyine taşırdı.
PUBLIC_ERROR_CODES: frozenset[str] = frozenset(
    {
        "runner_start_failed",
        "runner_timeout",
        "runner_failed",
        "playbook_failed",
        "runner_output_invalid",
        "runner_no_hosts",
        "workspace_unavailable",
        "workspace_integrity_failed",
        "result_limit_exceeded",
        "execution_binding_invalid",
        "interrupted_by_restart",
        "unknown_failure",
    }
)

# Tanınmayan, boş veya serbest metin bir hata kodunun **tek** karşılığı.
UNKNOWN_FAILURE = "unknown_failure"

# Yayımlanmış sonucun app-data köküne göreli tek geçerli konumu. Biçim
# :mod:`app.services.execution.job_state` ile aynıdır ama oradan import edilmez:
# burada dosya sistemine dokunulmaz, yalnızca kaydedilmiş dizginin **tam olarak**
# bu Job'a ait beklenen değer olup olmadığına bakılır.
_ARTIFACT_TEMPLATE = "jobs/{job_id}/result.json"

# ``expired`` + daha önce claim edilmiş bir plana bağlı bir Job'ın hâlâ
# okunabilir sayılması için taşıması gereken **tek** durum kümesi
# (bkz. :func:`_authorized_statement`, koşul 4). Değer
# :mod:`app.services.execution.job_state`'in kendi ``_TERMINAL_STATUSES``'ı ve
# :mod:`app.services.execution.result_service`'in ``_TERMINAL_STATUSES``'ıyla
# **aynıdır** ama oradan import edilmez (bu modülün import sınırı için bkz.
# ``test_the_read_service_imports_no_execution_or_route_layer``); üçü de
# PLAYBOOK Job'ları için "kalıcı biçimde bitmiş" tanımını taşır. ``CANCELED``
# bilinçli olarak **dışarıdadır**: PLAYBOOK Job'ları için hiçbir yazma yolu
# (``job_state``) bu statüyü üretmez — yalnız PING Job'ları için kullanılır
# (:mod:`app.services.jobs.service`) — ve burada terminal kümeye eklemek,
# gerçekte hiç var olmayan bir durumu var gibi genişletmek olurdu.
_TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.SUCCESSFUL, JobStatus.FAILED})


class JobNotFoundError(AppError):
    """İstenen Job bu aktör için okunabilir değil.

    Dört ayrı sebep aynı cevabı üretir: kimlik hiç yok, satır başka bir aktöre
    ait, satır bir PING işi ya da satır plan bağı olmayan legacy bir PLAYBOOK
    kaydı. Ayrım yapmak, var olan bir Job'ın **varlığını** kimliğini deneyerek
    öğrenmeyi mümkün kılardı: "başkasına ait" cevabı, "böyle bir kayıt yok"
    cevabından farklı olduğu anda kimlik uzayı taranabilir hâle gelir.

    Mesaj ve ``details`` bu yüzden sabittir; istenen kimliği, aktörü veya satırın
    hangi koşulda elendiğini taşımaz.
    """

    status_code = 404
    code = "job_not_found"


_NOT_FOUND_MESSAGE = "Böyle bir çalıştırma kaydı bulunamadı."
_NOT_FOUND_DETAILS = {"reason": "not_found"}


@dataclass(frozen=True, slots=True)
class PlaybookJobCursor:
    """Bir sayfanın bittiği **kesin** nokta.

    İki alan birlikte anlamlıdır: ``created_at`` tek başına eşit zaman damgası
    taşıyan satırları ayıramaz, ``job_id`` tek başına sıralamayı tarif etmez.
    """

    created_at: datetime
    job_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaybookJobSummary:
    """Bir PLAYBOOK Job'ının dışarı verilebilir **tam** tanımı.

    Nesne taşımadıklarıyla tanımlanır: ``requested_by``, ``execution_plan_id``,
    ``workspace_id``, ``manifest_digest``, ``artifact_path``, ``worker_id``,
    ``heartbeat_at``, ``lease_expires_at``, plan token'ı, absolute path,
    environment ve argv burada **yoktur**.

    ``job_type`` sabittir ve alan olarak durur: sorgu yalnız PLAYBOOK satırlarını
    okur. ``mode`` ise (R1-V3H2A) artık sabit **değildir** — ``Job.mode``
    sütunundan olduğu gibi okunur; bir varsayılanı yoktur, çünkü varsayılan bir
    değer sorgunun mode'u okumayı unuttuğu bir gerilemeyi sessizce gizlerdi.

    ``project_id`` ve ``playbook_path`` veritabanı sütunu olarak nullable'dır ama
    burada **değildir**. Sorgu bağı ikisini de planın karşılık gelen ``NOT NULL``
    alanına eşitler (:func:`_authorized_statement`); ``NULL`` taşıyan bir satır o
    eşitliği hiçbir zaman sağlayamaz ve zaten görünmez. Alanları yine de
    ``| None`` bırakmak, okuyan her tarafı var olmayan bir duruma karşı
    dallanmaya zorlar ve sözleşmeyi gerçekte olduğundan zayıf gösterirdi. Değer
    **uydurulmaz**: bağı bozuk bir satır düzeltilerek değil, hiç okunmayarak
    elenir.

    ``project_name`` ve ``inventory_name`` (R1-V3J0B2) aynı gerekçeyle
    doldurulur: :func:`_authorized_statement` içindeki ``INNER JOIN`` ile
    ``Project``/``Inventory`` satırından okunur, istemcide veya ID'den tahmin
    edilerek **üretilmez**. Kayıt silinemez (her iki ilişki de ``RESTRICT``),
    ama satır her nasılsa join'i sağlayamazsa (ör. FK doğrulaması kapalı bir
    bağlantıda oluşmuş bozuk veri) fail-closed davranış devreye girer: satır
    hiç dönmez, sahte bir ad ("Bilinmeyen project" gibi) uydurulmaz.
    """

    job_id: str
    job_type: Literal["playbook"] = field(default="playbook")
    status: JobStatus
    mode: ExecutionMode
    project_id: int
    project_name: str
    inventory_id: int
    inventory_name: str
    playbook_path: str
    return_code: int | None
    error_code: str | None
    result_truncated: bool
    has_recorded_result: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class PlaybookJobPage:
    """Tek bir sayfa ve — yalnız devamı varsa — sonraki sayfanın başlangıcı.

    ``next_cursor`` ile ``has_more`` arasındaki bağ bir invariant'tır ve burada
    zorlanır. İkisinin ayrışması iki yönde de yanlış olurdu: cursor'suz bir
    ``has_more=True`` istemciyi devam edemeyeceği bir sayfaya çağırır, dolu bir
    cursor ile ``has_more=False`` ise var olmayan bir sayfayı işaret ederdi.
    """

    items: tuple[PlaybookJobSummary, ...]
    has_more: bool
    next_cursor: PlaybookJobCursor | None

    def __post_init__(self) -> None:
        if (self.next_cursor is not None) is not self.has_more:
            raise ValueError("Sonraki sayfa işaretçisi yalnız devamı olan sayfada bulunur.")


def list_playbook_jobs(
    session: Session,
    *,
    requested_by: str,
    project_id: int | None = None,
    status: JobStatus | None = None,
    mode: ExecutionMode | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    before_created_at: datetime | None = None,
    before_job_id: str | None = None,
) -> PlaybookJobPage:
    """Aktörün yetkilendirilmiş PLAYBOOK Job'larını sınırlı bir sayfa hâlinde okur.

    Sıra ``created_at DESC, id DESC``'tir; en yeni iş başta gelir. Sayfalama
    keyset tabanlıdır: ``before_created_at`` ve ``before_job_id`` **birlikte**
    verilir ve o çiftten kesin olarak sonra gelen satırlardan devam edilir.
    Yalnız birini vermek reddedilir — eksik bir cursor, eşit zaman damgası
    taşıyan satırlarda sessizce satır atlar ya da tekrar ederdi.

    Devamın olup olmadığı ``limit + 1`` satır sorgulanarak öğrenilir. Ayrı bir
    ``COUNT(*)`` çalıştırmak hem ikinci bir tam tarama demek olurdu hem de iki
    sorgu arasına giren bir satır yüzünden "devamı var" bilgisini yalan hâle
    getirebilirdi.

    Args:
        session: Aktif veritabanı session'ı. Fonksiyon çıkışında — dolu sayfa,
            boş sayfa veya hata — açık transaction bırakılmaz.
        requested_by: Geçerli aktör. Sorgu koşuluna **tam eşleşme** olarak girer
            ve cevaba çıkmaz.
        project_id: Verilirse yalnız o project'in işleri; ``>= 1`` olmalıdır.
        status: Verilirse yalnız o durumdaki işler; gerçek bir :class:`JobStatus`
            üyesi olmalıdır, ham dizgi kabul edilmez.
        mode: Verilirse yalnız o kipteki işler; gerçek bir :class:`ExecutionMode`
            üyesi olmalıdır, ham ``"check"``/``"normal"`` dizgisi kabul edilmez.
        limit: Sayfa boyutu; ``1``–:data:`MAX_PAGE_LIMIT` aralığında.
        before_created_at: Cursor'ın zaman bileşeni; timezone-aware olmalıdır.
        before_job_id: Cursor'ın kimlik bileşeni; canonical UUID4 olmalıdır.

    Returns:
        Değişmez :class:`PlaybookJobSummary` nesnelerinden oluşan bir
        :class:`PlaybookJobPage`.

    Raises:
        ValueError: ``limit`` aralık dışıysa, ``project_id`` pozitif değilse,
            ``status`` gerçek bir :class:`JobStatus` üyesi değilse, ``mode``
            gerçek bir :class:`ExecutionMode` üyesi değilse, cursor alanları
            eksik/biçimsiz ise ya da cursor zamanı naive ise. Bu yolda
            veritabanına **hiç** dokunulmaz.
        Exception: Sorgu, sonuç okuması **veya** özete dönüşüm sırasında çıkan
            her hata rollback'ten sonra olduğu gibi yeniden yükselir. Ne boş
            sayfaya ne de bir :class:`~app.core.errors.AppError`'a çevrilir ve
            metni dışarı yansıtılmaz. Kapsam veritabanı arızasıyla sınırlı
            **değildir**: yalnız ``SQLAlchemyError`` yakalansaydı, satırlar
            geldikten sonra dönüşümde çıkan herhangi bir hata çağırana açık bir
            okuma transaction'ı devrederdi.
    """
    page_size = _require_limit(limit)
    project = _require_project_id(project_id)
    state = _require_status(status)
    kind = _require_mode(mode)
    cursor = _require_cursor(before_created_at, before_job_id)

    statement = _authorized_statement(requested_by)
    if project is not None:
        statement = statement.where(Job.project_id == project)
    if state is not None:
        statement = statement.where(Job.status == state)
    if kind is not None:
        statement = statement.where(Job.mode == kind)
    if cursor is not None:
        statement = statement.where(_after(cursor))

    # `limit + 1`: fazladan gelen tek satır "devamı var" demektir ve cevaba
    # konmaz. Sayı bu yüzden ayrı bir sorgu değil, aynı sorgunun bir fazlasıdır.
    statement = statement.order_by(Job.created_at.desc(), Job.id.desc()).limit(page_size + 1)

    try:
        rows = session.execute(statement).all()
        # Dönüşüm rollback'ten **önce** yapılır. Satırlar `Row` olduğu için lazy
        # load riski zaten yoktur, ama sıra yine de sözleşmenin parçasıdır:
        # dışarı çıkan her değer transaction kapanmadan düz Python değerine
        # dönüşür ve çağıran elindeki nesneyi kullanırken veritabanına
        # dokunmaz.
        summaries = [_to_summary(row) for row in rows]
    except Exception:
        # Kapsam bilinçli olarak `SQLAlchemyError`'dan **geniştir**. Transaction
        # `execute` ile açılır ve dönüşüm hâlâ o transaction'ın içindedir; orada
        # çıkan bir `AttributeError`, `TypeError` veya invariant `ValueError`'ı
        # kapsam dışı bırakmak, session'ı çağırana açık bir okuma kilidiyle
        # devretmek demekti. Hata **çevrilmez**: yakalanır, transaction kapatılır
        # ve olduğu gibi yükselir; `BaseException` (iptal, `KeyboardInterrupt`)
        # bilinçli olarak kapsam dışıdır.
        session.rollback()
        raise

    # Salt-okunur servis çağırana açık bir okuma transaction'ı devretmez: kapalı
    # kalan bir okuma kilidi sonraki yazmayı bekletir ve sayfa üretildikten
    # sonra tutulmasının hiçbir doğruluk katkısı yoktur.
    session.rollback()

    has_more = len(summaries) > page_size
    items = summaries[:page_size]
    next_cursor = (
        PlaybookJobCursor(created_at=items[-1].created_at, job_id=items[-1].job_id)
        if has_more
        else None
    )
    return PlaybookJobPage(items=tuple(items), has_more=has_more, next_cursor=next_cursor)


def get_playbook_job(
    session: Session,
    job_id: str,
    *,
    requested_by: str,
) -> PlaybookJobSummary:
    """Tek bir yetkilendirilmiş PLAYBOOK Job'ını aktöre bağlı olarak okur.

    Yetkilendirme bağının tamamı sorgu koşulundadır, okunan satır üzerinde
    yapılan bir karşılaştırma değil: satırı önce almak, "bağı bozuk" ile "yok"
    arasındaki farkı bir zamanlama veya kod yolu üzerinden gözlemlenebilir
    kılardı.

    Args:
        session: Aktif veritabanı session'ı. Fonksiyon çıkışında — bulundu veya
            bulunamadı — açık transaction bırakılmaz.
        job_id: Okunacak Job'ın canonical UUID4 kimliği.
        requested_by: Geçerli aktör; tam eşleşme aranır ve cevaba çıkmaz.

    Returns:
        Değişmez bir :class:`PlaybookJobSummary`.

    Raises:
        ValueError: ``job_id`` canonical UUID4 değilse. Veritabanına **hiç**
            dokunulmaz; biçimsiz bir kimlik bir sorgu turu hak etmez.
        JobNotFoundError: Kimlik yok, satır başka bir aktöre ait, satır bir PING
            işi, plan bağı olmayan legacy bir PLAYBOOK kaydı ya da plan bağı
            **bozulmuş** bir terminal kayıt. Hepsi aynı sabit cevabı üretir ve
            hangi koşulda elendiği bildirilmez.
        Exception: Sorgu, sonuç okuması **veya** özete dönüşüm sırasında çıkan
            her hata rollback'ten sonra olduğu gibi yeniden yükselir.
            "Bulunamadı"ya **çevrilmez** — geçici bir arızayı kalıcı bir yokluk
            gibi göstermek, kullanıcıya var olan bir kaydın silindiğini söylemek
            olurdu — ve metni dışarı yansıtılmaz.
    """
    identifier = _require_uuid4(job_id, "Job kimliği canonical UUID4 olmalıdır.")

    try:
        row = session.execute(
            _authorized_statement(requested_by).where(Job.id == identifier)
        ).first()
        summary = None if row is None else _to_summary(row)
    except Exception:
        # Kapsam list yolundakiyle aynıdır ve aynı gerekçeye dayanır: dönüşüm
        # hâlâ açık transaction'ın içindedir.
        session.rollback()
        raise

    session.rollback()

    if summary is None:
        raise JobNotFoundError(_NOT_FOUND_MESSAGE, details=dict(_NOT_FOUND_DETAILS))
    return summary


# Sorgunun okuduğu **tam** sütun kümesi. Yasak alanlar (`requested_by`,
# `execution_plan_id`, `worker_id`, `heartbeat_at`, `lease_expires_at`,
# `limit_pattern`) buraya hiç girmez: okunmayan bir sütun yanlışlıkla dışarı
# verilemez. `artifact_path` tek istisnadır ve satırın kendisi olarak değil,
# yalnız bir `bool`'a dönüşmek üzere okunur. `mode` R1-V3H2A ile eklenir:
# özetin kipi artık sabit değildir, satırdan okunur.
#
# `ExecutionPlanRecord`'ın **hiçbir** sütunu buraya girmez; plan tablosu yalnız
# `_authorized_statement` içinde bir yetki koşulu olarak görünür. `Project` ve
# `Inventory`'nin ise (R1-V3J0B2) **yalnız** `name` sütunu girer: path,
# description, actor, plan/token/workspace/artifact alanları hiçbir koşulda
# buraya taşınmaz.
_SUMMARY_COLUMNS = (
    Job.id,
    Job.status,
    Job.mode,
    Job.project_id,
    Project.name.label("project_name"),
    Job.inventory_id,
    Inventory.name.label("inventory_name"),
    Job.playbook_path,
    Job.return_code,
    Job.error_code,
    Job.result_truncated,
    Job.artifact_path,
    Job.created_at,
    Job.started_at,
    Job.finished_at,
)


def _authorized_statement(requested_by: str) -> Select[Any]:
    """Görünürlüğün **tamamı**: tek bir ``INNER JOIN`` ve tek bir ``WHERE``.

    Koşullar şunlardır:

    1. ``Job.job_type == PLAYBOOK`` — PING'in kendi onay akışı vardır ve plan
       kaydı üretmez.
    2. ``Job.execution_plan_id IS NOT NULL`` — satır bir onay biletinden
       doğmuştur. ``INNER JOIN`` bunu zaten zorlar; koşul yine de açıkça durur
       ki niyet ``JOIN`` türünün bir yan etkisine bağlı kalmasın.
    3. ``ExecutionPlanRecord.id == Job.execution_plan_id`` — bilet gerçekten
       **mevcuttur**. ``RESTRICT`` foreign key silinmesini engeller ama doğrudan
       yazılmış bir satır var olmayan bir plana işaret edebilir.
    4. ``ExecutionPlanRecord.status == CLAIMED`` **veya**
       (``status == EXPIRED`` **ve** ``claimed_at IS NOT NULL`` **ve**
       ``Job.status`` :data:`_TERMINAL_JOB_STATUSES` içinde) — bilet daha önce
       claim edilmiş, artık yalnız TTL temizliği yüzünden ``expired`` ve
       yetkilendirdiği Job **terminal**. ``prepared`` bir plana bağlı Job, hiç
       claim edilmemiş bir onaydan doğmuş görünür ve hâlâ elenir. Yalnız
       ``status == EXPIRED`` + ``claimed_at IS NOT NULL`` da tek başına
       yetmez: :func:`~app.services.execution.store.sweep_expired_plans`
       aktif (``pending``/``running``) bir PLAYBOOK Job'ına bağlı planı hiç
       ``expired`` yapmaz (bkz. o fonksiyonun ``_HAS_ACTIVE_PLAYBOOK_JOB``
       koruması) ve worker (:mod:`app.services.execution.job_state`) zaten
       yalnız ``claimed`` bir planı çalıştırır; dolayısıyla ``expired`` +
       ``claimed_at`` dolu + ``pending``/``running`` bir Job kombinasyonu
       normal yazma yollarında **hiç oluşmaz** ve fail-closed elenir — bu
       genişleme yalnız kalıcı **terminal** geçmişi (``successful``/
       ``failed``) kapsar.

       ``claimed_at``'in bu koşuldaki rolü konusunda dürüst olmak gerekir:
       ``ck_execution_plans_claimed_has_claimed_at`` kısıtı yalnız
       ``status == claimed`` satırında ``claimed_at IS NOT NULL`` şartını
       zorunlu kılar; bir ``expired`` satırda ``claimed_at``'in nasıl
       dolduğuna dair hiçbir garanti **vermez**. Uygulamanın normal yazma
       yollarında ``claimed_at`` yalnız tek atomik claim UPDATE'i sırasında
       yazılır (:func:`~app.services.execution.store.claim_plan_row`) ve
       :func:`~app.services.execution.store.sweep_expired_plans` ile
       :func:`~app.services.execution.store.reconcile_execution_plans` bu
       alana hiç dokunmaz — yalnız ``status``'u değiştirirler. Ama bu,
       ``claimed_at``'i kriptografik veya **taklit edilemez** bir kanıt
       yapmaz: veritabanına doğrudan erişimi olan biri satıra istediği
       ``claimed_at``'i de yazabilir — tıpkı ``status``, ``project_id`` veya
       ``requested_by``'ı yazabileceği gibi. Buradaki fail-closed sınır bu
       yüzden tek bir "sahte kanıtlanamaz" sütuna değil, **hepsinin
       birlikte** doğru olmasına dayanır: uygulama-yönetimli ``claimed_at``
       izi + terminal ``Job.status`` + aşağıdaki bütün immutable binding
       kontrolleri (5-12). Doğrudan bir DB yazımı bunların hepsini aynı anda
       tutarlı biçimde sahtelemek zorunda kalır — ki bu zaten var olan tehdit
       modelinin (veritabanına doğrudan erişim) dışındadır ve bu servisin
       iddiası hiçbir zaman olmamıştır.
    5-7. ``Job.requested_by == requested_by``, ``ExecutionPlanRecord.requested_by
       == requested_by`` ve ikisinin **birbirine** eşitliği. Üçüncüsü ilk
       ikisinden mantıken çıkar ama açıkça yazılır: koşul, aktör filtresinin
       ileride gevşemesi hâlinde de Job ile planın aynı aktöre ait olmasını
       ayakta tutar.
    8-10. ``Job.project_id``, ``Job.inventory_id`` ve ``Job.playbook_path``
       planın karşılık gelen alanına eşittir. ``NULL`` taşıyan bir Job alanı bu
       eşitliği **hiçbir zaman** sağlamaz (``NULL = x`` doğru değildir), böylece
       nullable sütunlar ayrı bir kontrole gerek kalmadan elenir.
    11. ``Job.limit_pattern IS NULL`` — onaylanan plan hedef kümesini daraltan
       bir desen taşımaz; taşıyan bir satır onaylanandan başka bir işi tarif
       eder.
    12. ``Job.mode == ExecutionPlanRecord.mode`` (R1-V3H2A) — Job'un çalıştırma
       kipi, onu yetkilendiren plan kaydının kipiyle **aynı** olmalıdır. Normal
       mode public yüzeye açıldığından beri bağ project/inventory/playbook
       üçlüsüyle sınırlı kalamaz: Job'un kipi doğru zincirde her zaman plandan
       miras alınır (:mod:`app.services.execution.authorize`), ama doğrudan
       yazılmış veya başka bir yoldan bozulmuş bir satır ikisini
       ayrıştırabilir. Böyle bir satır, kullanıcının onayladığı kipten başka
       bir kiple çalıştırılmış bir işi onun geçmişi gibi gösterirdi.

    **Neden yalnız ``execution_plan_id IS NOT NULL`` yetmez?** Veritabanındaki
    ``ck_jobs_active_playbook_is_authorized`` yalnız ``pending``/``running``
    satırları kapsar. Terminal satırlar bilinçli olarak dışarıdadır (geçmiş
    kayıtlar ve migration'ın kapattığı legacy satırlar orada durur), dolayısıyla
    plan kimliği dolu ama project/inventory/playbook/aktör bağı bozuk bir
    terminal satır veritabanı tarafından kabul edilir. Okuma yüzeyi bu boşluğu
    kendi predicate'iyle kapatır.

    **Neden ``expired`` durumunun tamamı değil, yalnız ``claimed_at`` dolu
    ve Job'u terminal olan alt kümesi kabul edilir?** ``expired`` iki
    tamamen farklı geçmişi aynı sütun değerinde birleştirir: (a) hiç claim
    edilmeden süresi geçmiş bir hazırlık ve (b) claim edilip bir Job üretmiş,
    sonradan yalnız TTL temizliğinin (bkz.
    :func:`~app.services.execution.store.sweep_expired_plans`) dokunduğu bir
    bilet. Yalnız ikincisi kullanıcının onayladığı ve gerçekten çalıştırdığı
    işi temsil eder; ilkini de kabul etmek, hiçbir zaman onaylanmamış bir
    plana bağlı — veya böyle görünecek şekilde doğrudan yazılmış — bir satırı
    geçmişe sokardı. ``claimed_at`` uygulamanın normal yazma yollarında bu
    ikisini ayıran **pratik** işarettir (yukarıdaki 4 numaralı koşulun kendi
    gerekçesine bakınız — bu bir kriptografik kanıt iddiası değildir);
    ``Job.status``'un terminal olması ise ayrıca, aktif bir ``pending``/
    ``running`` Job'a bağlı planın zaten ``sweep_expired_plans`` tarafından
    hiç ``expired`` yapılmadığı normal akışta hiç oluşmayan bir durumu
    (``expired`` + claim edilmiş + hâlâ aktif Job) tutarsız sayıp fail-closed
    elemeyi sağlar. Geçici bir durum alanını (``status``) tek başına yetki
    kanıtı saymak yerine, uygulamanın normalde geri dönmediği bir alanı
    (``claimed_at``) ve Job'un kendi terminal durumunu da birlikte aramak bu
    yüzden sözleşmeyi gevşetmez: fail-closed olan tek şey genişler, o da
    yalnızca daha önce claim edilmiş ve artık kalıcı biçimde bitmiş işlere.

    Plan tablosundan **hiçbir sütun seçilmez**: :data:`_SUMMARY_COLUMNS` yalnız
    ``Job`` sütunlarından oluşur (artı ``Project``/``Inventory``'nin yalnız
    ``name``'i, aşağıya bakınız). Plan burada bir veri kaynağı değil, bir yetki
    koşuludur; alanlarını okumak, token özetini ve digest'i materyalize edilmiş
    satıra taşırdı.

    **Project/Inventory adı nereden gelir?** (R1-V3J0B2) ``Project`` ve
    ``Inventory``'ye eklenen iki ``INNER JOIN``, isimleri **yalnızca** kayıt
    ilişkisinden okur; istemciden alınmaz, ID'den tahmin edilmez. Her iki
    join de ``id`` eşitliğidir ve ``Job.project_id``/``Job.inventory_id``
    zaten yukarıdaki 8-9 numaralı koşullarla planın ``NOT NULL`` alanına
    eşitlenmiştir — bu fonksiyona ulaşan bir satırda ikisi de doludur. Project
    ve Inventory kayıtları ``RESTRICT`` foreign key'lerle korunur ve
    referanslı bir Job varken silinemez; join yine de doğal olarak
    **fail-closed**'dır — bir satır her nasılsa eşleşen bir Project veya
    Inventory bulamazsa ``INNER JOIN`` o satırı sessizce eler, sahte bir isim
    üretmez.
    """
    return (
        select(*_SUMMARY_COLUMNS)
        .join(ExecutionPlanRecord, ExecutionPlanRecord.id == Job.execution_plan_id)
        .join(Project, Project.id == Job.project_id)
        .join(Inventory, Inventory.id == Job.inventory_id)
        .where(
            Job.job_type == JobType.PLAYBOOK,
            Job.execution_plan_id.is_not(None),
            or_(
                ExecutionPlanRecord.status == ExecutionPlanStatus.CLAIMED,
                and_(
                    ExecutionPlanRecord.status == ExecutionPlanStatus.EXPIRED,
                    ExecutionPlanRecord.claimed_at.is_not(None),
                    Job.status.in_(_TERMINAL_JOB_STATUSES),
                ),
            ),
            Job.requested_by == requested_by,
            ExecutionPlanRecord.requested_by == requested_by,
            Job.requested_by == ExecutionPlanRecord.requested_by,
            Job.project_id == ExecutionPlanRecord.project_id,
            Job.inventory_id == ExecutionPlanRecord.inventory_id,
            Job.playbook_path == ExecutionPlanRecord.playbook_path,
            Job.limit_pattern.is_(None),
            Job.mode == ExecutionPlanRecord.mode,
        )
    )


def _after(cursor: PlaybookJobCursor) -> ColumnElement[bool]:
    """``(created_at, id)`` çiftinden **kesin olarak** sonra gelen satırlar.

    Sıra azalan olduğu için "sonra gelen" küçük olandır. Karşılaştırma satır
    değeri (``(a, b) < (c, d)``) yerine açık ``OR`` biçiminde yazılır: satır
    değeri karşılaştırması SQLite'ın eski sürümlerinde ve bazı sürücülerde
    desteklenmez, açık biçim ise her iki veritabanında aynı planı üretir.

    Eşitlik iki tarafta da dışarıdadır: cursor satırının kendisi bir önceki
    sayfanın son satırıdır ve ikinci kez okunmamalıdır.
    """
    return or_(
        Job.created_at < cursor.created_at,
        and_(Job.created_at == cursor.created_at, Job.id < cursor.job_id),
    )


def _to_summary(row: Row[Any]) -> PlaybookJobSummary:
    """Bir veritabanı satırını dışarı verilebilir değişmez özete çevirir.

    ``project_id`` ve ``playbook_path`` burada ``NULL`` kontrolü **görmez**:
    :func:`_authorized_statement` ikisini de planın ``NOT NULL`` alanına
    eşitlediği için bu fonksiyona ulaşan bir satırda dolu olmaları zorunludur.
    Burada ayrıca bir fallback kurmak, sorgunun gevşemesini sessizce
    yamalamaktan ibaret olurdu.
    """
    job_id: str = row.id
    status: JobStatus = row.status
    project_id: int = row.project_id
    playbook_path: str = row.playbook_path
    return PlaybookJobSummary(
        job_id=job_id,
        status=status,
        mode=row.mode,
        project_id=project_id,
        project_name=row.project_name,
        inventory_id=row.inventory_id,
        inventory_name=row.inventory_name,
        playbook_path=playbook_path,
        return_code=row.return_code,
        error_code=_public_error_code(status, row.error_code),
        result_truncated=bool(row.result_truncated),
        has_recorded_result=row.artifact_path == _ARTIFACT_TEMPLATE.format(job_id=job_id),
        created_at=_stored_utc(row.created_at),
        started_at=_stored_utc_or_none(row.started_at),
        finished_at=_stored_utc_or_none(row.finished_at),
    )


def _public_error_code(status: JobStatus, stored: str | None) -> str | None:
    """Kaydedilmiş hata kodunu public sözleşmeye daraltır.

    İki ayrı kural vardır ve sırası önemlidir. Önce **durum**: ``failed``
    olmayan bir satırın hata kodu daima ``None``'dır. Beklenmedik biçimde kod
    taşıyan bir ``successful`` satır, kaydın kendisiyle çelişir; onu dışarı
    taşımak "başarılı ama şu hatayla" gibi okunamaz bir sonuç üretirdi.

    Sonra **içerik**: bilinen bir kod aynen geçer, tanınmayan/boş/serbest metin
    bir değer :data:`UNKNOWN_FAILURE` olur. Kaydedilmiş ham değer hiçbir koşulda
    dışarı çıkmaz ve hangi değerin daraltıldığı da bildirilmez.
    """
    if status is not JobStatus.FAILED:
        return None
    if stored in PUBLIC_ERROR_CODES:
        return stored
    return UNKNOWN_FAILURE


def _stored_utc(value: datetime) -> datetime:
    """Veritabanından okunan zamanı aware UTC'ye getirir.

    SQLite, ``DateTime(timezone=True)`` sütunlarını **tzinfo olmadan** geri
    verir: offset saklama biçiminin bir parçası değildir. Naive bir değeri UTC
    kabul etmek bu yüzden bir tahmin değil, "DB UTC saklar" sözleşmesinin
    okunmasıdır — uygulamanın yazdığı her zaman damgası
    (:func:`datetime.now(UTC) <datetime.datetime.now>` veya ``func.now()``)
    UTC'dir.

    Varsayım **yalnızca bu yönde** yapılır. Çağırandan gelen naive bir değer
    (bkz. :func:`_require_moment`) reddedilir: orada UTC olduğuna dair hiçbir
    sözleşme yoktur ve sunucunun yerel saatini UTC ilan etmek zaman çizgisini
    sessizce kaydırırdı.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stored_utc_or_none(value: datetime | None) -> datetime | None:
    """:func:`_stored_utc`'nin nullable sütunlar için karşılığı."""
    return None if value is None else _stored_utc(value)


def _require_limit(limit: int) -> int:
    """Sayfa boyutunu doğrular. ``bool`` bilinçli olarak reddedilir.

    ``bool`` bir ``int`` alt sınıfıdır: ``True`` sessizce tek satırlık bir sayfa
    olurdu ve çağıranın hatası bir sonuç gibi görünürdü.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("Sayfa boyutu tam sayı olmalıdır.")
    if not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError("Sayfa boyutu izin verilen aralığın dışında.")
    return limit


def _require_project_id(project_id: int | None) -> int | None:
    """Project filtresini doğrular; ``0`` ve negatif değer kabul edilmez."""
    if project_id is None:
        return None
    if isinstance(project_id, bool) or not isinstance(project_id, int):
        raise ValueError("Project kimliği tam sayı olmalıdır.")
    if project_id < 1:
        raise ValueError("Project kimliği pozitif olmalıdır.")
    return project_id


def _require_status(status: JobStatus | None) -> JobStatus | None:
    """Yalnız gerçek :class:`JobStatus` **üyesini** kabul eder.

    :class:`JobStatus` bir :class:`~enum.StrEnum` olduğu için ham ``"failed"``
    dizgisi üyeye eşit sayılır ve bir ``in`` kontrolünü tek başına geçerdi. O
    yol, filtrenin tip sisteminden değil çağıranın elindeki serbest metinden
    gelmesine izin verirdi; yazım hatası taşıyan bir dizgi sessizce hiçbir satır
    döndürmez ve "hiç iş yok" gibi okunurdu.
    """
    if status is None:
        return None
    if not isinstance(status, JobStatus):
        raise ValueError("Durum filtresi gerçek bir JobStatus üyesi olmalıdır.")
    return status


def _require_mode(mode: ExecutionMode | None) -> ExecutionMode | None:
    """Yalnız gerçek :class:`ExecutionMode` **üyesini** kabul eder.

    :func:`_require_status` ile aynı gerekçe: :class:`ExecutionMode` bir
    :class:`~enum.StrEnum` olduğu için ham ``"check"``/``"normal"`` dizgisi
    üyeye eşit sayılır. Servis katmanı bunu kabul etseydi filtre tip
    sisteminden değil çağıranın elindeki serbest metinden gelirdi; yazım
    hatası taşıyan bir dizgi sessizce hiçbir satır döndürmez ve "hiç iş yok"
    gibi okunurdu.
    """
    if mode is None:
        return None
    if not isinstance(mode, ExecutionMode):
        raise ValueError("Kip filtresi gerçek bir ExecutionMode üyesi olmalıdır.")
    return mode


def _require_cursor(
    before_created_at: datetime | None, before_job_id: str | None
) -> PlaybookJobCursor | None:
    """Cursor çiftini doğrular: ikisi birlikte verilir ya da hiçbiri.

    Yarım bir cursor sessizce yanlış bir sayfa üretirdi — yalnız ``created_at``
    ile devam etmek eşit zaman damgası taşıyan satırları atlar, yalnız ``id``
    ile devam etmek ise sıralamayla ilgisiz bir kesme yapardı.
    """
    if (before_created_at is None) != (before_job_id is None):
        raise ValueError("Cursor alanları birlikte verilmelidir.")
    if before_created_at is None or before_job_id is None:
        return None
    return PlaybookJobCursor(
        created_at=_require_moment(before_created_at),
        job_id=_require_uuid4(before_job_id, "Cursor kimliği canonical UUID4 olmalıdır."),
    )


def _require_moment(moment: datetime) -> datetime:
    """Çağıranın verdiği zamanı UTC'ye normalize eder; naive değeri reddeder."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Cursor zamanı timezone-aware olmalıdır.")
    return moment.astimezone(UTC)


def _require_uuid4(value: str, message: str) -> str:
    """Kimliğin canonical UUID4 olduğunu doğrular; değeri hataya yazmaz."""
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(message)
    return value
