"""R1-V3D2A2B1 sonuç yükleyicisinin yol, izin, sınır ve decode sözleşmesi.

Üç kural bu dosyanın tamamında geçerlidir:

1. **Reddetme testleri gerçekten bozmalıdır.** Bir senaryonun "symlink koydum"
   veya "izni bozdum" demesi yetmez; kurulumun beklenen hâli oluşturduğu
   (``lstat`` symlink diyor, mod gerçekten yanlış) ölçülür. Aksi hâlde yükleyici
   gevşediğinde bile geçen bir test kalırdı.
2. **Koruma testleri vacuous olamaz.** Dışarıdaki bir hedefin "değişmediğini"
   göstermek, hedefin gerçekten var olduğu ve okunabilir bir içerik taşıdığı
   önce doğrulanmazsa hiçbir şey kanıtlamaz.
3. **Tek hata cevabı.** Bütün dosya sistemi ve JSON ihlalleri aynı koda, aynı
   mesaja ve aynı ``details``'e düşer; üstelik bu üçlü parser'ın ürettiğiyle
   **aynıdır**. Ortak yardımcı :func:`_rejects` bunu her senaryoda ölçer.

Testler gerçek ``JobArtifactStore`` ile yazılmış bir sonucu da okur: yükleyicinin
kabul ettiği biçim, deponun gerçekte ürettiği biçimdir.
"""

from __future__ import annotations

import ast
import builtins
import errno
import inspect
import json
import os
import pathlib
import shutil
import socket
import stat
import uuid
from collections.abc import Callable, Iterator
from enum import IntEnum
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import (
    PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING,
    PLAYBOOK_RUNNER_MIN_RESULT_BYTES,
)
from app.services.execution import result_reader
from app.services.execution.result import (
    MIN_ALLOWED_RESULT_BYTES as PARSER_MIN_RESULT_BYTES,
)
from app.services.execution.result import (
    JobResultUnavailableError,
    parse_playbook_result,
)
from app.services.execution.result_reader import (
    DIRECTORY_MODE,
    FILE_MODE,
    JOBS_DIRNAME,
    MAX_ALLOWED_RESULT_BYTES,
    MIN_ALLOWED_RESULT_BYTES,
    READ_CHUNK_BYTES,
    RESULT_FILENAME,
    _read_result_document,
)
from app.services.jobs import artifacts as artifacts_module
from app.services.jobs.artifacts import JobArtifactStore

# Testlerin varsayılan bütçesi. Ham tavan bunun iki katı artı birdir (200_001),
# yani senaryolar birkaç kilobyte'lık belgelerle rahatça çalışır.
MAX_RESULT_BYTES = 100_000

DOCUMENT: dict[str, Any] = {
    "schema_version": 1,
    "outcome": "successful",
    "recap": {"web-1": {"ok": 1, "changed": 0}},
    "events": [{"event": "runner_on_ok", "host": "web-1"}],
}

# Dışarıdaki hedeflerin içeriği. Testler önce bu metnin gerçekten orada
# olduğunu, sonra okuma denemesinden sonra da orada kaldığını ölçer.
OUTSIDE_CONTENT = "SENTINEL-OUTSIDE-TARGET"

# "Aşırı nesting" senaryosunun derinliği. ``sys.getrecursionlimit()`` burada
# ölçüt **değildir**: CPython 3.12'de JSON'un C tarayıcısı Python çağrı
# yığınından ayrı ve çok daha yüksek bir sınır kullanır (5.000 seviye hâlâ
# kabul edilir). Değer o sınırın belirgin biçimde üzerindedir ama üreteceği
# belge (100.000 byte) ham bütçenin (200.001) altında kalır.
_NESTING_DEPTH = 50_000


# --- Ortak kurulum ------------------------------------------------------------


@pytest.fixture
def job_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def other_job_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def app_data(tmp_path: Path) -> Path:
    """0700 izinli, boş bir app-data kökü.

    İzin, ``ensure_app_data_dirs``'in production'da uyguladığı izindir; depo
    kendi ``jobs`` kökünü ve Job dizinini zaten 0700 yapar.
    """
    return _make_app_data(tmp_path)


def _make_app_data(parent: Path, name: str = "app-data") -> Path:
    root = parent / name
    root.mkdir(parents=True)
    os.chmod(root, DIRECTORY_MODE)
    return root


def publish(app_data: Path, job_id: str, document: dict[str, Any] | None = None) -> Path:
    """Gerçek depo ile bir sonuç yayımlar ve dosyanın yolunu döndürür."""
    store = JobArtifactStore(app_data)
    store.create(job_id)
    store.write_result(job_id, DOCUMENT if document is None else document)
    return app_data / JOBS_DIRNAME / job_id / RESULT_FILENAME


def write_raw(app_data: Path, job_id: str, payload: bytes) -> Path:
    """Ham byte'ları deponun düzeni ve izinleriyle yazar."""
    job_dir = app_data / JOBS_DIRNAME / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(app_data / JOBS_DIRNAME, DIRECTORY_MODE)
    os.chmod(job_dir, DIRECTORY_MODE)
    path = job_dir / RESULT_FILENAME
    path.write_bytes(payload)
    os.chmod(path, FILE_MODE)
    return path


def prepare_job_directory(app_data: Path, job_id: str) -> Path:
    """Sonuç dosyası olmayan, 0700 izinli bir Job dizini kurar."""
    job_dir = app_data / JOBS_DIRNAME / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(app_data / JOBS_DIRNAME, DIRECTORY_MODE)
    os.chmod(job_dir, DIRECTORY_MODE)
    return job_dir


def read(
    app_data: Path,
    job_id: str,
    *,
    max_result_bytes: int = MAX_RESULT_BYTES,
) -> object:
    return _read_result_document(
        app_data_dir=app_data,
        job_id=job_id,
        max_result_bytes=max_result_bytes,
    )


# Senaryoların bind ettiği socket'ler. Kapatılmasalardı hem descriptor sızıntısı
# testini kirletir hem de "okuma hiçbir descriptor sızdırmaz" iddiasını ölçülemez
# kılarlardı: sayım, testin kendi açtığı fd'yi yükleyiciye yazardı.
_BOUND_SOCKETS: list[socket.socket] = []


@pytest.fixture(autouse=True)
def _close_bound_sockets() -> Iterator[None]:
    """Senaryoların açtığı unix socket'leri test sonunda kapatır."""
    yield
    for endpoint in _BOUND_SOCKETS:
        endpoint.close()
    _BOUND_SOCKETS.clear()


def bind_socket(directory: Path, name: str) -> socket.socket:
    """Verilen dizinde bir unix socket girdisi oluşturur.

    ``sun_path`` 108 byte ile sınırlıdır ve ``tmp_path`` + Job kimliği bu sınıra
    çok yaklaşır; bu yüzden bind, dizine geçici olarak girilerek **göreli** adla
    yapılır. Aksi hâlde test, yükleyici yüzünden değil ``AF_UNIX`` sınırı
    yüzünden düşerdi.
    """
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _BOUND_SOCKETS.append(endpoint)
    previous = os.getcwd()
    os.chdir(directory)
    try:
        endpoint.bind(name)
    finally:
        os.chdir(previous)
    return endpoint


def canonical_size(document: object) -> int:
    return len(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    )


# --- Hata sözleşmesi ----------------------------------------------------------


def reference_error() -> JobResultUnavailableError:
    """Parser'ın **belge** ihlalinde ürettiği hata.

    Yükleyicinin hatası bununla birebir aynı olmalıdır: iki katmanın farklı metin
    üretmesi, çağırana "dosya mı bozuk, belge mi" sorusunu cevaplatır ve
    artifact'in durumu hata cevabı üzerinden daraltılabilirdi.
    """
    with pytest.raises(JobResultUnavailableError) as caught:
        parse_playbook_result(
            {"schema_version": 999},
            expected_job_id=str(uuid.uuid4()),
            max_events=1,
            max_result_bytes=PARSER_MIN_RESULT_BYTES,
        )
    return caught.value


