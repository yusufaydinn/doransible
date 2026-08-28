"""Tamamlanmış PING ölçümlerinin salt-okunur geçmişi (R1-V3J1A).

Bu modül **yeni bir şey üretmez**: ne tablo, ne scheduler, ne arka plan
yoklaması, ne de yeni bir artifact biçimi. Ping sonucu T-204B2'den beri
zaten kalıcıdır — ``jobs`` tablosunda ``job_type='ping'`` bir satır ve
``app-data/jobs/<job_id>/result.json`` altında sanitize edilmiş bir belge. Burada
yapılan tek şey, o iki kaydı **birlikte** ve **yalnız okuyarak** sunmaktır.

**Sıra sabittir ve güvenliğin kendisidir.**

1. Çağıranın parametreleri yalnız lexical/tip kontrolleriyle doğrulanır. Bu
   adımda SQL ve dosya sistemine **hiç** dokunulmaz.
2. Inventory'nin varlığı :func:`~app.services.inventories.service.get_inventory`
   ile sorulur. 404 sözleşmesi burada yeniden yazılmaz; mevcut davranış aynen
   kullanılır.
3. Tek bir ``SELECT`` çalışır. Görünürlüğün **tamamı** ``WHERE`` içindedir:
   başka bir aktörün veya başka bir inventory'nin satırı Python'a hiç gelmez,
   dolayısıyla sonradan filtrelenmesi de gerekmez. Sorgudan sonra — dolu liste,
   boş liste veya hata — session'da açık transaction bırakılmaz.
4. Artifact'ler **transaction kapandıktan sonra** okunur. Okuma
   :func:`~app.services.execution.result_reader._read_result_document`'ın
   descriptor-relative ve bounded okuyucusudur; hedef yalnız
   ``settings.app_data_dir`` ile canonical Job kimliğinden **türetilir**.
   ``artifact_path`` sütunundaki dizgi hiçbir zaman bir yol olarak açılmaz; o
   sütun yalnız ``WHERE`` içinde beklenen değere **eşit mi** diye sorulur.
5. Her belge dar bir ping doğrulayıcısından geçer ve satırla **exact**
   karşılaştırılır (kimlik, inventory, durum, return code, zaman damgaları).

**Tek hata sözleşmesi.** Eksik, symlink, aşırı büyük, bozuk, yanlış şemadan
gelen veya DB satırıyla uyuşmayan bir artifact — hepsi aynı parametresiz
:class:`PingHistoryUnavailableError` (503) olur. Satır **sessizce atlanmaz**:
atlamak, geçmişi eksik ama tam görünen bir listeye çevirirdi. Hata cevabı yol,
belge içeriği, host adı, host mesajı veya digest **taşımaz**.

**Public cevap yüzeyi bilinçli olarak dardır.** Host adları, host mesajları,
``requested_by``, ``artifact_path``, project/inventory path'i, ``limit``,
``project_id``, stdout/stderr, argv, environment, token ve snapshot bu modülün
döndürdüğü hiçbir yapıda **yer almaz**. ``hosts`` belgede doğrulanır — çünkü
doğrulanmayan bir alan, özetin tutarlılığını da kanıtlanamaz kılardı — ama
doğrulamadan sonra dışarı **taşınmaz**.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Select, literal, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import Job, JobStatus, JobType
from app.services.execution.result import JobResultUnavailableError
from app.services.execution.result_reader import (
    JOBS_DIRNAME,
    RESULT_FILENAME,
    _read_result_document,
)
from app.services.inventories.ping_confirm import (
    FAILED,
    NO_RESULT,
    PING_JOB_TYPE,
    REACHABLE,
    RESULT_SCHEMA_VERSION,
    UNREACHABLE,
)
from app.services.inventories.service import get_inventory

# Sayfa boyutu sınırları. Cursor/pagination bilinçli olarak **yoktur**: bu
# yüzey "son ölçümler ne durumdaydı" sorusunu yanıtlar, geçmişin tamamını
# gezmek için bir arayüz değildir. Sınırsız bir liste, tek bir istekle
# artifact başına bir dosya okuması demek olurdu.
MIN_PING_HISTORY_LIMIT: Final[int] = 1
DEFAULT_PING_HISTORY_LIMIT: Final[int] = 10
MAX_PING_HISTORY_LIMIT: Final[int] = 25

# Tek bir ping artifact'i için okuma bütçesi.
#
# Değer `playbook_runner_max_result_bytes` varsayılanıyla **aynıdır** ve oradan
# import edilmez: bu bir ayar değil, bu okuma yolunun kendi sabit tavanıdır.
# Ping belgesi host başına yalnız üç kısa alan taşır; bu bütçe binlerce host'u
# rahatça kapsar ve `_read_result_document` ham dosyaya kendi zarfını (iki kat
# + 1) uygular. Ayrı bir ayar açmak, kullanıcıya sunum yüzeyinin okuyabileceği
# dosya boyutunu ayarlatmak olurdu.
MAX_PING_RESULT_BYTES: Final[int] = 1_000_000

# Belgedeki host mesajının kabul edilen azami uzunluğu.
#
# Türetme ölçülebilir: `ping_execution` mesajı `sanitize_output(...,
# max_length=400)` ile 400 karaktere kırpar ve sonuna tek bir "…" koyar (en çok
# 401 karakter). Ardından `_mask_connection_values` her bağlantı değerini
# ``***`` ile değiştirir; en kötü durumda tek karakterlik bir değer üç karaktere
# çıkar, yani metin en fazla üçe katlanabilir. Tavan bu yüzden 401'in üç
# katıdır — yazan tarafın üretebileceği en uzun mesaj hâlâ geçerlidir, ötesi
# bu belgenin bu yoldan gelmediğinin işaretidir.
MAX_PING_HOST_MESSAGE_LENGTH: Final[int] = 3 * 401

# Belgenin taşıyabileceği **tam** anahtar kümesi. Fazlası da eksiği de
# reddedilir: fazlalık, yazan tarafın bilmediğimiz bir alanı; eksiklik ise
# başka bir sürümden gelmiş bir belgedir.
_PING_RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "job_id",
        "job_type",
        "status",
        "inventory_id",
        "project_id",
        "limit",
        "return_code",
        "started_at",
        "finished_at",
        "summary",
        "hosts",
    }
)

_PING_SUMMARY_FIELDS: Final[frozenset[str]] = frozenset(
    {"total", "reachable", "unreachable", "failed", "no_result"}
)

_PING_HOST_FIELDS: Final[frozenset[str]] = frozenset({"name", "status", "message"})

# Host durumlarının allowlist'i. Sıra yoktur; küme, yazan tarafın sabitlerinden
# kurulur ki iki tarafın sözlüğü ayrışamasın.
_PING_HOST_STATUSES: Final[frozenset[str]] = frozenset({REACHABLE, UNREACHABLE, FAILED, NO_RESULT})

# UTC'nin tek geçerli offset'i. Sabit olarak durur ki karşılaştırma her çağrıda
# yeni bir `timedelta` kurmasın ve "UTC" tanımı tek bir yerde yazılsın.
_ZERO_OFFSET: Final[timedelta] = timedelta(0)

# Geçmişte görünebilecek **tek** durum kümesi. `pending`/`running`/`canceled`
# bir ping'in yayımlanmış sonucu yoktur; kapı hem SQL'de hem burada durur.
_TERMINAL_STATUSES: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.SUCCESSFUL, JobStatus.FAILED}
)


class PingHistoryUnavailableError(AppError):
    """Ping geçmişi şu anda sunulabilir değil.

    Tek bir cevap birbirinden çok farklı sebepleri kapsar: dosya yok, symlink,
    aşırı büyük, bozuk JSON, başka bir şema sürümü, başka bir Job'a ait belge
    veya DB satırıyla uyuşmayan bir alan. Ayrım yapmak, dosyanın içeriğini hata
    cevabı üzerinden ölçmeye yarardı — "yanlış inventory" ile "bozuk sayaç"
    arasındaki fark, artifact'te ne olduğunu adım adım daraltmayı mümkün kılar.

    ``503`` bilinçlidir. ``500`` sunucunun kendi hatasını, ``404`` kaydın
    yokluğunu ilan ederdi; buradaki durum ise "geçmiş şu an sunulabilir değil"
    demektir — Job kayıtları vardır ve okunabilir, sunulamayan yalnızca
    sonuçlarıdır (:class:`~app.services.execution.result.JobResultUnavailableError`
    ile aynı gerekçe).

    **Sabitlik constructor'ın kendisiyle kurulur.** Sınıf, atası
    :class:`~app.core.errors.AppError`'ın aksine hiçbir parametre almaz: mesajı
    ve ``details``'i kendisi kurar. Sözleşme bir konvansiyon olarak bırakılsaydı,
    hatayı yükselten her yeni yol kendi metnini geçirebilirdi ve "bütün ihlaller
    ayırt edilemez" iddiası sessizce düşerdi.

    ``details`` her örnekte **yeniden** kurulur; sınıf düzeyinde paylaşılan tek
    bir sözlük, onu değiştiren tek bir çağıran yüzünden sonraki bütün hata
    cevaplarını değiştirirdi.
    """

    status_code = 503
    code = "ping_history_unavailable"

    def __init__(self) -> None:
        super().__init__("Ping geçmişi şu anda okunamıyor.", details={"reason": "unavailable"})


class _RejectedDocument(Exception):
    """Belge sözleşmeyi ihlal etti.

    Yalnız modül içinde taşınan bir işarettir ve **hiçbir ayrıntı taşımaz**:
    mesajı, ihlal eden değeri ve alan adı yoktur. Bir metin taşısaydı, onu bir
    gün hata cevabına veya loga geçiren tek bir satır belgenin içeriğini dışarı
    çıkarırdı.
    """


@dataclass(frozen=True, slots=True)
class PingHistorySummary:
    """Bir ölçümün host durum sayımları.

    Alanlar :class:`~app.services.inventories.ping_confirm.PingSummary` ile
    birebir aynıdır; host **adları** ve mesajları burada yoktur.
    """

    total: int
    reachable: int
    unreachable: int
    failed: int
    no_result: int


@dataclass(frozen=True, slots=True)
class PingHistoryItem:
    """Geçmişte görünen tek bir tamamlanmış ölçüm.

    ``requested_by``, ``artifact_path``, ``project_id``, ``limit``, host adı ve
    host mesajı bilinçli olarak **taşınmaz** (GUVENLIK.md bölüm 3). ``job_id``
    operatörün kaydı ayrıca inceleyebilmesi için durur ve zaten mevcut Job
    okuma yüzeyinin public kimliğidir.
    """

    job_id: str
    status: str
    return_code: int | None
    started_at: datetime
    finished_at: datetime
    summary: PingHistorySummary


@dataclass(frozen=True, slots=True)
class PingHistoryPage:
    """Bir inventory'nin sınırlı ping geçmişi.

    ``inventory_id`` istekten değil, **varlığı doğrulanmış** kayıttan gelir.
    """

    inventory_id: int
    items: tuple[PingHistoryItem, ...]


def list_ping_runs(
    session: Session,
    inventory_id: int,
    *,
    requested_by: str,
    app_data_dir: Path,
    limit: int = DEFAULT_PING_HISTORY_LIMIT,
) -> PingHistoryPage:
    """Bir inventory'nin tamamlanmış ping ölçümlerini en yeni önce döndürür.

    Sıra ``finished_at DESC, id DESC``'tir. İkinci anahtar süs değildir: aynı
    ``finished_at`` değerini taşıyan iki satırın sırası tek anahtarla
    belirsizdir ve aynı sorgu iki çağrıda farklı sıra üretebilirdi.

    Args:
        session: Aktif veritabanı session'ı. Fonksiyon çıkışında — dolu liste,
            boş liste veya hata — açık transaction bırakılmaz.
        inventory_id: Geçmişi istenen inventory'nin kimliği; ``>= 1``.
        requested_by: Geçerli aktör. Sorgu koşuluna **tam eşleşme** olarak girer
            ve cevaba çıkmaz.
        app_data_dir: App-data kökü. Gerçek bir :class:`~pathlib.Path`,
            absolute ve ``..`` bileşeni taşımayan bir POSIX yolu olmalıdır.
            Okuma hedefi yalnız bu kökten ve Job kimliğinden türetilir.
        limit: Azami satır sayısı; :data:`MIN_PING_HISTORY_LIMIT` ile
            :data:`MAX_PING_HISTORY_LIMIT` arasında.

    Returns:
        Değişmez bir :class:`PingHistoryPage`.

    Raises:
        ValueError: **Çağıranın** parametreleri geçersizse. Bu yolda ne SQL ne
            dosya sistemi çalışır.
        NotFoundError: Inventory kaydı yoksa. Bu yolda dosya sistemine hiç
            dokunulmaz.
        PingHistoryUnavailableError: Görünen satırlardan **herhangi birinin**
            artifact'i okunamıyorsa, doğrulamayı geçemiyorsa veya satırla
            tutarsızsa.
    """
    identifier = _require_inventory_id(inventory_id)
    actor = _require_requested_by(requested_by)
    root = _require_app_data_dir(app_data_dir)
    page_size = _require_limit(limit)

    try:
        inventory = get_inventory(session, identifier)
        # Kayıt hâlâ session'a bağlıyken okunur: `rollback` bütün örnekleri
        # expire eder ve sonradan okumak ikinci bir sorgu doğururdu.
        existing_id: int = inventory.id
        rows = session.execute(_visible_statement(actor, identifier, page_size)).all()
        # Dönüşüm rollback'ten **önce** yapılır: dışarı çıkan her değer
        # transaction kapanmadan düz Python değerine dönüşür.
        job_rows = [_to_row(row) for row in rows]
    except Exception:
        # Kapsam bilinçli olarak `SQLAlchemyError`'dan geniştir: `NotFoundError`
        # de, dönüşümde çıkacak bir `TypeError` de buradan geçer. Aksi hâlde
        # 404 yolu çağırana açık bir okuma transaction'ı devrederdi. Hata
        # **çevrilmez**; olduğu gibi yükselir.
        session.rollback()
        raise

    # Salt-okunur servis çağırana açık bir okuma transaction'ı devretmez ve
    # dosya sistemi okuması transaction'ın **dışında** kalır: bir okuma kilidi,
    # sürerken açılan her dosya kadar uzun tutulurdu.
    session.rollback()

    return PingHistoryPage(
        inventory_id=existing_id,
        items=tuple(_to_item(job_row, app_data_dir=root) for job_row in job_rows),
    )


# --- Çağıran sözleşmesi -------------------------------------------------------


def _require_inventory_id(value: int) -> int:
    """Inventory kimliğinin gerçek bir ``int`` ve pozitif olduğunu doğrular.

    Kontrol ``type(...) is int``'tir: ``bool`` ve ``IntEnum`` gibi bütün ``int``
    alt sınıfları tek kuralla elenir. ``True``'nun sessizce "1 numaralı
    inventory" anlamına gelmesi bu ailenin yalnız en görünür örneğidir.
    """
    if type(value) is not int:
        raise ValueError("Inventory kimliği tam sayı olmalıdır.")
    if value < 1:
        raise ValueError("Inventory kimliği pozitif olmalıdır.")
    return value


def _require_requested_by(value: str) -> str:
    """Aktörün gerçek bir ``str`` ve boş olmadığını doğrular.

    ``str`` alt sınıfı ``__eq__`` davranışını değiştirebilir ve sorgu
    koşuluna öyle girerse "tam eşleşme" sözü çağıranın kararına bağlı kalırdı.
    Değer hata mesajına yazılmaz.
    """
    if type(value) is not str or not value:
        raise ValueError("Aktör boş olmayan bir metin olmalıdır.")
    return value


def _require_app_data_dir(value: Path) -> Path:
    """App-data kökünün gerçek, absolute ve ``..`` taşımayan bir yol olduğunu doğrular.

    Sözleşme :func:`~app.services.execution.result_reader._require_app_data_dir`
    ile aynıdır ve burada **tekrar** uygulanır: çağıranın hatası dosya
    sistemine ve SQL'e hiç dokunmadan elenmelidir; aşağıdaki okuyucunun kendi
    kontrolüne güvenmek bu adımı sorgudan sonraya taşırdı.
    """
    if not isinstance(value, Path):
        raise ValueError("App-data kökü bir Path olmalıdır.")
    parts = value.parts
    if not value.is_absolute() or not parts or parts[0] != "/":
        raise ValueError("App-data kökü absolute bir POSIX yolu olmalıdır.")
    if any(part == ".." for part in parts[1:]):
        raise ValueError("App-data kökü `..` bileşeni taşıyamaz.")
    return value


def _require_limit(value: int) -> int:
    """Sayfa boyutunun gerçek bir ``int`` ve geçerli aralıkta olduğunu doğrular."""
    if type(value) is not int:
        raise ValueError("Limit tam sayı olmalıdır.")
    if not MIN_PING_HISTORY_LIMIT <= value <= MAX_PING_HISTORY_LIMIT:
        raise ValueError("Limit izin verilen aralığın dışında.")
    return value


# --- Görünürlük: tek SELECT ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class _JobRow:
    """Bir Job satırının, transaction kapanmadan alınmış düz kopyası."""

    job_id: str
    inventory_id: int
    status: JobStatus
    return_code: int | None
    started_at: datetime
    finished_at: datetime


def _visible_statement(requested_by: str, inventory_id: int, page_size: int) -> Select[Any]:
    """Görünürlüğün **tamamı**: tek bir ``WHERE`` ve tek bir ``ORDER BY``.

    Koşullar şunlardır:

    1. ``Job.job_type == PING`` — PLAYBOOK işleri bu yüzeyde hiç görünmez;
       onların kendi okuma yolu (``GET /api/jobs``) vardır.
    2. ``Job.inventory_id == inventory_id`` — başka bir inventory'nin ölçümü
       Python'a **hiç gelmez**. Satırı alıp sonradan elemek, "görünmüyor"
       sözünü bir kod yoluna bağlardı; burada veritabanı onu hiç döndürmez.
    3. ``Job.requested_by == requested_by`` — aynı gerekçe aktör için.
    4. ``Job.status IN (successful, failed)`` — yalnız terminal ölçümler.
       ``pending``/``running`` bir ping'in henüz sonucu **yoktur**,
       ``canceled`` olanın ise yayımlanmış bir belgesi olmaz.
    5. ``Job.artifact_path == 'jobs/' || Job.id || '/result.json'`` — kayıt
       **tam olarak** bu Job'a ait yayımlanmış sonucu göstermelidir.
       Karşılaştırma veritabanında yapılır ve sütundaki dizgi hiçbir zaman bir
       yol olarak **açılmaz**: okuma hedefi app-data kökü ile Job kimliğinden
       türetilir. Başka bir şey yazılmış bir satır burada elenir, uydurma bir
       dosyaya gidilmez.
    6. ``started_at``/``finished_at`` ``NOT NULL`` — sıralamanın ve cevabın
       zorunlu alanlarının kaynağıdır. ``finish_job`` ikisini de doldurur; boş
       taşıyan bir satır bu yoldan gelmemiştir ve sıralamada sessizce belirsiz
       bir yere düşerdi.

    Sıra ``finished_at DESC, id DESC``'tir ve ``LIMIT`` veritabanındadır:
    fazladan satır alıp Python'da kesmek, sınırın koruduğu işin (satır başına
    bir dosya okuması) bir kısmını yine de yapmak olurdu.
    """
    expected_artifact = literal(f"{JOBS_DIRNAME}/").concat(Job.id).concat(f"/{RESULT_FILENAME}")
    return (
        select(
            Job.id,
            Job.inventory_id,
            Job.status,
            Job.return_code,
            Job.started_at,
            Job.finished_at,
        )
        .where(
            Job.job_type == JobType.PING,
            Job.inventory_id == inventory_id,
            Job.requested_by == requested_by,
            Job.status.in_(sorted(_TERMINAL_STATUSES)),
            Job.artifact_path == expected_artifact,
            Job.started_at.is_not(None),
            Job.finished_at.is_not(None),
        )
        .order_by(Job.finished_at.desc(), Job.id.desc())
        .limit(page_size)
    )


def _to_row(row: Any) -> _JobRow:
    """Bir veritabanı satırını düz, değişmez bir kopyaya çevirir.

    ``started_at``/``finished_at`` burada ``NULL`` kontrolü **görmez**: sorgu
    ikisini de ``NOT NULL`` ile eledi. Burada ayrıca bir fallback kurmak,
    sorgunun gevşemesini sessizce yamalamaktan ibaret olurdu.
    """
    return _JobRow(
        job_id=row.id,
        inventory_id=row.inventory_id,
        status=row.status,
        return_code=row.return_code,
        started_at=_stored_utc(row.started_at),
        finished_at=_stored_utc(row.finished_at),
    )


def _stored_utc(value: datetime) -> datetime:
    """Veritabanından okunan zamanı aware UTC'ye getirir.

    SQLite, ``DateTime(timezone=True)`` sütunlarını **tzinfo olmadan** geri
    verir: offset saklama biçiminin bir parçası değildir. Naive bir değeri UTC
    kabul etmek bu yüzden bir tahmin değil, "DB UTC saklar" sözleşmesinin
    okunmasıdır — uygulamanın yazdığı her damga ``datetime.now(UTC)``'dir.
    Varsayım yalnız **bu yönde** yapılır; belgeden gelen naive bir damga
    (bkz. :func:`_require_utc_moment`) reddedilir.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# --- Artifact okuma ve bağlama ------------------------------------------------


