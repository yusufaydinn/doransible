"""R1-V3D2A2A sonuç okuyucusunun şeması, sınırları ve sızdırmazlığı.

İki kural bu dosyanın tamamında geçerlidir:

1. **Sızıntı testleri vacuous olamaz.** Bir sentinel'in hata cevabında
   **bulunmadığını** göstermek tek başına hiçbir şey kanıtlamaz — sentinel giriş
   belgesinde de yoksa test her koşulda geçer. Bu yüzden her sızıntı testi önce
   sentinel'in **giriş belgesinde gerçekten bulunduğunu** doğrular.
2. **Reddetme testleri belgeyi gerçekten bozmalıdır.** Bir alanı "eksilttiğini"
   sanan ama aslında hiç dokunmayan bir test, parser gevşediğinde bile geçerdi;
   bu yüzden bozulan alanın önce belgede bulunduğu (ya da bulunmadığı)
   ölçülür.

Parser'ın **tek** hata cevabı vardır: :class:`JobResultUnavailableError`. Bütün
belge ihlalleri için kod, mesaj ve ``details`` aynıdır ve testler bunu tek tek
değil, ortak bir yardımcı (:func:`_rejects`) üzerinden ölçer.
"""

from __future__ import annotations

import ast
import builtins
import copy
import inspect
import json
import os
import pathlib
import sqlite3
import subprocess
import uuid
from dataclasses import FrozenInstanceError, fields
from typing import Any

import pytest

from app.core.config import (
    PLAYBOOK_RUNNER_MAX_EVENTS_CEILING,
    PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING,
    PLAYBOOK_RUNNER_MIN_RESULT_BYTES,
)
from app.core.errors import AppError
from app.services.execution import result as result_module
from app.services.execution.normalize import (
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
    HostRecap,
    NormalizedEvent,
    NormalizedRun,
    normalize_runner_output,
)
from app.services.execution.result import (
    ANSIBLE_OUTPUT_FIELDS,
    EVENT_FIELDS,
    MAX_ALLOWED_EVENTS,
    MAX_ALLOWED_RESULT_BYTES,
    MIN_ALLOWED_RESULT_BYTES,
    RECAP_FIELDS,
    RESULT_ERROR_CODES,
    RESULT_EVENT_TYPES,
    RESULT_FIELDS_V1,
    RESULT_FIELDS_V2,
    RESULT_OUTCOMES,
    SUPPORTED_SCHEMA_VERSIONS,
    JobResultUnavailableError,
    PlaybookHostRecap,
    PlaybookJobResult,
    PlaybookResultEvent,
    parse_playbook_result,
)

JOB_ID = "3f2b7c1a-9d4e-4a6b-8c1d-5e7f9a0b2c3d"
OTHER_JOB_ID = "8c1d5e7f-9a0b-4c3d-9d4e-3f2b7c1a2c3d"
KNOWN_HOSTS = ("web-1", "db-1")

MAX_EVENTS = 100
MAX_RESULT_BYTES = 100_000

# Sonuç belgesinde **hiçbir seviyede** bulunmaması gereken alanlar ve onlara
# konan sentinel değerler. Değerlerin hepsi "SENTINEL" taşır; testler önce
# sentinel'in belgede bulunduğunu, sonra hata cevabında bulunmadığını ölçer.
# Hiçbiri gerçek bir credential değildir.
RAW_FIELDS: dict[str, Any] = {
    "stdout": "TASK [SENTINEL-STDOUT-BODY] ****",
    "stderr": "fatal: SENTINEL-STDERR-BODY",
    "event_data": {"res": {"msg": "SENTINEL-EVENT-DATA"}},
    "res": {"changed": True, "msg": "SENTINEL-RES-BODY"},
    "task_args": "SENTINEL-TASK-ARGS",
    "task_path": "/srv/SENTINEL-TASK-PATH/site.yml:11",
    "command": "/usr/bin/sudo /bin/sh -c 'echo SENTINEL-ARGV'",
    "argv": ["ansible-playbook", "--private-key", "/secrets/SENTINEL-KEY.pem"],
    "environment": {"ANSIBLE_VAULT_PASSWORD_FILE": "/secrets/SENTINEL-VAULT"},
    "hostvars": {"web-1": {"ansible_become_password": "SENTINEL-BECOME-PW"}},
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY----- SENTINEL-KEY-BODY -----END-----",
    "private_key_path": "/secrets/SENTINEL-KEY-PATH.pem",
    "token": "Bearer SENTINEL-API-TOKEN-9f3a",
    "digest": "sha256:SENTINELDIGEST2c26b46b68ffc68ff99b453c1d3041341342",
    "traceback": "Traceback (most recent call last): File '/opt/SENTINEL-TB.py'",
    "artifact_path": "jobs/SENTINEL-ARTIFACT/result.json",
    "workspace_id": "SENTINEL-WORKSPACE-ID",
}

SENTINEL_MARKER = "SENTINEL"


# --- Belge kurucuları ---------------------------------------------------------


def recap_entry(**overrides: Any) -> dict[str, Any]:
    """Tek bir host'un sayaçları."""
    entry: dict[str, Any] = {
        "ok": 1,
        "changed": 0,
        "failures": 0,
        "unreachable": 0,
        "skipped": 0,
        "rescued": 0,
        "ignored": 0,
    }
    entry.update(overrides)
    return entry


def event_entry(**overrides: Any) -> dict[str, Any]:
    """Tek bir sonuç event'i."""
    entry: dict[str, Any] = {
        "event": "runner_on_ok",
        "host": "web-1",
        "task": "Ping",
        "changed": False,
        "failed": False,
    }
    entry.update(overrides)
    return entry


# Üst düzey ``stdout``'tan gelen, bilinçle **ham** taşınan display metni. Ham
# çıktı sözleşmesinin testleri bu değerin cevapta bulunduğunu ölçer; parser onu
# sansürlemez, yalnız şeklini doğrular.
DISPLAY_OUTPUT = "TASK [Ping] ****\nok: [web-1]\nPLAY RECAP ****"


def legacy_document(**overrides: Any) -> dict[str, Any]:
    """``schema_version=1`` belgesi: diskteki eski artifact'in **tam** şekli.

    Output alanlarını **taşımaz** ve taşımamalıdır; o sürümün tanımı budur.
    """
    document: dict[str, Any] = {
        "schema_version": LEGACY_SCHEMA_VERSION,
        "job_id": JOB_ID,
        "return_code": 0,
        "outcome": OUTCOME_SUCCESSFUL,
        "error_code": None,
        "recap": {"web-1": recap_entry()},
        "events": [
            event_entry(event="playbook_on_task_start", host=None, task="Ping"),
            event_entry(),
        ],
        "events_truncated": False,
        "result_truncated": False,
    }
    document.update(overrides)
    return document


def successful_document(**overrides: Any) -> dict[str, Any]:
    """Bütün invariant'ları sağlayan başarılı bir ``schema_version=2`` belgesi."""
    document = legacy_document()
    document.update(
        {
            "schema_version": SCHEMA_VERSION,
            "ansible_output": DISPLAY_OUTPUT,
            "ansible_output_truncated": False,
        }
    )
    document.update(overrides)
    return document


def failed_document(**overrides: Any) -> dict[str, Any]:
    """Geçerli, başarısız bir sonuç belgesi."""
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": JOB_ID,
        "return_code": 2,
        "outcome": OUTCOME_FAILED,
        "error_code": ERROR_RUNNER_FAILED,
        "recap": {"web-1": recap_entry(ok=0, failures=1)},
        "events": [event_entry(event="runner_on_failed", failed=True)],
        "events_truncated": False,
        "result_truncated": False,
        "ansible_output": DISPLAY_OUTPUT,
        "ansible_output_truncated": False,
    }
    document.update(overrides)
    return document


def parse(document: object, **overrides: Any) -> PlaybookJobResult:
    """Varsayılan çağıran parametreleriyle parser."""
    kwargs: dict[str, Any] = {
        "expected_job_id": JOB_ID,
        "max_events": MAX_EVENTS,
        "max_result_bytes": MAX_RESULT_BYTES,
    }
    kwargs.update(overrides)
    return parse_playbook_result(document, **kwargs)


def _rejects(document: object, **overrides: Any) -> JobResultUnavailableError:
    """Belgenin **tam olarak** sabit sözleşmeyle reddedildiğini ölçer.

    Her ihlal aynı cevabı üretmek zorundadır; ayrım yapan bir cevap, dosyanın
    içeriğini hata mesajı üzerinden daraltmayı mümkün kılardı.
    """
    with pytest.raises(JobResultUnavailableError) as caught:
        parse(document, **overrides)

    error = caught.value
    assert isinstance(error, AppError)
    assert error.status_code == 503
    assert error.code == "job_result_unavailable"
    assert error.details == {"reason": "unavailable"}
    assert error.message == _reference_error().message
    return error


def _reference_error() -> JobResultUnavailableError:
    """Sözleşmenin referans hatası: tamamen boş bir belge."""
    with pytest.raises(JobResultUnavailableError) as caught:
        parse({})
    return caught.value


# --- Gerçek normalize çıktısıyla round-trip -----------------------------------


def _stdout(*lines: dict[str, Any]) -> str:
    return "\n".join(json.dumps(line) for line in lines)


