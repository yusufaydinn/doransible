"""Yayımlanmış normalize sonuç belgesinin katı okunması (R1-V3D2A2A).

Bu modül **saf bir doğrulayıcıdır**. Veritabanına, dosya sistemine, artifact
deposuna, runner'a ve HTTP katmanına dokunmaz; JSON metni bile okumaz. Girdisi
zaten decode edilmiş bir Python nesnesidir, çıktısı değişmez bir dataclass'tır.
``result.json``'ı **açan** bir yol bu turda hâlâ yoktur.

**Yazan taraf güvenilir sayılmaz.** Belgeyi :mod:`app.services.execution.normalize`
üretir, ama üretildiği an ile okunduğu an arasında dosya bir sürüm yükseltmesinden,
elle yapılmış bir düzeltmeden, kısmi bir yazmadan veya doğrudan diske yazılmış
başka bir Job'ın sonucundan geçmiş olabilir. Okuyucunun "bunu biz yazdık" diyerek
alanları olduğu gibi geçirmesi, o andan sonra dosyanın içeriğine ne konursa
onun API yüzeyine çıkması demekti. Bu yüzden burada belge **yeniden** ve tam
olarak doğrulanır.

**Allowlist, denylist değil.** Her seviyede alan kümesi **tam eşitlikle**
ölçülür: fazlası da eksiği de reddedilir. Böylece ``stdout``, ``stderr``,
``event_data``, ``res``, ``task_args``, ``task_path``, ``command``, ``argv``,
``environment``, ``hostvars``, private key içeriği/yolu, token, digest,
``traceback``, ``artifact_path`` ve ``workspace_id`` gibi alanların **hiçbiri**
ayrıca sayılmadan elenir. Adı önceden bilinen bir denylist, yarın eklenecek
alanı kaçırırdı; tam eşitlik ise adı bilinmeyeni de eler.

**İki sürüm, iki tam alan kümesi (R1-V3J3A).** Yazan taraf artık yalnız
:data:`~app.services.execution.normalize.SCHEMA_VERSION` (2) üretir, ama sürüm 1
belgeleri diskte kalır ve gerçek bir migration yoktur. Okuyucu ikisini de kabul
eder ve **hiçbirini diğerine normalize etmez**: dönen nesne artifact'in gerçek
sürümünü taşır. Her sürümün kendi tam alan kümesi vardır
(:data:`RESULT_FIELDS_V1`, :data:`RESULT_FIELDS_V2`) ve karışamazlar — sürüm 1
diye işaretlenmiş ama output alanı taşıyan bir belge de, sürüm 2 diye
işaretlenmiş ama output alanı eksik bir belge de fail-closed reddedilir. Sürüm 1
okunduğunda ``ansible_output``/``ansible_output_truncated`` ``None``/``False``
olur; bu bir varsayım değil, o sürümün tanımıdır.

**``ansible_output`` doğrulanır ama sansürlenmez.** Alan, operatörün terminalde
göreceği **ham** Ansible display metnidir ve "secret-free" sayılmaz (gerekçesi
:mod:`app.services.execution.normalize` docstring'indedir). Burada ölçülen tek
şey **şekli**dir: gerçek bir ``str`` veya ``None``, UTF-8'e kodlanabilir ve
:data:`~app.services.execution.normalize.MAX_ANSIBLE_OUTPUT_BYTES` sınırının
içinde. İçeriğine dokunulmaz; dokunulsaydı okuyucu, yazan tarafın ürettiğinden
başka bir metni "ham çıktı" diye sunardı.

**Fail-closed ve sessiz.** Geçersiz bir belge kısmen düzeltilmez, alanı
silinmez, değeri normalize edilmez ve yerine bir fallback üretilmez: tek çıktı
:class:`JobResultUnavailableError`'dır. Bütün belge kaynaklı ihlaller — yanlış
şema sürümü, yanlış Job kimliği, fazla/eksik alan, yanlış tip, bilinmeyen
event/hata kodu, semantik çelişki, event/byte sınırı — **aynı** koda, aynı
mesaja ve aynı ``details``'e düşer. Hangi alanın hangi değerde takıldığı dışarı
bildirilmez: ihlalin yerini söyleyen bir cevap, dosyanın içeriğini bir hata
mesajı üzerinden okunabilir kılardı.

**Değerler paylaşılır, alan adları kilitlenir.** Event türleri, hata kodları,
sonuç değerleri ve metin sınırı normalize katmanından **import edilir**; burada
yeniden yazılmazlar. Çağıran sınırlarının aralığı da :mod:`app.core.config` ile
paylaşılır. Alan **kümeleri** ise bilinçli olarak açıkça yazılır ve türetilmez:
bu modül şema sürümlerinin tüketici sınırıdır, yazan tarafa eklenen bir alanın
buradan sessizce geçmesi sürümlenmemiş bir genişleme olurdu (bkz.
:data:`RESULT_FIELDS_BY_VERSION`). ``normalize_runner_output`` **çağrılmaz**:
burada üretilen bir şey yoktur, yalnız üretilmiş bir belge okunur.

**Bütçe önce, dönüşüm sonra.** Canonical byte bütçesi nested recap/event
dönüşümünden **önce** ve artımlı olarak uygulanır: ölçüm bütçeyi aşan ilk
parçada durur, tam bir ikinci belge hiçbir zaman bellekte kurulmaz (bkz.
:func:`_parse` ve :func:`_require_within_byte_limit`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from app.core.config import (
    PLAYBOOK_RUNNER_MAX_EVENTS_CEILING,
    PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING,
    PLAYBOOK_RUNNER_MIN_RESULT_BYTES,
)
from app.core.errors import AppError
from app.services.execution.normalize import (
    ALLOWED_EVENTS,
    ERROR_PLAYBOOK_FAILED,
    ERROR_RESULT_LIMIT_EXCEEDED,
    ERROR_RUNNER_FAILED,
    ERROR_RUNNER_NO_HOSTS,
    ERROR_RUNNER_OUTPUT_INVALID,
    ERROR_RUNNER_TIMEOUT,
    LEGACY_SCHEMA_VERSION,
    MAX_ANSIBLE_OUTPUT_BYTES,
    MAX_TEXT_LENGTH,
    OUTCOME_FAILED,
    OUTCOME_SUCCESSFUL,
    SCHEMA_VERSION,
)

# Belgenin her seviyesindeki **tam** alan kümeleri. Kümeler bilinçli olarak
# **açıkça yazılır**; :func:`dataclasses.fields` ile normalize dataclass'larından
# türetilmezler.
#
# Bu modül şema sürümlerinin **tüketici** sınırıdır. Türetilmiş bir küme,
# yazan tarafa eklenen bir alanı okuyan tarafta da kendiliğinden kabul edilir
# kılardı: yeni alan tek bir commit'te normalize'a girer, buradan sessizce geçer
# ve — şema sürümü hiç artmadan — serileştirme sınırına kadar taşınırdı. Yeni bir
# alan, okuyucunun ve cevap şemasının **açıkça** gözden geçirilmesiyle kabul
# edilmelidir; o inceleme de bu üç sabitin elle değiştirilmesidir.
#
# Bedeli, normalize tarafında eklenen bir alanın burada "fazla alan" olarak
# reddedilmesidir ve bu bilinçli bir fail-closed tercihidir: sürümlenmemiş bir
# genişlemenin sessizce okunması, okunamamasından daha kötüdür. Kümelerin bugünkü
# eşitliği testle ayrıca ölçülür.
RESULT_FIELDS_V1: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "job_id",
        "return_code",
        "outcome",
        "error_code",
        "recap",
        "events",
        "events_truncated",
        "result_truncated",
    }
)

# Sürüm 2, sürüm 1'in **üstüne** yalnız iki display-output alanı ekler
# (R1-V3J3A). Küme, birleşimden türetilmiş olsa bile ayrı bir sabittir ve
# sürümün kendi tam alan kümesidir: her sürüm kendi kümesini taşır, biri
# diğerinin alanını kabul edemez.
ANSIBLE_OUTPUT_FIELDS: Final[frozenset[str]] = frozenset(
    {"ansible_output", "ansible_output_truncated"}
)
RESULT_FIELDS_V2: Final[frozenset[str]] = RESULT_FIELDS_V1 | ANSIBLE_OUTPUT_FIELDS

# Sürümden **tam** alan kümesine eşleme. Bir belgenin hangi kümeyle ölçüleceğini
# belgenin kendi ``schema_version``'ı belirler ve bilinmeyen bir sürüm buraya
# hiç giremez: eşlemede olmayan sürüm fail-closed reddedilir.
#
# İki kümenin ayrı durması bilinçlidir. Tek bir "en geniş küme" tutulsaydı,
# sürüm 1 diye işaretlenmiş ama output alanları taşıyan bir belge — yani hiçbir
# writer'ın üretemeyeceği bir belge — sessizce okunurdu. Aynı biçimde, sürüm 2
# diye işaretlenmiş ama output alanı eksik bir belge de kabul edilirdi ve
# okuyucu eksik alanı ``None`` varsayarak olmayan bir ölçümü uydururdu.
RESULT_FIELDS_BY_VERSION: Final[Mapping[int, frozenset[str]]] = MappingProxyType(
    {
        LEGACY_SCHEMA_VERSION: RESULT_FIELDS_V1,
        SCHEMA_VERSION: RESULT_FIELDS_V2,
    }
)

# Okunabilir **bütün** şema sürümleri. Yazan taraf artık yalnız
# :data:`~app.services.execution.normalize.SCHEMA_VERSION` üretir; sürüm 1
# diskte kaldığı için okunmaya devam eder ve gerçek bir migration yoktur.
SUPPORTED_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset(RESULT_FIELDS_BY_VERSION)

RECAP_FIELDS: Final[frozenset[str]] = frozenset(
    {"ok", "changed", "failures", "unreachable", "skipped", "rescued", "ignored"}
)
EVENT_FIELDS: Final[frozenset[str]] = frozenset({"event", "host", "task", "changed", "failed"})

# ``outcome`` alanının alabileceği **bütün** değerler.
RESULT_OUTCOMES: Final[frozenset[str]] = frozenset({OUTCOME_SUCCESSFUL, OUTCOME_FAILED})

# ``error_code`` alanının alabileceği **bütün** değerler: normalize katmanının
# üretebildiği sabit kodlar. Küme
# :data:`app.services.execution.read.PUBLIC_ERROR_CODES` ile aynı **değildir** ve
# ondan türetilmez: orası bir Job **satırının** taşıyabileceği kodları tarif eder
# (worker'ın yazdığı `runner_start_failed`, `workspace_unavailable` ve recovery
# kodları dahil), burası bir sonuç **belgesinin** taşıyabileceklerini. İkisini tek
# listeye bağlamak, dosyaya hiçbir zaman yazılamayacak kodları burada geçerli
# sayardı.
#
# ``schema_version`` belgenin **alan kümesini** korur (bkz.
# :data:`RESULT_FIELDS_BY_VERSION`).
# Var olan sabit bir alanın allowlist **değer** kümesine yeni bir sabit eklemek
# bu dilimde bir sürüm artışı değildir: alan kümesi değişmez, eski belgeler
# okunmaya devam eder ve yeni kodu tanımayan bir okuyucu belgeyi zaten
# fail-closed reddeder. Sürüm ancak alan eklendiğinde/çıkarıldığında artar.
RESULT_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        ERROR_RUNNER_FAILED,
        ERROR_PLAYBOOK_FAILED,
        ERROR_RUNNER_TIMEOUT,
        ERROR_RUNNER_OUTPUT_INVALID,
        ERROR_RESULT_LIMIT_EXCEEDED,
        ERROR_RUNNER_NO_HOSTS,
    }
)

# Sonuca girebilecek **tek** event türleri; normalize'ın allowlist'iyle aynıdır.
RESULT_EVENT_TYPES: Final[frozenset[str]] = frozenset(ALLOWED_EVENTS)

# Çağıranın verebileceği sınırlar. Ayarların kabul ettiği aralık neyse parser'ın
# kabul ettiği de odur; ayrı yazılmış bir kopya, ayarların geçerli saydığı bir
# yapılandırmanın okuma yolunda reddedilmesi demek olurdu.
#
# Tabanın kendisi bir doğruluk kontrolüdür: normalizer sınır aşımında sabit
# boyutlu bir fail-closed belge üretir (``schema_version=2`` için ölçülen 267
# byte) ve bu belge **her koşulda** yayımlanabilir olmalıdır (bkz.
# :data:`~app.core.config.PLAYBOOK_RUNNER_MIN_RESULT_BYTES`). Daha küçük bir
# bütçeyi kabul etmek, okuyucunun production'ın kendi geçerli çıktısını
# reddetmesine yol açardı.
MAX_ALLOWED_EVENTS: Final[int] = PLAYBOOK_RUNNER_MAX_EVENTS_CEILING
MAX_ALLOWED_RESULT_BYTES: Final[int] = PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING
MIN_ALLOWED_RESULT_BYTES: Final[int] = PLAYBOOK_RUNNER_MIN_RESULT_BYTES

# Sonuç belgesinin canonical biçimini üreten encoder.
#
# Ayarlar :meth:`~app.services.execution.normalize.NormalizedRun.serialize` ile
# **birebir** aynıdır: yazan taraf sınırı o biçimde ölçtü, okuyan taraf başka bir
# biçimde ölçseydi aynı belge bir tarafta sınırın altında, diğerinde üstünde
# çıkardı. Encoder tek bir örnektir ama durum taşımaz: ``iterencode`` her çağrıda
# kendi circular-reference işaretçilerini kurar.
_CANONICAL_ENCODER: Final[json.JSONEncoder] = json.JSONEncoder(
    sort_keys=True, separators=(",", ":"), ensure_ascii=True
)


class JobResultUnavailableError(AppError):
    """Bu Job'ın sonucu okunabilir değil.

    Tek bir cevap, birbirinden çok farklı sebepleri kapsar: belge başka bir
    şema sürümünden, başka bir Job'dan, bozuk bir yazmadan veya elle yapılmış
    bir düzenlemeden geliyor olabilir. Ayrım yapmak, dosyanın içeriğini hata
    cevabı üzerinden ölçmeye yarardı — "bilinmeyen event türü" ile "yanlış Job
    kimliği" arasındaki fark, dosyada ne olduğunu adım adım daraltmayı mümkün
    kılar.

    ``503`` bilinçlidir. ``500`` sunucunun kendi hatasını, ``404`` kaydın
    yokluğunu ilan ederdi; buradaki durum ise "sonuç şu an sunulabilir değil"
    demektir — Job kaydı vardır ve okunabilir, sunulamayan yalnızca sonucudur.

    Mesaj ve ``details`` sabittir: ihlal eden değeri, Job kimliğini, alan adını,
    dosya yolunu ve parser'ın kendi hata metnini **taşımaz**.

    **Sabitlik constructor'ın kendisiyle kurulur.** Sınıf, atası
    :class:`~app.core.errors.AppError`'ın aksine hiçbir parametre almaz: mesajı
    ve ``details``'i kendisi kurar. Sözleşme bir konvansiyon olarak yazılıp
    çağıranların ona uymasına bırakılsaydı, hatayı yükselten her yeni yol kendi
    metnini geçirebilirdi ve "bütün ihlaller ayırt edilemez" iddiası, çağıran
    sayısı arttıkça sessizce düşerdi. Artık `raise JobResultUnavailableError()`
    dışında bir kullanım tip denetiminde durur.

    ``details`` her örnekte **yeniden** kurulur. Sınıf düzeyinde paylaşılan tek
    bir sözlük, onu değiştiren tek bir çağıran yüzünden sonraki bütün hata
    cevaplarını değiştirirdi; paylaşılan bir :class:`~types.MappingProxyType` de
    ``AppError.details``'in düz sözlük sözleşmesini bozardı.
    """

    status_code = 503
    code = "job_result_unavailable"

    def __init__(self) -> None:
        super().__init__("Çalıştırma sonucu şu anda okunamıyor.", details={"reason": "unavailable"})


class _RejectedDocument(Exception):
    """Belge sözleşmeyi ihlal etti.

    İçeride taşınan bir işarettir ve **hiçbir ayrıntı taşımaz**: mesajı, offending
    değeri ve alan adı yoktur. Bir metin taşısaydı, onu bir gün hata cevabına
    veya loga geçiren tek bir satır belgenin içeriğini dışarı çıkarırdı.
    """


@dataclass(frozen=True, slots=True)
class PlaybookResultEvent:
    """Sonuç belgesindeki tek bir event'in doğrulanmış hâli.

    Alanlar :class:`~app.services.execution.normalize.NormalizedEvent` ile birebir
    aynıdır; ``event_data``, ``res``, ``task_args``, ``task_path`` ve ``stdout``
    burada **yoktur** ve okunmaz.
    """

    event: str
    host: str | None
    task: str | None
    changed: bool
    failed: bool


@dataclass(frozen=True, slots=True)
class PlaybookHostRecap:
    """Tek bir host için **yalnız sayısal** özet.

    Metin taşımaz: host adı bu nesnenin içinde değil, onu tutan eşlemenin
    anahtarındadır.
    """

    ok: int
    changed: int
    failures: int
    unreachable: int
    skipped: int
    rescued: int
    ignored: int


@dataclass(frozen=True, slots=True, repr=False)
class PlaybookJobResult:
    """Doğrulanmış bir çalıştırma sonucu.

    ``recap`` değiştirilemez bir eşlemedir (:class:`~types.MappingProxyType`):
    ``frozen=True`` yalnız alanın **yeniden atanmasını** engeller, düz bir
    ``dict`` tutulsaydı içeriği dışarıdan değiştirilebilirdi ve "doğrulanmış
    sonuç" doğrulandığı hâliyle kalmazdı.

    ``__repr__`` bilinçli olarak **dar**dır: belgenin içeriğini dökmez. Varsayılan
    dataclass repr'i host adlarını, task metinlerini ve Job kimliğini taşırdı ve
    tek bir ``logger.debug(result)``, ``repr(...)`` çağrısı veya bir test
    çıktısındaki assertion farkı onları görünür kılardı.
    """

    # ``outcome`` ve ``error_code`` sabit kümelerden gelir
    # (:data:`RESULT_OUTCOMES`, :data:`RESULT_ERROR_CODES`) ama ``Literal``
    # olarak yazılmazlar: kümeler normalize katmanının sabitlerinden runtime
    # türetilir ve ``Literal`` runtime bir kümeden üretilemez. Elle yazılmış bir
    # ``Literal``, tam da kaçınılan ikinci doğruluk kaynağı olurdu. Kilit
    # serileştirme sınırındadır (:mod:`app.schemas.job`) ve iki tanımın eşitliği
    # testle sabitlenir.
    schema_version: int
    job_id: str
    return_code: int
    outcome: str
    error_code: str | None
    recap: Mapping[str, PlaybookHostRecap]
    events: tuple[PlaybookResultEvent, ...]
    events_truncated: bool
    result_truncated: bool
    ansible_output: str | None
    ansible_output_truncated: bool

    def __repr__(self) -> str:
        """İçerik değil, **şekil**: sonuç, host sayısı ve event sayısı.

        ``ansible_output`` burada da **yoktur** ve olmaması artık yalnız bir
        gürültü tercihi değil: alan ham display metnidir ve tek bir
        ``repr(result)`` onu log'a düşürürdü.
        """
        return (
            f"PlaybookJobResult(outcome={self.outcome!r}, "
            f"hosts={len(self.recap)}, events={len(self.events)})"
        )


def parse_playbook_result(
    document: object,
    *,
    expected_job_id: str,
    max_events: int,
    max_result_bytes: int,
) -> PlaybookJobResult:
    """Yayımlanmış bir normalize sonuç belgesini katı biçimde doğrular.

    Belge, :meth:`~app.services.execution.normalize.NormalizedRun.to_document`
    çıktısının **decode edilmiş** hâli olmalıdır. Fonksiyon dosya açmaz, JSON
    metni okumaz, veritabanına ve runner'a dokunmaz; girdiyi de **değiştirmez**.

    Args:
        document: Doğrulanacak nesne. Tipi bilinçli olarak ``object``'tir:
            ``dict`` ilan etmek, çağıranın elindeki ``Any``'yi tip sistemi
            üzerinden doğrulanmış gibi göstermek olurdu.
        expected_job_id: Belgenin ait olması gereken Job'ın canonical UUID4
            kimliği. Belgedeki ``job_id`` bununla **byte-for-byte** eşleşmelidir;
            farklı yazılmış bir UUID normalize edilip kabul edilmez. Karşılaştırma
            "bu dosya bu Job'ın mı" sorusunun tek cevabıdır: eşleşmeyen bir belge,
            başka bir çalıştırmanın sonucunu bu Job'ın geçmişi gibi gösterirdi.
        max_events: Kabul edilecek azami event sayısı; ``1`` ile
            :data:`MAX_ALLOWED_EVENTS` arasında.
        max_result_bytes: Canonical compact JSON karşılığının azami boyutu;
            :data:`MIN_ALLOWED_RESULT_BYTES` ile :data:`MAX_ALLOWED_RESULT_BYTES`
            arasında.

    Returns:
        Değişmez bir :class:`PlaybookJobResult`.

    Raises:
        ValueError: **Çağıranın** parametreleri geçersizse (kimlik canonical
            UUID4 değil, sınırlar tam sayı değil, pozitif değil veya tavanı
            aşıyor). Bu yolda belgeye hiç bakılmaz ve SQL/dosya sistemi
            çalışmaz. Hata bilinçli olarak :class:`JobResultUnavailableError`
            **değildir**: yanlış çağrılmış bir fonksiyon ile bozuk bir artifact
            aynı şey değildir ve ikisini tek cevaba bağlamak, bir programlama
            hatasını "sonuç okunamıyor" diye kullanıcıya yansıtırdı.
        JobResultUnavailableError: Belge sözleşmeye uymuyorsa. Bütün ihlaller
            aynı kodu, mesajı ve ``details``'i üretir.
    """
    job_id = _require_expected_job_id(expected_job_id)
    event_limit = _require_limit(max_events, floor=1, ceiling=MAX_ALLOWED_EVENTS)
    byte_limit = _require_limit(
        max_result_bytes, floor=MIN_ALLOWED_RESULT_BYTES, ceiling=MAX_ALLOWED_RESULT_BYTES
    )

    try:
        return _parse(
            document,
            expected_job_id=job_id,
            max_events=event_limit,
            max_result_bytes=byte_limit,
        )
    except _RejectedDocument:
        # `from None`: parser'ın kendi zinciri hata cevabına ve loglanan
        # traceback'e taşınmaz.
        raise JobResultUnavailableError() from None


# --- Çağıran sözleşmesi -------------------------------------------------------


def _require_expected_job_id(value: str) -> str:
    """Beklenen kimliğin canonical **küçük harfli** UUID4 olduğunu doğrular.

    ``str(uuid.UUID(...))`` her zaman küçük harfli canonical biçimi üretir, bu
    yüzden eşitlik kontrolü büyük harfli, süslü parantezli veya tiresiz yazımı
    tek başına eler. Değer hata mesajına **yazılmaz**.
    """
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Beklenen Job kimliği canonical UUID4 olmalıdır.") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("Beklenen Job kimliği canonical UUID4 olmalıdır.")
    return value


def _require_limit(value: int, *, floor: int, ceiling: int) -> int:
    """Sınırın gerçek bir tam sayı ve verilen aralıkta olduğunu doğrular.

    ``bool`` reddedilir: ``int``'in alt sınıfı olduğu için ``True`` sessizce
    "bir event" veya "bir byte" anlamına gelir ve çağıranın hatası, sınırı aşmış
    gibi görünen bir sonuç üretirdi.

    Byte bütçesinin tabanı ``1`` **değildir** (bkz.
    :data:`MIN_ALLOWED_RESULT_BYTES`): normalizer'ın sabit fail-closed belgesinin
    altında kalan bir bütçe, okuyucuya production'ın kendi geçerli çıktısını
    reddettirirdi.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Sonuç sınırları tam sayı olmalıdır.")
    if not floor <= value <= ceiling:
        raise ValueError("Sonuç sınırları izin verilen aralığın dışında.")
    return value


