"""`POST /api/projects/{id}/executions` sözleşmesi (R1-V3D1).

Bu, hazırlanmış bir plan token'ını tüketen **ilk public HTTP yüzeyidir**. Ölçülen
iddialar, bir çalıştırma kapısının açılırken kaybetmemesi gereken şeylerdir:

1. *İstek yalnız rezervasyon yapar.* Runner çağrılmaz, alt süreç açılmaz, SSH
   bağlantısı kurulmaz, artifact üretilmez ve worker'a bakılmaz. ``201``
   "Job kalıcı olarak oluşturuldu" demektir, "execution başladı" değil; cevaptaki
   alanın adı da bu yüzden ``status`` değil ``initial_status``'tur.
2. *Atomiklik.* Bir token'ın tek geçerli sonucu **bir** Job'dır: aynı token
   ikinci kez tüketilemez ve ikinci bir aktif PLAYBOOK Job'ı hiç doğmaz.
3. *Yanlış bağlam token yakmaz.* Başka project, inventory, playbook, aktör veya
   host key politikasıyla gelen istek hiçbir satırı eşleştirmez; kullanıcının
   elindeki geçerli bilet yanlış bir denemeyle kaybolmaz.
4. *Tek kod.* Bilinmeyen, biçimsiz, süresi geçmiş, yanlış bağlamlı ve içeriği
   değişmiş plan aynı generic ``execution_plan_invalid``'i döndürür; hangi
   kontrolün takıldığı dışarıdan görünmez.
5. *Sızdırmazlık.* Ne cevap ne de hata token'ı, aktörü, plan/workspace kimliğini,
   digest'i, absolute path'i, environment'ı, argv'yi veya artifact yolunu taşır.
6. *Dar gövde.* İstemci çalıştırma parametresi, aktör veya politika gönderemez.
"""

from __future__ import annotations

import ast
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select, text
from sqlalchemy.orm import Session

from app.api.routes import executions as executions_route
from app.core.config import Settings
from app.models import (
    ExecutionMode,
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    Job,
    JobStatus,
    JobType,
)
from app.services.execution import workspace as ws
from app.services.execution.authorize import AuthorizedPlaybookJob
from tests.support import stub_parser_command

PLAYBOOK = "---\n- name: Ornek\n  hosts: all\n"
OTHER_PLAYBOOK = "---\n- name: Web\n  hosts: web\n"
INVENTORY_TEXT = "[web]\nweb01\n"

SIMPLE_OUTPUT: dict[str, Any] = {
    "_meta": {"hostvars": {"web01": {"ansible_host": "10.0.0.10"}}},
    "all": {"children": ["ungrouped", "web"]},
    "web": {"hosts": ["web01"]},
}

# Cevabın **tam** alan kümesi. Eşitlikle ölçülür: sessizce eklenen bir alan
# (plan kimliği, workspace, digest, aktör, artifact yolu) testi düşürür.
SAFE_RESPONSE_FIELDS = {
    "job_id",
    "job_type",
    "initial_status",
    "mode",
    "project_id",
    "inventory_id",
    "playbook_path",
    "accepted_at",
}

# İstemcinin **hiçbir koşulda** gönderemeyeceği alanlar (`extra="forbid"`).
#
# `mode` R1-V3H2A ile bu kümenin dışına çıkmıştır: istemci artık *beklenen*
# kipi söyleyebilir (fingerprint ve claim koşuluna girer), ama Job'a yazılan
# değeri değil.
FORBIDDEN_REQUEST_FIELDS = (
    "requested_by",
    "fingerprint",
    "host_key_policy",
    "connection",
    "become",
    "limit",
    "tags",
    "skip_tags",
    "extra_vars",
)

pytestmark = pytest.mark.skipif(
    not ws.secure_filesystem_available(),
    reason="Descriptor-relative dosya sistemi primitive'leri bu platformda yok (ADR-017).",
)


# --- Yardımcılar --------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _use_stub(settings: Settings, tmp_path: Path, payload: dict[str, Any]) -> None:
    target = tmp_path / "payload.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    settings.ansible_inventory_command = stub_parser_command("payload", payload=str(target))


def _create_project(client: TestClient, path: Path, name: str = "Web") -> int:
    path.mkdir(parents=True, exist_ok=True)
    response = client.post("/api/projects", json={"name": name, "path": str(path)})
    assert response.status_code == 201, response.text
    project_id: int = response.json()["id"]
    return project_id


def _create_linked_inventory(
    client: TestClient, target: Path, project_id: int, name: str = "Prod"
) -> int:
    response = client.post(
        "/api/inventories",
        json={
            "name": name,
            "path": str(target),
            "source_type": "ini",
            "project_id": project_id,
        },
    )
    assert response.status_code == 201, response.text
    inventory_id: int = response.json()["id"]
    return inventory_id


def _create_standalone_inventory(client: TestClient, target: Path) -> int:
    """Hiçbir project'e bağlı olmayan inventory (ADR-015 ayrımı)."""
    response = client.post(
        "/api/inventories",
        json={"name": "Serbest", "path": str(target), "source_type": "ini"},
    )
    assert response.status_code == 201, response.text
    inventory_id: int = response.json()["id"]
    return inventory_id


def _prepare(client: TestClient, project_id: int, **payload: Any) -> httpx.Response:
    """``mode`` çağıran tarafından verilmezse ``check`` varsayılır (R1-V3H2A)."""
    payload.setdefault("mode", "check")
    return cast(
        httpx.Response,
        client.post(f"/api/projects/{project_id}/execution-plans", json=payload),
    )


def _launch(client: TestClient, project_id: int, **payload: Any) -> httpx.Response:
    """``mode`` çağıran tarafından verilmezse ``check`` varsayılır (R1-V3H2A)."""
    payload.setdefault("mode", "check")
    return cast(
        httpx.Response,
        client.post(f"/api/projects/{project_id}/executions", json=payload),
    )


