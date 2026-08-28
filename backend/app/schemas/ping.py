"""Ping preview (T-204A), confirm (T-204B2) ve geçmiş (R1-V3J1A) şemaları."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import UUID4, BaseModel, ConfigDict, Field, StrictInt

# UTC sözleşmesi tek yerde tanımlıdır ve burada **tekrar yazılmaz**. İkinci bir
# kopya, biri gevşediğinde iki endpoint'in farklı zaman sözleşmesi taşımasına
# yol açardı.
from app.schemas.execution import UtcDatetime

# Gövde boyutunu sınırlar. Limit'in **anlam** doğrulaması domain katmanındadır
# (`ping_invalid_limit`); burada yalnızca aşırı büyük bir payload elenir. Böylece
# makul uzunluktaki her değer, biçimsel bir 422 yerine domain kodunu alır.
MAX_LIMIT_PAYLOAD_LENGTH = 4096

# Token her zaman 43 karakterlik base64url'dir; biçim doğrulaması preview
# deposunda yapılır ve bilinmeyen token ile biçimsiz token aynı cevabı üretir.
MAX_TOKEN_LENGTH = 128


class PingPreviewCreate(BaseModel):
    """Ping onay planı isteği.

    ``extra="forbid"``: istemci host pattern'i, modül adı, modül argümanı,
    timeout veya fork sayısı **gönderemez**. Çalıştırılacak modül kodda
    sabittir.
    """

    model_config = ConfigDict(extra="forbid")

    limit: str | None = Field(
        default=None,
        max_length=MAX_LIMIT_PAYLOAD_LENGTH,
        description=(
            "Host pattern'i. Alan hiç verilmezse tüm inventory hedeflenir; boş metin geçersizdir."
        ),
    )


class PingPreviewTokenRequest(BaseModel):
    """Token taşıyan istek gövdesi (cancel ve confirm).

    Token **yalnızca gövdede** taşınır; URL veya query string'e konmaz çünkü
    proxy ve erişim log'ları URL'leri kaydeder.

    ``extra="forbid"``: confirm isteği de limit, timeout, forks, modül, modül
    argümanı veya inventory path'i **kabul etmez**. Çalıştırılacak iş yalnızca
    onaylanmış plandan gelir; istemcinin ikinci bir kanaldan parametre
    geçirebilmesi, onay ile execution arasındaki bağı koparırdı.
    """

    model_config = ConfigDict(extra="forbid")

    preview_token: str = Field(
        min_length=1,
        max_length=MAX_TOKEN_LENGTH,
        description="Preview cevabında bir kez dönen onay token'ı",
    )


class PingPlanInventoryResponse(BaseModel):
    """Plandaki inventory tanıtımı."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    binding: str = Field(description="`project` veya `standalone`")
    project_id: int | None
    project_name: str | None


class PingPlanResponse(BaseModel):
    """Kullanıcının onaylayacağı plan.

    Yalnızca güvenli alanlar bulunur. Adres, kullanıcı, private key yolu ve
    diğer host değişkenleri bilinçli olarak **yer almaz** (GUVENLIK.md bölüm 3).
    """

    model_config = ConfigDict(from_attributes=True)

    inventory: PingPlanInventoryResponse
    operation: str
    operation_effect: str
    limit: str | None
    host_count: int = Field(description="Kesin hedef sayısı; listeden bağımsızdır")
    hosts: list[str] = Field(description="Hedef host adları, ada göre sıralı")
    hosts_truncated: bool = Field(description="Liste yapılandırılmış üst sınırla kırpıldıysa true")
    connection: str
    host_key_policy: str
    become: bool


class PingPreviewResponse(BaseModel):
    """Preview cevabı.

    ``preview_token`` yalnızca burada, bir kez döner. Sunucuda token'ın kendisi
    saklanmaz; state yalnızca SHA-256 özetiyle adreslenir.
    """

    model_config = ConfigDict(from_attributes=True)

    preview_token: str
    expires_at: datetime
    plan: PingPlanResponse