def _stats(**counters: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": {},
        "changed": {},
        "failures": {},
        "dark": {},
        "skipped": {},
        "rescued": {},
        "ignored": {},
        "processed": {},
    }
    payload.update(counters)
    return {"event": "playbook_on_stats", "event_data": payload}


def _normalize(
    stdout_text: str,
    *,
    return_code: int,
    timed_out: bool = False,
    raw_limit_exceeded: bool = False,
) -> NormalizedRun:
    """Gerçek normalize katmanını çağırır; sahte belge kurulmaz."""
    return normalize_runner_output(
        job_id=JOB_ID,
        stdout_text=stdout_text,
        return_code=return_code,
        timed_out=timed_out,
        oversized_stream=None,
        raw_limit_exceeded=raw_limit_exceeded,
        known_hosts=KNOWN_HOSTS,
        connection_values=("deploy-operator", "22"),
        max_events=MAX_EVENTS,
        max_result_bytes=MAX_RESULT_BYTES,
    )


def _successful_run() -> NormalizedRun:
    return _normalize(
        _stdout(
            {"event": "playbook_on_task_start", "event_data": {"task": "Ping"}},
            {
                "event": "runner_on_ok",
                "event_data": {"host": "web-1", "task": "Ping", "res": {"changed": False}},
            },
            _stats(ok={"web-1": 1}, processed={"web-1": 1}),
        ),
        return_code=0,
    )


def test_a_real_successful_normalize_document_round_trips() -> None:
    """Yazan tarafın **gerçek** çıktısı okuyan taraftan geçer.

    Elle kurulmuş bir belge sözleşmenin yalnız bu dosyadaki yorumunu ölçerdi;
    round-trip, iki katmanın gerçekten aynı şemayı konuştuğunu ölçer.
    """
    run = _successful_run()
    document = run.to_document()

    parsed = parse(document)

    assert parsed.schema_version == SCHEMA_VERSION
    assert parsed.job_id == JOB_ID
    assert parsed.return_code == 0
    assert parsed.outcome == OUTCOME_SUCCESSFUL
    assert parsed.error_code is None
    assert parsed.events_truncated is False
    assert parsed.result_truncated is False
    assert set(parsed.recap) == {"web-1"}
    assert parsed.recap["web-1"] == PlaybookHostRecap(
        ok=1, changed=0, failures=0, unreachable=0, skipped=0, rescued=0, ignored=0
    )
    assert parsed.events == (
        PlaybookResultEvent(
            event="playbook_on_task_start", host=None, task="Ping", changed=False, failed=False
        ),
        PlaybookResultEvent(
            event="runner_on_ok", host="web-1", task="Ping", changed=False, failed=False
        ),
    )


def test_a_real_failed_normalize_document_round_trips() -> None:
    """Başarısız ama **geçerli** bir sonuç da okunabilirdir.

    Recap gerçek bir host başarısızlığı bildirdiği için kod ``playbook_failed``
    olur; okuyucu onu ``runner_failed`` kadar sıradan bir belge sayar.
    """
    run = _normalize(
        _stdout(
            {"event": "playbook_on_task_start", "event_data": {"task": "Harden"}},
            {
                "event": "runner_on_failed",
                "event_data": {"host": "web-1", "task": "Harden", "res": {"failed": True}},
            },
            _stats(failures={"web-1": 1}, processed={"web-1": 1}),
        ),
        return_code=2,
    )

    parsed = parse(run.to_document())

    assert parsed.outcome == OUTCOME_FAILED
    assert parsed.error_code == ERROR_PLAYBOOK_FAILED
    assert parsed.return_code == 2
    assert parsed.recap["web-1"].failures == 1
    assert [event.failed for event in parsed.events] == [False, True]


def test_a_real_fail_closed_normalize_document_round_trips() -> None:
    """Fail-closed sonuç: boş recap, boş event listesi ve kırpma işareti."""
    run = _normalize("", return_code=-9, timed_out=True)

    parsed = parse(run.to_document())

    assert parsed.outcome == OUTCOME_FAILED
    assert parsed.error_code == ERROR_RUNNER_TIMEOUT
    assert parsed.recap == {}
    assert parsed.events == ()
    assert parsed.events_truncated is True
    assert parsed.result_truncated is False


# --- Alan kümeleri ------------------------------------------------------------


def test_adding_an_error_code_value_does_not_bump_the_schema_version() -> None:
    """Sürüm alan **kümesini** korur, bir alanın değer kümesini değil.

    R1-V3G1B ``error_code``'a yeni bir sabit ekledi; alan kümesi değişmedi ve
    sürüm o dilimde artmadı. R1-V3J3A ise gerçekten **alan** ekledi ve sürüm tam
    da bu yüzden 2'ye çıktı. İki durumu ayıran kural burada kilitlenir.

    Sürümü gereksiz yere artırmanın bedeli somuttur: karşılaştırma **exact**
    olduğu için (bkz. parser) diskteki eski belgeler bir anda okunamaz olurdu.
    Bu yüzden sürüm 1 hâlâ desteklenir ve okunur.
    """
    assert SCHEMA_VERSION == 2
    assert LEGACY_SCHEMA_VERSION == 1
    assert successful_document()["schema_version"] == SCHEMA_VERSION
    assert set(successful_document()) == RESULT_FIELDS_V2
    # Değer eklemesi sürümü artırmadı: kod sürüm 1 kümesinde de geçerlidir.
    assert ERROR_PLAYBOOK_FAILED in RESULT_ERROR_CODES
    assert (
        parse(
            legacy_document(
                outcome=OUTCOME_FAILED,
                return_code=2,
                error_code=ERROR_PLAYBOOK_FAILED,
                recap={"web-1": recap_entry(ok=0, failures=1)},
                events=[event_entry(event="runner_on_failed", failed=True)],
            )
        ).error_code
        == ERROR_PLAYBOOK_FAILED
    )


def test_the_allowlists_are_exactly_the_documented_field_sets() -> None:
    """Kümeler türetilir ama **beklenen değerleri** burada sabittir.

    Türetme tek doğruluk kaynağını korur; sabitlenen liste ise normalize
    tarafında sessizce eklenen bir alanın burada da sessizce kabul edilmesini
    engeller.
    """
    assert RESULT_FIELDS_V1 == {
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
    assert RESULT_FIELDS_V2 == RESULT_FIELDS_V1 | {
        "ansible_output",
        "ansible_output_truncated",
    }
    assert ANSIBLE_OUTPUT_FIELDS == {"ansible_output", "ansible_output_truncated"}
    assert SUPPORTED_SCHEMA_VERSIONS == {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
    assert RECAP_FIELDS == {
        "ok",
        "changed",
        "failures",
        "unreachable",
        "skipped",
        "rescued",
        "ignored",
    }
    assert EVENT_FIELDS == {"event", "host", "task", "changed", "failed"}
    assert RESULT_OUTCOMES == {"successful", "failed"}
    assert RESULT_ERROR_CODES == {
        "runner_failed",
        "playbook_failed",
        "runner_timeout",
        "runner_output_invalid",
        "result_limit_exceeded",
        "runner_no_hosts",
    }
    assert RESULT_EVENT_TYPES == {
        "playbook_on_task_start",
        "runner_on_ok",
        "runner_on_failed",
        "runner_on_skipped",
        "runner_on_unreachable",
    }


def test_the_allowlists_match_the_writers_document_shape() -> None:
    """Okunan alan kümesi, yazan tarafın **bugün** gerçekten ürettiği kümedir."""
    document = _successful_run().to_document()

    assert set(document) == RESULT_FIELDS_V2
    assert set(document["recap"]["web-1"]) == RECAP_FIELDS
    for event in document["events"]:
        assert set(event) == EVENT_FIELDS


def test_the_allowlists_match_the_normalize_dataclasses_today() -> None:
    """Bugünkü eşitlik ölçülür ama **türetme** değildir.

    Kümeler açıkça yazıldığı için bu eşitlik kendiliğinden doğru değildir; burada
    ölçülür. Normalize'a bir alan eklendiğinde bu test düşer ve o düşüş
    kasıtlıdır: yeni alan, okuyucunun ve cevap şemasının açıkça gözden
    geçirilmesini gerektirir (``schema_version`` tüketici sınırı).
    """
    assert RESULT_FIELDS_V2 == {field.name for field in fields(NormalizedRun)}
    assert RECAP_FIELDS == {field.name for field in fields(HostRecap)}
    assert EVENT_FIELDS == {field.name for field in fields(NormalizedEvent)}


@pytest.mark.parametrize(
    ("level", "field"),
    [
        ("top", "duration_seconds"),
        ("top", "started_at"),
        ("top", "schema_minor"),
        ("recap", "duration_seconds"),
        ("recap", "first_failure"),
        ("event", "duration_seconds"),
        ("event", "sequence"),
    ],
)
def test_a_future_normalizer_field_is_not_accepted_automatically(level: str, field: str) -> None:
    """Yazan tarafa eklenmiş **gibi** duran bir alan sessizce geçmez.

    Alan kümeleri ``dataclasses.fields`` ile türetilseydi, normalize'a eklenen
    böyle bir alan aynı commit'te okuyucudan da geçer ve — şema sürümü hiç
    artmadan — serileştirme sınırına kadar taşınırdı. Explicit kilit tam olarak
    bunu engeller.
    """
    document = _with_raw_field(level, field, 42)
    assert field in json.dumps(document)

    _rejects(document)


def test_the_field_sets_are_not_derived_from_the_normalize_dataclasses() -> None:
    """Kilit gerçekten explicit'tir; kaynakta bir türetme bulunmaz.

    Docstring'de "explicit" yazması bir şey kanıtlamaz; ölçülen AST'in
    kendisidir. ``fields(...)`` çağrısı ya da normalize dataclass'larının import
    edilmesi, kilidi sessizce yeniden türetmeye çevirirdi.
    """
    tree = ast.parse(inspect.getsource(result_module))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    attribute_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    bound = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "fields" not in called
    assert "fields" not in attribute_calls
    assert "fields" not in bound
    for dataclass_name in ("NormalizedRun", "HostRecap", "NormalizedEvent"):
        assert dataclass_name not in bound, dataclass_name
        assert not hasattr(result_module, dataclass_name), dataclass_name


def test_the_internal_models_expose_exactly_the_documented_fields() -> None:
    """Dataclass'lar belgedeki alan kümelerinin dışına çıkmaz."""
    assert set(PlaybookJobResult.__dataclass_fields__) == RESULT_FIELDS_V2
    assert set(PlaybookHostRecap.__dataclass_fields__) == RECAP_FIELDS
    assert set(PlaybookResultEvent.__dataclass_fields__) == EVENT_FIELDS


def test_the_parsed_result_is_immutable() -> None:
    """Doğrulanmış sonuç, doğrulandığı hâlde kalır."""
    parsed = parse(successful_document())

    with pytest.raises(FrozenInstanceError):
        parsed.outcome = OUTCOME_FAILED  # type: ignore[misc]
    with pytest.raises(TypeError):
        parsed.recap["web-1"] = PlaybookHostRecap(  # type: ignore[index]
            ok=9, changed=0, failures=0, unreachable=0, skipped=0, rescued=0, ignored=0
        )
    assert parsed.recap["web-1"].ok == 1


# --- Çağıran sözleşmesi (ValueError) ------------------------------------------


@pytest.mark.parametrize(
    "expected",
    [
        "",
        "not-a-uuid",
        JOB_ID.upper(),
        JOB_ID.replace("-", ""),
        "{" + JOB_ID + "}",
        str(uuid.UUID("3f2b7c1a-9d4e-1a6b-8c1d-5e7f9a0b2c3d")),
    ],
)
def test_a_non_canonical_expected_job_id_is_a_caller_error(expected: str) -> None:
    """Beklenen kimlik canonical UUID4 değilse çağıran hatalıdır.

    Hata bilinçli olarak :class:`JobResultUnavailableError` **değildir**: yanlış
    çağrılmış bir fonksiyon ile bozuk bir artifact aynı şey değildir.
    """
    with pytest.raises(ValueError) as caught:
        parse_playbook_result(
            successful_document(),
            expected_job_id=expected,
            max_events=MAX_EVENTS,
            max_result_bytes=MAX_RESULT_BYTES,
        )

    assert not isinstance(caught.value, AppError)
    assert not expected or expected not in str(caught.value)


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.0, "10", None, MAX_ALLOWED_EVENTS + 1])
def test_an_invalid_event_limit_is_a_caller_error(limit: Any) -> None:
    """``max_events`` pozitif, gerçek bir tam sayı ve tavanın altında olmalıdır."""
    with pytest.raises(ValueError) as caught:
        parse(successful_document(), max_events=limit)

    assert not isinstance(caught.value, AppError)


