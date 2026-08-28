"""Job okuma cevabı sözleşmeleri (R1-V3D2A1).

Buradaki testler servisi değil **şemayı** ölçer. Şemalar bu turda bir route'a
bağlı değildir; yine de sözleşmenin doğruluk kaynağı serileştirme sınırıdır:
:mod:`app.services.execution.read` bir gün gevşerse cevap sessizce API'ye
taşınmamalı, doğrulama sırasında düşmelidir.

Dört kısıt fail-closed ölçülür:

1. ``extra="forbid"`` — sözleşmede olmayan hiçbir alan geçemez. Fail-open bir
   şema, servise sonradan eklenen ``requested_by`` veya ``artifact_path``
   alanını hiç kimse fark etmeden dışarı taşırdı.
2. ``Literal`` — ``job_type``, ``mode``, ``status`` ve ``error_code`` sabit
   kümelerden gelir; serbest metin bir hata kodu buradan geçemez.
3. ``UUID4`` — kimlik canonical UUID4'tür ve JSON'da küçük harfli kalır.
4. UTC — zaman damgaları timezone-aware **ve** UTC olmak zorundadır; naive ya
   da kaydırılmış bir damga sessizce düzeltilmez, düşer.

R1-V3D2A2A ile aynı dosyada **sonuç** şemaları da ölçülür. Orada beşinci bir
kısıt vardır: ``StrictInt``/``StrictBool``/``StrictStr``. Pydantic'in lax kipi
``1``'i ``True``, ``"3"``'ü ``3`` sayardı ve parser'ın bilinçle reddettiği
``bool``-as-``int`` karışıklığı serileştirme sınırından geri girerdi. Sonuç
testleri ayrıca iki tanımın — parser'ın runtime frozenset'leri ile şemanın
``Literal``'ları — ayrışamayacağını sabitler.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.schemas.job import (
    PlaybookHostRecapResponse,
    PlaybookJobCursorResponse,
    PlaybookJobListResponse,
    PlaybookJobResultResponse,
    PlaybookJobSummaryResponse,
    PlaybookResultEventResponse,
)
from app.services.execution.result import (
    EVENT_FIELDS,
    RECAP_FIELDS,
    RESULT_ERROR_CODES,
    RESULT_EVENT_TYPES,
    RESULT_FIELDS_V1,
    RESULT_FIELDS_V2,
    RESULT_OUTCOMES,
    parse_playbook_result,
)

JOB_ID = "9f2c4b1e-1111-4222-8333-444455556666"
CURSOR_ID = "0a1b2c3d-4444-4555-8666-777788889999"


def _summary(**overrides: Any) -> dict[str, Any]:
    """Geçerli bir Job özeti; ``overrides`` tek alanı bozmak içindir."""
    payload: dict[str, Any] = {
        "job_id": JOB_ID,
        "job_type": "playbook",
        "status": "successful",
        "mode": "check",
        "project_id": 1,
        "project_name": "Web",
        "inventory_id": 2,
        "inventory_name": "Prod",
        "playbook_path": "site.yml",
        "return_code": 0,
        "error_code": None,
        "result_truncated": False,
        "has_recorded_result": True,
        "created_at": "2026-08-17T12:00:00Z",
        "started_at": "2026-08-17T12:00:05Z",
        "finished_at": "2026-08-17T12:01:00Z",
    }
    payload.update(overrides)
    return payload


def _page(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "items": [_summary()],
        "has_more": True,
        "next_cursor": {"created_at": "2026-08-17T12:00:00Z", "job_id": CURSOR_ID},
    }
    payload.update(overrides)
    return payload


# --- Mutlu yol ----------------------------------------------------------------


def test_a_valid_summary_is_accepted() -> None:
    """Sözleşmeye uyan özet değişmeden doğrulanır."""
    response = PlaybookJobSummaryResponse.model_validate(_summary())

    assert str(response.job_id) == JOB_ID
    assert response.job_type == "playbook"
    assert response.mode == "check"
    assert response.status == "successful"
    assert response.error_code is None
    assert response.has_recorded_result is True
    assert response.created_at == datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def test_a_normal_mode_summary_is_also_accepted() -> None:
    """R1-V3H2A: özet artık ``normal`` kipi de taşıyabilir, yalnız ``check`` değil."""
    response = PlaybookJobSummaryResponse.model_validate(_summary(mode="normal"))

    assert response.mode == "normal"


def test_a_valid_page_is_accepted() -> None:
    """Liste cevabı, öğe listesi ve cursor'ıyla birlikte doğrulanır."""
    response = PlaybookJobListResponse.model_validate(_page())

    assert len(response.items) == 1
    assert response.has_more is True
    assert response.next_cursor is not None
    assert str(response.next_cursor.job_id) == CURSOR_ID


