"""Yayımlanmış sonuç belgesinin descriptor-relative ve sınırlı okunması (R1-V3D2A2B1).

Bu modül **yalnız okur**. Bir Job'ın ``app-data/jobs/<canonical-job-id>/result.json``
dosyasını açar, sınırlı biçimde byte'larını alır ve JSON'u decode edilmiş bir
Python nesnesine çevirir. Şema doğrulaması **burada yapılmaz**: belgenin
sözleşmeye uyup uymadığı :func:`~app.services.execution.result.parse_playbook_result`
tarafından ölçülür ve iki taraf B2'de birleştirilir. Bu turda modülün dışa
açılmış bir yolu da yoktur (:mod:`app.services.execution` bu modülü export etmez).

**Yol çağırandan alınmaz.** Fonksiyon ``artifact_path``, göreli bir yol veya
herhangi bir dosya adı kabul etmez; okunacak yer app-data kökü ile Job kimliğinden
**türetilir**. Job satırındaki ``artifact_dir``'i çağırandan alıp açmak, o sütunu
yazabilen her yolu bir dosya okuma ilkeline çevirirdi: bir Job kaydı bir gün
başka bir Job'ın, project ağacının veya ``/etc`` altındaki bir dosyanın yolunu
taşırsa okuyucu onu sorgusuz açardı. Kimlikten türetilen tek bir düzen, o soruyu
tümüyle ortadan kaldırır.

Güvenlik sözleşmesi:

- Bütün erişimler **descriptor-relative**'dir (``dir_fd`` + ``O_NOFOLLOW``).
  App-data kökü bile tek parça bir path metni olarak açılmaz: absolute yolun her
  bileşeni ``/`` descriptor'ından başlayarak tek tek, ``O_DIRECTORY | O_NOFOLLOW``
  ile açılır ve açılan descriptor ile isimdeki girdinin ``(st_dev, st_ino)``
  değeri karşılaştırılır. Path metnini çözüp sonra açmak kanıt sayılmaz: çözme
  ile açma arasındaki pencerede bileşenlerden biri symlink ile değiştirilebilir.
- ``Path.open``, :func:`open`, ``Path.read_bytes``, :mod:`shutil` ve :mod:`glob`
  **kullanılmaz**; hiçbiri ``dir_fd`` almaz ve hepsi ara symlink'leri izler.
- App-data, ``jobs`` ve Job dizini gerçek dizin ve **tam** ``0700`` olmalıdır;
  Job girdisi symlink, dosya, FIFO veya socket ise reddedilir. Yanlış izin
  ``chmod`` ile **düzeltilmez**: bu bir okuma yoludur, bir onarım yolu değildir
  ve okuyan tarafın izin genişletmesi, daraltmanın kendisini anlamsız kılardı.
- ``result.json`` ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK`` ile açılır; açıldıktan
  **sonra** ``fstat`` ile normal dosya olduğu, izninin tam ``0600`` olduğu ve
  ``st_nlink == 1`` olduğu doğrulanır. ``O_NONBLOCK``, yerine FIFO konmuş bir
  girdinin süreci yazar bekleyerek süresiz bloke etmesini engeller; ``st_nlink``
  kontrolü, dosyanın dizin dışından da yazılabilen bir hardlink olmasını eler.
- Bütün descriptor'lar başarı ve hata yollarında kapanır.

**Platform sınırı.** Güvenli primitive'ler yalnız POSIX'te vardır. Bulunmazlarsa
zayıf bir fallback ile devam **edilmez**; okuma fail-closed biçimde
:class:`~app.services.execution.result.JobResultUnavailableError` üretir
(:mod:`app.services.execution.workspace` ile aynı sınır).

**İki ayrı hata sınıfı.** Çağıranın parametreleri geçersizse (``app_data_dir``
relative, Job kimliği canonical değil, sınır aralık dışında) :class:`ValueError`
yükselir ve **dosya sistemine hiç dokunulmaz**. Dosya sistemi veya JSON kaynaklı
her ihlal ise tek bir ``JobResultUnavailableError``'a düşer. Ayrım
:func:`~app.services.execution.result.parse_playbook_result` ile aynıdır: yanlış
çağrılmış bir fonksiyon ile bozuk bir artifact aynı şey değildir.

**Tek ve sessiz hata.** Eksik dosya, yanlış izin, symlink, hardlink, FIFO, boyut
aşımı, bozuk UTF-8 ve geçersiz JSON — hepsi **aynı** koda, aynı mesaja ve aynı
``details``'e düşer. Mesaj parser'ınkiyle aynıdır çünkü ikisi de aynı sabit
hatayı **parametresiz** yükseltir: metin
:class:`~app.services.execution.result.JobResultUnavailableError`'ın kendi
constructor'ındadır ve buradan verilemez. İki katmanın farklı metin üretmesi,
çağırana "dosya mı bozuk, belge mi" sorusunu cevaplatır ve artifact'in durumu
hata cevabı üzerinden adım adım daraltılabilirdi. Aynı sebeple path, Job
kimliği, sınır değeri ve dosyanın ham içeriği hiçbir hata metnine,
``details``'e veya traceback'e girmez.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

from app.core.config import (
    PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING,
    PLAYBOOK_RUNNER_MIN_RESULT_BYTES,
)
from app.services.execution.result import JobResultUnavailableError

# Okunacak düzen. Adlar :class:`~app.services.jobs.artifacts.JobArtifactStore`'un
# yazdığı düzenle aynıdır ama ondan **import edilmez**: okuyucu, yazan modülün
# yeniden adlandırılan bir sabitini sessizce takip etmemelidir. Bugünkü eşitlik
# testle ayrıca ölçülür.
JOBS_DIRNAME: Final[str] = "jobs"
RESULT_FILENAME: Final[str] = "result.json"

# Dizinler yalnız sahibine açık (0700), sonuç dosyası yalnız sahibine okunur
# (0600) olmalıdır. Değerler **tam eşitlikle** ölçülür: "en fazla bu kadar açık"
# demek, `0700`'ün yanında `0500`'ü de kabul etmek olurdu ve deponun yazdığı
# gerçek izinden sapan bir dizin, artık deponun yazdığı dizin değildir.
DIRECTORY_MODE: Final[int] = 0o700
FILE_MODE: Final[int] = 0o600

# Okuma bir seferde bu kadar byte ister. Sabit ve küçüktür: ``os.read(fd, limit)``
# gibi tek büyük bir çağrı, sınırın kendisi kadar belleği sınır aşılmış olsa bile
# tek adımda ayırırdı.
READ_CHUNK_BYTES: Final[int] = 65_536

# Çağıranın verebileceği canonical byte bütçesinin aralığı. Aralık
# :mod:`app.core.config` ve :mod:`app.services.execution.result` ile aynıdır;
# ayrı yazılmış bir kopya, ayarların geçerli saydığı bir bütçenin okuma yolunda
# reddedilmesi demek olurdu.
MIN_ALLOWED_RESULT_BYTES: Final[int] = PLAYBOOK_RUNNER_MIN_RESULT_BYTES
MAX_ALLOWED_RESULT_BYTES: Final[int] = PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING

_DIRECTORY_FLAGS: Final[int] = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
_FILE_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

# Yol bileşeni olarak kabul edilmeyen ad. Küme bilinçli olarak yalnız ``..``
# taşır: ``PurePath`` yapıcıda ``.`` bileşenlerini ve tekrarlanan ayırıcıları
# **zaten** düşürür, yani ``Path("/x/./y").parts`` hiçbir zaman ``"."``
# içermez. Bu adları da listeye koymak, kodun hiç ulaşılamayan bir dalını
# "reddediyoruz" diye ilan etmek olurdu; verilemeyen bir güvence yazılmaz.
_UNSAFE_PARTS: Final[frozenset[str]] = frozenset({".."})

# ``U+FEFF``. Kaynakta escape ile yazılır: görünmez bir karakter olarak
# gömülseydi, onu silen ya da kopyalarken bozan bir düzenleme kontrolü sessizce
# etkisiz bırakırdı.
_BYTE_ORDER_MARK: Final[str] = "\ufeff"


class _UnreadableArtifact(Exception):
    """Artifact okunabilir değil.

    Yalnız modül içinde taşınan bir işarettir ve **hiçbir ayrıntı taşımaz**:
    mesajı, path'i, errno'su ve dosya içeriği yoktur. Bir metin taşısaydı, onu
    bir gün hata cevabına veya loga geçiren tek bir satır artifact'in durumunu
    dışarı çıkarırdı (:class:`~app.services.execution.result._RejectedDocument`
    ile aynı gerekçe).
    """


def _read_result_document(
    *,
    app_data_dir: Path,
    job_id: str,
    max_result_bytes: int,
) -> object:
    """Bir Job'ın yayımlanmış sonuç belgesini okur ve decode eder.

    Yalnız ``<app_data_dir>/jobs/<job_id>/result.json`` okunur. Fonksiyon hiçbir
    dosya, dizin veya izin **oluşturmaz ve değiştirmez**; şema doğrulaması
    yapmaz ve :func:`~app.services.execution.result.parse_playbook_result`
    çağırmaz.

    Args:
        app_data_dir: App-data kökü. Gerçek bir :class:`~pathlib.Path`,
            absolute ve ``..`` bileşeni taşımayan bir POSIX yolu olmalıdır.
        job_id: Canonical küçük harfli UUID4 Job kimliği. Gerçek bir ``str``
            olmalıdır; alt sınıfı kabul edilmez.
        max_result_bytes: Belgenin **canonical** compact JSON karşılığı için
            geçerli bütçe; :data:`MIN_ALLOWED_RESULT_BYTES` ile
            :data:`MAX_ALLOWED_RESULT_BYTES` arasında. Ham dosya için uygulanan
            tavan bundan türetilir (bkz. :func:`_raw_budget`).

    Returns:
        ``json.loads`` karşılığı, decode edilmiş bir Python nesnesi. Tip
        bilinçli olarak ``object``'tir: dosyadan gelen bir değeri ``dict`` ilan
        etmek, doğrulanmamış bir şeyi tip sistemi üzerinden doğrulanmış gibi
        göstermek olurdu.

    Raises:
        ValueError: **Çağıranın** parametreleri geçersizse. Bu yolda dosya
            sistemine hiç dokunulmaz.
        JobResultUnavailableError: Dosya sistemi veya JSON kaynaklı her ihlalde.
            Bütün ihlaller aynı kodu, mesajı ve ``details``'i üretir.
    """
    root = _require_app_data_dir(app_data_dir)
    name = _require_job_id(job_id)
    byte_limit = _require_result_byte_limit(max_result_bytes)

    try:
        raw = _read_raw_artifact(root, name, max_raw_bytes=_raw_budget(byte_limit))
        return _decode_document(raw)
    except _UnreadableArtifact:
        # `from None`: okuyucunun kendi zinciri (errno, path, decoder mesajı)
        # hata cevabına ve loglanan traceback'e taşınmaz.
        raise JobResultUnavailableError() from None


# --- Çağıran sözleşmesi -------------------------------------------------------


def _require_app_data_dir(value: Path) -> Path:
    """App-data kökünün gerçek, absolute ve ``..`` taşımayan bir yol olduğunu doğrular.

    Bağlayıcı sözleşme **üç** maddedir ve fazlası vaat edilmez:

    1. Değer gerçek bir :class:`~pathlib.Path`'tir.
    2. Yol absolute'tur. Relative bir kök, sürecin o anki çalışma dizinine bağlı
       olurdu: aynı çağrı farklı bir cwd altında başka bir ağacı okurdu.
    3. Hiçbir bileşen ``..`` değildir. ``..`` taşıyan bir kök aynı dizini başka
       bir yazımla adlandırır; descriptor zinciri onu güvenle yürüyebilse de
       kabul etmek, "kök tam olarak budur" sözünü tek bir yazım olmaktan
       çıkarır ve yapılandırmadan gelen bir alias'ı meşrulaştırırdı.

    Asıl güvenlik burada değil, **zincirdedir**: geriye kalan bütün bileşenler
    ``/`` descriptor'ından başlayarak ``O_NOFOLLOW`` ile tek tek yürünür
    (:func:`_open_app_data`). Lexical bir kontrol tek başına symlink'e karşı
    hiçbir şey söylemez.

    ``.`` bileşeni için bir **iddia yoktur**: ``PurePath`` onu yapıcıda düşürür,
    yani ``Path("/x/./y")`` bu fonksiyona zaten ``('/', 'x', 'y')`` olarak
    ulaşır. Reddedildiğini yazmak, hiç çalışmayan bir kontrolü güvence gibi
    sunmak olurdu.

    Değerin kendisi hata mesajına **yazılmaz**.
    """
    if not isinstance(value, Path):
        raise ValueError("App-data kökü bir Path olmalıdır.")
    parts = value.parts
    if not value.is_absolute() or not parts or parts[0] != "/":
        raise ValueError("App-data kökü absolute bir POSIX yolu olmalıdır.")
    if any(part in _UNSAFE_PARTS for part in parts[1:]):
        raise ValueError("App-data kökü `..` bileşeni taşıyamaz.")
    return value


def _require_job_id(value: str) -> str:
    """Job kimliğinin gerçek bir ``str`` ve canonical **küçük harfli** UUID4 olduğunu doğrular.

    Tür kontrolü ``isinstance`` değil ``type(...) is str``'dir. ``str`` alt
    sınıflanabilir ve alt sınıf ``__eq__``, ``__hash__`` veya ``__str__``
    davranışını değiştirebilir: canonical eşitlik testini geçen ama dizin adı
    olarak başka bir şeye çözülen bir değer, tam da bu fonksiyonun elemesi
    gereken şeydir. Kimlik burada bir metin değil, bir **dizin adıdır**;
    ``os`` çağrılarına giden şeyin düz ``str`` olduğu bilinmelidir.

    Canonical UUID4 biçimi, ayırıcı veya ``..`` taşıyan bir adın dizin adı
    olarak kullanılmasını tek başına imkânsız kılar. ``str(uuid.UUID(...))`` her
    zaman küçük harfli canonical biçimi üretir, bu yüzden eşitlik kontrolü büyük
    harfli, süslü parantezli veya tiresiz yazımı da eler. Değer hata mesajına
    **yazılmaz**.
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


