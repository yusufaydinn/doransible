"""Migration 0008 — kalıcı execution mode veri sözleşmesi (R1-V3H1A).

Mode'un tek karşılığı `ansible-runner` argv'sidir: ``check``, argv'ye
``--cmdline=--check`` **eklenen** mode'dur; ``normal``, eklenmeyen mode'dur.
Bu dosyadaki hiçbir test ``check``'in yan etkisiz olduğunu veya hedefte
değişiklik üretmediğini ölçmez — böyle bir garanti verilmemektedir.

Dört iddia ölçülür:

1. *Ortak tip.* ``ExecutionMode`` tam olarak ``check`` ve ``normal`` üyelerini
   taşır ve **iki tablo da aynı tanımı** kullanır. Plan ile Job iki ayrı
   string union taşısaydı, biri genişletildiğinde diğeri sessizce eski kalır
   ve "aynı mode" iddiası tip düzeyinde hiçbir şey ifade etmezdi.
2. *Şema.* Sütun iki tabloda da ``NOT NULL``'dır, ``check``/``normal`` dışına
   çıkamaz ve varsayılanı ``check``'tir. PING satırları yalnız ``check``
   taşıyabilir; PLAYBOOK satırları iki mode'u da taşıyabilir.
3. *Eski veri.* Yükseltme mevcut satırların **yalnız** yeni sütununa dokunur.
   Snapshot, iki tablonun ``mode`` dışındaki **bütün** veri sütunlarını
   upgrade öncesi/sonrası karşılaştırır ve satırlar bunu anlamlı kılacak
   biçimde ayırt edilebilir non-default değerlerle seed edilir.
4. *Kapsam.* Veritabanı artık ``normal`` değerini temsil edebilir. Bu dosyanın
   yazıldığı turda (R1-V3H1A) üretim kodu hâlâ yalnız ``check`` üretiyordu;
   R1-V3H2A'dan beri public plan/prepare/launch API'si istemcinin seçtiği
   kipi (``check`` veya ``normal``) kabul eder ve taşır — aşağıdaki 5.
   bölüm artık bunu ölçer. Runner argv'si kipi R1-V3H1B2B'den beri okur.

Mode testleri bilinçli olarak **ham SQL** ile yazılır. ORM üzerinden yazmak,
ihlali Python tarafında yakalanabilir kılar ve asıl soruyu — *doğrudan
veritabanına yazılan bir satır da reddediliyor mu* — yanıtsız bırakırdı.

Geçersiz mode testlerinde kullanılan sabit, enum üyesi **olmayan** ayırt
edilebilir bir sentinel değerdir; execution mode sözleşmesi hakkında bir iddia
taşımaz ve o satırların tek işi CHECK constraint tarafından reddedilmektir.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, Inspector, inspect, text
from sqlalchemy.engine.interfaces import ReflectedColumn
from sqlalchemy.exc import IntegrityError, StatementError

from app.core.config import Settings
from app.db.session import create_db_engine
from app.models import ExecutionMode
from tests.support import alembic_config

PREVIOUS_REVISION = "0007_add_playbook_runner_foundation"

MODE_TABLES = ("execution_plans", "jobs")

# Migration'ın yazdığı backfill değeri. Migration geçmişi dondurulmuş bir
# kayıttır; uygulama enum'u ileride genişlese bile bu değer değişmemelidir.
BACKFILL_MODE = "check"


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


def _digest() -> str:
    """64 karakterlik, benzersiz bir sahte özet."""
    return uuid.uuid4().hex + uuid.uuid4().hex


def _seed_projects_and_inventories(connection: Connection, now: datetime) -> None:
    """Plan ve Job satırlarının FK'lerini karşılayan kayıtlar.

    İki project ve iki inventory kurulur: legacy snapshot testinin
    ``project_id``/``inventory_id`` sütunlarını gerçekten ölçebilmesi için
    satırların hepsinin aynı FK'yi taşımaması gerekir.
    """
    for project_id, name in ((1, "Web"), (2, "Db")):
        connection.execute(
            text(
                "INSERT INTO projects (id, name, path, path_key, is_active, "
                "created_at, updated_at) "
                "VALUES (:id, :name, :path, :path, 1, :now, :now)"
            ),
            {"id": project_id, "name": name, "path": f"/tmp/p{project_id}", "now": now},
        )
    for inventory_id, project_id, name in ((1, 1, "Prod"), (2, 2, "Test")):
        connection.execute(
            text(
                "INSERT INTO inventories (id, project_id, name, path, source_type, "
                "created_at, updated_at) "
                "VALUES (:id, :project, :name, :path, 'ini', :now, :now)"
            ),
            {
                "id": inventory_id,
                "project": project_id,
                "name": name,
                "path": f"/tmp/p{project_id}/{name.lower()}.ini",
                "now": now,
            },
        )


def _insert_row(connection: Connection, table: str, values: dict[str, object]) -> None:
    """Sütun adlarını verilen sözlükten kurup tek satır yazar.

    ``mode`` yalnız sözlükte varsa anılır; 0008 **öncesi** şemaya yazan
    çağrılar onu hiç vermez.
    """
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    connection.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"), values)


def _insert_plan(
    connection: Connection,
    *,
    status: str,
    now: datetime,
    claimed: bool,
    mode: str | None = None,
    project_id: int = 1,
    inventory_id: int = 1,
    playbook_path: str = "site.yml",
    requested_by: str = "actor",
    ttl: timedelta = timedelta(hours=1),
) -> str:
    """Bir execution plan satırı yazar; ``mode`` verilmezse sütun hiç anılmaz."""
    plan_id = str(uuid.uuid4())
    values: dict[str, object] = {
        "id": plan_id,
        "token_hash": _digest(),
        "project_id": project_id,
        "inventory_id": inventory_id,
        "playbook_path": playbook_path,
        "requested_by": requested_by,
        "input_fingerprint": _digest(),
        "workspace_id": str(uuid.uuid4()),
        "manifest_digest": _digest(),
        "status": status,
        "created_at": now,
        "expires_at": now + ttl,
        "claimed_at": now if claimed else None,
    }
    if mode is not None:
        values["mode"] = mode
    _insert_row(connection, "execution_plans", values)
    return plan_id


def _insert_ping_job(
    connection: Connection,
    *,
    status: str,
    now: datetime,
    mode: str | None = None,
    inventory_id: int = 1,
    project_id: int | None = None,
    requested_by: str = "actor",
    artifact_path: str | None = None,
    return_code: int | None = None,
    error_code: str | None = None,
    result_truncated: int = 0,
) -> str:
    """Bir PING Job satırı yazar.

    Ping'in planı ve kirası yoktur (``ck_jobs_ping_has_no_execution_plan``,
    ``ck_jobs_ping_has_no_lease``); o sütunlar bilinçli olarak hiç doldurulmaz.
    """
    job_id = str(uuid.uuid4())
    terminal = status in ("successful", "failed", "canceled")
    values: dict[str, object] = {
        "id": job_id,
        "job_type": "ping",
        "status": status,
        "inventory_id": inventory_id,
        "project_id": project_id,
        "requested_by": requested_by,
        "artifact_path": artifact_path,
        "return_code": return_code,
        "error_code": error_code,
        "result_truncated": result_truncated,
        "started_at": now if status == "running" or terminal else None,
        "finished_at": now if terminal else None,
        "created_at": now,
    }
    if mode is not None:
        values["mode"] = mode
    _insert_row(connection, "jobs", values)
    return job_id


def _insert_playbook_job(
    connection: Connection,
    *,
    status: str,
    now: datetime,
    plan_id: str,
    mode: str | None = None,
    inventory_id: int = 1,
    project_id: int = 1,
    playbook_path: str = "site.yml",
    limit_pattern: str | None = None,
    requested_by: str = "actor",
    artifact_path: str | None = None,
    return_code: int | None = None,
    error_code: str | None = None,
    result_truncated: int = 0,
    worker_id: str | None = None,
    heartbeat_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
) -> str:
    """Yetkilendirilmiş bir PLAYBOOK Job satırı yazar.

    Plan bağı gerçekten kurulur (``ck_jobs_active_playbook_is_authorized``);
    yarım bir satır üretmek, ölçülmek istenen invariant yerine başka bir
    kısıtın tetiklenmesine yol açardı.
    """
    job_id = str(uuid.uuid4())
    terminal = status in ("successful", "failed", "canceled")
    values: dict[str, object] = {
        "id": job_id,
        "job_type": "playbook",
        "status": status,
        "inventory_id": inventory_id,
        "project_id": project_id,
        "playbook_path": playbook_path,
        "execution_plan_id": plan_id,
        "limit_pattern": limit_pattern,
        "requested_by": requested_by,
        "artifact_path": artifact_path,
        "return_code": return_code,
        "error_code": error_code,
        "result_truncated": result_truncated,
        "worker_id": worker_id,
        "heartbeat_at": heartbeat_at,
        "lease_expires_at": lease_expires_at,
        "started_at": now if status == "running" or terminal else None,
        "finished_at": now if terminal else None,
        "created_at": now,
    }
    if mode is not None:
        values["mode"] = mode
    _insert_row(connection, "jobs", values)
    return job_id


# --- 1. Ortak tip ------------------------------------------------------------


def test_execution_mode_has_exactly_check_and_normal() -> None:
    """Enum tam olarak iki üye taşır ve değerleri sütuna yazılan stringlerdir."""
    assert [member.value for member in ExecutionMode] == ["check", "normal"]
    assert ExecutionMode.CHECK == "check"
    assert ExecutionMode.NORMAL == "normal"


def test_execution_mode_is_importable_from_the_model_surface() -> None:
    """Ortak tip model public yüzeyinden okunur; iki ayrı tanım yoktur."""
    import app.models as models
    from app.models.execution_mode import ExecutionMode as CanonicalExecutionMode

    assert "ExecutionMode" in models.__all__
    assert models.ExecutionMode is CanonicalExecutionMode


def test_plan_and_job_share_one_mode_type() -> None:
    """İki sütun **aynı** enum tanımına yaslanır.

    Ayrı tanımlar biri genişletildiğinde diğerini sessizce eski bırakırdı; o
    hâlde plan ile Job'ın aynı kipi taşıdığı iddiası tip düzeyinde hiçbir şey
    ifade etmezdi.
    """
    from sqlalchemy import Enum as SqlEnum

    from app.models import ExecutionPlanRecord, Job

    plan_type = ExecutionPlanRecord.__table__.c.mode.type
    job_type = Job.__table__.c.mode.type
    assert isinstance(plan_type, SqlEnum)
    assert isinstance(job_type, SqlEnum)

    assert plan_type.enum_class is ExecutionMode
    assert job_type.enum_class is ExecutionMode
    assert plan_type.enums == job_type.enums == ["check", "normal"]
    assert plan_type.name == job_type.name == "execution_mode"


# --- 2. Şema -----------------------------------------------------------------


def _mode_column(inspector: Inspector, table: str) -> ReflectedColumn:
    return next(column for column in inspector.get_columns(table) if column["name"] == "mode")


@pytest.mark.parametrize("table", MODE_TABLES)
def test_mode_is_not_null_with_a_check_server_default(
    settings: Settings, engine: Engine, table: str
) -> None:
    """Sütun iki tabloda da zorunludur ve sunucu varsayılanı ``check``'tir.

    Nullable olsaydı "mode belirtilmedi" ile "``--check`` ile çalıştırıldı"
    aynı değere düşerdi ve mode'a bakan hiçbir sorgu ikisini ayırt edemezdi.
    Varsayılanın ``check`` olması bir yan etkisizlik garantisi değil, ``normal``
    mode'a sessiz yükseltmenin engellenmesidir.
    """
    _upgrade(settings)
    column = _mode_column(inspect(engine), table)

    assert column["nullable"] is False
    assert str(column["default"]).strip("'") == BACKFILL_MODE


@pytest.mark.parametrize("table", MODE_TABLES)
def test_database_refuses_an_unknown_mode(settings: Settings, engine: Engine, table: str) -> None:
    """İzin verilen değer kümesi uygulamada değil **veritabanında** durur."""
    _upgrade(settings)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_projects_and_inventories(connection, now)

    with pytest.raises(IntegrityError, match=f"ck_{table}_execution_mode"):
        with engine.begin() as connection:
            if table == "execution_plans":
                _insert_plan(connection, status="prepared", now=now, claimed=False, mode="dry-run")
            else:
                _insert_ping_job(connection, status="pending", now=now, mode="dry-run")


def test_orm_refuses_an_unknown_mode(settings: Settings, engine: Engine) -> None:
    """ORM yolu da aynı kümeyi uygular; geçersiz değer flush'a bile gitmez."""
    from sqlalchemy.orm import Session

    from app.models import Job, JobStatus, JobType

    _upgrade(settings)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        _seed_projects_and_inventories(connection, now)

    with Session(engine) as session:
        session.add(
            Job(
                id=str(uuid.uuid4()),
                job_type=JobType.PING,
                status=JobStatus.PENDING,
                mode="dry-run",
                inventory_id=1,
                requested_by="actor",
                created_at=now,
            )
        )
        with pytest.raises((StatementError, LookupError)):
            session.flush()
        session.rollback()