@pytest.mark.parametrize(
    "limit",
    [
        0,
        -1,
        True,
        False,
        1.0,
        "10",
        None,
        1,
        40,
        MIN_ALLOWED_RESULT_BYTES - 1,
        MAX_ALLOWED_RESULT_BYTES + 1,
    ],
)
def test_an_invalid_byte_limit_is_a_caller_error(limit: Any) -> None:
    """``max_result_bytes`` gerçek bir tam sayı ve taban–tavan aralığında olmalıdır."""
    with pytest.raises(ValueError) as caught:
        parse(successful_document(), max_result_bytes=limit)

    assert not isinstance(caught.value, AppError)


def test_the_caller_limits_match_the_repository_configuration_bounds() -> None:
    """Parser'ın aralığı, ayarların kabul ettiği aralıktır.

    İki tanımın ayrışması, ayarların izin verdiği bir sınırın parser tarafından
    reddedilmesi (ya da tersi) demek olurdu. Sabitler artık config'in **public**
    yüzeyinden gelir; parser private bir eşlemeye bağlı değildir.
    """
    assert MAX_ALLOWED_EVENTS == PLAYBOOK_RUNNER_MAX_EVENTS_CEILING
    assert MAX_ALLOWED_RESULT_BYTES == PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING
    assert MIN_ALLOWED_RESULT_BYTES == PLAYBOOK_RUNNER_MIN_RESULT_BYTES
    assert parse(successful_document(), max_events=MAX_ALLOWED_EVENTS).outcome == OUTCOME_SUCCESSFUL
    assert (
        parse(successful_document(), max_result_bytes=MAX_ALLOWED_RESULT_BYTES).outcome
        == OUTCOME_SUCCESSFUL
    )


def _fail_closed_runs() -> dict[str, NormalizedRun]:
    """Normalizer'ın üretebildiği **bütün** fail-closed zarfları.

    Her biri gerçek normalize çağrısından gelir: elle kurulmuş bir belge yalnız
    bu dosyadaki yorumu ölçerdi.
    """
    return {
        ERROR_RUNNER_TIMEOUT: _normalize("", return_code=-9, timed_out=True),
        ERROR_RESULT_LIMIT_EXCEEDED: _normalize("", return_code=1, raw_limit_exceeded=True),
        ERROR_RUNNER_OUTPUT_INVALID: _normalize("bu satir JSON degil", return_code=0),
        ERROR_RUNNER_FAILED: _normalize("", return_code=2),
        ERROR_RUNNER_NO_HOSTS: _normalize(_stdout(_stats()), return_code=0),
    }


def test_the_normalizer_failure_envelopes_fit_the_minimum_budget() -> None:
    """Kök nedenin regresyon testi: sabit arıza zarfları **en küçük** bütçede okunur.

    Ölçülen zincir şuydu: ayarlar ``max_result_bytes=40``'a izin veriyordu,
    normalizer sınır aşımında sabit bir belge yayımlıyordu (en uzun kodla,
    ``result_limit_exceeded``) ve aynı 40 byte'ı uygulayan okuyucu production'ın
    kendi geçerli belgesini reddediyordu. Taban artık o zarfın üstündedir ve
    zincir kapalıdır.

    Boyut burada **ölçülür**, seçilmez. ``schema_version=2`` iki output alanı
    ekledi ve zarf 212 byte'tan 267'ye çıktı; taban da bu ölçüm yüzünden 256'dan
    yükseltildi. Sabit bir sayı yerine "zarf ≤ taban" ilişkisini kilitlemek
    yetmezdi: taban büyütülürse ilişki kendiliğinden sağlanır ve büyümenin
    nereden geldiği görünmez olurdu.

    Zarfların **hepsi** ölçülür; tek bir örnek, en uzun hata kodunu kaçırabilirdi.

    Küme eşitliği bir kilittir: normalizer'a eklenen yeni bir fail-closed kod
    burada da ölçülmeden geçemez. ``playbook_failed`` bilinçli olarak dışarıdadır
    ve bu bir gevşetme değildir — o kod **hiçbir** yolda boş bir zarf üretmez,
    tanımı gereği dolu bir recap taşır (bkz. aşağıdaki test).
    """
    runs = _fail_closed_runs()
    assert set(runs) == RESULT_ERROR_CODES - {ERROR_PLAYBOOK_FAILED}

    sizes = {}
    for code, run in runs.items():
        # Vacuous değil: zarf gerçekten fail-closed'dur — kod dolu, recap ve
        # event listesi boştur.
        assert run.outcome == OUTCOME_FAILED
        assert run.error_code == code
        assert run.recap == {} and run.events == ()

        sizes[code] = len(run.serialize().encode("utf-8"))
        parsed = parse(run.to_document(), max_result_bytes=MIN_ALLOWED_RESULT_BYTES)
        assert parsed.error_code == code

    assert sizes[ERROR_RESULT_LIMIT_EXCEEDED] == 267
    assert max(sizes.values()) <= MIN_ALLOWED_RESULT_BYTES
    # Zarf hiçbir yolda display output taşımaz; iki alan da sabittir.
    for run in runs.values():
        assert run.ansible_output is None
        assert run.ansible_output_truncated is False


def test_the_playbook_failure_code_never_appears_in_an_empty_envelope() -> None:
    """``playbook_failed`` kanıtsız bir zarfa düşemez.

    Kodun bütün anlamı kanıta dayanır: tek ve son terminal event, boş olmayan
    ``processed`` ve kapsamıyla tutarlı recap. Bu yüzden onu boş recap'li bir
    fail-closed zarfta görmek, sınıflandırmanın kanıttan koptuğu anlamına
    gelirdi. Fail-closed yolların **hepsi** burada tek tek ölçülür.
    """
    for run in _fail_closed_runs().values():
        assert run.error_code != ERROR_PLAYBOOK_FAILED

    # Karşı yön: kod üretildiğinde recap gerçekten doludur ve failure taşır.
    produced = _normalize(
        _stdout(_stats(failures={"web-1": 1}, processed={"web-1": 1})), return_code=2
    )
    assert produced.error_code == ERROR_PLAYBOOK_FAILED
    assert produced.recap["web-1"].failures == 1


