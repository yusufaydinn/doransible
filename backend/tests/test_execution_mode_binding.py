"""Execution mode'un plan → fingerprint → claim → Job zinciri (R1-V3H1B1).

R1-V3H1A kipi yalnız **şema** olarak kurdu: iki tablo bir sütun ve ortak bir tip
kazandı, ama üretim kodu kipi hiçbir yere yazmıyordu. Bu dilim o boşluğu
kapatır — kip artık hazırlanmış planın kalıcı parçasıdır ve zincirin her
halkasında **değişmez** biçimde bağlıdır.

Ölçülen dört sınır:

1. *Özet.* ``mode`` girdi özetine canonical enum değeriyle girer; check ve
   normal aynı diğer girdilerle bile aynı özeti üretemez.
2. *Kalıcılık.* Plan satırı kipi ORM/DB varsayılanından değil, çağıranın
   **açıkça** verdiği değerden alır.
3. *Claim.* Atomik UPDATE kipi iki bağımsız biçimde arar: okunabilir sütun ve
   özet. Yanlış kiple yapılan deneme hiçbir satırı eşleştirmez ve bileti
   **tüketmez**; aynı bilet doğru kiple sonradan hâlâ çalışır.
4. *Miras.* Job'un kipi caller'ın beklentisinden veya sütun varsayılanından
   değil, **claim edilen plan satırından** gelir.

Ölçümler gerçek dondurulmuş workspace ve gerçek plan zinciri üzerinde yapılır;
"şu fonksiyon şu kadar çağrıldı" sayımıyla yetinilmez. Yanlış-kip testinin
vacuous olmadığı, aynı token'ın doğru kiple sonradan tam bir Job üretmesiyle
kanıtlanır.

**Public yüzey R1-V3H2A ile açılmıştır.** Bu dosya yazıldığında (R1-V3H1B1)
istemcinin kip söyleyebileceği bir alan yoktu; artık plan/prepare/launch
istek şemaları zorunlu bir ``mode`` alanı taşır ve seçilen kip yukarıdaki dört
sınırın **aynısından** geçer — sözleşme burada değişmez, yalnız girdinin
kaynağı değişir (sunucu sabiti yerine istemcinin seçimi). Uçtan uca HTTP
sözleşmesi (mismatch, token hayatta kalması, response mode'unun kaynağı)
``tests/test_execution_prepare_api.py`` ve ``tests/test_execution_launch_api.py``
içinde ölçülür; burada yalnız request/response şemalarının `mode`'u doğru
bağladığı doğrulanır. Runner argv'si kipi R1-V3H1B2B'den beri **okur**:
``build_runner_arguments`` zorunlu bir ``mode`` alır ve ``--cmdline=--check``'i
yalnız ``ExecutionMode.CHECK`` için ekler. Bu dosyanın son bölümü o kilitleri
de ölçer.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    ExecutionMode,
    ExecutionPlanRecord,
    ExecutionPlanStatus,
    Inventory,
    InventorySourceType,
    Job,
    Project,
)
from app.services.execution import authorize as authorize_module
from app.services.execution import workspace as ws
from app.services.execution.authorize import claim_and_reserve_playbook_job
from app.services.execution.store import (
    ExecutionPlanInvalidError,
    claim_plan_row,
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


def _fingerprint(
    project: Project, inventory: Inventory, *, mode: ExecutionMode = ExecutionMode.CHECK
) -> str:
    """Hazırlama yolunun ürettiği özet; kip dışındaki her şey sabit tutulur."""
    return input_fingerprint(
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        mode=mode,
        connection="ssh",
        become=False,
        limit=None,
        tags=None,
        skip_tags=None,
        host_key_policy=POLICY,
    )


def _prepare(
    session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    *,
    mode: ExecutionMode,
) -> str:
    """Verilen kip için gerçek bir dondurulmuş plan hazırlar; token döndürür.

    Normal mode'u üretebilen tek yol budur: public hazırlama sözleşmesi hâlâ
    check-only'dir, bu yüzden normal plan yalnız store primitive'i üzerinden
    temsil edilebilir.
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
        fingerprint=_fingerprint(project, inventory, mode=mode),
        mode=mode,
        requested_by=ACTOR,
        workspace_id=frozen.workspace_id,
        manifest_digest=frozen.manifest_digest,
        ttl_seconds=TTL,
    )
    return prepared.token


