"""Execution plan ve launch response sözleşmeleri (R1-V1F, R1-V3D1).

Buradaki testler servisi değil **şemayı** ölçer: R1-V1'in değişmez alanları
(``mode``, ``connection``, ``become``, ``executable``, ``binding``,
``not_executable_reason``, ``limit``/``tags``/``skip_tags``) ``Literal`` ile
bağlıdır. Servis bir gün yanlışlıkla gevşerse cevap sessizce API'ye taşınmaz;
serileştirme sınırında hata verir.

R1-V3D1'in launch cevabı aynı yaklaşımı iki alan daha ileri taşır: ``job_id``
UUID4, ``accepted_at`` timezone-aware **ve** UTC olmak zorundadır. İkisi de
sessizce düzeltilmez — yanlış girdi normalize edilseydi, hatalı bir kaynağın
ürettiği kimlik veya zaman çizgisi doğruymuş gibi API'ye geçerdi.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.execution import ExecutionLaunchResponse, ExecutionPlanResponse


def _payload(**overrides: Any) -> dict[str, Any]:
    """Geçerli bir R1-V1 plan cevabı; `overrides` tek alanı bozmak içindir."""
    payload: dict[str, Any] = {
        "project": {"id": 1, "name": "Web"},
        "inventory": {"id": 2, "name": "Prod", "binding": "project"},
        "playbook": {
            "path": "site.yml",
            "name": "site.yml",
            "size_bytes": 240,
            "modified_at": "2026-07-18T17:45:00Z",
        },
        "mode": "check",
        "limit": None,
        "tags": None,
        "skip_tags": None,
        "host_count": 2,
        "hosts": ["db01", "web01"],
        "hosts_truncated": False,
        "connection": "ssh",
        "host_key_policy": "strict",
        "become": False,
        "executable": False,
        "not_executable_reason": "execution_not_enabled",
        "generated_at": "2026-07-28T10:05:00Z",
    }
    payload.update(overrides)
    return payload


def test_valid_plan_is_accepted() -> None:
    """R1-V1 sözleşmesine uyan cevap değişmeden doğrulanır."""
    response = ExecutionPlanResponse.model_validate(_payload())

    assert response.executable is False
    assert response.mode == "check"
    assert response.connection == "ssh"
    assert response.become is False
    assert response.limit is None
    assert response.tags is None
    assert response.skip_tags is None
    assert response.inventory.binding == "project"
    assert response.not_executable_reason == "execution_not_enabled"


def test_a_normal_mode_plan_is_also_accepted() -> None:
    """R1-V3H2A: ``normal`` da geçerli bir plan kipidir, yalnız ``check`` değil."""
    response = ExecutionPlanResponse.model_validate(_payload(mode="normal"))

    assert response.mode == "normal"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # Çalıştırılabilir bir plan bu dilimde üretilemez: Kapı C hâlâ OPEN.
        ("executable", True),
        # `mode` artık `check`/`normal` ikisini de taşıyabilir (R1-V3H2A);
        # yalnız enum dışı bir değer reddedilir.
        ("mode", "diff"),
        ("connection", "local"),
        ("become", True),
        ("not_executable_reason", "ready"),
        # Kapsam dışı parametreler plana geri sızamaz.
        ("limit", "web01"),
        ("tags", "deploy"),
        ("skip_tags", "slow"),
    ],
)
def test_contract_field_cannot_be_loosened(field: str, value: Any) -> None:
    """Sabit alanın değeri değişirse cevap üretilemez."""
    with pytest.raises(ValidationError) as exc_info:
        ExecutionPlanResponse.model_validate(_payload(**{field: value}))

    assert exc_info.value.error_count() == 1
    assert exc_info.value.errors()[0]["loc"] == (field,)


def test_inventory_binding_cannot_be_standalone() -> None:
    """Standalone inventory plana giremez; şema da bunu kabul etmez."""
    payload = _payload(inventory={"id": 2, "name": "Prod", "binding": "standalone"})

    with pytest.raises(ValidationError) as exc_info:
        ExecutionPlanResponse.model_validate(payload)

    assert exc_info.value.errors()[0]["loc"] == ("inventory", "binding")


# --- Launch cevabı (R1-V3D1) --------------------------------------------------


JOB_ID = "6f1c0f6e-3d2b-4a9c-8f5e-2b7d1c4a9e30"


def _launch_payload(**overrides: Any) -> dict[str, Any]:
    """Geçerli bir R1-V3D1 launch cevabı; `overrides` tek alanı bozmak içindir."""
    payload: dict[str, Any] = {
        "job_id": JOB_ID,
        "job_type": "playbook",
        "initial_status": "pending",
        "mode": "check",
        "project_id": 1,
        "inventory_id": 3,
        "playbook_path": "site.yml",
        "accepted_at": "2026-08-17T10:05:00Z",
    }
    payload.update(overrides)
    return payload


def test_valid_launch_response_is_accepted_and_stays_canonical() -> None:
    """Sözleşmeye uyan cevap doğrulanır ve JSON'da canonical lowercase kalır.

    Kimliğin tipe bağlanması serileştirmeyi değiştirmemelidir: istemcinin gördüğü
    değer yine düz bir UUID string'idir, bir nesne veya farklı bir gösterim değil.
    """
    response = ExecutionLaunchResponse.model_validate(_launch_payload())

    assert response.job_type == "playbook"
    assert response.initial_status == "pending"
    assert response.mode == "check"
    assert response.job_id.version == 4
    assert response.accepted_at == datetime(2026, 8, 17, 10, 5, tzinfo=UTC)


def test_a_normal_mode_launch_response_is_also_accepted() -> None:
    """R1-V3H2A: launch cevabı da ``normal`` kipi taşıyabilir."""
    response = ExecutionLaunchResponse.model_validate(_launch_payload(mode="normal"))

    assert response.mode == "normal"

    dumped = response.model_dump(mode="json")
    assert dumped["job_id"] == JOB_ID
    assert dumped["job_id"] == JOB_ID.lower()
    assert dumped["accepted_at"].startswith("2026-08-17T10:05:00")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_type", "ping"),
        ("initial_status", "running"),
        ("initial_status", "successful"),
        # `mode` artık `check`/`normal` ikisini de taşıyabilir (R1-V3H2A);
        # yalnız enum dışı bir değer reddedilir.
        ("mode", "diff"),
    ],
)
def test_launch_contract_field_cannot_be_loosened(field: str, value: Any) -> None:
    """Literal alanlar bu dilimde de bağlayıcıdır.

    ``initial_status`` özellikle ölçülür: alan "Job **şu durumda oluşturuldu**"
    der. ``running`` veya ``successful`` yazılabilseydi, cevap tutamayacağı bir
    güncellik sözü verirdi.
    """
    with pytest.raises(ValidationError) as exc_info:
        ExecutionLaunchResponse.model_validate(_launch_payload(**{field: value}))

    assert exc_info.value.error_count() == 1
    assert exc_info.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize(
    "job_id",
    [
        # Biçimsiz.
        "job-1",
        "",
        "6f1c0f6e3d2b4a9c8f5e2b7d1c4a9e3",
        # Doğru biçim, yanlış sürüm: UUID1 zaman ve MAC adresinden türer, yani
        # tahmin edilebilir ve controller'ın donanımını sızdırabilir.
        "a8098c1a-f86e-11da-bd1a-00112444be1e",
    ],
)
def test_job_id_must_be_a_uuid4(job_id: str) -> None:
    """Kimlik yalnız UUID4'tür; biçimsiz veya başka sürüm kabul edilmez."""
    with pytest.raises(ValidationError) as exc_info:
        ExecutionLaunchResponse.model_validate(_launch_payload(job_id=job_id))

    assert exc_info.value.errors()[0]["loc"] == ("job_id",)