def test_the_widest_failure_envelope_still_fits_the_minimum_budget() -> None:
    """En geniş hâl bile tabana sığar: en uzun hata kodu + en uzun çıkış kodu.

    Zarfın boyutu bütçeye değil **yapısına** bağlıdır; tek değişken alan
    ``return_code``'dur ve o da sınırlıdır.
    """
    run = _normalize("", return_code=-2_147_483_648, raw_limit_exceeded=True)

    assert run.error_code == ERROR_RESULT_LIMIT_EXCEEDED
    assert len(run.serialize().encode("utf-8")) <= MIN_ALLOWED_RESULT_BYTES
    assert parse(run.to_document(), max_result_bytes=MIN_ALLOWED_RESULT_BYTES).return_code == (
        -2_147_483_648
    )


def test_the_smallest_configurable_budget_is_accepted_by_the_parser() -> None:
    """Ayarların kabul ettiği en küçük bütçe, parser'ın da kabul ettiği tabandır."""
    assert parse(successful_document(), max_result_bytes=MIN_ALLOWED_RESULT_BYTES + 1000)

    with pytest.raises(ValueError):
        parse(successful_document(), max_result_bytes=MIN_ALLOWED_RESULT_BYTES - 1)


def test_caller_errors_do_not_look_at_the_document() -> None:
    """Parametre hatası, belgeye hiç bakılmadan üretilir."""
    with pytest.raises(ValueError):
        parse("bu bir belge bile değil", max_events=0)


# --- Job kimliği bağı ---------------------------------------------------------


def test_the_document_job_id_must_match_byte_for_byte() -> None:
    """Doğru kimlik geçer, başka bir Job'ın sonucu geçmez."""
    assert parse(successful_document()).job_id == JOB_ID

    _rejects(successful_document(job_id=OTHER_JOB_ID))


@pytest.mark.parametrize(
    "written",
    [
        JOB_ID.upper(),
        JOB_ID.replace("-", ""),
        "{" + JOB_ID + "}",
        "urn:uuid:" + JOB_ID,
        " " + JOB_ID,
        JOB_ID + "\n",
    ],
)
def test_a_differently_written_job_id_is_not_normalized_into_a_match(written: str) -> None:
    """UUID yazımı normalize edilip kabul edilmez.

    Kabul edilseydi "bu dosya bu Job'ın" sözü, dosyaya kimin hangi biçimde
    yazdığına göre değişen bir söz olurdu.
    """
    assert uuid.UUID(written.strip().removeprefix("urn:uuid:")) == uuid.UUID(JOB_ID)

    _rejects(successful_document(job_id=written))


@pytest.mark.parametrize("written", [None, 42, True, ["x"], {"id": JOB_ID}])
def test_a_non_string_job_id_is_rejected(written: Any) -> None:
    """Kimlik metin değilse belge okunmaz."""
    _rejects(successful_document(job_id=written))


# --- Şema sürümü --------------------------------------------------------------


@pytest.mark.parametrize("version", [0, 3, -1, "1", 1.0, True, None])
def test_an_unsupported_schema_version_is_rejected(version: Any) -> None:
    """Sürümü görmeden bir belge yorumlanmaz; ``True`` da ``1`` sayılmaz."""
    # Vacuous değil: değer ya gerçek bir ``int`` değildir ya da desteklenen
    # sürümlerin dışındadır.
    is_real_int = isinstance(version, int) and not isinstance(version, bool)
    assert not is_real_int or version not in SUPPORTED_SCHEMA_VERSIONS

    _rejects(successful_document(schema_version=version))


# --- Üst düzey alan kümesi ----------------------------------------------------


@pytest.mark.parametrize("field", sorted(RESULT_FIELDS_V2))
def test_a_missing_top_level_field_is_rejected(field: str) -> None:
    """Eksik alan varsayılana çevrilmez."""
    document = successful_document()
    assert field in document
    del document[field]

    _rejects(document)


@pytest.mark.parametrize("field", ["job_type", "requested_by", "started_at", "counter", "uuid"])
def test_an_extra_top_level_field_is_rejected(field: str) -> None:
    """Sözleşmede olmayan alan sessizce yok sayılmaz, belgeyi düşürür."""
    document = successful_document(**{field: "eklenmiş"})
    assert field in document

    _rejects(document)


@pytest.mark.parametrize("document", [None, [], "{}", 0, 1.5, True, ()])
def test_a_non_object_document_is_rejected(document: Any) -> None:
    """Belge bir JSON object olmalıdır."""
    _rejects(document)


def test_a_mapping_with_non_string_keys_is_rejected() -> None:
    """Anahtarı metin olmayan bir eşleme JSON object değildir."""
    document = successful_document()
    document[1] = "x"  # type: ignore[index]

    _rejects(document)


# --- Recap --------------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(RECAP_FIELDS))
def test_a_missing_recap_counter_is_rejected(field: str) -> None:
    """Eksik sayaç sıfır varsayılmaz: olmayan bir ölçüm uydurulmaz."""
    entry = recap_entry()
    assert field in entry
    del entry[field]

    _rejects(successful_document(recap={"web-1": entry}))


@pytest.mark.parametrize("field", ["dark", "processed", "ok_total", "host"])
def test_an_extra_recap_field_is_rejected(field: str) -> None:
    """Recap yalnız belgelenen sayaçlardan oluşur."""
    _rejects(successful_document(recap={"web-1": recap_entry(**{field: 1})}))


@pytest.mark.parametrize("value", [-1, -100])
def test_a_negative_recap_counter_is_rejected(value: int) -> None:
    """Negatif sayaç hiçbir çalıştırmayı tarif etmez."""
    _rejects(successful_document(recap={"web-1": recap_entry(skipped=value)}))


@pytest.mark.parametrize("value", [True, False, "1", 1.0, None, [1]])
def test_a_non_integer_recap_counter_is_rejected(value: Any) -> None:
    """Sayaç gerçek bir ``int`` olmalıdır; ``bool`` sessizce sayıya dönüşmez."""
    _rejects(successful_document(recap={"web-1": recap_entry(ok=value)}))


@pytest.mark.parametrize("recap", [None, [], "web-1", 0, {"web-1": []}, {"web-1": "ok"}])
def test_a_malformed_recap_shape_is_rejected(recap: Any) -> None:
    """Recap bir object'tir ve değerleri de object'tir."""
    _rejects(successful_document(recap=recap))


def test_an_empty_or_oversized_recap_host_name_is_rejected() -> None:
    """Host adı boş olamaz ve normalize'ın metin sınırını aşamaz."""
    _rejects(successful_document(recap={"": recap_entry()}, events=[]))

    long_host = "h" * (MAX_TEXT_LENGTH + 1)
    _rejects(successful_document(recap={long_host: recap_entry()}, events=[]))

    boundary = "h" * MAX_TEXT_LENGTH
    parsed = parse(successful_document(recap={boundary: recap_entry()}, events=[]))
    assert set(parsed.recap) == {boundary}


# --- Event listesi ------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(EVENT_FIELDS))
def test_a_missing_event_field_is_rejected(field: str) -> None:
    """Event'in alan kümesi eksik olamaz."""
    entry = event_entry()
    assert field in entry
    del entry[field]

    _rejects(successful_document(events=[entry]))


@pytest.mark.parametrize("field", ["counter", "uuid", "created", "pid", "start_line"])
def test_an_extra_event_field_is_rejected(field: str) -> None:
    """Event'e fazladan bir alan eklenemez."""
    _rejects(successful_document(events=[event_entry(**{field: 1})]))


@pytest.mark.parametrize(
    "name",
    [
        "playbook_on_stats",
        "runner_on_start",
        "verbose",
        "playbook_on_play_start",
        "",
        None,
        1,
    ],
)
def test_an_unknown_event_type_is_rejected(name: Any) -> None:
    """Yalnız normalize allowlist'indeki türler geçer.

    ``playbook_on_stats`` bilinçli olarak reddedilir: terminal event sonuca
    **girmez**, yalnız recap'i üretir.
    """
    assert name not in RESULT_EVENT_TYPES

    _rejects(successful_document(events=[event_entry(event=name)]))


@pytest.mark.parametrize("events", [None, {}, "[]", 0, ({"event": "runner_on_ok"},)])
def test_a_malformed_events_shape_is_rejected(events: Any) -> None:
    """Event listesi bir JSON array olmalıdır."""
    _rejects(successful_document(events=events))


@pytest.mark.parametrize("value", [1, 0, "true", "false", None])
def test_a_non_boolean_event_flag_is_rejected(value: Any) -> None:
    """``changed``/``failed`` gerçek ``bool`` olmalıdır."""
    _rejects(successful_document(events=[event_entry(changed=value)]))
    _rejects(successful_document(events=[event_entry(failed=value)]))


@pytest.mark.parametrize("value", [1, True, [], {}, ""])
def test_a_malformed_event_host_is_rejected(value: Any) -> None:
    """Event host'u ``null`` ya da boş olmayan bir metindir."""
    _rejects(successful_document(events=[event_entry(host=value)]))


@pytest.mark.parametrize("value", [1, True, [], {}])
def test_a_malformed_event_task_is_rejected(value: Any) -> None:
    """Event task'ı ``null`` ya da metindir."""
    _rejects(successful_document(events=[event_entry(task=value)]))