def _require_result_byte_limit(value: int) -> int:
    """Bütçenin gerçek bir ``int`` ve geçerli aralıkta olduğunu doğrular.

    Kontrol ``type(...) is int``'tir ve bu, ``bool`` ile birlikte **bütün**
    ``int`` alt sınıflarını (``IntEnum``, ``IntFlag``, elle yazılmış sarmalayan
    tipler) tek bir kuralla eler. ``bool``'u ayrıca saymak yetmezdi: bir
    ``IntEnum`` üyesi de aritmetikte sessizce kendi sayısal değerine düşer ve
    bütçe, çağıranın hiç kastetmediği bir sayı olurdu. ``True``'nun "bir byte"
    anlamına gelmesi bu ailenin yalnız en görünür örneğidir.

    Taban :data:`MIN_ALLOWED_RESULT_BYTES`'tır; altındaki bir bütçe,
    normalizer'ın her koşulda yayımlanabilmesi gereken sabit fail-closed
    belgesini bile okunamaz yapardı.
    """
    if type(value) is not int:
        raise ValueError("Sonuç byte sınırı tam sayı olmalıdır.")
    if not MIN_ALLOWED_RESULT_BYTES <= value <= MAX_ALLOWED_RESULT_BYTES:
        raise ValueError("Sonuç byte sınırı geçerli aralıkta olmalıdır.")
    return value


