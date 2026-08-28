"""Yetkilendirilmiş PLAYBOOK Job'larının salt-okunur sorgusu (R1-V3D2A1).

Ölçülen altı sınır:

1. *Görünürlük.* Bir satır ancak onu üreten onay biletiyle **hâlâ tutarlıysa**
   okunur: PLAYBOOK'tur, mevcut ve ``claimed`` bir plana bağlıdır, project /
   inventory / playbook / aktör dörtlüsünü o planla paylaşır ve
   ``limit_pattern`` taşımaz. PING işleri, plan bağı olmayan legacy satırlar,
   başka bir aktörün işleri ve bağı **bozulmuş** terminal satırlar listede hiç
   görünmez; tek tek istendiğinde de var olmayan bir kimlikle **aynı** cevabı
   üretir.
2. *Sıra ve sayfalama.* ``created_at DESC, id DESC`` kararlıdır; keyset
   sayfalaması aynı satırı iki kez vermez ve arada satır atlamaz.
3. *Alan sözleşmesi.* Özetin alan kümesi **tam eşitlikle** ölçülür: aktör,
   plan/workspace kimliği, digest, artifact yolu, worker kimliği ve kira
   alanları ne dataclass'ta ne de cevap şemasında bulunur.
4. *Hata kodu daraltması.* Bilinen kod aynen taşınır; tanınmayan, boş veya
   serbest metin bir kod (bir workspace yolu, bir token parçası)
   ``unknown_failure``'a düşer ve ham değer hiçbir yere sızmaz.
5. *Girdi doğrulaması SQL'den önce.* Biçimsiz kimlik, aralık dışı ``limit``,
   pozitif olmayan ``project_id``, ham dizgi ``status`` ve yarım cursor
   veritabanına **hiç** ulaşmaz.
6. *Transaction hijyeni.* Dolu sayfa, boş sayfa ve bulunamadı yollarının
   hiçbiri çağırana açık transaction bırakmaz; okuma arızası rollback'le
   kapanır ve boş sayfa diye gizlenmez.

Testler gerçek veritabanı davranışını ölçer: şema gerçek migration zinciriyle
kurulur, sıralama ve keyset karşılaştırması gerçek SQLite üzerinde koşar ve
okuma arızası mock'lanmaz — bozulan ifadeyi gerçek cursor reddeder.

**Kurulumdaki tek zorunluluk:** ``uq_jobs_active_playbook_global`` aynı anda
yalnız **bir** aktif (``pending``/``running``) PLAYBOOK satırına izin verir. Bu
yüzden çok satırlı senaryolar terminal satırlarla kurulur; aktif durumlar tek
tek ölçülür.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, event, select
from sqlalchemy.exc import SQLAlchemyError
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
from app.schemas.job import PlaybookJobListResponse, PlaybookJobSummaryResponse
from app.services.execution import read
from app.services.execution.read import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    PUBLIC_ERROR_CODES,
    UNKNOWN_FAILURE,
    JobNotFoundError,
    PlaybookJobCursor,
    PlaybookJobPage,
    PlaybookJobSummary,
    get_playbook_job,
    list_playbook_jobs,
)

ACTOR = "yerel-operator"
OTHER_ACTOR = "baska-operator"
PLAYBOOK_PATH = "site.yml"

# Özetin **tam** alan kümesi. Eşitlikle ölçülür: sessizce eklenen bir alan
# (aktör, plan kimliği, workspace, digest, artifact yolu) testi düşürür.
SAFE_SUMMARY_FIELDS = {
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

# Hiçbir koşulda taşınmayacak alan adları.
FORBIDDEN_SUMMARY_FIELDS = (
    "requested_by",
    "actor",
    "execution_plan_id",
    "plan_id",
    "artifact_path",
    "worker_id",
    "heartbeat_at",
    "lease_expires_at",
    "plan_token",
    "token",
    "token_hash",
    "input_fingerprint",
    "workspace_id",
    "manifest_digest",
    "limit_pattern",
    "environment",
    "argv",
    "command",
    "private_key",
    # Join yalnız isimleri dışarı çıkarır (R1-V3J0B2); Project/Inventory'nin
    # path ve description'ı görünmez.
    "project_path",
    "inventory_path",
    "project_description",
)


# --- Kurulum yardımcıları -----------------------------------------------------


@pytest.fixture
def records(db_session: Session, tmp_path: Any) -> tuple[Project, Inventory]:
    """Job ve planın FK'lerini karşılayan asgari kayıtlar."""
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


def _hex64() -> str:
    """Tekil, 64 küçük harfli hex karakter (token_hash/digest biçimi)."""
    return uuid.uuid4().hex * 2


# "Bu alan için kurulum varsayılanını kullan" işareti. ``None``'dan **ayrıdır**:
# ``project_id=None`` gerçekten ``NULL`` bir sütun değeri üretmelidir, çünkü
# bozuk binding vakalarından biri tam olarak budur. Sentinel olmasaydı testin
# kurmak istediği vaka sessizce geçerli bir satıra dönüşür ve hiçbir şey
# ölçmezdi.
_DEFAULT: Any = object()


def _plan(
    session: Session,
    *,
    project_id: int,
    inventory_id: int,
    requested_by: str,
    playbook_path: str,
    moment: datetime,
    status: ExecutionPlanStatus = ExecutionPlanStatus.CLAIMED,
    mode: ExecutionMode = ExecutionMode.CHECK,
    claimed_at: Any = _DEFAULT,
) -> str:
    """Bir plan satırı yazar ve kimliğini döndürür.

    Varsayılan ``claimed``'dir: yalnız tüketilmiş bir bilet gerçek bir Job'ı
    yetkilendirmiş olabilir. ``claimed_at`` varsayılan olarak yalnız o durumda
    doldurulur (``ck_execution_plans_claimed_has_claimed_at``).

    ``claimed_at`` açıkça verilebilir (:data:`_DEFAULT` dışında bir değerle):
    bu, TTL temizliğinin (:func:`~app.services.execution.store.sweep_expired_plans`)
    ürettiği "önceden claim edilmiş, sonradan yalnız süresi geçtiği için
    ``expired``" durumunu kurmanın **tek** yoludur — o fonksiyon ``status``'u
    değiştirir ama ``claimed_at``'e hiç dokunmaz, dolayısıyla ``status=expired``
    ve ``claimed_at`` dolu birlikte durabilir.
    """
    plan_id = str(uuid.uuid4())
    resolved_claimed_at = (
        (moment if status is ExecutionPlanStatus.CLAIMED else None)
        if claimed_at is _DEFAULT
        else claimed_at
    )
    session.add(
        ExecutionPlanRecord(
            id=plan_id,
            token_hash=_hex64(),
            project_id=project_id,
            inventory_id=inventory_id,
            playbook_path=playbook_path,
            requested_by=requested_by,
            input_fingerprint=_hex64(),
            workspace_id=str(uuid.uuid4()),
            manifest_digest=_hex64(),
            status=status,
            mode=mode,
            created_at=moment,
            expires_at=moment + timedelta(hours=1),
            claimed_at=resolved_claimed_at,
        )
    )
    session.flush()
    return plan_id


def _seed(
    session: Session,
    records: tuple[Project, Inventory],
    *,
    job_id: str | None = None,
    job_type: JobType = JobType.PLAYBOOK,
    # Varsayılan **terminal**'dir: `uq_jobs_active_playbook_global` aynı anda
    # yalnız bir aktif PLAYBOOK satırına izin verir ve çok satırlı senaryolar
    # ancak böyle kurulabilir.
    status: JobStatus = JobStatus.SUCCESSFUL,
    requested_by: str = ACTOR,
    created_at: datetime | None = None,
    with_plan: bool = True,
    project_id: Any = _DEFAULT,
    playbook_path: str | None = PLAYBOOK_PATH,
    plan_overrides: dict[str, Any] | None = None,
    **overrides: Any,
) -> str:
    """Tek bir Job satırı (ve gerekiyorsa onu yetkilendiren planı) yazar.

    Varsayılan hâl **görünür** bir satırdır: Job ile plan aynı project,
    inventory, playbook ve aktörü taşır, plan ``claimed``'dir ve
    ``limit_pattern`` boştur. Her görünürlük testi bu bağın tek bir halkasını
    bozar, böylece elenmenin sebebi tekildir.

    ``project_id`` ve ``playbook_path`` **Job'ın** alanlarıdır ve ``None``
    verildiğinde gerçekten ``NULL`` yazılır. Planın kendi alanları ise
    ``plan_overrides`` ile ayrıca bozulabilir; ikisinin ayrı olması, "Job'ın
    değeri eksik" ile "Job'ın değeri planınkinden farklı" vakalarını
    birbirinden ayırır.
    """
    project, inventory = records
    moment = created_at or datetime.now(UTC)
    identifier = job_id or str(uuid.uuid4())
    owner = project.id if project_id is _DEFAULT else project_id

    plan_id: str | None = None
    if with_plan:
        # Planın sütunları ``NOT NULL``'dır; Job tarafında bilinçli olarak
        # boşaltılan bir alan için plan kurulum varsayılanına düşer.
        plan_fields: dict[str, Any] = {
            "project_id": project.id if owner is None else owner,
            "inventory_id": inventory.id,
            "requested_by": requested_by,
            "playbook_path": PLAYBOOK_PATH if playbook_path is None else playbook_path,
            "moment": moment,
        }
        plan_fields.update(plan_overrides or {})
        plan_id = _plan(session, **plan_fields)

    fields: dict[str, Any] = {
        "id": identifier,
        "job_type": job_type,
        "status": status,
        "execution_plan_id": plan_id,
        "project_id": owner,
        "inventory_id": inventory.id,
        "playbook_path": playbook_path,
        "limit_pattern": None,
        "requested_by": requested_by,
        "created_at": moment,
    }
    if status is JobStatus.RUNNING:
        # `ck_jobs_running_has_started_at` + `ck_jobs_running_playbook_has_lease`
        # + `ck_jobs_running_playbook_lease_outlives_heartbeat`.
        fields["started_at"] = moment
        if job_type is JobType.PLAYBOOK:
            fields["worker_id"] = str(uuid.uuid4())
            fields["heartbeat_at"] = moment
            fields["lease_expires_at"] = moment + timedelta(seconds=30)
    fields.update(overrides)
    session.add(Job(**fields))
    session.commit()
    return identifier