def test_event_text_is_bounded_by_the_normalize_limit() -> None:
    """Metin sınırı normalize ile aynıdır ve sınırda kabul edilir."""
    boundary = "t" * MAX_TEXT_LENGTH
    parsed = parse(successful_document(events=[event_entry(task=boundary)]))
    assert parsed.events[0].task == boundary

    _rejects(successful_document(events=[event_entry(task="t" * (MAX_TEXT_LENGTH + 1))]))


# --- Sonuç ve hata kodu -------------------------------------------------------


@pytest.mark.parametrize("outcome", ["ok", "success", "SUCCESSFUL", "", None, 1, "canceled"])
def test_an_unknown_outcome_is_rejected(outcome: Any) -> None:
    """``outcome`` sabit iki değerden biridir."""
    assert outcome not in RESULT_OUTCOMES

    _rejects(successful_document(outcome=outcome))


@pytest.mark.parametrize(
    "code",
    [
        "runner_start_failed",
        "workspace_unavailable",
        "interrupted_by_restart",
        "unknown_failure",
        "boom",
        "",
        1,
        True,
    ],
)
def test_an_error_code_outside_the_result_allowlist_is_rejected(code: Any) -> None:
    """Belgeye yazılamayacak bir kod okunurken de kabul edilmez.

    İlk dört değer gerçekten var olan Job kodlarıdır ama ``normalize`` onları bir
    **belgeye** hiç yazmaz; sonucun içinde görülmeleri, dosyanın başka bir yerden
    geldiğini gösterir.
    """
    assert code not in RESULT_ERROR_CODES

    _rejects(failed_document(error_code=code))


@pytest.mark.parametrize("code", sorted(RESULT_ERROR_CODES))
def test_every_allowlisted_error_code_is_accepted_on_a_failed_result(code: str) -> None:
    """Allowlist'teki her kod başarısız bir sonuçta okunabilir."""
    assert parse(failed_document(error_code=code)).error_code == code


@pytest.mark.parametrize("value", [True, False, 0, 1, "false", None, "true"])
def test_a_non_boolean_truncation_flag_is_rejected_when_not_a_bool(value: Any) -> None:
    """Kırpma bayrakları gerçek ``bool`` olmalıdır; sayı geçmez."""
    if isinstance(value, bool):
        # Gerçek bool'lar geçerlidir; burada ölçülen yalnız tip kontrolüdür.
        parse(failed_document(events_truncated=value, result_truncated=value))
        return

    _rejects(failed_document(events_truncated=value))
    _rejects(failed_document(result_truncated=value))


@pytest.mark.parametrize("value", [True, "0", 1.5, None, [0]])
def test_a_non_integer_return_code_is_rejected(value: Any) -> None:
    """``return_code`` gerçek bir ``int``'tir; ``true`` sessizce ``1`` olmaz."""
    _rejects(failed_document(return_code=value))


# --- Semantik invariant'lar ---------------------------------------------------


def test_a_successful_result_must_have_a_zero_return_code() -> None:
    """``successful`` ve ``rc != 0`` birlikte olamaz."""
    _rejects(successful_document(return_code=1))
    _rejects(successful_document(return_code=-9))


def test_a_successful_result_must_not_carry_an_error_code() -> None:
    """ "Başarılı ama şu hatayla" okunabilir bir sonuç değildir."""
    _rejects(successful_document(error_code=ERROR_RUNNER_FAILED))


@pytest.mark.parametrize("flag", ["events_truncated", "result_truncated"])
def test_a_successful_result_must_not_be_truncated(flag: str) -> None:
    """Kırpılmış bir sonuç tam sanılamaz."""
    _rejects(successful_document(**{flag: True}))


def test_a_successful_result_must_have_a_non_empty_recap() -> None:
    """Hiçbir host'a dokunmamış bir çalıştırma başarılı değildir."""
    _rejects(successful_document(recap={}, events=[]))


@pytest.mark.parametrize("counter", ["failures", "unreachable"])
def test_a_successful_result_cannot_report_failed_or_unreachable_hosts(counter: str) -> None:
    """``rc=0`` recap'teki başarısızlığı geçersiz kılmaz."""
    _rejects(successful_document(recap={"web-1": recap_entry(**{counter: 1})}))


def test_a_truncated_result_cannot_be_successful() -> None:
    """Kırpma bayrağı ile başarı birlikte bulunamaz."""
    document = successful_document(result_truncated=True)
    assert document["outcome"] == OUTCOME_SUCCESSFUL

    _rejects(document)


def test_a_failed_result_must_carry_an_error_code() -> None:
    """Sebepsiz bir başarısızlık, okuyan tarafa hiçbir şey söylemez."""
    _rejects(failed_document(error_code=None))


def test_an_event_host_must_appear_in_the_recap() -> None:
    """Recap çalıştırmanın kapsamıdır; dışındaki bir host kapsamı yalanlar."""
    document = successful_document(events=[event_entry(host="db-1")])
    assert "db-1" not in document["recap"]

    _rejects(document)

    # Kapsam genişletildiğinde aynı belge okunabilir olur: reddedilen şey host'un
    # kendisi değil, recap ile event listesinin ayrışmasıdır.
    widened = successful_document(
        recap={"web-1": recap_entry(), "db-1": recap_entry()},
        events=[event_entry(host="db-1")],
    )
    assert parse(widened).events[0].host == "db-1"


def test_a_failed_result_may_have_an_empty_recap() -> None:
    """Fail-closed sonuçta recap boştur ve bu geçerlidir."""
    parsed = parse(
        failed_document(error_code=ERROR_RUNNER_TIMEOUT, recap={}, events=[], events_truncated=True)
    )

    assert parsed.recap == {}
    assert parsed.events == ()


# --- Sınırlar -----------------------------------------------------------------


def _many_events(count: int) -> list[dict[str, Any]]:
    """Host taşımayan, sınır ölçmeye yarayan event listesi."""
    return [
        event_entry(event="playbook_on_task_start", host=None, task=f"T{index}")
        for index in range(count)
    ]


def test_the_event_count_limit_is_enforced_at_its_boundary() -> None:
    """Sınırın kendisi geçer, bir fazlası geçmez."""
    document = successful_document(events=_many_events(10))
    assert len(document["events"]) == 10

    assert len(parse(document, max_events=10).events) == 10
    _rejects(document, max_events=9)


def test_the_byte_limit_is_measured_exactly_as_the_writer_measures_it() -> None:
    """Okuyan ve yazan taraf aynı canonical biçimi ölçer.

    Biçim ayrışsaydı aynı belge bir tarafta sınırın altında, diğerinde üstünde
    çıkardı ve sınır hiçbir şey söylemezdi.
    """
    run = _successful_run()
    document = run.to_document()
    size = len(run.serialize().encode("utf-8"))

    assert parse(document, max_result_bytes=size).job_id == JOB_ID
    _rejects(document, max_result_bytes=size - 1)


def test_a_result_over_the_byte_limit_is_rejected() -> None:
    """Sınırı aşan belge kırpılmaz, reddedilir."""
    document = successful_document(events=_many_events(50))
    size = _canonical_size(document)
    assert size > MIN_ALLOWED_RESULT_BYTES

    assert parse(document, max_result_bytes=size).events_truncated is False
    _rejects(document, max_result_bytes=size - 1)


def _canonical_size(document: dict[str, Any]) -> int:
    """Belgenin canonical compact JSON boyutu."""
    return len(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    )


HUGE_COUNT = 2_000


def _huge_document() -> dict[str, Any]:
    """Bütçeyi kat kat aşan, çok sayıda recap ve event taşıyan bir belge."""
    return successful_document(
        recap={f"host-{index}": recap_entry() for index in range(HUGE_COUNT)},
        events=_many_events(HUGE_COUNT),
    )


# --- Artımlı byte bütçesi -----------------------------------------------------


def test_the_canonical_encoder_produces_the_writers_exact_bytes() -> None:
    """Artımlı ölçüm, yazan tarafın ürettiği metnin **aynısını** üretir.

    Chunk'ları birleştirmek ``NormalizedRun.serialize()`` ile birebir aynı
    dizgiyi vermelidir; vermeseydi iki taraf farklı bir belgenin boyutunu ölçer
    ve sınır hiçbir şey söylemezdi.
    """
    run = _successful_run()
    document = run.to_document()

    chunks = list(result_module._CANONICAL_ENCODER.iterencode(document))

    assert "".join(chunks) == run.serialize()
    # Gerçekten artımlı: tek parçalık bir "iterencode" ölçümü bounded yapmazdı.
    assert len(chunks) > 1