def _to_item(row: _JobRow, *, app_data_dir: Path) -> PingHistoryItem:
    """Satırın yayımlanmış belgesini okur, doğrular ve satıra bağlar.

    Okuyucunun kendi ihlalleri (:class:`~app.services.execution.result.JobResultUnavailableError`)
    ile doğrulayıcının ihlalleri (:class:`_RejectedDocument`) aynı cevaba düşer:
    ikisini ayırmak, dosyanın **durumunu** (var/yok, okunabilir/bozuk) hata
    kodu üzerinden ölçülebilir kılardı.

    **Kimlik doğrulaması da bu sınırın içindedir** (R1-V3J1AF). Dışarıda
    kalsaydı, doğrudan SQL ile yazılmış veya bozuk legacy bir satırın biçimsiz
    ``id``'si private :class:`_RejectedDocument`'ı servisten dışarı kaçırırdı ve
    çağıran, sözleşmedeki sabit ``503`` yerine yakalanmamış bir istisna görürdü.
    Sıra yine korunur: kimlik **okumadan önce** doğrulanır, yani biçimsiz bir ad
    hiçbir zaman bir dizin adı olarak aşağı katmana geçmez.
    """
    try:
        job_id = _require_canonical_job_id(row.job_id)
        document = _read_result_document(
            app_data_dir=app_data_dir,
            job_id=job_id,
            max_result_bytes=MAX_PING_RESULT_BYTES,
        )
        summary = _parse_ping_document(document, row=row)
    except (JobResultUnavailableError, _RejectedDocument):
        # `from None`: okuyucunun ve doğrulayıcının zinciri (errno, path,
        # decoder mesajı) hata cevabına ve loglanan traceback'e taşınmaz.
        raise PingHistoryUnavailableError() from None

    return PingHistoryItem(
        job_id=job_id,
        status=row.status.value,
        return_code=row.return_code,
        started_at=row.started_at,
        finished_at=row.finished_at,
        summary=summary,
    )


