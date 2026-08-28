"""Public launch façade'ının sınırı (R1-V3D1).

Atomik claim + rezervasyon davranışı :mod:`tests.test_execution_authorize`
tarafından ölçülür ve burada **yeniden ölçülmez**. Buradaki iddia yalnız
façade'ın kendisidir:

1. *Özet sunucuda kurulur.* İmzada ne ``fingerprint`` ne de ``mode``,
   ``connection``, ``become``, ``limit``, ``tags``, ``skip_tags`` vardır; alt
   servise giden özet plan sabitlerinden ve isteğin bağlamından üretilir.
2. *Politika bağlayıcıdır.* Hazırlama anındaki ``host_key_policy`` ile
   çalıştırma anındaki ayrışırsa token hiçbir satırı eşleştirmez ve
   **tüketilmez**.
3. *Arıza daraltılır.* Veritabanı hatası public-safe tek bir 503'e çevrilir;
   dışarı ne DB metni, ne token, ne path, ne digest çıkar. Arıza **hangi
   aşamada** olursa olsun — final commit'te ya da claim UPDATE'inin kendisinde
   — session çağırana açık/failed transaction ile dönmez ve plan ``prepared``
   kalır.
4. *Kapsam kilidi.* Façade runner, subprocess, worker veya artifact katmanına
   dokunmaz ve açtığı HTTP yüzeyi tam olarak **bir** endpoint'tir. O
   endpoint'in kendi davranışı :mod:`tests.test_execution_launch_api`
   tarafından ölçülür.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

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
from app.services.execution import launch as launch_module
from app.services.execution import workspace as ws
from app.services.execution.launch import (
    ExecutionLaunchUnavailableError,
    launch_prepared_playbook_job,
)
from app.services.execution.store import (
    ExecutionPlanInvalidError,
    input_fingerprint,
    store_prepared_plan,
)
from app.services.execution.workspace import freeze_workspace

SNAPSHOT = '{\n  "all": {\n    "hosts": {\n      "web01": {}\n    }\n  }\n}\n'
PLAYBOOK_PATH = "site.yml"
PLAYBOOK_TEXT = "---\n- hosts: all\n"
TTL = 600.0
ACTOR = "yerel-operator"
POLICY = "strict"
OTHER_POLICY = "accept_new"

pytestmark = pytest.mark.skipif(
    not ws.secure_filesystem_available(),
    reason="Descriptor-relative dosya sistemi primitive'leri bu platformda yok (ADR-017).",
)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "execution-plans"
    root.mkdir(mode=0o700)
    return root


@pytest.fixture
def source_project(tmp_path: Path) -> Path:
    root = tmp_path / "proje"
    root.mkdir()
    (root / PLAYBOOK_PATH).write_text(PLAYBOOK_TEXT, encoding="utf-8")
    return root


@pytest.fixture
def records(db_session: Session, tmp_path: Path) -> tuple[Project, Inventory]:
    project = Project(name="Web", path=str(tmp_path / "proje"))
    db_session.add(project)
    db_session.commit()
    inventory = Inventory(
        name="Prod",
        path=str(tmp_path / "proje" / "hosts.ini"),
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    db_session.add(inventory)
    db_session.commit()
    return project, inventory


def _expected_fingerprint(
    project: Project, inventory: Inventory, *, host_key_policy: str = POLICY
) -> str:
    """Hazırlama yolunun ürettiği özet; testte **elle**, sabitler açık yazılır."""
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
        host_key_policy=host_key_policy,
    )


def _prepare(
    session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> str:
    """Dondurulmuş workspace ve plan kaydı üretir; raw token'ı döndürür."""
    project, inventory = records
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=SNAPSHOT
    )
    prepared = store_prepared_plan(
        session,
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        fingerprint=_expected_fingerprint(project, inventory),
        mode=ExecutionMode.CHECK,
        requested_by=ACTOR,
        workspace_id=frozen.workspace_id,
        manifest_digest=frozen.manifest_digest,
        ttl_seconds=TTL,
    )
    return prepared.token


def _launch(
    session: Session,
    workspace_root: Path,
    records: tuple[Project, Inventory],
    token: str,
    **overrides: Any,
) -> Any:
    project, inventory = records
    arguments: dict[str, Any] = {
        "token": token,
        "mode": ExecutionMode.CHECK,
        "project_id": project.id,
        "inventory_id": inventory.id,
        "playbook_path": PLAYBOOK_PATH,
        "requested_by": ACTOR,
        "workspace_root": workspace_root,
        "host_key_policy": POLICY,
    }
    arguments.update(overrides)
    return launch_prepared_playbook_job(session, **arguments)