def test_an_empty_page_carries_no_cursor() -> None:
    """Boş sayfa geçerlidir ve devam işareti taşımaz."""
    response = PlaybookJobListResponse.model_validate(
        {"items": [], "has_more": False, "next_cursor": None}
    )

    assert response.items == []
    assert response.has_more is False
    assert response.next_cursor is None


def test_uuids_stay_canonical_lowercase_in_json() -> None:
    """JSON çıktısında kimlikler canonical **küçük harfli** dizgidir.

    Pydantic ``UUID4``'ü Python nesnesi olarak tutar; JSON'a çıkarken biçimin
    değişmemesi sözleşmenin parçasıdır — istemci aynı dizgiyi cursor olarak geri
    gönderebilmelidir.
    """
    rendered = PlaybookJobListResponse.model_validate(_page()).model_dump(mode="json")

    assert rendered["items"][0]["job_id"] == JOB_ID
    assert rendered["next_cursor"]["job_id"] == CURSOR_ID
    assert JOB_ID.lower() == JOB_ID


def test_timestamps_are_serialized_as_utc() -> None:
    """Zaman damgaları UTC olarak çıkar; yerel saat üretilmez."""
    rendered = PlaybookJobSummaryResponse.model_validate(_summary()).model_dump(mode="json")

    for field in ("created_at", "started_at", "finished_at"):
        assert rendered[field].endswith("Z") or rendered[field].endswith("+00:00"), field


def test_genuinely_optional_fields_accept_null() -> None:
    """Başlamamış/bitmemiş bir iş temsil edilebilir.

    Nullable olan alanlar gerçekten belirsiz olabilenlerdir: henüz başlamamış
    bir işin ``started_at``'i, bitmemiş bir işin ``finished_at``'i ve
    ``return_code``'u, ``failed`` olmayan bir satırın ``error_code``'u.
    ``project_id`` ve ``playbook_path`` bu kümede **değildir** (bkz. aşağıdaki
    test).
    """
    response = PlaybookJobSummaryResponse.model_validate(
        _summary(
            status="pending",
            return_code=None,
            error_code=None,
            started_at=None,
            finished_at=None,
            has_recorded_result=False,
        )
    )

    assert response.started_at is None
    assert response.finished_at is None
    assert response.return_code is None
    assert response.error_code is None
    assert response.project_id == 1
    assert response.playbook_path == "site.yml"


@pytest.mark.parametrize("field", ["project_id", "playbook_path", "project_name", "inventory_name"])
def test_the_binding_backed_fields_reject_null(field: str) -> None:
    """``project_id`` ve ``playbook_path`` ``null`` olamaz (R1-V3D2A1F).

    Veritabanı sütunları nullable'dır ama okuma sorgusu ikisini de onay
    biletinin ``NOT NULL`` alanına eşitler; ``NULL`` taşıyan bir satır o bağı
    sağlayamaz ve zaten hiç okunmaz. Şemanın onları ``| None`` bırakması,
    gerçekte oluşamayan bir durumu sözleşmeye yazmak ve her istemciyi ona karşı
    dallanmaya zorlamak olurdu — ayrıca bağın gevşemesi hâlinde eksik bir kaydın
    sessizce dışarı çıkmasına izin verirdi.

    ``project_name`` ve ``inventory_name`` (R1-V3J0B2) aynı gerekçeye tabidir:
    ``INNER JOIN`` bir eşleşme bulamazsa satırın kendisi hiç dönmez, dolayısıyla
    ``NULL`` bir isim bu sözleşmede temsil edilebilir bir durum **değildir**.
    """
    with pytest.raises(ValidationError):
        PlaybookJobSummaryResponse.model_validate(_summary(**{field: None}))


# --- Fail-closed: sabit alanlar -----------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # Bu dilimde başka bir Job türü okunmaz.
        ("job_type", "ping"),
        ("job_type", "PLAYBOOK"),
        # `mode` artık `check`/`normal` ikisini de taşıyabilir (R1-V3H2A);
        # yalnız enum dışı bir değer reddedilir.
        ("mode", "diff"),
        # Durum sabit bir kümeden gelir; yeni bir üye kendiliğinden geçemez.
        ("status", "timeout"),
        ("status", "SUCCESSFUL"),
        ("status", "queued"),
    ],
)
def test_a_value_outside_the_literal_set_is_refused(field: str, value: str) -> None:
    """``Literal`` dışı değer cevaba taşınmaz, serileştirme sınırında düşer."""
    with pytest.raises(ValidationError):
        PlaybookJobSummaryResponse.model_validate(_summary(**{field: value}))


