"""Job geçmişinin gerçek TTL temizliğinden sonra da hayatta kaldığı (R1-V3J5).

Kök neden: :func:`~app.services.execution.store.sweep_expired_plans` bir
kaydı, ona bağlı aktif (``pending``/``running``) bir PLAYBOOK Job'ı yoksa,
süresi geçtiğinde ``expired`` yapar — ``claimed_at``'e hiç dokunmaz. Job ve
onun ``result.json`` artifact'i silinmez; yalnız plan satırı işaretlenir. Eski
:func:`~app.services.execution.read._authorized_statement` yalnızca
``status == claimed`` arardı ve bu yüzden **daha önce gerçekten claim
edilmiş, sonradan yalnızca TTL yüzünden expired olmuş** bir plana bağlı
terminal bir Job'ı listeden ve tekil okumadan düşürüyordu — kayıt hâlâ
veritabanındayken.

Bu dosya kök nedeni **uçtan uca gerçek bileşenlerle** ölçer: gerçek
``freeze_workspace``, gerçek ``store_prepared_plan`` + ``claim_and_reserve_playbook_job``
(R1-V3A'nın tek atomik transaction'ı), gerçek ``sweep_expired_plans`` — hiçbiri
mock'lanmaz — ve API seviyesinde ``GET /api/jobs``, ``GET /api/jobs/{id}``,
``GET /api/jobs/{id}/result``. Servis seviyesindeki bindings/filtre/sayfalama
ayrıntıları :mod:`tests.test_execution_job_read` içinde ayrıca ölçülür; burada
tek bir gerçekçi "restart sonrası geçmiş kayboldu mu" senaryosu doğrulanır.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import (
    ExecutionMode,
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    Inventory,
    InventorySourceType,
    Job,
    JobStatus,
    JobType,
    Project,
)
from app.services.execution import workspace as ws
from app.services.execution.authorize import claim_and_reserve_playbook_job
from app.services.execution.normalize import OUTCOME_SUCCESSFUL, SCHEMA_VERSION
from app.services.execution.read import get_playbook_job, list_playbook_jobs
from app.services.execution.result_service import get_playbook_job_result
from app.services.execution.store import (
    input_fingerprint,
    store_prepared_plan,
    sweep_expired_plans,
)
from app.services.execution.workspace import freeze_workspace, workspace_exists
from app.services.jobs.artifacts import JobArtifactStore

PLAYBOOK_PATH = "site.yml"
PLAYBOOK_TEXT = "---\n- hosts: all\n"
SNAPSHOT = '{\n  "all": {\n    "hosts": {\n      "web01": {}\n    }\n  }\n}\n'
ACTOR = "local-single-user"  # Settings.local_actor'ın varsayılanı.
TTL = 600.0
MAX_EVENTS = 100
MAX_RESULT_BYTES = 100_000

pytestmark = pytest.mark.skipif(
    not ws.secure_filesystem_available(),
    reason="Descriptor-relative dosya sistemi primitive'leri bu platformda yok (ADR-017).",
)


@pytest.fixture
def workspace_root(settings: Settings) -> Path:
    """Gerçek execution-plan workspace kökü; test izole ``app-data`` altındadır."""
    root = settings.app_data_dir / "execution-plans"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


@pytest.fixture
def source_project(project_root: Path) -> Path:
    root = project_root / "proje"
    root.mkdir()
    (root / PLAYBOOK_PATH).write_text(PLAYBOOK_TEXT, encoding="utf-8")
    return root


@pytest.fixture
def records(db_session: Session, source_project: Path) -> tuple[Project, Inventory]:
    project = Project(name="Web", path=str(source_project))
    db_session.add(project)
    db_session.commit()
    inventory = Inventory(
        name="Prod",
        path=str(source_project / "hosts.ini"),
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    db_session.add(inventory)
    db_session.commit()
    return project, inventory


def _fingerprint(project: Project, inventory: Inventory) -> str:
    return input_fingerprint(
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        mode=ExecutionMode.CHECK,
        connection="ssh",
        become=False,
        limit=None,
        tags=None,
        skip_tags=None,
        host_key_policy="strict",
    )


def _prepare_and_claim(
    session: Session,
    *,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    now: datetime,
) -> str:
    """Gerçek dondurulmuş workspace + plan + claim ile tek bir ``pending`` Job üretir.

    R1-V3A'nın tek transaction'ını (:func:`claim_and_reserve_playbook_job`)
    kullanır: claim ile Job aynı commit'te kalıcı olur, tıpkı gerçek
    ``POST /api/projects/{project_id}/executions`` yolunda olduğu gibi.
    """
    project, inventory = records
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    prepared = store_prepared_plan(
        session,
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        fingerprint=_fingerprint(project, inventory),
        mode=ExecutionMode.CHECK,
        requested_by=ACTOR,
        workspace_id=frozen.workspace_id,
        manifest_digest=frozen.manifest_digest,
        ttl_seconds=TTL,
        now=now,
    )
    authorized = claim_and_reserve_playbook_job(
        session,
        token=prepared.token,
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        fingerprint=_fingerprint(project, inventory),
        mode=ExecutionMode.CHECK,
        requested_by=ACTOR,
        workspace_root=workspace_root,
        now=now,
    )
    return authorized.job_id


def _finish_job(session: Session, job_id: str, *, moment: datetime) -> None:
    """Job'ı gerçek worker'ı çalıştırmadan ``successful`` terminale taşır.

    Bu dosya yalnızca plan/okuma sözleşmesini ölçer; executor/worker ayrı
    dilimlerde test edilir. Burada yapılan, worker'ın normal bitirme yolunun
    Job satırında bırakacağı **nihai** alan kümesidir.
    """
    job = session.execute(select(Job).where(Job.id == job_id)).scalar_one()
    job.status = JobStatus.SUCCESSFUL
    job.started_at = moment
    job.finished_at = moment + timedelta(seconds=5)
    job.return_code = 0
    job.artifact_path = f"jobs/{job_id}/result.json"
    session.commit()


def _publish_result(app_data_dir: Path, job_id: str) -> None:
    store = JobArtifactStore(app_data_dir)
    store.create(job_id)
    store.write_result(
        job_id,
        {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "return_code": 0,
            "outcome": OUTCOME_SUCCESSFUL,
            "error_code": None,
            "recap": {
                "web01": {
                    "ok": 1,
                    "changed": 0,
                    "failures": 0,
                    "unreachable": 0,
                    "skipped": 0,
                    "rescued": 0,
                    "ignored": 0,
                }
            },
            "events": [
                {
                    "event": "runner_on_ok",
                    "host": "web01",
                    "task": "Ping",
                    "changed": False,
                    "failed": False,
                }
            ],
            "events_truncated": False,
            "result_truncated": False,
            "ansible_output": "ok: [web01]",
            "ansible_output_truncated": False,
        },
    )


# --- Kök vaka: gerçek claim → terminal → gerçek TTL sweep → hâlâ okunabilir --


def test_a_terminal_job_survives_a_real_ttl_sweep_of_its_plan(
    db_session: Session,
    settings: Settings,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Restart/reconcile eşdeğeri: gerçek `sweep_expired_plans` çalıştıktan
    sonra terminal Job hem serviste hem (dolaylı olarak) API'de okunabilir
    kalır; plan `expired`e düşer ve workspace'i gerçekten silinir.
    """
    claimed_moment = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    job_id = _prepare_and_claim(
        db_session,
        workspace_root=workspace_root,
        source_project=source_project,
        records=records,
        now=claimed_moment,
    )
    _finish_job(db_session, job_id, moment=claimed_moment + timedelta(seconds=1))
    _publish_result(settings.app_data_dir, job_id)

    plan = db_session.execute(select(ExecutionPlanRecord)).scalar_one()
    assert plan.status is ExecutionPlanStatus.CLAIMED
    assert workspace_exists(workspace_root, plan.workspace_id) is True

    # TTL'yi kesin biçimde geçen bir an: `prepare` anı + TTL + fazlası.
    after_ttl = claimed_moment + timedelta(seconds=TTL + 3600)

    result = sweep_expired_plans(db_session, workspace_root=workspace_root, now=after_ttl)

    assert result.expired_records == 1
    assert result.removed_workspaces == 1

    db_session.expire_all()
    plan = db_session.execute(select(ExecutionPlanRecord)).scalar_one()
    assert plan.status is ExecutionPlanStatus.EXPIRED
    # Kök neden burada: temizlik `claimed_at`'e hiç dokunmaz.
    assert plan.claimed_at is not None
    assert workspace_exists(workspace_root, plan.workspace_id) is False

    # Job satırı ve artifact'i temizlikten etkilenmez.
    job = db_session.execute(select(Job).where(Job.id == job_id)).scalar_one()
    assert job.status is JobStatus.SUCCESSFUL

    # Geçmiş hâlâ okunabilir: liste, tekil okuma ve sonuç.
    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    assert [item.job_id for item in page.items] == [job_id]

    summary = get_playbook_job(db_session, job_id, requested_by=ACTOR)
    assert summary.status is JobStatus.SUCCESSFUL
    assert summary.has_recorded_result is True

    fetched_result = get_playbook_job_result(
        db_session,
        job_id,
        requested_by=ACTOR,
        app_data_dir=settings.app_data_dir,
        max_events=MAX_EVENTS,
        max_result_bytes=MAX_RESULT_BYTES,
    )
    assert fetched_result.job_id == job_id
    assert fetched_result.outcome == OUTCOME_SUCCESSFUL