def _require_canonical_job_id(value: str) -> str:
    """Satırdaki kimliğin canonical **küçük harfli** UUID4 olduğunu doğrular.

    Kontrol modelin ``@validates`` kancasına bırakılmaz: o kanca yalnız ORM
    üzerinden yazılan satırlarda çalışır, doğrudan SQL ile yazılmış bir satır
    onu hiç görmez. Biçimsiz bir kimlik bir dizin adı olarak aşağı katmana
    geçerse okuyucunun kendi ``ValueError``'ı bu yolu ``500``'e çevirirdi;
    hâlbuki bu, sunulamayan bir kayıttır — cevabı diğer bütün ihlallerle aynı
    olmalıdır.
    """
    if type(value) is not str:
        raise _RejectedDocument
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise _RejectedDocument from None
    if parsed.version != 4 or str(parsed) != value:
        raise _RejectedDocument
    return value


# --- Dar ping belgesi doğrulayıcısı -------------------------------------------


def _parse_ping_document(document: object, *, row: _JobRow) -> PingHistorySummary:
    """Belgeyi katı biçimde doğrular ve DB satırına **exact** bağlar.

    Tek hata sinyali :class:`_RejectedDocument`'tır ve **sıra sözleşmenin
    parçasıdır**:

    1. Üst düzey object şekli ve **tam** alan kümesi.
    2. Kapların temel şekli (``summary`` object, ``hosts`` list).
    3. Şema sürümü ve ``job_type``.
    4. DB bağı: ``job_id``, ``inventory_id``, ``status``, ``return_code``,
       ``started_at``, ``finished_at``.
    5. Dışarı çıkmayan alanların şekli (``project_id``, ``limit``).
    6. ``summary`` sayaçları ve iç tutarlılığı.
    7. ``hosts`` içeriği ve özetle tutarlılığı.

    Belge boyutu burada ikinci kez ölçülmez: ham dosya tavanını
    :func:`~app.services.execution.result_reader._read_result_document` zaten
    uyguladı ve decode edilmiş nesne o tavanın içinden geldi. İkinci bir
    canonical ölçüm, aynı sınırı ikinci bir biçimde tanımlamak olurdu.
    """
    top = _require_mapping(document)
    if set(top) != _PING_RESULT_FIELDS:
        raise _RejectedDocument

    summary_source = top["summary"]
    hosts_source = top["hosts"]
    if type(summary_source) is not dict or type(hosts_source) is not list:
        raise _RejectedDocument

    if _require_int(top["schema_version"]) != RESULT_SCHEMA_VERSION:
        raise _RejectedDocument

    job_type = top["job_type"]
    if type(job_type) is not str or job_type != PING_JOB_TYPE:
        raise _RejectedDocument

    # Kimlik karşılaştırması **ham dizgi** üzerindedir: `uuid.UUID(...)` ile
    # ayrıştırıp karşılaştırmak farklı yazılmış (büyük harfli, süslü parantezli)
    # bir kimliği eşit sayardı ve "bu dosya bu Job'ın" sözü yazım biçimine göre
    # değişen bir söz olurdu.
    job_id = top["job_id"]
    if type(job_id) is not str or job_id != row.job_id:
        raise _RejectedDocument

    if _require_int(top["inventory_id"]) != row.inventory_id:
        raise _RejectedDocument

    status = top["status"]
    if type(status) is not str or status != row.status.value:
        raise _RejectedDocument
    # Satırın kendisi de terminal olmalıdır. Sorgu bunu zaten eledi; kontrol
    # yine de durur ki "geçmişte yalnız terminal ölçüm görünür" sözü tek bir
    # `WHERE` maddesine bağlı kalmasın.
    if row.status not in _TERMINAL_STATUSES:
        raise _RejectedDocument

    return_code = top["return_code"]
    if return_code is not None and type(return_code) is not int:
        raise _RejectedDocument
    if return_code != row.return_code:
        raise _RejectedDocument

    if _require_utc_moment(top["started_at"]) != row.started_at:
        raise _RejectedDocument
    if _require_utc_moment(top["finished_at"]) != row.finished_at:
        raise _RejectedDocument

    # Dışarı çıkmayan iki alan da **şekil** olarak doğrulanır. Doğrulanmayan bir
    # alan, "belgenin tamamı sözleşmeye uyuyor" iddiasını yalnız kısmen doğru
    # kılardı; değerleri yine de hiçbir yere taşınmaz.
    project_id = top["project_id"]
    if project_id is not None and type(project_id) is not int:
        raise _RejectedDocument
    limit_pattern = top["limit"]
    if limit_pattern is not None and type(limit_pattern) is not str:
        raise _RejectedDocument

    summary = _require_summary(summary_source)
    _require_hosts(hosts_source, summary=summary)
    return summary