@pytest.mark.parametrize(
    "error_code",
    [
        "runner_failed",
        "playbook_failed",
        "runner_timeout",
        "workspace_integrity_failed",
        "execution_binding_invalid",
        "interrupted_by_restart",
        "unknown_failure",
        None,
    ],
)
def test_every_public_error_code_is_accepted(error_code: str | None) -> None:
    """Allowlist'teki her kod (ve ``None``) geçerlidir."""
    response = PlaybookJobSummaryResponse.model_validate(
        _summary(status="failed", error_code=error_code, return_code=2)
    )

    assert response.error_code == error_code


@pytest.mark.parametrize(
    "error_code",
    [
        "",
        "beklenmedik_kod",
        "RUNNER_FAILED",
        "/srv/app-data/execution-plans/9f2c/project/site.yml bulunamadı",
        "OperationalError: no such column: aselai_secret_token",
        "ssh: connect to host 10.0.0.10 port 22: Connection refused",
    ],
)
def test_a_free_text_error_code_is_refused(error_code: str) -> None:
    """Serbest metin bir hata kodu şemadan geçemez.

    ``Job.error_code`` veritabanı tarafında serbest bir ``String(64)``'tür:
    doğrudan yazılmış bir satır oraya bir workspace yolu, bir token parçası veya
    bir exception metni koyabilir. Servis bunu ``unknown_failure``'a daraltır;
    burası o daraltmanın ikinci savunmasıdır.
    """
    with pytest.raises(ValidationError):
        PlaybookJobSummaryResponse.model_validate(_summary(status="failed", error_code=error_code))


# --- Fail-closed: kimlik ------------------------------------------------------


@pytest.mark.parametrize(
    "job_id",
    [
        "",
        "kisa",
        "../../etc/hosts",
        # UUID1 zaman ve MAC taşır; kimlik uzayı tahmin edilebilir hâle gelirdi.
        "9f2c4b1e-1111-1222-8333-444455556666",
    ],
)
def test_a_non_uuid4_identifier_is_refused(job_id: str) -> None:
    """Kimlik canonical UUID4 olmak zorundadır; kural şemada da durur."""
    with pytest.raises(ValidationError):
        PlaybookJobSummaryResponse.model_validate(_summary(job_id=job_id))
    with pytest.raises(ValidationError):
        PlaybookJobCursorResponse.model_validate(
            {"created_at": "2026-08-17T12:00:00Z", "job_id": job_id}
        )


@pytest.mark.parametrize(
    "written",
    [
        "9F2C4B1E-1111-4222-8333-444455556666",
        "{9f2c4b1e-1111-4222-8333-444455556666}",
        "9f2c4b1e11114222833344445555 6666".replace(" ", ""),
    ],
)
def test_a_non_canonical_uuid4_spelling_is_normalized_not_echoed(written: str) -> None:
    """Büyük harfli, süslü parantezli ve tiresiz yazım **canonical**'a düşer.

    Şema burada reddetmez, normalize eder — ve asıl güvence budur: JSON'a çıkan
    dizgi her koşulda küçük harfli canonical biçimdir, dolayısıyla aynı Job iki
    farklı yazımla iki farklı kimlik gibi görünemez. Girdinin **kendisinin**
    canonical olması ayrı bir sözleşmedir ve servis katmanında durur
    (:func:`app.services.execution.read.get_playbook_job` biçimsiz kimliği
    SQL'e hiç ulaştırmadan reddeder).
    """
    response = PlaybookJobSummaryResponse.model_validate(_summary(job_id=written))

    assert response.model_dump(mode="json")["job_id"] == JOB_ID
    assert written not in response.model_dump_json() or written == JOB_ID


def test_a_uuid1_cursor_identifier_is_refused() -> None:
    """Cursor kimliği de aynı kısıttadır."""
    with pytest.raises(ValidationError):
        PlaybookJobCursorResponse.model_validate(
            {"created_at": "2026-08-17T12:00:00Z", "job_id": str(uuid.uuid1())}
        )


# --- Fail-closed: zaman -------------------------------------------------------


