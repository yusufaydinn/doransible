"""Acquire edilmiş Job'dan doğrulanmış execution girdisi (R1-V3C1C2A).

Merkez iddia: **çalıştırılacak girdi yalnız dondurulmuş workspace'ten gelir.**
Çağıran serbest bir project veya inventory yolu veremez, özgün ağaç hiç
açılmaz, en küçük içerik farkı fail-closed reddedilir ve üretilen bağlam
yalnız bellekte yaşayan değişmez bir nesnedir.

Testler gerçek bir dondurulmuş workspace kullanır: `freeze_workspace` ile
yazılan içerik neyse doğrulanan da odur.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import stat
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from app.models import ExecutionMode
from app.services.ansible.inventory_snapshot import InventoryUnsafeError
from app.services.execution import executor as ex
from app.services.execution.executor import (
    PreparedExecutionInputs,
    prepare_execution_inputs,
)
from app.services.execution.job_state import AcquiredPlaybookJob
from app.services.execution.workspace import (
    WorkspaceIntegrityError,
    WorkspaceUnavailableError,
    freeze_workspace,
    secure_filesystem_available,
)

pytestmark = pytest.mark.skipif(
    not secure_filesystem_available(),
    reason="Descriptor-relative dosya sistemi primitive'leri bu platformda yok (ADR-017).",
)

WORKER_ID = "6f1c0b6a-1f2c-4a3d-8b7e-5c4d3e2f1a09"
JOB_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
PLAN_ID = "3c2b1a09-8f7e-4d6c-9b5a-4e3d2c1b0a99"


def _snapshot(hosts: dict[str, dict[str, Any]]) -> str:
    """Uygulamanın ürettiği snapshot biçiminde bir metin."""
    return json.dumps({"all": {"hosts": hosts}}, indent=2, sort_keys=True) + "\n"


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "execution-plans"
    root.mkdir()
    return root


@pytest.fixture
def source_project(tmp_path: Path) -> Path:
    """Küçük ama tipik bir project ağacı."""
    root = tmp_path / "proje"
    (root / "playbooks").mkdir(parents=True)
    (root / "site.yml").write_text("---\n- hosts: all\n", encoding="utf-8")
    (root / "playbooks" / "web.yml").write_text("---\n- hosts: web\n", encoding="utf-8")
    return root


@pytest.fixture
def source_inventory(tmp_path: Path) -> Path:
    """Özgün inventory dosyası; hazırlamadan sonra **hiç** açılmamalıdır."""
    path = tmp_path / "hosts.ini"
    path.write_text("[web]\nweb01\n", encoding="utf-8")
    return path


def _freeze(
    workspace_root: Path,
    source_project: Path,
    snapshot: str,
) -> tuple[str, AcquiredPlaybookJob]:
    """Gerçek bir workspace dondurur ve ona bağlı Job bağlamını üretir."""
    frozen = freeze_workspace(
        workspace_root, project_root=source_project, inventory_snapshot=snapshot
    )
    return frozen.workspace_id, _job(frozen.workspace_id, frozen.manifest_digest)


def _job(workspace_id: str, manifest_digest: str) -> AcquiredPlaybookJob:
    return AcquiredPlaybookJob(
        job_id=JOB_ID,
        execution_plan_id=PLAN_ID,
        workspace_id=workspace_id,
        manifest_digest=manifest_digest,
        project_id=1,
        inventory_id=2,
        playbook_path="site.yml",
        requested_by="operator",
        mode=ExecutionMode.CHECK,
        worker_id=WORKER_ID,
    )


@pytest.fixture
def frozen_job(
    workspace_root: Path, source_project: Path, source_inventory: Path
) -> AcquiredPlaybookJob:
    """Tek host taşıyan, sorunsuz bir dondurulmuş workspace ve Job."""
    _, job = _freeze(
        workspace_root,
        source_project,
        _snapshot({"web01": {"ansible_host": "10.0.0.11", "ansible_user": "deploy"}}),
    )
    return job


# --- Mutlu yol ---------------------------------------------------------------


def test_valid_workspace_produces_an_immutable_context(
    workspace_root: Path, frozen_job: AcquiredPlaybookJob
) -> None:
    """Geçerli workspace, alanları doldurulmuş ve değiştirilemez bir bağlam üretir."""
    prepared = prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])

    assert isinstance(prepared, PreparedExecutionInputs)
    assert prepared.job is frozen_job
    assert prepared.inventory_hosts == ("web01",)
    assert json.loads(prepared.inventory_snapshot)["all"]["hosts"]["web01"] == {
        "ansible_host": "10.0.0.11",
        "ansible_user": "deploy",
    }
    assert set(prepared.connection_values) == {"10.0.0.11", "deploy"}

    with pytest.raises(FrozenInstanceError):
        prepared.frozen_project_root = Path("/tmp")  # type: ignore[misc]


def test_project_and_inventory_are_fixed_paths_of_the_same_workspace(
    workspace_root: Path, frozen_job: AcquiredPlaybookJob
) -> None:
    """İki yol da aynı workspace dizininin sabit çocuklarıdır."""
    prepared = prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])

    workspace_dir = workspace_root / frozen_job.workspace_id
    assert prepared.frozen_project_root == workspace_dir / "project"
    assert prepared.frozen_inventory_path == workspace_dir / "inventory" / "hosts.yml"
    # Runner düzen kontrolünün beklediği biçim: absolute ve alias'sız.
    assert prepared.frozen_project_root.is_absolute()
    assert prepared.frozen_inventory_path.parent.parent == prepared.frozen_project_root.parent
    assert ".." not in prepared.frozen_project_root.parts


def test_hosts_are_deterministic_and_sorted(workspace_root: Path, source_project: Path) -> None:
    """Host listesi ada göre sıralıdır ve her çağrıda aynıdır."""
    _, job = _freeze(
        workspace_root,
        source_project,
        _snapshot({"web02": {}, "db01": {}, "web01": {}}),
    )

    first = prepare_execution_inputs(job, workspace_root=workspace_root, key_roots=[])
    second = prepare_execution_inputs(job, workspace_root=workspace_root, key_roots=[])

    assert first.inventory_hosts == ("db01", "web01", "web02")
    assert first.inventory_hosts == second.inventory_hosts


def test_frozen_values_survive_source_mutation(
    workspace_root: Path,
    source_project: Path,
    source_inventory: Path,
    frozen_job: AcquiredPlaybookJob,
) -> None:
    """Özgün project ve inventory sonradan değişse de dondurulmuş değerler kullanılır."""
    (source_project / "site.yml").write_text("---\n- hosts: baska\n", encoding="utf-8")
    (source_project / "sonradan.yml").write_text("---\n", encoding="utf-8")
    source_inventory.write_text("[web]\nsaldirgan01\n", encoding="utf-8")

    prepared = prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])

    assert prepared.inventory_hosts == ("web01",)
    assert (prepared.frozen_project_root / "site.yml").read_text(
        encoding="utf-8"
    ) == "---\n- hosts: all\n"
    assert not (prepared.frozen_project_root / "sonradan.yml").exists()


def test_original_files_are_never_opened(
    workspace_root: Path,
    source_project: Path,
    source_inventory: Path,
    frozen_job: AcquiredPlaybookJob,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Özgün ağaç ve özgün inventory hiçbir aşamada açılmaz.

    Kanıt iki katmanlıdır: ``os.open`` sarmalanıp açılan **her** yol kaydedilir
    ve özgün ağacın altındaki hiçbir yolun açılmadığı gösterilir; ayrıca ağaç
    tümüyle silindiğinde bile hazırlık aynı sonucu üretir.
    """
    opened: list[str] = []
    real_open = os.open

    def _record(path: Any, *args: Any, **kwargs: Any) -> int:
        if isinstance(path, (str, bytes, os.PathLike)):
            opened.append(os.fsdecode(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _record)
    prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])
    monkeypatch.undo()

    assert str(source_project) not in opened
    assert str(source_inventory) not in opened
    assert not any(entry.startswith(f"{source_project}{os.sep}") for entry in opened)

    for path in sorted(source_project.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    source_project.rmdir()
    source_inventory.unlink()

    prepared = prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])
    assert prepared.inventory_hosts == ("web01",)