# --- Belge doğrulaması --------------------------------------------------------


def _parse(
    document: object,
    *,
    expected_job_id: str,
    max_events: int,
    max_result_bytes: int,
) -> PlaybookJobResult:
    """Belgenin tamamını doğrular; tek hata sinyali :class:`_RejectedDocument`'tır.

    **Sıra sözleşmenin parçasıdır** ve şöyledir:

    1. Üst düzey object şekli, ``schema_version`` ve o sürümün **tam** alan
       kümesi.
    2. ``recap``/``events`` kaplarının temel şekli ve event **sayısı**.
    3. Canonical JSON **byte bütçesi** (artımlı; bkz.
       :func:`_require_within_byte_limit`). Bütçe belgenin tamamını kapsar:
       ``ansible_output`` da onun içindedir.
    4. Üst düzey skaler alanlar (sürüm 2'de display output alanları dahil).
    5. Nested recap/event dönüşümü.
    6. Semantik invariant'lar.

    Bütçe, nested dönüşümden **önce** gelir ve bu bilinçlidir. Ters sırada,
    milyonlarca recap girdisi taşıyan bir belge önce baştan sona doğrulanır,
    her girdi için bir dataclass kurulur ve **ancak ondan sonra** "zaten çok
    büyüktü" denirdi; yani sınırın koruduğu iş tam olarak yapılmış olurdu. Şimdi
    ölçüm, bütçeyi aşan ilk chunk'ta durur ve nested dönüşüm hiç çalışmaz.

    Temel kap şekli bütçeden **önce** ölçülür çünkü ölçümün kendisi bir kap
    varsayar; alan kümesi de öyle, aksi hâlde sınırsız sayıda anahtar taşıyan
    bir object bütçe adımına kadar taşınırdı.
    """
    top = _require_mapping(document)

    # Sürüm **önce** okunur: belgenin hangi tam alan kümesiyle ölçüleceğini o
    # belirler. Ters sırada tek bir "en geniş küme" kullanmak gerekirdi ve o
    # küme, hiçbir writer'ın üretemeyeceği karma belgeleri (v1 + output alanı,
    # v2 − output alanı) kabul ederdi.
    schema_version = _require_int(top.get("schema_version"))
    fields_for_version = RESULT_FIELDS_BY_VERSION.get(schema_version)
    if fields_for_version is None or set(top) != fields_for_version:
        raise _RejectedDocument

    # Kapların **temel** şekli. Nested içerik burada gezilmez; yalnız bütçe
    # ölçümünün ve event sayısının anlamlı olabilmesi için gereken en az kontrol
    # yapılır.
    recap_source = top["recap"]
    events_source = top["events"]
    if type(recap_source) is not dict or type(events_source) is not list:
        raise _RejectedDocument
    # Sayı kontrolü de bütçeden önce gelir ve ucuzdur: `len` kapları gezmez.
    if len(events_source) > max_events:
        raise _RejectedDocument

    _require_within_byte_limit(top, max_result_bytes=max_result_bytes)

    # Kimlik karşılaştırması **ham dizgi** üzerindedir: `uuid.UUID(...)` ile
    # ayrıştırıp karşılaştırmak, farklı yazılmış (büyük harfli, süslü parantezli)
    # bir kimliği eşit sayardı ve "bu dosya bu Job'ın" sözü yazım biçimine göre
    # değişen bir söz olurdu.
    job_id = top["job_id"]
    if type(job_id) is not str or job_id != expected_job_id:
        raise _RejectedDocument

    return_code = _require_int(top["return_code"])

    outcome = top["outcome"]
    if type(outcome) is not str or outcome not in RESULT_OUTCOMES:
        raise _RejectedDocument

    error_code = top["error_code"]
    if error_code is not None and (
        type(error_code) is not str or error_code not in RESULT_ERROR_CODES
    ):
        raise _RejectedDocument

    events_truncated = _require_bool(top["events_truncated"])
    result_truncated = _require_bool(top["result_truncated"])

    # Sürüm 1 belgesinde alanlar **yoktur** ve okunmaz; ``None``/``False``
    # varsayılan bir doldurma değil, o sürümün tanımıdır. Böylece çağıran tek
    # bir sonuç şekliyle çalışır ve sürüm ayrımını taşımak zorunda kalmaz.
    ansible_output = None
    ansible_output_truncated = False
    if schema_version != LEGACY_SCHEMA_VERSION:
        ansible_output = _require_ansible_output(top["ansible_output"])
        ansible_output_truncated = _require_bool(top["ansible_output_truncated"])

    recap = _require_recap(recap_source)
    events = _require_events(events_source)

    _require_consistency(
        outcome=outcome,
        return_code=return_code,
        error_code=error_code,
        recap=recap,
        events=events,
        events_truncated=events_truncated,
        result_truncated=result_truncated,
    )

    # Sürüm **normalize edilmez**: dönen nesne artifact'in gerçek sürümünü
    # taşır. Hepsini en yeni sürüme yazmak, okunan belgenin taşımadığı bir
    # sözleşmeyi taşıyormuş gibi göstermek olurdu.
    return PlaybookJobResult(
        schema_version=schema_version,
        job_id=job_id,
        return_code=return_code,
        outcome=outcome,
        error_code=error_code,
        recap=MappingProxyType(recap),
        events=events,
        events_truncated=events_truncated,
        result_truncated=result_truncated,
        ansible_output=ansible_output,
        ansible_output_truncated=ansible_output_truncated,
    )