def test_database_refuses_a_ping_job_in_normal_mode(settings: Settings, engine: Engine) -> None:
    """Ping argv'sinde ``--check`` diye bir bayrak yoktur.

    Mode'un karşılığı yalnız `ansible-runner` playbook argv'sindedir; ping o
    yoldan geçmez ve sabit bir ad-hoc argv kurar. ``mode = 'normal'`` taşıyan
    bir ping satırı bu yüzden hiçbir argv farkına karşılık gelmez ve mode'a
    bakan her sorgu tarafından yanlış sınıflandırılırdı.
    """
    _upgrade(settings)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        _seed_projects_and_inventories(connection, now)

    with pytest.raises(IntegrityError, match="ck_jobs_ping_is_check_only"):
        with engine.begin() as connection:
            _insert_ping_job(connection, status="pending", now=now, mode="normal")


def test_database_accepts_a_playbook_job_in_normal_mode(settings: Settings, engine: Engine) -> None:
    """Şema ``normal`` mode'unu **temsil edebilir**: PLAYBOOK satırı kabul edilir.

    Satır bütün mevcut invariantları sağlar (plan bağı kurulur, `limit` boş
    kalır, kira alanları `pending` için boştur); reddedilirse bu, kipin değil
    başka bir kısıtın sonucu olurdu.
    """
    _upgrade(settings)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_projects_and_inventories(connection, now)
        plan_id = _insert_plan(connection, status="claimed", now=now, claimed=True, mode="normal")
        job_id = _insert_playbook_job(
            connection, status="pending", now=now, plan_id=plan_id, mode="normal"
        )

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT mode FROM jobs WHERE id = :id"), {"id": job_id}
            ).scalar_one()
            == "normal"
        )
        assert (
            connection.execute(
                text("SELECT mode FROM execution_plans WHERE id = :id"), {"id": plan_id}
            ).scalar_one()
            == "normal"
        )