# --- Bütünlük ----------------------------------------------------------------


def test_changed_project_content_fails_verification(
    workspace_root: Path, frozen_job: AcquiredPlaybookJob
) -> None:
    """Dondurulmuş project içeriği değişirse hazırlık fail-closed düşer."""
    site = workspace_root / frozen_job.workspace_id / "project" / "site.yml"
    site.write_text("---\n- hosts: saldirgan\n", encoding="utf-8")

    with pytest.raises(WorkspaceIntegrityError) as exc_info:
        prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])

    assert exc_info.value.details == {"reason": "content_digest_mismatch"}


def test_changed_frozen_inventory_fails_verification(
    workspace_root: Path, frozen_job: AcquiredPlaybookJob
) -> None:
    """Dondurulmuş inventory değişirse host listesi hiç okunmadan reddedilir."""
    hosts = workspace_root / frozen_job.workspace_id / "inventory" / "hosts.yml"
    hosts.write_text(_snapshot({"saldirgan01": {}}), encoding="utf-8")

    with pytest.raises(WorkspaceIntegrityError) as exc_info:
        prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])

    assert exc_info.value.details == {"reason": "content_digest_mismatch"}


def test_changed_manifest_fails_verification(
    workspace_root: Path, frozen_job: AcquiredPlaybookJob
) -> None:
    """Yalnız ``manifest.json`` düzenlenirse de doğrulama düşer."""
    path = workspace_root / frozen_job.workspace_id / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["entries"][-1]["sha256"] = "0" * 64
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(WorkspaceIntegrityError) as exc_info:
        prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])

    assert exc_info.value.details == {"reason": "manifest_mismatch"}