def _rejects(
    app_data: Path,
    job_id: str,
    *,
    max_result_bytes: int = MAX_RESULT_BYTES,
) -> JobResultUnavailableError:
    """Okuma tek ve sabit hataya düşmeli."""
    reference = reference_error()
    with pytest.raises(JobResultUnavailableError) as caught:
        read(app_data, job_id, max_result_bytes=max_result_bytes)
    error = caught.value
    assert error.status_code == 503
    assert error.code == "job_result_unavailable"
    assert error.message == reference.message
    assert error.details == reference.details
    return error


# --- Gerçek depo ile uçtan uca ------------------------------------------------


def test_a_result_written_by_the_real_store_is_read_back(app_data: Path, job_id: str) -> None:
    """Deponun **gerçekte** yazdığı biçim kabul edilir.

    Depo ``json.dumps``'ın varsayılan ayırıcılarını kullanır ve sonuna newline
    koyar; parser ise bütçeyi canonical compact biçim üzerinden ölçer. İki biçim
    aynı belge için farklı boyuttadır ve test, ham dosyanın canonical'dan büyük
    olduğunu ama iki katlık zarfın içinde kaldığını ayrıca ölçer.
    """
    path = publish(app_data, job_id)
    raw = path.read_bytes()

    # Vacuous değil: dosya gerçekten boşluklu ve newline ile bitiyor.
    assert b'"schema_version": 1' in raw
    assert raw.endswith(b"\n")
    assert len(raw) > canonical_size(DOCUMENT)

    document = read(app_data, job_id, max_result_bytes=MIN_ALLOWED_RESULT_BYTES)

    assert document == json.loads(raw)
    assert document == DOCUMENT
    assert len(raw) <= MIN_ALLOWED_RESULT_BYTES * 2 + 1


def test_the_reader_layout_matches_the_artifact_store(app_data: Path, job_id: str) -> None:
    """Okunan düzen, yazılan düzendir.

    Sabitler depodan **import edilmez**; bugünkü eşitlikleri burada ölçülür.
    Ayrılırlarsa bu test düşer ve okuyucu sessizce boş bir dizine bakmaz.
    """
    assert RESULT_FILENAME == artifacts_module.RESULT_FILENAME

    relative = JobArtifactStore(app_data).create(job_id)

    assert relative == f"{JOBS_DIRNAME}/{job_id}"
    assert (app_data / relative).is_dir()


def test_the_reader_accepts_the_permissions_the_store_writes(app_data: Path, job_id: str) -> None:
    """Beklenen izinler gerçekten deponun yazdığı izinlerdir."""
    path = publish(app_data, job_id)

    assert stat.S_IMODE(app_data.stat().st_mode) == DIRECTORY_MODE
    assert stat.S_IMODE((app_data / JOBS_DIRNAME).stat().st_mode) == DIRECTORY_MODE
    assert stat.S_IMODE(path.parent.stat().st_mode) == DIRECTORY_MODE
    assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE
    assert path.stat().st_nlink == 1

    assert read(app_data, job_id) == DOCUMENT


@pytest.mark.parametrize(
    "payload",
    [b"[1, 2]", b'"metin"', b"42", b"null", b"true", b'{"bilinmeyen": "alan"}'],
    ids=["list", "string", "number", "null", "bool", "unknown_field"],
)
def test_the_loader_does_not_validate_the_schema(
    app_data: Path, job_id: str, payload: bytes
) -> None:
    """Yükleyici şema doğrulamaz; ``json.loads`` karşılığını döndürür.

    Şema kontrolü B2'de parser'a aittir. Burada bir sonuç belgesine hiç
    benzemeyen belgeler de kabul edilmelidir: iki sorumluluğu tek katmanda
    birleştirmek, "yükleyici neyi reddetti" sorusunu ölçülemez kılardı.
    """
    write_raw(app_data, job_id, payload)

    assert read(app_data, job_id) == json.loads(payload)


# --- Çağıran sözleşmesi -------------------------------------------------------


def test_a_relative_app_data_root_is_a_caller_error(job_id: str) -> None:
    """Relative kök, sürecin o anki çalışma dizinine bağlı olurdu."""
    with pytest.raises(ValueError, match="absolute"):
        read(Path("app-data"), job_id)


def test_a_dot_dot_alias_of_the_app_data_root_is_a_caller_error(
    tmp_path: Path, job_id: str
) -> None:
    """``..`` ile yazılmış bir alias, doğru dizini gösterse bile reddedilir.

    Bağlayıcı sözleşme yalnız üç şey vaat eder: gerçek ``Path``, absolute yol ve
    ``..`` taşımayan bileşenler. ``.`` için bir iddia **yoktur** — bkz.
    :func:`test_a_single_dot_component_never_reaches_the_reader`.
    """
    app_data = _make_app_data(tmp_path)
    publish(app_data, job_id)
    alias = tmp_path / "yan" / ".." / "app-data"

    # Vacuous değil: alias gerçekten aynı dizini adlandırıyor.
    (tmp_path / "yan").mkdir()
    assert ".." in alias.parts
    assert alias.resolve() == app_data.resolve()

    with pytest.raises(ValueError, match=r"\.\."):
        read(alias, job_id)


def test_a_single_dot_component_never_reaches_the_reader(app_data: Path, job_id: str) -> None:
    """``.`` bileşeni bir güvence değil, ``pathlib``'in normalizasyonudur.

    ``PurePath`` ``.`` bileşenlerini **yapıcıda** düşürür: fonksiyona ulaşan yol
    zaten onları taşımaz, dolayısıyla "reddediyoruz" demek hiç çalışmayan bir
    kontrolü güvence gibi sunmak olurdu. Test bunu tersinden ölçer — ``./``
    yazımıyla kurulmuş bir kök **kabul edilir**, çünkü aslında normalize edilmiş
    ve aynı yoldur.
    """
    publish(app_data, job_id)
    dotted = Path(str(app_data.parent) + "/./" + app_data.name)

    # Vacuous değil: yazım gerçekten `.` içeriyor ama `parts` içermiyor.
    assert "/./" in str(app_data.parent) + "/./" + app_data.name
    assert "." not in dotted.parts
    assert dotted.parts == app_data.parts

    assert read(dotted, job_id) == DOCUMENT


@pytest.mark.parametrize(
    "value",
    ["/tmp/app-data", b"/tmp/app-data", None, 42],
    ids=["str", "bytes", "none", "int"],
)
def test_an_app_data_root_that_is_not_a_path_is_a_caller_error(value: Any, job_id: str) -> None:
    """``Path`` olmayan bir kök kabul edilmez."""
    with pytest.raises(ValueError, match="Path"):
        read(value, job_id)


@pytest.mark.parametrize(
    "value",
    [
        "3F2B7C1A-9D4E-4A6B-8C1D-5E7F9A0B2C3D",
        "3f2b7c1a9d4e4a6b8c1d5e7f9a0b2c3d",
        "{3f2b7c1a-9d4e-4a6b-8c1d-5e7f9a0b2c3d}",
        "3f2b7c1a-9d4e-1a6b-8c1d-5e7f9a0b2c3d",
        "../../etc",
        "jobs/3f2b7c1a-9d4e-4a6b-8c1d-5e7f9a0b2c3d",
        "",
        "..",
        None,
        42,
    ],
    ids=[
        "uppercase",
        "no_dashes",
        "braced",
        "version_1",
        "traversal",
        "with_separator",
        "empty",
        "dot_dot",
        "none",
        "int",
    ],
)
def test_a_non_canonical_job_id_is_a_caller_error(app_data: Path, value: Any) -> None:
    """Job kimliği aynı zamanda bir dizin adıdır.

    Canonical UUID4 biçimi, ayırıcı veya ``..`` taşıyan bir adın dizin adı olarak
    kullanılmasını tek başına imkânsız kılar.
    """
    with pytest.raises(ValueError, match="UUID4"):
        read(app_data, value)


class _JobId(str):
    """Canonical bir UUID4 metnini taşıyan ``str`` alt sınıfı."""


