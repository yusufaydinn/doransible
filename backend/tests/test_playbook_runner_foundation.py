"""Migration 0007 — playbook runner temeli (R1-V3C1A).

Üç iddia ölçülür:

1. *Şema.* Sonuç alanları (``error_code``, ``result_truncated``) ve worker
   ownership/lease üçlüsü eklenir; ``up → down → up`` şemayı aynı yere getirir
   ve model ile migration ayrışmaz.
2. *Concurrency.* Global aktif PLAYBOOK sınırı **veritabanı tarafından**
   uygulanır: ikinci bir `pending`, bir `pending` + bir `running` ve iki
   `running` kombinasyonlarının hiçbiri kabul edilmez. Sınırı in-process bir
   sayaçla ölçmek, iki backend sürecinin aynı sayacı paylaşmadığı gerçeğini
   gizlerdi.
3. *Eski veri.* Migration'dan önce yazılmış etkin PLAYBOOK satırları
   kendiliğinden çalıştırılamaz hâle gelir: terminal `failed` olur ve sebebi
   ``interrupted_by_upgrade`` yazılır.

Ownership testleri bilinçli olarak **ham SQL** ile yazılır. ORM üzerinden
yazmak, ihlali Python tarafında yakalanabilir kılar ve asıl soruyu — *doğrudan
veritabanına yazılan bir satır da reddediliyor mu* — yanıtsız bırakırdı.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, Table, inspect, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex

from app.core.config import Settings
from app.db.session import create_db_engine
from app.models import Job
from tests.support import alembic_config, make_settings

PREVIOUS_REVISION = "0006_bind_execution_plans_to_actor_and_job"
ACTIVE_PLAYBOOK_INDEX = "uq_jobs_active_playbook_global"

# SQLite kısmi unique index ihlalinde index adını değil **anahtar sütunu**
# bildirir. Beklenen metin buradan okunur: `job_type` üzerinde bir unique ihlali
# yalnız bu index tarafından üretilebilir (tabloda başka unique `job_type`
# kısıtı yoktur) ve bu, sınırın gerçekten global olduğunun da kanıtıdır.
ACTIVE_PLAYBOOK_VIOLATION = "UNIQUE constraint failed: jobs.job_type"

# Migration'ın yazdığı sebep kodu. Migration geçmişi dondurulmuş bir kayıttır;
# uygulama sabiti ileride değişse bile bu değer değişmemelidir.
INTERRUPTED_BY_UPGRADE = "interrupted_by_upgrade"


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    engine = create_db_engine(settings)
    try:
        yield engine
    finally:
        engine.dispose()


def _upgrade(settings: Settings, revision: str = "head") -> None:
    command.upgrade(alembic_config(settings.resolve_database_url()), revision)


def _downgrade(settings: Settings, revision: str) -> None:
    command.downgrade(alembic_config(settings.resolve_database_url()), revision)


def _column_names(engine: Engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("jobs")}


def _seed_project_and_inventory(connection: Connection, now: datetime) -> None:
    """Job'ların FK'lerini karşılayan asgari kayıtlar."""
    connection.execute(
        text(
            "INSERT INTO projects (id, name, path, path_key, is_active, created_at, updated_at) "
            "VALUES (1, 'Web', '/tmp/p', '/tmp/p', 1, :now, :now)"
        ),
        {"now": now},
    )
    connection.execute(
        text(
            "INSERT INTO inventories (id, project_id, name, path, source_type, "
            "created_at, updated_at) "
            "VALUES (1, 1, 'Prod', '/tmp/p/hosts.ini', 'ini', :now, :now)"
        ),
        {"now": now},
    )
    connection.execute(
        text(
            "INSERT INTO inventories (id, project_id, name, path, source_type, "
            "created_at, updated_at) "
            "VALUES (2, 1, 'Test', '/tmp/p/test.ini', 'ini', :now, :now)"
        ),
        {"now": now},
    )


def _insert_playbook_job(
    connection: Connection,
    *,
    status: str,
    inventory_id: int = 1,
    worker_id: str | None = None,
    heartbeat_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
    now: datetime | None = None,
) -> str:
    """Yetkilendirilmiş bir PLAYBOOK Job satırını **ham SQL** ile yazar.

    Plan bağı gerçekten kurulur (``ck_jobs_active_playbook_is_authorized``);
    kısıtı atlatmak için yarım bir satır üretmek, ölçülmek istenen invariant
    yerine başka bir kısıtın tetiklenmesine yol açardı.
    """
    moment = now or datetime.now(UTC)
    plan_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    unique = uuid.uuid4().hex
    connection.execute(
        text(
            "INSERT INTO execution_plans (id, token_hash, project_id, inventory_id, "
            "playbook_path, requested_by, input_fingerprint, workspace_id, manifest_digest, "
            "status, created_at, expires_at, claimed_at) VALUES "
            "(:id, :token_hash, 1, :inventory, 'site.yml', 'actor', :fingerprint, "
            ":workspace_id, :digest, 'claimed', :created, :expires, :created)"
        ),
        {
            "id": plan_id,
            "token_hash": unique + unique[:32],
            "fingerprint": unique + unique[:32],
            "workspace_id": str(uuid.uuid4()),
            "digest": unique + unique[:32],
            "inventory": inventory_id,
            "created": moment,
            "expires": moment + timedelta(hours=1),
        },
    )
    connection.execute(
        text(
            "INSERT INTO jobs (id, job_type, status, inventory_id, project_id, playbook_path, "
            "execution_plan_id, limit_pattern, requested_by, started_at, created_at, "
            "result_truncated, worker_id, heartbeat_at, lease_expires_at) "
            "VALUES (:id, 'playbook', :status, :inventory, 1, 'site.yml', :plan, NULL, "
            "'actor', :started, :created, 0, :worker, :heartbeat, :lease)"
        ),
        {
            "id": job_id,
            "status": status,
            "inventory": inventory_id,
            "plan": plan_id,
            "started": moment if status == "running" else None,
            "created": moment,
            "worker": worker_id,
            "heartbeat": heartbeat_at,
            "lease": lease_expires_at,
        },
    )
    return job_id


def _insert_legacy_playbook_job(
    connection: Connection, *, status: str, inventory_id: int, now: datetime
) -> str:
    """0007 **öncesi** şemaya bir etkin PLAYBOOK Job'ı yazar.

    Satır gerçekten yetkilendirilmiştir (plan bağı kurulur): 0006'dan sonra
    ``ck_jobs_active_playbook_is_authorized`` zaten yürürlüktedir, dolayısıyla
    yükseltmeyi bekleyen gerçekçi legacy satır tam olarak budur — R1-V3A'nın
    ürettiği, arkasında hiçbir worker olmayan `pending`/`running` bir Job.
    """
    plan_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    unique = uuid.uuid4().hex
    connection.execute(
        text(
            "INSERT INTO execution_plans (id, token_hash, project_id, inventory_id, "
            "playbook_path, requested_by, input_fingerprint, workspace_id, manifest_digest, "
            "status, created_at, expires_at, claimed_at) VALUES "
            "(:id, :token_hash, 1, :inventory, 'site.yml', 'actor', :fingerprint, "
            ":workspace_id, :digest, 'claimed', :created, :expires, :created)"
        ),
        {
            "id": plan_id,
            "token_hash": unique + unique[:32],
            "fingerprint": unique + unique[:32],
            "workspace_id": str(uuid.uuid4()),
            "digest": unique + unique[:32],
            "inventory": inventory_id,
            "created": now,
            "expires": now + timedelta(hours=1),
        },
    )
    connection.execute(
        text(
            "INSERT INTO jobs (id, job_type, status, inventory_id, project_id, playbook_path, "
            "execution_plan_id, limit_pattern, requested_by, started_at, created_at) "
            "VALUES (:id, 'playbook', :status, :inventory, 1, 'site.yml', :plan, NULL, "
            "'eski-aktor', :started, :created)"
        ),
        {
            "id": job_id,
            "status": status,
            "inventory": inventory_id,
            "plan": plan_id,
            "started": now if status == "running" else None,
            "created": now,
        },
    )
    return job_id


def _owned(now: datetime) -> dict[str, object]:
    """Geçerli bir `running` sahiplik/lease üçlüsü."""
    return {
        "worker_id": str(uuid.uuid4()),
        "heartbeat_at": now,
        "lease_expires_at": now + timedelta(minutes=2),
    }


# --- Şema --------------------------------------------------------------------


def test_upgrade_adds_the_runner_foundation_columns(settings: Settings, engine: Engine) -> None:
    """Sonuç ve ownership alanları eklenir; ``result_truncated`` NOT NULL'dır."""
    _upgrade(settings)
    columns = {column["name"]: column for column in inspect(engine).get_columns("jobs")}

    assert {
        "error_code",
        "result_truncated",
        "worker_id",
        "heartbeat_at",
        "lease_expires_at",
    } <= set(columns)

    # Kırpılma göstergesi nullable olsaydı "kırpılmadı" ile "bilinmiyor" aynı
    # değere düşerdi ve eksik bir sonuç tam sonuç gibi okunabilirdi.
    assert columns["result_truncated"]["nullable"] is False
    for name in ("error_code", "worker_id", "heartbeat_at", "lease_expires_at"):
        assert columns[name]["nullable"] is True, name

    checks = {check["name"] for check in inspect(engine).get_check_constraints("jobs")}
    assert {
        "ck_jobs_running_playbook_has_lease",
        "ck_jobs_idle_playbook_has_no_lease",
        "ck_jobs_ping_has_no_lease",
        "ck_jobs_running_playbook_lease_outlives_heartbeat",
    } <= checks