def _authorize(
    session: Session,
    workspace_root: Path,
    records: tuple[Project, Inventory],
    token: str,
    *,
    mode: ExecutionMode,
) -> Any:
    project, inventory = records
    return claim_and_reserve_playbook_job(
        session,
        token=token,
        project_id=project.id,
        inventory_id=inventory.id,
        playbook_path=PLAYBOOK_PATH,
        fingerprint=_fingerprint(project, inventory, mode=mode),
        mode=mode,
        requested_by=ACTOR,
        workspace_root=workspace_root,
    )


def _plan(session: Session) -> ExecutionPlanRecord:
    session.expire_all()
    return session.execute(select(ExecutionPlanRecord)).scalar_one()


def _jobs(session: Session) -> list[Job]:
    session.expire_all()
    return list(session.execute(select(Job)).scalars().all())


# --- 1. Fingerprint ----------------------------------------------------------


def test_check_and_normal_produce_different_fingerprints(
    records: tuple[Project, Inventory],
) -> None:
    """Kip özete gerçekten bağlıdır: diğer her girdi aynıyken özetler ayrışır.

    Ayrışmasaydı, check için hazırlanmış bir bilet normal mode çalıştırmanın
    beklediği özetle eşleşir ve kip kısıtı yalnız sütun karşılaştırmasına
    kalırdı.
    """
    project, inventory = records

    check = _fingerprint(project, inventory, mode=ExecutionMode.CHECK)
    normal = _fingerprint(project, inventory, mode=ExecutionMode.NORMAL)

    assert check != normal


