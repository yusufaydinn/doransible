"""Execution plan servisi (R1-V1).

API testleri sözleşmeyi ölçer; buradaki testler servis katmanının kendi
davranışını ölçer: veritabanına yazmadığı, plan sabitlerini bozmadığı ve
zaman damgalarını timezone-aware UTC ürettiği.
"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import ExecutionMode, Inventory, InventorySourceType, Project
from app.services.execution import (
    MAX_PREVIEW_HOSTS,
    NOT_EXECUTABLE_REASON,
    ExecutionPlan,
    InventoryNotLinkedToProjectError,
    PlaybookNotDiscoveredError,
    build_execution_plan,
)
from app.services.inventories import ParserLimits
from app.services.projects import ScanLimits
from app.services.projects.service import ProjectInactiveError
from tests.support import stub_parser_command

PLAYBOOK = "---\n- name: Ornek\n  hosts: all\n"
INVENTORY_TEXT = "[web]\nweb01\n"

OUTPUT: dict[str, Any] = {
    "_meta": {"hostvars": {"web02": {}, "db01": {}, "app03": {}}},
    "all": {"children": ["web"]},
    "web": {"hosts": ["web02", "db01", "app03"]},
}


@pytest.fixture
def project(db_session: Session, project_root: Path) -> Project:
    """Playbook ve inventory taşıyan, kayıtlı bir aktif project."""
    directory = project_root / "proje"
    (directory / "inventories").mkdir(parents=True, exist_ok=True)
    (directory / "site.yml").write_text(PLAYBOOK, encoding="utf-8")
    (directory / "inventories" / "hosts.ini").write_text(INVENTORY_TEXT, encoding="utf-8")

    record = Project(name="Web", path=str(directory))
    db_session.add(record)
    db_session.commit()
    return record


@pytest.fixture
def inventory(db_session: Session, project: Project) -> Inventory:
    """`project`'e bağlı inventory kaydı."""
    record = Inventory(
        name="Prod",
        path=str(Path(project.path) / "inventories" / "hosts.ini"),
        source_type=InventorySourceType.INI,
        project_id=project.id,
    )
    db_session.add(record)
    db_session.commit()
    return record


@pytest.fixture
def parser_command(tmp_path: Path) -> list[str]:
    """Sabit bir host kümesi döndüren stub parser komutu."""
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(OUTPUT), encoding="utf-8")
    return stub_parser_command("payload", payload=str(payload))


def _build(
    db_session: Session,
    settings: Settings,
    project: Project,
    *,
    inventory_id: int,
    playbook_path: str,
    command: list[str],
    mode: ExecutionMode = ExecutionMode.CHECK,
) -> ExecutionPlan:
    return build_execution_plan(
        db_session,
        project.id,
        mode=mode,
        inventory_id=inventory_id,
        playbook_path=playbook_path,
        project_roots=settings.resolve_project_root_allowlist(),
        inventory_roots=settings.resolve_inventory_root_allowlist(),
        key_roots=settings.resolve_ssh_key_root_allowlist(),
        command=command,
        parser_limits=ParserLimits.from_settings(settings),
        scan_limits=ScanLimits.from_settings(settings),
        host_key_policy=settings.ssh_host_key_policy,
    )


def test_plan_is_never_executable(
    db_session: Session,
    settings: Settings,
    project: Project,
    inventory: Inventory,
    parser_command: list[str],
) -> None:
    """`executable` sabittir ve gerekçesi makine tarafından okunabilir."""
    plan = _build(
        db_session,
        settings,
        project,
        inventory_id=inventory.id,
        playbook_path="site.yml",
        command=parser_command,
    )

    assert plan.executable is False
    assert plan.not_executable_reason == NOT_EXECUTABLE_REASON
    assert plan.mode == "check"
    assert plan.limit is None
    assert plan.tags is None
    assert plan.skip_tags is None
    assert plan.become is False
    assert plan.connection == "ssh"
    assert plan.inventory.binding == "project"


def test_plan_carries_the_selected_mode(
    db_session: Session,
    settings: Settings,
    project: Project,
    inventory: Inventory,
    parser_command: list[str],
) -> None:
    """Plan (R1-V3H2A) çağıranın verdiği kipi taşır; kendi bir varsayım kurmaz."""
    plan = _build(
        db_session,
        settings,
        project,
        inventory_id=inventory.id,
        playbook_path="site.yml",
        command=parser_command,
        mode=ExecutionMode.NORMAL,
    )

    assert plan.mode is ExecutionMode.NORMAL


def test_generated_at_is_timezone_aware_utc(
    db_session: Session,
    settings: Settings,
    project: Project,
    inventory: Inventory,
    parser_command: list[str],
) -> None:
    plan = _build(
        db_session,
        settings,
        project,
        inventory_id=inventory.id,
        playbook_path="site.yml",
        command=parser_command,
    )

    assert plan.generated_at.tzinfo is not None
    assert plan.generated_at.utcoffset() == UTC.utcoffset(None)
    assert plan.playbook.modified_at.tzinfo is not None