class _ByteLimit(int):
    """Geçerli bir bütçeyi taşıyan ``int`` alt sınıfı."""


class _Budget(IntEnum):
    """``int`` alt sınıfının en yaygın hâli."""

    DEFAULT = MAX_RESULT_BYTES


def test_a_job_id_subclass_is_a_caller_error(app_data: Path, job_id: str) -> None:
    """``str`` alt sınıfı reddedilir.

    Alt sınıf ``__eq__``, ``__hash__`` veya ``__str__`` davranışını
    değiştirebilir: canonical eşitlik testini geçen ama dizin adı olarak başka
    bir şeye çözülen bir değer, tam da bu kontrolün elemesi gereken şeydir.
    Kimlik burada bir metin değil, ``os`` çağrılarına giden bir **dizin adıdır**.
    """
    publish(app_data, job_id)
    subclassed = _JobId(job_id)

    # Vacuous değil: değer canonical UUID4 testinin kendisini geçiyor ve düz
    # `str` hâli gerçekten okunabiliyor.
    assert subclassed == job_id
    assert str(uuid.UUID(subclassed)) == subclassed
    assert read(app_data, job_id) == DOCUMENT

    with pytest.raises(ValueError, match="UUID4"):
        read(app_data, subclassed)


@pytest.mark.parametrize(
    "factory",
    [lambda: _ByteLimit(MAX_RESULT_BYTES), lambda: _Budget.DEFAULT],
    ids=["int_subclass", "int_enum"],
)
def test_a_byte_limit_subclass_is_a_caller_error(
    app_data: Path, job_id: str, factory: Callable[[], int]
) -> None:
    """``int`` alt sınıfları reddedilir.

    ``bool``'u ayrıca saymak yetmezdi: bir ``IntEnum`` üyesi de aritmetikte
    sessizce kendi sayısal değerine düşer ve bütçe, çağıranın hiç kastetmediği
    bir sayı olurdu. ``type(...) is int`` bütün aileyi tek kuralla eler.
    """
    publish(app_data, job_id)
    value = factory()

    # Vacuous değil: değer sayısal olarak geçerli aralıkta ve düz `int` hâli
    # gerçekten kabul ediliyor.
    assert MIN_ALLOWED_RESULT_BYTES <= value <= MAX_ALLOWED_RESULT_BYTES
    assert read(app_data, job_id, max_result_bytes=int(value)) == DOCUMENT

    with pytest.raises(ValueError, match="byte"):
        read(app_data, job_id, max_result_bytes=value)


def test_subclass_caller_errors_never_touch_the_filesystem(
    app_data: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alt sınıf reddi de dosya sistemine dokunulmadan **önce** olur."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Çağıran hatası yolunda dosya sistemine dokunulmamalıdır.")

    monkeypatch.setattr(os, "open", boom)
    monkeypatch.setattr(os, "stat", boom)

    with pytest.raises(ValueError):
        read(app_data, _JobId(job_id))
    with pytest.raises(ValueError):
        read(app_data, job_id, max_result_bytes=_ByteLimit(MAX_RESULT_BYTES))
    with pytest.raises(ValueError):
        read(app_data, job_id, max_result_bytes=_Budget.DEFAULT)


@pytest.mark.parametrize(
    "value",
    [True, False, 1.0, "1024", None, MIN_ALLOWED_RESULT_BYTES - 1, MAX_ALLOWED_RESULT_BYTES + 1, 0],
    ids=["true", "false", "float", "str", "none", "below_floor", "above_ceiling", "zero"],
)
def test_an_invalid_byte_limit_is_a_caller_error(app_data: Path, job_id: str, value: Any) -> None:
    """``bool`` de reddedilir: ``True`` sessizce "bir byte" anlamına gelirdi."""
    with pytest.raises(ValueError, match="byte"):
        read(app_data, job_id, max_result_bytes=value)


def test_the_reader_shares_the_configured_limit_range() -> None:
    """Aralık ayarların ve parser'ın aralığıyla aynıdır."""
    assert MIN_ALLOWED_RESULT_BYTES == PLAYBOOK_RUNNER_MIN_RESULT_BYTES
    assert MAX_ALLOWED_RESULT_BYTES == PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING
    assert MIN_ALLOWED_RESULT_BYTES == PARSER_MIN_RESULT_BYTES


def test_caller_errors_never_touch_the_filesystem(
    app_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Çağıran sözleşmesi, dosya sistemine dokunulmadan **önce** ölçülür."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Çağıran hatası yolunda dosya sistemine dokunulmamalıdır.")

    monkeypatch.setattr(os, "open", boom)
    monkeypatch.setattr(os, "stat", boom)
    monkeypatch.setattr(os, "listdir", boom)
    monkeypatch.setattr(builtins, "open", boom)

    with pytest.raises(ValueError):
        read(app_data, "GECERSIZ")
    with pytest.raises(ValueError):
        read(Path("relative"), str(uuid.uuid4()))
    with pytest.raises(ValueError):
        read(app_data, str(uuid.uuid4()), max_result_bytes=True)


def test_a_caller_error_is_not_the_unavailable_error(app_data: Path) -> None:
    """Yanlış çağrılmış bir fonksiyon ile bozuk bir artifact aynı şey değildir."""
    with pytest.raises(ValueError) as caught:
        read(app_data, "GECERSIZ")

    assert not isinstance(caught.value, JobResultUnavailableError)


# --- Bozuk senaryolar ---------------------------------------------------------
#
# Her senaryo, kullanılacak app-data kökünü döndürür: bazıları kökün kendisini
# symlink veya alias yapar.

Scenario = Callable[[Path, str], Path]


def _scenario_missing_app_data(tmp_path: Path, job_id: str) -> Path:
    return tmp_path / "app-data"


def _scenario_missing_jobs(tmp_path: Path, job_id: str) -> Path:
    return _make_app_data(tmp_path)