def test_the_fingerprint_uses_the_canonical_enum_value(
    records: tuple[Project, Inventory],
) -> None:
    """Canonical gövdeye enum'un ``repr``'i değil, sözleşmedeki değer girer.

    Beklenen özet burada **elle** kurulur: üretim kodunun kendi serialization'ı
    kullanılsaydı, ``"ExecutionMode.CHECK"`` yazan bir gerileme de testi
    geçerdi. Değerin ``"check"``/``"normal"`` olması kalıcı token
    uyumluluğunun parçasıdır.
    """
    project, inventory = records

    for mode in (ExecutionMode.CHECK, ExecutionMode.NORMAL):
        canonical = json.dumps(
            {
                "project_id": project.id,
                "inventory_id": inventory.id,
                "playbook_path": PLAYBOOK_PATH,
                "mode": mode.value,
                "connection": "ssh",
                "become": False,
                "limit": None,
                "tags": None,
                "skip_tags": None,
                "host_key_policy": POLICY,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        assert _fingerprint(project, inventory, mode=mode) == expected
        assert f'"mode":"{mode.value}"' in canonical


# --- 2. Plan satırının kalıcılığı --------------------------------------------


def test_the_store_primitive_writes_the_mode_it_is_given(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Explicit ``normal`` verildiğinde satır ``normal`` olur; varsayılan ezmez.

    Sütunun ORM/DB varsayılanı ``check``'tir. Yazılan değer varsayılandan
    türeseydi bu iddia mümkün olmazdı: normal plan hiç temsil edilemez, kip
    zincirin ilk halkasında sessizce düşerdi.
    """
    _prepare(db_session, workspace_root, source_project, records, mode=ExecutionMode.NORMAL)

    assert _plan(db_session).mode is ExecutionMode.NORMAL
    stored = db_session.execute(text("SELECT mode FROM execution_plans")).scalar_one()
    assert stored == "normal"


def test_mode_is_a_required_keyword_only_argument_without_a_default() -> None:
    """İki primitive de kipi **istemek** zorundadır; sessizce varsayamaz.

    Varsayılanı olan bir parametre, kipi hiç düşünmeyen bir çağrının doğru
    davranıyormuş gibi görünmesine izin verirdi. Pozisyonel olabilseydi de
    ``fingerprint`` ile ``mode`` çağrı yerinde yer değiştirebilirdi.
    """
    for function in (store_prepared_plan, claim_plan_row, claim_and_reserve_playbook_job):
        parameter = inspect.signature(function).parameters["mode"]

        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, function.__name__
        assert parameter.default is inspect.Parameter.empty, function.__name__


# --- 3. Claim kipe bağlıdır --------------------------------------------------


def test_a_normal_plan_is_refused_by_a_claim_expecting_check(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Normal hazırlanmış bilet, check bekleyen claim ile eşleşmez.

    Ret **genel** ``execution_plan_invalid`` ile döner: kipe özgü bir kod,
    elindeki token'ın hangi kip için hazırlandığını deneme yanılmayla
    öğrenilebilir kılan bir oracle olurdu.
    """
    token = _prepare(db_session, workspace_root, source_project, records, mode=ExecutionMode.NORMAL)

    with pytest.raises(ExecutionPlanInvalidError) as error:
        _authorize(db_session, workspace_root, records, token, mode=ExecutionMode.CHECK)

    assert error.value.code == "execution_plan_invalid"
    assert error.value.status_code == 409
    assert error.value.details == {"reason": "invalid"}


def test_a_wrong_mode_attempt_neither_consumes_the_token_nor_creates_a_job(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Yanlış kip: plan ``prepared`` kalır, Job yok, bilet **yanmaz**.

    Testin vacuous olmadığı son adımda kanıtlanır: aynı token doğru kiple
    yeniden denendiğinde tam olarak bir Job üretir. Bu adım olmasaydı, claim'i
    her koşulda reddeden bir gerileme de testi geçerdi.
    """
    token = _prepare(db_session, workspace_root, source_project, records, mode=ExecutionMode.NORMAL)

    with pytest.raises(ExecutionPlanInvalidError):
        _authorize(db_session, workspace_root, records, token, mode=ExecutionMode.CHECK)

    plan = _plan(db_session)
    assert plan.status is ExecutionPlanStatus.PREPARED
    assert plan.claimed_at is None
    assert plan.mode is ExecutionMode.NORMAL
    assert _jobs(db_session) == []

    # Bilet hâlâ elde: doğru kiple tam olarak bir Job doğar.
    authorized = _authorize(db_session, workspace_root, records, token, mode=ExecutionMode.NORMAL)
    assert [job.id for job in _jobs(db_session)] == [authorized.job_id]
    assert _plan(db_session).status is ExecutionPlanStatus.CLAIMED


def test_the_claim_condition_binds_mode_in_two_independent_ways(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Sütun koşulu özetten **bağımsızdır**: doğru özet tek başına yetmez.

    Plan normal hazırlanmıştır; claim doğru (normal) özeti verir ama beklenen
    kip olarak ``check`` söyler. Kip yalnız fingerprint üzerinden bağlansaydı bu
    çağrı geçerdi. Sütun koşulu olduğu için hiçbir satır eşleşmez.
    """
    project, inventory = records
    token = _prepare(db_session, workspace_root, source_project, records, mode=ExecutionMode.NORMAL)

    with pytest.raises(ExecutionPlanInvalidError):
        claim_plan_row(
            db_session,
            token=token,
            project_id=project.id,
            inventory_id=inventory.id,
            playbook_path=PLAYBOOK_PATH,
            # Özet planınkiyle **aynı**; ayrışan tek şey beklenen kiptir.
            fingerprint=_fingerprint(project, inventory, mode=ExecutionMode.NORMAL),
            mode=ExecutionMode.CHECK,
            requested_by=ACTOR,
            now=datetime.now(UTC),
        )
    db_session.rollback()

    assert _plan(db_session).status is ExecutionPlanStatus.PREPARED


def test_a_reused_token_is_still_refused_in_either_mode(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
) -> None:
    """Tek kullanım garantisi kipten bağımsızdır: ikinci deneme Job üretmez."""
    token = _prepare(db_session, workspace_root, source_project, records, mode=ExecutionMode.NORMAL)
    authorized = _authorize(db_session, workspace_root, records, token, mode=ExecutionMode.NORMAL)

    for mode in (ExecutionMode.NORMAL, ExecutionMode.CHECK):
        with pytest.raises(ExecutionPlanInvalidError):
            _authorize(db_session, workspace_root, records, token, mode=mode)

    assert [job.id for job in _jobs(db_session)] == [authorized.job_id]


# --- 4. Job kipi plandan miras alır ------------------------------------------


@pytest.mark.parametrize("mode", [ExecutionMode.CHECK, ExecutionMode.NORMAL])
def test_the_job_carries_the_mode_of_the_plan_that_authorized_it(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    mode: ExecutionMode,
) -> None:
    """Check plan → check Job; normal plan → normal Job.

    Üç yüzey de aynı değeri gösterir: plan satırı, dönen
    :class:`AuthorizedPlaybookJob` ve veritabanındaki Job satırı.
    """
    token = _prepare(db_session, workspace_root, source_project, records, mode=mode)

    authorized = _authorize(db_session, workspace_root, records, token, mode=mode)

    plan = _plan(db_session)
    jobs = _jobs(db_session)
    assert len(jobs) == 1
    assert plan.mode is mode
    assert jobs[0].mode is mode
    assert authorized.mode is mode
    stored = db_session.execute(text("SELECT mode FROM jobs")).scalar_one()
    assert stored == mode.value


def test_the_job_mode_comes_from_the_claimed_row_not_the_caller_or_the_default(
    db_session: Session,
    workspace_root: Path,
    source_project: Path,
    records: tuple[Project, Inventory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kipin kaynağı ölçülür: caller ``check`` der, plan satırı ``normal``dir.

    Mutlu yolda caller'ın beklediği kip ile plan satırının kipi **zorunlu
    olarak** aynıdır (claim koşulu bunu şart koşar), bu yüzden ikisini ayırt
    etmek ancak claim ile Job INSERT arasında satırı ayrıştırarak mümkündür.
    Gerçek claim çalışır; yalnız dönen kaydın kipi ayrıştırılır.

    İki gerileme birden yakalanır: caller'ın değerini Job'a kopyalayan bir
    uygulama ``check`` yazardı, sütun varsayılanına yaslanan bir uygulama da
    ``check`` yazardı. Doğru uygulama satırdan okur ve ``normal`` yazar.
    """
    real_claim = authorize_module.claim_plan_row
    seen: list[ExecutionMode] = []

    def _diverging_claim(session: Session, **kwargs: Any) -> ExecutionPlanRecord:
        record = real_claim(session, **kwargs)
        seen.append(kwargs["mode"])
        # Plan satırı caller'ın beklentisinden ayrıştırılır.
        record.mode = ExecutionMode.NORMAL
        return record

    monkeypatch.setattr(authorize_module, "claim_plan_row", _diverging_claim)

    token = _prepare(db_session, workspace_root, source_project, records, mode=ExecutionMode.CHECK)
    authorized = _authorize(db_session, workspace_root, records, token, mode=ExecutionMode.CHECK)

    assert seen == [ExecutionMode.CHECK], "claim koşuluna caller'ın kipi gitmeli"
    jobs = _jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].mode is ExecutionMode.NORMAL
    assert authorized.mode is ExecutionMode.NORMAL
    # Job'un kipi, onu yetkilendiren plan satırının kipiyle aynıdır.
    assert jobs[0].mode is _plan(db_session).mode


# --- 5. Kapsam: public yüzey artık her iki kipi de kabul eder (R1-V3H2A) ------
#
# Bu bölüm bir önceki dilimde (R1-V3H1B1) "public yüzey hâlâ check-only"
# iddiasını ölçüyordu. R1-V3H2A o kilidi bilerek açar: uçtan uca mode seçimi,
# kip mismatch ve kip binding'inin HTTP yüzeyindeki karşılığı artık
# ``tests/test_execution_prepare_api.py`` ve ``tests/test_execution_launch_api.py``
# içinde ölçülür. Burada yalnız iki şey kalır: request şemalarının `mode`'u
# gerçekten zorunlu kıldığı ve `build_runner_arguments`'ın doğrulanmış kipten
# başka bir kaynağa bakmadığı.


def test_public_requests_must_supply_an_explicit_mode(client: TestClient) -> None:
    """İki request şemasında da ``mode`` artık **zorunlu** bir alandır.

    Alan yokluğu değil varlığı ölçülür: R1-V3H1B1'de bu test tam tersini
    doğruluyordu. ``extra="forbid"`` hâlâ geçerlidir — ``mode`` dışında hiçbir
    çalıştırma parametresi kabul edilmez.
    """
    from app.schemas.execution import ExecutionLaunchCreate, ExecutionPlanCreate

    for schema in (ExecutionPlanCreate, ExecutionLaunchCreate):
        field = schema.model_fields["mode"]
        assert field.annotation is ExecutionMode, schema.__name__
        assert field.is_required(), schema.__name__
        assert schema.model_config.get("extra") == "forbid", schema.__name__


def test_every_published_mode_field_allows_exactly_check_and_normal(client: TestClient) -> None:
    """OpenAPI yüzeyinde ``mode`` adlı her alan tam olarak ``check``/``normal`` taşır.

    Kontrol tek tek şemaları değil **bütün** yayımlanmış şemaları tarar: yeni
    bir istek/cevap şeması eklendiğinde de kilit kendiliğinden geçerli olur.
    Üçüncü bir değerin sessizce sızmadığını da aynı taramada doğrular.
    """
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    checked = 0

    for name, schema in schemas.items():
        field = schema.get("properties", {}).get("mode")
        if field is None:
            continue
        checked += 1
        reference = field.get("$ref", "")
        assert reference.rsplit("/", 1)[-1] == "ExecutionMode", f"{name}: {field}"

    assert checked > 0, "en az bir public şema `mode` alanı yayımlamalı"
    assert schemas["ExecutionMode"]["enum"] == ["check", "normal"]


def test_the_runner_argv_is_built_only_from_the_verified_mode() -> None:
    """Runner argv'si **yalnız** doğrulanmış kipe göre kurulur (R1-V3H1B2B).

    Bu turda kip plan ve Job'a yazılıyordu ama onu argv'ye çeviren bir yol
    yoktu. O yol artık vardır ve zorunlu, default'suz bir keyword-only
    ``mode`` parametresidir: ``CHECK`` ``--cmdline=--check``'i tam bir kez
    ekler, ``NORMAL`` argv'yi bu tek argüman dışında birebir aynı bırakır.
    R1-V3H2A'dan beri public yüzey her iki kipi de üretebilir; argv'nin
    kaynağı yine de tek ve değişmez kalır — doğrulanmış ``mode``, başka
    hiçbir şey değil.
    """
    import uuid

    from app.services.execution.runner_process import (
        CHECK_CMDLINE_ARGUMENT,
        build_runner_arguments,
    )

    parameters = inspect.signature(build_runner_arguments).parameters
    assert "mode" in parameters
    assert parameters["mode"].default is inspect.Parameter.empty
    assert parameters["mode"].kind is inspect.Parameter.KEYWORD_ONLY

    common: dict[str, Any] = {
        "command": ["ansible-runner"],
        "run_dir": Path("/tmp/run"),
        "frozen_project_root": Path("/tmp/frozen"),
        "frozen_inventory_path": Path("/tmp/frozen/hosts.ini"),
        "raw_dir": Path("/tmp/raw"),
        "job_id": str(uuid.uuid4()),
        "playbook_path": PLAYBOOK_PATH,
    }

    assert CHECK_CMDLINE_ARGUMENT == "--cmdline=--check"

    check_argv = build_runner_arguments(**common, mode=ExecutionMode.CHECK)
    assert check_argv.count(CHECK_CMDLINE_ARGUMENT) == 1

    normal_argv = build_runner_arguments(**common, mode=ExecutionMode.NORMAL)
    assert CHECK_CMDLINE_ARGUMENT not in normal_argv
    assert normal_argv == check_argv[:-1]