def test_a_generated_uuid1_is_refused_even_as_an_object() -> None:
    """Kontrol string ayrıştırmasına değil **sürüme** bakar.

    Hazır bir ``uuid.UUID`` nesnesi geçirmek doğrulamayı atlatmamalıdır; aksi
    hâlde kural yalnızca JSON gövdesinden gelen değerler için geçerli olurdu.
    """
    generated = uuid.uuid1()
    assert generated.version == 1

    with pytest.raises(ValidationError) as exc_info:
        ExecutionLaunchResponse.model_validate(_launch_payload(job_id=generated))

    assert exc_info.value.errors()[0]["loc"] == ("job_id",)


@pytest.mark.parametrize(
    "accepted_at",
    [
        # Naive: sunucunun yerel saatini UTC saymak zaman çizgisini kaydırırdı.
        "2026-08-17T10:05:00",
        datetime(2026, 8, 17, 10, 5),
        # UTC dışı offset: sessizce çevrilseydi yanlış kaynak doğru görünürdü.
        "2026-08-17T10:05:00+03:00",
        datetime(2026, 8, 17, 10, 5, tzinfo=timezone(timedelta(hours=3))),
        datetime(2026, 8, 17, 10, 5, tzinfo=timezone(timedelta(hours=-5))),
    ],
)
def test_accepted_at_must_be_utc(accepted_at: Any) -> None:
    """Naive ve UTC dışı damga **normalize edilmez**, reddedilir."""
    with pytest.raises(ValidationError) as exc_info:
        ExecutionLaunchResponse.model_validate(_launch_payload(accepted_at=accepted_at))

    assert exc_info.value.errors()[0]["loc"] == ("accepted_at",)


@pytest.mark.parametrize(
    "accepted_at",
    [
        "2026-08-17T10:05:00Z",
        "2026-08-17T10:05:00+00:00",
        datetime(2026, 8, 17, 10, 5, tzinfo=UTC),
        datetime(2026, 8, 17, 10, 5, tzinfo=timezone(timedelta(0))),
    ],
)
def test_utc_is_accepted_however_it_is_written(accepted_at: Any) -> None:
    """``Z``, ``+00:00`` ve sıfır offset'li tzinfo aynı anı tarif eder."""
    response = ExecutionLaunchResponse.model_validate(_launch_payload(accepted_at=accepted_at))

    assert response.accepted_at == datetime(2026, 8, 17, 10, 5, tzinfo=UTC)
    assert response.accepted_at.utcoffset() == timedelta(0)