def _seed_series(
    session: Session,
    records: tuple[Project, Inventory],
    count: int,
    *,
    base: datetime | None = None,
    step: timedelta = timedelta(minutes=1),
    **options: Any,
) -> list[str]:
    """``count`` terminal Job'ı **en yeniden en eskiye** doğru döndürür."""
    origin = base or datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    identifiers = [
        _seed(session, records, created_at=origin + step * index, **options)
        for index in range(count)
    ]
    return list(reversed(identifiers))


@pytest.fixture
def counted_statements(migrated_engine: Engine) -> Iterator[list[str]]:
    """Engine üzerinde çalıştırılan her SQL ifadesini kaydeder."""
    seen: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any, **_kwargs: Any) -> None:
        seen.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _record)


def _ids(page: PlaybookJobPage) -> list[str]:
    return [item.job_id for item in page.items]


def _result_path(job_id: str) -> str:
    """Yayımlanmış sonucun app-data köküne göreli **tek** geçerli konumu."""
    return f"jobs/{job_id}/result.json"


def _raw_sql(engine: Engine, statement: str, parameters: tuple[Any, ...] = ()) -> None:
    """ORM ve FK doğrulamasını atlayarak ham SQL çalıştırır.

    Yalnız bir test kullanır: ``RESTRICT`` foreign key'in üretilmesine izin
    vermediği, var olmayan bir plana işaret eden Job satırı. Servisin böyle bir
    satıra karşı savunması ancak böyle ölçülebilir. PRAGMA çağrının sonunda geri
    açılır: bağlantı havuza FK doğrulaması kapalı hâlde dönerse sonraki testler
    sessizce zayıflardı.
    """
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.execute(statement, parameters)
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
        raw.commit()
    finally:
        raw.close()


# --- Sıra ---------------------------------------------------------------------