def test_the_byte_budget_runs_before_the_nested_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bütçe aşıldığında nested recap/event dönüşümü **hiç** çalışmaz.

    Davranışla kanıtlanır: dönüşüm fonksiyonları patlayıcıyla değiştirilir. Ters
    sırada bir parser onları çağırır ve ``AssertionError`` ile düşerdi; doğru
    sırada olan parser onlara hiç ulaşmadan generic 503 üretir.
    """

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Bütçe aşıldıktan sonra nested dönüşüm çalışmamalıdır.")

    monkeypatch.setattr(result_module, "_require_recap", boom)
    monkeypatch.setattr(result_module, "_require_events", boom)

    document = _huge_document()
    assert _canonical_size(document) > MIN_ALLOWED_RESULT_BYTES

    # `max_events` bilinçli olarak yükseltilir: sayı sınırı belgeyi zaten
    # elerdi ve ölçülen şey bütçe sırası olmaktan çıkardı.
    _rejects(document, max_events=HUGE_COUNT, max_result_bytes=MIN_ALLOWED_RESULT_BYTES)


def test_the_nested_conversion_still_runs_for_a_document_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kontrol grubu: bütçe aşılmadığında aynı fonksiyonlar **çağrılır**.

    Bu olmadan yukarıdaki test vacuous olurdu — parser onları hiçbir zaman
    çağırmıyor olsaydı da geçerdi.
    """
    calls: list[str] = []
    original_recap = result_module._require_recap
    original_events = result_module._require_events

    def spy_recap(value: object) -> Any:
        calls.append("recap")
        return original_recap(value)

    def spy_events(value: Any) -> Any:
        calls.append("events")
        return original_events(value)

    monkeypatch.setattr(result_module, "_require_recap", spy_recap)
    monkeypatch.setattr(result_module, "_require_events", spy_events)

    parse(successful_document())

    assert calls == ["recap", "events"]


def test_the_budget_never_builds_a_second_full_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``json.dumps`` çağrılmaz: ikinci bir tam belge bellekte kurulmaz."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Bütçe ölçümü tam bir belge serileştirmemelidir.")

    monkeypatch.setattr(json, "dumps", boom)
    monkeypatch.setattr(json.JSONEncoder, "encode", boom)

    assert parse(successful_document()).outcome == OUTCOME_SUCCESSFUL
    _rejects(_huge_document(), max_events=HUGE_COUNT, max_result_bytes=MIN_ALLOWED_RESULT_BYTES)


def test_the_parser_source_measures_incrementally() -> None:
    """Kaynak kilidi: ölçüm ``iterencode`` ile yapılır, tam serileştirmeyle değil.

    ``str.encode`` çağrısı beklenir ve serbesttir — chunk'ları byte'a çeviren
    odur. Yasak olan, tek seferde bütün belgeyi metne çeviren ``json.dumps`` ve
    ``JSONEncoder.encode``'dur.
    """
    source = inspect.getsource(result_module)
    tree = ast.parse(source)
    attribute_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "iterencode" in attribute_calls
    # Tam belge serileştiren iki yol da kaynakta bulunmaz. Kontrol AST üzerinden
    # yapılır; docstring'de geçen bir ad testi ne geçirir ne düşürür.
    assert "dumps" not in attribute_calls
    assert "encode" not in {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"json", "_CANONICAL_ENCODER"}
    }


def test_the_byte_budget_is_exact_at_its_boundary() -> None:
    """Sınırın kendisi kabul edilir, bir eksiği reddedilir."""
    document = successful_document(events=_many_events(20))
    size = _canonical_size(document)
    assert size > MIN_ALLOWED_RESULT_BYTES

    assert parse(document, max_result_bytes=size).job_id == JOB_ID
    _rejects(document, max_result_bytes=size - 1)


def test_the_budget_rejection_does_not_mutate_the_input() -> None:
    """Bütçe reddi de girdiyi değiştirmez."""
    document = _huge_document()
    snapshot = copy.deepcopy(document)

    _rejects(document, max_events=HUGE_COUNT, max_result_bytes=MIN_ALLOWED_RESULT_BYTES)

    assert document == snapshot
    assert len(document["recap"]) == HUGE_COUNT
    assert len(document["events"]) == HUGE_COUNT


@pytest.mark.parametrize(
    "value",
    [
        {"unserializable"},
        float("nan"),
        float("inf"),
        object(),
        1j,
    ],
)
def test_a_value_the_canonical_encoder_rejects_falls_into_the_same_error(value: Any) -> None:
    """Serileştirme ihlalleri de aynı generic cevaba düşer.

    ``TypeError``/``ValueError``'ın kendi metni offending değeri veya tipini
    yazardı; hiçbiri cevaba taşınmaz.
    """
    error = _rejects(successful_document(return_code=value))

    assert repr(value) not in error.message
    assert repr(value) not in repr(error.details)
    assert type(value).__name__ not in error.message


def test_a_circular_document_does_not_crash_the_parser() -> None:
    """Döngüsel referans generic cevaba düşer, ``ValueError`` olarak sızmaz."""
    document = successful_document()
    document["recap"]["web-1"]["ok"] = 1
    document["events"].append(document["events"])

    _rejects(document)


# --- Katı JSON kapları --------------------------------------------------------


class _HostileDict(dict[str, Any]):
    """Doğrulanan değer ile sonradan okunan değerin ayrıştığı bir eşleme.

    ``set(...)`` gerçek anahtarları gösterir ama her okuma sentinel döndürür:
    ``isinstance`` ile kabul edilen bir alt sınıfta doğrulama, sonradan okunan
    değer hakkında hiçbir şey söylemezdi.
    """

    def __getitem__(self, key: str) -> Any:
        return "SENTINEL-SWAPPED-VALUE"


class _HostileList(list[Any]):
    """Uzunluğunu olduğundan küçük gösteren bir liste."""

    def __len__(self) -> int:
        return 0


@pytest.mark.parametrize("level", ["top", "recap_container", "recap_entry", "event"])
def test_a_dict_subclass_is_not_accepted_as_a_json_object(level: str) -> None:
    """Parser JSON decoder çıktısı bekler; davranış üreten bir alt sınıf geçmez.

    ``isinstance`` böyle bir nesneyi kabul ederdi ve doğrulanan değer ile
    sonradan okunan değerin aynı olacağı garantisi düşerdi.
    """
    document: Any = successful_document()
    if level == "top":
        hostile = _HostileDict(document)
        document = hostile
    elif level == "recap_container":
        hostile = _HostileDict(document["recap"])
        document["recap"] = hostile
    elif level == "recap_entry":
        hostile = _HostileDict(recap_entry())
        document["recap"] = {"web-1": hostile}
    else:
        hostile = _HostileDict(event_entry())
        document["events"] = [hostile]

    # Vacuous değil: alt sınıf gerçekten yalan söylüyor — anahtar kümesi doğru,
    # okunan her değer sentinel.
    assert set(hostile), level
    assert all(hostile[key] == "SENTINEL-SWAPPED-VALUE" for key in hostile)

    error = _rejects(document)

    assert SENTINEL_MARKER not in error.message
    assert SENTINEL_MARKER not in repr(error.details)
    assert SENTINEL_MARKER not in repr(error)


def test_a_list_subclass_is_not_accepted_as_a_json_array() -> None:
    """Event listesi düz bir ``list`` olmalıdır.

    Vacuous değil: alt sınıf gerçekten yalan söylüyor — ``len`` sıfır döndürerek
    her sayı sınırını geçerdi.
    """
    hostile = _HostileList([event_entry() for _ in range(5)])
    assert len(hostile) == 0
    assert len(list(hostile)) == 5

    _rejects(successful_document(events=hostile), max_events=1)


def test_a_string_subclass_is_not_accepted_as_a_json_string() -> None:
    """Metin alanları da düz ``str`` olmalıdır."""

    class _HostileStr(str):
        pass

    _rejects(successful_document(job_id=_HostileStr(JOB_ID)))
    _rejects(successful_document(events=[event_entry(task=_HostileStr("Ping"))]))


# --- Raw alanlar ve sızdırmazlık ----------------------------------------------


def _with_raw_field(level: str, name: str, value: Any) -> dict[str, Any]:
    """Belgenin bir seviyesine yasak bir alan yerleştirir."""
    if level == "top":
        return successful_document(**{name: value})
    if level == "recap":
        return successful_document(recap={"web-1": recap_entry(**{name: value})})
    return successful_document(events=[event_entry(**{name: value})])


@pytest.mark.parametrize("level", ["top", "recap", "event"])
@pytest.mark.parametrize("name", sorted(RAW_FIELDS))
def test_a_raw_or_secret_bearing_field_falls_into_the_same_generic_error(
    level: str, name: str
) -> None:
    """Raw alan hangi seviyede olursa olsun aynı sabit 503'e düşer.

    Önce sentinel'in belgede **gerçekten** bulunduğu doğrulanır: bulunmuyorsa
    test hiçbir şey ölçmezdi.
    """
    document = _with_raw_field(level, name, RAW_FIELDS[name])
    serialized = json.dumps(document)
    assert SENTINEL_MARKER in serialized

    error = _rejects(document)

    assert SENTINEL_MARKER not in error.message
    assert SENTINEL_MARKER not in repr(error.details)
    assert SENTINEL_MARKER not in repr(error)
    assert SENTINEL_MARKER not in str(error)


@pytest.mark.parametrize("level", ["top", "recap", "event"])
@pytest.mark.parametrize("name", sorted(RAW_FIELDS))
def test_the_error_never_names_the_offending_field(level: str, name: str) -> None:
    """Hata cevabı ihlalin **yerini** de söylemez."""
    error = _rejects(_with_raw_field(level, name, RAW_FIELDS[name]))

    assert name not in error.message
    assert name not in repr(error.details)


