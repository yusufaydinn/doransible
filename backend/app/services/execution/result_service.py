"""Yetkilendirilmiş, salt-okunur Job sonucu birleşimi (R1-V3D2A2B2).

Bu modül üç ayrı, önceden var olan parçayı **tek** bir public yolda birbirine
bağlar: D2A1'in yetkilendirilmiş Job özeti (:func:`~app.services.execution.read.get_playbook_job`),
D2A2B1'in private ve bounded ``result.json`` okuyucusu
(:func:`~app.services.execution.result_reader._read_result_document`) ve
D2A2A'nın katı normalize sonuç doğrulayıcısı
(:func:`~app.services.execution.result.parse_playbook_result`). Üç parçanın da
kendi sözleşmesi burada **yeniden yazılmaz**; bu modül yalnız sabit sırayla
birbirine bağlar ve DB özeti ile artifact'i son bir kez karşılaştırır. HTTP
route ve UI bu turda hâlâ yoktur.

**Sıra sabittir ve güvenliğin kendisidir.**

1. Çağıranın parametreleri — Job kimliği, app-data kökü, event/byte sınırları —
   yalnız lexical/tip kontrolleriyle doğrulanır. Bu adımda SQL ve dosya
   sistemine **hiç** dokunulmaz.
2. :func:`~app.services.execution.read.get_playbook_job` çağrılır. Yetkilendirmenin
   **tek** kaynağı budur; burada ayrıca bir aktör/project/plan kontrolü
   **kurulmaz**. Fonksiyon kendi rollback'ini yapar, yani bu adımdan sonra
   session'da açık bir transaction yoktur.
3. Job yalnız terminal (``successful``/``failed``) bir durumdaysa ve
   ``has_recorded_result`` tam olarak ``True`` ise devam edilir. Aksi hâlde
   dosya sistemine hiç dokunulmadan sabit bir :class:`JobResultUnavailableError`
   yükselir — ``pending``/``running``/``canceled`` bir Job'ın veya kaydedilmiş
   sonucu olmayan bir Job'ın ``result.json``'ı hiçbir koşulda açılmaz.
4. :func:`~app.services.execution.result_reader._read_result_document` dosyayı
   okur. Hedef yalnız app-data kökü ve canonical Job kimliğinden türetilir; ne
   çağırandan ne DB'deki ``artifact_path``'ten bir yol alınır — D2A1 zaten o
   sütunu bir ``bool``'a indirger ve satırın kendisinde bir yol taşımaz.
5. :func:`~app.services.execution.result.parse_playbook_result` decode edilmiş
   belgeyi katı biçimde doğrular.
6. Ayrıştırılmış sonuç, DB özetiyle ``job_id``, terminal durum ↔ ``outcome``,
   ``return_code``, ``error_code`` ve ``result_truncated`` alanlarında **exact**
   karşılaştırılır. Herhangi biri uyuşmazsa değer düzeltilmez veya biri
   diğerine göre uydurulmaz; sonuç yalnız sabit bir ``JobResultUnavailableError``
   olur.

**Tek hata sözleşmesi.** ``has_recorded_result=False``, terminal olmayan durum,
eksik/bozuk/yanlış-Job'a-ait dosya, parser reddi ve DB ↔ artifact uyuşmazlığı
— hepsi aynı parametresiz :class:`~app.services.execution.result.JobResultUnavailableError`
(503) olur. Çağıranın kendi programlama hatası (biçimsiz kimlik, aralık dışı
sınır) ayrı kalır ve ``ValueError`` olarak yükselir; 503'e **daraltılmaz**.
Yetkisiz veya var olmayan bir Job ise D2A1'in aynı
:class:`~app.services.execution.read.JobNotFoundError` (404) sözleşmesini
korur ve bu yolda dosya sistemine hiç dokunulmaz.

Bu modül runner, worker, subprocess, workspace, token/store, route, frontend
ve artifact writer katmanlarının **hiçbirini** import etmez: yalnız bir okuma
kompozisyonudur, bir çalıştırma veya yazma yolu değildir.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Final

from sqlalchemy.orm import Session

from app.models import JobStatus
from app.services.execution.read import PlaybookJobSummary, get_playbook_job
from app.services.execution.result import (
    MAX_ALLOWED_EVENTS,
    MAX_ALLOWED_RESULT_BYTES,
    MIN_ALLOWED_RESULT_BYTES,
    JobResultUnavailableError,
    PlaybookJobResult,
    parse_playbook_result,
)
from app.services.execution.result_reader import _read_result_document

# Sonucu okunabilecek **tek** durum kümesi. ``pending``/``running``/``canceled``
# bir Job'ın hiçbir zaman kaydedilmiş bir sonucu **olamaz** — ama bu fonksiyon
# o varsayıma güvenmez; kapı burada, DB özetinin durumu üzerinde ayrıca
# uygulanır.
_TERMINAL_STATUSES: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.SUCCESSFUL, JobStatus.FAILED}
)

# Terminal durumun belgedeki karşılığı. Değerler
# :mod:`app.services.execution.normalize`'ın ``OUTCOME_SUCCESSFUL``/``OUTCOME_FAILED``
# sabitleriyle **aynıdır** ama oradan import edilmez: ``JobStatus`` zaten bir
# ``StrEnum``'dur ve üyelerinin dizgi karşılığı bu sabitlerle birebir örtüşür;
# ayrı bir import, aynı iki dizgiyi ikinci bir kaynaktan tekrar tanımlamaktan
# ibaret olurdu.
_EXPECTED_OUTCOME: Final[dict[JobStatus, str]] = {
    JobStatus.SUCCESSFUL: "successful",
    JobStatus.FAILED: "failed",
}


def get_playbook_job_result(
    session: Session,
    job_id: str,
    *,
    requested_by: str,
    app_data_dir: Path,
    max_events: int,
    max_result_bytes: int,
) -> PlaybookJobResult:
    """Yetkilendirilmiş bir Job'ın doğrulanmış sonucunu okur.

    Args:
        session: Aktif veritabanı session'ı. :func:`~app.services.execution.read.get_playbook_job`
            döndüğünde açık bir transaction bırakmaz; bu fonksiyon ondan
            sonra session'a bir daha dokunmaz.
        job_id: Okunacak Job'ın canonical **küçük harfli** UUID4 kimliği. Gerçek
            bir ``str`` olmalıdır; dizin adı davranışını değiştirebilecek bir
            alt sınıf kabul edilmez.
        requested_by: Geçerli aktör. Anlamı ve doğrulaması tümüyle
            :func:`~app.services.execution.read.get_playbook_job`'a aittir; bu
            fonksiyon burada yeni bir normalizasyon **kurmaz**.
        app_data_dir: App-data kökü. Gerçek bir :class:`~pathlib.Path`,
            absolute ve ``..`` bileşeni taşımayan bir POSIX yolu olmalıdır.
            Okuma hedefi yalnız bu kökten ve ``job_id``'den türetilir.
        max_events: Kabul edilecek azami event sayısı; ``1`` ile
            :data:`~app.services.execution.result.MAX_ALLOWED_EVENTS` arasında.
        max_result_bytes: Sonucun canonical compact JSON karşılığı için azami
            boyut; :data:`~app.services.execution.result.MIN_ALLOWED_RESULT_BYTES`
            ile :data:`~app.services.execution.result.MAX_ALLOWED_RESULT_BYTES`
            arasında.

    Returns:
        Değişmez bir :class:`~app.services.execution.result.PlaybookJobResult`.

    Raises:
        ValueError: **Çağıranın** parametreleri geçersizse. Bu yolda ne SQL ne
            dosya sistemi çalışır.
        JobNotFoundError: Job bu aktör için okunabilir değilse (bkz.
            :func:`~app.services.execution.read.get_playbook_job`). Bu yolda
            dosya sistemine hiç dokunulmaz.
        JobResultUnavailableError: Job terminal değilse, kaydedilmiş bir sonucu
            yoksa, sonuç dosyası okunamıyorsa, belge şema/semantik doğrulamasını
            geçemiyorsa veya doğrulanmış belge DB özetiyle **tutarsızsa**. Bütün
            bu yollar aynı sabit, parametresiz hataya düşer.
    """
    identifier = _require_job_id(job_id)
    root = _require_app_data_dir(app_data_dir)
    event_limit = _require_max_events(max_events)
    byte_limit = _require_max_result_bytes(max_result_bytes)

    summary = get_playbook_job(session, identifier, requested_by=requested_by)

    if summary.status not in _TERMINAL_STATUSES or not summary.has_recorded_result:
        raise JobResultUnavailableError()

    document = _read_result_document(
        app_data_dir=root,
        job_id=identifier,
        max_result_bytes=byte_limit,
    )
    result = parse_playbook_result(
        document,
        expected_job_id=identifier,
        max_events=event_limit,
        max_result_bytes=byte_limit,
    )

    _require_consistent_with_summary(summary, result)
    return result


# --- Çağıran sözleşmesi -------------------------------------------------------


def _require_job_id(value: str) -> str:
    """Job kimliğinin gerçek bir ``str`` ve canonical **küçük harfli** UUID4 olduğunu doğrular.

    Tür kontrolü ``isinstance`` değil ``type(...) is str``'dir: ``str`` alt
    sınıfı ``__eq__`` veya ``__str__`` davranışını değiştirebilir ve aşağı
    katmanlara bir dizin adı olarak sızabilir (bkz.
    :mod:`app.services.execution.result_reader`'ın aynı gerekçesi). Değer hata
    mesajına yazılmaz.
    """
    if type(value) is not str:
        raise ValueError("Job kimliği canonical UUID4 olmalıdır.")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Job kimliği canonical UUID4 olmalıdır.") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("Job kimliği canonical UUID4 olmalıdır.")
    return value


def _require_app_data_dir(value: Path) -> Path:
    """App-data kökünün gerçek, absolute ve ``..`` taşımayan bir yol olduğunu doğrular.

    Sözleşme :func:`~app.services.execution.result_reader._require_app_data_dir`
    ile aynıdır ve burada **tekrar** uygulanır: bu fonksiyon çağıranın hatasını
    dosya sistemine hiç dokunmadan elemek zorundadır, aşağıdaki private
    okuyucunun kendi kontrolüne güvenmek bu adımı SQL'den sonraya taşırdı.
    """
    if not isinstance(value, Path):
        raise ValueError("App-data kökü bir Path olmalıdır.")
    parts = value.parts
    if not value.is_absolute() or not parts or parts[0] != "/":
        raise ValueError("App-data kökü absolute bir POSIX yolu olmalıdır.")
    if any(part == ".." for part in parts[1:]):
        raise ValueError("App-data kökü `..` bileşeni taşıyamaz.")
    return value


def _require_max_events(value: int) -> int:
    """Event sınırının gerçek bir ``int`` ve geçerli aralıkta olduğunu doğrular.

    Kontrol ``type(...) is int``'tir: ``bool`` ve ``IntEnum`` gibi bütün ``int``
    alt sınıfları tek kuralla elenir. ``True``'nun sessizce "bir event" anlamına
    gelmesi bu ailenin yalnız en görünür örneğidir.
    """
    if type(value) is not int:
        raise ValueError("Event sınırı tam sayı olmalıdır.")
    if not 1 <= value <= MAX_ALLOWED_EVENTS:
        raise ValueError("Event sınırı izin verilen aralığın dışında.")
    return value


def _require_max_result_bytes(value: int) -> int:
    """Byte sınırının gerçek bir ``int`` ve geçerli aralıkta olduğunu doğrular.

    Taban :data:`~app.services.execution.result.MIN_ALLOWED_RESULT_BYTES`'tır ve
    **yeniden 1'e çekilmez**: bunun altındaki bir bütçe, normalizer'ın her
    koşulda yayımlanabilmesi gereken sabit fail-closed belgesini bile okunamaz
    yapardı (bkz. o sabitin kendi gerekçesi).
    """
    if type(value) is not int:
        raise ValueError("Sonuç byte sınırı tam sayı olmalıdır.")
    if not MIN_ALLOWED_RESULT_BYTES <= value <= MAX_ALLOWED_RESULT_BYTES:
        raise ValueError("Sonuç byte sınırı geçerli aralıkta olmalıdır.")
    return value


# --- DB ↔ artifact tutarlılığı -------------------------------------------------


def _require_consistent_with_summary(
    summary: PlaybookJobSummary, result: PlaybookJobResult
) -> None:
    """Ayrıştırılmış belgenin DB özetiyle **exact** tutarlı olduğunu doğrular.

    Beş alan tek tek karşılaştırılır ve hiçbiri düzeltilmez veya biri diğerine
    göre uydurulmaz: uyuşmazlığın kendisi, dosyanın bu Job'ın güncel kaydını
    taşımadığının kanıtıdır. ``error_code`` karşılaştırması ayrıca DB'nin kendi
    daraltmasını (bkz. :data:`~app.services.execution.read.UNKNOWN_FAILURE`)
    örtük biçimde kapsar: DB tanınmayan bir kodu ``unknown_failure``'a
    daraltmışsa ve belge bilinen bir kod taşıyorsa, ikisi eşit **olamaz** ve
    bu fonksiyon onu bir tutarsızlık olarak eler — ayrı bir özel durum
    yazılmaz.
    """
    if result.job_id != summary.job_id:
        raise JobResultUnavailableError()
    if result.outcome != _EXPECTED_OUTCOME[summary.status]:
        raise JobResultUnavailableError()
    if result.return_code != summary.return_code:
        raise JobResultUnavailableError()
    if result.error_code != summary.error_code:
        raise JobResultUnavailableError()
    if result.result_truncated != summary.result_truncated:
        raise JobResultUnavailableError()
