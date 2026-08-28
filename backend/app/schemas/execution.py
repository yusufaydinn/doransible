"""Execution plan önizleme, hazırlama ve çalıştırma endpoint şemaları."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    UUID4,
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from app.models import ExecutionMode

# Gövde boyutunu sınırlar. Yolun **anlamı** burada çözülmez: girdi yalnızca
# keşif sonucuyla birebir karşılaştırılır (`playbook_not_discovered`).
MAX_PLAYBOOK_PATH_LENGTH = 4096

# Bilinçli olarak `strip_whitespace` **yoktur**. Kırpma, güvenlik kararına giren
# bir girdiyi sessizce başka bir değere çevirirdi; kullanıcının gönderdiği metin
# neyse eşleşme de onunla yapılır.
PlaybookPath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_PLAYBOOK_PATH_LENGTH),
]

# Gövde boyutunu sınırlar. Token her zaman 43 karakterlik base64url'dir, ama
# **biçim** doğrulaması burada yapılmaz: tam regex'i şemaya koymak, bilinmeyen
# bir token ile biçimsiz bir token'ı birbirinden ayırt edilebilir kılardı
# (422 ile 409). Makul uzunluktaki her değer plan deposuna ulaşır ve orada tek
# bir generic `execution_plan_invalid` üretir.
MAX_PLAN_TOKEN_LENGTH = 128


def _require_utc(value: datetime) -> datetime:
    """Zaman damgasının gerçekten UTC olduğunu doğrular; **normalize etmez**.

    Sessiz dönüştürme bilinçli olarak reddedilir. Bir cevabı ``+03:00``'ten
    UTC'ye çevirmek, yanlış bir kaynağın ürettiği damgayı doğruymuş gibi
    gösterirdi; naive bir damgayı UTC saymak ise sunucunun yerel saatini UTC
    ilan etmek olurdu. İkisi de zaman çizgisini sessizce kaydırır ve Job'ın ne
    zaman kabul edildiği sorusunun tek doğru cevabı kalmazdı. Bu yüzden yanlış
    girdi düzeltilmez, serileştirme sınırında **düşer**.
    """
    offset = value.utcoffset()
    if offset is None:  # pragma: no cover - `AwareDatetime` bunu zaten eler
        raise ValueError("Zaman damgası timezone-aware olmalıdır.")
    if offset != timedelta(0):
        raise ValueError("Zaman damgası UTC olmalıdır.")
    return value


# Yalnız timezone-aware **ve** UTC. `AwareDatetime` naive değeri eler; offset
# kontrolü UTC dışı bir bölgeyi eler.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_require_utc)]


class ExecutionPlanCreate(BaseModel):
    """Execution plan isteği (R1-V3H2A).

    ``extra="forbid"``: istemci ``limit``, ``tags``, ``skip_tags``,
    ``extra_vars``, ``forks`` veya ``timeout`` **gönderemez**. Bu alanlar bu
    dilimde kapsam dışıdır ve planda sunucu tarafından sabitlenir; ikinci bir
    kanaldan parametre geçirilebilmesi, gösterilen plan ile istenen iş
    arasındaki bağı daha ilk adımda koparırdı.

    ``mode`` bilinçli olarak **zorunludur** ve varsayılanı yoktur:
    :class:`~app.models.execution_mode.ExecutionMode` yalnız ``check`` ve
    ``normal`` üyelerini taşıdığı için bilinmeyen, ``null``, boş veya
    farklı case bir değer (``"Check"``, ``"CHECK"``) Pydantic tarafından
    doğrudan reddedilir ve 422 ``request_validation_error`` üretir. Bir
    varsayılan tanımlanmış olsaydı, kip söylemeyi unutan bir çağrı sessizce
    ``check``'e düşerdi; seçim her istekte **açık** olmak zorundadır.
    """

    model_config = ConfigDict(extra="forbid")

    mode: ExecutionMode = Field(description="Çalıştırma kipi: `check` veya `normal`")
    inventory_id: int = Field(ge=1, description="Aynı project'e bağlı inventory kaydı")
    playbook_path: PlaybookPath = Field(
        description="Project köküne göreli, keşifte listelenmiş playbook yolu"
    )


class ExecutionPlanProjectResponse(BaseModel):
    """Plandaki project tanıtımı; sunucudaki dizin yolu **yer almaz**."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class ExecutionPlanInventoryResponse(BaseModel):
    """Plandaki inventory tanıtımı; dosya yolu **yer almaz**."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    binding: Literal["project"] = Field(description="Bu dilimde daima `project`")


class ExecutionPlanPlaybookResponse(BaseModel):
    """Plandaki playbook tanıtımı.

    ``path`` project köküne görelidir; sunucudaki absolute yol dışarı verilmez
    (GUVENLIK.md bölüm 3).
    """

    model_config = ConfigDict(from_attributes=True)

    path: str
    name: str
    size_bytes: int
    modified_at: datetime


class ExecutionPlanResponse(BaseModel):
    """Çalıştırılamaz plan özeti (R1-V3H2A: ``check`` ve ``normal`` ikisi de).

    Plan token'ı, snapshot içeriği, hostvar, private key yolu, argv ve parser
    çıktısı bilinçli olarak **yer almaz**. ``executable`` daima ``False``'tur ve
    bu cevap ileride çalıştırılabilir bir onay olarak yeniden yorumlanamaz;
    gerçek çalıştırma ayrı bir dilimde, ayrı bir onay mekanizmasıyla gelecektir.

    Değişmez alanlar ``Literal`` ile bağlanmıştır: servis bir gün yanlışlıkla
    ``executable=True`` üretirse cevap sessizce API'ye taşınmaz, serileştirme
    sırasında hata verir. ``mode`` bu turdan itibaren sabit değildir — istekte
    seçilen kipi **aynen** taşır; kaynağı isteğin kendisi değil, hazırlanmış
    (veya önizlenen) domain nesnesinin ``mode`` alanıdır.
    """

    model_config = ConfigDict(from_attributes=True)

    project: ExecutionPlanProjectResponse
    inventory: ExecutionPlanInventoryResponse
    playbook: ExecutionPlanPlaybookResponse
    mode: ExecutionMode = Field(description="Seçilen çalıştırma kipi: `check` veya `normal`")
    limit: Literal[None] = Field(description="Kapsam dışı; daima `null`")
    tags: Literal[None] = Field(description="Kapsam dışı; daima `null`")
    skip_tags: Literal[None] = Field(description="Kapsam dışı; daima `null`")
    host_count: int = Field(description="Kesin hedef sayısı; listeden bağımsızdır")
    hosts: list[str] = Field(description="Ada göre sıralı host adları")
    hosts_truncated: bool = Field(description="Liste üst sınırla kırpıldıysa true")
    connection: Literal["ssh"]
    host_key_policy: str
    become: Literal[False]
    executable: Literal[False] = Field(description="Bu dilimde daima `false`")
    not_executable_reason: Literal["execution_not_enabled"]
    generated_at: datetime


class PreparedExecutionPlanResponse(BaseModel):
    """Onaya hazırlanmış planın cevabı (R1-V2).

    ``plan_token`` **yalnızca burada, bir kez** döner: sunucuda özetinden
    başkası saklanmaz ve aynı token bir daha üretilemez.

    Cevap dondurulmuş workspace'in yolunu, manifest'in tamamını, hostvar'ları,
    private key bilgilerini ve argv'yi **taşımaz**. İç plan ``executable=false``
    olmaya devam eder: çalıştırma yetkisini taşıyan tek şey ``plan_token``'dır,
    plan özetinin kendisi değil. Token'ı tüketen tek yol
    ``POST /api/projects/{project_id}/executions``'tır (R1-V3D1).
    """

    model_config = ConfigDict(from_attributes=True)

    plan_token: str = Field(description="Tek kullanımlık, TTL'li onay token'ı")
    expires_at: datetime = Field(description="Token'ın son geçerlilik anı (UTC)")
    manifest_digest: str = Field(description="Dondurulmuş içeriğin SHA-256 özeti")
    prepared: Literal[True]
    plan: ExecutionPlanResponse


class ExecutionLaunchCreate(BaseModel):
    """Hazırlanmış planı çalıştırmaya alma isteği (R1-V3D1, R1-V3H2A).

    Token **yalnızca gövdede** taşınır; URL'ye, query string'e veya header'a
    konmaz çünkü proxy ve erişim log'ları URL'leri kaydeder.

    ``extra="forbid"``: istemci ``requested_by``, ``fingerprint``,
    ``host_key_policy``, ``connection``, ``become``, ``limit``, ``tags``,
    ``skip_tags`` veya ``extra_vars`` **gönderemez**. Bu değerlerin tamamı ya
    sunucu ayarından ya da onaylanmış planın kendi sabitlerinden gelir; ikinci
    bir kanaldan parametre geçirilebilmesi, kullanıcının onayladığı plan ile
    çalıştırılan iş arasındaki bağı koparırdı.

    ``inventory_id`` ve ``playbook_path`` istemciden alınır ama Job'a **yazılmaz**:
    yalnız claim koşulunda beklenen bağlam olarak kullanılırlar. Yanlış bir
    bağlam hiçbir satırı eşleştirmez ve token'ı tüketmez.

    ``mode`` **istisnadır**: diğerlerinin aksine Job'a yazılan değerin kaynağı
    değildir (o hep claim edilen plan satırıdır), ama yine de bir yazılabilir
    alan değil, yalnız *beklenen* kiptir — fingerprint'e ve atomik claim
    koşuluna girer. ``check`` hazırlanmış bir token'ı ``normal`` ile (ya da
    tersini) çalıştırmaya çalışmak hiçbir satırı eşleştirmez ve token
    tüketilmez; doğru kiple sonraki deneme hâlâ çalışır.
    """

    model_config = ConfigDict(extra="forbid")

    plan_token: str = Field(
        min_length=1,
        max_length=MAX_PLAN_TOKEN_LENGTH,
        description="Hazırlama cevabında bir kez dönen, tek kullanımlık onay token'ı",
    )
    mode: ExecutionMode = Field(
        description="Planın hazırlandığı **beklenen** çalıştırma kipi: `check` veya `normal`"
    )
    inventory_id: int = Field(ge=1, description="Planın hazırlandığı inventory kaydı")
    playbook_path: PlaybookPath = Field(
        description="Planın hazırlandığı, project köküne göreli playbook yolu"
    )


class ExecutionLaunchResponse(BaseModel):
    """Kalıcı olarak rezerve edilmiş ``pending`` PLAYBOOK Job'ının cevabı (R1-V3D1).

    Alan adı bilinçli olarak ``status`` **değil** ``initial_status``'tur: Job
    kalıcı olduğu anda arka plan worker'ı onu bu cevap istemciye ulaşmadan
    alabilir. ``status`` deseydik, cevap tutamayacağı bir güncellik sözü verirdi;
    ``initial_status`` yalnız "kayıt bu durumda oluşturuldu" der. 201 de bu
    yüzden "execution başladı" değil, "Job kalıcı olarak oluşturuldu" demektir.

    Alanlar istek gövdesinden **kopyalanmaz**, claim edilen plandan üretilir:
    cevap, istemcinin ne istediğini değil sunucunun neyi kaydettiğini tarif eder.

    Token, aktör, plan/workspace kimliği, manifest digest'i, absolute path,
    private key bilgisi, environment, argv ve artifact yolu bilinçli olarak
    **yer almaz** (GUVENLIK.md bölüm 3).

    Değişmez alanlar gibi ``job_id`` ve ``accepted_at`` de **tiple** bağlanmıştır,
    yalnız docstring'le değil: kimlik UUID4, zaman damgası UTC olmak zorundadır.
    Kural yalnızca Job modelinin ``@validates``'inde dursaydı, cevabı doğrudan
    kuran bir yol (ileride bir başka servis, bir test yardımcısı) onu hiç
    görmeden UUID1 veya yerel saat üretebilirdi. Serileştirme sınırı bu yüzden
    kendi kontrolünü yapar.
    """

    job_id: UUID4 = Field(description="Oluşturulan Job'un canonical UUID4 kimliği")
    job_type: Literal["playbook"]
    initial_status: Literal["pending"] = Field(
        description="Job'un oluşturulduğu andaki durumu; güncel durum garantisi değildir"
    )
    mode: ExecutionMode = Field(
        description="Claim edilen plan kaydının kipi; istek gövdesinden kopyalanmaz"
    )
    project_id: int
    inventory_id: int
    playbook_path: str = Field(description="Project köküne göreli yol")
    accepted_at: UtcDatetime = Field(description="Planın claim edildiği an (UTC)")