def _require_mapping(value: object) -> Mapping[str, Any]:
    """Bir JSON **object** olduğunu ve anahtarlarının metin olduğunu doğrular.

    Kontrol ``isinstance`` değil ``type(...) is dict``'tir. Belge bir JSON
    decoder çıktısıdır ve orada object'in tek karşılığı düz ``dict``'tir; bir
    alt sınıf ise **davranış** taşıyabilir: ``__getitem__`` veya ``keys``
    ezilmiş bir eşlemede ``set(value)`` bir alan kümesi gösterirken
    ``value["summary"]`` başka bir şey döndürebilir.
    """
    if type(value) is not dict:
        raise _RejectedDocument
    if not all(type(key) is str for key in value):
        raise _RejectedDocument
    return value


def _require_int(value: object) -> int:
    """Gerçek bir ``int`` olduğunu doğrular; ``bool`` kabul edilmez.

    ``bool`` ``int``'in alt sınıfıdır: ``"total": true`` sessizce ``1`` olur ve
    bozuk bir belge geçerli görünürdü. ``type(...) is int`` onu ayrıca saymaya
    gerek kalmadan eler ve bir ``IntEnum`` alt sınıfını da dışarıda bırakır.
    """
    if type(value) is not int:
        raise _RejectedDocument
    return value