def _prepared_token(
    client: TestClient,
    project_id: int,
    inventory_id: int,
    playbook_path: str = "site.yml",
    mode: str = "check",
) -> str:
    response = _prepare(
        client, project_id, mode=mode, inventory_id=inventory_id, playbook_path=playbook_path
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["plan_token"]
    return token


def _jobs(engine: Engine) -> list[Job]:
    with Session(engine, expire_on_commit=False) as session:
        return list(session.execute(select(Job)).scalars().all())


def _plans(engine: Engine) -> list[ExecutionPlanRecord]:
    with Session(engine, expire_on_commit=False) as session:
        return list(session.execute(select(ExecutionPlanRecord)).scalars().all())


def _plan_status(engine: Engine, plan_id: str) -> ExecutionPlanStatus:
    with Session(engine, expire_on_commit=False) as session:
        record = session.get(ExecutionPlanRecord, plan_id)
        assert record is not None
        return record.status


def _finish_job_successfully(engine: Engine, job_id: str) -> None:
    """Bir Job'ı **test kurulumu olarak** terminal ``successful`` yapar.

    Doğrudan yazılır çünkü ölçülen şey durum makinesi değil, terminal bir satırın
    aktif PLAYBOOK sınırını serbest bırakmasıdır. Satır yine de şemanın istediği
    biçimde bırakılır: ``started_at``/``finished_at`` dolar, ``return_code`` 0
    olur ve worker sahiplik alanları boş kalır
    (``ck_jobs_idle_playbook_has_no_lease``).
    """
    moment = datetime.now(UTC)
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = JobStatus.SUCCESSFUL
        job.started_at = moment - timedelta(seconds=1)
        job.finished_at = moment
        job.return_code = 0
        session.commit()


def _assert_invalid_plan(response: httpx.Response, *, token: str) -> None:
    """409, tek kod, sabit ``details`` ve ham token'ın cevapta hiç geçmemesi."""
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "execution_plan_invalid"
    assert error["details"] == {"reason": "invalid"}
    assert token not in response.text
    assert token[:8] not in response.text


@pytest.fixture
def project_dir(project_root: Path) -> Path:
    directory = project_root / "proje"
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / "site.yml", PLAYBOOK)
    _write(directory / "playbooks" / "web.yml", OTHER_PLAYBOOK)
    _write(directory / "inventories" / "production.ini", INVENTORY_TEXT)
    return directory


@pytest.fixture
def launch_context(
    client: TestClient, project_dir: Path, tmp_path: Path, settings: Settings
) -> tuple[int, int]:
    project_id = _create_project(client, project_dir)
    inventory_id = _create_linked_inventory(
        client, project_dir / "inventories" / "production.ini", project_id
    )
    _use_stub(settings, tmp_path, SIMPLE_OUTPUT)
    return project_id, inventory_id


# --- Mutlu yol ----------------------------------------------------------------


def test_prepared_plan_is_launched_into_a_single_pending_job(
    client: TestClient,
    launch_context: tuple[int, int],
    migrated_engine: Engine,
    settings: Settings,
) -> None:
    """Hazırla → çalıştırmaya al: 201, tam cevap sözleşmesi ve tek ``pending`` Job.

    Cevabın alanları istek gövdesinden **kopyalanmaz**, claim edilen plandan
    üretilir; ölçüm bu yüzden hem cevabı hem de veritabanı satırını karşılaştırır.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)
    before = datetime.now(UTC)

    response = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == SAFE_RESPONSE_FIELDS
    assert body["job_type"] == "playbook"
    # "Job şu durumda oluşturuldu" der; "şu an bu durumda" demez.
    assert body["initial_status"] == "pending"
    assert body["mode"] == "check"
    assert body["project_id"] == project_id
    assert body["inventory_id"] == inventory_id
    assert body["playbook_path"] == "site.yml"
    # Kimlik canonical UUID4'tür; Job satırının kimliğiyle birebir aynıdır.
    parsed = uuid.UUID(body["job_id"])
    assert parsed.version == 4
    assert str(parsed) == body["job_id"]

    accepted_at = datetime.fromisoformat(body["accepted_at"])
    assert accepted_at.tzinfo is not None
    assert before - timedelta(seconds=5) <= accepted_at <= datetime.now(UTC) + timedelta(seconds=5)

    # Tek kullanımlık sır taşıyan istek hiçbir ara katmanda saklanmamalıdır.
    assert response.headers["Cache-Control"] == "no-store"

    jobs = _jobs(migrated_engine)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == body["job_id"]
    assert job.job_type is JobType.PLAYBOOK
    assert job.status is JobStatus.PENDING
    assert job.project_id == project_id
    assert job.inventory_id == inventory_id
    assert job.playbook_path == "site.yml"
    # Aktör istekten değil sunucu ayarından gelir ve cevaba çıkmaz.
    assert job.requested_by == settings.local_actor
    assert job.limit_pattern is None

    # Plan tüketildi ve Job ona bağlandı.
    plans = _plans(migrated_engine)
    assert len(plans) == 1
    assert plans[0].status is ExecutionPlanStatus.CLAIMED
    assert job.execution_plan_id == plans[0].id


def test_response_leaks_no_token_actor_path_or_execution_detail(
    client: TestClient,
    launch_context: tuple[int, int],
    migrated_engine: Engine,
    settings: Settings,
    project_dir: Path,
) -> None:
    """Cevap ne sırrı ne de controller dosya sistemini tarif eder.

    Ölçüm iki yönlüdür: yasak **alan adları** ve yasak **değerler**. Yalnız alan
    adına bakmak, sızıntının başka bir alanın içine gömülmesini kaçırırdı.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)

    response = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    plan = _plans(migrated_engine)[0]

    assert set(body) == SAFE_RESPONSE_FIELDS
    for forbidden in (
        "plan_token",
        "token",
        "requested_by",
        "actor",
        "plan_id",
        "execution_plan_id",
        "workspace_id",
        "manifest_digest",
        "artifact_path",
        "environment",
        "argv",
        "command",
        "private_key",
        "worker_id",
        "lease_expires_at",
    ):
        assert forbidden not in body

    rendered = response.text
    for secret in (
        token,
        token[:8],
        plan.id,
        plan.workspace_id,
        plan.manifest_digest,
        settings.local_actor,
        str(project_dir),
        str(settings.app_data_dir),
        str(settings.resolve_execution_plan_dir()),
    ):
        assert secret not in rendered, secret