def _require_mapping(value: object) -> Mapping[str, Any]:
    """Bir JSON **object** olduğunu ve anahtarlarının metin olduğunu doğrular.

    Kontrol ``isinstance`` değil ``type(...) is dict``'tir. Belge bir JSON
    decoder çıktısı olmalıdır ve orada object'in tek karşılığı düz ``dict``'tir;
    bir alt sınıf ise **davranış** taşıyabilir. Ölçülebilir fark şudur:
    ``__missing__``, ``__getitem__`` veya ``keys`` ezilmiş bir eşlemede
    doğrulanan değer ile sonradan okunan değer aynı olmak zorunda değildir —
    ``set(value)`` bir alan kümesi gösterirken ``value["events"]`` başka bir şey
    döndürebilir. ``isinstance`` böyle bir nesneyi kabul eder ve doğrulamanın
    tamamını, sonucun geleceği kaynağın kendi kararına bağlardı.
    """
    if type(value) is not dict:
        raise _RejectedDocument
    if not all(type(key) is str for key in value):
        raise _RejectedDocument
    return value


def _require_int(value: object) -> int:
    """Gerçek bir ``int`` olduğunu doğrular; ``bool`` kabul edilmez.

    ``bool`` ``int``'in alt sınıfıdır: ``"return_code": true`` sessizce ``1``
    olur ve bozuk bir belge geçerli görünürdü. ``type(...) is int`` onu ayrıca
    saymaya gerek kalmadan eler ve bir ``IntEnum`` alt sınıfını da dışarıda
    bırakır.
    """
    if type(value) is not int:
        raise _RejectedDocument
    return value