@pytest.mark.parametrize("table", MODE_TABLES)
def test_mode_defaults_to_check_when_a_writer_omits_it(
    settings: Settings, engine: Engine, table: str
) -> None:
    """Mode'u anmayan bir INSERT ``check``'te kalır, ``normal``'a yükselmez."""
    _upgrade(settings)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        _seed_projects_and_inventories(connection, now)
        if table == "execution_plans":
            row_id = _insert_plan(connection, status="prepared", now=now, claimed=False)
        else:
            row_id = _insert_ping_job(connection, status="pending", now=now)

    with engine.connect() as connection:
        mode = connection.execute(
            text(f"SELECT mode FROM {table} WHERE id = :id"), {"id": row_id}
        ).scalar_one()
    assert mode == BACKFILL_MODE


def test_orm_constructors_still_produce_check_without_being_told(
    settings: Settings, engine: Engine
) -> None:
    """Mevcut constructor'lar kipi hiç anmaz ve davranış değiştirmez."""
    from sqlalchemy.orm import Session

    from app.models import Job, JobStatus, JobType

    _upgrade(settings)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        _seed_projects_and_inventories(connection, now)

    with Session(engine) as session:
        job = Job(
            id=str(uuid.uuid4()),
            job_type=JobType.PING,
            status=JobStatus.PENDING,
            inventory_id=1,
            requested_by="actor",
            created_at=now,
        )
        session.add(job)
        session.commit()
        assert job.mode is ExecutionMode.CHECK


