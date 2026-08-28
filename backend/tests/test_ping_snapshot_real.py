"""Üretilen snapshot'ın gerçek Ansible ile sözleşmesi (T-204A).

Stub'ların kapatamadığı boşluk: kendi ürettiğimiz snapshot belgesini Ansible
gerçekten bizim beklediğimiz gibi ayrıştırıyor mu? İki fazlı tasarımın tamamı
buna dayanır — limit, özgün inventory'de değil **Snapshot A üzerinde** çözülür.

Ansible, Windows'u control node olarak desteklemez; testler orada açıkça
atlanır.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.ansible.inventory_snapshot import (
    build_snapshot_plan,
    render_full_snapshot,
    render_target_snapshot,
)
from app.services.inventories.parser import (
    ENABLED_INVENTORY_PLUGINS,
    YAML_ONLY_INVENTORY_PLUGINS,
    ParserLimits,
    load_parser_output,
    run_inventory_parser,
)
from tests.support import real_parser_available

pytestmark = pytest.mark.skipif(
    not real_parser_available(),
    reason=(
        "`ansible-inventory` bu platformda çalıştırılamıyor. "
        "Ansible, Windows'u control node olarak desteklemez."
    ),
)

SOURCE_INVENTORY = """\
[web]
web01 ansible_host=10.0.0.10
web02 ansible_host=10.0.0.11

[db]
db01 ansible_host=10.0.0.20

[production:children]
web
db