def test_job_status_enum_gains_no_timeout_member(settings: Settings, engine: Engine) -> None:
    """Timeout ayrı bir durum değil, başarısızlığın bir sebebidir.

    Yeni bir enum üyesi, mevcut bütün terminal-durum sorgularını ve
    ``finish_job`` çağrılarını sessizce eksik bırakırdı; timeout ileride
    ``status = 'failed'`` + ``error_code = 'runner_timeout'`` olarak temsil
    edilecektir.
    """
    from app.models import JobStatus

    assert {member.value for member in JobStatus} == {
        "pending",
        "running",
        "successful",
        "failed",
        "canceled",
    }

    _upgrade(settings)
    status_check = next(
        check
        for check in inspect(engine).get_check_constraints("jobs")
        if check["name"] == "ck_jobs_job_status"
    )
    assert "timeout" not in str(status_check["sqltext"])


def test_migration_round_trip_restores_the_same_schema(settings: Settings, engine: Engine) -> None:
    """``up → down → up`` şemayı aynı yere getirir."""
    _upgrade(settings)
    before = _snapshot(engine)

    _downgrade(settings, PREVIOUS_REVISION)

    after_down = _column_names(engine)
    for name in ("error_code", "result_truncated", "worker_id", "heartbeat_at", "lease_expires_at"):
        assert name not in after_down, name
    # 0006 şeması yerinde durur.
    assert "execution_plan_id" in after_down
    assert ACTIVE_PLAYBOOK_INDEX not in {
        index["name"] for index in inspect(engine).get_indexes("jobs")
    }

    _upgrade(settings)
    assert _snapshot(engine) == before