# --- 3. Eski veri ------------------------------------------------------------


def _data_columns(engine: Engine, table: str) -> list[str]:
    """Tablonun ``mode`` **dışındaki** bütün sütunları, şemadan okunarak.

    Liste elle yazılsaydı, ileride eklenen bir sütun sessizce kapsam dışı
    kalır ve snapshot onu kaybeden bir migration'ı fark etmezdi.
    """
    return [
        column["name"] for column in inspect(engine).get_columns(table) if column["name"] != "mode"
    ]


def _rows(engine: Engine, table: str, columns: Sequence[str]) -> list[tuple[object, ...]]:
    """Verilen sütunları ``id`` sırasıyla, **ham** depolanmış değerleriyle okur.

    Sorgu ``text()`` ile kurulur ve ORM tipi devreye girmez; dolayısıyla
    karşılaştırma SQLite'ın sakladığı değerin kendisi üzerindedir (tarihler
    depolandıkları string biçiminde gelir). Değerleri Python tipine çevirip
    karşılaştırmak, biçimi değiştiren ama "eşdeğer" görünen bir dönüşümü
    gizlerdi.
    """
    selected = ", ".join(columns)
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(text(f"SELECT {selected} FROM {table} ORDER BY id"))
        ]


# Snapshot'ın **en az** kapsaması gereken sütunlar. Asıl karşılaştırma
# `_data_columns()` ile şemadan türetilir; bu kümeler yalnız türetmenin
# daraldığını fark etmek için durur.
REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "execution_plans": frozenset(
        {
            "id",
            "token_hash",
            "project_id",
            "inventory_id",
            "playbook_path",
            "requested_by",
            "input_fingerprint",
            "workspace_id",
            "manifest_digest",
            "status",
            "created_at",
            "expires_at",
            "claimed_at",
        }
    ),
    "jobs": frozenset(
        {
            "id",
            "job_type",
            "status",
            "inventory_id",
            "project_id",
            "playbook_path",
            "execution_plan_id",
            "limit_pattern",
            "requested_by",
            "artifact_path",
            "return_code",
            "error_code",
            "result_truncated",
            "worker_id",
            "heartbeat_at",
            "lease_expires_at",
            "started_at",
            "finished_at",
            "created_at",
        }
    ),
}


