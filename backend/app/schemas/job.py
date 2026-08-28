"""Job okuma endpoint'lerinin cevap şemaları (R1-V3D2A1, route bağı R1-V3D2B).

Şemalar ``GET /api/jobs``, ``GET /api/jobs/{job_id}`` ve
``GET /api/jobs/{job_id}/result`` route'larına bağlıdır
(:mod:`app.api.routes.jobs`). Sözleşmenin doğruluk kaynağı yine serileştirme
sınırıdır: :mod:`app.services.execution.read` bir gün yanlışlıkla gevşerse —
aktörü, plan kimliğini, absolute bir yolu ya da serbest metin bir hata kodunu
özete koyarsa — cevap sessizce API'ye taşınmaz, doğrulama sırasında düşer.

Üç kısıt bilinçlidir:

- ``extra="forbid"``: sözleşmede olmayan **hiçbir** alan geçemez. Fail-open bir
  şema, servise sonradan eklenen bir alanı (``requested_by``,
  ``execution_plan_id``, ``artifact_path``) hiç kimse fark etmeden dışarı
  taşırdı.
- ``Literal``/enum: ``job_type`` ve ``status`` metinden değil sabit bir kümeden
  gelir; ``mode`` de aynı yaklaşımla :class:`~app.models.execution_mode.ExecutionMode`
  ile bağlıdır (R1-V3H2A: artık yalnız ``check`` değil, ``check``/``normal``
  ikisi de geçerlidir — ama üçüncü bir değer yine geçemez). Yeni bir
  :class:`~app.models.job.JobStatus` üyesi eklendiğinde cevap kendiliğinden
  genişlemez; sözleşme açıkça güncellenmelidir.
- ``UUID4`` ve :data:`~app.schemas.execution.UtcDatetime`: kimlik canonical
  UUID4, zaman damgaları timezone-aware **ve** UTC olmak zorundadır. Kural
  yalnız modelde veya serviste dursaydı, cevabı doğrudan kuran bir yol onu hiç
  görmeden UUID1 veya yerel saat üretebilirdi.

R1-V3D2A2A ile buraya **sonuç** şemaları da eklendi. Aynı üç kısıt orada da
geçerlidir, üstüne bir dördüncüsü gelir: sayısal ve mantıksal alanlar
``StrictInt``/``StrictBool`` ile yazılır. Pydantic'in varsayılan (lax) kipi
``"3"``'ü ``3``, ``1``'i ``True`` sayar; bir sonuç belgesinin sınırında bu
gevşeklik, parser'ın bilinçle reddettiği ``bool``-as-``int`` karışıklığını
serileştirme tarafından geri açardı.

"""

from __future__ import annotations

from typing import Literal

from pydantic import UUID4, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from app.models import ExecutionMode

# UTC sözleşmesi tek yerde tanımlıdır ve burada **tekrar yazılmaz**. İkinci bir
# kopya, biri gevşediğinde iki endpoint'in farklı zaman sözleşmesi taşımasına
# yol açardı.
from app.schemas.execution import UtcDatetime

# ``error_code`` alanının dışarı çıkabilecek bütün değerleri. Küme
# :data:`app.services.execution.read.PUBLIC_ERROR_CODES` ile aynıdır ama
# ``Literal`` bir tip olduğu için runtime bir frozenset'ten üretilemez; ikisi
# arasındaki eşitlik testle sabitlenir. Serviste bilinmeyen bir kod zaten
# ``unknown_failure``'a daraltılır — burası o daraltmanın ikinci savunmasıdır.
PublicErrorCode = Literal[
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
]

JobStatusLiteral = Literal["pending", "running", "successful", "failed", "canceled"]