def _raw_budget(max_result_bytes: int) -> int:
    """Ham dosya için okuma tavanı.

    Parser bütçeyi **canonical** compact JSON üzerinden ölçer
    (``separators=(",", ":")``), depo ise aynı belgeyi ``json.dumps``'ın
    varsayılan ayırıcılarıyla (``", "`` ve ``": "``) ve sonuna tek bir newline
    koyarak yazar. İki biçim aynı belge için farklı boyuttadır; ham dosyaya
    canonical sınırı doğrudan uygulamak, deponun kendi geçerli çıktısını
    reddederdi.

    İki kat zarf yeterlidir ve fazlasına gerek yoktur: varsayılan biçimin
    canonical'a eklediği her boşluğun karşılığında canonical metinde zaten bir
    ``:`` veya ``,`` vardır, yani eklenen boşluk sayısı canonical uzunluğu
    aşamaz. ``+1`` deponun sonuna koyduğu newline'dır.

    Bu tavan yalnız **okumayı** sınırlar; asıl canonical sınırı B2'de
    :func:`~app.services.execution.result.parse_playbook_result` decode edilmiş
    belge üzerinde yeniden uygular. Bu yüzden buradaki gevşeklik dışarı çıkmaz:
    zarfın içinde kalan ama canonical sınırı aşan bir belge orada reddedilir.
    """
    return max_result_bytes * 2 + 1