@pytest.mark.parametrize("field", ["created_at", "started_at", "finished_at"])
def test_a_naive_timestamp_is_refused(field: str) -> None:
    """Naive damga UTC sayılmaz: sunucunun yerel saatini UTC ilan etmek olurdu."""
    with pytest.raises(ValidationError):
        PlaybookJobSummaryResponse.model_validate(_summary(**{field: "2026-08-17T12:00:00"}))


@pytest.mark.parametrize("field", ["created_at", "started_at", "finished_at"])
def test_a_non_utc_timestamp_is_refused(field: str) -> None:
    """``+03:00`` taşıyan bir damga sessizce çevrilmez, **düşer**.

    Sessiz dönüştürme yanlış bir kaynağın ürettiği damgayı doğruymuş gibi
    gösterirdi; zaman çizgisinin tek doğru cevabı kalmazdı.
    """
    shifted = datetime(2026, 8, 17, 15, 0, tzinfo=timezone(timedelta(hours=3)))
    with pytest.raises(ValidationError):
        PlaybookJobSummaryResponse.model_validate(_summary(**{field: shifted}))


def test_a_non_utc_cursor_timestamp_is_refused() -> None:
    """Cursor zamanı da UTC olmak zorundadır."""
    with pytest.raises(ValidationError):
        PlaybookJobCursorResponse.model_validate(
            {
                "created_at": datetime(2026, 8, 17, 15, 0, tzinfo=timezone(timedelta(hours=3))),
                "job_id": CURSOR_ID,
            }
        )


# --- Fail-closed: fazladan alan -----------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "requested_by",
        "actor",
        "execution_plan_id",
        "plan_id",
        "artifact_path",
        "worker_id",
        "heartbeat_at",
        "lease_expires_at",
        "workspace_id",
        "manifest_digest",
        "plan_token",
        "token_hash",
        "limit_pattern",
        "environment",
        "argv",
        "private_key",
        # Join yalnız isimleri dışarı çıkarır (R1-V3J0B2); Project/Inventory'nin
        # path ve description'ı görünmez.
        "project_path",
        "inventory_path",
        "project_description",
    ],
)
def test_an_extra_field_is_refused_by_the_summary(field: str) -> None:
    """``extra="forbid"``: sözleşmede olmayan alan sessizce yok sayılmaz, düşer.

    Yok sayılsaydı ihlal hiç görünmezdi; düştüğü için servise sonradan eklenen
    bir alan **derhâl** ortaya çıkar.
    """
    with pytest.raises(ValidationError):
        PlaybookJobSummaryResponse.model_validate(_summary(**{field: "x"}))


@pytest.mark.parametrize("field", ["total", "offset", "requested_by", "page"])
def test_an_extra_field_is_refused_by_the_list(field: str) -> None:
    """Liste zarfı da dardır: toplam sayı ve offset bilinçli olarak yoktur."""
    with pytest.raises(ValidationError):
        PlaybookJobListResponse.model_validate(_page(**{field: 1}))


def test_an_extra_field_is_refused_by_the_cursor() -> None:
    """Cursor tam olarak iki alandır."""
    with pytest.raises(ValidationError):
        PlaybookJobCursorResponse.model_validate(
            {"created_at": "2026-08-17T12:00:00Z", "job_id": CURSOR_ID, "index": 3}
        )


# --- Fail-closed: eksik alan --------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "job_id",
        "job_type",
        "status",
        "mode",
        "project_id",
        "project_name",
        "inventory_id",
        "inventory_name",
        "playbook_path",
        "return_code",
        "error_code",
        "result_truncated",
        "has_recorded_result",
        "created_at",
        "started_at",
        "finished_at",
    ],
)
def test_every_summary_field_is_required(field: str) -> None:
    """Alanların hiçbirinin varsayılanı yoktur; eksik bir alan sessizce doldurulmaz.

    Varsayılan taşıyan bir alan, servisin onu üretmeyi unutmasını gizlerdi:
    ``has_recorded_result`` için ``False``, ``status`` için bir sabit sessizce
    yanlış bir cevap üretirdi.
    """
    payload = _summary()
    del payload[field]

    with pytest.raises(ValidationError):
        PlaybookJobSummaryResponse.model_validate(payload)


@pytest.mark.parametrize("field", ["items", "has_more", "next_cursor"])
def test_every_list_field_is_required(field: str) -> None:
    """Liste zarfının üç alanı da zorunludur."""
    payload = _page()
    del payload[field]

    with pytest.raises(ValidationError):
        PlaybookJobListResponse.model_validate(payload)


# --- Alan kümesi kilidi -------------------------------------------------------