def _snapshot(engine: Engine) -> dict[str, object]:
    """Karşılaştırılabilir bir ``jobs`` şema özeti.

    CHECK'ler de karşılaştırılır: downgrade onları bırakıp giderse ikinci
    upgrade "aynı şema" görünürken invariant kaybolurdu.
    """
    inspector = inspect(engine)
    return {
        "columns": {
            column["name"]: bool(column["nullable"]) for column in inspector.get_columns("jobs")
        },
        "indexes": {
            (index["name"], tuple(index["column_names"]), bool(index["unique"]))
            for index in inspector.get_indexes("jobs")
        },
        "checks": {
            (check["name"], " ".join(str(check["sqltext"]).split()))
            for check in inspector.get_check_constraints("jobs")
        },
    }


def test_model_and_migration_do_not_drift(settings: Settings, engine: Engine) -> None:
    """``compare_metadata`` boş olmalı: model ile migration ayrışmamalı."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from app.db.base import Base

    _upgrade(settings)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


# --- Global concurrency index ------------------------------------------------


def test_active_playbook_index_compiles_for_sqlite_and_postgresql() -> None:
    """Aynı invariant iki dialect'te de aynı biçimde üretilmelidir.

    Anahtarın ``job_type`` olması kuralın kendisidir: predicate zaten
    ``job_type = 'playbook'`` dediği için index'e giren bütün satırlar **aynı**
    anahtarı taşır ve ikincisi yinelenen anahtar olur.
    """
    table = cast(Table, Job.__table__)
    index = next(index for index in table.indexes if index.name == ACTIVE_PLAYBOOK_INDEX)

    sqlite_sql = str(CreateIndex(index).compile(dialect=sqlite.dialect()))
    postgres_sql = str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    for sql in (sqlite_sql, postgres_sql):
        assert "UNIQUE INDEX" in sql
        assert "(job_type)" in sql.replace(" (", "(")
        assert "job_type = 'playbook'" in sql
        assert "status IN ('pending', 'running')" in sql


def test_sqlite_index_definition_carries_the_predicate(settings: Settings, engine: Engine) -> None:
    """Koşulu kaybolmuş bir index bütün PLAYBOOK geçmişini tek satıra indirirdi."""
    _upgrade(settings)
    with engine.connect() as connection:
        definition = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
            {"name": ACTIVE_PLAYBOOK_INDEX},
        ).scalar_one()

    assert "UNIQUE INDEX" in definition
    assert "job_type = 'playbook'" in definition
    assert "status IN ('pending', 'running')" in definition


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("pending", "pending", id="pending+pending"),
        pytest.param("pending", "running", id="pending+running"),
        pytest.param("running", "pending", id="running+pending"),
        pytest.param("running", "running", id="running+running"),
    ],
)
def test_database_refuses_a_second_active_playbook_job(
    settings: Settings, engine: Engine, first: str, second: str
) -> None:
    """Global sınır 1'dir: ikinci etkin PLAYBOOK satırı hiçbir bileşimde geçmez.

    İkinci satır bilinçli olarak **başka bir inventory** kullanır: sınır ping'in
    aksine inventory başına değil **global**dir ve farklı bir inventory seçmek
    onu delmemelidir.
    """
    _upgrade(settings)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_project_and_inventory(connection, now)
        _insert_playbook_job(
            connection,
            status=first,
            inventory_id=1,
            now=now,
            **(_owned(now) if first == "running" else {}),  # type: ignore[arg-type]
        )

    with pytest.raises(IntegrityError, match=ACTIVE_PLAYBOOK_VIOLATION):
        with engine.begin() as connection:
            _insert_playbook_job(
                connection,
                status=second,
                inventory_id=2,
                now=now,
                **(_owned(now) if second == "running" else {}),  # type: ignore[arg-type]
            )


@pytest.mark.parametrize("terminal", ["successful", "failed", "canceled"])
def test_a_new_pending_job_is_accepted_after_a_terminal_one(
    settings: Settings, engine: Engine, terminal: str
) -> None:
    """Sınır meşru işi engellemez: terminal Job predicate'in dışına çıkar."""
    _upgrade(settings)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_project_and_inventory(connection, now)
        job_id = _insert_playbook_job(connection, status="pending", now=now)

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET status = :status, finished_at = :now WHERE id = :id"),
            {"status": terminal, "now": now, "id": job_id},
        )

    with engine.begin() as connection:
        _insert_playbook_job(connection, status="pending", inventory_id=2, now=now)

    with engine.connect() as connection:
        active = connection.execute(
            text(
                "SELECT COUNT(*) FROM jobs WHERE job_type = 'playbook' "
                "AND status IN ('pending', 'running')"
            )
        ).scalar_one()
    assert active == 1