def _seed_legacy_rows(settings: Settings, engine: Engine) -> datetime:
    """0008 **öncesi** şemaya ayırt edilebilir bir satır kümesi yazar.

    Satırlar bilinçli olarak "boş" değildir. Her sütunda en az bir satır
    NULL'dan ve varsayılandan farklı bir değer taşır; aksi hâlde snapshot
    karşılaştırması NULL'ları NULL'larla eşleyip her migration'a yeşil ışık
    yakar, yani yanlış pozitif üretirdi.

    Kurulum mevcut invariantları ihlal etmez:

    - Yalnız **bir** etkin PLAYBOOK satırı vardır
      (``uq_jobs_active_playbook_global``) ve o satır `running` seçilir; böylece
      worker/heartbeat/lease üçlüsü gerçek değerlerle temsil edilir.
    - Etkin ping inventory başına birdir (``uq_jobs_active_ping_inventory``).
    - `limit_pattern` yalnız **terminal** bir PLAYBOOK satırında doludur; etkin
      satırlarda ``ck_jobs_active_playbook_is_authorized`` onu boş tutar.
    - Ping satırları plan ve kira taşımaz.
    """
    _upgrade(settings, PREVIOUS_REVISION)
    now = datetime.now(UTC)
    heartbeat = now - timedelta(seconds=20)

    with engine.begin() as connection:
        _seed_projects_and_inventories(connection, now)

        # Planın üç durumu da temsil edilir; hiçbiri expire edilmemeli.
        # Alanlar satırdan satıra farklıdır: aynı değerleri tekrarlamak,
        # sütunları birbirine karıştıran bir migration'ı görünmez kılardı.
        _insert_plan(
            connection,
            status="prepared",
            now=now - timedelta(minutes=5),
            claimed=False,
            project_id=1,
            inventory_id=1,
            playbook_path="playbooks/web/rollout.yml",
            requested_by="hazirlayan-aktor",
            ttl=timedelta(minutes=30),
        )
        _insert_plan(
            connection,
            status="expired",
            now=now - timedelta(hours=3),
            claimed=False,
            project_id=2,
            inventory_id=2,
            playbook_path="playbooks/db/backup.yml",
            requested_by="suresi-dolan-aktor",
            ttl=timedelta(minutes=10),
        )
        running_plan = _insert_plan(
            connection,
            status="claimed",
            now=now - timedelta(minutes=2),
            claimed=True,
            project_id=1,
            inventory_id=1,
            playbook_path="playbooks/web/rollout.yml",
            requested_by="calisan-aktor",
            ttl=timedelta(hours=2),
        )
        terminal_plan = _insert_plan(
            connection,
            status="claimed",
            now=now - timedelta(days=2),
            claimed=True,
            project_id=2,
            inventory_id=2,
            playbook_path="playbooks/db/backup.yml",
            requested_by="gecmis-aktor",
            ttl=timedelta(hours=1),
        )

        # Terminal ping: sonuç alanları dolu.
        _insert_ping_job(
            connection,
            status="failed",
            now=now - timedelta(hours=6),
            inventory_id=2,
            project_id=2,
            requested_by="ping-aktoru",
            artifact_path="artifacts/ping/2026-08-13",
            return_code=4,
            error_code="ping_unreachable",
            result_truncated=1,
        )
        # Etkin ping: sonuç alanları boş, kirası yok.
        _insert_ping_job(
            connection, status="pending", now=now - timedelta(minutes=1), inventory_id=1
        )
        # Tek etkin PLAYBOOK satırı `running` seçilir: sahiplik/kira üçlüsü
        # ancak bu durumda dolu olabilir (`ck_jobs_running_playbook_has_lease`).
        _insert_playbook_job(
            connection,
            status="running",
            now=now - timedelta(minutes=2),
            plan_id=running_plan,
            inventory_id=1,
            project_id=1,
            playbook_path="playbooks/web/rollout.yml",
            requested_by="calisan-aktor",
            artifact_path="artifacts/web/rollout/current",
            worker_id=str(uuid.uuid4()),
            heartbeat_at=heartbeat,
            lease_expires_at=heartbeat + timedelta(seconds=90),
        )
        # Terminal PLAYBOOK: `limit_pattern` ve sonuç alanları ancak burada
        # dolu olabilir; etkin satırlarda yetkilendirme kısıtı onları kapatır.
        _insert_playbook_job(
            connection,
            status="failed",
            now=now - timedelta(days=2),
            plan_id=terminal_plan,
            inventory_id=2,
            project_id=2,
            playbook_path="playbooks/db/backup.yml",
            limit_pattern="db01",
            requested_by="gecmis-aktor",
            artifact_path="artifacts/db/backup/2026-08-17",
            return_code=2,
            error_code="runner_timeout",
            result_truncated=1,
        )

    return now