def _require_count(value: object) -> int:
    """Bir sayacın gerçek ``int`` ve **negatif olmadığını** doğrular.

    Negatif bir sayaç toplamı yine tutturabilir (``-1 + 6 == 5``); tutarlılık
    kontrolü tek başına onu yakalamaz.
    """
    count = _require_int(value)
    if count < 0:
        raise _RejectedDocument
    return count


def _require_utc_moment(value: object) -> datetime:
    """ISO 8601 damganın gerçek, timezone-aware **ve** UTC olduğunu doğrular.

    Naive bir damga sessizce UTC sayılmaz: belgenin hangi saat diliminde
    yazıldığına dair bir sözleşme yoktur ve sunucunun yerel saatini UTC ilan
    etmek zaman çizgisini kaydırırdı. UTC dışı bir offset de çevrilmez —
    çevirmek, yanlış bir kaynağın ürettiği damgayı doğruymuş gibi gösterirdi.
    """
    if type(value) is not str:
        raise _RejectedDocument
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _RejectedDocument from None
    offset = parsed.utcoffset()
    if offset is None or offset != _ZERO_OFFSET:
        raise _RejectedDocument
    return parsed.astimezone(UTC)


def _require_summary(source: Mapping[str, Any]) -> PingHistorySummary:
    """``summary``'nin **tam** beş alanını ve iç tutarlılığını doğrular."""
    if set(source) != _PING_SUMMARY_FIELDS:
        raise _RejectedDocument
    total = _require_count(source["total"])
    reachable = _require_count(source["reachable"])
    unreachable = _require_count(source["unreachable"])
    failed = _require_count(source["failed"])
    no_result = _require_count(source["no_result"])
    if reachable + unreachable + failed + no_result != total:
        raise _RejectedDocument
    return PingHistorySummary(
        total=total,
        reachable=reachable,
        unreachable=unreachable,
        failed=failed,
        no_result=no_result,
    )