def test_the_ping_index_and_authorization_checks_survive(
    settings: Settings, engine: Engine
) -> None:
    """0007 batch mode tabloyu yeniden kurar; önceki invariantlar korunmalı."""
    _upgrade(settings)

    with engine.connect() as connection:
        ping_index = connection.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = 'uq_jobs_active_ping_inventory'"
            )
        ).scalar_one()

    assert "UNIQUE INDEX" in ping_index
    assert "job_type = 'ping'" in ping_index
    assert "status IN ('pending', 'running')" in ping_index

    checks = {check["name"] for check in inspect(engine).get_check_constraints("jobs")}
    assert {
        "ck_jobs_active_playbook_is_authorized",
        "ck_jobs_ping_has_no_execution_plan",
        "ck_jobs_running_has_started_at",
    } <= checks


# --- Ownership / lease invariantları -----------------------------------------


@pytest.mark.parametrize(
    "missing",
    ["worker_id", "heartbeat_at", "lease_expires_at"],
)
def test_running_playbook_job_must_carry_its_lease(
    settings: Settings, engine: Engine, missing: str
) -> None:
    """Sahipsiz bir `running` satır kabul edilmez.

    Alanlar boş kalabilseydi, çökmüş bir worker'ın bıraktığı `running` satır ile
    canlı bir execution'ı veritabanı içinde ayırt etmenin hiçbir yolu olmazdı.
    """
    _upgrade(settings)
    now = datetime.now(UTC)
    ownership = _owned(now)
    ownership[missing] = None

    with engine.begin() as connection:
        _seed_project_and_inventory(connection, now)

    with pytest.raises(IntegrityError, match="ck_jobs_running_playbook_has_lease"):
        with engine.begin() as connection:
            _insert_playbook_job(connection, status="running", now=now, **ownership)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["pending", "successful", "failed", "canceled"])