def test_wrong_expected_digest_fails(workspace_root: Path, frozen_job: AcquiredPlaybookJob) -> None:
    """İçerik sağlam olsa bile Job'daki digest tutmuyorsa hazırlık düşer."""
    other = _job(frozen_job.workspace_id, "0" * 64)

    with pytest.raises(WorkspaceIntegrityError) as exc_info:
        prepare_execution_inputs(other, workspace_root=workspace_root, key_roots=[])

    assert exc_info.value.details == {"reason": "content_digest_mismatch"}


def test_missing_workspace_fails_closed(
    workspace_root: Path, frozen_job: AcquiredPlaybookJob
) -> None:
    """Kaybolmuş workspace bütünlük ihlali değil, erişilemezliktir."""
    workspace = workspace_root / frozen_job.workspace_id
    for path in sorted(workspace.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    workspace.rmdir()

    with pytest.raises(WorkspaceUnavailableError):
        prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])


def test_symlinked_workspace_is_never_followed(
    workspace_root: Path, source_project: Path, tmp_path: Path
) -> None:
    """Kök altına konmuş bir bağlantı workspace sayılmaz."""
    real_root = tmp_path / "gercek-plan-kok"
    real_root.mkdir()
    workspace_id, job = _freeze(real_root, source_project, _snapshot({"web01": {}}))
    os.symlink(real_root / workspace_id, workspace_root / workspace_id, target_is_directory=True)

    with pytest.raises(WorkspaceUnavailableError):
        prepare_execution_inputs(job, workspace_root=workspace_root, key_roots=[])