def _require_bool(value: object) -> bool:
    """Gerçek bir ``bool`` olduğunu doğrular; ``0``/``1`` kabul edilmez.

    Sayıyı bayrak yerine geçirmek, ``result_truncated: 0`` yazan bir belgeyi
    "kırpılmamış" ilan etmek olurdu; oysa o belge şemayı hiç izlemiyordur ve geri
    kalanı hakkında da bir şey söylemez.
    """
    if type(value) is not bool:
        raise _RejectedDocument
    return value


def _require_ansible_output(value: object) -> str | None:
    """Ham display metnini doğrular: ``None`` veya sınırlı, UTF-8 bir ``str``.

    Doğrulanan **yalnız şekildir**. İçerik sansürlenmez, kırpılmaz ve
    değiştirilmez: alan bilinçle ham taşınır (bkz. modül docstring'i) ve okuyan
    tarafın onu "temizlemesi", yazan tarafın ürettiğinden başka bir metni "ham
    çıktı" diye sunmak olurdu. Sınırı aşan bir belge de düzeltilmez, reddedilir.

    Sınır :data:`~app.services.execution.normalize.MAX_ANSIBLE_OUTPUT_BYTES`'tır
    ve oradan **import edilir**: yazan taraf metni tam olarak o bütçede keser,
    burada ikinci bir sayı yazılsaydı writer'ın ürettiği en büyük geçerli belge
    okunamayabilirdi. Ölçüm ``len(value)`` değil **UTF-8 byte** üzerindedir;
    karakter sayısı, çok baytlı bir metinde sınırın kendisini ölçmezdi.

    UTF-8'e kodlanamayan bir metin (JSON'un ``\\udXXX`` kaçışlarından doğabilen
    yalnız-surrogate değerler) reddedilir. Böyle bir değer bu sınırdan geçseydi
    byte olarak hiç ölçülemez ve HTTP cevabının serileştirilmesini düşürürdü;
    fail-closed reddetmek, kullanıcıya 500 döndürmekten dürüsttür.
    """
    if value is None:
        return None
    if type(value) is not str:
        raise _RejectedDocument
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _RejectedDocument from None
    if len(encoded) > MAX_ANSIBLE_OUTPUT_BYTES:
        raise _RejectedDocument
    return value