[ungrouped]
yalniz ansible_host=10.0.0.99
"""

COMMAND = ["ansible-inventory"]


def _parse(path: Path, *, limit: str | None = None, plugins: str) -> dict[str, dict[str, object]]:
    raw = run_inventory_parser(
        path,
        command=COMMAND,
        limits=ParserLimits(),
        limit=limit,
        inventory_plugins=plugins,
    )
    return load_parser_output(raw).host_variables


def _snapshot_from(source: Path, target: Path) -> None:
    parsed = load_parser_output(
        run_inventory_parser(
            source,
            command=COMMAND,
            limits=ParserLimits(),
            inventory_plugins=ENABLED_INVENTORY_PLUGINS,
        )
    )
    plan = build_snapshot_plan(
        parsed.host_variables, parsed.direct_hosts, parsed.children, key_roots=[]
    )
    target.write_text(render_full_snapshot(plan), encoding="utf-8")


@pytest.fixture
def source_inventory(tmp_path: Path) -> Path:
    path = tmp_path / "hosts.ini"
    path.write_text(SOURCE_INVENTORY, encoding="utf-8")
    return path


def test_generated_snapshot_is_parsed_by_real_ansible(
    source_inventory: Path, tmp_path: Path
) -> None:
    """JSON-as-YAML snapshot gerçek `yaml` eklentisiyle ayrıştırılır.

    Bu sayede PyYAML'a doğrudan bağımlılık gerekmez.
    """
    snapshot = tmp_path / "inventory-all.yml"
    _snapshot_from(source_inventory, snapshot)

    hostvars = _parse(snapshot, plugins=YAML_ONLY_INVENTORY_PLUGINS)

    assert set(hostvars) == {"web01", "web02", "db01", "yalniz"}
    assert hostvars["web01"]["ansible_host"] == "10.0.0.10"


def test_group_topology_survives_the_snapshot(source_inventory: Path, tmp_path: Path) -> None:
    """Grup ve alt grup limitleri snapshot üzerinde çözülebilir olmalıdır."""
    snapshot = tmp_path / "inventory-all.yml"
    _snapshot_from(source_inventory, snapshot)

    assert set(_parse(snapshot, limit="web", plugins=YAML_ONLY_INVENTORY_PLUGINS)) == {
        "web01",
        "web02",
    }
    assert set(_parse(snapshot, limit="production", plugins=YAML_ONLY_INVENTORY_PLUGINS)) == {
        "web01",
        "web02",
        "db01",
    }


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        ("production:!web02", {"web01", "db01"}),
        ("web:&production", {"web01", "web02"}),
        ("web01,db01", {"web01", "db01"}),
        ("web*", {"web01", "web02"}),
    ],
)
def test_union_intersection_and_exclusion_resolve_on_the_snapshot(
    source_inventory: Path, tmp_path: Path, limit: str, expected: set[str]
) -> None:
    snapshot = tmp_path / "inventory-all.yml"
    _snapshot_from(source_inventory, snapshot)

    assert set(_parse(snapshot, limit=limit, plugins=YAML_ONLY_INVENTORY_PLUGINS)) == expected


def test_yaml_only_plugin_prevents_a_ghost_host(source_inventory: Path, tmp_path: Path) -> None:
    """`ini` eklentisi açıkken snapshot'tan hayalet bir host doğuyor.

    Ölçülen davranış: `ini` eklentisi JSON metnini ayrıştırmaya çalışır ve
    başarısız olmadan **önce** paylaşılan inventory nesnesine ``{`` adında bir
    host ekler. Hayalet ``_meta.hostvars`` içinde değil **grup topolojisinde**
    belirir; oradan da snapshot doğrulamasına girip geçerli bir inventory'yi
    yanlışlıkla reddettirebilirdi.

    Bu yüzden uygulamanın kendi ürettiği snapshot'lar yalnızca `yaml`
    eklentisiyle okunur.
    """
    snapshot = tmp_path / "inventory-all.yml"
    _snapshot_from(source_inventory, snapshot)

    def _groups(plugins: str) -> dict[str, list[str]]:
        raw = run_inventory_parser(
            snapshot, command=COMMAND, limits=ParserLimits(), inventory_plugins=plugins
        )
        parsed = load_parser_output(raw)
        return {group: sorted(hosts) for group, hosts in parsed.direct_hosts.items()}

    with_ini = _groups(ENABLED_INVENTORY_PLUGINS)
    yaml_only = _groups(YAML_ONLY_INVENTORY_PLUGINS)

    assert "{" in with_ini["ungrouped"], (
        "hayalet host artık üretilmiyorsa yaml-only koruması gözden geçirilmeli"
    )
    assert "{" not in yaml_only["ungrouped"]
    assert yaml_only["ungrouped"] == ["yalniz"]
    assert set(_parse(snapshot, plugins=YAML_ONLY_INVENTORY_PLUGINS)) == {
        "web01",
        "web02",
        "db01",
        "yalniz",
    }


def test_target_snapshot_contains_exactly_the_resolved_hosts(
    source_inventory: Path, tmp_path: Path
) -> None:
    """Snapshot B yalnızca hedefleri taşır; Phase 2'de `--limit` gerekmez."""
    parsed = load_parser_output(
        run_inventory_parser(source_inventory, command=COMMAND, limits=ParserLimits())
    )
    plan = build_snapshot_plan(
        parsed.host_variables, parsed.direct_hosts, parsed.children, key_roots=[]
    )
    target = tmp_path / "inventory-targets.yml"
    target.write_text(render_target_snapshot(plan, ["web01", "db01"]), encoding="utf-8")

    hostvars = _parse(target, plugins=YAML_ONLY_INVENTORY_PLUGINS)

    assert set(hostvars) == {"web01", "db01"}
    assert hostvars["db01"]["ansible_host"] == "10.0.0.20"
    document = json.loads(target.read_text(encoding="utf-8"))
    assert "children" not in document["all"]


def test_snapshot_never_carries_a_dynamic_inventory_script(tmp_path: Path) -> None:
    """`script` eklentisi kapalıdır; snapshot da bir script'i canlandıramaz."""
    snapshot = tmp_path / "inventory-all.yml"
    snapshot.write_text(
        json.dumps({"all": {"hosts": {"web01": {"ansible_host": "10.0.0.10"}}}}),
        encoding="utf-8",
    )

    hostvars = _parse(snapshot, plugins=YAML_ONLY_INVENTORY_PLUGINS)

    assert set(hostvars) == {"web01"}