class PlaybookJobSummaryResponse(BaseModel):
    """Bir PLAYBOOK Job'ının listede görünen **tam** alan kümesi.

    Aktör, plan/workspace kimliği, manifest digest'i, artifact yolu, worker
    kimliği, kira alanları, absolute path, environment ve argv bilinçli olarak
    **yer almaz** (GUVENLIK.md bölüm 3).

    ``has_recorded_result`` yalnız veritabanı kaydının bu Job'a ait yayımlanmış
    sonucu gösterdiğini söyler. Dosyanın gerçekten mevcut veya okunabilir
    olduğunu **iddia etmez**: filesystem doğrulaması ayrı bir dilimdedir. Alanın
    adı bu yüzden ``result_available`` değildir — okunabilirlik sözü veren bir
    ad, tutulamayacak bir garanti olurdu.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    job_id: UUID4 = Field(description="Job'un canonical UUID4 kimliği")
    job_type: Literal["playbook"]
    status: JobStatusLiteral
    mode: ExecutionMode = Field(
        description="Job'u yetkilendiren plan kaydının kipi: `check` veya `normal`"
    )
    # Veritabanı sütunu nullable'dır, bu alan **değildir**. Okuma sorgusu her
    # ikisini de onay biletinin ``NOT NULL`` alanına eşitler; ``NULL`` taşıyan
    # bir satır o bağı sağlayamaz ve zaten hiç okunmaz. Alanı ``| None``
    # bırakmak, gerçekte oluşamayan bir durumu sözleşmeye yazmak ve her
    # istemciyi ona karşı dallanmaya zorlamak olurdu.
    project_id: int
    # Project kaydından join ile okunur (R1-V3J0B2); istemcide veya ID'den
    # tahmin edilerek üretilmez. ``path``, ``description`` gibi diğer Project
    # alanları burada **yoktur**.
    project_name: str
    inventory_id: int
    # Aynı gerekçeyle Inventory kaydından okunur; ``path`` burada **yoktur**.
    inventory_name: str
    playbook_path: str = Field(description="Project köküne göreli yol")
    return_code: int | None
    error_code: PublicErrorCode | None = Field(
        description="Yalnız `failed` durumunda dolu; sabit sözlükten gelir"
    )
    result_truncated: bool
    has_recorded_result: bool = Field(
        description="Kayıt bu Job'a ait bir sonuç dosyası gösteriyor mu; varlık garantisi değildir"
    )
    created_at: UtcDatetime
    started_at: UtcDatetime | None
    finished_at: UtcDatetime | None


class PlaybookJobCursorResponse(BaseModel):
    """Sonraki sayfanın başlangıç noktası.

    İki alan **birlikte** anlamlıdır: aynı ``created_at``'i taşıyan satırları
    yalnız ``job_id`` ayırır. Cursor opaque bir dizgi değil açık bir çifttir;
    böylece istemci onu doğrudan sorgu parametresi olarak geri verebilir ve
    sunucu tarafında çözülmesi gereken bir sır oluşmaz.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    created_at: UtcDatetime
    job_id: UUID4


class PlaybookJobListResponse(BaseModel):
    """Bir sayfa Job özeti ve devamının olup olmadığı.

    ``next_cursor`` yalnız ``has_more`` doğruyken doludur. Bu bağ şemada değil
    **serviste** zorlanır (:class:`~app.services.execution.read.PlaybookJobPage`
    invariant'ı): sayfayı üreten yer, ikisinin ayrışamayacağı tek yerdir.
    Şemanın burada tekrar kontrol etmesi, ihlali cevabın oluştuğu noktadan
    uzakta yakalayıp gerçek sebebi gizlerdi.

    Toplam kayıt sayısı bilinçli olarak **yoktur**: ``COUNT(*)`` her sayfada
    ikinci bir tam tarama demek olurdu ve iki sorgu arasında değişen bir sayı
    zaten tutulamayan bir söz verirdi.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    items: list[PlaybookJobSummaryResponse]
    has_more: bool
    next_cursor: PlaybookJobCursorResponse | None


# --- Sonuç sözleşmesi (R1-V3D2A2A) -------------------------------------------

# Bir sonuç belgesindeki ``error_code`` alanının **bütün** değerleri. Küme
# yukarıdaki :data:`PublicErrorCode` ile aynı **değildir**: orası bir Job
# satırının taşıyabileceği kodları tarif eder (worker'ın yazdığı
# ``runner_start_failed``, ``workspace_unavailable`` ve recovery kodları dahil),
# burası yalnız :mod:`app.services.execution.normalize`'ın bir **belgeye**
# yazabildiklerini. İkisini birleştirmek, dosyaya hiçbir zaman yazılamayacak bir
# kodu sonuç sözleşmesinde geçerli sayardı.
#
# Eşitliği :data:`app.services.execution.result.RESULT_ERROR_CODES` iledir ve
# testle sabitlenir; ``Literal`` runtime bir ``frozenset``'ten üretilemediği için
# iki tanım ayrı durur.
ResultErrorCode = Literal[
    "runner_failed",
    "playbook_failed",
    "runner_timeout",
    "runner_output_invalid",
    "result_limit_exceeded",
    "runner_no_hosts",
]

# Sonuca girebilen **tek** event türleri; eşitliği
# :data:`app.services.execution.result.RESULT_EVENT_TYPES` iledir. Serbest bir
# ``str`` bırakmak, normalize allowlist'i gevşediğinde yeni event türlerinin
# sessizce dışarı çıkması demek olurdu.
ResultEventType = Literal[
    "playbook_on_task_start",
    "runner_on_ok",
    "runner_on_failed",
    "runner_on_skipped",
    "runner_on_unreachable",
]


class PlaybookResultEventResponse(BaseModel):
    """Sonuçtaki tek bir event'in **tam** alan kümesi.

    ``event_data``, ``res``, ``stdout``, ``stderr``, ``task_args``, ``task_path``,
    ``command`` ve ``argv`` burada **yoktur**; ``extra="forbid"`` sayesinde
    sonradan da giremezler.

    ``host`` ve ``task`` serbest metindir ama **yalnız** bu ikisi metindir:
    normalize katmanı ikisini de maskeleyip kırparak üretir, sonuç okuyucusu da
    uzunluğunu ayrıca doğrular.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    event: ResultEventType
    host: StrictStr | None = Field(description="Event'in hedef host'u; task olaylarında boş")
    task: StrictStr | None = Field(description="Maskelenmiş task adı")
    changed: StrictBool
    failed: StrictBool