def _prepared_plans(engine: Engine) -> list[tuple[object, ...]]:
    """`prepared` planların kimliği, token özeti ve TTL'i."""
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT id, token_hash, created_at, expires_at, claimed_at "
                    "FROM execution_plans WHERE status = 'prepared' ORDER BY id"
                )
            )
        ]


def test_legacy_seed_distinguishes_every_snapshotted_column(
    settings: Settings, engine: Engine
) -> None:
    """Snapshot'ın yanlış pozitif vermediğini önce **seed** kanıtlar.

    Karşılaştırılan her sütunda en az bir satır NULL olmayan bir değer taşır;
    ayrıca sütun başına birden fazla ayrı değer bulunan alanlar, sütunları
    birbirine karıştıran bir dönüşümün de yakalanabileceğini gösterir. Bu
    kontrol olmasaydı "hiçbir şey değişmedi" iddiası boş tablolarda da
    geçerdi.
    """
    _seed_legacy_rows(settings, engine)

    for table, required in REQUIRED_COLUMNS.items():
        columns = _data_columns(engine, table)
        assert required <= set(columns), table

        rows = _rows(engine, table, columns)
        assert rows, table
        for index, name in enumerate(columns):
            values = {row[index] for row in rows}
            assert values != {None}, f"{table}.{name} yalnız NULL taşıyor"

        # Varsayılanla yetinilmediğinin ayrıca kanıtı: 0007'de eklenen
        # `result_truncated` iki değeri de taşımalı.
        if table == "jobs":
            truncated = {row[columns.index("result_truncated")] for row in rows}
            assert truncated == {0, 1}


def test_upgrade_backfills_every_legacy_row_as_check(settings: Settings, engine: Engine) -> None:
    """Legacy satırlar ``check`` ile doldurulur.

    Gerekçe, bu satırların hedeflerinde değişiklik olmadığı değil, önceki
    production yolunun **tamamının** ``--check`` ile çalışmış olmasıdır;
    onları ``normal`` işaretlemek hiç kurulmamış bir argv'yi tarihe yazmak
    olurdu.
    """
    _seed_legacy_rows(settings, engine)
    _upgrade(settings)

    with engine.connect() as connection:
        for table in MODE_TABLES:
            modes = connection.execute(text(f"SELECT DISTINCT mode FROM {table}")).scalars().all()
            assert modes == [BACKFILL_MODE], table

        # Job tipine göre de ayrı ayrı doğrulanır: ping ve playbook satırları
        # farklı yollardan üretilmiştir.
        by_type = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                text("SELECT job_type, COUNT(DISTINCT mode) FROM jobs GROUP BY job_type")
            )
        }
    assert by_type == {"ping": 1, "playbook": 1}


def test_upgrade_touches_nothing_but_the_new_column(settings: Settings, engine: Engine) -> None:
    """``mode`` dışındaki **her** sütunun değeri aynı kalır.

    Karşılaştırılan sütun listesi elle değil şemadan türetilir ve upgrade'in
    şemaya eklediği tek şeyin ``mode`` olduğu ayrıca doğrulanır. Migration'ın
    tek veri etkisi yeni sütunu doldurmaktır: legacy plan token'ları expire
    **edilmez**, hiçbir satır silinmez.
    """
    _seed_legacy_rows(settings, engine)
    before = {table: _data_columns(engine, table) for table in MODE_TABLES}
    snapshot = {table: _rows(engine, table, before[table]) for table in MODE_TABLES}
    prepared_before = _prepared_plans(engine)

    _upgrade(settings)

    for table in MODE_TABLES:
        # Şemaya eklenen tek sütun `mode`; başka bir sütun eklenip veya
        # düşürülüp de snapshot'ın dışında kalamaz.
        after = {column["name"] for column in inspect(engine).get_columns(table)}
        assert after == set(before[table]) | {"mode"}, table

        assert _rows(engine, table, before[table]) == snapshot[table], table

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM execution_plans")).scalar_one() == 4
        assert connection.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one() == 4

    # Hazır bir plan hazır kalır ve token'ı yerinde durur: yükseltme onay
    # biletlerini yakmaz. Ayrı ayrı ölçülür çünkü kaybı en pahalı olan satır
    # budur — expire edilmiş bir token bir daha claim edilemez.
    assert len(prepared_before) == 1
    assert _prepared_plans(engine) == prepared_before


# --- 4. Şema round-trip ve sürüklenme ----------------------------------------