# --- Atomiklik ve tekrar kullanım ---------------------------------------------


def test_the_same_token_cannot_be_launched_twice(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine
) -> None:
    """Bir token'ın tek geçerli sonucu **bir** Job'dır.

    İkinci istek 409 alır ve ikinci bir Job doğmaz: tek kullanım garantisi
    HTTP yüzeyinde de durur.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)
    payload = {
        "plan_token": token,
        "inventory_id": inventory_id,
        "playbook_path": "site.yml",
    }

    first = _launch(client, project_id, **payload)
    assert first.status_code == 201, first.text

    second = _launch(client, project_id, **payload)

    _assert_invalid_plan(second, token=token)
    jobs = _jobs(migrated_engine)
    assert [job.id for job in jobs] == [first.json()["job_id"]]


def test_a_second_active_playbook_job_is_refused_without_burning_the_token(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine
) -> None:
    """Global aktif PLAYBOOK sınırı (1) HTTP yüzeyinden de aşılamaz.

    Sınırın doğruluk kaynağı kısmi unique index'tir (``uq_jobs_active_playbook_
    global``), süreç içi bir sayaç değil: iki backend süreci böyle bir sayacı
    paylaşmazdı. İhlal veritabanı sınırında doğar ve public-safe bir arıza
    cevabına daraltılır.

    Asıl iddia reddetmenin kendisi değil, **biletin hayatta kalmasıdır**. "Plan
    hâlâ ``prepared``" demek yeterli bir kanıt değildir: satır doğru görünse de
    token'ın gerçekten claim edilebilir kaldığını ancak yeniden kullanmak
    gösterir. Bu yüzden ilk Job terminal yapılır ve **aynı** ikinci token'la
    yeniden denenir; bu kez 201 gelmelidir. Aksi hâlde kullanıcı, sırf zamanlama
    yüzünden yeniden hazırlamaya zorlanırdı.
    """
    project_id, inventory_id = launch_context
    first_token = _prepared_token(client, project_id, inventory_id)
    second_token = _prepared_token(client, project_id, inventory_id)
    assert first_token != second_token

    accepted = _launch(
        client,
        project_id,
        plan_token=first_token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert accepted.status_code == 201, accepted.text

    refused = _launch(
        client,
        project_id,
        plan_token=second_token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )

    assert refused.status_code == 503, refused.text
    error = refused.json()["error"]
    assert error["code"] == "execution_launch_unavailable"
    assert error["details"] == {"reason": "unavailable"}
    # Veritabanı metni, index adı ve token dışarı çıkmaz.
    for secret in (second_token, second_token[:8], "uq_jobs_active_playbook_global", "UNIQUE"):
        assert secret not in refused.text, secret

    first_job_id = accepted.json()["job_id"]
    assert [job.id for job in _jobs(migrated_engine)] == [first_job_id]
    # İkinci bilet yanmadı: hâlâ `prepared`.
    statuses = {plan.status for plan in _plans(migrated_engine)}
    assert statuses == {ExecutionPlanStatus.CLAIMED, ExecutionPlanStatus.PREPARED}

    # İlk Job terminal yapılır. Yalnız test kurulumudur: production davranışı,
    # yeni bir kilit veya semafor eklenmez. Satır DB invariantlarına uygun
    # bırakılır — `ck_jobs_idle_playbook_has_no_lease` terminal bir PLAYBOOK
    # satırında worker/heartbeat/lease alanlarının **boş** olmasını ister, yani
    # biten iş kimseye ait değildir.
    _finish_job_successfully(migrated_engine, first_job_id)

    # Kritik adım: **aynı** ikinci token, hiçbir yeniden hazırlama olmadan.
    retried = _launch(
        client,
        project_id,
        plan_token=second_token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )

    assert retried.status_code == 201, retried.text
    second_job_id = retried.json()["job_id"]
    assert second_job_id != first_job_id
    assert retried.json()["initial_status"] == "pending"

    jobs = {job.id: job for job in _jobs(migrated_engine)}
    assert set(jobs) == {first_job_id, second_job_id}
    assert jobs[first_job_id].status is JobStatus.SUCCESSFUL
    assert jobs[second_job_id].status is JobStatus.PENDING
    assert jobs[second_job_id].job_type is JobType.PLAYBOOK
    # Aktif PLAYBOOK sayısı yine bir: terminal satır sınırı işgal etmez.
    active = [job for job in jobs.values() if job.status in (JobStatus.PENDING, JobStatus.RUNNING)]
    assert [job.id for job in active] == [second_job_id]

    # Her iki bilet de tam olarak birer Job'a dönüştü.
    plans = {plan.id: plan for plan in _plans(migrated_engine)}
    assert len(plans) == 2
    assert {plan.status for plan in plans.values()} == {ExecutionPlanStatus.CLAIMED}
    assert {job.execution_plan_id for job in jobs.values()} == set(plans)


# --- Generic reddetme ---------------------------------------------------------


def test_unknown_and_malformed_tokens_return_the_same_generic_error(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine
) -> None:
    """Bilinmeyen ile biçimsiz token ayırt edilemez.

    Şemanın tam token regex'ini doğrulamaması bilinçlidir: doğrulasaydı biçimsiz
    bir token 422, bilinmeyen bir token 409 alırdı ve token'ın **biçimi** bir
    yan kanal olurdu. Makul uzunluktaki her değer aynı 409'u alır.
    """
    project_id, inventory_id = launch_context
    unknown = "A" * 43
    candidates = (
        unknown,
        "kisa",
        "biçimsiz token!!",
        "../../etc/hosts",
        "a" * 120,
    )

    for candidate in candidates:
        response = _launch(
            client,
            project_id,
            plan_token=candidate,
            inventory_id=inventory_id,
            playbook_path="site.yml",
        )
        _assert_invalid_plan(response, token=candidate)

    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine) == []


def test_an_expired_token_is_refused_and_creates_no_job(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine
) -> None:
    """TTL'i geçmiş bilet claim edilemez; ayrı bir kod da almaz."""
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)

    # Kayıt geçmişe taşınır. `created_at` de kaydırılır: şemadaki
    # `ck_execution_plans_expiry_after_creation` yalnız `expires_at`'i geriye
    # çekmeye izin vermez ve haklıdır — TTL'i olmayan bir plan hiç var olmamalı.
    with Session(migrated_engine) as session:
        plan = session.execute(select(ExecutionPlanRecord)).scalar_one()
        plan.created_at = datetime.now(UTC) - timedelta(hours=2)
        plan.expires_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()

    response = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )

    _assert_invalid_plan(response, token=token)
    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED


def test_a_wrong_context_does_not_consume_the_token(
    client: TestClient,
    launch_context: tuple[int, int],
    project_root: Path,
    migrated_engine: Engine,
) -> None:
    """Yanlış project, inventory veya playbook hiçbir satırı eşleştirmez.

    Asıl iddia reddetmenin kendisi değil, **token'ın hayatta kalmasıdır**:
    kullanıcının elindeki geçerli bilet yanlış bir denemeyle kaybolmamalıdır.
    Bu yüzden her yanlış denemeden sonra doğru bağlamla tek bir Job üretilebildiği
    de ölçülür.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)

    other_root = project_root / "diger"
    _write(other_root / "inventories" / "production.ini", INVENTORY_TEXT)
    other_project_id = _create_project(client, other_root, name="Diger")
    other_inventory_id = _create_linked_inventory(
        client, other_root / "inventories" / "production.ini", other_project_id, name="Diger"
    )

    wrong_attempts: tuple[tuple[int, dict[str, Any]], ...] = (
        # Başka project (path parametresi).
        (other_project_id, {"inventory_id": inventory_id, "playbook_path": "site.yml"}),
        # Başka inventory.
        (project_id, {"inventory_id": other_inventory_id, "playbook_path": "site.yml"}),
        # Onaylananın dışında bir playbook.
        (project_id, {"inventory_id": inventory_id, "playbook_path": "playbooks/web.yml"}),
    )
    for target_project, payload in wrong_attempts:
        response = _launch(client, target_project, plan_token=token, **payload)
        _assert_invalid_plan(response, token=token)
        assert _jobs(migrated_engine) == []
        assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED

    accepted = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert accepted.status_code == 201, accepted.text
    assert len(_jobs(migrated_engine)) == 1


def test_a_standalone_inventory_cannot_be_substituted(
    client: TestClient,
    launch_context: tuple[int, int],
    inventory_root: Path,
    migrated_engine: Engine,
) -> None:
    """Project'e bağlı olmayan bir inventory ile çalıştırma açıkça reddedilir.

    Standalone inventory hiçbir project'e bağlı değildir; onu bir project planına
    iliştirmek, onaylanan hedef kümesinin yerine başka bir kümeyi koymak olurdu.
    Reddetme claim koşulunda gerçekleşir: token **tüketilmez** ve doğru
    inventory ile hâlâ kullanılabilir.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)
    standalone = inventory_root / "serbest.ini"
    standalone.write_text(INVENTORY_TEXT, encoding="utf-8")
    standalone_id = _create_standalone_inventory(client, standalone)

    response = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=standalone_id,
        playbook_path="site.yml",
    )

    _assert_invalid_plan(response, token=token)
    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED

    accepted = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert accepted.status_code == 201, accepted.text
    assert len(_jobs(migrated_engine)) == 1


@pytest.mark.parametrize(
    "playbook_path",
    ["../../etc/hosts", "playbooks/web.yml", "/etc/hosts", "site.yml\n"],
)
def test_a_different_playbook_path_creates_no_job_and_keeps_the_token(
    client: TestClient,
    launch_context: tuple[int, int],
    migrated_engine: Engine,
    playbook_path: str,
) -> None:
    """Onaylanandan farklı her yol reddedilir; traversal denemesi de dâhil.

    Yol burada dosya sistemi olarak **çözülmez**: claim koşulu onaylanan metnin
    kendisiyle karşılaştırır. Bu yüzden ``../../etc/hosts`` ile ``site.yml\\n``
    aynı sonucu verir — biri "tehlikeli", diğeri "masum" sayılmaz.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)

    response = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path=playbook_path,
    )

    _assert_invalid_plan(response, token=token)
    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED

    accepted = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert accepted.status_code == 201, accepted.text


# --- Kip seçimi (R1-V3H2A) -----------------------------------------------------


@pytest.mark.parametrize("mode", ["check", "normal"])
def test_check_and_normal_prepare_then_launch_happy_paths(
    client: TestClient,
    launch_context: tuple[int, int],
    migrated_engine: Engine,
    mode: str,
) -> None:
    """Her iki kip de prepare → launch zincirini uçtan uca tamamlar.

    Ölçülen: launch cevabındaki ``mode``, claim edilen plan kaydı ve Job
    satırının kipi **aynı** değeri taşır — istekten kopyalanan bir değer değil.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id, mode=mode)

    response = _launch(
        client,
        project_id,
        plan_token=token,
        mode=mode,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )

    assert response.status_code == 201, response.text
    assert response.json()["mode"] == mode

    plan = _plans(migrated_engine)[0]
    assert plan.mode.value == mode
    assert plan.status is ExecutionPlanStatus.CLAIMED
    job = _jobs(migrated_engine)[0]
    assert job.mode.value == mode