def test_the_schema_field_sets_are_locked() -> None:
    """Üç şemanın alan kümesi tam eşitlikle sabitlenir."""
    assert set(PlaybookJobSummaryResponse.model_fields) == {
        "job_id",
        "job_type",
        "status",
        "mode",
        "project_id",
        "project_name",
        "inventory_id",
        "inventory_name",
        "playbook_path",
        "return_code",
        "error_code",
        "result_truncated",
        "has_recorded_result",
        "created_at",
        "started_at",
        "finished_at",
    }
    assert set(PlaybookJobCursorResponse.model_fields) == {"created_at", "job_id"}
    assert set(PlaybookJobListResponse.model_fields) == {"items", "has_more", "next_cursor"}


def test_the_module_exports_exactly_the_documented_schemas() -> None:
    """Kapsam kilidi: modülde tam olarak altı cevap şeması vardır.

    R1-V3D2A1'de bu liste üç isimdi ve sonuç şemasının yokluğunu sabitliyordu;
    R1-V3D2A2A onu üç isim **daha** ile genişletir. Liste yine tam eşitlikle
    ölçülür: sessizce eklenen bir şema, ölçülmemiş bir okuma yüzeyinin ilk
    parçası olurdu.
    """
    import app.schemas.job as module

    exported = {
        name for name in vars(module) if name.endswith("Response") and not name.startswith("_")
    }
    assert exported == {
        "PlaybookJobSummaryResponse",
        "PlaybookJobCursorResponse",
        "PlaybookJobListResponse",
        "PlaybookJobResultResponse",
        "PlaybookHostRecapResponse",
        "PlaybookResultEventResponse",
    }


# --- Sonuç sözleşmesi (R1-V3D2A2A) -------------------------------------------


def _result_recap(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": 1,
        "changed": 0,
        "failures": 0,
        "unreachable": 0,
        "skipped": 0,
        "rescued": 0,
        "ignored": 0,
    }
    payload.update(overrides)
    return payload


def _result_event(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event": "runner_on_ok",
        "host": "web-1",
        "task": "Ping",
        "changed": False,
        "failed": False,
    }
    payload.update(overrides)
    return payload


# Cevabın taşıdığı ham display metni. Sansürlenmez; şema onu yalnız ``str`` ya da
# ``None`` olarak doğrular.
DISPLAY_OUTPUT = "TASK [Ping] ****\nok: [web-1]"


def _result(**overrides: Any) -> dict[str, Any]:
    """Geçerli bir ``schema_version=2`` cevabı; ``overrides`` tek alanı bozmak içindir."""
    payload: dict[str, Any] = {
        "schema_version": 2,
        "job_id": JOB_ID,
        "return_code": 0,
        "outcome": "successful",
        "error_code": None,
        "recap": {"web-1": _result_recap()},
        "events": [_result_event()],
        "events_truncated": False,
        "result_truncated": False,
        "ansible_output": DISPLAY_OUTPUT,
        "ansible_output_truncated": False,
    }
    payload.update(overrides)
    return payload


def test_a_valid_result_is_accepted_and_keeps_the_document_shape() -> None:
    """Public JSON şekli, normalize belgesinin şekliyle aynıdır."""
    response = PlaybookJobResultResponse.model_validate(_result())

    assert str(response.job_id) == JOB_ID
    assert response.outcome == "successful"
    assert response.recap["web-1"].ok == 1
    assert response.events[0].event == "runner_on_ok"
    assert response.model_dump(mode="json") == _result()


def test_a_parsed_internal_result_serializes_back_to_its_document() -> None:
    """İç model → cevap → JSON yolu belgeyi **değiştirmeden** taşır.

    Round-trip, iki tarafın gerçekten aynı sözleşmeyi konuştuğunu ölçer: elle
    kurulmuş bir sözlük yalnız şemanın kendi yorumunu ölçerdi.
    """
    document = _result(
        outcome="failed",
        return_code=2,
        error_code="runner_failed",
        recap={"web-1": _result_recap(ok=0, failures=1)},
        events=[_result_event(event="runner_on_failed", failed=True)],
    )
    parsed = parse_playbook_result(
        document, expected_job_id=JOB_ID, max_events=100, max_result_bytes=100_000
    )

    response = PlaybookJobResultResponse.model_validate(parsed)

    assert response.model_dump(mode="json") == document