def test_the_error_carries_no_job_id_field_name_or_parser_text() -> None:
    """Sabit sözleşme: mesaj ve ``details`` hiçbir belge ayrıntısı taşımaz."""
    error = _rejects(successful_document(job_id=OTHER_JOB_ID, outcome="boom"))

    for leaked in (JOB_ID, OTHER_JOB_ID, "boom", "outcome", "job_id", "_RejectedDocument"):
        assert leaked not in error.message, leaked
        assert leaked not in repr(error.details), leaked
        assert leaked not in repr(error), leaked


def test_every_violation_produces_the_identical_error_contract() -> None:
    """Farklı ihlaller ayırt edilemez cevaplar üretir."""
    violations: list[dict[str, Any]] = [
        successful_document(schema_version=3),
        successful_document(job_id=OTHER_JOB_ID),
        successful_document(outcome="boom"),
        successful_document(return_code=1),
        successful_document(recap={}, events=[]),
        successful_document(events=[event_entry(event="playbook_on_stats")]),
        failed_document(error_code=None),
        successful_document(stdout="SENTINEL"),
    ]

    responses = set()
    for document in violations:
        error = _rejects(document)
        assert isinstance(error.details, dict)
        responses.add(
            (error.status_code, error.code, error.message, tuple(sorted(error.details.items())))
        )
    assert len(responses) == 1


def test_the_error_details_are_not_shared_between_raises() -> None:
    """Her hata kendi ``details`` sözlüğünü taşır.

    Paylaşılan bir sözlük, onu değiştiren tek bir çağıran yüzünden sonraki bütün
    hata cevaplarını değiştirirdi.
    """
    first = _rejects(successful_document(schema_version=3))
    assert isinstance(first.details, dict)
    first.details["reason"] = "değiştirildi"

    second = _rejects(successful_document(schema_version=3))
    assert second.details == {"reason": "unavailable"}


def test_the_error_builds_its_own_message_and_details() -> None:
    """Sabitlik sınıfın **constructor'ındadır**, çağıranların nezaketinde değil.

    Sözleşme bir konvansiyon olarak yazılıp her ``raise`` yerine bırakılsaydı,
    hatayı yükselten yeni bir yol kendi metnini geçirebilirdi ve "bütün ihlaller
    ayırt edilemez" iddiası çağıran sayısı arttıkça sessizce düşerdi.
    """
    error = JobResultUnavailableError()

    assert error.status_code == 503
    assert error.code == "job_result_unavailable"
    assert error.details == {"reason": "unavailable"}
    assert error.message == _reference_error().message
    assert str(error) == error.message