def test_unexpected_layout_fails_closed(
    workspace_root: Path, frozen_job: AcquiredPlaybookJob
) -> None:
    """Dondurulmuş ağaca sonradan eklenen girdi düzeni bozar."""
    (workspace_root / frozen_job.workspace_id / "fazladan").mkdir(mode=0o700)

    with pytest.raises(WorkspaceIntegrityError) as exc_info:
        prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])

    assert exc_info.value.details == {"reason": "unexpected_layout"}


@pytest.mark.parametrize("root_name", ["goreli-kok", "."])
def test_relative_workspace_root_is_refused(
    frozen_job: AcquiredPlaybookJob, root_name: str
) -> None:
    """Relative bir kök path işlemine hiç dönüşmez."""
    with pytest.raises(WorkspaceUnavailableError) as exc_info:
        prepare_execution_inputs(frozen_job, workspace_root=Path(root_name), key_roots=[])

    assert exc_info.value.message == "Execution workspace kökü geçersiz."


def test_alias_workspace_root_is_refused(
    workspace_root: Path, frozen_job: AcquiredPlaybookJob
) -> None:
    """``..`` taşıyan bir kök, aynı dizini ikinci bir metinle temsil ederdi."""
    alias = workspace_root.parent / "yok" / ".." / workspace_root.name

    with pytest.raises(WorkspaceUnavailableError):
        prepare_execution_inputs(frozen_job, workspace_root=alias, key_roots=[])


# --- Snapshot doğrulaması ----------------------------------------------------


@pytest.mark.parametrize(
    "snapshot",
    [
        "not-json",
        "{}",
        "",
        json.dumps({"all": {"hosts": {}}}),
        json.dumps({"all": {"hosts": []}}),
        json.dumps({"all": {"hosts": {"web01": []}}}),
        json.dumps({"all": {"hosts": {"web 01": {}}}}),
    ],
)
def test_malformed_or_empty_frozen_snapshot_is_refused(
    workspace_root: Path, source_project: Path, snapshot: str
) -> None:
    """Boş veya bozuk snapshot, manifest tutsa bile fail-closed reddedilir.

    Snapshot **dondurulurken** bozuktur; dolayısıyla digest tutar ve reddin
    kaynağı bütünlük değil, snapshot yapısının yeniden doğrulanmasıdır.
    """
    _, job = _freeze(workspace_root, source_project, snapshot)

    with pytest.raises(InventoryUnsafeError):
        prepare_execution_inputs(job, workspace_root=workspace_root, key_roots=[])


@pytest.mark.parametrize(
    "variable",
    ["ansible_ssh_private_key_file", "ansible_private_key_file"],
)
def test_allowlisted_private_key_is_accepted(
    workspace_root: Path, source_project: Path, secrets_root: Path, variable: str
) -> None:
    """İzin verilen kök altındaki mevcut anahtar kabul edilir ve değeri taşınır."""
    key = secrets_root / "id_ed25519"
    key.write_text("private-material", encoding="utf-8")
    _, job = _freeze(workspace_root, source_project, _snapshot({"web01": {variable: str(key)}}))

    prepared = prepare_execution_inputs(
        job, workspace_root=workspace_root, key_roots=[secrets_root]
    )

    assert str(key) in prepared.connection_values


def test_key_outside_the_effective_allowlist_is_refused(
    workspace_root: Path, source_project: Path, secrets_root: Path, tmp_path: Path
) -> None:
    """Execution anındaki allowlist daralmışsa çalıştırma reddedilir.

    Preview anındaki doğrulama kalıcı bir garanti değildir: burada anahtar
    yerinde durur, değişen tek şey etkin köktür.
    """
    key = secrets_root / "id_ed25519"
    key.write_text("private-material", encoding="utf-8")
    _, job = _freeze(
        workspace_root,
        source_project,
        _snapshot({"web01": {"ansible_ssh_private_key_file": str(key)}}),
    )
    other_root = tmp_path / "baska-secrets-kok"
    other_root.mkdir()

    with pytest.raises(InventoryUnsafeError) as exc_info:
        prepare_execution_inputs(job, workspace_root=workspace_root, key_roots=[other_root])

    rendered = f"{exc_info.value.message} {exc_info.value.details}"
    assert str(key) not in rendered