@pytest.mark.parametrize("field", ["worker_id", "heartbeat_at", "lease_expires_at"])
def test_idle_playbook_job_cannot_carry_ownership(
    settings: Settings, engine: Engine, status: str, field: str
) -> None:
    """`pending` veya terminal bir Job'ta duran sahiplik alanı reddedilir.

    Böyle bir alan, sahibi olmayan bir kirayı canlıymış gibi gösterirdi.
    """
    _upgrade(settings)
    now = datetime.now(UTC)
    values: dict[str, object] = {field: str(uuid.uuid4()) if field == "worker_id" else now}

    with engine.begin() as connection:
        _seed_project_and_inventory(connection, now)

    with pytest.raises(IntegrityError, match="ck_jobs_idle_playbook_has_no_lease"):
        with engine.begin() as connection:
            _insert_playbook_job(connection, status=status, now=now, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["worker_id", "heartbeat_at", "lease_expires_at"])
def test_ping_job_cannot_carry_ownership(settings: Settings, engine: Engine, field: str) -> None:
    """Ping'in worker'ı ve kirası yoktur; iki sahiplik modeli karışamaz."""
    _upgrade(settings)
    now = datetime.now(UTC)
    value: object = str(uuid.uuid4()) if field == "worker_id" else now

    with engine.begin() as connection:
        _seed_project_and_inventory(connection, now)

    with pytest.raises(IntegrityError, match="ck_jobs_ping_has_no_lease"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO jobs (id, job_type, status, inventory_id, requested_by, "
                    f"created_at, result_truncated, {field}) "
                    "VALUES (:id, 'ping', 'pending', 1, 'actor', :now, 0, :value)"
                ),
                {"id": str(uuid.uuid4()), "now": now, "value": value},
            )


@pytest.mark.parametrize(
    "offset_seconds",
    [
        pytest.param(0, id="lease==heartbeat"),
        pytest.param(-1, id="lease<heartbeat"),
        pytest.param(-3600, id="lease-long-expired"),
    ],
)
def test_running_playbook_lease_must_outlive_its_heartbeat(
    settings: Settings, engine: Engine, offset_seconds: int
) -> None:
    """Yazıldığı anda süresi geçmiş bir kira kabul edilmez.

    Alanların dolu olması yetmez: ``lease_expires_at <= heartbeat_at`` taşıyan
    bir satır, canlı bir execution'ı terk edilmiş gösterir ve işin ikinci bir
    worker tarafından devralınmasına yol açardı — concurrency=1 sınırının
    veritabanında durmasının anlamı da kalmazdı.
    """
    _upgrade(settings)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_project_and_inventory(connection, now)

    with pytest.raises(IntegrityError, match="ck_jobs_running_playbook_lease_outlives_heartbeat"):
        with engine.begin() as connection:
            _insert_playbook_job(
                connection,
                status="running",
                now=now,
                worker_id=str(uuid.uuid4()),
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=offset_seconds),
            )


