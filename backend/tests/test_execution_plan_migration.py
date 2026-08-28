"""Migration 0006 — aktör ve Job bağı (R1-V3A).

İki iddia ölçülür:

1. *Şema.* ``execution_plans.requested_by`` ``NOT NULL``'dır;
   ``jobs.execution_plan_id`` nullable, unique ve ``RESTRICT`` FK'dir. Ping'in
   kısmi unique index'i batch mode tablo yeniden kurulumundan sağ çıkar.
2. *Eski veri.* Aktör bağı olmadan (R1-V2) hazırlanmış satırlar claim
   edilemez hâle gelir: internal bir sentinel'e bağlanır ve ``expired``
   yapılır. Sentinel hiçbir ``local_actor`` değeri olamaz, bu yüzden o satırlar
   hiçbir claim koşuluyla eşleşemez.

``up → down → up`` şemayı aynı yere getirmelidir: bir migration'ı geri almanın
imkânsız olduğunu üretimde fark etmek istenmez.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import Engine, Inspector, inspect, text

from app.core.config import INTERNAL_ACTOR_PREFIX, LEGACY_PLAN_ACTOR, Settings
from app.db.session import create_db_engine
from tests.support import alembic_config, make_settings

PREVIOUS_REVISION = "0005_create_execution_plans_table"

# Migration'ın yazdığı değer; `app.core.config.LEGACY_PLAN_ACTOR` ile aynıdır.
# İkisinin ayrışması, eski satırların artık sentinel'e bağlanmadığı anlamına
# gelirdi — bu yüzden burada açıkça karşılaştırılır.
MIGRATION_SENTINEL = "__legacy_unattributed_plan__"


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


def _columns(inspector: Inspector, table: str) -> dict[str, bool]:
    """Sütun adı → nullable."""
    return {column["name"]: bool(column["nullable"]) for column in inspector.get_columns(table)}


def _snapshot(engine: Engine) -> dict[str, object]:
    """Karşılaştırılabilir bir şema özeti."""
    inspector = inspect(engine)
    return {
        table: {
            "columns": _columns(inspector, table),
            "indexes": {
                (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                for index in inspector.get_indexes(table)
            },
            "unique": {
                (constraint["name"], tuple(constraint["column_names"]))
                for constraint in inspector.get_unique_constraints(table)
            },
            "foreign_keys": {
                (
                    key["referred_table"],
                    tuple(key["constrained_columns"]),
                    str(key.get("options", {}).get("ondelete")),
                )
                for key in inspector.get_foreign_keys(table)
            },
            # CHECK'ler de karşılaştırılır: downgrade onları bırakıp giderse
            # ikinci upgrade "aynı şema" görünürken invariant kaybolurdu.
            "checks": {
                (check["name"], " ".join(str(check["sqltext"]).split()))
                for check in inspector.get_check_constraints(table)
            },
        }
        for table in ("execution_plans", "jobs")
    }


def test_upgrade_adds_the_actor_and_job_binding(settings: Settings, engine: Engine) -> None:
    """Aktör sütunu ``NOT NULL``; plan bağı nullable, unique ve ``RESTRICT``."""
    _upgrade(settings)
    inspector = inspect(engine)

    assert _columns(inspector, "execution_plans")["requested_by"] is False
    assert _columns(inspector, "jobs")["execution_plan_id"] is True

    plan_binding = next(
        key
        for key in inspector.get_foreign_keys("jobs")
        if key["constrained_columns"] == ["execution_plan_id"]
    )
    assert plan_binding["referred_table"] == "execution_plans"
    assert plan_binding["referred_columns"] == ["id"]
    assert plan_binding["options"]["ondelete"] == "RESTRICT"

    unique_columns = [
        constraint["column_names"] for constraint in inspector.get_unique_constraints("jobs")
    ]
    assert ["execution_plan_id"] in unique_columns

    # Bağ yalnız belge değeri taşımaz; iki CHECK ile veritabanında uygulanır.
    checks = {check["name"] for check in inspector.get_check_constraints("jobs")}
    assert {
        "ck_jobs_active_playbook_is_authorized",
        "ck_jobs_ping_has_no_execution_plan",
    } <= checks


def test_partial_ping_index_survives_the_table_rebuild(settings: Settings, engine: Engine) -> None:
    """Batch mode tabloyu yeniden kurar; ping'in kısmi unique index'i korunmalı.

    Index'in adı kadar ``WHERE`` yan tümcesi de önemlidir: koşulu kaybolmuş bir
    index, inventory başına **tek** Job'a izin verir ve ping akışını bozardı.
    """
    _upgrade(settings)

    with engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = 'uq_jobs_active_ping_inventory'"
            )
        ).scalar_one()

    assert "UNIQUE INDEX" in definition
    assert "job_type = 'ping'" in definition
    assert "status IN ('pending', 'running')" in definition


def test_migration_round_trip_restores_the_same_schema(settings: Settings, engine: Engine) -> None:
    """``up → down → up`` şemayı aynı yere getirir."""
    _upgrade(settings)
    before = _snapshot(engine)

    _downgrade(settings, PREVIOUS_REVISION)

    inspector = inspect(engine)
    assert "requested_by" not in _columns(inspector, "execution_plans")
    assert "execution_plan_id" not in _columns(inspector, "jobs")
    # Önceki şemanın geri kalanı yerinde durur.
    assert "execution_plans" in inspector.get_table_names()
    assert "token_hash" in _columns(inspector, "execution_plans")

    _upgrade(settings)
    assert _snapshot(engine) == before


def test_legacy_rows_become_unclaimable(settings: Settings, engine: Engine) -> None:
    """Aktör bağı olmayan satırlar sentinel'e bağlanır ve ``expired`` yapılır.

    Gerçek bir aktör *uydurmak*, doğrulanmamış bir kimliğe doğrulanmış görünüm
    vermek olurdu. Bunun yerine hiçbir ``local_actor`` değerinin alamayacağı bir
    sentinel yazılır; durum geçişi de o satırları claim koşulunun dışına atar.
    """
    _upgrade(settings, PREVIOUS_REVISION)
    created = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, name, path, path_key, is_active, "
                "created_at, updated_at) VALUES (1, 'Web', '/tmp/p', '/tmp/p', 1, :now, :now)"
            ),
            {"now": created},
        )
        connection.execute(
            text(
                "INSERT INTO inventories (id, project_id, name, path, source_type, "
                "created_at, updated_at) "
                "VALUES (1, 1, 'Prod', '/tmp/p/hosts.ini', 'ini', :now, :now)"
            ),
            {"now": created},
        )
        for index, status in enumerate(("prepared", "claimed", "expired")):
            connection.execute(
                text(
                    "INSERT INTO execution_plans (id, token_hash, project_id, inventory_id, "
                    "playbook_path, input_fingerprint, workspace_id, manifest_digest, status, "
                    "created_at, expires_at, claimed_at) VALUES "
                    "(:id, :token_hash, 1, 1, 'site.yml', :fingerprint, :workspace_id, "
                    ":digest, :status, :created, :expires, :claimed)"
                ),
                {
                    "id": f"0000000{index}-0000-4000-8000-000000000000",
                    "token_hash": f"{index}" * 64,
                    "fingerprint": f"{index}" * 64,
                    "workspace_id": f"1000000{index}-0000-4000-8000-000000000000",
                    "digest": f"{index}" * 64,
                    "status": status,
                    "created": created,
                    "expires": created + timedelta(hours=1),
                    "claimed": created if status == "claimed" else None,
                },
            )

    _upgrade(settings)

    with engine.connect() as connection:
        rows = (
            connection.execute(text("SELECT status, requested_by FROM execution_plans ORDER BY id"))
            .mappings()
            .all()
        )

    assert [row["requested_by"] for row in rows] == [MIGRATION_SENTINEL] * 3
    # Hiçbiri claim edilebilir bir durumda kalmaz.
    assert {row["status"] for row in rows} == {"expired"}


def test_legacy_active_playbook_jobs_are_closed_instead_of_faked(
    settings: Settings, engine: Engine
) -> None:
    """Yetkilendirilmemiş etkin PLAYBOOK satırları terminal duruma alınır.

    İki seçenek de reddedilir: satırlara uydurma bir execution planı bağlamak,
    onaylanmamış bir işe onaylanmış görünüm verirdi; migration'ı düşürmek ise
    yükseltmeyi elle veri düzeltmesine bağımlı kılardı. İş hiç çalıştırılmamış
    sayılarak `failed` yapılır ve kayıt tarihsel iz olarak korunur.
    """
    _upgrade(settings, PREVIOUS_REVISION)
    created = datetime.now(UTC)
    started = created + timedelta(minutes=1)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, name, path, path_key, is_active, "
                "created_at, updated_at) VALUES (1, 'Web', '/tmp/p', '/tmp/p', 1, :now, :now)"
            ),
            {"now": created},
        )
        connection.execute(
            text(
                "INSERT INTO inventories (id, project_id, name, path, source_type, "
                "created_at, updated_at) "
                "VALUES (1, 1, 'Prod', '/tmp/p/hosts.ini', 'ini', :now, :now)"
            ),
            {"now": created},
        )
        rows = [
            ("pending-job", "playbook", "pending", None),
            ("running-job", "playbook", "running", started),
            # Zaten terminal olan legacy satır: dokunulmamalı.
            ("done-job", "playbook", "successful", started),
            # Ping akışı etkilenmemeli.
            ("ping-job", "ping", "pending", None),
        ]
        for name, job_type, status, start in rows:
            connection.execute(
                text(
                    "INSERT INTO jobs (id, job_type, status, inventory_id, project_id, "
                    "playbook_path, limit_pattern, requested_by, started_at, created_at) "
                    "VALUES (:id, :job_type, :status, 1, 1, :playbook, NULL, 'eski-aktor', "
                    ":started, :created)"
                ),
                {
                    "id": _uuid_for(name),
                    "job_type": job_type,
                    "status": status,
                    "playbook": "site.yml" if job_type == "playbook" else None,
                    "started": start,
                    "created": created,
                },
            )

    _upgrade(settings)

    with engine.connect() as connection:
        jobs = {
            row["id"]: row
            for row in connection.execute(
                text("SELECT id, status, execution_plan_id, finished_at FROM jobs")
            )
            .mappings()
            .all()
        }

    for name in ("pending-job", "running-job"):
        closed = jobs[_uuid_for(name)]
        assert closed["status"] == "failed", name
        assert closed["finished_at"] is not None, name
        # Sahte bir plan bağlanmaz: iş yetkilendirilmemiş kalır.
        assert closed["execution_plan_id"] is None, name

    assert jobs[_uuid_for("done-job")]["status"] == "successful"
    assert jobs[_uuid_for("ping-job")]["status"] == "pending"
    assert jobs[_uuid_for("ping-job")]["execution_plan_id"] is None


def _uuid_for(name: str) -> str:
    """Ada göre kararlı, canonical bir UUID4 üretir (test verisi okunabilir kalsın)."""
    digest = sha256(name.encode("utf-8")).hexdigest()
    return str(UUID(digest[:32], version=4))


def test_the_sentinel_can_never_be_a_configured_actor() -> None:
    """Sentinel, yapılandırmadan gelen bir aktörle **çakışamaz**.

    Çakışabilseydi, sentinel'e bağlanmış eski satırlar gerçek bir kullanıcı
    isteğiyle eşleşebilir hâle gelirdi. Değer sessizce düzeltilmez, reddedilir.
    """
    assert LEGACY_PLAN_ACTOR == MIGRATION_SENTINEL
    assert LEGACY_PLAN_ACTOR.startswith(INTERNAL_ACTOR_PREFIX)

    for candidate in (LEGACY_PLAN_ACTOR, f"{INTERNAL_ACTOR_PREFIX}baska"):
        with pytest.raises(ValueError, match=INTERNAL_ACTOR_PREFIX):
            make_settings(local_actor=candidate)