def test_the_newest_job_comes_first(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Liste ``created_at DESC``'tir: en son kaydedilen iş başta gelir."""
    expected = _seed_series(db_session, records, 4)

    page = list_playbook_jobs(db_session, requested_by=ACTOR)

    assert _ids(page) == expected
    assert page.has_more is False
    assert page.next_cursor is None


def test_equal_timestamps_are_broken_by_descending_id(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Aynı mikrosaniyeyi taşıyan satırlarda sıra ``id DESC`` ile kararlıdır.

    ``id`` ikincil anahtar olmasaydı sıra sürücünün satır sırasına kalırdı;
    kararsız bir sıra keyset sayfalamasında satır tekrarına veya atlanan
    satıra dönüşürdü.
    """
    moment = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    identifiers = sorted(str(uuid.uuid4()) for _ in range(5))
    for identifier in identifiers:
        _seed(db_session, records, job_id=identifier, created_at=moment)

    page = list_playbook_jobs(db_session, requested_by=ACTOR)

    assert _ids(page) == list(reversed(identifiers))
    assert {item.created_at for item in page.items} == {moment}


# --- Sayfalama ----------------------------------------------------------------


def test_a_full_page_reports_more_with_a_cursor(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """``limit + 1`` sorgulanır: fazladan satır cevaba **konmaz**, işaret olur."""
    expected = _seed_series(db_session, records, 5)

    page = list_playbook_jobs(db_session, requested_by=ACTOR, limit=2)

    assert _ids(page) == expected[:2]
    assert page.has_more is True
    assert page.next_cursor == PlaybookJobCursor(
        created_at=page.items[-1].created_at, job_id=expected[1]
    )


def test_the_last_page_carries_no_cursor(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Tam olarak ``limit`` kadar satır kaldığında devam işareti üretilmez."""
    expected = _seed_series(db_session, records, 3)

    page = list_playbook_jobs(db_session, requested_by=ACTOR, limit=3)

    assert _ids(page) == expected
    assert page.has_more is False
    assert page.next_cursor is None


def test_paging_through_every_job_skips_and_repeats_nothing(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Sayfa sayfa okuma **tam** listeyi üretir: ne tekrar ne atlama.

    Yarısı eşit ``created_at`` taşır; keyset karşılaştırmasının ikinci
    anahtarı olmasaydı sınırdaki satırlar ya iki kez okunur ya hiç okunmazdı.
    """
    origin = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    for index in range(9):
        # Üçer satır aynı zaman damgasını paylaşır.
        _seed(db_session, records, created_at=origin + timedelta(minutes=index // 3))

    everything = list_playbook_jobs(db_session, requested_by=ACTOR, limit=MAX_PAGE_LIMIT)
    assert len(everything.items) == 9

    collected: list[str] = []
    cursor: PlaybookJobCursor | None = None
    for _ in range(9):
        page = list_playbook_jobs(
            db_session,
            requested_by=ACTOR,
            limit=2,
            before_created_at=None if cursor is None else cursor.created_at,
            before_job_id=None if cursor is None else cursor.job_id,
        )
        collected.extend(_ids(page))
        cursor = page.next_cursor
        if cursor is None:
            break

    assert cursor is None
    assert collected == _ids(everything)
    assert len(collected) == len(set(collected))


def test_a_cursor_beyond_the_last_row_yields_an_empty_page(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Son satırın ötesindeki cursor boş ve devamsız bir sayfa üretir."""
    origin = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    oldest = _seed(db_session, records, created_at=origin)

    page = list_playbook_jobs(
        db_session,
        requested_by=ACTOR,
        before_created_at=origin,
        before_job_id=oldest,
    )

    assert page.items == ()
    assert page.has_more is False
    assert page.next_cursor is None


def test_the_page_invariant_binds_cursor_to_has_more() -> None:
    """``next_cursor`` ile ``has_more`` ayrışamaz; sayfa nesnesi bunu reddeder."""
    cursor = PlaybookJobCursor(created_at=datetime.now(UTC), job_id=str(uuid.uuid4()))

    with pytest.raises(ValueError):
        PlaybookJobPage(items=(), has_more=True, next_cursor=None)
    with pytest.raises(ValueError):
        PlaybookJobPage(items=(), has_more=False, next_cursor=cursor)


# --- Filtreler ----------------------------------------------------------------


def test_the_project_filter_selects_only_that_project(
    db_session: Session, records: tuple[Project, Inventory], tmp_path: Any
) -> None:
    """``project_id`` verildiğinde başka project'in işleri hiç okunmaz."""
    project, _ = records
    other = Project(name="Diger", path=str(tmp_path / "diger"))
    db_session.add(other)
    db_session.commit()

    mine = _seed_series(db_session, records, 2)
    theirs = _seed_series(
        db_session,
        records,
        2,
        base=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
        project_id=other.id,
    )

    page = list_playbook_jobs(db_session, requested_by=ACTOR, project_id=project.id)

    assert _ids(page) == mine
    assert set(_ids(page)).isdisjoint(theirs)


def test_list_and_detail_carry_the_projects_own_name(
    db_session: Session, records: tuple[Project, Inventory], tmp_path: Any
) -> None:
    """Özet ID'nin yanında gerçek project/inventory adını taşır (R1-V3J0B2).

    İki project'in adı farklıdır; her Job kendi project'inin adını taşımalı,
    ID'den tahmin edilen veya bir başkasınınkiyle karışan bir ad **değil**.
    """
    project, inventory = records
    other = Project(name="Diger", path=str(tmp_path / "diger"))
    db_session.add(other)
    db_session.commit()

    mine = _seed(db_session, records)
    theirs = _seed(
        db_session,
        records,
        created_at=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
        project_id=other.id,
    )

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    by_id = {item.job_id: item for item in page.items}

    assert by_id[mine].project_name == project.name == "Web"
    assert by_id[mine].inventory_name == inventory.name == "Prod"
    assert by_id[theirs].project_name == other.name == "Diger"

    detail = get_playbook_job(db_session, mine, requested_by=ACTOR)
    assert detail.project_name == project.name
    assert detail.inventory_name == inventory.name


def test_the_status_filter_selects_only_that_status(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """``status`` verildiğinde diğer durumlar hiç okunmaz."""
    successful = _seed_series(db_session, records, 2)
    failed = _seed_series(
        db_session,
        records,
        2,
        base=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
        status=JobStatus.FAILED,
        error_code="runner_failed",
    )

    page = list_playbook_jobs(db_session, requested_by=ACTOR, status=JobStatus.FAILED)

    assert _ids(page) == failed
    assert set(_ids(page)).isdisjoint(successful)
    assert {item.status for item in page.items} == {JobStatus.FAILED}


def test_project_and_status_filters_apply_together(
    db_session: Session, records: tuple[Project, Inventory], tmp_path: Any
) -> None:
    """İki filtre birlikte verildiğinde kesişim okunur, birleşim değil."""
    project, _ = records
    other = Project(name="Diger", path=str(tmp_path / "diger"))
    db_session.add(other)
    db_session.commit()
    origin = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

    wanted = _seed(
        db_session,
        records,
        created_at=origin,
        status=JobStatus.FAILED,
        error_code="runner_timeout",
    )
    # Doğru project, yanlış durum.
    _seed(db_session, records, created_at=origin + timedelta(minutes=1))
    # Doğru durum, yanlış project.
    _seed(
        db_session,
        records,
        created_at=origin + timedelta(minutes=2),
        status=JobStatus.FAILED,
        error_code="runner_timeout",
        project_id=other.id,
    )

    page = list_playbook_jobs(
        db_session,
        requested_by=ACTOR,
        project_id=project.id,
        status=JobStatus.FAILED,
    )

    assert _ids(page) == [wanted]


def test_the_mode_filter_selects_only_that_mode(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """``mode`` verildiğinde diğer kip hiç okunmaz (R1-V3J0B2)."""
    checked = _seed_series(db_session, records, 2)
    normal = _seed_series(
        db_session,
        records,
        2,
        base=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
        mode=ExecutionMode.NORMAL,
        plan_overrides={"mode": ExecutionMode.NORMAL},
    )

    page = list_playbook_jobs(db_session, requested_by=ACTOR, mode=ExecutionMode.NORMAL)

    assert _ids(page) == normal
    assert set(_ids(page)).isdisjoint(checked)
    assert {item.mode for item in page.items} == {ExecutionMode.NORMAL}


def test_status_mode_and_project_filters_apply_together(
    db_session: Session, records: tuple[Project, Inventory], tmp_path: Any
) -> None:
    """Üç filtre birlikte verildiğinde yalnız üçünü de karşılayan satır okunur."""
    project, _ = records
    other = Project(name="Diger", path=str(tmp_path / "diger"))
    db_session.add(other)
    db_session.commit()
    origin = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

    wanted = _seed(
        db_session,
        records,
        created_at=origin,
        status=JobStatus.FAILED,
        error_code="runner_timeout",
        mode=ExecutionMode.NORMAL,
        plan_overrides={"mode": ExecutionMode.NORMAL},
    )
    # Doğru project ve durum, yanlış kip.
    _seed(
        db_session,
        records,
        created_at=origin + timedelta(minutes=1),
        status=JobStatus.FAILED,
        error_code="runner_timeout",
    )
    # Doğru project ve kip, yanlış durum.
    _seed(
        db_session,
        records,
        created_at=origin + timedelta(minutes=2),
        mode=ExecutionMode.NORMAL,
        plan_overrides={"mode": ExecutionMode.NORMAL},
    )
    # Doğru durum ve kip, yanlış project.
    _seed(
        db_session,
        records,
        created_at=origin + timedelta(minutes=3),
        status=JobStatus.FAILED,
        error_code="runner_timeout",
        mode=ExecutionMode.NORMAL,
        plan_overrides={"mode": ExecutionMode.NORMAL},
        project_id=other.id,
    )

    page = list_playbook_jobs(
        db_session,
        requested_by=ACTOR,
        project_id=project.id,
        status=JobStatus.FAILED,
        mode=ExecutionMode.NORMAL,
    )

    assert _ids(page) == [wanted]


# --- Görünürlük ---------------------------------------------------------------


def test_another_actors_jobs_are_never_listed(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Aktör izolasyonu sorgu koşulundadır; liste yalnız çağıranın işlerini taşır."""
    mine = _seed_series(db_session, records, 2)
    theirs = _seed_series(
        db_session,
        records,
        2,
        base=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
        requested_by=OTHER_ACTOR,
    )

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    other_page = list_playbook_jobs(db_session, requested_by=OTHER_ACTOR)

    assert _ids(page) == mine
    assert _ids(other_page) == theirs
    assert set(_ids(page)).isdisjoint(_ids(other_page))


def test_ping_and_planless_playbook_rows_are_invisible(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """PING ve plan bağı olmayan legacy PLAYBOOK satırları listeye girmez.

    İkisi de aynı aktöre aittir ve aynı project/inventory'yi taşır: elenmelerinin
    tek sebebi "yetkilendirilmiş PLAYBOOK" tanımıdır.
    """
    origin = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    authorized = _seed(db_session, records, created_at=origin)
    # PING'in planı olamaz (`ck_jobs_ping_has_no_execution_plan`).
    _seed(
        db_session,
        records,
        created_at=origin + timedelta(minutes=1),
        job_type=JobType.PING,
        with_plan=False,
        playbook_path=None,
    )
    # Terminal PLAYBOOK satırı `ck_jobs_active_playbook_is_authorized`'ın
    # dışındadır; migration'ın kapattığı legacy kayıtlar tam olarak böyledir.
    _seed(
        db_session,
        records,
        created_at=origin + timedelta(minutes=2),
        with_plan=False,
    )

    page = list_playbook_jobs(db_session, requested_by=ACTOR)

    assert _ids(page) == [authorized]


def test_an_active_pending_job_is_listed(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Kuyrukta bekleyen iş de okunur: liste terminal satırlarla sınırlı değildir."""
    pending = _seed(db_session, records, status=JobStatus.PENDING)

    page = list_playbook_jobs(db_session, requested_by=ACTOR)

    assert _ids(page) == [pending]
    assert page.items[0].status is JobStatus.PENDING
    assert page.items[0].started_at is None
    assert page.items[0].finished_at is None


# --- Bozuk yetkilendirme bağı (R1-V3D2A1F) ------------------------------------
#
# Bu bölümün ölçtüğü boşluk şudur: `ck_jobs_active_playbook_is_authorized`
# yalnız `pending`/`running` satırları kapsar. Terminal bir PLAYBOOK satırı
# `execution_plan_id`'yi taşıdığı hâlde project, inventory, playbook, aktör veya
# `limit_pattern` bağı bozulmuş olabilir ve veritabanı onu kabul eder. Yalnız
# "plan kimliği dolu mu" diye bakan bir okuma yüzeyi, kullanıcının onayladığı
# işten **başka** bir işi onun geçmişi gibi gösterirdi.


BROKEN_BINDINGS = (
    "job_project_null",
    "job_playbook_null",
    "job_limit_pattern_set",
    "project_mismatch",
    "inventory_mismatch",
    "playbook_mismatch",
    "actor_mismatch",
    "plan_prepared",
    "plan_expired",
    # R1-V3H2A: Job'un kipi, onu yetkilendiren plan kaydının kipiyle aynı
    # olmalıdır. Doğru zincirde ikisi her zaman eşittir (authorize.py Job'u
    # plan kaydından miras aldırır); bu vaka yalnız doğrudan yazılmış veya
    # başka bir yoldan ayrışmış bir satırı ölçer.
    "mode_mismatch",
)


@pytest.fixture
def other_records(db_session: Session, tmp_path: Any) -> tuple[Project, Inventory]:
    """Uyuşmazlık vakaları için ikinci bir project ve inventory."""
    project = Project(name="Diger", path=str(tmp_path / "diger"))
    db_session.add(project)
    db_session.commit()
    inventory = Inventory(
        name="Diger Prod",
        path=str(tmp_path / "diger" / "hosts.ini"),
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    db_session.add(inventory)
    db_session.commit()
    return project, inventory


def _seed_broken(
    session: Session,
    records: tuple[Project, Inventory],
    other_records: tuple[Project, Inventory],
    case: str,
) -> str:
    """Tek bir halkası bozulmuş, gerçek bir plana bağlı terminal Job yazar."""
    other_project, other_inventory = other_records
    cases: dict[str, dict[str, Any]] = {
        "job_project_null": {"project_id": None},
        "job_playbook_null": {"playbook_path": None},
        "job_limit_pattern_set": {"limit_pattern": "web*"},
        "project_mismatch": {"plan_overrides": {"project_id": other_project.id}},
        "inventory_mismatch": {"plan_overrides": {"inventory_id": other_inventory.id}},
        "playbook_mismatch": {"plan_overrides": {"playbook_path": "playbooks/web.yml"}},
        "actor_mismatch": {"plan_overrides": {"requested_by": OTHER_ACTOR}},
        "plan_prepared": {"plan_overrides": {"status": ExecutionPlanStatus.PREPARED}},
        "plan_expired": {"plan_overrides": {"status": ExecutionPlanStatus.EXPIRED}},
        "mode_mismatch": {"mode": ExecutionMode.NORMAL},
    }
    return _seed(session, records, **cases[case])


def _legacy_visible(engine: Engine, job_id: str) -> bool:
    """Satır, **düzeltme öncesi** üçlü koşulu karşılıyor mu?

    Bağımsız bir bağlantıdan sorulur ve iki işi birden görür: vakanın gerçekten
    veritabanına yazıldığını kanıtlar (satır yoksa cevap ``False`` olurdu) ve
    her vakanın bir **regresyon** vakası olduğunu gösterir — eski predicate onu
    görünür buluyordu, yeni bağ elemelidir. Aksi hâlde test, hiçbir zaman
    sızmamış bir satırın sızmadığını doğrulamaktan ibaret kalırdı.
    """
    with Session(engine) as observer:
        row = observer.execute(
            select(Job.id).where(
                Job.id == job_id,
                Job.job_type == JobType.PLAYBOOK,
                Job.execution_plan_id.is_not(None),
                Job.requested_by == ACTOR,
            )
        ).first()
    return row is not None


@pytest.mark.parametrize("case", BROKEN_BINDINGS)
def test_a_broken_authorization_binding_is_never_listed(
    db_session: Session,
    records: tuple[Project, Inventory],
    other_records: tuple[Project, Inventory],
    migrated_engine: Engine,
    case: str,
) -> None:
    """Bağı bozuk terminal satır listede **hiç** görünmez.

    Yanında bağı sağlam bir satır durur: ölçüm "liste boş döndü" değil,
    "yalnız sağlam satır döndü"dür — sorgunun tümüyle hiçbir şey döndürmemesi
    de testi geçirirdi.
    """
    healthy = _seed(db_session, records, created_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC))
    broken = _seed_broken(db_session, records, other_records, case)

    # Vaka gerçekten yazıldı ve eski üçlü koşula göre **görünür** olurdu.
    assert _legacy_visible(migrated_engine, broken), case

    page = list_playbook_jobs(db_session, requested_by=ACTOR)

    assert _ids(page) == [healthy]
    assert not db_session.in_transaction()


@pytest.mark.parametrize("case", BROKEN_BINDINGS)
def test_a_broken_authorization_binding_produces_the_same_not_found(
    db_session: Session,
    records: tuple[Project, Inventory],
    other_records: tuple[Project, Inventory],
    migrated_engine: Engine,
    case: str,
) -> None:
    """Tekil okuma da aynı sabit 404'ü üretir; hangi halkanın koptuğu söylenmez.

    Ayrım yapmak, bağın hangi alanının bozuk olduğunu deneme yanılmayla
    öğrenilebilir kılardı — ve o bilgi, satırın gerçekte hangi project'e,
    inventory'ye veya aktöre bağlı olduğunu ele verirdi.
    """
    broken = _seed_broken(db_session, records, other_records, case)
    assert _legacy_visible(migrated_engine, broken), case

    with pytest.raises(JobNotFoundError) as caught:
        get_playbook_job(db_session, broken, requested_by=ACTOR)

    error = caught.value
    assert error.status_code == 404
    assert error.code == "job_not_found"
    assert error.details == {"reason": "not_found"}
    assert error.message == "Böyle bir çalıştırma kaydı bulunamadı."
    # Elenme sebebi ne mesaja ne details'a girer.
    rendered = f"{error.message} {json.dumps(error.details)}"
    for leak in (
        case,
        broken,
        ACTOR,
        OTHER_ACTOR,
        PLAYBOOK_PATH,
        "playbooks/web.yml",
        "limit",
        "plan",
        "binding",
        "project",
        "inventory",
    ):
        assert leak not in rendered, leak
    assert not db_session.in_transaction()


# --- Durably claimed, sonradan yalnız TTL yüzünden expired (R1-V3J5) ---------
#
# `sweep_expired_plans` bir plana bağlı aktif pending/running Job yoksa,
# TTL'si geçmiş her kaydı `expired` yapar; `claimed_at`'e hiç dokunmaz. Böyle
# bir kayda bağlı **terminal** bir Job, kullanıcının gerçekten onayladığı ve
# çalıştırdığı işi temsil etmeye devam eder — yalnızca temizlik onu "claimed"
# olmaktan çıkarmıştır. Genişleme bilinçli olarak **terminal** (``successful``/
# ``failed``) Job'larla sınırlıdır: gerçek ``sweep_expired_plans`` aktif
# (``pending``/``running``) bir Job'a bağlı planı zaten hiç ``expired``
# yapmaz ve worker (``job_state._binding_is_valid``) yalnız ``claimed`` bir
# planı çalıştırır — dolayısıyla ``expired`` + ``claimed_at`` dolu +
# ``pending``/``running`` bir Job, normal yazma yollarında hiç oluşmayan
# tutarsız bir durumdur ve fail-closed elenmelidir. Bu bölüm üç yönü birlikte
# ölçer: genişleyen görünürlük yalnız terminal durumlarda gerçekten çalışır
# (pozitif), aktif durumlarda **çalışmaz** (negatif) ve hâlâ her binding'e
# bağlıdır (negatif) — TTL'nin dokunmadığı hiçbir kontrol gevşemez.
#
# ``claimed_at``'in rolü hakkında dürüst olmak gerekir: ``sweep_expired_plans``
# bu alana dokunmadığı ve ``ck_execution_plans_claimed_has_claimed_at`` yalnız
# ``claimed`` satırda dolu olmasını zorunlu kıldığı için, uygulamanın normal
# yazma yollarında ``expired`` + ``claimed_at`` dolu kombinasyonu "bir zamanlar
# claim edildi, sonra yalnız TTL yüzünden süresi geçti" anlamına gelir — ama bu
# **kriptografik bir kanıt veya taklit edilemez bir sütun değildir**; doğrudan
# veritabanı erişimi olan biri ``claimed_at``'i de tıpkı ``status`` veya
# ``project_id`` gibi elle yazabilir. Fail-closed sınır tek bir sahte
# kanıtlanamaz sütuna değil, ``claimed_at`` + terminal ``Job.status`` +
# bütün immutable binding kontrollerinin **birlikte** doğru olmasına dayanır.


@pytest.mark.parametrize("status", [JobStatus.SUCCESSFUL, JobStatus.FAILED])
def test_a_durably_claimed_job_stays_visible_after_its_plan_expires_via_ttl(
    db_session: Session, records: tuple[Project, Inventory], status: JobStatus
) -> None:
    """R1-V3J5 kök vaka: TTL temizliğinin `expired` yaptığı ama önceden
    gerçekten claim edilmiş bir plana bağlı **terminal** Job, listede ve
    tekil okumada görünmeye devam eder. Hem ``successful`` hem ``failed``
    ölçülür: genişleme belirli bir sonuca değil, terminalliğe bağlıdır.
    """
    extra: dict[str, Any] = {"error_code": "runner_timeout"} if status is JobStatus.FAILED else {}
    job_id = _seed(
        db_session,
        records,
        status=status,
        plan_overrides={
            "status": ExecutionPlanStatus.EXPIRED,
            "claimed_at": datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
        },
        **extra,
    )

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    assert _ids(page) == [job_id]

    summary = get_playbook_job(db_session, job_id, requested_by=ACTOR)
    assert summary.job_id == job_id
    assert summary.status is status


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_a_pending_or_running_job_bound_to_an_expired_but_claimed_plan_is_invisible(
    db_session: Session, records: tuple[Project, Inventory], status: JobStatus
) -> None:
    """Genişleme **yalnız** terminal Job'lara uygulanır: ``expired`` + dolu
    ``claimed_at`` + hâlâ ``pending``/``running`` bir satır listede de tekil
    okumada da görünmemelidir.

    Bu durum gerçek ``sweep_expired_plans``'in ürettiği bir durum
    **değildir** (aktif bir Job'a bağlı plan hiç ``expired`` yapılmaz, bkz.
    ``_HAS_ACTIVE_PLAYBOOK_JOB``) ve worker da yalnız ``claimed`` bir planı
    çalıştırır (``job_state._binding_is_valid``); bu satır bu yüzden normal
    yazma yollarının asla üretemeyeceği, doğrudan kurulmuş bir tutarsızlığı
    ölçer — okuma yüzeyi yine de fail-closed davranmalıdır.
    """
    healthy = _seed(db_session, records, created_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC))
    # `status=RUNNING` için `_seed` kendisi `started_at`/`worker_id`/
    # `heartbeat_at`/`lease_expires_at`'i CHECK kısıtlarını sağlayacak
    # biçimde otomatik doldurur; burada elle tekrarlanmaz.
    invisible = _seed(
        db_session,
        records,
        status=status,
        created_at=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
        plan_overrides={
            "status": ExecutionPlanStatus.EXPIRED,
            "claimed_at": datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
        },
    )

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    assert _ids(page) == [healthy]
    assert invisible not in _ids(page)

    with pytest.raises(JobNotFoundError):
        get_playbook_job(db_session, invisible, requested_by=ACTOR)


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_a_claimed_plans_pending_or_running_job_stays_visible(
    db_session: Session, records: tuple[Project, Inventory], status: JobStatus
) -> None:
    """R1-V3J5'in genişlemesi ``claimed`` dalını değiştirmez: bir plan hâlâ
    ``claimed`` olduğu sürece ``pending``/``running`` bir Job, düzeltme
    öncesindeki gibi görünür kalır.

    Bu davranış zaten :func:`test_an_active_pending_job_is_listed` ve
    :func:`test_a_healthy_binding_survives_every_status` tarafından ölçülür;
    bu test yalnız R1-V3J5-AUDIT-FIX1'in istediği regresyonu isimlendirir ve
    ``expired`` dalıyla yan yana durur.
    """
    job_id = _seed(db_session, records, status=status)

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    assert _ids(page) == [job_id]

    summary = get_playbook_job(db_session, job_id, requested_by=ACTOR)
    assert summary.status is status


def test_a_canceled_job_bound_to_an_expired_but_claimed_plan_is_invisible(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """``CANCELED`` genişletilmiş terminal kümenin **dışındadır**.

    Hiçbir PLAYBOOK yazma yolu (``job_state``) bir PLAYBOOK Job'ı
    ``canceled`` yapmaz — bu statü yalnız PING Job'ları için kullanılır
    (``app.services.jobs.service``). Genişlemeyi ``job_state``'in kendi
    ``successful``/``failed`` terminal tanımına sadık tutmak için ``CANCELED``
    bilinçli olarak uydurma biçimde terminal kümeye eklenmez; ``expired`` +
    dolu ``claimed_at`` altında bile görünmez kalmalıdır. (``claimed`` bir
    plan altında ``CANCELED``'in görünür kalması ayrı ve değişmeyen bir
    davranıştır, bkz. :func:`test_a_healthy_binding_survives_every_status`.)
    """
    healthy = _seed(db_session, records, created_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC))
    invisible = _seed(
        db_session,
        records,
        status=JobStatus.CANCELED,
        created_at=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
        plan_overrides={
            "status": ExecutionPlanStatus.EXPIRED,
            "claimed_at": datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
        },
    )

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    assert _ids(page) == [healthy]

    with pytest.raises(JobNotFoundError):
        get_playbook_job(db_session, invisible, requested_by=ACTOR)


# Binding mismatch vakalarının expired+claimed varyantı. `plan_prepared` ve
# `plan_expired` (claimed_at=None, hiç claim edilmemiş) BROKEN_BINDINGS'te
# zaten ölçülür ve bu genişlemeyle ilgisizdir; burada tekrarlanmaz.
EXPIRED_CLAIMED_BROKEN_BINDINGS = (
    "job_project_null",
    "job_playbook_null",
    "job_limit_pattern_set",
    "project_mismatch",
    "inventory_mismatch",
    "playbook_mismatch",
    "actor_mismatch",
    "mode_mismatch",
)


def _seed_expired_claimed_broken(
    session: Session,
    records: tuple[Project, Inventory],
    other_records: tuple[Project, Inventory],
    case: str,
) -> str:
    """Durably claimed, sonra ``expired`` olmuş bir plana bağlı, tek halkası
    bozuk **terminal** Job yazar (``_seed``'in varsayılan durumu
    ``successful``'dır — terminallik koşulu burada zaten sağlanır).

    Amaç: R1-V3J5'in genişlettiği "``expired`` + ``claimed_at`` dolu + Job
    terminal" koşulunun hiçbir mevcut binding kontrolünü gevşetmediğini
    kanıtlamak. Bu fonksiyon olmasaydı yalnız plan-status genişlemesi ölçülür,
    sağlam olmayan bir satırın bu genişlemeyle birlikte sızıp sızmadığı
    ölçülmezdi.
    """
    other_project, other_inventory = other_records
    claimed_at = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    base_plan_overrides: dict[str, Any] = {
        "status": ExecutionPlanStatus.EXPIRED,
        "claimed_at": claimed_at,
    }
    cases: dict[str, dict[str, Any]] = {
        "job_project_null": {"project_id": None, "plan_overrides": dict(base_plan_overrides)},
        "job_playbook_null": {"playbook_path": None, "plan_overrides": dict(base_plan_overrides)},
        "job_limit_pattern_set": {
            "limit_pattern": "web*",
            "plan_overrides": dict(base_plan_overrides),
        },
        "project_mismatch": {
            "plan_overrides": {**base_plan_overrides, "project_id": other_project.id}
        },
        "inventory_mismatch": {
            "plan_overrides": {**base_plan_overrides, "inventory_id": other_inventory.id}
        },
        "playbook_mismatch": {
            "plan_overrides": {**base_plan_overrides, "playbook_path": "playbooks/web.yml"}
        },
        "actor_mismatch": {"plan_overrides": {**base_plan_overrides, "requested_by": OTHER_ACTOR}},
        "mode_mismatch": {
            "mode": ExecutionMode.NORMAL,
            "plan_overrides": dict(base_plan_overrides),
        },
    }
    return _seed(session, records, **cases[case])


@pytest.mark.parametrize("case", EXPIRED_CLAIMED_BROKEN_BINDINGS)
def test_a_broken_binding_stays_invisible_even_when_its_plan_was_durably_claimed(
    db_session: Session,
    records: tuple[Project, Inventory],
    other_records: tuple[Project, Inventory],
    case: str,
) -> None:
    """Genişleyen görünürlük yalnız **sağlam** bağlı satırlara uygulanır.

    Bu vakaların her biri düzeltme öncesinde de görünmezdi (plan ``claimed``
    değildi diye); düzeltme sonrasında da görünmez kalmalıdır — bu kez
    ``ExecutionPlanRecord`` ile ``Job`` arasındaki proje/inventory/playbook/
    aktör/kip/``limit_pattern`` bağı bozuk olduğu için.
    """
    healthy = _seed(db_session, records, created_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC))
    broken = _seed_expired_claimed_broken(db_session, records, other_records, case)

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    assert _ids(page) == [healthy]

    with pytest.raises(JobNotFoundError):
        get_playbook_job(db_session, broken, requested_by=ACTOR)


def test_the_plan_table_is_a_condition_not_a_source(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Sorgu plana **katılır** ama ondan hiçbir sütun okumaz.

    Ölçüm ifadenin kendisi üzerindedir: ``FROM``'da plan tablosu vardır (bağ
    SQL'de uygulanır, Python'da değil) ama ``SELECT`` listesinde yalnız ``jobs``
    sütunları bulunur. Plan alanlarını seçmek, token özetini, ``workspace_id``'yi
    ve digest'i materyalize edilmiş satıra taşırdı.
    """
    statement = str(read._authorized_statement(ACTOR))
    projection, _, remainder = statement.partition("FROM")

    assert "execution_plans" not in projection
    assert "JOIN execution_plans" in remainder
    for column in ("token_hash", "workspace_id", "manifest_digest", "input_fingerprint"):
        assert column not in statement, column

    # Ve gerçekten çalışır: sağlam satır hâlâ okunur.
    job_id = _seed(db_session, records)
    assert _ids(list_playbook_jobs(db_session, requested_by=ACTOR)) == [job_id]


def test_a_job_pointing_at_a_missing_plan_is_invisible(
    db_session: Session, records: tuple[Project, Inventory], migrated_engine: Engine
) -> None:
    """Var olmayan bir plana işaret eden Job da elenir.

    ``RESTRICT`` foreign key planın silinmesini engeller, ama doğrudan yazılmış
    bir satır hiç var olmamış bir plana işaret edebilir. ``INNER JOIN`` böyle
    bir satırı eşleştiremez; ``LEFT JOIN`` olsaydı satır plan alanları ``NULL``
    ile geçer ve yalnız "kimlik dolu" koşulunu sağlamış olurdu.
    """
    healthy = _seed(db_session, records)
    orphan = str(uuid.uuid4())
    _raw_sql(
        migrated_engine,
        "INSERT INTO jobs (id, job_type, status, inventory_id, project_id, playbook_path,"
        " execution_plan_id, requested_by, result_truncated, created_at)"
        " VALUES (?, 'playbook', 'successful', ?, ?, ?, ?, ?, 0, ?)",
        (
            orphan,
            records[1].id,
            records[0].id,
            PLAYBOOK_PATH,
            str(uuid.uuid4()),
            ACTOR,
            "2026-08-17 12:00:00.000000",
        ),
    )
    assert _legacy_visible(migrated_engine, orphan)

    page = list_playbook_jobs(db_session, requested_by=ACTOR)

    assert _ids(page) == [healthy]
    with pytest.raises(JobNotFoundError):
        get_playbook_job(db_session, orphan, requested_by=ACTOR)
    assert not db_session.in_transaction()


def test_a_healthy_binding_survives_every_status(
    db_session: Session, records: tuple[Project, Inventory], migrated_engine: Engine
) -> None:
    """Bağ sağlamken satır her durumda okunur: düzeltme meşru kaydı elemez.

    Aktif durumlar tek tek ölçülür; ``uq_jobs_active_playbook_global`` aynı anda
    yalnız bir aktif PLAYBOOK satırına izin verir.
    """
    for status in (
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.SUCCESSFUL,
        JobStatus.FAILED,
        JobStatus.CANCELED,
    ):
        job_id = _seed(db_session, records, status=status)

        summary = get_playbook_job(db_session, job_id, requested_by=ACTOR)
        assert summary.status is status
        assert summary.project_id == records[0].id
        assert summary.playbook_path == PLAYBOOK_PATH
        assert job_id in _ids(list_playbook_jobs(db_session, requested_by=ACTOR))

        # Sıradaki durumun aktif satır sınırına takılmaması için temizlenir.
        with Session(migrated_engine) as cleaner:
            cleaner.execute(delete(Job).where(Job.id == job_id))
            cleaner.execute(delete(ExecutionPlanRecord))
            cleaner.commit()


# --- get: mutlu yol ve tek cevap ----------------------------------------------


def test_get_returns_the_full_summary_of_one_job(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Tekil okuma listedeki özetin **aynısını** üretir."""
    project, inventory = records
    started = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    # Kimlik önceden üretilir: artifact yolu **tam olarak** bu Job'a ait
    # beklenen değer olmalıdır ve ancak böyle kurulabilir.
    job_id = str(uuid.uuid4())
    _seed(
        db_session,
        records,
        job_id=job_id,
        created_at=started - timedelta(minutes=1),
        status=JobStatus.SUCCESSFUL,
        started_at=started,
        finished_at=started + timedelta(seconds=42),
        return_code=0,
        artifact_path=_result_path(job_id),
    )

    summary = get_playbook_job(db_session, job_id, requested_by=ACTOR)

    assert summary.job_id == job_id
    assert summary.job_type == "playbook"
    assert summary.mode == "check"
    assert summary.status is JobStatus.SUCCESSFUL
    assert summary.project_id == project.id
    assert summary.project_name == project.name
    assert summary.inventory_id == inventory.id
    assert summary.inventory_name == inventory.name
    assert summary.playbook_path == PLAYBOOK_PATH
    assert summary.return_code == 0
    assert summary.error_code is None
    assert summary.result_truncated is False
    assert summary.has_recorded_result is True
    assert summary.started_at == started
    assert summary.finished_at == started + timedelta(seconds=42)

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    assert page.items == (summary,)


def test_a_normal_mode_job_is_visible_in_both_list_and_detail(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """R1-V3H2A: özet artık sabit ``check`` değildir; ``normal`` da okunabilir.

    Job ile onu yetkilendiren plan kaydı **aynı** (``normal``) kipi taşıdığı
    sürece satır, ``check`` bir satırdan farksız biçimde hem listede hem
    tekil okumada görünür.
    """
    job_id = _seed(
        db_session,
        records,
        mode=ExecutionMode.NORMAL,
        plan_overrides={"mode": ExecutionMode.NORMAL},
    )

    summary = get_playbook_job(db_session, job_id, requested_by=ACTOR)
    assert summary.mode is ExecutionMode.NORMAL

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    assert _ids(page) == [job_id]
    assert page.items[0].mode is ExecutionMode.NORMAL


@pytest.mark.parametrize(
    "invisible",
    ["missing", "other_actor", "ping", "planless"],
)
def test_every_invisible_job_produces_the_same_not_found(
    db_session: Session, records: tuple[Project, Inventory], invisible: str
) -> None:
    """Yok, başkasının, PING ve planless satır **aynı** 404'ü üretir.

    Ayrım yapmak, var olan bir Job'ın varlığını kimlik deneyerek öğrenmeyi
    mümkün kılardı: "başkasına ait" cevabı "böyle bir kayıt yok" cevabından
    farklı olduğu anda kimlik uzayı taranabilir hâle gelir.
    """
    if invisible == "missing":
        job_id = str(uuid.uuid4())
    elif invisible == "other_actor":
        job_id = _seed(db_session, records, requested_by=OTHER_ACTOR)
    elif invisible == "ping":
        job_id = _seed(
            db_session, records, job_type=JobType.PING, with_plan=False, playbook_path=None
        )
    else:
        job_id = _seed(db_session, records, with_plan=False)

    with pytest.raises(JobNotFoundError) as caught:
        get_playbook_job(db_session, job_id, requested_by=ACTOR)

    error = caught.value
    assert error.status_code == 404
    assert error.code == "job_not_found"
    assert error.details == {"reason": "not_found"}
    # Sabit mesaj: istenen kimliği, aktörü ve elenme sebebini taşımaz.
    assert job_id not in error.message
    assert ACTOR not in error.message
    assert OTHER_ACTOR not in error.message
    assert error.message == "Böyle bir çalıştırma kaydı bulunamadı."


def test_all_not_found_paths_share_one_message(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Dört elenme yolu tek bir (mesaj, kod, details) üçlüsüne düşer."""
    candidates = [
        str(uuid.uuid4()),
        _seed(db_session, records, requested_by=OTHER_ACTOR),
        _seed(db_session, records, job_type=JobType.PING, with_plan=False, playbook_path=None),
        _seed(db_session, records, with_plan=False),
    ]

    observed = set()
    for candidate in candidates:
        with pytest.raises(JobNotFoundError) as caught:
            get_playbook_job(db_session, candidate, requested_by=ACTOR)
        observed.add((caught.value.code, caught.value.message, json.dumps(caught.value.details)))

    assert len(observed) == 1


# --- Alan sözleşmesi ----------------------------------------------------------


def test_the_summary_carries_exactly_the_safe_field_set() -> None:
    """Dataclass ve cevap şeması **aynı** ve tam alan kümesini taşır."""
    assert {field.name for field in dataclasses.fields(PlaybookJobSummary)} == SAFE_SUMMARY_FIELDS
    assert set(PlaybookJobSummaryResponse.model_fields) == SAFE_SUMMARY_FIELDS
    for forbidden in FORBIDDEN_SUMMARY_FIELDS:
        assert forbidden not in SAFE_SUMMARY_FIELDS, forbidden


def test_no_secret_value_reaches_the_serialized_summary(
    db_session: Session, records: tuple[Project, Inventory], tmp_path: Any
) -> None:
    """Serileştirilmiş cevap ne aktörü ne plan izlerini ne de sunucu yolunu taşır.

    Ölçüm iki yönlüdür: yasak **alan adları** ve yasak **değerler**. Yalnız alan
    adına bakmak, sızıntının başka bir alanın içine gömülmesini kaçırırdı.
    """
    project, inventory = records
    job_id = _seed(db_session, records, artifact_path="jobs/x/result.json")
    plan = db_session.execute(select(ExecutionPlanRecord)).scalars().one()
    plan_id, workspace_id, digest, token_hash, fingerprint = (
        plan.id,
        plan.workspace_id,
        plan.manifest_digest,
        plan.token_hash,
        plan.input_fingerprint,
    )

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    rendered = PlaybookJobListResponse.model_validate(page).model_dump_json()
    body = json.loads(rendered)

    assert [item["job_id"] for item in body["items"]] == [job_id]
    for forbidden in FORBIDDEN_SUMMARY_FIELDS:
        assert forbidden not in body["items"][0], forbidden
    # Adlar (R1-V3J0B2) görünmesi **gereken** tek Project/Inventory
    # bilgisidir; kendi dosya yolları görünmez.
    assert body["items"][0]["project_name"] == project.name
    assert body["items"][0]["inventory_name"] == inventory.name
    for secret in (
        ACTOR,
        plan_id,
        workspace_id,
        digest,
        token_hash,
        fingerprint,
        str(tmp_path),
        "jobs/x/result.json",
        project.path,
        inventory.path,
    ):
        assert secret not in rendered, secret


def test_the_artifact_path_becomes_only_a_boolean(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """``artifact_path`` dışarı bir yol olarak değil, tek bir ``bool`` olarak çıkar.

    ``True`` yalnız kaydın **tam olarak** bu Job'a ait sonucu göstermesi
    demektir; başka bir Job'ın yolu veya serbest bir dizgi ``False``'tur.
    Dosyanın gerçekten var olduğu **iddia edilmez**: filesystem doğrulaması
    ayrı bir dilimdedir ve burada hiçbir dosya açılmaz.
    """
    stranger = str(uuid.uuid4())
    recorded = str(uuid.uuid4())
    _seed(
        db_session,
        records,
        job_id=recorded,
        created_at=datetime(2026, 8, 17, 12, 3, tzinfo=UTC),
        artifact_path=_result_path(recorded),
    )

    cases = {
        recorded: True,
        _seed(
            db_session,
            records,
            created_at=datetime(2026, 8, 17, 12, 2, tzinfo=UTC),
            artifact_path=None,
        ): False,
        # Başka bir Job'ın yayımlanmış sonucu.
        _seed(
            db_session,
            records,
            created_at=datetime(2026, 8, 17, 12, 1, tzinfo=UTC),
            artifact_path=_result_path(stranger),
        ): False,
        # Absolute bir yol asla "kayıtlı sonuç" sayılmaz.
        _seed(
            db_session,
            records,
            created_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
            artifact_path="/srv/app-data/jobs/leak/result.json",
        ): False,
    }

    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    observed = {item.job_id: item.has_recorded_result for item in page.items}

    assert observed == cases


# --- Hata kodu daraltması -----------------------------------------------------


@pytest.mark.parametrize("code", sorted(PUBLIC_ERROR_CODES))
def test_a_known_error_code_is_carried_verbatim(
    db_session: Session, records: tuple[Project, Inventory], code: str
) -> None:
    """Allowlist'teki kod olduğu gibi taşınır."""
    job_id = _seed(db_session, records, status=JobStatus.FAILED, error_code=code)

    summary = get_playbook_job(db_session, job_id, requested_by=ACTOR)

    assert summary.error_code == code


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "",
        "beklenmedik_kod",
        "RUNNER_FAILED",
        "/srv/app-data/execution-plans/9f2c/project/site.yml bulunamadı",
        "OperationalError: no such column: aselai_secret_token",
    ],
)
def test_an_unknown_error_code_collapses_to_unknown_failure(
    db_session: Session, records: tuple[Project, Inventory], stored: str | None
) -> None:
    """Tanınmayan, boş veya serbest metin bir kodun tek karşılığı vardır.

    Ham değer cevaba **hiç** ulaşmaz: bir workspace yolu ya da exception metni
    olarak yazılmış tek bir satır, sonucun okunabildiği her yerde görünür
    olurdu.
    """
    job_id = _seed(db_session, records, status=JobStatus.FAILED, error_code=stored)

    summary = get_playbook_job(db_session, job_id, requested_by=ACTOR)

    assert summary.error_code == UNKNOWN_FAILURE
    rendered = PlaybookJobSummaryResponse.model_validate(summary).model_dump_json()
    if stored:
        assert stored not in rendered
        assert stored[:12] not in rendered


@pytest.mark.parametrize(
    "status", [JobStatus.SUCCESSFUL, JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CANCELED]
)
def test_a_non_failed_row_never_reports_an_error_code(
    db_session: Session, records: tuple[Project, Inventory], status: JobStatus
) -> None:
    """``failed`` olmayan bir satırın hata kodu daima ``None``'dır.

    Beklenmedik biçimde kod taşıyan bir ``successful`` satır kaydın kendisiyle
    çelişir; onu dışarı taşımak "başarılı ama şu hatayla" gibi okunamaz bir
    sonuç üretirdi. Aktif durumlar tek tek ölçülür: aynı anda yalnız bir aktif
    PLAYBOOK satırı bulunabilir.
    """
    job_id = _seed(db_session, records, status=status, error_code="runner_timeout")

    summary = get_playbook_job(db_session, job_id, requested_by=ACTOR)

    assert summary.status is status
    assert summary.error_code is None


# --- Girdi doğrulaması SQL'den önce -------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_LIMIT + 1, 10_000, True])
def test_an_out_of_range_limit_never_reaches_the_database(
    db_session: Session, counted_statements: list[str], limit: Any
) -> None:
    """Aralık dışı ``limit`` (ve ``bool``) sorgu turu hak etmez."""
    with pytest.raises(ValueError):
        list_playbook_jobs(db_session, requested_by=ACTOR, limit=limit)
    assert counted_statements == []


@pytest.mark.parametrize("project_id", [0, -1, True])
def test_a_non_positive_project_id_never_reaches_the_database(
    db_session: Session, counted_statements: list[str], project_id: Any
) -> None:
    """``project_id >= 1``; sıfır, negatif ve ``bool`` reddedilir."""
    with pytest.raises(ValueError):
        list_playbook_jobs(db_session, requested_by=ACTOR, project_id=project_id)
    assert counted_statements == []


@pytest.mark.parametrize("status", ["failed", "FAILED", 3, "successful"])
def test_a_raw_string_status_never_reaches_the_database(
    db_session: Session, counted_statements: list[str], status: Any
) -> None:
    """Filtre ham dizgiden değil tip sisteminden gelir.

    ``JobStatus`` bir ``StrEnum`` olduğu için ``"failed"`` üyeye eşit sayılır ve
    tek başına bir ``in`` kontrolünü geçerdi; yazım hatası taşıyan bir dizgi de
    sessizce hiçbir satır döndürür ve "hiç iş yok" gibi okunurdu.
    """
    with pytest.raises(ValueError):
        list_playbook_jobs(db_session, requested_by=ACTOR, status=status)
    assert counted_statements == []


@pytest.mark.parametrize("mode", ["check", "normal", "CHECK", 3])
def test_a_raw_string_mode_never_reaches_the_database(
    db_session: Session, counted_statements: list[str], mode: Any
) -> None:
    """Kip filtresi ham dizgiden değil tip sisteminden gelir (R1-V3J0B2).

    :class:`ExecutionMode` bir ``StrEnum`` olduğu için ``"check"`` üyeye eşit
    sayılır ve tek başına bir ``in`` kontrolünü geçerdi; servis katmanı ham bir
    dizgiyi kabul etseydi filtre çağıranın elindeki serbest metinden gelirdi.
    """
    with pytest.raises(ValueError):
        list_playbook_jobs(db_session, requested_by=ACTOR, mode=mode)
    assert counted_statements == []


@pytest.mark.parametrize(
    "job_id",
    [
        "",
        "kisa",
        "../../etc/hosts",
        "9F2C4B1E-1111-4222-8333-444455556666",
        str(uuid.uuid1()),
        "{" + str(uuid.uuid4()) + "}",
    ],
)
def test_a_non_canonical_uuid_never_reaches_the_database(
    db_session: Session, counted_statements: list[str], job_id: str
) -> None:
    """Biçimsiz, büyük harfli, UUID1 ve süslü parantezli kimlik reddedilir."""
    with pytest.raises(ValueError):
        get_playbook_job(db_session, job_id, requested_by=ACTOR)
    assert counted_statements == []


@pytest.mark.parametrize(
    ("created_at", "job_id"),
    [
        (datetime(2026, 8, 17, 12, 0, tzinfo=UTC), None),
        (None, str(uuid.uuid4())),
    ],
)
def test_a_half_cursor_never_reaches_the_database(
    db_session: Session,
    counted_statements: list[str],
    created_at: datetime | None,
    job_id: str | None,
) -> None:
    """Cursor alanları birlikte verilir ya da hiçbiri; yarım cursor reddedilir."""
    with pytest.raises(ValueError):
        list_playbook_jobs(
            db_session,
            requested_by=ACTOR,
            before_created_at=created_at,
            before_job_id=job_id,
        )
    assert counted_statements == []


def test_a_malformed_cursor_id_never_reaches_the_database(
    db_session: Session, counted_statements: list[str]
) -> None:
    """Cursor kimliği de canonical UUID4 olmalıdır."""
    with pytest.raises(ValueError):
        list_playbook_jobs(
            db_session,
            requested_by=ACTOR,
            before_created_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
            before_job_id="not-a-uuid",
        )
    assert counted_statements == []


# --- Zaman sözleşmesi ---------------------------------------------------------


def test_a_naive_cursor_from_the_caller_is_refused(
    db_session: Session, counted_statements: list[str]
) -> None:
    """Çağıranın verdiği naive zaman reddedilir; sessizce UTC sayılmaz.

    Naive bir değeri UTC kabul etmek sunucunun yerel saatini UTC ilan etmek
    olurdu ve sayfa sınırını saat farkı kadar kaydırırdı.
    """
    with pytest.raises(ValueError):
        list_playbook_jobs(
            db_session,
            requested_by=ACTOR,
            before_created_at=datetime(2026, 8, 17, 12, 0),
            before_job_id=str(uuid.uuid4()),
        )
    assert counted_statements == []


def test_a_non_utc_cursor_is_converted_not_refused(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Aware ama UTC olmayan cursor **çevrilir**: aynı an aynı sayfayı verir."""
    origin = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    newer = _seed(db_session, records, created_at=origin + timedelta(minutes=1))
    _seed(db_session, records, created_at=origin)

    page = list_playbook_jobs(
        db_session,
        requested_by=ACTOR,
        before_created_at=(origin + timedelta(minutes=1)).astimezone(timezone(timedelta(hours=3))),
        before_job_id=newer,
    )

    assert len(page.items) == 1
    assert page.items[0].created_at == origin


def test_a_naive_database_timestamp_is_read_as_utc(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """SQLite'ın tzinfo'suz döndürdüğü zaman **okuma yönünde** UTC kabul edilir.

    Sütun ``DateTime(timezone=True)``'dır ama SQLite offset saklamaz; tek doğru
    yorum "DB UTC saklar" sözleşmesidir. Bu varsayım yalnız burada yapılır ve
    çağıranın naive cursor'ı hâlâ reddedilir (bkz. yukarıdaki test).
    """
    moment = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    job_id = _seed(db_session, records, created_at=moment, status=JobStatus.PENDING)

    stored = db_session.execute(select(Job.created_at).where(Job.id == job_id)).scalar_one()
    summary = get_playbook_job(db_session, job_id, requested_by=ACTOR)

    # Ham okuma gerçekten naive'dir; sözleşmenin uygulandığı yer servistir.
    assert stored.tzinfo is None
    assert summary.created_at.tzinfo is not None
    assert summary.created_at.utcoffset() == timedelta(0)
    assert summary.created_at == moment
    # Cevap şeması da UTC ister; naive bir damga buradan geçemezdi.
    rendered = PlaybookJobSummaryResponse.model_validate(summary)
    assert rendered.created_at == moment


# --- Transaction hijyeni ------------------------------------------------------


def test_a_populated_page_leaves_no_open_transaction(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Dolu sayfa çağırana açık okuma transaction'ı devretmez."""
    _seed_series(db_session, records, 3)

    page = list_playbook_jobs(db_session, requested_by=ACTOR, limit=2)

    assert page.has_more is True
    assert not db_session.in_transaction()


def test_an_empty_page_leaves_no_open_transaction(db_session: Session) -> None:
    """Boş sayfa da transaction bırakmaz."""
    page = list_playbook_jobs(db_session, requested_by=ACTOR)

    assert page.items == ()
    assert not db_session.in_transaction()


def test_a_successful_get_leaves_no_open_transaction(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """Bulunan Job sonrasında da transaction kapalıdır."""
    job_id = _seed(db_session, records)

    get_playbook_job(db_session, job_id, requested_by=ACTOR)

    assert not db_session.in_transaction()


def test_a_not_found_get_leaves_no_open_transaction(
    db_session: Session, records: tuple[Project, Inventory]
) -> None:
    """404 yolu da transaction bırakmaz: rollback hatadan **önce** yapılır."""
    other = _seed(db_session, records, requested_by=OTHER_ACTOR)

    with pytest.raises(JobNotFoundError):
        get_playbook_job(db_session, other, requested_by=ACTOR)

    assert not db_session.in_transaction()


def test_the_service_writes_nothing(
    db_session: Session, records: tuple[Project, Inventory], counted_statements: list[str]
) -> None:
    """Okuma yolunda ``INSERT``/``UPDATE``/``DELETE``/``COMMIT`` yoktur."""
    job_id = _seed(db_session, records)
    counted_statements.clear()

    list_playbook_jobs(db_session, requested_by=ACTOR)
    get_playbook_job(db_session, job_id, requested_by=ACTOR)

    assert counted_statements != []
    for statement in counted_statements:
        head = " ".join(statement.split()).upper()
        assert head.startswith("SELECT"), statement


def _breaks_job_select(statement: str) -> bool:
    """Yalnız Job okumasını tanır; kurulum yazmalarını değil."""
    normalized = " ".join(statement.split()).lower()
    return normalized.startswith("select") and " from jobs" in normalized


@pytest.mark.parametrize("operation", ["list", "get"])
def test_a_failing_select_rolls_back_and_re_raises(
    db_session: Session,
    records: tuple[Project, Inventory],
    migrated_engine: Engine,
    operation: str,
) -> None:
    """Okuma arızası boş sayfa/404 diye gizlenmez; session yeniden kullanılabilir.

    Arıza taklit **edilmez**: listener yalnız Job ``SELECT``'ini bozar, hatayı
    gerçek sqlite cursor'ı üretir. Ölçülen, hatanın olduğu gibi yükselmesi,
    session'ın açık transaction bırakmaması ve arıza kalktıktan sonra **aynı**
    session'ın çalışmaya devam etmesidir.
    """
    job_id = _seed(db_session, records)
    targeted: list[str] = []

    def _break_select(
        _conn: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> tuple[str, Any]:
        if _breaks_job_select(statement):
            targeted.append(statement)
            return "SELECT aselai_injected_read_failure FROM jobs", parameters
        return statement, parameters

    event.listen(migrated_engine, "before_cursor_execute", _break_select, retval=True)
    try:
        with pytest.raises(SQLAlchemyError):
            if operation == "list":
                list_playbook_jobs(db_session, requested_by=ACTOR)
            else:
                get_playbook_job(db_session, job_id, requested_by=ACTOR)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _break_select)

    assert len(targeted) == 1, targeted
    assert not db_session.in_transaction()

    # Aynı session arıza kalktıktan sonra sağlamdır.
    page = list_playbook_jobs(db_session, requested_by=ACTOR)
    assert _ids(page) == [job_id]


def _rows(engine: Engine) -> tuple[list[Any], list[Any]]:
    """Job ve plan satırlarının bağımsız bir bağlantıdan alınmış anlık görüntüsü."""
    with Session(engine) as observer:
        jobs = observer.execute(
            select(Job.id, Job.status, Job.error_code, Job.artifact_path).order_by(Job.id)
        ).all()
        plans = observer.execute(
            select(ExecutionPlanRecord.id, ExecutionPlanRecord.status).order_by(
                ExecutionPlanRecord.id
            )
        ).all()
    return list(jobs), list(plans)


@pytest.mark.parametrize("operation", ["list", "get"])
def test_a_conversion_failure_rolls_back_and_re_raises(
    db_session: Session,
    records: tuple[Project, Inventory],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """``SELECT`` bittikten **sonra** çıkan hata da transaction bırakmaz.

    Bu, arıza sınırının veritabanı hatalarıyla sınırlı olmadığının kanıtıdır.
    Dönüşüm ``execute``'un açtığı transaction'ın **içindedir**: yalnız
    ``SQLAlchemyError`` yakalansaydı buradaki ``RuntimeError`` session'ı çağırana
    açık bir okuma kilidiyle devrederdi — sonraki yazma beklerdi ve kilidin
    nereden geldiği hiçbir yerde görünmezdi.

    Hata **çevrilmez**: boş sayfa, 404 veya bir ``AppError`` değil, aynı nesne
    yükselir.
    """
    job_id = _seed(db_session, records)
    before = _rows(migrated_engine)
    sentinel = RuntimeError("aselai_injected_conversion_failure")
    converted: list[Any] = []

    def _explode(row: Any) -> PlaybookJobSummary:
        # Çağrılmış olması, `SELECT`'in gerçekten tamamlandığını kanıtlar:
        # sorgu düşseydi dönüşüme hiç sıra gelmezdi.
        converted.append(row)
        raise sentinel

    monkeypatch.setattr(read, "_to_summary", _explode)
    with pytest.raises(RuntimeError) as caught:
        if operation == "list":
            list_playbook_jobs(db_session, requested_by=ACTOR)
        else:
            get_playbook_job(db_session, job_id, requested_by=ACTOR)

    assert caught.value is sentinel
    assert len(converted) == 1
    assert not db_session.in_transaction()
    # Salt-okunur servis hiçbir satırı değiştirmedi.
    assert _rows(migrated_engine) == before

    monkeypatch.undo()

    # Aynı session hem ham sorgu hem de normal read çağrısı yapabilir. Ham
    # sorgunun açtığı transaction testin kendisine aittir ve burada kapatılır;
    # ölçülen, servisin bıraktığı sınırdır.
    assert db_session.execute(select(Job.id)).scalars().all() == [job_id]
    db_session.rollback()

    assert _ids(list_playbook_jobs(db_session, requested_by=ACTOR)) == [job_id]
    assert get_playbook_job(db_session, job_id, requested_by=ACTOR).job_id == job_id
    assert not db_session.in_transaction()


# --- Kapsam kilidi ------------------------------------------------------------


def test_the_read_service_imports_no_execution_or_route_layer() -> None:
    """Modülün **gerçek** import listesi bir sözleşmedir ve tam eşitlikle ölçülür.

    Docstring'de geçen bir modül adı testi ne geçirir ne düşürür; ölçülen AST'in
    kendisidir. Dosya sistemi, artifact deposu, subprocess, runner, worker ve
    HTTP katmanı buraya giremez: salt-okunur bir sorgu servisinin bunların
    hiçbirine ihtiyacı yoktur ve birine bağlanması, okuma yolunun sessizce yan
    etki üretebileceği anlamına gelirdi.
    """
    tree = ast.parse(inspect.getsource(read))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported == {
        "__future__",
        "uuid",
        "dataclasses",
        "datetime",
        "typing",
        "sqlalchemy",
        "sqlalchemy.orm",
        "app.core.errors",
        "app.models",
    }
    for forbidden in (
        "os",
        "pathlib",
        "shutil",
        "subprocess",
        "threading",
        "ansible_runner",
        "fastapi",
        "app.api.routes.executions",
        "app.core.config",
        "app.services.jobs.artifacts",
        "app.services.execution.executor",
        "app.services.execution.runner_process",
        "app.services.execution.store",
        "app.services.execution.worker",
        "app.services.execution.workspace",
    ):
        assert forbidden not in imported, forbidden


def test_the_public_error_allowlist_matches_the_schema_literal() -> None:
    """Servisin allowlist'i ile şemanın ``Literal``'ı ayrışamaz.

    ``Literal`` runtime bir ``frozenset``'ten üretilemez; iki tanım bu yüzden
    ayrı durur ve eşitlikleri burada sabitlenir. Ayrışmaları, serviste kabul
    edilen bir kodun serileştirme sınırında düşmesi demek olurdu.
    """
    from typing import get_args

    literal = PlaybookJobSummaryResponse.model_fields["error_code"].annotation
    assert PUBLIC_ERROR_CODES == frozenset(get_args(get_args(literal)[0]))
    assert UNKNOWN_FAILURE in PUBLIC_ERROR_CODES


def test_the_job_read_surface_is_exactly_list_detail_and_result(client: TestClient) -> None:
    """Kapsam kilidi: Job'ı **okuyan** yüzey R1-V3D2B ile GET'e sınırlı üçtür.

    ``GET /api/jobs`` (D2A1) ve ``GET /api/jobs/{job_id}/result`` (D2A2B2) artık
    bağlıdır; bunların dışında ne bir mutasyon (POST/PATCH/DELETE) ne de fazladan
    bir okuma yolu vardır. Bu servisin kendisi değil, HTTP yüzeyinin sözleşmesi
    ölçülür.

    Toplam operasyon sayısının tarihçesi: R1-V3J0C controller path browse yolunu
    ekleyerek 19→20, R1-V3J1 persistent ping history ``ping-runs`` yolunu
    ekleyerek 20→21 yaptı. R1-V3J2 yalnız **frontend** cursor pagination'dı ve
    R1-V3J3A yalnız mevcut sonuç cevabının şemasını genişletti; ikisi de backend
    route eklemedi.
    """
    spec = client.get("/openapi.json").json()

    assert set(spec["paths"]) == {
        "/health",
        "/api/projects",
        "/api/projects/{project_id}",
        "/api/projects/{project_id}/playbooks",
        "/api/projects/{project_id}/execution-plan",
        "/api/projects/{project_id}/execution-plans",
        "/api/projects/{project_id}/executions",
        "/api/inventories",
        "/api/inventories/{inventory_id}",
        "/api/inventories/{inventory_id}/hosts",
        "/api/inventories/{inventory_id}/ping",
        "/api/inventories/{inventory_id}/ping/preview",
        "/api/inventories/{inventory_id}/ping/preview/cancel",
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/jobs/{job_id}/result",
        # R1-V3J0C: Project/Inventory formlarının "Gözat…" dialogu için tek,
        # salt-okunur controller path browse yolu.
        "/api/controller-paths",
        # R1-V3J1: kalıcı ping geçmişi için tek, salt-okunur liste yolu.
        "/api/inventories/{inventory_id}/ping-runs",
    }
    assert sum(len(operations) for operations in spec["paths"].values()) == 21
    assert set(spec["paths"]["/api/projects/{project_id}/executions"]) == {"post"}
    # Job yüzeyinin üç yolu da yalnız GET'tir; ekleme, güncelleme veya silme yok.
    for path in ("/api/jobs", "/api/jobs/{job_id}", "/api/jobs/{job_id}/result"):
        assert set(spec["paths"][path]) == {"get"}

    for name in (
        "PlaybookJobSummaryResponse",
        "PlaybookJobListResponse",
        "PlaybookJobCursorResponse",
    ):
        assert name in spec.get("components", {}).get("schemas", {}), name


def test_the_default_page_limit_is_bounded() -> None:
    """Varsayılan sayfa boyutu üst sınırın içindedir ve sınırsız bir liste yoktur."""
    assert 1 <= DEFAULT_PAGE_LIMIT <= MAX_PAGE_LIMIT
    assert MAX_PAGE_LIMIT == 100