def test_the_result_schema_field_sets_are_locked() -> None:
    """Üç sonuç şemasının alan kümesi tam eşitlikle sabitlenir."""
    assert set(PlaybookJobResultResponse.model_fields) == {
        "schema_version",
        "job_id",
        "return_code",
        "outcome",
        "error_code",
        "recap",
        "events",
        "events_truncated",
        "result_truncated",
        "ansible_output",
        "ansible_output_truncated",
    }
    assert set(PlaybookHostRecapResponse.model_fields) == {
        "ok",
        "changed",
        "failures",
        "unreachable",
        "skipped",
        "rescued",
        "ignored",
    }
    assert set(PlaybookResultEventResponse.model_fields) == {
        "event",
        "host",
        "task",
        "changed",
        "failed",
    }


def test_the_result_schema_field_sets_match_the_parser() -> None:
    """Şema ile parser'ın alan kümeleri ayrışamaz.

    Cevap **tek** bir şekle sahiptir ve o şekil en geniş sürümün (``2``) alan
    kümesidir. Sürüm 1 artifact'i okunduğunda output alanları ``null``/``false``
    döner; böylece frontend sürüm ayrımını hiç taşımaz.
    """
    assert set(PlaybookJobResultResponse.model_fields) == RESULT_FIELDS_V2
    assert RESULT_FIELDS_V1 < RESULT_FIELDS_V2
    assert set(PlaybookHostRecapResponse.model_fields) == RECAP_FIELDS
    assert set(PlaybookResultEventResponse.model_fields) == EVENT_FIELDS


def test_the_result_literals_match_the_parser_allowlists() -> None:
    """``Literal`` kümeleri parser'ın frozenset'leriyle birebir aynıdır.

    ``Literal`` runtime bir ``frozenset``'ten üretilemez; iki tanım bu yüzden
    ayrı durur ve eşitlikleri burada sabitlenir. Ayrışmaları, parser'ın kabul
    ettiği bir değerin serileştirme sınırında düşmesi (ya da tersi) demek olurdu.
    """
    outcome = PlaybookJobResultResponse.model_fields["outcome"].annotation
    assert RESULT_OUTCOMES == frozenset(get_args(outcome))

    error_code = PlaybookJobResultResponse.model_fields["error_code"].annotation
    assert RESULT_ERROR_CODES == frozenset(get_args(get_args(error_code)[0]))

    event = PlaybookResultEventResponse.model_fields["event"].annotation
    assert RESULT_EVENT_TYPES == frozenset(get_args(event))


@pytest.mark.parametrize("field", sorted(RESULT_FIELDS_V2))
def test_every_result_field_is_required(field: str) -> None:
    """Sonuç alanlarının hiçbirinin varsayılanı yoktur."""
    payload = _result()
    del payload[field]

    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(payload)


@pytest.mark.parametrize("field", sorted(RECAP_FIELDS))
def test_every_recap_counter_is_required(field: str) -> None:
    """Eksik bir sayaç sıfır varsayılmaz."""
    entry = _result_recap()
    del entry[field]

    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(recap={"web-1": entry}))


@pytest.mark.parametrize("field", sorted(EVENT_FIELDS))
def test_every_event_field_is_required(field: str) -> None:
    """Event'in alan kümesi eksik olamaz."""
    entry = _result_event()
    del entry[field]

    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(events=[entry]))


@pytest.mark.parametrize(
    "field",
    ["stdout", "stderr", "event_data", "artifact_path", "workspace_id", "requested_by"],
)
def test_an_extra_top_level_result_field_is_forbidden(field: str) -> None:
    """Sözleşmede olmayan bir alan cevaba giremez."""
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(**{field: "sızıntı"}))


@pytest.mark.parametrize("field", ["dark", "processed", "host", "hostvars"])
def test_an_extra_recap_field_is_forbidden(field: str) -> None:
    """Recap yalnız sayaçlardan oluşur; metin taşıyan bir alan eklenemez."""
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(
            _result(recap={"web-1": _result_recap(**{field: 1})})
        )


@pytest.mark.parametrize("field", ["res", "task_args", "task_path", "stdout", "counter"])
def test_an_extra_event_field_is_forbidden(field: str) -> None:
    """Event'e raw bir alan eklenemez."""
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(events=[_result_event(**{field: "x"})]))


@pytest.mark.parametrize("version", [0, 3, -1, "1", 1.0, True, None])
def test_an_unsupported_schema_version_is_rejected(version: Any) -> None:
    """``schema_version`` yalnız ``1`` veya ``2``'dir; ``True``/``1.0`` geçemez.

    ``Literal[1, 2]`` bunu tek başına sağlamazdı: ``Literal`` şemasına
    ``strict`` uygulanamaz ve lax kipte ``True`` da ``1`` sayılır.
    """
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(schema_version=version))