def _require_hosts(source: list[Any], *, summary: PingHistorySummary) -> None:
    """``hosts`` listesini doğrular ve özetle karşılaştırır.

    Liste **belge içinde** doğrulanır ama dışarı taşınmaz: host adı ve mesajı
    public geçmiş cevabının parçası değildir (GUVENLIK.md bölüm 3). Yine de
    doğrulanır, çünkü doğrulanmayan bir liste özetin doğruluğunu da kanıtsız
    bırakırdı — sayaçlar ancak saydıkları şey görüldüğünde bir şey söyler.

    Adların **tekilliği** ayrıca aranır: aynı host'u iki kez taşıyan bir belge,
    sayımı tutturuyor olsa bile başka bir host'un sonucunu gizliyor demektir.
    """
    if len(source) != summary.total:
        raise _RejectedDocument

    counts = {REACHABLE: 0, UNREACHABLE: 0, FAILED: 0, NO_RESULT: 0}
    names: set[str] = set()
    for entry in source:
        host = _require_mapping(entry)
        if set(host) != _PING_HOST_FIELDS:
            raise _RejectedDocument

        name = host["name"]
        if type(name) is not str or not name:
            raise _RejectedDocument
        if name in names:
            raise _RejectedDocument
        names.add(name)

        status = host["status"]
        if type(status) is not str or status not in _PING_HOST_STATUSES:
            raise _RejectedDocument
        counts[status] += 1

        message = host["message"]
        if message is not None and (
            type(message) is not str or len(message) > MAX_PING_HOST_MESSAGE_LENGTH
        ):
            raise _RejectedDocument

    if (
        counts[REACHABLE] != summary.reachable
        or counts[UNREACHABLE] != summary.unreachable
        or counts[FAILED] != summary.failed
        or counts[NO_RESULT] != summary.no_result
    ):
        raise _RejectedDocument