# --- Descriptor zinciri -------------------------------------------------------


def _read_raw_artifact(app_data_dir: Path, job_name: str, *, max_raw_bytes: int) -> bytes:
    """``app-data/jobs/<job>/result.json`` dosyasının ham byte'larını döndürür."""
    _require_posix_descriptors()
    with _open_app_data(app_data_dir) as app_fd:
        _require_private_directory(app_fd)
        with _open_child_directory(app_fd, JOBS_DIRNAME) as jobs_fd:
            _require_private_directory(jobs_fd)
            with _open_child_directory(jobs_fd, job_name) as job_fd:
                _require_private_directory(job_fd)
                return _read_result_file(job_fd, max_raw_bytes=max_raw_bytes)


def _require_posix_descriptors() -> None:
    """Güvenli primitive'ler yoksa fail-closed durur.

    ``O_NOFOLLOW``/``O_DIRECTORY`` bulunmayan bir platformda okuma, symlink
    izleyen zayıf bir yola düşürülmez: bu modülün bütün güvenlik iddiası o iki
    bayrağa dayanır.
    """
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise _UnreadableArtifact


@contextlib.contextmanager
def _open_app_data(app_data_dir: Path) -> Iterator[int]:
    """App-data kökünü ``/``'dan başlayan bir descriptor zinciriyle açar.

    Kök tek parça bir path metni olarak açılmaz. Absolute yolun her bileşeni,
    bir öncekinin descriptor'ına göre ``O_DIRECTORY | O_NOFOLLOW`` ile açılır ve
    açılan descriptor ile isimdeki girdinin ``(st_dev, st_ino)`` değeri
    karşılaştırılır: böylece **hiçbir** ara bileşen symlink olamaz ve zincirin
    ortasına sonradan konan bir symlink de yakalanır. ``os.open(app_data_dir)``
    tek çağrısı bunu yapamazdı — ``O_NOFOLLOW`` yalnız **son** bileşene bakar,
    ara bileşenler yine izlenirdi.

    Her adımda yalnız iki descriptor açık kalır; bir üst seviye, alt seviye
    doğrulandıktan hemen sonra kapatılır.
    """
    parts = app_data_dir.parts
    try:
        current = os.open(parts[0], _DIRECTORY_FLAGS)
    except OSError as exc:
        raise _UnreadableArtifact from exc
    try:
        for name in parts[1:]:
            try:
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=current)
            except OSError as exc:
                raise _UnreadableArtifact from exc
            try:
                _require_same_entry(child, current, name)
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        yield current
    finally:
        os.close(current)