@pytest.mark.parametrize("version", [1, 2])
def test_both_supported_schema_versions_are_accepted(version: int) -> None:
    """Sürüm 1 artifact'i de sürüm 2 artifact'i de **aynı** cevap şeklini doldurur."""
    response = PlaybookJobResultResponse.model_validate(_result(schema_version=version))

    assert response.schema_version == version


@pytest.mark.parametrize("value", [1, 0, "true", "false", None])
def test_a_non_boolean_flag_is_rejected(value: Any) -> None:
    """``StrictBool``: ``1`` sessizce ``True`` olmaz.

    Lax kipte ``1`` geçerdi ve parser'ın bilinçle reddettiği ``bool``-as-``int``
    karışıklığı serileştirme sınırından geri girerdi.
    """
    for field in ("events_truncated", "result_truncated"):
        with pytest.raises(ValidationError):
            PlaybookJobResultResponse.model_validate(_result(**{field: value}))
    for field in ("changed", "failed"):
        with pytest.raises(ValidationError):
            PlaybookJobResultResponse.model_validate(
                _result(events=[_result_event(**{field: value})])
            )


@pytest.mark.parametrize("value", [True, "0", 1.5, None])
def test_a_non_integer_return_code_is_rejected(value: Any) -> None:
    """``StrictInt``: ``true`` ve ``"0"`` bir çıkış kodu değildir."""
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(return_code=value))


@pytest.mark.parametrize("value", [True, "1", 1.5, None, -1])
def test_a_malformed_recap_counter_is_rejected(value: Any) -> None:
    """Sayaç gerçek, negatif olmayan bir tam sayıdır."""
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(recap={"web-1": _result_recap(ok=value)}))


@pytest.mark.parametrize("value", [1, True, [], {}])
def test_a_non_string_event_text_is_rejected(value: Any) -> None:
    """``StrictStr``: host ve task ya metindir ya ``null``."""
    for field in ("host", "task"):
        with pytest.raises(ValidationError):
            PlaybookJobResultResponse.model_validate(
                _result(events=[_result_event(**{field: value})])
            )


@pytest.mark.parametrize("outcome", ["ok", "success", "SUCCESSFUL", "canceled", None, 1])
def test_an_unknown_outcome_is_rejected(outcome: Any) -> None:
    """``outcome`` sabit iki değerden biridir."""
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(outcome=outcome))


@pytest.mark.parametrize(
    "code", ["runner_start_failed", "workspace_unavailable", "unknown_failure", "boom"]
)
def test_an_error_code_outside_the_result_allowlist_is_rejected(code: str) -> None:
    """Job satırının taşıyabildiği bir kod, sonuç belgesinde geçerli değildir."""
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(outcome="failed", error_code=code))


@pytest.mark.parametrize("name", ["playbook_on_stats", "runner_on_start", "verbose", ""])
def test_an_unknown_event_type_is_rejected(name: str) -> None:
    """Yalnız normalize allowlist'indeki event türleri geçer."""
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(events=[_result_event(event=name)]))


def test_the_result_job_id_must_be_a_canonical_uuid4() -> None:
    """Kimlik UUID4'tür ve JSON'da küçük harfli canonical biçimde kalır."""
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(job_id="not-a-uuid"))
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(
            _result(job_id=str(uuid.UUID("9f2c4b1e-1111-1222-8333-444455556666")))
        )

    response = PlaybookJobResultResponse.model_validate(_result(job_id=JOB_ID.upper()))
    assert response.model_dump(mode="json")["job_id"] == JOB_ID


def test_the_result_recap_and_events_are_the_only_container_surfaces() -> None:
    """``recap``/``events`` dışında serbest bir kap açılmaz.

    İkisi de tarif edilmiş modellerden oluşur; hiçbir alan ``dict[str, Any]``
    değildir ve serbest bir JSON parçası cevaba giremez.
    """
    recap = PlaybookJobResultResponse.model_fields["recap"].annotation
    assert get_args(recap) == (str, PlaybookHostRecapResponse)

    events = PlaybookJobResultResponse.model_fields["events"].annotation
    assert get_args(events) == (PlaybookResultEventResponse,)

    for model in (
        PlaybookJobResultResponse,
        PlaybookHostRecapResponse,
        PlaybookResultEventResponse,
    ):
        for name, info in model.model_fields.items():
            assert info.annotation is not Any, (model.__name__, name)