def test_a_lease_that_outlives_the_heartbeat_is_accepted(
    settings: Settings, engine: Engine
) -> None:
    """Kısıt meşru işi engellemez: ileri tarihli kira kabul edilir."""
    _upgrade(settings)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_project_and_inventory(connection, now)
        job_id = _insert_playbook_job(
            connection,
            status="running",
            now=now,
            worker_id=str(uuid.uuid4()),
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=1),
        )

    with engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT heartbeat_at, lease_expires_at FROM jobs WHERE id = :id"),
                {"id": job_id},
            )
            .mappings()
            .one()
        )
    assert row["lease_expires_at"] > row["heartbeat_at"]


def test_the_lease_ordering_check_does_not_touch_ping(settings: Settings, engine: Engine) -> None:
    """Ping satırları bu kısıtın kapsamı dışındadır ve davranışı değişmez.

    Ping'in kirası yoktur; alanları zaten ``ck_jobs_ping_has_no_lease`` ile
    boş tutulur, dolayısıyla sıralama kısıtı onu hiç bağlamaz.
    """
    _upgrade(settings)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_project_and_inventory(connection, now)
        connection.execute(
            text(
                "INSERT INTO jobs (id, job_type, status, inventory_id, requested_by, "
                "created_at, started_at, result_truncated) "
                "VALUES (:id, 'ping', 'running', 1, 'actor', :now, :now, 0)"
            ),
            {"id": str(uuid.uuid4()), "now": now},
        )

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM jobs WHERE job_type = 'ping' AND status = 'running'")
            ).scalar_one()
            == 1
        )


def test_a_fully_owned_running_playbook_job_is_accepted(settings: Settings, engine: Engine) -> None:
    """Kısıtlar meşru işi engellemez: eksiksiz sahiplik taşıyan satır kabul edilir."""
    _upgrade(settings)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_project_and_inventory(connection, now)
        job_id = _insert_playbook_job(connection, status="running", now=now, **_owned(now))  # type: ignore[arg-type]

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT status, worker_id, heartbeat_at, lease_expires_at, "
                    "result_truncated, error_code FROM jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
            .mappings()
            .one()
        )

    assert row["status"] == "running"
    assert row["worker_id"] is not None
    assert row["heartbeat_at"] is not None
    assert row["lease_expires_at"] is not None
    assert row["result_truncated"] == 0
    assert row["error_code"] is None


def test_result_truncated_cannot_be_null(settings: Settings, engine: Engine) -> None:
    """Eksik bir sonuç, tam sonuç gibi okunabilir olmamalıdır."""
    _upgrade(settings)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_project_and_inventory(connection, now)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO jobs (id, job_type, status, inventory_id, requested_by, "
                    "created_at, result_truncated) "
                    "VALUES (:id, 'ping', 'pending', 1, 'actor', :now, NULL)"
                ),
                {"id": str(uuid.uuid4()), "now": now},
            )


# --- Eski veri ---------------------------------------------------------------