def test_the_same_history_is_readable_through_the_http_api_after_a_real_sweep(
    client: TestClient,
    db_session: Session,
    settings: Settings,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Aynı senaryo, servis fonksiyonları yerine gerçek HTTP yüzeyinden ölçülür.

    `client` fixture'ı bu testin kullandığı `db_session`/`settings` ile
    **aynı** migrate edilmiş engine'i paylaşır (ikisi de `migrated_engine`
    fixture'ından türer); bu yüzden burada yazılan satırlar API'nin kendi
    session'ından da görünür.
    """
    claimed_moment = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
    job_id = _prepare_and_claim(
        db_session,
        workspace_root=workspace_root,
        source_project=source_project,
        records=records,
        now=claimed_moment,
    )
    _finish_job(db_session, job_id, moment=claimed_moment + timedelta(seconds=1))
    _publish_result(settings.app_data_dir, job_id)

    sweep_expired_plans(
        db_session,
        workspace_root=workspace_root,
        now=claimed_moment + timedelta(seconds=TTL + 3600),
    )
    db_session.commit()

    listed = client.get("/api/jobs")
    assert listed.status_code == 200
    body = listed.json()
    assert [item["job_id"] for item in body["items"]] == [job_id]

    detail = client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "successful"

    result = client.get(f"/api/jobs/{job_id}/result")
    assert result.status_code == 200
    assert result.json()["job_id"] == job_id
    assert result.json()["outcome"] == OUTCOME_SUCCESSFUL


# --- Negatif kontrol: hiç claim edilmeden expired olan bir plan yetki vermez -


def test_a_plan_that_expires_via_real_sweep_without_ever_being_claimed_authorizes_nothing(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Hazırlanmış ama hiç claim edilmemiş bir plan gerçek `sweep` ile expired
    olur; `claimed_at` hiçbir zaman dolmaz ve — doğrudan yazılmış bir Job satırı
    onu işaret etse bile — geçmişe yetki vermez.

    Normal akışta claim edilmeden bir Job asla üretilemez
    (:func:`claim_and_reserve_playbook_job` claim başarısızken Job yazmaz); bu
    yüzden ikinci yarı, düzeltmenin gevşetmediğini göstermek için bilinçli
    olarak doğrudan bir satır yazar (bkz. `tests.test_execution_job_read`'in
    aynı gerekçeli `BROKEN_BINDINGS["plan_expired"]` vakası).
    """
    project, inventory = records
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    prepared_moment = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    prepared = store_prepared_plan(
        db_session,
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        fingerprint=_fingerprint(project, inventory),
        mode=ExecutionMode.CHECK,
        requested_by=ACTOR,
        workspace_id=frozen.workspace_id,
        manifest_digest=frozen.manifest_digest,
        ttl_seconds=TTL,
        now=prepared_moment,
    )

    after_ttl = prepared_moment + timedelta(seconds=TTL + 3600)
    result = sweep_expired_plans(db_session, workspace_root=workspace_root, now=after_ttl)
    assert result.expired_records == 1

    db_session.expire_all()
    plan = db_session.execute(select(ExecutionPlanRecord)).scalar_one()
    assert plan.status is ExecutionPlanStatus.EXPIRED
    assert plan.claimed_at is None
    assert plan.id == prepared.plan_id

    fabricated_job_id = str(uuid.uuid4())
    db_session.add(
        Job(
            id=fabricated_job_id,
            job_type=JobType.PLAYBOOK,
            status=JobStatus.SUCCESSFUL,
            mode=ExecutionMode.CHECK,
            execution_plan_id=plan.id,
            project_id=project.id,
            inventory_id=inventory.id,
            playbook_path=PLAYBOOK_PATH,
            limit_pattern=None,
            requested_by=ACTOR,
            return_code=0,
            started_at=prepared_moment,
            finished_at=prepared_moment + timedelta(seconds=1),
            created_at=prepared_moment,
        )
    )
    db_session.commit()

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    assert page.items == ()