def test_missing_key_is_refused(
    workspace_root: Path, source_project: Path, secrets_root: Path
) -> None:
    """Preview'dan sonra silinen anahtar execution anında yakalanır."""
    key = secrets_root / "id_ed25519"
    key.write_text("private-material", encoding="utf-8")
    _, job = _freeze(
        workspace_root,
        source_project,
        _snapshot({"web01": {"ansible_ssh_private_key_file": str(key)}}),
    )
    key.unlink()

    with pytest.raises(InventoryUnsafeError):
        prepare_execution_inputs(job, workspace_root=workspace_root, key_roots=[secrets_root])


def test_key_replaced_by_an_outside_symlink_is_refused(
    workspace_root: Path, source_project: Path, secrets_root: Path, tmp_path: Path
) -> None:
    """Execution öncesinde symlink'e çevrilmiş anahtar reddedilir."""
    key = secrets_root / "id_ed25519"
    key.write_text("private-material", encoding="utf-8")
    _, job = _freeze(
        workspace_root,
        source_project,
        _snapshot({"web01": {"ansible_private_key_file": str(key)}}),
    )
    outside = tmp_path / "kok-disi-anahtar"
    outside.write_text("outside-secret", encoding="utf-8")
    key.unlink()
    key.symlink_to(outside)

    with pytest.raises(InventoryUnsafeError) as exc_info:
        prepare_execution_inputs(job, workspace_root=workspace_root, key_roots=[secrets_root])

    rendered = f"{exc_info.value.message} {exc_info.value.details}"
    assert str(outside) not in rendered
    assert "outside-secret" not in rendered


# --- Sözleşme ----------------------------------------------------------------


def test_caller_cannot_supply_project_or_inventory_paths() -> None:
    """İmza serbest bir project/inventory path parametresi **taşımaz**."""
    parameters = inspect.signature(prepare_execution_inputs).parameters

    assert list(parameters) == ["job", "workspace_root", "key_roots"]
    assert parameters["job"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    for name in ("workspace_root", "key_roots"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is inspect.Parameter.empty


def test_connection_values_stay_in_memory_only(
    workspace_root: Path, source_project: Path, secrets_root: Path
) -> None:
    """Bağlantı değerleri yalnız bağlamdadır; gösterimi hiçbir alanı basmaz."""
    key = secrets_root / "id_ed25519"
    key.write_text("private-material", encoding="utf-8")
    _, job = _freeze(
        workspace_root,
        source_project,
        _snapshot(
            {
                "web01": {
                    "ansible_host": "10.0.0.11",
                    "ansible_user": "deploy",
                    "ansible_ssh_private_key_file": str(key),
                }
            }
        ),
    )

    prepared = prepare_execution_inputs(
        job, workspace_root=workspace_root, key_roots=[secrets_root]
    )

    assert str(key) in prepared.connection_values
    # Kazara loglama (`logger.info("%s", inputs)`) tek bir değer bile
    # sızdırmamalıdır: gösterim sabittir.
    for rendered in (repr(prepared), str(prepared), f"{prepared}"):
        assert rendered == "<PreparedExecutionInputs>"
        for secret in (str(key), "10.0.0.11", "deploy", job.workspace_id):
            assert secret not in rendered


def test_prepared_context_is_an_internal_dataclass_only(
    workspace_root: Path, frozen_job: AcquiredPlaybookJob
) -> None:
    """Bağlam serialize edilebilir bir taşıma nesnesi değildir."""
    prepared = prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])

    # Ne bir Pydantic modeli ne de kendi serialization yüzeyi vardır.
    for attribute in ("model_dump", "model_dump_json", "dict", "json", "to_dict", "serialize"):
        assert not hasattr(prepared, attribute)
    with pytest.raises(TypeError):
        json.dumps(prepared)