@pytest.mark.parametrize(
    "args, kwargs",
    [
        (("başka mesaj",), {}),
        ((), {"details": {"reason": "leak"}}),
        (("başka mesaj",), {"details": {"reason": "leak"}}),
    ],
    ids=["message", "details", "both"],
)
def test_the_caller_cannot_choose_the_message_or_details(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    """Constructor hiçbir parametre almaz.

    Ölçüm runtime'dadır: tip denetimi bu çağrıları zaten durdurur, ama sınıf
    imzası bir gün gevşetilirse testin düşmesi gerekir.
    """
    with pytest.raises(TypeError):
        JobResultUnavailableError(*args, **kwargs)


def test_each_error_instance_owns_its_details_dictionary() -> None:
    """``details`` paylaşılan bir nesne değildir.

    Sınıf düzeyinde tek bir sözlük paylaşılsaydı, onu değiştiren tek bir çağıran
    sonraki bütün hata cevaplarını değiştirirdi.
    """
    first = JobResultUnavailableError()
    second = JobResultUnavailableError()

    assert first.details == second.details
    assert first.details is not second.details

    assert isinstance(first.details, dict)
    first.details["reason"] = "değiştirildi"

    assert second.details == {"reason": "unavailable"}
    assert JobResultUnavailableError().details == {"reason": "unavailable"}


def test_the_module_keeps_no_shared_message_or_details_constant() -> None:
    """Paylaşılan sabitler kaldırıldı.

    Modül düzeyinde duran bir mesaj/``details`` sabiti, başka bir modülün onu
    private olarak import edip kendi ``raise``'ini kurmasına davetiyeydi; tam da
    constructor'ın kapattığı yol.
    """
    assert not hasattr(result_module, "_UNAVAILABLE_MESSAGE")
    assert not hasattr(result_module, "_UNAVAILABLE_DETAILS")


# --- Girdiye dokunmama --------------------------------------------------------


def test_the_input_document_is_not_mutated_on_success() -> None:
    """Doğrulama girdiyi değiştirmez."""
    document = successful_document()
    snapshot = copy.deepcopy(document)

    parse(document)

    assert document == snapshot


def test_the_input_document_is_not_mutated_on_rejection() -> None:
    """Reddedilen belge de düzeltilmez, alanı silinmez."""
    document = successful_document(stdout=RAW_FIELDS["stdout"], return_code=7)
    snapshot = copy.deepcopy(document)

    _rejects(document)

    assert document == snapshot
    assert "stdout" in document


def test_the_result_does_not_alias_the_input_containers() -> None:
    """Sonuç, girdinin kaplarına bağlı kalmaz."""
    document = successful_document()
    parsed = parse(document)

    document["recap"]["web-1"]["ok"] = 999
    document["events"].append(event_entry(event="runner_on_skipped"))

    assert parsed.recap["web-1"].ok == 1
    assert len(parsed.events) == 2


# --- Repr ---------------------------------------------------------------------


def test_the_result_repr_does_not_dump_the_document() -> None:
    """``repr`` içerik değil şekil gösterir.

    Varsayılan dataclass repr'i host adlarını, task metinlerini ve Job kimliğini
    taşırdı; tek bir ``logger.debug(result)`` çağrısı onları göründür kılardı.
    """
    task = "Deploy SENTINEL-TASK-NAME"
    parsed = parse(successful_document(events=[event_entry(task=task)]))

    # Vacuous değil: sentinel gerçekten sonucun içindedir.
    assert parsed.events[0].task == task

    text = repr(parsed)
    for leaked in (SENTINEL_MARKER, task, JOB_ID, "web-1", "recap=", "events=("):
        assert leaked not in text, leaked
    assert text == "PlaybookJobResult(outcome='successful', hosts=1, events=1)"


def test_the_result_str_matches_its_repr() -> None:
    """``str`` de ayrı bir sızıntı yolu açmaz."""
    parsed = parse(successful_document())

    assert str(parsed) == repr(parsed)
    assert JOB_ID not in str(parsed)


# --- Kapsam kilidi ------------------------------------------------------------


def test_the_parser_touches_no_filesystem_database_or_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saf parser: dosya açmaz, süreç başlatmaz, veritabanına bağlanmaz."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Saf parser bu çağrıyı yapmamalıdır.")

    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(os, "open", boom)
    monkeypatch.setattr(os, "listdir", boom)
    monkeypatch.setattr(os, "stat", boom)
    monkeypatch.setattr(pathlib.Path, "open", boom)
    monkeypatch.setattr(pathlib.Path, "read_text", boom)
    monkeypatch.setattr(pathlib.Path, "exists", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(sqlite3, "connect", boom)

    assert parse(successful_document()).outcome == OUTCOME_SUCCESSFUL
    _rejects(successful_document(schema_version=3))


def test_the_result_parser_imports_no_io_database_or_route_layer() -> None:
    """Modülün **gerçek** import listesi bir sözleşmedir ve tam eşitlikle ölçülür.

    Docstring'de geçen bir modül adı testi ne geçirir ne düşürür; ölçülen AST'in
    kendisidir. Dosya sistemi, veritabanı, subprocess, runner ve HTTP katmanı
    buraya giremez: saf bir doğrulayıcının hiçbirine ihtiyacı yoktur ve birine
    bağlanması, okuma yolunun sessizce yan etki üretebileceği anlamına gelirdi.
    """
    tree = ast.parse(inspect.getsource(result_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported == {
        "__future__",
        "json",
        "uuid",
        "collections.abc",
        "dataclasses",
        "types",
        "typing",
        "app.core.config",
        "app.core.errors",
        "app.services.execution.normalize",
    }
    for forbidden in (
        "os",
        "pathlib",
        "shutil",
        "subprocess",
        "sqlite3",
        "threading",
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
        "app.services.execution.runner_process",
        "app.services.execution.store",
        "app.services.execution.worker",
        "app.services.execution.workspace",
    ):
        assert forbidden not in imported, forbidden


def test_the_parser_does_not_call_the_normalize_entry_point() -> None:
    """Burada üretilen bir şey yoktur; yalnız üretilmiş bir belge okunur.

    ``normalize_runner_output`` çağrılsaydı okuyucu, okuduğu belgeyi yeniden
    üretebilen bir yol açar ve "yazan taraf ayrıdır" sözü düşerdi.
    """
    tree = ast.parse(inspect.getsource(result_module))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    bound = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "normalize_runner_output" not in called
    # Hiç bağlanmamış bir ad çağrılamaz: yasak, bir kullanımın yokluğundan değil
    # ismin hiç içeri alınmamasından gelir.
    assert "normalize_runner_output" not in bound
    assert not hasattr(result_module, "normalize_runner_output")


# --- R1-V3J3A: sürüm 1/2 uyumluluğu ve display output ------------------------


def test_a_legacy_version_one_document_is_still_readable() -> None:
    """Diskteki eski artifact okunmaya devam eder.

    Gerçek bir migration yoktur: sürüm 1 belgeleri yerinde kalır. Okunduklarında
    output alanları ``None``/``False``'tur — bu bir varsayılan doldurma değil, o
    sürümün **tanımı**dır: belge hiç display çıktısı taşımıyordu.
    """
    document = legacy_document()
    assert set(document) == RESULT_FIELDS_V1
    assert not (ANSIBLE_OUTPUT_FIELDS & set(document))

    parsed = parse(document)

    assert parsed.schema_version == LEGACY_SCHEMA_VERSION
    assert parsed.ansible_output is None
    assert parsed.ansible_output_truncated is False
    # Geri kalan sözleşme değişmez.
    assert parsed.outcome == OUTCOME_SUCCESSFUL
    assert parsed.recap["web-1"].ok == 1
    assert len(parsed.events) == 2


def test_the_parser_never_normalizes_the_artifact_version() -> None:
    """Dönen nesne artifact'in **gerçek** sürümünü taşır.

    Hepsini en yeni sürüme yazmak, okunan belgenin taşımadığı bir sözleşmeyi
    taşıyormuş gibi göstermek olurdu.
    """
    assert parse(legacy_document()).schema_version == LEGACY_SCHEMA_VERSION
    assert parse(successful_document()).schema_version == SCHEMA_VERSION
    assert LEGACY_SCHEMA_VERSION != SCHEMA_VERSION


def test_a_version_two_document_carries_the_display_output_verbatim() -> None:
    """Parser ham metni **değiştirmez**: sansürlemez, kırpmaz, normalize etmez."""
    parsed = parse(successful_document())

    assert parsed.ansible_output == DISPLAY_OUTPUT
    assert parsed.ansible_output_truncated is False


def test_a_truncated_flag_is_carried_on_both_kinds_of_result() -> None:
    """``ansible_output_truncated`` metinle birlikte de, metinsiz de geçerlidir.

    İki gerçek yol vardır: 128 KiB sınırında kırpılmış bir metin (dolu + ``True``)
    ve genel sonuç bütçesine sığmadığı için tümüyle bırakılmış bir çıktı
    (``None`` + ``True``). İkisi de yazan tarafın üretebildiği belgelerdir.
    """
    trimmed = parse(successful_document(ansible_output_truncated=True))
    assert trimmed.ansible_output == DISPLAY_OUTPUT
    assert trimmed.ansible_output_truncated is True

    dropped = parse(successful_document(ansible_output=None, ansible_output_truncated=True))
    assert dropped.ansible_output is None
    assert dropped.ansible_output_truncated is True


@pytest.mark.parametrize("field", sorted(ANSIBLE_OUTPUT_FIELDS))
def test_a_version_one_document_carrying_an_output_field_is_rejected(field: str) -> None:
    """Sürüm 1 kümesi kapalıdır: hiçbir writer böyle bir belge üretemez."""
    document = legacy_document(**{field: None if field == "ansible_output" else False})
    assert field in document

    _rejects(document)


@pytest.mark.parametrize("field", sorted(ANSIBLE_OUTPUT_FIELDS))
def test_a_version_two_document_missing_an_output_field_is_rejected(field: str) -> None:
    """Eksik output alanı varsayılana çevrilmez; belge düşer.

    Doldurmak, olmayan bir ölçümü uydurmak olurdu: "çıktı yoktu" ile "alan hiç
    yazılmadı" aynı şey değildir.
    """
    document = successful_document()
    assert field in document
    del document[field]

    _rejects(document)


@pytest.mark.parametrize(
    "value",
    [7, 1.5, True, False, ["ok"], {"text": "ok"}, b"ok"],
    ids=["int", "float", "true", "false", "list", "object", "bytes"],
)
def test_a_non_string_ansible_output_is_rejected(value: Any) -> None:
    """``ansible_output`` yalnız gerçek bir ``str`` veya ``None``'dır."""
    _rejects(successful_document(ansible_output=value))


@pytest.mark.parametrize("value", [1, 0, "true", "false", None, [], "yes"])
def test_a_non_boolean_output_truncated_flag_is_rejected(value: Any) -> None:
    """Bayrak yalnız gerçek ``bool`` kabul eder; ``1`` sessizce ``True`` olmaz."""
    _rejects(successful_document(ansible_output_truncated=value))


def test_an_ansible_output_over_the_byte_limit_is_rejected() -> None:
    """Sınır **UTF-8 byte** üzerinden ve yazan tarafla aynı sabitle uygulanır.

    Sınırın tam üstündeki belge düzeltilmez, reddedilir: kırpmak okuyucunun işi
    olsaydı, yazan tarafın ürettiğinden başka bir metin "ham çıktı" diye
    sunulurdu.
    """
    # Genel bütçe bilinçli olarak geniş: reddedilen şeyin **output sınırı**
    # olduğu ölçülmelidir, byte bütçesi değil.
    roomy = {"max_result_bytes": MAX_ALLOWED_RESULT_BYTES}

    assert parse(successful_document(ansible_output="a" * MAX_ANSIBLE_OUTPUT_BYTES), **roomy)
    _rejects(successful_document(ansible_output="a" * (MAX_ANSIBLE_OUTPUT_BYTES + 1)), **roomy)

    # Ölçüm karakter değil byte sayar: çok baytlı metin sınıra daha erken çarpar.
    multibyte = "ö" * (MAX_ANSIBLE_OUTPUT_BYTES // 2)
    assert len(multibyte) < MAX_ANSIBLE_OUTPUT_BYTES
    assert parse(successful_document(ansible_output=multibyte), **roomy)
    _rejects(successful_document(ansible_output=multibyte + "ö"), **roomy)


def test_an_output_that_cannot_be_encoded_as_utf8_is_rejected() -> None:
    """Yalnız-surrogate bir metin byte olarak ölçülemez ve fail-closed düşer.

    JSON ``\\udXXX`` kaçışları böyle bir değer üretebilir. Kabul edilseydi cevabın
    serileştirilmesi düşerdi; kullanıcıya 500 döndürmektense belgeyi sabit 503
    sözleşmesiyle reddetmek dürüsttür.
    """
    lone_surrogate = "ok: [web-1] \ud800"
    with pytest.raises(UnicodeEncodeError):
        lone_surrogate.encode("utf-8")

    _rejects(successful_document(ansible_output=lone_surrogate))


def test_the_byte_budget_covers_the_display_output_too() -> None:
    """Genel byte bütçesi ``ansible_output``'u da kapsar; ölçüm belge geneline aittir."""
    small = successful_document(ansible_output="ok")
    budget = len(
        json.dumps(small, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    )

    assert parse(small, max_result_bytes=budget).ansible_output == "ok"
    _rejects(successful_document(ansible_output="ok" + "x" * 100), max_result_bytes=budget)


def test_the_display_output_never_enters_the_result_repr() -> None:
    """``repr`` ham metni basmaz: tek bir log satırı onu dışarı taşırdı."""
    parsed = parse(successful_document(ansible_output=f"{SENTINEL_MARKER}-DISPLAY-BODY"))

    assert parsed.ansible_output is not None
    assert SENTINEL_MARKER not in repr(parsed)
    assert "ansible_output" not in repr(parsed)
    assert repr(parsed) == "PlaybookJobResult(outcome='successful', hosts=1, events=2)"


def test_every_output_violation_falls_into_the_same_fixed_contract() -> None:
    """Output ihlallerinin hepsi diğer ihlallerle **ayırt edilemez** cevap üretir."""
    violations: list[dict[str, Any]] = [
        legacy_document(ansible_output=None),
        legacy_document(ansible_output_truncated=False),
        successful_document(ansible_output=7),
        successful_document(ansible_output_truncated=1),
        successful_document(ansible_output="a" * (MAX_ANSIBLE_OUTPUT_BYTES + 1)),
        successful_document(schema_version=3),
    ]

    responses = set()
    for document in violations:
        error = _rejects(document)
        assert isinstance(error.details, dict)
        responses.add(
            (error.status_code, error.code, error.message, tuple(sorted(error.details.items())))
        )
    # Referans hata da aynı kümeye düşer: ayrım yapan tek bir cevap yoktur.
    reference = _reference_error()
    assert isinstance(reference.details, dict)
    responses.add(
        (
            reference.status_code,
            reference.code,
            reference.message,
            tuple(sorted(reference.details.items())),
        )
    )
    assert len(responses) == 1


def test_a_real_version_two_normalize_document_round_trips_with_its_output() -> None:
    """Yazan tarafın **gerçek** çıktısı, display metniyle birlikte okunur."""
    run = _normalize(
        _stdout(
            {
                "event": "runner_on_ok",
                "stdout": f"ok: [web-1] => {SENTINEL_MARKER}-DISPLAY-BODY",
                "event_data": {"host": "web-1", "task": "Ping", "res": {"changed": False}},
            },
            _stats(ok={"web-1": 1}, processed={"web-1": 1}),
        ),
        return_code=0,
    )
    document = run.to_document()
    assert document["schema_version"] == SCHEMA_VERSION
    assert set(document) == RESULT_FIELDS_V2

    parsed = parse(document)

    # Ham yüzey gerçekten hamdır: sentinel taşınır, sansürlenmez.
    assert parsed.ansible_output == f"ok: [web-1] => {SENTINEL_MARKER}-DISPLAY-BODY"
    assert parsed.ansible_output_truncated is False
    # Structured yüzey ondan etkilenmez.
    assert parsed.events[0].task == "Ping"
    assert parsed.recap["web-1"].ok == 1