def test_hosts_are_sorted_alphabetically(
    db_session: Session,
    settings: Settings,
    project: Project,
    inventory: Inventory,
    parser_command: list[str],
) -> None:
    """Parser çıktısındaki sıra değil, alfabetik sıra döner."""
    plan = _build(
        db_session,
        settings,
        project,
        inventory_id=inventory.id,
        playbook_path="site.yml",
        command=parser_command,
    )

    assert plan.hosts == ["app03", "db01", "web02"]
    assert plan.host_count == 3
    assert plan.hosts_truncated is False


def test_preview_host_limit_is_the_documented_constant() -> None:
    """Sınır sihirli sayı değil, açık bir sabittir."""
    assert MAX_PREVIEW_HOSTS == 100


def test_service_never_writes_to_the_database(
    db_session: Session,
    settings: Settings,
    project: Project,
    inventory: Inventory,
    parser_command: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan üretimi salt okumadır: commit, flush ve add çağrılmaz."""

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Plan servisi veritabanına yazmamalı")

    monkeypatch.setattr(Session, "commit", _forbidden)
    monkeypatch.setattr(Session, "flush", _forbidden)
    monkeypatch.setattr(Session, "add", _forbidden)
    monkeypatch.setattr(Session, "delete", _forbidden)

    plan = _build(
        db_session,
        settings,
        project,
        inventory_id=inventory.id,
        playbook_path="site.yml",
        command=parser_command,
    )

    assert plan.host_count == 3
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted


def test_standalone_inventory_is_rejected(
    db_session: Session,
    settings: Settings,
    project: Project,
    inventory_root: Path,
    parser_command: list[str],
) -> None:
    standalone_path = inventory_root / "hosts.ini"
    standalone_path.write_text(INVENTORY_TEXT, encoding="utf-8")
    standalone = Inventory(
        name="Bagimsiz",
        path=str(standalone_path),
        source_type=InventorySourceType.INI,
    )
    db_session.add(standalone)
    db_session.commit()

    with pytest.raises(InventoryNotLinkedToProjectError) as error:
        _build(
            db_session,
            settings,
            project,
            inventory_id=standalone.id,
            playbook_path="site.yml",
            command=parser_command,
        )

    assert error.value.details == {"project_id": project.id, "inventory_id": standalone.id}


def test_cross_project_inventory_is_rejected(
    db_session: Session,
    settings: Settings,
    project: Project,
    project_root: Path,
    parser_command: list[str],
) -> None:
    """Başka project'e bağlı inventory de aynı hatayı alır."""
    other_dir = project_root / "diger"
    other_dir.mkdir(parents=True, exist_ok=True)
    other_inventory_path = other_dir / "hosts.ini"
    other_inventory_path.write_text(INVENTORY_TEXT, encoding="utf-8")
    other = Project(name="Diger", path=str(other_dir))
    db_session.add(other)
    db_session.commit()
    foreign = Inventory(
        name="DigerEnvanter",
        path=str(other_inventory_path),
        source_type=InventorySourceType.INI,
        project_id=other.id,
    )
    db_session.add(foreign)
    db_session.commit()

    with pytest.raises(InventoryNotLinkedToProjectError) as error:
        _build(
            db_session,
            settings,
            project,
            inventory_id=foreign.id,
            playbook_path="site.yml",
            command=parser_command,
        )

    # Hata, sahibi olan project hakkında hiçbir şey taşımaz.
    assert error.value.details == {"project_id": project.id, "inventory_id": foreign.id}
    assert other.name not in error.value.message


def test_inactive_project_is_rejected(
    db_session: Session,
    settings: Settings,
    project: Project,
    inventory: Inventory,
    parser_command: list[str],
) -> None:
    project.is_active = False
    db_session.commit()

    with pytest.raises(ProjectInactiveError):
        _build(
            db_session,
            settings,
            project,
            inventory_id=inventory.id,
            playbook_path="site.yml",
            command=parser_command,
        )


def test_undiscovered_playbook_is_rejected_without_touching_the_path(
    db_session: Session,
    settings: Settings,
    project: Project,
    inventory: Inventory,
    parser_command: list[str],
) -> None:
    """Var olan ama keşfedilmemiş bir dosya da reddedilir."""
    hidden = Path(project.path) / "notlar.txt"
    hidden.write_text("playbook degil", encoding="utf-8")

    with pytest.raises(PlaybookNotDiscoveredError) as error:
        _build(
            db_session,
            settings,
            project,
            inventory_id=inventory.id,
            playbook_path="notlar.txt",
            command=parser_command,
        )

    assert error.value.details == {"project_id": project.id}
    assert "notlar.txt" not in error.value.message


def test_playbook_is_validated_before_the_parser_runs(
    db_session: Session,
    settings: Settings,
    project: Project,
    inventory: Inventory,
) -> None:
    """Geçersiz playbook girdisinde hiç alt süreç başlatılmaz.

    Parser komutu bilinçli olarak **çalıştırılamaz** bir yola ayarlanır: çağrı
    yapılsaydı `inventory_parser_unavailable` alınırdı.
    """
    with pytest.raises(PlaybookNotDiscoveredError):
        _build(
            db_session,
            settings,
            project,
            inventory_id=inventory.id,
            playbook_path="../../etc/hosts",
            command=["/olmayan/komut/ansible-inventory"],
        )