def _snapshot(engine: Engine) -> dict[str, object]:
    """Karşılaştırılabilir bir şema özeti.

    CHECK, FK, unique ve index'ler birlikte karşılaştırılır: downgrade
    onlardan birini bırakıp giderse ikinci upgrade "aynı şema" görünürken
    invariant kaybolurdu.
    """
    inspector = inspect(engine)
    return {
        table: {
            "columns": {
                column["name"]: (str(column["type"]), bool(column["nullable"]))
                for column in inspector.get_columns(table)
            },
            "checks": {
                (check["name"], " ".join(str(check["sqltext"]).split()))
                for check in inspector.get_check_constraints(table)
            },
            "uniques": {
                (unique["name"], tuple(unique["column_names"]))
                for unique in inspector.get_unique_constraints(table)
            },
            "foreign_keys": {
                (
                    key["name"],
                    tuple(key["constrained_columns"]),
                    key["referred_table"],
                    key["options"].get("ondelete"),
                )
                for key in inspector.get_foreign_keys(table)
            },
            "indexes": {
                (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                for index in inspector.get_indexes(table)
            },
        }
        for table in MODE_TABLES
    }


def test_migration_round_trip_restores_the_same_schema(settings: Settings, engine: Engine) -> None:
    """``up → down → up`` şemayı aynı yere getirir.

    Bir migration'ı geri almanın imkânsız olduğunu üretimde fark etmek
    istenmez.
    """
    _upgrade(settings)
    before = _snapshot(engine)

    _downgrade(settings, PREVIOUS_REVISION)
    _upgrade(settings)

    assert _snapshot(engine) == before


def test_downgrade_removes_the_columns_and_the_new_constraints(
    settings: Settings, engine: Engine
) -> None:
    """Geri alma yarım bırakmaz: sütunlar da yeni CHECK'ler de gider."""
    _upgrade(settings)
    _downgrade(settings, PREVIOUS_REVISION)

    inspector = inspect(engine)
    for table in MODE_TABLES:
        assert "mode" not in {column["name"] for column in inspector.get_columns(table)}
        checks = {check["name"] for check in inspector.get_check_constraints(table)}
        assert f"ck_{table}_execution_mode" not in checks
    assert "ck_jobs_ping_is_check_only" not in {
        check["name"] for check in inspector.get_check_constraints("jobs")
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


def test_existing_invariants_survive_the_table_rebuild(settings: Settings, engine: Engine) -> None:
    """0008 batch mode tabloları yeniden kurar; önceki invariantlar korunmalı.

    Kısmi unique index'ler yalnız adlarıyla değil **predicate'leriyle**
    karşılaştırılır: koşulu kaybolmuş bir index, bütün PLAYBOOK geçmişini tek
    satıra indirirdi.
    """
    _upgrade(settings)

    with engine.connect() as connection:
        definitions = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                text("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")
            )
        }

    ping_index = definitions["uq_jobs_active_ping_inventory"]
    assert "UNIQUE INDEX" in ping_index
    assert "job_type = 'ping'" in ping_index
    assert "status IN ('pending', 'running')" in ping_index

    playbook_index = definitions["uq_jobs_active_playbook_global"]
    assert "UNIQUE INDEX" in playbook_index
    assert "job_type = 'playbook'" in playbook_index
    assert "status IN ('pending', 'running')" in playbook_index

    inspector = inspect(engine)
    job_checks = {check["name"] for check in inspector.get_check_constraints("jobs")}
    assert {
        "ck_jobs_running_has_started_at",
        "ck_jobs_active_playbook_is_authorized",
        "ck_jobs_ping_has_no_execution_plan",
        "ck_jobs_running_playbook_has_lease",
        "ck_jobs_idle_playbook_has_no_lease",
        "ck_jobs_ping_has_no_lease",
        "ck_jobs_running_playbook_lease_outlives_heartbeat",
        "ck_jobs_job_type",
        "ck_jobs_job_status",
    } <= job_checks

    plan_checks = {check["name"] for check in inspector.get_check_constraints("execution_plans")}
    assert {
        "ck_execution_plans_claimed_has_claimed_at",
        "ck_execution_plans_expiry_after_creation",
        "ck_execution_plans_execution_plan_status",
    } <= plan_checks

    assert {
        (unique["name"], tuple(unique["column_names"]))
        for unique in inspector.get_unique_constraints("execution_plans")
    } == {
        ("uq_execution_plans_token_hash", ("token_hash",)),
        ("uq_execution_plans_workspace_id", ("workspace_id",)),
    }
    assert ("uq_jobs_execution_plan_id", ("execution_plan_id",)) in {
        (unique["name"], tuple(unique["column_names"]))
        for unique in inspector.get_unique_constraints("jobs")
    }
    assert {
        (key["name"], key["referred_table"], key["options"].get("ondelete"))
        for key in inspector.get_foreign_keys("jobs")
    } == {
        ("fk_jobs_inventory_id_inventories", "inventories", "RESTRICT"),
        ("fk_jobs_project_id_projects", "projects", "RESTRICT"),
        ("fk_jobs_execution_plan_id_execution_plans", "execution_plans", "RESTRICT"),
    }