def test_launch_response_mode_comes_from_the_claimed_plan_not_the_request_body(
    client: TestClient,
    launch_context: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route ``mode=payload.mode`` yazarsa bu test **kırmızı** olmalıdır (R1-V3H2A-AUDIT-FIX1).

    Gerçek claim zincirinde istek ve plan kaydının kipi zaten eşit olmak
    zorundadır (yanlış kiple gelen istek hiç claim edilemez), bu yüzden
    mutlu yol testleri tek başına response'un **hangi** alandan geldiğini
    ayırt edemez. Bu test ayrımı zorlar: route'un çağırdığı
    :func:`~app.services.execution.launch.launch_prepared_playbook_job`
    sahte bir servisle değiştirilir ve sahte servis, gerçek bir zincirde asla
    üretilemeyecek bir cevap verir — istek ``check`` derken claim edilen plan
    kaydı ``normal`` taşır. İki ayrı sözleşme birlikte kanıtlanır:

    1. İstek gövdesindeki ``mode``, alt servise **beklenen** kip olarak gider
       (``kwargs["mode"] is ExecutionMode.CHECK`` doğrulaması).
    2. HTTP cevabındaki ``mode``, istekten değil dönen
       ``AuthorizedPlaybookJob.mode``'dan gelir.
    """
    project_id, inventory_id = launch_context
    seen: list[dict[str, Any]] = []
    job_id = str(uuid.uuid4())
    claimed_at = datetime.now(UTC)

    def _fake_launch(session: Any, **kwargs: Any) -> AuthorizedPlaybookJob:
        seen.append(kwargs)
        assert kwargs["mode"] is ExecutionMode.CHECK
        return AuthorizedPlaybookJob(
            job_id=job_id,
            plan_id=str(uuid.uuid4()),
            workspace_id=str(uuid.uuid4()),
            manifest_digest="a" * 64,
            project_id=project_id,
            inventory_id=inventory_id,
            playbook_path="site.yml",
            requested_by="yerel-operator",
            mode=ExecutionMode.NORMAL,
            claimed_at=claimed_at,
        )

    monkeypatch.setattr(executions_route, "launch_prepared_playbook_job", _fake_launch)

    response = _launch(
        client,
        project_id,
        plan_token="A" * 43,
        mode="check",
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )

    assert response.status_code == 201, response.text
    assert len(seen) == 1
    assert response.json()["mode"] == "normal"


def test_check_prepared_token_refuses_a_normal_launch_without_burning_it(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine
) -> None:
    """``check`` için hazırlanmış bilet, ``normal`` launch isteğiyle eşleşmez.

    Kip yükseltme mümkün değildir: yanlış kiple gelen istek generic
    ``execution_plan_invalid`` alır, token tüketilmez ve doğru kiple sonraki
    deneme başarılı olur.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id, mode="check")

    mismatched = _launch(
        client,
        project_id,
        plan_token=token,
        mode="normal",
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )

    _assert_invalid_plan(mismatched, token=token)
    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED

    accepted = _launch(
        client,
        project_id,
        plan_token=token,
        mode="check",
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["mode"] == "check"


def test_normal_prepared_token_refuses_a_check_launch_without_burning_it(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine
) -> None:
    """``normal`` için hazırlanmış bilet, ``check`` launch isteğiyle eşleşmez.

    Yükseltmenin tersi de aynı biçimde engellenir: kip düşürme de mümkün
    değildir ve token yine yakılmaz.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id, mode="normal")

    mismatched = _launch(
        client,
        project_id,
        plan_token=token,
        mode="check",
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )

    _assert_invalid_plan(mismatched, token=token)
    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED

    accepted = _launch(
        client,
        project_id,
        plan_token=token,
        mode="normal",
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["mode"] == "normal"


def test_mode_is_a_required_field(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine
) -> None:
    """``mode`` verilmezse istek domain katmanına ulaşmadan 422 alır."""
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)

    response = client.post(
        f"/api/projects/{project_id}/executions",
        json={"plan_token": token, "inventory_id": inventory_id, "playbook_path": "site.yml"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"
    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED


@pytest.mark.parametrize(
    "mode",
    [None, "", "   ", "Check", "CHECK", "Normal", "check ", "dry-run", "diff", 123, True],
)
def test_invalid_mode_values_are_rejected(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine, mode: object
) -> None:
    """Bilinmeyen, boş, whitespace'li veya farklı case bir ``mode`` 422 alır."""
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)

    response = client.post(
        f"/api/projects/{project_id}/executions",
        json={
            "plan_token": token,
            "mode": mode,
            "inventory_id": inventory_id,
            "playbook_path": "site.yml",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"
    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED


@pytest.mark.parametrize(
    ("setting_name", "changed_value"),
    [("local_actor", "baska-operator"), ("ssh_host_key_policy", "accept_new")],
)
def test_server_side_context_drift_refuses_the_launch(
    client: TestClient,
    launch_context: tuple[int, int],
    settings: Settings,
    migrated_engine: Engine,
    setting_name: str,
    changed_value: str,
) -> None:
    """Aktör veya host key politikası hazırlama ile çalıştırma arasında değişirse 409.

    İkisi de claim koşulundadır (politika girdi özeti üzerinden). Ayrışma
    "kullanıcının onayladığı koşullar artık geçerli değil" demektir ve fail-closed
    davranılır. Token yine **tüketilmez**: eski değer geri geldiğinde aynı bilet
    çalışır.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)
    original = getattr(settings, setting_name)
    payload = {
        "plan_token": token,
        "inventory_id": inventory_id,
        "playbook_path": "site.yml",
    }

    setattr(settings, setting_name, changed_value)
    try:
        response = _launch(client, project_id, **payload)
    finally:
        setattr(settings, setting_name, original)

    _assert_invalid_plan(response, token=token)
    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED

    accepted = _launch(client, project_id, **payload)
    assert accepted.status_code == 201, accepted.text
    assert len(_jobs(migrated_engine)) == 1


def test_tampered_frozen_content_expires_the_plan_and_creates_no_job(
    client: TestClient,
    launch_context: tuple[int, int],
    settings: Settings,
    migrated_engine: Engine,
) -> None:
    """İçeriği değişmiş dondurulmuş workspace fail-closed reddedilir.

    Burada token **bilerek yakılır**: içeriği değişmiş bir workspace'in planı
    artık kullanıcının onayladığı planı temsil etmez ve bileti yeniden claim
    edilebilir bırakmak, saldırganın içeriği değiştirip yeniden denemesine kapı
    açardı. Bu yüzden Job oluşmaz, plan ``expired`` olur ve token bir daha
    kullanılamaz.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)
    plan = _plans(migrated_engine)[0]
    frozen_playbook = (
        settings.resolve_execution_plan_dir() / plan.workspace_id / "project" / "site.yml"
    )
    frozen_playbook.write_text(PLAYBOOK + "# sonradan eklendi\n", encoding="utf-8")

    response = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )

    _assert_invalid_plan(response, token=token)
    assert _jobs(migrated_engine) == []
    assert _plan_status(migrated_engine, plan.id) is ExecutionPlanStatus.EXPIRED

    # Aynı token bir daha kullanılamaz ve ayırt edilebilir bir kod da almaz.
    again = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    _assert_invalid_plan(again, token=token)
    assert _jobs(migrated_engine) == []