def _scenario_missing_job_directory(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    jobs = app_data / JOBS_DIRNAME
    jobs.mkdir()
    os.chmod(jobs, DIRECTORY_MODE)
    return app_data


def _scenario_missing_result(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    prepare_job_directory(app_data, job_id)
    return app_data


def _scenario_app_data_mode(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    publish(app_data, job_id)
    os.chmod(app_data, 0o755)
    assert stat.S_IMODE(app_data.stat().st_mode) == 0o755
    return app_data


def _scenario_jobs_mode(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    publish(app_data, job_id)
    os.chmod(app_data / JOBS_DIRNAME, 0o750)
    return app_data


def _scenario_job_mode(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    path = publish(app_data, job_id)
    os.chmod(path.parent, 0o777)
    return app_data


def _scenario_result_mode(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    path = publish(app_data, job_id)
    os.chmod(path, 0o644)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    return app_data


def _scenario_app_data_symlink(tmp_path: Path, job_id: str) -> Path:
    real = _make_app_data(tmp_path, "gercek-app-data")
    publish(real, job_id)
    alias = tmp_path / "app-data"
    alias.symlink_to(real, target_is_directory=True)
    assert alias.is_symlink()
    return alias


def _scenario_parent_component_symlink(tmp_path: Path, job_id: str) -> Path:
    real = tmp_path / "gercek"
    app_data = _make_app_data(real)
    publish(app_data, job_id)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert alias.is_symlink()
    return alias / "app-data"


def _scenario_jobs_symlink(tmp_path: Path, job_id: str) -> Path:
    outside = _make_app_data(tmp_path, "disarisi")
    publish(outside, job_id)
    app_data = _make_app_data(tmp_path)
    (app_data / JOBS_DIRNAME).symlink_to(outside / JOBS_DIRNAME, target_is_directory=True)
    return app_data


def _scenario_job_symlink(tmp_path: Path, job_id: str) -> Path:
    outside = _make_app_data(tmp_path, "disarisi")
    publish(outside, job_id)
    app_data = _make_app_data(tmp_path)
    jobs = app_data / JOBS_DIRNAME
    jobs.mkdir()
    os.chmod(jobs, DIRECTORY_MODE)
    (jobs / job_id).symlink_to(outside / JOBS_DIRNAME / job_id, target_is_directory=True)
    return app_data


def _scenario_result_symlink(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    job_dir = prepare_job_directory(app_data, job_id)
    outside = tmp_path / "disarisi.json"
    outside.write_text(OUTSIDE_CONTENT, encoding="utf-8")
    (job_dir / RESULT_FILENAME).symlink_to(outside)
    return app_data


def _scenario_job_entry_is_file(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    jobs = app_data / JOBS_DIRNAME
    jobs.mkdir()
    os.chmod(jobs, DIRECTORY_MODE)
    (jobs / job_id).write_bytes(b"{}")
    return app_data


def _scenario_job_entry_is_fifo(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    jobs = app_data / JOBS_DIRNAME
    jobs.mkdir()
    os.chmod(jobs, DIRECTORY_MODE)
    os.mkfifo(jobs / job_id, DIRECTORY_MODE)
    return app_data


def _scenario_job_entry_is_socket(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    jobs = app_data / JOBS_DIRNAME
    jobs.mkdir()
    os.chmod(jobs, DIRECTORY_MODE)
    bind_socket(jobs, job_id)
    return app_data


def _scenario_result_is_directory(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    job_dir = prepare_job_directory(app_data, job_id)
    (job_dir / RESULT_FILENAME).mkdir()
    os.chmod(job_dir / RESULT_FILENAME, FILE_MODE)
    return app_data


def _scenario_result_is_fifo(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    job_dir = prepare_job_directory(app_data, job_id)
    os.mkfifo(job_dir / RESULT_FILENAME, FILE_MODE)
    return app_data


def _scenario_result_is_socket(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    job_dir = prepare_job_directory(app_data, job_id)
    bind_socket(job_dir, RESULT_FILENAME)
    os.chmod(job_dir / RESULT_FILENAME, FILE_MODE)
    return app_data


def _scenario_result_hardlink(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    path = publish(app_data, job_id)
    outside = tmp_path / "hardlink.json"
    os.link(path, outside)
    assert path.stat().st_nlink == 2
    return app_data


def _scenario_too_large(tmp_path: Path, job_id: str) -> Path:
    app_data = _make_app_data(tmp_path)
    payload = b'{"pad": "' + b"a" * (MAX_RESULT_BYTES * 2 + 1) + b'"}\n'
    write_raw(app_data, job_id, payload)
    return app_data


def _scenario_deep_nesting(tmp_path: Path, job_id: str) -> Path:
    """Dengeli ama aşırı derin bir belge.

    Derinlik, CPython'un JSON tarayıcısının kendi özyineleme sınırının belirgin
    biçimde üzerindedir (``sys.getrecursionlimit()`` ile aynı **değildir**: C
    tarayıcısının sınırı 3.12'de ondan çok daha yüksektir). Belge sözdizimsel
    olarak geçerlidir ve ham bütçenin **içinde** kalır: reddin sebebi boyut
    değil, derinliktir.
    """
    app_data = _make_app_data(tmp_path)
    payload = b"[" * _NESTING_DEPTH + b"]" * _NESTING_DEPTH
    assert len(payload) <= MAX_RESULT_BYTES * 2 + 1
    write_raw(app_data, job_id, payload)
    return app_data


def _raw_scenario(payload: bytes) -> Scenario:
    def scenario(tmp_path: Path, job_id: str) -> Path:
        app_data = _make_app_data(tmp_path)
        write_raw(app_data, job_id, payload)
        return app_data

    return scenario


BROKEN_SCENARIOS: dict[str, Scenario] = {
    "missing_app_data": _scenario_missing_app_data,
    "missing_jobs": _scenario_missing_jobs,
    "missing_job_directory": _scenario_missing_job_directory,
    "missing_result": _scenario_missing_result,
    "app_data_mode": _scenario_app_data_mode,
    "jobs_mode": _scenario_jobs_mode,
    "job_mode": _scenario_job_mode,
    "result_mode": _scenario_result_mode,
    "app_data_symlink": _scenario_app_data_symlink,
    "parent_component_symlink": _scenario_parent_component_symlink,
    "jobs_symlink": _scenario_jobs_symlink,
    "job_symlink": _scenario_job_symlink,
    "result_symlink": _scenario_result_symlink,
    "job_entry_is_file": _scenario_job_entry_is_file,
    "job_entry_is_fifo": _scenario_job_entry_is_fifo,
    "job_entry_is_socket": _scenario_job_entry_is_socket,
    "result_is_directory": _scenario_result_is_directory,
    "result_is_fifo": _scenario_result_is_fifo,
    "result_is_socket": _scenario_result_is_socket,
    "result_hardlink": _scenario_result_hardlink,
    "too_large": _scenario_too_large,
    "deep_nesting": _scenario_deep_nesting,
    "empty": _raw_scenario(b""),
    "whitespace_only": _raw_scenario(b"   \n"),
    "invalid_utf8": _raw_scenario(b'{"a": "\xff\xfe"}'),
    "bom": _raw_scenario(b'\xef\xbb\xbf{"a": 1}'),
    "utf16": _raw_scenario('{"a": 1}'.encode("utf-16")),
    "truncated": _raw_scenario(b'{"a": '),
    "trailing_document": _raw_scenario(b'{"a": 1}\n{"b": 2}\n'),
    "trailing_garbage": _raw_scenario(b'{"a": 1}garbage'),
    "duplicate_top_level_key": _raw_scenario(b'{"a": 1, "a": 2}\n'),
    "duplicate_nested_key": _raw_scenario(b'{"x": {"y": [{"a": 1, "a": 2}]}}\n'),
    "nan": _raw_scenario(b'{"a": NaN}\n'),
    "infinity": _raw_scenario(b'{"a": Infinity}\n'),
    "negative_infinity": _raw_scenario(b'{"a": -Infinity}\n'),
    "overflow_float": _raw_scenario(b'{"a": 1e999}\n'),
    "not_json": _raw_scenario(b"merhaba\n"),
}


@pytest.mark.parametrize("name", sorted(BROKEN_SCENARIOS), ids=sorted(BROKEN_SCENARIOS))
def test_every_broken_artifact_produces_the_same_error(
    tmp_path: Path, job_id: str, name: str
) -> None:
    """Bütün arızalar tek bir cevaba düşer.

    Eksik dosya, yanlış izin, symlink, hardlink, FIFO, boyut aşımı ve geçersiz
    JSON birbirinden ayırt edilemez. Ayrım yapmak, artifact'in durumunu hata
    cevabı üzerinden adım adım daraltmayı mümkün kılardı.
    """
    app_data = BROKEN_SCENARIOS[name](tmp_path, job_id)

    _rejects(app_data, job_id)


@pytest.mark.parametrize("name", sorted(BROKEN_SCENARIOS), ids=sorted(BROKEN_SCENARIOS))
def test_no_failure_leaks_the_path_job_id_or_content(
    tmp_path: Path, job_id: str, name: str
) -> None:
    """Hata metni ne yolu, ne kimliği, ne de ham içeriği taşır."""
    app_data = BROKEN_SCENARIOS[name](tmp_path, job_id)

    error = _rejects(app_data, job_id)

    surfaces = (error.message, repr(error.details), repr(error), str(error))
    for leaked in (
        job_id,
        str(app_data),
        str(tmp_path),
        RESULT_FILENAME,
        JOBS_DIRNAME,
        OUTSIDE_CONTENT,
        "SENTINEL",
        "schema_version",
        str(MAX_RESULT_BYTES),
        "_UnreadableArtifact",
        "Errno",
    ):
        for surface in surfaces:
            assert leaked not in surface, (name, leaked)


# --- Dış hedeflerin korunması -------------------------------------------------


def test_a_result_symlink_does_not_read_or_change_its_target(
    app_data: Path, job_id: str, tmp_path: Path
) -> None:
    """Symlink izlenmez; hedefi ne okunur ne değiştirilir."""
    job_dir = prepare_job_directory(app_data, job_id)
    outside = tmp_path / "disarisi.json"
    outside.write_text(OUTSIDE_CONTENT, encoding="utf-8")
    before = outside.stat()
    (job_dir / RESULT_FILENAME).symlink_to(outside)

    # Vacuous değil: symlink gerçekten kuruldu ve hedefi okunabilir.
    assert (job_dir / RESULT_FILENAME).is_symlink()
    assert outside.read_text(encoding="utf-8") == OUTSIDE_CONTENT

    _rejects(app_data, job_id)

    assert outside.read_text(encoding="utf-8") == OUTSIDE_CONTENT
    assert outside.stat().st_mtime_ns == before.st_mtime_ns
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(before.st_mode)


def test_a_jobs_symlink_does_not_reach_the_tree_it_points_at(tmp_path: Path, job_id: str) -> None:
    """``jobs`` symlink olduğunda hedefteki gerçek sonuç bile okunmaz."""
    outside = _make_app_data(tmp_path, "disarisi")
    target = publish(outside, job_id)
    app_data = _make_app_data(tmp_path)
    (app_data / JOBS_DIRNAME).symlink_to(outside / JOBS_DIRNAME, target_is_directory=True)

    # Vacuous değil: hedefte gerçekten okunabilir bir sonuç var.
    assert json.loads(target.read_bytes()) == DOCUMENT

    _rejects(app_data, job_id)

    assert json.loads(target.read_bytes()) == DOCUMENT


def test_a_hardlinked_result_is_rejected_without_touching_the_other_name(
    app_data: Path, job_id: str, tmp_path: Path
) -> None:
    """İkinci bir ad, dizin izinlerinden bağımsız bir yazma kapısıdır.

    ``O_NOFOLLOW`` hardlink'i görmez; onu eleyen tek şey ``st_nlink == 1``
    kontrolüdür.
    """
    path = publish(app_data, job_id)
    outside = tmp_path / "hardlink.json"
    os.link(path, outside)

    # Vacuous değil: gerçekten iki ad var ve içerik okunabilir.
    assert path.stat().st_nlink == 2
    assert json.loads(outside.read_bytes()) == DOCUMENT

    _rejects(app_data, job_id)

    assert json.loads(outside.read_bytes()) == DOCUMENT
    assert outside.stat().st_nlink == 2


def test_a_neighbour_job_is_neither_opened_nor_changed(
    app_data: Path, job_id: str, other_job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Komşu Job'ın dizini hiç açılmaz ve dosyası değişmez."""
    publish(app_data, job_id, {"okunan": True})
    neighbour = publish(app_data, other_job_id, {"komsu": True})
    before = neighbour.stat()

    opened: list[str] = []
    real_open = os.open

    def recording_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if isinstance(path, str):
            opened.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    document = read(app_data, job_id)
    monkeypatch.undo()

    assert document == {"okunan": True}
    # Vacuous değil: okunan Job'ın adı gerçekten kayda girdi.
    assert job_id in opened
    assert other_job_id not in opened

    assert json.loads(neighbour.read_bytes()) == {"komsu": True}
    assert neighbour.stat().st_mtime_ns == before.st_mtime_ns
    assert stat.S_IMODE(neighbour.stat().st_mode) == FILE_MODE


# --- Kimlik doğrulaması (dev/ino) ---------------------------------------------


class _PatchedStat:
    """Gerçek bir ``stat_result``'ın tek alanı değiştirilmiş görünümü."""

    def __init__(self, real: os.stat_result, **overrides: int) -> None:
        self._real = real
        for name, value in overrides.items():
            setattr(self, name, value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.mark.parametrize("target", [JOBS_DIRNAME, RESULT_FILENAME], ids=["jobs", "result"])
def test_an_entry_swapped_between_open_and_use_is_rejected(
    app_data: Path, job_id: str, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``O_NOFOLLOW`` açma anını korur; değiş-tokuşu ``(dev, ino)`` yakalar."""
    publish(app_data, job_id)
    assert read(app_data, job_id) == DOCUMENT

    real_stat = os.stat

    def swapping_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        info = real_stat(path, *args, **kwargs)
        if path == target:
            return _PatchedStat(info, st_ino=info.st_ino + 1)
        return info

    monkeypatch.setattr(os, "stat", swapping_stat)
    try:
        _rejects(app_data, job_id)
    finally:
        monkeypatch.undo()


def test_a_result_reported_as_a_device_is_rejected(
    app_data: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Device girdisi reddedilir.

    Gerçek bir device node ``mknod`` gerektirir ve ayrıcalıksız bir testte
    kurulamaz; bu yüzden ``fstat``'ın bildirdiği tür enjekte edilir. Ölçülen şey
    zaten ``S_ISREG`` kapısıdır.
    """
    publish(app_data, job_id)
    real_fstat = os.fstat

    def device_fstat(descriptor: int) -> Any:
        info = real_fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            return _PatchedStat(info, st_mode=stat.S_IFCHR | FILE_MODE)
        return info

    monkeypatch.setattr(os, "fstat", device_fstat)
    try:
        _rejects(app_data, job_id)
    finally:
        monkeypatch.undo()


# --- Ham bütçe ----------------------------------------------------------------


def _padded_payload(size: int) -> bytes:
    """Tam olarak ``size`` byte uzunluğunda geçerli bir JSON belgesi."""
    payload = b'{"pad": "' + b"a" * (size - 12) + b'"}\n'
    assert len(payload) == size
    return payload


def test_the_raw_budget_is_two_envelopes_plus_the_newline(app_data: Path, job_id: str) -> None:
    """Tavan tam olarak ``max_result_bytes * 2 + 1``'dir.

    Sınır sabitten türetilir, elle yazılmaz: taban şema sürümüyle birlikte artar
    (R1-V3J3A'da 256 → 320) ve sabit bir sayı, ayarların artık kabul etmediği bir
    bütçeyi test edilir sanardı.
    """
    limit = MIN_ALLOWED_RESULT_BYTES
    payload = _padded_payload(limit * 2 + 1)
    path = write_raw(app_data, job_id, payload)

    assert path.stat().st_size == limit * 2 + 1

    assert read(app_data, job_id, max_result_bytes=limit) == json.loads(payload)


def test_one_byte_past_the_raw_budget_is_rejected(app_data: Path, job_id: str) -> None:
    """Sınırın bir fazlası reddedilir."""
    limit = MIN_ALLOWED_RESULT_BYTES
    payload = _padded_payload(limit * 2 + 2)
    path = write_raw(app_data, job_id, payload)

    assert path.stat().st_size == limit * 2 + 2

    _rejects(app_data, job_id, max_result_bytes=limit)


def test_a_file_that_grows_after_its_fstat_stops_at_the_ceiling(
    app_data: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fstat`` küçük gösterse bile okuma tavanı aşınca durur.

    ``st_size`` okuma başlamadan önceki andır; yalnız ona güvenmek, dosya okuma
    sırasında büyüdüğünde tavanı etkisiz bırakırdı. Test, bildirilen boyutu
    küçülterek erken elemeyi devre dışı bırakır ve okuma **döngüsünün** durup
    durmadığını ölçer.
    """
    limit = MIN_ALLOWED_RESULT_BYTES
    real_size = 400_000
    path = write_raw(app_data, job_id, _padded_payload(real_size))
    assert path.stat().st_size == real_size

    real_fstat = os.fstat

    def shrinking_fstat(descriptor: int) -> Any:
        info = real_fstat(descriptor)
        if stat.S_ISREG(info.st_mode) and info.st_size == real_size:
            return _PatchedStat(info, st_size=10)
        return info

    requested: list[int] = []
    real_read = os.read

    def counting_read(descriptor: int, size: int) -> bytes:
        requested.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "fstat", shrinking_fstat)
    monkeypatch.setattr(os, "read", counting_read)
    with pytest.raises(JobResultUnavailableError):
        read(app_data, job_id, max_result_bytes=limit)
    monkeypatch.undo()

    # Okuma tek bir parçadan sonra durdu: dosyanın tamamı hiç okunmadı.
    assert requested == [READ_CHUNK_BYTES]
    assert sum(requested) < real_size


def test_the_read_never_asks_for_the_whole_ceiling_at_once(
    app_data: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Okuma sabit boyutlu parçalarla yapılır, tek dev bir çağrıyla değil."""
    payload = _padded_payload(READ_CHUNK_BYTES * 2 + 500)
    write_raw(app_data, job_id, payload)

    requested: list[int] = []
    real_read = os.read

    def counting_read(descriptor: int, size: int) -> bytes:
        requested.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", counting_read)
    document = read(app_data, job_id)
    monkeypatch.undo()

    assert document == json.loads(payload)
    assert requested
    assert set(requested) == {READ_CHUNK_BYTES}
    assert READ_CHUNK_BYTES < MAX_ALLOWED_RESULT_BYTES


def test_a_file_whose_size_changes_during_the_read_is_rejected(
    app_data: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EOF'tan sonra boyut değiştiyse okunan byte'lar tutarlı bir görüntü değildir."""
    path = write_raw(app_data, job_id, b'{"a": 1}\n')
    real_read = os.read

    def growing_read(descriptor: int, size: int) -> bytes:
        chunk = real_read(descriptor, size)
        if not chunk:
            with path.open("ab") as handle:
                handle.write(b"x")
        return chunk

    monkeypatch.setattr(os, "read", growing_read)
    try:
        _rejects(app_data, job_id)
    finally:
        monkeypatch.undo()


def test_a_file_whose_mtime_changes_during_the_read_is_rejected(
    app_data: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boyut aynı kalsa bile ``mtime`` değişmişse dosya yeniden yazılmıştır."""
    path = write_raw(app_data, job_id, b'{"a": 1}\n')
    before = path.stat().st_mtime_ns
    real_read = os.read

    def retouching_read(descriptor: int, size: int) -> bytes:
        chunk = real_read(descriptor, size)
        os.utime(path, ns=(0, 0))
        return chunk

    monkeypatch.setattr(os, "read", retouching_read)
    try:
        _rejects(app_data, job_id)
    finally:
        monkeypatch.undo()

    # Vacuous değil: mtime gerçekten değişti.
    assert path.stat().st_mtime_ns != before


# --- İsim bağı (final name binding) -------------------------------------------


# Aynı uzunlukta, farklı içerikli iki belge. Uzunluk eşitliği bilinçlidir: rename
# sonrası `st_size` da değişmezse, reddin sebebinin boyut kontrolü olamayacağı
# ölçülebilir hâle gelir.
_OLD_PAYLOAD = b'{"kim": "eski-inode"}\n'
_NEW_PAYLOAD = b'{"kim": "yeni-inode"}\n'
assert len(_OLD_PAYLOAD) == len(_NEW_PAYLOAD)


def _swapping_reader(monkeypatch: pytest.MonkeyPatch, job_dir: Path) -> dict[str, os.stat_result]:
    """EOF anında ``result.json`` adını gerçek ``rename`` ile başka bir inode'a bağlar.

    Değiş-tokuş, ilk isim-bağı kontrolünden **sonra** ve son kontrolden **önce**
    olur. Açık descriptor eski inode'u tutmaya devam eder; ``rename`` ona hiç
    dokunmaz. Fonksiyon, takasın hemen öncesindeki ve sonrasındaki ``fstat``
    değerlerini döndürür ki testler ``_require_unchanged``'in memnun kaldığını
    ölçebilsin.
    """
    observed: dict[str, os.stat_result] = {}
    real_read = os.read
    done = False

    def swapping_read(descriptor: int, size: int) -> bytes:
        nonlocal done
        chunk = real_read(descriptor, size)
        if not chunk and not done:
            done = True
            observed["before"] = os.fstat(descriptor)
            os.rename(job_dir / RESULT_FILENAME, job_dir / "eski.json")
            os.rename(job_dir / "halef.json", job_dir / RESULT_FILENAME)
            observed["after"] = os.fstat(descriptor)
        return chunk

    monkeypatch.setattr(os, "read", swapping_read)
    return observed


def _prepare_rename_swap(app_data: Path, job_id: str) -> Path:
    """Eski sonucu ve onun yerine geçecek eşdeğer dosyayı hazırlar."""
    path = write_raw(app_data, job_id, _OLD_PAYLOAD)
    successor = path.parent / "halef.json"
    successor.write_bytes(_NEW_PAYLOAD)
    os.chmod(successor, FILE_MODE)

    # Vacuous değil: halef gerçekten aynı izin, aynı boyut ve tek adlı.
    assert successor.stat().st_size == path.stat().st_size
    assert stat.S_IMODE(successor.stat().st_mode) == FILE_MODE
    assert successor.stat().st_nlink == 1
    assert successor.stat().st_ino != path.stat().st_ino
    return path


def test_a_name_rebound_to_another_inode_during_the_read_is_rejected(
    app_data: Path, job_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Okuma bittiğinde ad hâlâ okunan inode'a mı bağlı.

    ``rename`` açık descriptor'ın gördüğü inode'a **hiç dokunmaz**: dosya okuma
    boyunca byte-for-byte aynı kalır ve ``_require_unchanged`` memnun kalır. Ama
    ``result.json`` adı çoktan başka bir inode'a bağlanmıştır ve okunan içerik
    artık yayımlanmış sonuç değildir. Bunu yakalayan tek şey, okuma sonrasındaki
    ikinci isim-bağı kontrolüdür.
    """
    path = _prepare_rename_swap(app_data, job_id)
    outside = tmp_path / "disarisi.json"
    outside.write_text(OUTSIDE_CONTENT, encoding="utf-8")

    observed = _swapping_reader(monkeypatch, path.parent)
    try:
        _rejects(app_data, job_id)
    finally:
        monkeypatch.undo()

    # Reddin sebebi boyut/mtime/inode değişimi **olamaz**: descriptor'ın gördüğü
    # dosya takas boyunca birebir aynı kaldı.
    before, after = observed["before"], observed["after"]
    assert (before.st_dev, before.st_ino, before.st_nlink) == (
        after.st_dev,
        after.st_ino,
        after.st_nlink,
    )
    assert before.st_size == after.st_size
    assert before.st_mtime_ns == after.st_mtime_ns

    # Ne yeni dosya ne de dışarıdaki hedef değişti.
    assert (path.parent / RESULT_FILENAME).read_bytes() == _NEW_PAYLOAD
    assert (path.parent / "eski.json").read_bytes() == _OLD_PAYLOAD
    assert outside.read_text(encoding="utf-8") == OUTSIDE_CONTENT


def test_without_the_final_name_binding_check_the_stale_inode_would_be_returned(
    app_data: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kırmızı/yeşil kanıtı: kontrol kaldırılınca aynı senaryo **kabul** edilir.

    Son isim-bağı kontrolü devre dışı bırakılır (yalnız ``result.json`` için ve
    yalnız **ikinci** çağrıda; açılış anındaki ilk kontrol olduğu gibi kalır).
    Aynı takas bu kez sessizce geçer ve okuyucu, adı artık başka bir inode'a
    bağlı olan **eski** belgeyi döndürür. Testin kendisi, düzeltmenin gerçekten
    gerekli olduğunun ölçüsüdür: fix olmadan yol yeşildir.
    """
    path = _prepare_rename_swap(app_data, job_id)

    real_require_same_entry = result_reader._require_same_entry
    seen = {"count": 0}

    def skipping_same_entry(child_fd: int, parent_fd: int, name: str) -> None:
        if name == RESULT_FILENAME:
            seen["count"] += 1
            if seen["count"] == 2:
                return
        real_require_same_entry(child_fd, parent_fd, name)

    monkeypatch.setattr(result_reader, "_require_same_entry", skipping_same_entry)
    _swapping_reader(monkeypatch, path.parent)
    try:
        stale = read(app_data, job_id)
    finally:
        monkeypatch.undo()

    # Kontrol atlandı ve eski inode'un içeriği döndü.
    assert seen["count"] == 2
    assert stale == json.loads(_OLD_PAYLOAD)
    assert stale != json.loads(_NEW_PAYLOAD)
    assert (path.parent / RESULT_FILENAME).read_bytes() == _NEW_PAYLOAD


def test_the_result_name_binding_is_verified_before_and_after_the_read(
    app_data: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sağlıklı okumada isim bağı tam **iki** kez doğrulanır.

    Bir kere doğrulamak, ``rename`` penceresini açık bırakırdı; ikisi ayrı
    sorulara cevap verir ve ikisi de gereklidir.
    """
    publish(app_data, job_id)

    names: list[str] = []
    real_require_same_entry = result_reader._require_same_entry

    def counting_same_entry(child_fd: int, parent_fd: int, name: str) -> None:
        names.append(name)
        real_require_same_entry(child_fd, parent_fd, name)

    monkeypatch.setattr(result_reader, "_require_same_entry", counting_same_entry)
    try:
        document = read(app_data, job_id)
    finally:
        monkeypatch.undo()

    assert document == DOCUMENT
    assert names.count(RESULT_FILENAME) == 2
    assert names.count(JOBS_DIRNAME) == 1
    assert names.count(job_id) == 1


# --- Bloke olmama -------------------------------------------------------------


def test_the_result_is_opened_without_blocking_on_a_fifo(app_data: Path, job_id: str) -> None:
    """Yazarı olmayan bir FIFO süreci bloke etmez.

    ``O_NONBLOCK`` olmasaydı ``open`` bir yazar gelene kadar **süresiz** beklerdi
    ve bu test hiç bitmezdi; ölçüm bu yüzden bir timeout'a değil, çağrının
    dönmesine dayanır. Bayrağın gerçekten istendiği ayrıca ölçülür.
    """
    job_dir = prepare_job_directory(app_data, job_id)
    fifo = job_dir / RESULT_FILENAME
    os.mkfifo(fifo, FILE_MODE)

    # Vacuous değil: girdi gerçekten bir FIFO ve yazarı yok.
    assert stat.S_ISFIFO(fifo.lstat().st_mode)

    _rejects(app_data, job_id)


def test_every_open_uses_nofollow_and_nonblock(
    app_data: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Her ``os.open`` çağrısı symlink izlemez ve bloke olmaz."""
    publish(app_data, job_id)

    flags: list[int] = []
    real_open = os.open

    def recording_open(path: Any, flag: int, *args: Any, **kwargs: Any) -> int:
        flags.append(flag)
        return real_open(path, flag, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    read(app_data, job_id)
    monkeypatch.undo()

    assert flags
    for flag in flags:
        assert flag & os.O_NOFOLLOW
        assert flag & os.O_NONBLOCK
        assert (flag & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)) == 0


def test_a_platform_without_safe_primitives_fails_closed(
    app_data: Path, job_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX primitive'leri yoksa zayıf bir yola düşülmez."""
    publish(app_data, job_id)
    monkeypatch.setattr(os, "name", "nt")
    try:
        _rejects(app_data, job_id)
    finally:
        monkeypatch.undo()


# --- Yan etkisizlik -----------------------------------------------------------


MUTATING_CALLS = (
    (os, "chmod"),
    (os, "fchmod"),
    (os, "chown"),
    (os, "mkdir"),
    (os, "makedirs"),
    (os, "unlink"),
    (os, "remove"),
    (os, "rmdir"),
    (os, "rename"),
    (os, "replace"),
    (os, "link"),
    (os, "symlink"),
    (os, "write"),
    (os, "truncate"),
    (os, "ftruncate"),
    (os, "utime"),
    (shutil, "copy"),
    (shutil, "copytree"),
    (shutil, "rmtree"),
    (shutil, "move"),
    (pathlib.Path, "mkdir"),
    (pathlib.Path, "write_text"),
    (pathlib.Path, "write_bytes"),
    (pathlib.Path, "unlink"),
    (pathlib.Path, "chmod"),
)


@pytest.mark.parametrize(
    "name", ["healthy", *sorted(BROKEN_SCENARIOS)], ids=["healthy", *sorted(BROKEN_SCENARIOS)]
)
def test_the_reader_never_writes_creates_or_chmods(
    tmp_path: Path, job_id: str, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Okuma yolu hiçbir şey oluşturmaz, silmez, yeniden adlandırmaz ve chmod'lamaz.

    Sağlıklı ve bozuk yolların ikisi de ölçülür: onarım eğilimi en çok bozuk
    yolda ortaya çıkar ve yanlış bir izni "düzelten" bir okuyucu, daraltmanın
    kendisini anlamsız kılardı.
    """
    if name == "healthy":
        app_data = _make_app_data(tmp_path)
        publish(app_data, job_id)
    else:
        app_data = BROKEN_SCENARIOS[name](tmp_path, job_id)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Okuma yolu dosya sistemini değiştirmemelidir.")

    for owner, attribute in MUTATING_CALLS:
        monkeypatch.setattr(owner, attribute, boom)
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(pathlib.Path, "open", boom)

    try:
        if name == "healthy":
            document: object = read(app_data, job_id)
        else:
            document = None
            with pytest.raises(JobResultUnavailableError):
                read(app_data, job_id)
    finally:
        monkeypatch.undo()

    if name == "healthy":
        assert document == DOCUMENT


@pytest.mark.parametrize(
    "name", ["healthy", *sorted(BROKEN_SCENARIOS)], ids=["healthy", *sorted(BROKEN_SCENARIOS)]
)
def test_no_descriptor_is_leaked_on_any_path(tmp_path: Path, job_id: str, name: str) -> None:
    """Açılan her descriptor başarı ve hata yollarında kapanır.

    Sızan bir descriptor uzun ömürlü bir süreçte birikir ve sonunda ``EMFILE``
    ile **başka** bir işlemi düşürürdü; ayrıca silinmiş bir dosyayı canlı
    tutarak diski de tutar.
    """
    if name == "healthy":
        app_data = _make_app_data(tmp_path)
        publish(app_data, job_id)
    else:
        app_data = BROKEN_SCENARIOS[name](tmp_path, job_id)

    before = _open_descriptors()
    for _ in range(5):
        try:
            read(app_data, job_id)
        except JobResultUnavailableError:
            pass
    after = _open_descriptors()

    assert after == before


def _open_descriptors() -> set[str]:
    """Sürecin açık descriptor kümesi."""
    proc = Path("/proc/self/fd")
    if not proc.is_dir():  # pragma: no cover - Linux dışı
        pytest.skip("Descriptor sayımı /proc gerektirir.")
    return set(os.listdir(proc))


def test_a_missing_result_does_not_create_the_job_directory(app_data: Path, job_id: str) -> None:
    """Okuma, olmayan bir düzeni **kurmaz**."""
    _rejects(app_data, job_id)

    assert not (app_data / JOBS_DIRNAME).exists()
    assert list(app_data.iterdir()) == []


def test_a_wrong_permission_is_rejected_not_repaired(app_data: Path, job_id: str) -> None:
    """Yanlış izin olduğu gibi bırakılır."""
    path = publish(app_data, job_id)
    os.chmod(path, 0o644)
    os.chmod(path.parent, 0o755)

    _rejects(app_data, job_id)

    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755


def test_reading_twice_returns_equal_but_independent_documents(app_data: Path, job_id: str) -> None:
    """Okuma idempotenttir ve çağrılar birbirinin nesnesini paylaşmaz."""
    publish(app_data, job_id)
    first = read(app_data, job_id)
    second = read(app_data, job_id)

    assert first == second
    assert first is not second


# --- Kapsam kilidi ------------------------------------------------------------


def _module_tree() -> ast.Module:
    return ast.parse(inspect.getsource(result_reader))


def _imported_modules() -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    return imported


def _bound_names() -> set[str]:
    return {
        alias.asname or alias.name
        for node in ast.walk(_module_tree())
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }


def _attribute_calls() -> set[tuple[str, str]]:
    """Kaynaktaki ``<ad>.<öznitelik>(...)`` çağrılarının tamamı."""
    return {
        (node.func.value.id, node.func.attr)
        for node in ast.walk(_module_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    }


def test_the_reader_imports_no_database_route_or_writer_layer() -> None:
    """Modülün **gerçek** import listesi bir sözleşmedir ve tam eşitlikle ölçülür.

    Docstring'de geçen bir modül adı testi ne geçirir ne düşürür; ölçülen AST'in
    kendisidir. Veritabanı, route, runner ve artifact **yazma** katmanı buraya
    giremez: bu bir okuma yoludur ve birine bağlanması, okumanın sessizce yan
    etki üretebileceği anlamına gelirdi.
    """
    assert _imported_modules() == {
        "__future__",
        "contextlib",
        "json",
        "math",
        "os",
        "stat",
        "uuid",
        "collections.abc",
        "pathlib",
        "typing",
        "app.core.config",
        "app.services.execution.result",
    }
    for forbidden in (
        "shutil",
        "glob",
        "subprocess",
        "sqlite3",
        "sqlalchemy",
        "sqlalchemy.orm",
        "ansible_runner",
        "fastapi",
        "app.models",
        "app.db.session",
        "app.schemas.job",
        "app.services.jobs.artifacts",
        "app.services.execution.executor",
        "app.services.execution.read",
        "app.services.execution.normalize",
        "app.services.execution.runner_process",
        "app.services.execution.store",
        "app.services.execution.worker",
        "app.services.execution.workspace",
    ):
        assert forbidden not in _imported_modules(), forbidden


def test_the_reader_does_not_import_or_call_the_parser() -> None:
    """Doğrulama B2'de bağlanacaktır; bu turda çağrılmaz.

    Yasak, bir kullanımın yokluğundan değil ismin hiç içeri alınmamasından
    gelir: hiç bağlanmamış bir ad çağrılamaz.
    """
    called = {
        node.func.id
        for node in ast.walk(_module_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    for forbidden in ("parse_playbook_result", "JobArtifactStore", "normalize_runner_output"):
        assert forbidden not in _bound_names(), forbidden
        assert forbidden not in called, forbidden
        assert not hasattr(result_reader, forbidden), forbidden


def test_the_reader_uses_no_path_walking_or_symlink_following_helper() -> None:
    """``Path.open``, :func:`open`, ``read_*``, ``shutil`` ve ``glob`` kullanılmaz.

    Hiçbiri ``dir_fd`` almaz; hepsi ara bileşenlerdeki symlink'leri izler ve
    modülün bütün güvenlik iddiasını geçersiz kılardı.
    """
    calls = _attribute_calls()
    forbidden_attributes = {
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "resolve",
        "glob",
        "rglob",
        "iglob",
        "iterdir",
        "walk",
        "expanduser",
    }
    for _owner, attribute in calls:
        assert attribute not in forbidden_attributes, attribute

    # ``open`` yalnız ``os.open`` olarak çağrılabilir.
    assert {owner for owner, attribute in calls if attribute == "open"} == {"os"}
    bare_calls = {
        node.func.id
        for node in ast.walk(_module_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in bare_calls


def test_the_reader_calls_no_mutating_os_function() -> None:
    """Kaynak kilidi: yazan, oluşturan ve izin değiştiren hiçbir çağrı yok."""
    used = {attribute for owner, attribute in _attribute_calls() if owner == "os"}

    assert used <= {"open", "close", "read", "fstat", "stat"}


def test_every_open_flag_constant_carries_nofollow() -> None:
    """``os.open`` yalnız iki kilitli bayrak sabitiyle çağrılır.

    Bayraklar çağrı yerinde tek tek yazılsaydı, bir gün eklenen üçüncü bir
    çağrıda ``O_NOFOLLOW``'un unutulması sessiz kalırdı. AST, kullanılan sabitin
    hangisi olduğunu; runtime kontrolü de o sabitin ne içerdiğini ölçer.
    """
    flag_arguments = {
        node.args[1].id
        for node in ast.walk(_module_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "open"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Name)
    }

    assert flag_arguments == {"_DIRECTORY_FLAGS", "_FILE_FLAGS"}
    for name in flag_arguments:
        flags = getattr(result_reader, name)
        assert flags & os.O_NOFOLLOW, name
        assert flags & os.O_NONBLOCK, name
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) == 0, name
    assert result_reader._DIRECTORY_FLAGS & os.O_DIRECTORY


def test_the_loader_is_not_exported_from_the_execution_package() -> None:
    """Bu turda public bir yol yoktur.

    Yükleyici B2'de parser'a bağlanacaktır; şimdiden export etmek, ölçülmemiş
    bir okuma yüzeyi açardı.
    """
    package_source = Path("app/services/execution/__init__.py").read_text(encoding="utf-8")

    assert "result_reader" not in package_source
    assert "_read_result_document" not in package_source

    from app.services import execution

    assert "_read_result_document" not in execution.__all__
    assert not hasattr(execution, "_read_result_document")


def test_the_public_names_of_the_reader_stay_private() -> None:
    """Modülün tek giriş noktası private'tır."""
    functions = {
        name
        for name, value in vars(result_reader).items()
        if callable(value) and getattr(value, "__module__", None) == result_reader.__name__
    }

    assert "_read_result_document" in functions
    assert not [name for name in functions if not name.startswith("_")]


def test_the_openapi_surface_matches_the_bound_job_routes(client: TestClient) -> None:
    """Kapsam kilidi: bu okuyucunun kendisi hiçbir operasyon eklemez.

    R1-V3D2B ile Job liste/detay/sonuç GET'leri bağlandı; bu private okuyucu
    hâlâ hiçbir route'a doğrudan bağlı değildir, yalnız ``result_service``
    üzerinden kullanılır.

    Toplam operasyon sayısı 21'dir ve bu sayının **hiçbir** parçası buradan
    gelmez: 21. operasyon R1-V3J1'in eklediği
    ``GET /api/inventories/{inventory_id}/ping-runs``'dır.
    """
    spec = client.get("/openapi.json").json()

    job_paths = {path for path in spec["paths"] if path.startswith("/api/jobs")}
    assert job_paths == {"/api/jobs", "/api/jobs/{job_id}", "/api/jobs/{job_id}/result"}
    assert sum(len(operations) for operations in spec["paths"].values()) == 21


def test_the_source_carries_no_test_only_escape_hatch() -> None:
    """Kaynakta ortam değişkeni veya bayrakla gevşetilebilen bir yol yok."""
    source = inspect.getsource(result_reader)

    for forbidden in ("getenv", "environ", "follow_symlinks=True", "O_CREAT", "O_WRONLY"):
        assert forbidden not in source, forbidden


# --- Yardımcıların kendisi ----------------------------------------------------


def test_the_error_of_the_loader_matches_the_error_of_the_parser(
    app_data: Path, job_id: str
) -> None:
    """İki katman ayırt edilemez.

    Yükleyicinin "dosya bozuk" hatası ile parser'ın "belge bozuk" hatası aynı
    kodu, aynı mesajı ve aynı ``details``'i taşır; çağıran hangisinin
    tetiklendiğini ölçemez.
    """
    write_raw(app_data, job_id, b"bozuk")
    parser_error = reference_error()

    with pytest.raises(JobResultUnavailableError) as caught:
        read(app_data, job_id)
    loader_error = caught.value

    assert type(loader_error) is type(parser_error)
    assert loader_error.message == parser_error.message
    assert loader_error.details == parser_error.details
    assert loader_error.code == parser_error.code
    assert loader_error.status_code == parser_error.status_code


def test_an_unreadable_job_directory_is_rejected(app_data: Path, job_id: str) -> None:
    """Okunamayan bir dizin de aynı tek hataya düşer."""
    job_dir = prepare_job_directory(app_data, job_id)
    (job_dir / RESULT_FILENAME).write_bytes(b"{}")
    os.chmod(job_dir / RESULT_FILENAME, FILE_MODE)
    os.chmod(job_dir, 0o300)
    try:
        error = _rejects(app_data, job_id)
        assert str(errno.EACCES) not in error.message
    finally:
        os.chmod(job_dir, DIRECTORY_MODE)