def _plan(session: Session) -> ExecutionPlanRecord:
    session.expire_all()
    return session.execute(select(ExecutionPlanRecord)).scalar_one()


def _jobs(session: Session) -> list[Job]:
    session.expire_all()
    return list(session.execute(select(Job)).scalars().all())


# --- Mutlu yol ---------------------------------------------------------------


def test_launch_reserves_a_pending_job_from_a_prepared_plan(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Façade, hazırlanmış planı tüketip tam bir ``pending`` PLAYBOOK Job üretir."""
    project, inventory = records
    token = _prepare(db_session, workspace_root, source_project, records)

    authorized = _launch(db_session, workspace_root, records, token)

    plan = _plan(db_session)
    assert plan.status is ExecutionPlanStatus.CLAIMED

    jobs = _jobs(db_session)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == authorized.job_id
    assert job.job_type is JobType.PLAYBOOK
    assert job.status is JobStatus.PENDING
    assert job.execution_plan_id == plan.id == authorized.plan_id
    assert job.project_id == project.id
    assert job.inventory_id == inventory.id
    assert job.playbook_path == PLAYBOOK_PATH
    assert job.requested_by == ACTOR
    # Hiçbir şey çalıştırılmaz: Job boş bir rezervasyondur.
    assert job.started_at is None
    assert job.artifact_path is None

    # Dönüş tipi alt servisin sözleşmesidir; façade yeni bir kabuk uydurmaz.
    assert type(authorized).__name__ == "AuthorizedPlaybookJob"
    assert authorized.manifest_digest == plan.manifest_digest
    # Raw token dönen nesnenin hiçbir alanında yer almaz.
    assert token not in repr(authorized)


# --- Özet sunucuda kurulur ---------------------------------------------------


def test_the_fingerprint_is_built_by_the_server_not_the_caller(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alt servise giden özet, plan sabitleri ve istek bağlamından üretilir.

    Çağrı yakalanır ama **engellenmez**: gerçek servis çalışmaya devam eder, bu
    yüzden ölçüm hem özeti hem de özetin gerçekten eşleştiğini kanıtlar.
    """
    project, inventory = records
    token = _prepare(db_session, workspace_root, source_project, records)
    seen: list[dict[str, Any]] = []
    real = launch_module.claim_and_reserve_playbook_job

    def _spy(session: Session, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(session, **kwargs)

    monkeypatch.setattr(launch_module, "claim_and_reserve_playbook_job", _spy)

    _launch(db_session, workspace_root, records, token)

    assert len(seen) == 1
    passed = seen[0]
    # `connection=ssh`, `become=false`, limit/tags/skip_tags=null ve verilen
    # politika özete bağlıdır.
    assert passed["fingerprint"] == _expected_fingerprint(project, inventory)
    assert passed["requested_by"] == ACTOR
    # `connection`, `become`, `limit`, `tags` ve `skip_tags` alt servise ayrı
    # alanlar olarak **geçmez**: onların tek izi özettir. `mode` ise bilinçli
    # bir istisnadır — claim koşulunda okunabilir bir sütun olarak da aranır.
    assert set(passed) == {
        "token",
        "project_id",
        "inventory_id",
        "playbook_path",
        "fingerprint",
        "mode",
        "requested_by",
        "workspace_root",
    }
    # Kip alt servise ayrı bir alan olarak da geçer; façade'ın kendisi artık
    # bir varsayım kurmaz (R1-V3H2A) — bu çağrının `check` görmesinin sebebi
    # `_launch` yardımcısının test varsayılanıdır, façade'ın sabiti değil.
    assert passed["mode"] is ExecutionMode.CHECK


def test_the_launch_signature_accepts_no_execution_parameters() -> None:
    """İmza, istemcinin özet veya çalıştırma parametresi vermesini imkânsız kılar.

    ``mode`` bu kuralın **istisnasıdır** (R1-V3H2A): imzada vardır ve
    zorunludur, ama yalnız *beklenen* kiptir — fingerprint ve claim koşuluna
    girer, Job'a yazılan değeri belirlemez.
    """
    parameters = inspect.signature(launch_prepared_playbook_job).parameters

    assert set(parameters) == {
        "session",
        "token",
        "mode",
        "project_id",
        "inventory_id",
        "playbook_path",
        "requested_by",
        "workspace_root",
        "host_key_policy",
    }
    mode_parameter = parameters["mode"]
    assert mode_parameter.default is inspect.Parameter.empty
    assert mode_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    for forbidden in ("fingerprint", "connection", "become", "limit", "tags", "skip_tags"):
        assert forbidden not in parameters

    # `session` dışındaki her şey keyword-only: çağrı yerinde sıra kayması
    # yüzünden project ile inventory'nin yer değiştirmesi mümkün değildir.
    keyword_only = [
        name
        for name, parameter in parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    ]
    assert len(keyword_only) == len(parameters) - 1


def test_a_different_host_key_policy_changes_the_fingerprint(
    records: tuple[Project, Inventory],
) -> None:
    """Politika özete gerçekten bağlıdır; iki politika aynı özeti üretmez."""
    project, inventory = records

    strict = _expected_fingerprint(project, inventory, host_key_policy=POLICY)
    accept_new = _expected_fingerprint(project, inventory, host_key_policy=OTHER_POLICY)

    assert strict != accept_new


def test_a_mismatched_host_key_policy_refuses_without_consuming_the_token(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Politika ayrıştıysa: Job yok, plan ``prepared``, token hâlâ kullanılabilir.

    Politika sunucu ayarından gelir; ayarın hazırlama ile çalıştırma arasında
    değişmesi kullanıcının onayladığı planı geçersiz kılar ama bileti
    **yakmaz**: reddedilen şey içerik değil, eşleşmeyen bağlamdır.
    """
    token = _prepare(db_session, workspace_root, source_project, records)

    with pytest.raises(ExecutionPlanInvalidError) as error:
        _launch(db_session, workspace_root, records, token, host_key_policy=OTHER_POLICY)

    assert error.value.code == "execution_plan_invalid"
    assert error.value.status_code == 409
    assert _jobs(db_session) == []
    assert _plan(db_session).status is ExecutionPlanStatus.PREPARED

    # Doğru politikayla aynı token tam olarak bir Job üretir.
    authorized = _launch(db_session, workspace_root, records, token)
    assert [job.id for job in _jobs(db_session)] == [authorized.job_id]


def test_a_reused_token_produces_no_second_job(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Tek kullanım garantisi façade üzerinden de geçerlidir."""
    token = _prepare(db_session, workspace_root, source_project, records)
    authorized = _launch(db_session, workspace_root, records, token)

    with pytest.raises(ExecutionPlanInvalidError):
        _launch(db_session, workspace_root, records, token)

    assert [job.id for job in _jobs(db_session)] == [authorized.job_id]


# --- Arıza daraltması --------------------------------------------------------


def test_a_database_failure_becomes_a_public_safe_unavailable_error(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """SQLAlchemyError 503'e çevrilir; sızıntı yok, rollback davranışı korunur.

    Ham ``SQLAlchemyError`` dışarı çıksaydı, cevaba veritabanı hata metni
    (dosya yolu, tablo ve sütun adları, hatta sorgu parametreleri) girerdi.
    Onu :class:`ExecutionPlanInvalidError`'a katlamak ise geçici bir arızayı
    "planınız geçersiz" diye gösterirdi; burada plan **geçerlidir** ve aynı
    token yeniden kullanılabilir.
    """
    token = _prepare(db_session, workspace_root, source_project, records)
    digest = _plan(db_session).manifest_digest
    db_text = "disk I/O error: /var/lib/aselai/app.db"
    real_commit = db_session.commit
    attempts: list[int] = []

    def _failing_commit() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("COMMIT", {}, Exception(db_text))
        real_commit()

    db_session.commit = _failing_commit  # type: ignore[method-assign]
    try:
        with pytest.raises(ExecutionLaunchUnavailableError) as error:
            _launch(db_session, workspace_root, records, token)
    finally:
        del db_session.commit

    assert error.value.status_code == 503
    assert error.value.code == "execution_launch_unavailable"
    rendered = f"{error.value.message} {error.value.details}"
    for secret in (token, token[:8], digest, db_text, "OperationalError", str(workspace_root)):
        assert secret not in rendered
    # Özgün arıza yalnız zincirde durur, mesajda değil.
    assert isinstance(error.value.__cause__, OperationalError)

    # Alt servisin rollback'i korunur: plan `prepared`, orphan Job yok.
    assert _plan(db_session).status is ExecutionPlanStatus.PREPARED
    assert _jobs(db_session) == []
    # Session açık ve kullanılabilir kaldı; failed transaction bırakılmadı.
    assert db_session.execute(select(ExecutionPlanRecord.id)).scalar_one() is not None
    authorized = _launch(db_session, workspace_root, records, token)
    assert [job.id for job in _jobs(db_session)] == [authorized.job_id]
    assert _plan(db_session).status is ExecutionPlanStatus.CLAIMED


# Bozulmuş ifadeye konan işaret. sqlite bilinmeyen sütunu prepare aşamasında
# reddeder; üretilen hata metni bu dizgeyi taşır, dolayısıyla "DB metni dışarı
# sızmıyor" iddiası gerçek bir metin üzerinde ölçülür.
INJECTED_MARKER = "aselai_injected_claim_failure"


def _is_claim_update(statement: str) -> bool:
    """Yalnız plan claim UPDATE'ini tanır.

    Hedef ifade metniyle doğrulanır: aynı çağrı sırasında birden çok ifade
    çalışır (Job INSERT, plan SELECT, `expire_plan_by_token` UPDATE'i) ve
    bunlardan herhangi birine hata enjekte eden bir test, iddia ettiği aşamayı
    hiç ölçmezdi.
    """
    normalized = " ".join(statement.split()).lower()
    return (
        normalized.startswith("update execution_plans")
        and "claimed_at" in normalized
        and "token_hash" in normalized
    )


def test_a_claim_stage_sql_failure_leaves_no_open_transaction(
    db_session: Session,
    migrated_engine: Engine,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Claim UPDATE'i SQL seviyesinde düşerse de session temiz kalır.

    Alt servisin rollback blokları Job flush'ını ve final commit'i kapsar;
    claim UPDATE'inin **kendisi** düştüğünde hata o blokların hiçbirine
    uğramadan yukarı çıkar ve transaction açık kalır. Ölçülen tam olarak
    budur: façade rollback etmeseydi ``in_transaction()`` bu noktada hâlâ
    ``True`` olurdu; yani daraltılmış hata, session'ı tanımsız bir transaction
    sınırıyla çağırana geri verirdi.

    Arıza taklit **edilmez**: listener yalnız claim ifadesini bozar, hatayı
    gerçek sqlite cursor'ı üretir ve SQLAlchemy onu tıpkı gerçek bir disk
    arızasında olduğu gibi :class:`~sqlalchemy.exc.OperationalError`'a
    sarmalar. Listener'ın kendisinden exception fırlatmak bunu ölçmezdi:
    ``before_cursor_execute``, SQLAlchemy'nin DBAPI hata sarmalayıcısının
    **dışında** çalışır.
    """
    token = _prepare(db_session, workspace_root, source_project, records)
    digest = _plan(db_session).manifest_digest
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
        with pytest.raises(ExecutionLaunchUnavailableError) as error:
            _launch(db_session, workspace_root, records, token)
    finally:
        # Listener her koşulda kalkar: kalırsa sonraki testler de bu engine
        # üzerinde claim edemezdi.
        event.remove(migrated_engine, "before_cursor_execute", _break_claim_update)

    # Hata gerçekten claim aşamasına enjekte edildi: hedeflenen ifade plan
    # satırını `claimed` yapan UPDATE'tir, başka bir sorgu değil.
    assert len(targeted) == 1, targeted
    hit = " ".join(targeted[0].split()).lower()
    assert hit.startswith("update execution_plans set status=")
    # Arıza DBAPI sınırında doğdu ve SQLAlchemy tarafından sarmalandı.
    cause = error.value.__cause__
    assert isinstance(cause, OperationalError)
    assert isinstance(cause.orig, sqlite3.OperationalError)

    assert error.value.status_code == 503
    assert error.value.code == "execution_launch_unavailable"
    rendered = f"{error.value.message} {error.value.details}"
    db_text = str(cause.orig)
    assert INJECTED_MARKER in db_text, db_text
    for secret in (token, token[:8], digest, db_text, INJECTED_MARKER, str(workspace_root)):
        assert secret not in rendered

    # Asıl regresyon: transaction açık/failed bırakılmadı.
    assert db_session.in_transaction() is False

    # Session hâlâ sorgu çalıştırabiliyor; claim de Job da kalıcı olmadı.
    assert db_session.execute(select(func.count()).select_from(Job)).scalar_one() == 0
    assert _plan(db_session).status is ExecutionPlanStatus.PREPARED
    assert _jobs(db_session) == []

    # Aynı token, aynı session ile tam bir `pending` Job üretir.
    authorized = _launch(db_session, workspace_root, records, token)
    jobs = _jobs(db_session)
    assert [job.id for job in jobs] == [authorized.job_id]
    assert jobs[0].status is JobStatus.PENDING
    assert _plan(db_session).status is ExecutionPlanStatus.CLAIMED


def test_domain_refusals_are_not_narrowed_into_unavailable(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Reddedilen istek 503 olmaz: iki hata sınıfı birbirine karışmaz."""
    token = _prepare(db_session, workspace_root, source_project, records)

    with pytest.raises(ExecutionPlanInvalidError):
        _launch(db_session, workspace_root, records, token, project_id=987)

    assert not issubclass(ExecutionPlanInvalidError, ExecutionLaunchUnavailableError)
    assert not issubclass(ExecutionLaunchUnavailableError, ExecutionPlanInvalidError)


# --- Kapsam kilidi -----------------------------------------------------------

FORBIDDEN_IMPORTS = (
    "subprocess",
    "app.services.execution.runner_process",
    "app.services.execution.runner_env",
    "app.services.execution.executor",
    "app.services.execution.worker",
    "app.services.execution.lease",
    "app.services.execution.reconcile",
    "app.api",
    "app.schemas",
    "app.services.jobs",
)


def test_the_facade_imports_no_execution_machinery() -> None:
    """Façade rezervasyon katmanının üstüne çıkmaz.

    Ölçüm import ifadeleri üzerinden yapılır: burada bir runner, süreç, worker
    veya route modülünün *görünür olması* bile, "bu tur hiçbir şey
    çalıştırmaz" iddiasının sessizce aşınması demektir.
    """
    source = Path(launch_module.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    for forbidden in FORBIDDEN_IMPORTS:
        assert not any(name == forbidden or name.startswith(f"{forbidden}.") for name in imported)

    # İzin verilen yüzey dar tutulur; artifact ve run dizini kavramları hiç geçmez.
    assert imported == {
        "__future__",
        "pathlib",
        "sqlalchemy.exc",
        "sqlalchemy.orm",
        "app.core.errors",
        # Ortak `ExecutionMode` tipi (R1-V3H1B1). Model yüzeyi çalıştırma
        # makinesi değildir: façade kipi burada **sabitler**, hiçbir şey
        # çalıştırmaz.
        "app.models",
        "app.services.execution.authorize",
        "app.services.execution.plan",
        "app.services.execution.store",
    }


def test_the_facade_opens_exactly_one_public_route(client: TestClient) -> None:
    """Façade'ın açtığı HTTP yüzeyi tam olarak **bir** yoldur (R1-V3D1).

    Önceki turda bu test "hiçbir yol yoktur" diyordu. Yol artık bilerek açıktır,
    dolayısıyla ölçülen iddia değişir ama gevşemez: façade tek bir endpoint'e
    bağlanır, o endpoint'in gövdesi yalnız token + bağlam (+ R1-V3H2A ile
    ``mode``) alır ve diğer çalıştırma parametreleri (``host_key_policy``,
    ``become``, ``limit``, …) hiçbir istek şemasına sızmaz.
    """
    spec = client.get("/openapi.json").json()

    execution_paths = {path for path in spec["paths"] if "execution" in path}
    assert execution_paths == {
        "/api/projects/{project_id}/execution-plan",
        "/api/projects/{project_id}/execution-plans",
        "/api/projects/{project_id}/executions",
    }

    schemas = spec.get("components", {}).get("schemas", {})
    token_operations: set[tuple[str, str]] = set()
    request_fields: set[str] = set()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            content = operation.get("requestBody", {}).get("content", {})
            fields: set[str] = set()
            for media in content.values():
                name = media.get("schema", {}).get("$ref", "").rsplit("/", 1)[-1]
                fields.update(schemas.get(name, {}).get("properties", {}))
            request_fields.update(fields)
            if "plan_token" in fields:
                token_operations.add((path, method))

    assert token_operations == {("/api/projects/{project_id}/executions", "post")}

    # Façade'ın imzasında olmayan hiçbir çalıştırma parametresi HTTP yüzeyinden
    # de geçemez. `mode` R1-V3H2A ile bu kümenin dışına çıkmıştır: façade'ın
    # imzasında artık vardır (yalnız *beklenen* kip olarak).
    for forbidden in (
        "host_key_policy",
        "fingerprint",
        "requested_by",
        "connection",
        "become",
        "tags",
        "skip_tags",
        "extra_vars",
    ):
        assert forbidden not in request_fields, forbidden