# --- Arıza sınırı -------------------------------------------------------------


INJECTED_MARKER = "aselai_injected_launch_failure"


def _is_claim_update(statement: str) -> bool:
    """Yalnız plan claim UPDATE'ini tanır; Job INSERT'i veya SELECT'i değil."""
    normalized = " ".join(statement.split()).lower()
    return (
        normalized.startswith("update execution_plans")
        and "claimed_at" in normalized
        and "token_hash" in normalized
    )


def test_a_database_failure_becomes_a_public_safe_503(
    client: TestClient,
    launch_context: tuple[int, int],
    migrated_engine: Engine,
    settings: Settings,
) -> None:
    """Façade'ın ``ExecutionLaunchUnavailableError``'ı API'de 503 olur.

    Arıza taklit **edilmez**: listener yalnız claim ifadesini bozar, hatayı gerçek
    sqlite cursor'ı üretir. Ölçülen, cevabın veritabanı metnini, token'ı veya
    controller path'ini taşımaması ve planın ``prepared`` kalmasıdır — geçici bir
    arıza kullanıcının biletini yakmamalıdır.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)
    plan = _plans(migrated_engine)[0]
    targeted: list[str] = []

    def _break_claim_update(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> tuple[str, Any]:
        if _is_claim_update(statement):
            targeted.append(statement)
            return f"UPDATE execution_plans SET {INJECTED_MARKER} = 1", parameters
        return statement, parameters

    event.listen(migrated_engine, "before_cursor_execute", _break_claim_update, retval=True)
    try:
        response = _launch(
            client,
            project_id,
            plan_token=token,
            inventory_id=inventory_id,
            playbook_path="site.yml",
        )
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _break_claim_update)

    assert len(targeted) == 1, targeted
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "execution_launch_unavailable"
    assert error["details"] == {"reason": "unavailable"}
    for secret in (
        token,
        token[:8],
        INJECTED_MARKER,
        "execution_plans",
        "OperationalError",
        plan.workspace_id,
        plan.manifest_digest,
        str(settings.app_data_dir),
    ):
        assert secret not in response.text, secret

    # Bilet yanmadı: aynı token, arıza kalktıktan sonra tam bir Job üretir.
    assert _jobs(migrated_engine) == []
    assert _plan_status(migrated_engine, plan.id) is ExecutionPlanStatus.PREPARED
    accepted = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert accepted.status_code == 201, accepted.text
    assert len(_jobs(migrated_engine)) == 1


# --- Dar istek gövdesi --------------------------------------------------------


@pytest.mark.parametrize("field", FORBIDDEN_REQUEST_FIELDS)
def test_execution_parameters_are_refused_by_the_request_schema(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine, field: str
) -> None:
    """İstemci aktör, politika veya çalıştırma parametresi gönderemez.

    ``extra="forbid"`` olmasaydı bu alanlar sessizce yok sayılırdı ve "gönderdim,
    kabul edildi" ile "gönderdim, atıldı" arasındaki fark kullanıcıya hiç
    görünmezdi. Ölçüm ayrıca doğrulama ayrıntılarının **ham token'ı geri
    yansıtmadığını** da kanıtlar.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)

    response = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
        **{field: "x"},
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "request_validation_error"
    # Alan **adı** görünür (hangi alanın reddedildiği bilinmelidir); değer değil.
    assert field in json.dumps(error["details"])
    assert token not in response.text
    assert token[:8] not in response.text

    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED


