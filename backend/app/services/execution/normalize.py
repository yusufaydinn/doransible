"""Runner JSON event akışının güvenli, kalıcı şemaya dönüşümü (R1-V3C1B).

Bu modül **saf bir dönüşüm katmanıdır**: veritabanına, dosya sistemine, raw
artifact'a ve HTTP modeline dokunmaz. Girdisi bir metin ve birkaç sayıdır,
çıktısı deterministik bir veri yapısıdır. Böylece "hangi alan dışarı çıkıyor"
sorusu tek bir yerde ve testlerle ölçülebilir biçimde cevaplanır.

**İki ayrı yüzey.** Sonuç belgesi bu turdan (R1-V3J3A) sonra birbirine
karıştırılmaması gereken iki şey taşır:

1. **Structured yüzey** — ``recap`` ve ``events``. Burada allowlist geçerlidir
   ve dar tutulur: ``event_data.res`` modülün tam sonucudur, ``task_path``
   sunucudaki mutlak yoldur, ``task_args`` çağrının kendisidir. Bu yapılara
   **hiçbir koşulda** girilmez; yalnız event türü, host adı, task adı ve iki
   boolean alınır. Host/task metinleri ayrıca maskelenir ve kırpılır.
2. **Display yüzeyi** — ``ansible_output``.
   Ansible'ın operatörün terminalde gördüğü satırlarıdır ve **bilinçli olarak
   ham taşınır**.

**Display yüzeyi "secret-free" değildir ve öyle sayılmamalıdır.** Ürün modeli
tek, güvenilir, profesyonel bir operatördür: CLI'da göreceği çıktıyı UI'da da
görebilir. Bu metin credential, playbook kaynak satırı, mutlak path veya başka
hassas bilgi içerebilir. Ölçüldü: ``no_log: true`` işaretli bir task'ın
**payload'ı** Ansible tarafından sansürlenir ama sonraki bir hata event'inin
``stdout`` alanı, hatanın kaynağını gösterirken o task'ın playbook **kaynak
satırını** — yani sansürlenen değeri — geri yazabilir. Bu ihtimal inkâr
edilmez; ``no_log`` davranışı korunur ama platform onun eksiksiz bir gizlilik
garantisi olduğunu **iddia etmez**.

Bu yüzden burada redaction **uygulanmaz**. Yarım bir maskeleme, ham olmayan bir
metni "ham çıktı" diye sunmak olurdu; sözleşme ya gerçek display output'u
taşımak ya da hiç taşımamaktır. Platformun eklediği korumalar başka
katmanlardadır: aktör bağlı result yetkilendirmesi, buradaki kesin byte sınırı,
``Cache-Control: no-store`` ve frontend'in plain-text render'ı.

**Kaynak tek ve dardır.** Display metni **yalnız** her event object'inin üst
düzey ``stdout`` alanından toplanır. ``event_data.res.stdout``,
``event_data.res.stderr``, ``res.msg``, task args ve sürecin kendi
``stderr``'i okunmaz; JSON event document'inin kendisi de kullanıcı çıktısı
değildir. Böylece "ham" olan yüzeyin sınırı ölçülebilir kalır.

**Fail-closed.** Geçersiz JSON, beklenmeyen şekil, bilinmeyen host, aşılmış
sınır ve kayıp terminal event durumlarında kısmi bir sonuç "başarılı ve tam"
gibi sunulmaz: sonuç ``outcome="failed"`` olur, sabit bir ``error_code``
taşır ve **event listesi ile recap boş bırakılır**. Kırpma olduğunda
``events_truncated``/``result_truncated`` bunu açıkça söyler; hiçbir yolda
kırpılmış bir liste tam bir liste gibi görünmez.

**"Başarılı" bir sonuç kanıt ister.** ``rc=0`` ve bir ``playbook_on_stats``
satırının varlığı tek başına yeterli sayılmaz; ikisi de bir şeyin gerçekten
çalıştığını söylemez:

- Terminal event **son** non-empty satır olmalıdır ve **tek** olmalıdır. Birden
  fazla terminal event veya ondan sonra gelen bir satır, akışın ya kırpıldığını
  ya da iki çalıştırmanın karıştığını gösterir (``runner_output_invalid``).
- ``event_data.processed`` yapısal olarak doğrulanır ve **boş olamaz**: hiçbir
  host'a dokunmamış bir çalıştırma ``rc=0`` dönse bile başarılı değildir
  (``runner_no_hosts``). Boş bir recap'i "her şey yolunda" diye sunmak,
  inventory'si hiç eşleşmemiş bir playbook'u başarı sanmak olurdu.
- Recap sayaçlarında görünen her host ``processed`` kümesinde de bulunmalıdır ve
  ``processed`` hostların hepsi — sayaçları sıfır olsa bile — recap'te temsil
  edilir. Aksi hâlde recap, çalıştırmanın kapsamını olduğundan dar gösterirdi.
- ``rc=0`` olsa bile recap'te ``failures`` veya ``unreachable`` varsa sonuç
  başarısızdır.

**Başarısızlığın iki sınıfı.** Yukarıdaki üç kanıt (tek ve son terminal event,
boş olmayan ve yapısal olarak doğrulanmış ``processed``, kapsamıyla tutarlı
recap) birlikte yalnız şunu söyler: runner çıktısı yapısal olarak güvenilir,
tek ve terminal bir Ansible sonucu içeriyor; ``processed`` boş değil ve recap
bu kapsamla tutarlı. Bu kanıt varken Ansible'ın terminal recap'i ``failures``
veya ``unreachable`` bildiriyorsa sonuç :data:`ERROR_PLAYBOOK_FAILED`'dır:
**güvenilir bir terminal recap'te** bazı task'ların başarısız olduğu ya da bazı
hostlara erişilemediği raporlanmıştır.

Kanıt bundan fazlasını **söylemez**. Hedef hosta mutlaka ulaşıldığını, her
task'ın gerçekten çalıştığını ya da kök nedenin altyapı dışında olduğunu
göstermez; ``unreachable`` zaten bunun tersini raporlayan sayaçtır. Kod bir kök
neden sınıflandırması değil, sonucun **nerede raporlandığının** kaydıdır.

Kanıtın eksik olduğu her yol :data:`ERROR_RUNNER_FAILED` olarak kalır ve o kod
**altyapı arızası biçiminde yeniden tanımlanmaz**: terminal event'i olmayan bir
erken çıkış, kapsamı boş bir çalıştırma ve recap'i temizken sıfırdan farklı bir
``rc`` dönen bir çalıştırma da oradadır. ``runner_failed`` legacy catch-all'dır.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.services.security.redaction import REDACTED, redact_text

# Kalıcı olabilecek şemanın sürümü. Alan eklendiğinde/çıkarıldığında artar;
# okuyan taraf sürümü görmeden bir belgeyi yorumlamamalıdır.
#
# **Sürüm 1** diskte kalmaya devam eder: R1-V3J3A'dan önce yayımlanmış bütün
# ``result.json`` belgeleri o sürümü taşır ve gerçek bir migration yoktur.
# Yazan taraf artık **yalnız** sürüm 2 üretir; okuyan taraf ikisini de kabul
# eder ve hiçbirini diğerine normalize etmez (bkz.
# :mod:`app.services.execution.result`).
LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2

OUTCOME_SUCCESSFUL = "successful"
OUTCOME_FAILED = "failed"

ERROR_RUNNER_FAILED = "runner_failed"
# Güvenilir bir terminal recap'te task failure veya unreachable host raporlandı.
# Kök nedeni **sınıflandırmaz**. Ayrı bir koddur: ``runner_failed`` hem bunu hem
# de kanıtsız çalıştırma arızalarını (terminal event yok, kapsam boş, recap ile
# ``rc`` çelişiyor) taşıyordu ve okuyan taraf ikisini ayıramıyordu. Kanıt kümesi
# modül docstring'indedir; kod yalnız o kanıt tamken üretilir ve executor'ın
# artifact/cleanup/lease arızalarından **hiçbir** yolda çıkamaz.
ERROR_PLAYBOOK_FAILED = "playbook_failed"
ERROR_RUNNER_TIMEOUT = "runner_timeout"
ERROR_RUNNER_OUTPUT_INVALID = "runner_output_invalid"
ERROR_RESULT_LIMIT_EXCEEDED = "result_limit_exceeded"
# Çalıştırma tamamlandı, terminal event geçerli, ama **hiçbir host işlenmedi**.
# Ayrı bir koddur: bu bir çıktı bozukluğu değil, kapsamı boş bir çalıştırmadır.
ERROR_RUNNER_NO_HOSTS = "runner_no_hosts"

# Sonuca alınan **tek** event türleri. Liste dar tutulur: her yeni tür, dışarı
# çıkan alan yüzeyini genişletir ve yeni bir sızıntı yolu açar.
TASK_EVENT = "playbook_on_task_start"
STATS_EVENT = "playbook_on_stats"
HOST_EVENTS = (
    "runner_on_ok",
    "runner_on_failed",
    "runner_on_skipped",
    "runner_on_unreachable",
)
ALLOWED_EVENTS = (TASK_EVENT, *HOST_EVENTS)

# Terminal event içinde çalıştırmanın **kapsamını** taşıyan alan: dokunulan
# hostların sayacı. Ölçüldü (ansible-runner 2.4.3): `playbook_on_stats`
# `event_data` sözlüğü `processed` alanını her zaman taşır.
PROCESSED_FIELD = "processed"

# Recap sayaçlarının şemadaki adları ve runner'ın `playbook_on_stats`
# içindeki karşılıkları. `dark`, Ansible'ın "unreachable" sayacıdır.
RECAP_COUNTERS: tuple[tuple[str, str], ...] = (
    ("ok", "ok"),
    ("changed", "changed"),
    ("failures", "failures"),
    ("unreachable", "dark"),
    ("skipped", "skipped"),
    ("rescued", "rescued"),
    ("ignored", "ignored"),
)

# Host ve task metinlerinin üst sınırı. Uzun bir ad tek başına secret değildir
# ama sınırsız metin, byte bütçesini tek bir event ile tüketebilirdi.
MAX_TEXT_LENGTH = 200

# Display output'un **tek** kaynağı: event object'inin **üst düzey** ``stdout``
# alanı. ``event_data.res.stdout``, ``event_data.res.stderr`` ve sürecin kendi
# ``stderr``'i bilinçle dışarıdadır: kaynağı dar tutmak, "ham" olan yüzeyin
# sınırının ölçülebilir kalmasının tek yoludur.
DISPLAY_OUTPUT_FIELD = "stdout"

# Toplanan satırların ayıracı. Metin başka hiçbir biçimde dönüştürülmez: ANSI
# dizileri, sekmeler ve boşluklar JSON'dan decode edildiği hâliyle taşınır.
# HTML/ANSI render'ı backend'in konusu değildir.
DISPLAY_OUTPUT_SEPARATOR = "\n"

# Saklanan display output'un **kesin** üst sınırı; ölçüm UTF-8 byte üzerindedir.
#
# Bu sabit tek doğruluk kaynağıdır: yazan taraf metni burada keser, okuyan taraf
# (:mod:`app.services.execution.result`) aynı sabiti **import eder**. İki ayrı
# sayı yazılsaydı, yazanın ürettiği en büyük geçerli belge okuyanda
# reddedilebilirdi.
#
# 128 KiB, tek bir çalıştırmanın ekran çıktısını pratikte taşıyacak kadar büyük,
# genel sonuç bütçesini (varsayılan 1 MB) tek başına tüketmeyecek kadar küçüktür.
MAX_ANSIBLE_OUTPUT_BYTES = 128 * 1024


@dataclass(frozen=True)
class NormalizedEvent:
    """Tek bir runner event'inin güvenli özeti."""

    event: str
    host: str | None
    task: str | None
    changed: bool
    failed: bool

    def to_document(self) -> dict[str, Any]:
        """Serileştirilebilir sözlük karşılığı."""
        return {
            "event": self.event,
            "host": self.host,
            "task": self.task,
            "changed": self.changed,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class HostRecap:
    """Tek bir host için **yalnız sayısal** özet."""

    ok: int
    changed: int
    failures: int
    unreachable: int
    skipped: int
    rescued: int
    ignored: int

    def to_document(self) -> dict[str, int]:
        """Serileştirilebilir sözlük karşılığı."""
        return {
            "ok": self.ok,
            "changed": self.changed,
            "failures": self.failures,
            "unreachable": self.unreachable,
            "skipped": self.skipped,
            "rescued": self.rescued,
            "ignored": self.ignored,
        }


@dataclass(frozen=True)
class NormalizedRun:
    """Bir çalıştırmanın kalıcı olabilecek sonucu.

    İki yüzey taşır (bkz. modül docstring'i): ``recap``/``events`` allowlist'ten
    geçmiş **structured** özettir, ``ansible_output`` ise operatörün terminalde
    göreceği **ham** display metnidir ve "secret-free" sayılmaz.

    ``ansible_output`` bilinçli olarak ``repr=False``'tur. Varsayılan dataclass
    repr'i 128 KiB'lık ham çıktıyı basardı ve tek bir ``logger.debug(run)``,
    ``repr(...)`` çağrısı ya da bir test assertion farkı onu log'a düşürürdü.
    Sözleşme "ham çıktı yalnız yetkili result cevabında bulunur" der; repr o
    cevap değildir.
    """

    schema_version: int
    job_id: str
    return_code: int
    outcome: str
    error_code: str | None
    recap: dict[str, HostRecap]
    events: tuple[NormalizedEvent, ...]
    events_truncated: bool
    result_truncated: bool
    ansible_output: str | None = field(repr=False)
    ansible_output_truncated: bool

    def to_document(self) -> dict[str, Any]:
        """Kalıcı hâle getirilebilecek sözlük karşılığı."""
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "return_code": self.return_code,
            "outcome": self.outcome,
            "error_code": self.error_code,
            "recap": {host: recap.to_document() for host, recap in self.recap.items()},
            "events": [event.to_document() for event in self.events],
            "events_truncated": self.events_truncated,
            "result_truncated": self.result_truncated,
            "ansible_output": self.ansible_output,
            "ansible_output_truncated": self.ansible_output_truncated,
        }

    def serialize(self) -> str:
        """Deterministik JSON metni.

        ``sort_keys`` ve sabit ayırıcılar bilinçlidir: aynı girdi her zaman aynı
        baytı üretmelidir, aksi hâlde byte sınırı ölçümü çalışmadan çalışmaya
        değişirdi.
        """
        return json.dumps(
            self.to_document(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )


def normalize_runner_output(
    *,
    job_id: str,
    stdout_text: str,
    return_code: int,
    timed_out: bool,
    oversized_stream: str | None,
    raw_limit_exceeded: bool,
    known_hosts: Sequence[str],
    connection_values: Sequence[str],
    max_events: int,
    max_result_bytes: int,
) -> NormalizedRun:
    """Runner ``--json`` çıktısını güvenli, sınırlı bir sonuca çevirir.

    Args:
        job_id: Job kimliği. Yalnız taşınır; burada bir kaynak açılmaz.
        stdout_text: Runner sürecinin ham stdout metni. JSON event akışı olarak
            ayrıştırılır; metnin **kendisi** sonuca girmez. Event'lerin üst düzey
            ``stdout`` alanları ise ``ansible_output``'a bounded biçimde taşınır
            (bkz. :func:`_display_output`).
        return_code: Sürecin çıkış kodu.
        timed_out: Süreç timeout ile sonlandırıldı mı.
        oversized_stream: stdout/stderr sınırı aşıldıysa akışın adı.
        raw_limit_exceeded: Raw artifact bütçesi aşıldı mı.
        known_hosts: Dondurulmuş inventory snapshot'ından bilinen host adları.
            Recap **yalnız** bu kümedeki hostlar için üretilir.
        connection_values: Snapshot'taki bağlantı değerleri (kullanıcı adı,
            adres, port, anahtar yolu ...). Metinlerde maskelenirler.
        max_events: İşlenecek azami runner event'i.
        max_result_bytes: Serileştirilmiş sonucun azami boyutu.

    Returns:
        Her yolda bir :class:`NormalizedRun`. Arıza durumlarında ``outcome``
        ``failed``, ``error_code`` doludur, recap/event listesi **boştur** ve
        ``ansible_output`` ``None``'dır: yapısal olarak güvenilmez bir akıştan
        ham metin kurtarılmaz. Display output yalnız yayımlanabilir normalize
        sonuç yolunda taşınır ve orada ``successful`` da ``playbook_failed`` de
        onu taşıyabilir.
        ``rc=0`` ve geçerli bir terminal event yetmez: hiçbir host işlenmemişse
        sonuç ``runner_no_hosts``, recap'te başarısız/erişilemeyen host varsa
        ``playbook_failed`` olur. Kanıtı eksik kalan her başarısızlık —
        terminal event yok, kapsam boş, recap temizken ``rc`` sıfırdan farklı —
        ``runner_failed`` olarak kalır.
    """
    known = set(known_hosts)
    masks = _ordered_masks(connection_values)

    if timed_out:
        return _failed(job_id, return_code, ERROR_RUNNER_TIMEOUT, events_truncated=True)
    if oversized_stream is not None or raw_limit_exceeded:
        return _failed(job_id, return_code, ERROR_RESULT_LIMIT_EXCEEDED, events_truncated=True)

    lines = [line for line in stdout_text.splitlines() if line.strip()]
    if len(lines) > max_events:
        return _failed(job_id, return_code, ERROR_RESULT_LIMIT_EXCEEDED, events_truncated=True)

    try:
        documents = [_require_event_document(line) for line in lines]
    except _InvalidOutputError:
        return _failed(job_id, return_code, ERROR_RUNNER_OUTPUT_INVALID)

    try:
        stats = _terminal_stats_event(documents)
    except _InvalidOutputError:
        return _failed(job_id, return_code, ERROR_RUNNER_OUTPUT_INVALID)

    if stats is None:
        # rc başarısızsa terminal event'in yokluğu şaşırtıcı değildir (syntax
        # hatası, erken çıkış); sonuç yine de "başarısız" olarak bildirilir ama
        # elde kalan kısmi event listesi tam sanılmasın diye taşınmaz.
        if return_code != 0:
            return _failed(job_id, return_code, ERROR_RUNNER_FAILED, events_truncated=True)
        return _failed(job_id, return_code, ERROR_RUNNER_OUTPUT_INVALID)

    try:
        processed = _require_processed_hosts(stats, known)
        recap = _build_recap(stats, known, processed)
        events = tuple(
            _safe_event(document, masks=masks, known=known)
            for document in documents
            if document.get("event") in ALLOWED_EVENTS
        )
    except _InvalidOutputError:
        return _failed(job_id, return_code, ERROR_RUNNER_OUTPUT_INVALID)

    if not processed:
        # Hiçbir host işlenmemişse taşınacak bir sonuç da yoktur: elde kalan
        # event'ler (örneğin task başlangıçları) bir çalıştırmanın kanıtı
        # değildir ve tam bir liste sanılmasınlar diye bırakılır.
        if return_code != 0:
            return _failed(job_id, return_code, ERROR_RUNNER_FAILED, events_truncated=True)
        return _failed(job_id, return_code, ERROR_RUNNER_NO_HOSTS)

    # `rc=0` tek başına başarı değildir: Ansible, `any_errors_fatal`/`ignore_errors`
    # ve `--check` kombinasyonlarında sıfır dönerken recap'te başarısız veya
    # erişilemeyen host bildirebilir.
    #
    # Aynı ölçüm iki soruyu birden cevaplar ve bu yüzden **bir kez** yapılır:
    # başarı kararını ve — başarısızlıkta — hangi sınıfa girdiğini. `rc` sınıfın
    # önkoşulu **değildir**: recap'teki failure/unreachable kanıtı `rc=0`'da da
    # `rc=2`'de de aynı şeyi söyler.
    host_failures = _has_host_failures(recap)
    successful = return_code == 0 and not host_failures
    if successful:
        error_code = None
    elif host_failures:
        error_code = ERROR_PLAYBOOK_FAILED
    else:
        # Buraya yalnız `rc != 0` ile gelinir ve recap temizdir: `rc` ile recap
        # ayrışmıştır. Ayrışmayı playbook lehine yorumlamak bir tahmin olurdu.
        error_code = ERROR_RUNNER_FAILED
    ansible_output, output_truncated = _display_output(documents)
    result = NormalizedRun(
        schema_version=SCHEMA_VERSION,
        job_id=job_id,
        return_code=return_code,
        outcome=OUTCOME_SUCCESSFUL if successful else OUTCOME_FAILED,
        error_code=error_code,
        recap=recap,
        events=events,
        events_truncated=False,
        result_truncated=False,
        ansible_output=ansible_output,
        ansible_output_truncated=output_truncated,
    )
    if _serialized_size(result) <= max_result_bytes:
        return result

    # Display output, geçerli bir recap/event sonucunu **düşürmemelidir**: ham
    # metin sonucun en büyük ve en az yapısal parçasıdır, structured özet ise
    # kullanıcının gerçekten karar verdiği yerdir. Bu yüzden bütçe aşımında önce
    # yalnız çıktı bırakılır ve bırakıldığı ``ansible_output_truncated`` ile
    # açıkça bildirilir; kullanıcı eksik olanın ne olduğunu bilir.
    if ansible_output is not None:
        without_output = replace(result, ansible_output=None, ansible_output_truncated=True)
        if _serialized_size(without_output) <= max_result_bytes:
            return without_output

    # Çıktısız sonuç da sığmıyorsa değişen bir şey yoktur: mevcut fail-closed
    # davranış aynen korunur.
    return _failed(job_id, return_code, ERROR_RESULT_LIMIT_EXCEEDED, result_truncated=True)


# --- Ayrıştırma --------------------------------------------------------------


class _InvalidOutputError(Exception):
    """Runner çıktısı beklenen yapıda değil."""


def _require_event_document(line: str) -> Mapping[str, Any]:
    """Tek bir stdout satırının JSON **object** olduğunu doğrular.

    Liste, sayı veya metin gibi başka bir JSON değeri kabul edilmez: satırın
    şekli doğrulanmadan alan okumak, beklenmeyen bir yapıyı sessizce boş bir
    event'e çevirirdi.
    """
    try:
        document = json.loads(line)
    except ValueError as exc:
        raise _InvalidOutputError from exc
    if not isinstance(document, dict):
        raise _InvalidOutputError
    if not isinstance(document.get("event"), str):
        raise _InvalidOutputError
    return document


def _terminal_stats_event(documents: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Güvenilir terminal event'i (``playbook_on_stats``) bulur.

    Terminal event **son** satır ve **tek** olmalıdır. "Son gördüğüm stats"
    yaklaşımı yeterli değildi: stats'tan sonra gelen bir satır akışın
    tamamlanmadığını, ikinci bir stats ise iki farklı çalıştırmanın aynı akışta
    birleştiğini gösterir. İkisinde de recap, gerçekte çalışandan başka bir şeyi
    özetlerdi.

    Returns:
        Terminal event; akışta hiç yoksa ``None``.

    Raises:
        _InvalidOutputError: Birden fazla terminal event varsa veya terminal
            event son non-empty satır değilse.
    """
    positions = [
        index for index, document in enumerate(documents) if document.get("event") == STATS_EVENT
    ]
    if not positions:
        return None
    if len(positions) > 1 or positions[0] != len(documents) - 1:
        raise _InvalidOutputError
    return documents[positions[0]]


def _require_processed_hosts(stats: Mapping[str, Any], known: set[str]) -> frozenset[str]:
    """Terminal event'in **kapsamını** (``processed``) yapısal olarak doğrular.

    Alanın varlığı değil, **şekli** ölçülür: sözlük olmalı, anahtarları bilinen
    host adları olmalı ve sayaçları negatif olmayan gerçek tam sayı olmalıdır.
    ``bool`` reddedilir; ``int``'in alt sınıfı olduğu için sessizce sayaç yerine
    geçerdi.

    Returns:
        İşlenmiş host adları. **Boş küme geçerli bir şekildir**; başarı kararı
        çağıranın işidir (bkz. :data:`ERROR_RUNNER_NO_HOSTS`).
    """
    event_data = stats.get("event_data")
    if not isinstance(event_data, dict):
        raise _InvalidOutputError

    raw = event_data.get(PROCESSED_FIELD)
    if not isinstance(raw, dict):
        raise _InvalidOutputError

    hosts: set[str] = set()
    for host, count in raw.items():
        if not isinstance(host, str) or host not in known:
            raise _InvalidOutputError
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise _InvalidOutputError
        hosts.add(host)
    return frozenset(hosts)


def _build_recap(
    stats: Mapping[str, Any], known: set[str], processed: frozenset[str]
) -> dict[str, HostRecap]:
    """Terminal event'ten **yalnız sayısal** ve **yalnız bilinen** recap kurar.

    Bilinmeyen bir host recap'e giremez ve sessizce atılmaz da: bilinmeyen bir
    ad, dondurulmuş inventory ile gerçekte çalıştırılan arasındaki bir ayrışmayı
    gösterir ve sonucun tamamını şüpheli kılar.

    Recap'in **kapsamı** ``processed``'dır: sayaçlarda görünen bir host orada da
    bulunmalıdır (bulunmuyorsa sayaçlar ile kapsam ayrışmıştır), ``processed``
    hostların hepsi ise sayaçları sıfır olsa bile recap'te temsil edilir. Aksi
    hâlde yalnız "hiçbir şey yapılmayan" hostlar sonuçtan düşer ve çalıştırma
    olduğundan dar görünürdü.
    """
    event_data = stats.get("event_data")
    if not isinstance(event_data, dict):
        raise _InvalidOutputError

    counters: dict[str, dict[str, int]] = {}
    for counter, source in RECAP_COUNTERS:
        raw = event_data.get(source, {})
        if not isinstance(raw, dict):
            raise _InvalidOutputError
        values: dict[str, int] = {}
        for host, count in raw.items():
            if not isinstance(host, str) or host not in known or host not in processed:
                raise _InvalidOutputError
            # `bool` `int`'in alt sınıfıdır; sayaç yerine geçmesine izin verilmez.
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise _InvalidOutputError
            values[host] = count
        counters[counter] = values

    return {
        host: HostRecap(
            **{counter: counters[counter].get(host, 0) for counter, _ in RECAP_COUNTERS}
        )
        for host in sorted(processed)
    }


def _has_host_failures(recap: Mapping[str, HostRecap]) -> bool:
    """Recap, başarısız veya erişilemeyen host bildiriyor mu."""
    return any(entry.failures > 0 or entry.unreachable > 0 for entry in recap.values())


def _safe_event(
    document: Mapping[str, Any], *, masks: Sequence[str], known: set[str]
) -> NormalizedEvent:
    """Bir event'ten yalnız izin verilen metaveriyi çıkarır.

    ``event_data`` **dolaşılmaz**: yalnız ``host`` ve ``task`` adları okunur.
    ``changed``/``failed`` gerçek boolean'lardan türetilir; ``res``'in kendisi
    hiçbir biçimde taşınmaz, bu yüzden ``no_log`` bir task'ın payload'ı sonuca
    giremez.
    """
    event = str(document["event"])
    event_data = document.get("event_data")
    if event_data is not None and not isinstance(event_data, dict):
        raise _InvalidOutputError
    data: Mapping[str, Any] = event_data or {}

    host = data.get("host")
    if host is not None:
        if not isinstance(host, str) or host not in known:
            raise _InvalidOutputError

    task = data.get("task")
    if task is not None and not isinstance(task, str):
        raise _InvalidOutputError

    result = data.get("res")
    changed = isinstance(result, dict) and result.get("changed") is True
    failed = event == "runner_on_failed" or (
        isinstance(result, dict) and result.get("failed") is True
    )

    return NormalizedEvent(
        event=event,
        host=_safe_text(host, masks) if host is not None else None,
        task=_safe_text(task, masks) if task is not None else None,
        changed=changed,
        failed=failed,
    )


# --- Display output ----------------------------------------------------------


def _display_output(documents: Sequence[Mapping[str, Any]]) -> tuple[str | None, bool]:
    """Event akışından bounded display metnini toplar.

    Kaynak **yalnız** her event object'inin üst düzey ``stdout`` alanıdır ve
    yalnız gerçek, boş olmayan bir ``str`` alınır: ``None``, sayı, liste veya
    boş metin sessizce bir satıra dönüşmez. Sıra korunur; satırlar
    :data:`DISPLAY_OUTPUT_SEPARATOR` ile birleştirilir ve metin başka hiçbir
    biçimde dönüştürülmez — burada redaction **yoktur** (bkz. modül docstring'i).

    Ölçüm :data:`MAX_ANSIBLE_OUTPUT_BYTES` üzerinden **UTF-8 byte** olarak
    yapılır ve bütçeyi aşan ilk parçada durur; ötesi hiç birleştirilmez. Karakter
    ortadan bölünmez (bkz. :func:`_utf8_prefix`).

    Returns:
        ``(metin, kırpıldı_mı)``. Uygun **hiçbir** satır yoksa ``(None, False)``:
        "çıktı yoktu" ile "çıktı vardı ama düşürüldü" ayrımı korunur.
    """
    chunks: list[str] = []
    budget = MAX_ANSIBLE_OUTPUT_BYTES
    found = False
    for document in documents:
        value = document.get(DISPLAY_OUTPUT_FIELD)
        if not isinstance(value, str) or not value:
            continue
        piece = value if not chunks else f"{DISPLAY_OUTPUT_SEPARATOR}{value}"
        try:
            size = len(piece.encode("utf-8"))
        except UnicodeEncodeError:
            # JSON, UTF-8'e hiç kodlanamayan yalnız-surrogate bir metin
            # taşıyabilir. Böyle bir satır byte olarak ölçülemez ve cevap
            # sınırında serileştirmeyi düşürürdü; taşınmaz ama akışın geri
            # kalanı da geçersiz sayılmaz — sonuç kararları değişmez.
            found = True
            return _joined(chunks, found=found, truncated=True)
        found = True
        if size <= budget:
            chunks.append(piece)
            budget -= size
            continue
        head = _utf8_prefix(piece, budget)
        if head:
            chunks.append(head)
        return _joined(chunks, found=found, truncated=True)
    return _joined(chunks, found=found, truncated=False)


def _joined(chunks: Sequence[str], *, found: bool, truncated: bool) -> tuple[str | None, bool]:
    """Toplanan parçaları tek metne çevirir; hiç satır yoksa ``None`` döner."""
    if not found:
        return None, False
    return "".join(chunks), truncated


def _utf8_prefix(text: str, limit: int) -> str:
    """Metnin, UTF-8 karşılığı ``limit`` byte'ı aşmayan en uzun ön ekini döner.

    Kesme **byte** üzerinde yapılır ama çok baytlı bir karakter ortadan
    bölünmez: ``errors="ignore"`` yarım kalan diziyi bütünüyle atar. Girdi
    geçerli bir ``str``'in kendi kodlaması olduğu için atılabilecek **tek** şey
    o yarım dizidir; başka bir kayıp olamaz.
    """
    if limit <= 0:
        return ""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _serialized_size(run: NormalizedRun) -> int:
    """Sonucun canonical JSON karşılığının byte boyutu."""
    return len(run.serialize().encode("utf-8"))


# --- Metin güvenliği ---------------------------------------------------------


def _ordered_masks(connection_values: Sequence[str]) -> tuple[str, ...]:
    """Maskelenecek bağlantı değerlerini **uzundan kısaya** sıralar.

    Sıra önemlidir: kısa bir değer önce maskelenirse uzun değerin içinden bir
    parça silinir ve geri kalan parça metinde açıkta kalırdı.
    """
    return tuple(sorted({value for value in connection_values if value}, key=len, reverse=True))


def _safe_text(text: str, masks: Sequence[str]) -> str:
    """Host/task metnini maskeleyip kırpar."""
    for mask in masks:
        text = text.replace(mask, REDACTED)
    text = redact_text(text)
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]
    return text


# --- Ortak -------------------------------------------------------------------


def _failed(
    job_id: str,
    return_code: int,
    error_code: str,
    *,
    events_truncated: bool = False,
    result_truncated: bool = False,
) -> NormalizedRun:
    """Kısmi hiçbir veri taşımayan fail-closed sonuç.

    Display output da taşınmaz. Timeout, geçersiz JSON, bozuk terminal event ve
    aşılmış sınır yollarında elde kalan ham metni "kurtarmak", akışın yapısal
    olarak güvenilmez olduğu bir yerde onun bir parçasını güvenilir gibi sunmak
    olurdu. ``ansible_output_truncated`` de ``False``'tur: zarf sabittir ve
    çalıştırmadan gelen hiçbir veriye — bir bayrağa bile — bağlı değildir; ne
    kırpıldığını zaten ``events_truncated``/``result_truncated`` söyler.
    """
    return NormalizedRun(
        schema_version=SCHEMA_VERSION,
        job_id=job_id,
        return_code=return_code,
        outcome=OUTCOME_FAILED,
        error_code=error_code,
        recap={},
        events=(),
        events_truncated=events_truncated,
        result_truncated=result_truncated,
        ansible_output=None,
        ansible_output_truncated=False,
    )