# --- 5. Kapsam: public yüzey artık her iki kipi de taşıyabilir (R1-V3H2A) ----
#
# Bu bölüm bir önceki dilimde (R1-V3H1A/B) "public yüzey hâlâ check-only"
# iddiasını ölçüyordu. R1-V3H2A o kilidi bilerek açar: aşağıdaki testler artık
# **tersini** doğrular — ``mode`` public request şemalarında zorunlu bir alan
# ve public response şemaları check/normal ikisini de taşıyabilir. DB tipinin
# (``ExecutionMode`` şeması) kendisi bu turda **değişmedi**; değişen yalnız onu
# çevreleyen API sözleşmesidir.


def test_public_response_schemas_now_carry_either_mode() -> None:
    """Plan/launch/job özeti cevapları artık ``check`` ve ``normal`` ikisini de taşıyabilir."""
    from app.schemas.execution import ExecutionLaunchResponse, ExecutionPlanResponse
    from app.schemas.job import PlaybookJobSummaryResponse

    for schema in (ExecutionPlanResponse, ExecutionLaunchResponse, PlaybookJobSummaryResponse):
        annotation = schema.model_fields["mode"].annotation
        assert annotation is ExecutionMode, schema.__name__


def test_public_request_schemas_now_require_an_explicit_mode() -> None:
    """``mode`` artık zorunlu bir istek alanıdır; varsayılanı yoktur.

    İki request şeması da ``extra="forbid"`` taşımaya devam eder — kip dışında
    hâlâ hiçbir çalıştırma parametresi kabul edilmez. ``mode``'un kendisi ise
    artık gövdede **bulunmak zorundadır**: eksik bırakılırsa (bkz.
    ``test_execution_prepare_api.py``/``test_execution_launch_api.py``) 422
    ``request_validation_error`` döner.
    """
    from app.schemas.execution import ExecutionLaunchCreate, ExecutionPlanCreate

    for schema in (ExecutionPlanCreate, ExecutionLaunchCreate):
        field = schema.model_fields["mode"]
        assert field.annotation is ExecutionMode, schema.__name__
        assert field.is_required(), schema.__name__
        assert schema.model_config.get("extra") == "forbid", schema.__name__


def test_runner_argv_now_reads_the_verified_mode_but_check_stays_the_only_public_one() -> None:
    """Runner argv'si artık kipi okur; ama üretilebilen tek public kip ``check``'tir.

    ``build_runner_arguments`` zorunlu, default'suz bir keyword-only ``mode``
    parametresi kazanmıştır (R1-V3H1B2B): ``CHECK`` için
    ``--cmdline=--check`` tam bir kez eklenir, ``NORMAL`` için hiç eklenmez.
    Bu dosyanın geri kalanının ölçtüğü şema/public-yüzey kilitleri
    (:func:`test_no_public_request_field_can_ask_for_normal_mode`) bu
    parametrenin varlığından etkilenmez: istemci hâlâ kip söyleyemez, plan ve
    Job zinciri hâlâ yalnız ``check`` üretir.
    """
    import inspect as python_inspect
    from pathlib import Path

    from app.services.execution.runner_process import (
        CHECK_CMDLINE_ARGUMENT,
        build_runner_arguments,
    )

    parameters = python_inspect.signature(build_runner_arguments).parameters
    assert "mode" in parameters
    assert parameters["mode"].default is python_inspect.Parameter.empty
    assert parameters["mode"].kind is python_inspect.Parameter.KEYWORD_ONLY

    job_id = str(uuid.uuid4())

    def build_argv(mode: ExecutionMode) -> list[str]:
        """Sabit girdilerle, yalnız kip değişen **tipli** bir çağrı sarmalayıcısı.

        Bir ``**dict`` ile keyword unpacking yerine her alanı doğrudan geçer:
        ``mypy`` bu yüzden her argümanı ``build_runner_arguments``'ın kendi
        parametre tipine göre denetleyebilir; geniş bir ``dict[str, object]``
        aracılığında tip bilgisi kaybolmaz.
        """
        return build_runner_arguments(
            command=["ansible-runner"],
            run_dir=Path("/tmp/run"),
            frozen_project_root=Path("/tmp/frozen"),
            frozen_inventory_path=Path("/tmp/frozen/hosts.ini"),
            raw_dir=Path("/tmp/raw"),
            job_id=job_id,
            playbook_path="site.yml",
            mode=mode,
        )

    assert CHECK_CMDLINE_ARGUMENT == "--cmdline=--check"

    check_argv = build_argv(ExecutionMode.CHECK)
    assert check_argv.count(CHECK_CMDLINE_ARGUMENT) == 1

    normal_argv = build_argv(ExecutionMode.NORMAL)
    assert CHECK_CMDLINE_ARGUMENT not in normal_argv
    assert normal_argv == check_argv[:-1]