@contextlib.contextmanager
def _open_child_directory(parent_fd: int, name: str) -> Iterator[int]:
    """Alt dizini descriptor-relative açar ve kimliğini doğrular.

    Symlink ``O_NOFOLLOW`` ile ``ELOOP``'a, normal dosya ve FIFO ise
    ``O_DIRECTORY`` ile ``ENOTDIR``'e düşer; hiçbiri izlenmez ve FIFO okuma
    için hiç açılmadığı için süreci bloke edemez.
    """
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _UnreadableArtifact from exc
    try:
        _require_same_entry(descriptor, parent_fd, name)
        yield descriptor
    finally:
        os.close(descriptor)


def _require_same_entry(child_fd: int, parent_fd: int, name: str) -> None:
    """Açılan descriptor ile isimdeki girdi hâlâ aynı nesne mi.

    Açma ile kullanma arasında yapılan bir değiş-tokuş ancak burada yakalanır:
    ``O_NOFOLLOW`` açma **anında** symlink'i eler, ama girdinin sonradan
    değiştirilmediğini söylemez.
    """
    try:
        opened = os.fstat(child_fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise _UnreadableArtifact from exc
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise _UnreadableArtifact


def _require_private_directory(dir_fd: int) -> None:
    """Descriptor gerçek bir dizin ve izni **tam** ``0700`` mü.

    Yanlış izin ``chmod`` ile düzeltilmez. Okuma yolunun izin genişletmesi ya da
    daraltması, deponun yazdığı iznin doğruluğunu ölçmek yerine onu sessizce
    yeniden yazmak olurdu; bir okuyucu, okuduğu ağacı değiştirmemelidir.
    """
    info = os.fstat(dir_fd)
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != DIRECTORY_MODE:
        raise _UnreadableArtifact


# --- Sınırlı okuma ------------------------------------------------------------


def _read_result_file(job_fd: int, *, max_raw_bytes: int) -> bytes:
    """``result.json``'ı açar, doğrular ve sınırlı biçimde okur.

    ``fstat`` iki kez çağrılır. İlki dosyayı sınıflandırır ve bildirilen boyutu
    tavana karşı **erken** eler; ikincisi okuma bittikten sonra dosyanın kimlik,
    boyut ve ``mtime`` olarak değişmediğini ölçer. Yalnız ilkine güvenmek
    yetmezdi: ``st_size`` okuma başlamadan önceki andır ve dosya okuma sırasında
    büyüyebilir. Yalnız ikincisine güvenmek de yetmezdi: tavan aşımı okuma
    **bitmeden** durdurulmalıdır.

    İsim bağı da **iki kez** doğrulanır ve ikisi ayrı sorulara cevap verir.
    İlki (açılışın hemen ardından) "açtığım descriptor, o an bu adı taşıyan
    girdi miydi" der. İkincisi (okuma bittikten sonra) "okuduğum içerik hâlâ bu
    adın içeriği mi" der. Bunlar aynı şey değildir: bir ``rename``, açık
    descriptor'ın gördüğü inode'a **hiç dokunmaz**. Dosya okuma boyunca
    byte-for-byte aynı kalır — ``st_dev``, ``st_ino``, ``st_size``,
    ``st_mtime_ns``, hepsi sabittir ve :func:`_require_unchanged` memnun kalır —
    ama ``result.json`` adı çoktan başka bir inode'a bağlanmıştır. Son kontrol
    olmasaydı okuyucu, artık yayımlanmış olmayan eski bir sonucu güncel sonuç
    diye döndürürdü; bir sürüm yükseltmesinin veya elle yapılmış bir düzeltmenin
    üzerine yazdığı belge, tam da bu pencerede eski hâliyle okunurdu.
    """
    try:
        descriptor = os.open(RESULT_FILENAME, _FILE_FLAGS, dir_fd=job_fd)
    except OSError as exc:
        raise _UnreadableArtifact from exc
    try:
        try:
            before = os.fstat(descriptor)
            _require_private_regular_file(before)
            _require_same_entry(descriptor, job_fd, RESULT_FILENAME)
            if before.st_size > max_raw_bytes:
                raise _UnreadableArtifact
            raw = _read_bounded(descriptor, max_raw_bytes=max_raw_bytes)
            _require_unchanged(before, os.fstat(descriptor), read_bytes=len(raw))
            _require_same_entry(descriptor, job_fd, RESULT_FILENAME)
        except OSError as exc:
            raise _UnreadableArtifact from exc
        return raw
    finally:
        os.close(descriptor)


def _require_private_regular_file(info: os.stat_result) -> None:
    """Açılmış girdi gerçek, tekil ve yalnız sahibine okunur bir dosya mı.

    Üç kontrolün üçü de ayrı bir saldırıyı eler:

    - ``S_ISREG``: dizin, FIFO, socket ve device girdileri reddedilir. Bunlar
      okunduğunda ya hata verir ya süresiz bloke eder ya da dosya sisteminin
      dışından veri getirirdi.
    - izin ``0600``: deponun yazdığı dosya budur. Daha açık bir dosya, başka bir
      kullanıcının yazabildiği ya da okuyabildiği bir dosyadır ve artık deponun
      yayımladığı belge sayılamaz.
    - ``st_nlink == 1``: dosyanın **tek** adı vardır. Bir hardlink, Job dizininin
      izinlerinden bağımsız olarak dizin dışından yazılabilen ikinci bir kapı
      açar; symlink'ten farklı olarak ``O_NOFOLLOW`` onu görmez.
    """
    if not stat.S_ISREG(info.st_mode):
        raise _UnreadableArtifact
    if stat.S_IMODE(info.st_mode) != FILE_MODE:
        raise _UnreadableArtifact
    if info.st_nlink != 1:
        raise _UnreadableArtifact


def _read_bounded(descriptor: int, *, max_raw_bytes: int) -> bytes:
    """Dosyayı sabit boyutlu parçalar hâlinde ve tavana kadar okur.

    Toplam, her parçadan sonra ölçülür: dosya ``fstat``'tan sonra büyüdüyse
    okuma, tavan aşılır aşılmaz durur ve geri kalanı hiç okunmaz. Tek bir
    ``os.read(descriptor, max_raw_bytes)`` çağrısı bunu yapamazdı — tavan kadar
    belleği, sınırın aşıldığı durumda bile tek adımda ayırırdı.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_raw_bytes:
            raise _UnreadableArtifact
        chunks.append(chunk)


def _require_unchanged(before: os.stat_result, after: os.stat_result, *, read_bytes: int) -> None:
    """Dosya okuma boyunca aynı mı kaldı.

    Okunan byte'ların tutarlı bir anlık görüntü olduğunu söyleyebilmek için
    dosyanın kimliği (``st_dev``/``st_ino``), ad sayısı, boyutu ve ``mtime``'ı
    okuma öncesi ve sonrası eşit olmalıdır. Okunan miktarın bildirilen boyuta
    eşitliği ayrıca ölçülür: kısa bir okuma, belgenin sessizce kırpılmış hâlini
    geçerli bir belge gibi gösterirdi.
    """
    if (before.st_dev, before.st_ino, before.st_nlink, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise _UnreadableArtifact
    if read_bytes != after.st_size:
        raise _UnreadableArtifact


# --- JSON decode --------------------------------------------------------------


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Aynı anahtarı iki kez taşıyan bir nesne reddedilir.

    ``json`` varsayılanı **son** değeri sessizce kazandırır. Aynı belge böylece
    iki farklı okuyucuya iki farklı şey söyleyebilir: doğrulayan taraf ilkini,
    başka bir araç sonuncusunu görebilir. Hook her nesting seviyesinde
    çağrıldığı için kontrol yalnız üst seviyeye değil, gömülü nesnelere de
    uygulanır.
    """
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise _UnreadableArtifact
        document[key] = value
    return document


def _reject_non_finite_constant(_literal: str) -> object:
    """``NaN``, ``Infinity`` ve ``-Infinity`` literal'leri reddedilir.

    Üçü de JSON'un kendisinde yoktur; Python'un decoder'ı onları bir uzantı
    olarak kabul eder. Sonuç belgesinde yerleri yoktur ve serileştirme sınırında
    geçerli JSON'a geri çevrilemezler.
    """
    raise _UnreadableArtifact


def _reject_non_finite_float(literal: str) -> float:
    """Taşarak sonsuza dönüşen sayı literal'leri de reddedilir.

    ``1e999`` geçerli JSON söz dizimidir ama ``float('inf')``'e çözülür ve
    :func:`_reject_non_finite_constant` onu görmez: literal bir sabit değil, bir
    sayıdır. İki yol da kapatılmazsa "sonsuz değer belgeye giremez" sözü yalnız
    yazımın bir biçimi için geçerli olurdu.
    """
    value = float(literal)
    if not math.isfinite(value):
        raise _UnreadableArtifact
    return value


# Decoder tek bir örnektir ama durum taşımaz: ``decode`` her çağrıda kendi
# tarayıcısını sıfırdan sürer.
_DECODER: Final[json.JSONDecoder] = json.JSONDecoder(
    object_pairs_hook=_reject_duplicate_keys,
    parse_constant=_reject_non_finite_constant,
    parse_float=_reject_non_finite_float,
)


def _decode_document(raw: bytes) -> object:
    """Ham byte'ları **tek** bir JSON belgesine çevirir.

    Kontroller sırayla: boş dosya, katı UTF-8, BOM ve tam olarak bir belge.
    ``JSONDecoder.decode`` baştaki boşluğu atlar, belgeyi okur ve sonrasında
    boşluk dışında bir şey kalırsa ``Extra data`` ile düşer; böylece deponun
    koyduğu newline kabul edilirken ikinci bir belge veya artık byte reddedilir.

    BOM ayrıca elenir. Decoder onu zaten geçersiz sayar, ama sebebi "beklenmeyen
    karakter" olurdu; burada niyet açıktır: UTF-8 belgesi BOM taşımaz ve bir
    BOM'u sessizce atlamak, aynı belgenin iki farklı byte dizisiyle yazılmasına
    izin vermek olurdu.

    Aşırı nesting ``RecursionError``, sınırı aşan sayı literal'i ``ValueError``
    üretir; ikisi de aynı tek hataya düşer.
    """
    if not raw:
        raise _UnreadableArtifact
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _UnreadableArtifact from exc
    if text.startswith(_BYTE_ORDER_MARK):
        raise _UnreadableArtifact
    try:
        return _DECODER.decode(text)
    except (ValueError, RecursionError) as exc:
        raise _UnreadableArtifact from exc