def _require_counter(value: object) -> int:
    """Recap sayacını doğrular: gerçek, negatif olmayan tam sayı."""
    count = _require_int(value)
    if count < 0:
        raise _RejectedDocument
    return count


def _require_text(value: object) -> str:
    """Host/task metnini doğrular: ``str`` ve normalize'ın sınırında.

    Sınır normalize'ın :data:`~app.services.execution.normalize.MAX_TEXT_LENGTH`
    değeridir ve buradan import edilir. Yazan taraf metni zaten o boyda keser;
    daha uzun bir metin, belgenin o katmandan geçmediğini gösterir.
    """
    if type(value) is not str or len(value) > MAX_TEXT_LENGTH:
        raise _RejectedDocument
    return value


def _require_host_name(value: object) -> str:
    """Host adını doğrular: boş olmayan, sınırlı metin.

    Boş ad bilinçli olarak reddedilir: recap anahtarı olarak bir hostu **temsil
    eder** ve boş bir anahtar hiçbir hostu tanımlamaz.
    """
    host = _require_text(value)
    if not host:
        raise _RejectedDocument
    return host


def _require_recap(value: object) -> dict[str, PlaybookHostRecap]:
    """Recap'i doğrular: host adına göre object, yalnız sayısal alanlar.

    Her girdinin alan kümesi **tam** eşitlikle ölçülür. Fazladan bir alan
    (örneğin ``stdout`` veya ``hostvars``) burada elenir; eksik bir alan ise
    sıfır varsayılmaz — eksik sayaç, belgenin başka bir şemadan geldiğini
    gösterir ve onu doldurmak, olmayan bir ölçümü uydurmak olurdu.
    """
    entries = _require_mapping(value)
    recap: dict[str, PlaybookHostRecap] = {}
    for host, counters in entries.items():
        name = _require_host_name(host)
        fields_ = _require_mapping(counters)
        if set(fields_) != RECAP_FIELDS:
            raise _RejectedDocument
        recap[name] = PlaybookHostRecap(
            ok=_require_counter(fields_["ok"]),
            changed=_require_counter(fields_["changed"]),
            failures=_require_counter(fields_["failures"]),
            unreachable=_require_counter(fields_["unreachable"]),
            skipped=_require_counter(fields_["skipped"]),
            rescued=_require_counter(fields_["rescued"]),
            ignored=_require_counter(fields_["ignored"]),
        )
    return recap