def test_legacy_active_playbook_jobs_are_closed_with_a_reason(
    settings: Settings, engine: Engine
) -> None:
    """Yükseltmeden önce kalan etkin satırlar kendiliğinden çalıştırılamaz olur.

    Arkalarında çalışan hiçbir süreç yoktur; onları `pending` bırakmak,
    operatörün onaylamadığı bir anda kendiliğinden başlayan bir execution
    üretirdi. Kayıt silinmez, terminal `failed` yapılır ve sebebi yazılır.
    """
    _upgrade(settings, PREVIOUS_REVISION)
    created = datetime.now(UTC)
    started = created + timedelta(minutes=1)

    identifiers: dict[str, str] = {}
    with engine.begin() as connection:
        _seed_project_and_inventory(connection, created)
        identifiers["pending-job"] = _insert_legacy_playbook_job(
            connection, status="pending", inventory_id=1, now=created
        )
        identifiers["running-job"] = _insert_legacy_playbook_job(
            connection, status="running", inventory_id=2, now=created
        )
        # Zaten terminal olan satır: dokunulmamalı. Terminal satırlar
        # authorization CHECK'inin dışındadır, bu yüzden plan bağı taşımaz.
        identifiers["done-job"] = str(uuid.uuid4())
        identifiers["ping-job"] = str(uuid.uuid4())
        connection.execute(
            text(
                "INSERT INTO jobs (id, job_type, status, inventory_id, project_id, "
                "playbook_path, limit_pattern, requested_by, started_at, created_at) "
                "VALUES (:id, 'playbook', 'successful', 1, 1, 'site.yml', NULL, "
                "'eski-aktor', :started, :created)"
            ),
            {"id": identifiers["done-job"], "started": started, "created": created},
        )
        # Ping akışı etkilenmemeli.
        connection.execute(
            text(
                "INSERT INTO jobs (id, job_type, status, inventory_id, requested_by, created_at) "
                "VALUES (:id, 'ping', 'pending', 1, 'eski-aktor', :created)"
            ),
            {"id": identifiers["ping-job"], "created": created},
        )

    _upgrade(settings)

    with engine.connect() as connection:
        jobs = {
            row["id"]: row
            for row in connection.execute(
                text(
                    "SELECT id, status, error_code, finished_at, result_truncated, "
                    "worker_id, heartbeat_at, lease_expires_at FROM jobs"
                )
            )
            .mappings()
            .all()
        }

    for name in ("pending-job", "running-job"):
        closed = jobs[identifiers[name]]
        assert closed["status"] == "failed", name
        assert closed["error_code"] == INTERRUPTED_BY_UPGRADE, name
        assert closed["finished_at"] is not None, name
        # Arkalarında hiçbir worker yoktur; sahiplik alanları boş kalır.
        assert closed["worker_id"] is None, name
        assert closed["heartbeat_at"] is None, name
        assert closed["lease_expires_at"] is None, name

    # Geçmiş kayıtlar ve ping akışı etkilenmez.
    assert jobs[identifiers["done-job"]]["status"] == "successful"
    assert jobs[identifiers["done-job"]]["error_code"] is None
    assert jobs[identifiers["ping-job"]]["status"] == "pending"

    # Yeni sütun mevcut satırlarda da NOT NULL sözleşmesini karşılar.
    assert all(row["result_truncated"] == 0 for row in jobs.values())


def test_two_legacy_active_playbook_jobs_do_not_block_the_upgrade(
    settings: Settings, engine: Engine
) -> None:
    """Yeni index eklenmeden önce eski satırların hepsi kapatılmalıdır.

    Temizlik index'ten sonra yapılsaydı, ikiden fazla etkin legacy satır taşıyan
    bir kurulumda yükseltme düşer ve elle veri düzeltmesine bağımlı hâle
    gelirdi.
    """
    _upgrade(settings, PREVIOUS_REVISION)
    created = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_project_and_inventory(connection, created)
        # İkisi de 0006 şemasında geçerlidir: global sınır henüz yoktur.
        _insert_legacy_playbook_job(connection, status="pending", inventory_id=1, now=created)
        _insert_legacy_playbook_job(connection, status="running", inventory_id=2, now=created)

    _upgrade(settings)

    with engine.connect() as connection:
        statuses = connection.execute(
            text("SELECT status, error_code FROM jobs WHERE job_type = 'playbook'")
        ).all()

    assert [(status, code) for status, code in statuses] == [
        ("failed", INTERRUPTED_BY_UPGRADE),
        ("failed", INTERRUPTED_BY_UPGRADE),
    ]


def test_the_migration_reason_matches_the_documented_code() -> None:
    """Sebep kodu, backlog'daki sabit hata kodu listesiyle aynı olmalıdır."""
    settings = make_settings()
    assert settings.playbook_worker_enabled is False
    assert INTERRUPTED_BY_UPGRADE == "interrupted_by_upgrade"