# --- R1-V3J3A: display output alanları ---------------------------------------


def test_the_display_output_fields_round_trip_untouched() -> None:
    """Ham metin cevapta **değiştirilmeden** taşınır: sansür serileştirmede de yok."""
    document = _result(ansible_output="fatal: [web-1] ansible_password=SENTINEL-PW")

    response = PlaybookJobResultResponse.model_validate(document)

    assert response.ansible_output == "fatal: [web-1] ansible_password=SENTINEL-PW"
    assert response.model_dump(mode="json") == document


def test_a_version_one_artifact_fills_the_output_fields_with_null_and_false() -> None:
    """Sürüm 1 artifact'i tek cevap şeklini bozmaz.

    Frontend sürüm ayrımını taşımaz: alanlar her zaman vardır, sürüm 1'de
    ``null``/``false`` olurlar.
    """
    parsed = parse_playbook_result(
        {
            "schema_version": 1,
            "job_id": JOB_ID,
            "return_code": 0,
            "outcome": "successful",
            "error_code": None,
            "recap": {"web-1": _result_recap()},
            "events": [_result_event()],
            "events_truncated": False,
            "result_truncated": False,
        },
        expected_job_id=JOB_ID,
        max_events=100,
        max_result_bytes=100_000,
    )

    response = PlaybookJobResultResponse.model_validate(parsed)
    body = response.model_dump(mode="json")

    assert body["schema_version"] == 1
    assert body["ansible_output"] is None
    assert body["ansible_output_truncated"] is False
    assert set(body) == set(_result())


@pytest.mark.parametrize("value", [7, 1.5, True, False, ["ok"], {"text": "ok"}])
def test_a_non_string_ansible_output_is_rejected(value: Any) -> None:
    """``StrictStr | None``: sayı da boolean da metin yerine geçemez."""
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(ansible_output=value))


@pytest.mark.parametrize("value", [1, 0, "true", "false", None])
def test_a_non_boolean_output_truncated_flag_is_rejected(value: Any) -> None:
    """``StrictBool``: ``1`` sessizce ``True`` olmaz."""
    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(_result(ansible_output_truncated=value))


@pytest.mark.parametrize("field", ["ansible_output", "ansible_output_truncated"])
def test_a_missing_output_field_is_rejected(field: str) -> None:
    """Output alanlarının da varsayılanı yoktur."""
    payload = _result()
    del payload[field]

    with pytest.raises(ValidationError):
        PlaybookJobResultResponse.model_validate(payload)


def test_the_output_fields_never_enter_the_job_list_or_detail_schemas() -> None:
    """Ham çıktı **yalnız** sonuç cevabındadır; özet yüzeylerine giremez."""
    for model in (PlaybookJobSummaryResponse, PlaybookJobListResponse):
        assert "ansible_output" not in model.model_fields
        assert "ansible_output_truncated" not in model.model_fields

    with pytest.raises(ValidationError):
        PlaybookJobSummaryResponse.model_validate(_summary(ansible_output="sızıntı"))


def test_the_job_and_result_schemas_are_bound_to_their_routes(client: TestClient) -> None:
    """Kapsam kilidi: bu dosyanın şemaları artık R1-V3D2B route'larına bağlıdır.

    ``GET /api/jobs``, ``GET /api/jobs/{job_id}`` ve
    ``GET /api/jobs/{job_id}/result`` bağlandı; üçü dışında Job'ı okuyan veya
    değiştiren fazladan bir yol yoktur.

    Toplam operasyon sayısı **21**'dir. 21. operasyon R1-V3J1'in eklediği
    ``GET /api/inventories/{inventory_id}/ping-runs``'dır; R1-V3J3A hiçbir route
    eklemedi, yalnız mevcut sonuç cevabını genişletti.
    """
    spec = client.get("/openapi.json").json()

    job_paths = {path for path in spec["paths"] if path.startswith("/api/jobs")}
    assert job_paths == {"/api/jobs", "/api/jobs/{job_id}", "/api/jobs/{job_id}/result"}
    for path in job_paths:
        assert set(spec["paths"][path]) == {"get"}
    assert sum(len(operations) for operations in spec["paths"].values()) == 21
    for name in (
        "PlaybookJobResultResponse",
        "PlaybookHostRecapResponse",
        "PlaybookResultEventResponse",
        "PlaybookJobSummaryResponse",
        "PlaybookJobListResponse",
        "PlaybookJobCursorResponse",
    ):
        assert name in spec.get("components", {}).get("schemas", {}), name