class PlaybookHostRecapResponse(BaseModel):
    """Tek bir host için **yalnız sayısal** özet.

    Host adı bu nesnede değil, onu taşıyan eşlemenin anahtarındadır: sayaçların
    yanına bir metin alanı eklemek, recap'i serbest metin taşıyabilen bir yüzeye
    çevirirdi.

    ``ge=0`` bilinçlidir: negatif bir sayaç hiçbir çalıştırmayı tarif etmez ve
    "kaç host başarısız" sorusunu okuyan tarafta anlamsız kılardı.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    ok: StrictInt = Field(ge=0)
    changed: StrictInt = Field(ge=0)
    failures: StrictInt = Field(ge=0)
    unreachable: StrictInt = Field(ge=0)
    skipped: StrictInt = Field(ge=0)
    rescued: StrictInt = Field(ge=0)
    ignored: StrictInt = Field(ge=0)


class PlaybookJobResultResponse(BaseModel):
    """Bir çalıştırma sonucunun dışarı verilebilir **tam** tanımı.

    Public JSON şekli, normalize belgesinin şekliyle **aynıdır**: ``recap`` host
    adına göre bir object, ``events`` bir listedir. İkisi de sözleşmede tarif
    edilmiş modellerden oluşur; serbest bir ``dict[str, Any]`` yüzeyi hiçbir
    seviyede açılmaz.

    ``artifact_path``, ``workspace_id``, ``manifest_digest``, ``requested_by``
    ve ``execution_plan_id`` burada **yoktur**.

    ``ansible_output`` ise R1-V3J3A'dan beri bilinçli olarak **vardır** ve
    Ansible'ın operatörün terminalde gördüğü ham display satırlarını taşır. Bu
    alan "secret-free" **değildir**: credential, playbook kaynak satırı veya
    mutlak path içerebilir (gerekçesi
    :mod:`app.services.execution.normalize` docstring'indedir). Ürün modeli tek,
    güvenilir, profesyonel bir operatördür; alanı koruyan şey sansür değil,
    mevcut aktör bağlı result yetkilendirmesi, normalize/parser katmanındaki
    kesin byte sınırı ve route'un ``Cache-Control: no-store`` başlığıdır.

    Alan yalnız **bu** cevapta bulunur: Job list ve detail şemaları onu taşımaz
    ve ``extra="forbid"`` sayesinde sonradan da taşıyamaz.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    # Sürüm ``Literal[1, 2]`` **değildir** ve bu bilinçlidir: ``Literal``
    # şemasına ``strict`` uygulanamaz, lax kipte ise Pydantic ``True`` ve
    # ``1.0``'ı da ``1`` sayar. Sürümü bool'dan doğabilen bir alan yapmak, tam da
    # parser'ın reddettiği karışıklığı serileştirme sınırından geri açardı.
    # ``ge``/``le`` aralığı iki geçerli sürüme kilitler ve ``StrictInt`` tipi de
    # zorlar.
    #
    # Aralık **iki** değeri kapsar çünkü sürüm 1 belgeleri diskte kalır ve
    # yeniden yazılmaz (gerçek bir migration yoktur). Cevap **şekli** yine tek
    # ve aynıdır: sürüm 1 okunduğunda output alanları ``null``/``false`` döner,
    # böylece frontend tek bir response shape ile çalışır.
    schema_version: StrictInt = Field(
        ge=1, le=2, description="Sonuç şeması sürümü; okunabilir artifact sürümü (1 veya 2)"
    )
    job_id: UUID4 = Field(description="Sonucun ait olduğu Job'un canonical UUID4 kimliği")
    return_code: StrictInt
    outcome: Literal["successful", "failed"]
    error_code: ResultErrorCode | None = Field(
        description="Yalnız `failed` sonuçta dolu; sabit sözlükten gelir"
    )
    recap: dict[str, PlaybookHostRecapResponse] = Field(description="Host adına göre sayısal özet")
    events: list[PlaybookResultEventResponse]
    events_truncated: StrictBool
    result_truncated: StrictBool
    ansible_output: StrictStr | None = Field(
        description=(
            "Ansible'ın ham display çıktısı; sansürlenmez ve secret içerebilir. Çıktı yoksa boş"
        )
    )
    ansible_output_truncated: StrictBool = Field(
        description="Display çıktısı byte sınırı veya sonuç bütçesi nedeniyle kırpıldı mı"
    )