def test_preparation_produces_no_side_effects(
    workspace_root: Path,
    source_project: Path,
    source_inventory: Path,
    frozen_job: AcquiredPlaybookJob,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hazırlık ne alt süreç ne de dosya sistemi girdisi üretir.

    Kapsanan yokluklar: subprocess, run directory, ``known_hosts`` ve artifact.
    Veritabanı yokluğu ayrıca modül sözleşmesiyle gösterilir
    (:func:`test_module_touches_no_database_or_process_layer`).
    """

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Hazırlık alt süreç başlatmamalıdır.")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)

    def _tree() -> dict[str, tuple[bool, int, str]]:
        """Ad, tür, izin **ve içerik** özeti: yeni girdi de değişen bayt da yakalanır."""
        observed: dict[str, tuple[bool, int, str]] = {}
        for path in sorted(tmp_path.rglob("*")):
            name = str(path.relative_to(tmp_path))
            status = path.lstat()
            digest = ""
            if stat.S_ISREG(status.st_mode):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            observed[name] = (path.is_dir(), stat.S_IMODE(status.st_mode), digest)
        return observed

    before = _tree()
    prepare_execution_inputs(frozen_job, workspace_root=workspace_root, key_roots=[])

    # Ne run directory, ne known_hosts, ne artifact, ne de tek bir değişmiş bayt.
    assert _tree() == before
    assert not any("known_hosts" in name for name in before)
    assert not any(name.startswith("jobs") or "artifact" in name for name in before)


def _imported_modules() -> set[str]:
    """Modülün import ettiği bütün modül adları."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(ex))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_import_surface_is_pinned() -> None:
    """Import yüzeyi bir sözleşmedir ve **tam eşitlikle** ölçülür.

    R1-V3C1C2B2B ile modül artık gerçekten çalıştırıyor, dolayısıyla ORM, süreç
    ve artifact katmanları burada bulunur. Ölçüm yine de anlamlıdır, çünkü
    listenin **büyümemesi** gereken tarafı vardır: HTTP yüzeyi (`fastapi`),
    alt süreç (`subprocess`), zamanlayıcı/döngü (`threading`, `time`,
    `asyncio`, `schedule`) ve serbest dosya sistemi (`shutil`, `glob`) buraya
    girmez. Kontrol metin araması değil gerçek import listesidir: docstring'de
    geçen bir modül adı testi ne geçirir ne düşürür.
    """
    assert _imported_modules() == {
        "__future__",
        "contextlib",
        "collections.abc",
        "dataclasses",
        "enum",
        "pathlib",
        "sqlalchemy.orm",
        "app.core.config",
        "app.models",
        "app.services.ansible.inventory_snapshot",
        # R1-V3C2C: yalnız gözlemci **protokolü** ve bileşik gözlemci. Executor
        # gözlemcinin neyi izlediğini bilmez; `threading` yine listede değildir.
        "app.services.ansible.process",
        "app.services.ansible.ssh",
        "app.services.execution.job_state",
        "app.services.execution.lease",
        "app.services.execution.normalize",
        "app.services.execution.runner_env",
        "app.services.execution.runner_process",
        "app.services.execution.workspace",
        "app.services.jobs.artifacts",
    }


def test_module_touches_the_filesystem_only_through_audited_services() -> None:
    """Modül dosya veya dizin oluşturan bir primitive **çağırmaz**.

    Executor bir orkestratördür: run directory'yi `runner_env`, artifact'ı
    `JobArtifactStore`, known-hosts'u `ssh` servisi açar. Ham bir ``open`` veya
    ``mkdir``, o katmanların descriptor-relative ve 0700 garantilerinin dışında
    ikinci bir yol açardı.
    """
    forbidden = {
        "open",
        "mkdir",
        "makedirs",
        "write_text",
        "write_bytes",
        "touch",
        "mkstemp",
        "mkdtemp",
        "symlink",
        "rename",
    }
    called: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(ex))):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)

    assert called & forbidden == set()