def test_oversized_token_is_refused_without_echoing_it(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine
) -> None:
    """Aşırı uzun token 422 alır ama cevaba **yazılmaz**.

    Pydantic'in ``input`` alanı ham değeri geri yansıtırdı; hata zarfı onu
    bilinçli olarak atar (``_SAFE_VALIDATION_FIELDS``).
    """
    project_id, inventory_id = launch_context
    oversized = "T" * 5000

    response = _launch(
        client,
        project_id,
        plan_token=oversized,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_error"
    assert oversized not in response.text
    assert oversized[:64] not in response.text
    assert _jobs(migrated_engine) == []


@pytest.mark.parametrize("inventory_id", [0, -1])
def test_inventory_id_must_be_positive(
    client: TestClient, launch_context: tuple[int, int], inventory_id: int
) -> None:
    """``inventory_id >= 1``; sıfır ve negatif değer domain katmanına ulaşmaz."""
    project_id, _ = launch_context
    response = _launch(
        client,
        project_id,
        plan_token="A" * 43,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert response.status_code == 422, response.text


def test_the_token_is_never_accepted_from_the_url_or_query_string(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine
) -> None:
    """Token yalnız gövdededir: query string'e konan bir token işe yaramaz.

    Proxy ve erişim log'ları URL'leri kaydeder; token'ın oradan kabul edilmesi
    onu kalıcı olarak sızdırırdı.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)

    response = cast(
        httpx.Response,
        client.post(
            f"/api/projects/{project_id}/executions?plan_token={token}",
            json={"inventory_id": inventory_id, "playbook_path": "site.yml"},
        ),
    )

    assert response.status_code == 422, response.text
    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED


@pytest.mark.parametrize("header", ["X-Plan-Token", "Authorization", "X-Execution-Plan-Token"])
def test_the_token_is_never_accepted_from_a_header(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine, header: str
) -> None:
    """Header'daki token da okunmaz: gövde eksik kaldığı için istek 422 alır.

    Query string'den ayrı ölçülür çünkü savunma farklıdır. Query string yanlışlıkla
    **loglanır**; bir header ise "hazır bir kimlik kanalı" gibi görünür ve ileride
    sessizce okunmaya başlanabilirdi. Ölçüm, token'ın hangi kanaldan gelirse
    gelsin yalnız gövdeden alındığını sabitler: reddedilme sebebi header'ın
    yanlış olması değil, ``plan_token``'ın gövdede **hiç bulunmamasıdır**.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)

    response = cast(
        httpx.Response,
        client.post(
            f"/api/projects/{project_id}/executions",
            json={"inventory_id": inventory_id, "playbook_path": "site.yml"},
            headers={header: token},
        ),
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "request_validation_error"
    # Eksik olan tam olarak gövdedeki `plan_token`'dır.
    locations = [tuple(item["loc"]) for item in error["details"]]
    assert ("body", "plan_token") in locations
    # Hata ham token'ı geri yansıtmaz.
    assert token not in response.text
    assert token[:8] not in response.text

    assert _jobs(migrated_engine) == []
    assert _plans(migrated_engine)[0].status is ExecutionPlanStatus.PREPARED

    # Bilet yanmadı: doğru gövdeyle aynı token tam bir Job üretir.
    accepted = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert accepted.status_code == 201, accepted.text
    assert len(_jobs(migrated_engine)) == 1


# --- Kapsam kilidi ------------------------------------------------------------


def test_the_route_reserves_a_job_without_touching_the_execution_layers(
    client: TestClient,
    launch_context: tuple[int, int],
    settings: Settings,
    migrated_engine: Engine,
) -> None:
    """İstek runner, alt süreç, SSH veya artifact üretmez; yalnız satır yazar.

    Ölçüm iki katmanlıdır: route modülünün **gerçek import listesi** (docstring'de
    geçen bir modül adı testi ne geçirir ne düşürür) ve isteğin diskte/veritabanında
    bıraktığı iz. Job ``pending`` doğar: worker alanları boştur, artifact yolu
    yoktur ve hiçbir run dizini açılmamıştır.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)

    response = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert response.status_code == 201, response.text

    job = _jobs(migrated_engine)[0]
    assert job.status is JobStatus.PENDING
    assert job.artifact_path is None
    assert job.started_at is None
    assert job.finished_at is None
    assert job.return_code is None
    assert job.error_code is None
    assert job.worker_id is None
    assert job.heartbeat_at is None
    assert job.lease_expires_at is None

    # Runner çalışma alanı hiç açılmadı.
    assert (
        not settings.resolve_execution_run_dir().exists()
        or list(settings.resolve_execution_run_dir().iterdir()) == []
    )
    # Artifact kökünde bu Job'a ait bir dizin yok.
    artifacts = settings.app_data_dir / "artifacts"
    if artifacts.exists():
        assert job.id not in {entry.name for entry in artifacts.iterdir()}

    # Route'un import yüzeyi bir sözleşmedir ve **tam eşitlikle** ölçülür.
    tree = ast.parse(Path("app/api/routes/executions.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    assert imported == {
        "__future__",
        # Yalnız `str` → `UUID4` dönüşümü için; kimlik burada **üretilmez**.
        "uuid",
        "typing",
        "fastapi",
        "sqlalchemy.orm",
        "app.core.config",
        "app.db.session",
        "app.schemas.execution",
        "app.services.execution",
        "app.services.inventories",
        "app.services.projects",
    }
    for forbidden in (
        "subprocess",
        "ansible_runner",
        "threading",
        "app.services.execution.executor",
        "app.services.execution.worker",
        "app.services.execution.runner_process",
        "app.services.jobs.artifacts",
    ):
        assert forbidden not in imported, forbidden


def test_openapi_exposes_exactly_one_launch_contract(client: TestClient) -> None:
    """OpenAPI: tek launch yolu, tek token şeması, korunan eski sözleşmeler.

    Spec burada bir belge değil bir **kilit** olarak okunur: token kabul eden
    ikinci bir yol ya da hazırlama gövdesine sızmış bir çalıştırma parametresi
    testi düşürür.
    """
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]

    launch_path = "/api/projects/{project_id}/executions"
    assert {path for path in spec["paths"] if "execution" in path} == {
        "/api/projects/{project_id}/execution-plan",
        "/api/projects/{project_id}/execution-plans",
        launch_path,
    }
    assert set(spec["paths"][launch_path]) == {"post"}
    assert set(spec["paths"][launch_path]["post"]["responses"]) >= {"201"}

    def _request_fields(path: str, method: str = "post") -> set[str]:
        content = spec["paths"][path][method]["requestBody"]["content"]
        reference = content["application/json"]["schema"]["$ref"]
        name = reference.rsplit("/", 1)[-1]
        return set(schemas[name]["properties"])

    # Launch gövdesi tam olarak dört alandır (R1-V3H2A: `mode` eklendi).
    assert _request_fields(launch_path) == {"plan_token", "mode", "inventory_id", "playbook_path"}

    # Token kabul eden **tek** istek şeması budur.
    token_operations = {
        (path, method)
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if isinstance(operation, dict)
        for media in operation.get("requestBody", {}).get("content", {}).values()
        if "plan_token"
        in schemas.get(media.get("schema", {}).get("$ref", "").rsplit("/", 1)[-1], {}).get(
            "properties", {}
        )
    }
    assert token_operations == {(launch_path, "post")}

    # Aktör, özet ve çalıştırma parametreleri hiçbir **execution** istek
    # şemasında yoktur. Ölçüm execution yüzeyiyle sınırlıdır: ping'in kendi
    # onay akışı kendi `limit` alanını taşır ve bu dilimin kapsamı değildir.
    execution_request_fields: set[str] = set()
    for path in spec["paths"]:
        if "execution" not in path:
            continue
        for operation in spec["paths"][path].values():
            if not isinstance(operation, dict):
                continue
            for media in operation.get("requestBody", {}).get("content", {}).values():
                name = media.get("schema", {}).get("$ref", "").rsplit("/", 1)[-1]
                execution_request_fields.update(schemas.get(name, {}).get("properties", {}))
    assert execution_request_fields == {"plan_token", "mode", "inventory_id", "playbook_path"}
    for forbidden in FORBIDDEN_REQUEST_FIELDS:
        assert forbidden not in execution_request_fields, forbidden

    # Eski sözleşmeler bozulmadı; `mode` R1-V3H2A ile üçüne de eklendi.
    for path in (
        "/api/projects/{project_id}/execution-plan",
        "/api/projects/{project_id}/execution-plans",
    ):
        assert _request_fields(path) == {"mode", "inventory_id", "playbook_path"}
    assert schemas["ExecutionPlanResponse"]["properties"]["executable"]["const"] is False

    # Cevap şeması da dar: literal alanlar ve güvenli alanlar dışında hiçbir şey.
    launch_response = schemas["ExecutionLaunchResponse"]
    assert set(launch_response["properties"]) == SAFE_RESPONSE_FIELDS
    assert launch_response["properties"]["job_type"]["const"] == "playbook"
    assert launch_response["properties"]["initial_status"]["const"] == "pending"
    # `mode` R1-V3H2A ile artık sabit değildir: `ExecutionMode` enum'una
    # referans verir ve yalnız `check`/`normal` değerlerini taşıyabilir.
    assert launch_response["properties"]["mode"]["$ref"] == "#/components/schemas/ExecutionMode"
    assert schemas["ExecutionMode"]["enum"] == ["check", "normal"]


def test_no_job_mutation_endpoint_exists_besides_launch(client: TestClient) -> None:
    """Kapsam kilidi: Job'ı **okuyan** yüzey (R1-V3D2B) GET'e sınırlıdır.

    Çalıştırmaya alma açıldı; Job listesi/detayı/sonucu (R1-V3D2A1, R1-V3D2A2B2,
    R1-V3D2B) da artık GET olarak bağlıdır. İptal ve UI hâlâ açılmadı; Job'ı
    okuyan hiçbir yol GET dışında bir metod kabul etmez.
    """
    spec = client.get("/openapi.json").json()
    for path in spec["paths"]:
        assert "cancel" not in path or "ping" in path, path
        if path.startswith("/api/jobs"):
            assert set(spec["paths"][path]) == {"get"}, path
    # Launch yolunda yalnız POST vardır; GET ile Job okunamaz.
    assert set(spec["paths"]["/api/projects/{project_id}/executions"]) == {"post"}


def test_launch_does_not_change_worker_defaults(settings: Settings) -> None:
    """Endpoint worker'ı açmaz ve varsayılanını değiştirmez.

    ``201`` "Job kuyruğa alındı" demektir. Worker kapalıyken de aynı cevabı
    vermek bilinçlidir: istek, çalıştırmanın **başladığını** değil işin kalıcı
    olarak kaydedildiğini bildirir.
    """
    assert settings.playbook_worker_enabled is False


def test_a_launched_job_is_visible_only_in_the_database(
    client: TestClient, launch_context: tuple[int, int], migrated_engine: Engine
) -> None:
    """Job satırı gerçekten kalıcıdır: ayrı bir bağlantı onu görür.

    İddia "cevap 201 döndü"nün ötesine geçer: aynı transaction içinde kalmış,
    commit edilmemiş bir satır bu sorguda görünmezdi.
    """
    project_id, inventory_id = launch_context
    token = _prepared_token(client, project_id, inventory_id)

    response = _launch(
        client,
        project_id,
        plan_token=token,
        inventory_id=inventory_id,
        playbook_path="site.yml",
    )
    assert response.status_code == 201, response.text

    with migrated_engine.connect() as connection:
        rows = connection.execute(text("SELECT id, job_type, status FROM jobs")).all()
    assert rows == [(response.json()["job_id"], "playbook", "pending")]