class PingRunHostResponse(BaseModel):
    """Tek bir host'un ping sonucu.

    ``message`` yalnızca ``unreachable`` ve ``failed`` durumlarında doludur ve
    redaction ile uzunluk sınırından geçmiştir. Ham stdout/stderr taşınmaz.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str
    status: str = Field(description="`reachable` | `unreachable` | `failed` | `no_result`")
    message: str | None


class PingRunSummaryResponse(BaseModel):
    """Host durumlarının sayımı."""

    model_config = ConfigDict(from_attributes=True)

    total: int
    reachable: int
    unreachable: int
    failed: int
    no_result: int


class PingRunResponse(BaseModel):
    """Confirm cevabı (T-204B2).

    Token, snapshot içeriği, artifact path'i, controller dosya sistemi
    ayrıntısı, argv ve ham çıktı bilinçli olarak **yer almaz**
    (GUVENLIK.md bölüm 3).
    """

    model_config = ConfigDict(from_attributes=True)

    job_id: str
    job_type: str
    status: str = Field(description="`successful` yalnız rc=0 ve tüm host'lar reachable ise")
    inventory_id: int
    project_id: int | None
    limit: str | None
    return_code: int | None
    started_at: datetime
    finished_at: datetime
    summary: PingRunSummaryResponse
    hosts: list[PingRunHostResponse] = Field(description="Ada göre deterministik sıralı")


# --- Ping geçmişi (R1-V3J1A) --------------------------------------------------
#
# Üç şema da ``extra="forbid"`` taşır ve bu, sözleşmenin asıl kilididir. Geçmiş
# yüzeyi bilinçli olarak **dardır**: host adı, host mesajı, ``requested_by``,
# ``artifact_path``, project/inventory path'i, ``limit``, ``project_id``,
# stdout/stderr, argv, environment, token ve snapshot burada yer almaz
# (GUVENLIK.md bölüm 3). Fail-open bir şema, servise sonradan eklenen bir alanı
# hiç kimse fark etmeden dışarı taşırdı.
#
# Sayısal alanlar ``StrictInt``'tir: Pydantic'in varsayılan (lax) kipi ``"3"``'ü
# ``3``, ``True``'yu ``1`` sayar ve bu, doğrulayıcının bilinçle reddettiği
# ``bool``-as-``int`` karışıklığını serileştirme tarafından geri açardı.


class PingHistorySummaryResponse(BaseModel):
    """Bir ölçümün host durum sayımları.

    Host **adları** ve mesajları burada yoktur; yalnızca kaç host'un hangi
    durumda olduğu bildirilir.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    total: StrictInt
    reachable: StrictInt
    unreachable: StrictInt
    failed: StrictInt
    no_result: StrictInt


class PingHistoryItemResponse(BaseModel):
    """Geçmişte görünen tek bir tamamlanmış ölçümün **tam** alan kümesi.

    ``status`` yalnız terminal iki değeri alabilir: geçmiş, sonucu yayımlanmış
    ölçümlerden oluşur ve ``pending``/``running``/``canceled`` bir satırın
    burada karşılığı yoktur.

    ``return_code`` nullable'dır ve bu gerçek bir durumdur: ``ansible`` ad-hoc
    komutu hiç başlatılamadığında Job ``failed`` olur, yayımlanan belge bütün
    host'ları ``no_result`` gösterir ve bir çıkış kodu **oluşmaz**. Alanı
    zorunlu yapmak, o ölçümü geçmişten silmek ya da uydurma bir kod üretmek
    olurdu.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    job_id: UUID4 = Field(description="Ölçümü üreten Job'un canonical UUID4 kimliği")
    status: Literal["successful", "failed"]
    return_code: StrictInt | None
    started_at: UtcDatetime
    finished_at: UtcDatetime
    summary: PingHistorySummaryResponse


class PingHistoryResponse(BaseModel):
    """Bir inventory'nin sınırlı ping geçmişi.

    ``inventory_id`` istekten değil, **varlığı doğrulanmış** kayıttan gelir.
    Liste en yeni ölçüm başta olacak biçimde sıralıdır ve ``limit`` kadar
    satır taşır; cursor/pagination bu turda bilinçli olarak yoktur.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    inventory_id: StrictInt
    items: list[PingHistoryItemResponse]