def _require_events(value: list[Any]) -> tuple[PlaybookResultEvent, ...]:
    """Event listesini tek tek doğrular.

    Listenin **şekli** ve **uzunluğu** buraya gelmeden ölçülmüştür (bkz.
    :func:`_parse`): sınırı aşmış bir listeyi önce baştan sona doğrulamak,
    sınırın koruduğu şeyi — sınırsız iş — tam olarak yapmak olurdu.
    """
    events: list[PlaybookResultEvent] = []
    for item in value:
        fields_ = _require_mapping(item)
        if set(fields_) != EVENT_FIELDS:
            raise _RejectedDocument

        name = fields_["event"]
        if type(name) is not str or name not in RESULT_EVENT_TYPES:
            raise _RejectedDocument

        host = fields_["host"]
        task = fields_["task"]
        events.append(
            PlaybookResultEvent(
                event=name,
                host=None if host is None else _require_host_name(host),
                task=None if task is None else _require_text(task),
                changed=_require_bool(fields_["changed"]),
                failed=_require_bool(fields_["failed"]),
            )
        )
    return tuple(events)


def _require_within_byte_limit(document: Mapping[str, Any], *, max_result_bytes: int) -> None:
    """Belgenin canonical compact JSON boyutunu **artımlı** olarak ölçer.

    Ölçüm tam bir metin üretmez. ``json.dumps`` çağrılsaydı sınırı ölçmek için
    önce sınırın koruduğu şey yapılırdı: gigabyte'lık bir belgenin **ikinci** bir
    tam kopyası bellekte kurulur, ancak ondan sonra "çok büyük" denirdi.
    :meth:`~json.JSONEncoder.iterencode` ise parça parça üretir ve buradaki
    döngü, toplam bütçeyi aşan **ilk** parçada durur; ötesi hiç üretilmez.

    Biçim :meth:`~app.services.execution.normalize.NormalizedRun.serialize` ile
    **birebir** aynıdır (``sort_keys``, sıkı ayırıcılar, ASCII). Yazan taraf
    sınırı o biçimde ölçtü; okuyan taraf başka bir biçimde ölçseydi aynı belge
    bir tarafta sınırın altında, diğerinde üstünde çıkardı.

    Bu noktada yalnız üst düzey **kap** şekli doğrulanmıştır; içerik hâlâ
    keyfîdir. Bu yüzden serileştirmenin kendi ihlalleri — JSON'a çevrilemeyen bir
    değer (:class:`TypeError`), döngüsel referans veya ``NaN``
    (:class:`ValueError`), aşırı derin iç içelik (:class:`RecursionError`) —
    yakalanır ve aynı generic cevaba düşer. Yakalanan hatanın **metni taşınmaz**:
    ``TypeError`` mesajı offending değerin tipini, ``ValueError`` ise değerin
    kendisini yazabilirdi.
    """
    total = 0
    try:
        for chunk in _CANONICAL_ENCODER.iterencode(document):
            total += len(chunk.encode("utf-8"))
            if total > max_result_bytes:
                raise _RejectedDocument
    except (TypeError, ValueError, RecursionError):
        # `from None`: encoder'ın zinciri ve metni dışarı taşınmaz.
        raise _RejectedDocument from None


def _require_consistency(
    *,
    outcome: str,
    return_code: int,
    error_code: str | None,
    recap: Mapping[str, PlaybookHostRecap],
    events: tuple[PlaybookResultEvent, ...],
    events_truncated: bool,
    result_truncated: bool,
) -> None:
    """Alanları tek tek geçerli olan bir belgenin **kendisiyle** tutarlılığı.

    Tip doğrulaması burada yetmez: her alanı ayrı ayrı geçerli, birlikte
    imkânsız bir belge kurulabilir. "Başarılı ama hata kodu dolu", "başarılı ama
    kırpılmış", "başarılı ama hiçbir host işlenmemiş" ya da "başarılı ama recap'te
    başarısız host var" — hepsi tek tek geçerli alanlardan oluşur ve hiçbiri
    :mod:`~app.services.execution.normalize` tarafından üretilemez.

    ``successful`` için kanıt aranır çünkü yanlış yönde hata pahalıdır: başarısız
    bir çalıştırmayı başarılı göstermek, kullanıcının yapılmadığı hâlde
    yapıldığını sandığı bir işlem demektir.

    Event'lerin hostları recap'te bulunmalıdır: recap çalıştırmanın **kapsamıdır**
    ve orada olmayan bir host, event listesinin başka bir çalıştırmadan geldiğini
    ya da recap'in kapsamı olduğundan dar gösterdiğini söyler.
    """
    if outcome == OUTCOME_SUCCESSFUL:
        if return_code != 0 or error_code is not None:
            raise _RejectedDocument
        if events_truncated or result_truncated:
            raise _RejectedDocument
        if not recap:
            raise _RejectedDocument
        if any(entry.failures > 0 or entry.unreachable > 0 for entry in recap.values()):
            raise _RejectedDocument
    else:
        # `failed` bir sonucun sebebi **her zaman** bildirilir; kodsuz bir
        # başarısızlık, okuyan tarafa "bir şey oldu" demekten ibaret olurdu.
        if error_code is None:
            raise _RejectedDocument

    # Kırpılmış bir sonuç tam sanılamaz. Kural yukarıdaki `successful` dalından
    # çıkarılabilir ama ayrıca yazılır: sözleşmenin bu maddesi, dalların ileride
    # yeniden düzenlenmesinden bağımsız olarak ayakta kalmalıdır.
    if result_truncated and outcome == OUTCOME_SUCCESSFUL:
        raise _RejectedDocument

    for event in events:
        if event.host is not None and event.host not in recap:
            raise _RejectedDocument
