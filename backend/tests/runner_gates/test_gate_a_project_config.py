"""Kapı A — project config ve controller kod yüzeyi ölçümü.

PRODUCTION KODU DEĞİLDİR (bkz. paket docstring'i).

Environment izolasyonu (bkz. `test_gate_a_environment.py`) Kapı A'nın yalnız
BİR alt sonucudur. Bu dosya ikinci ve daha geniş soruyu ölçer: **project
içeriği, controller üzerinde yapılandırma ve kod çalıştırabiliyor mu?**

Runner'ın `process_isolation` ayarı varsayılan olarak KAPALIDIR; yani
`ansible-playbook` controller üzerinde, ürünün kendi kullanıcısıyla, project
dizinine erişerek çalışır. Playbook'un işaret ettiği içerik controller'da kod
çalıştırabiliyorsa, "environment temiz" sonucu tek başına execution
izolasyonunu kanıtlamaz.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.runner_gates import probe_support as ps

pytestmark = [
    pytest.mark.runner_gate,
    pytest.mark.skipif(not ps.IS_LINUX, reason=ps.NON_LINUX_SKIP_REASON),
]

DUMP_SCRIPT = """
import json, os, sys

with open(sys.argv[1], "w") as handle:
    json.dump(
        {"forks": sys.argv[2], "ansible_config_env": os.environ.get("ANSIBLE_CONFIG")},
        handle,
    )
"""

CONFIG_PLAYBOOK = """
- name: gate-a project config probe
  hosts: probe
  gather_facts: false
  tasks:
    - name: efektif forks degerini dok
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "{{ probe_script }}"
          - "{{ probe_dump_path }}"
          - "{{ lookup('ansible.builtin.config', 'DEFAULT_FORKS') }}"
      changed_when: false
"""

# Project içeriğinin controller üzerinde KOD çalıştırıp çalıştıramadığını ölçer.
CODE_PLAYBOOK = """
- name: gate-a controller code surface probe
  hosts: probe
  gather_facts: false
  tasks:
    - name: lookup('pipe') ile controller'da komut calistir
      ansible.builtin.set_fact:
        pipe_out: "{{ lookup('ansible.builtin.pipe', probe_pipe_command) }}"
      ignore_errors: true

    - name: project-local lookup plugin'i calistir
      ansible.builtin.set_fact:
        plugin_out: "{{ lookup('probe_exec', probe_plugin_marker) }}"
      ignore_errors: true
"""

# `project/lookup_plugins/` altına konan, project sahibinin yazdığı Python kodu.
LOOKUP_PLUGIN = """
from pathlib import Path

from ansible.plugins.lookup import LookupBase


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        Path(terms[0]).write_text("project-lookup-plugin-calisti", encoding="utf-8")
        return ["ok"]
"""

PROJECT_ANSIBLE_CFG = """[defaults]
forks = 77
"""


PIPE_SCRIPT = """
import pathlib, sys

pathlib.Path(sys.argv[1]).write_text("pipe-lookup-calisti", encoding="utf-8")
print("ok")
"""

PIPE_MARKER_CONTENT = "pipe-lookup-calisti"
PLUGIN_MARKER_CONTENT = "project-lookup-plugin-calisti"


def _launch(
    workspace: Path, config: dict[str, object], env: dict[str, str]
) -> tuple[int, str | None]:
    """Child'ı çalıştırır; (returncode, runner_status) döndürür."""
    config_path = workspace / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    child_script = Path(__file__).parent / "runner_child.py"
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(child_script), str(config_path)],
        env=env,
        close_fds=True,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        cwd=str(workspace),
    )
    status: str | None = None
    result_path = Path(str(config["result_path"]))
    if result_path.exists():
        try:
            status = str(json.loads(result_path.read_text(encoding="utf-8"))["status"])
        except (OSError, ValueError, KeyError):
            status = None
    if completed.returncode != 0:
        raise AssertionError(f"child rc={completed.returncode} stderr={completed.stderr[-2000:]}")
    return completed.returncode, status


def _run_config_case(tmp_path: Path, *, case: str, pin_ansible_config: bool) -> dict[str, object]:
    workspace = tmp_path / case
    workspace.mkdir()
    pdd = ps.make_private_data_dir(workspace)
    (pdd / "project" / "gate_a_cfg.yml").write_text(CONFIG_PLAYBOOK, encoding="utf-8")
    # Project sahibinin koyduğu ansible.cfg — güvenilmeyen içerik.
    (pdd / "project" / "ansible.cfg").write_text(PROJECT_ANSIBLE_CFG, encoding="utf-8")

    dump_path = workspace / "cfg_view.json"
    result_path = workspace / "result.json"
    env = ps.build_isolated_environment(
        workspace=workspace,
        venv_bin=ps.venv_bin_dir(),
        pin_ansible_config=pin_ansible_config,
    )
    _rc, status = _launch(
        workspace,
        {
            "private_data_dir": str(pdd),
            "playbook": "gate_a_cfg.yml",
            "settings": {"job_timeout": 120, "suppress_ansible_output": True},
            "extravars": {
                "probe_python": sys.executable,
                "probe_script": DUMP_SCRIPT,
                "probe_dump_path": str(dump_path),
            },
            "result_path": str(result_path),
        },
        env,
    )

    observed = json.loads(dump_path.read_text(encoding="utf-8"))
    return {
        "case": case,
        "ansible_config_pinned": pin_ansible_config,
        "runner_status": status,
        "effective_forks": observed["forks"],
        "ansible_config_env": observed["ansible_config_env"],
    }


def test_gate_a_project_ansible_cfg_is_neutralised_only_when_config_is_pinned(
    tmp_path: Path,
) -> None:
    """Project `ansible.cfg` okunuyor mu? `ANSIBLE_CONFIG` sabitlenince duruyor mu?

    Negatif ölçüm (pinlenmemiş) sızıntının gerçek olduğunu, pozitif ölçüm
    (pinlenmiş) sınırın işe yaradığını kanıtlar. `forks` zararsız bir gösterge
    olarak seçilmiştir; asıl mesele project'in Ansible yapılandırmasını
    (plugin yolları dâhil) ele geçirebilmesidir.
    """
    unpinned = _run_config_case(tmp_path, case="unpinned", pin_ansible_config=False)
    pinned = _run_config_case(tmp_path, case="pinned", pin_ansible_config=True)

    print(
        "\nGATE-A MEASUREMENT [project-ansible-cfg] "
        + json.dumps({"unpinned": unpinned, "pinned": pinned}, indent=2)
    )

    assert unpinned["effective_forks"] == "77", (
        "ANSIBLE_CONFIG verilmediginde project ansible.cfg'nin okunmasi bekleniyordu; "
        f"olculen: {unpinned}"
    )
    assert pinned["effective_forks"] == "5", (
        f"ANSIBLE_CONFIG sabitlendiginde project ansible.cfg etkisiz olmaliydi; olculen: {pinned}"
    )
    assert pinned["ansible_config_env"] is not None
    assert unpinned["runner_status"] == "successful"
    assert pinned["runner_status"] == "successful"


@pytest.mark.parametrize("cmdline", [None, "--check"])
def test_gate_a_project_content_can_execute_code_on_controller(
    tmp_path: Path, cmdline: str | None
) -> None:
    """Project içeriği controller üzerinde kod çalıştırabiliyor mu?

    İki kanal ölçülür: `lookup('pipe', ...)` ve project'in kendi
    `lookup_plugins/` dizinine koyduğu Python kodu. Bunların herhangi biri
    çalışıyorsa, `ANSIBLE_CONFIG` sabitlenmiş olsa bile Kapı A'nın
    "controller kod izolasyonu" alt sonucu KAPANMAZ.

    `--check` ile de ölçülür: lookup'lar template değerlendirmesi sırasında
    çalıştığı için check mode'un bu yüzeyi sınırlayıp sınırlamadığı VARSAYILMAZ.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pdd = ps.make_private_data_dir(workspace)
    project = pdd / "project"
    (project / "gate_a_code.yml").write_text(CODE_PLAYBOOK, encoding="utf-8")
    (project / "lookup_plugins").mkdir()
    (project / "lookup_plugins" / "probe_exec.py").write_text(LOOKUP_PLUGIN, encoding="utf-8")

    pipe_marker = workspace / "pipe-executed.txt"
    plugin_marker = workspace / "plugin-executed.txt"
    result_path = workspace / "result.json"
    pipe_script = workspace / "pipe_probe.py"
    pipe_script.write_text(PIPE_SCRIPT, encoding="utf-8")

    env = ps.build_isolated_environment(workspace=workspace, venv_bin=ps.venv_bin_dir())
    launch_config: dict[str, object] = {
        "private_data_dir": str(pdd),
        "playbook": "gate_a_code.yml",
        "settings": {"job_timeout": 120, "suppress_ansible_output": True},
        "extravars": {
            "probe_pipe_command": f"{sys.executable} {pipe_script} {pipe_marker}",
            "probe_plugin_marker": str(plugin_marker),
        },
        "result_path": str(result_path),
    }
    if cmdline:
        launch_config["cmdline"] = cmdline
    _rc, status = _launch(workspace, launch_config, env)

    pipe_executed = pipe_marker.exists()
    plugin_executed = plugin_marker.exists()

    print(
        f"\nGATE-A MEASUREMENT [controller-code-surface cmdline={cmdline}] "
        + json.dumps(
            {
                "cmdline": cmdline,
                "runner_status": status,
                "lookup_pipe_executed_on_controller": pipe_executed,
                "project_lookup_plugin_executed_on_controller": plugin_executed,
                "plugin_marker_content": (
                    plugin_marker.read_text(encoding="utf-8") if plugin_executed else None
                ),
                "pipe_marker_content": (
                    pipe_marker.read_text(encoding="utf-8") if pipe_executed else None
                ),
                "runner_process_isolation": "kapalı (varsayılan)",
            },
            indent=2,
        )
    )

    # Bu assertion'lar ölçülen GÜVENSİZ davranışı sabitler. İKİ KANAL AYRI AYRI
    # bağlanır: biri ileride kapanırsa test DÜŞER ve ADR-021 Kapı A sonucunun
    # yeniden değerlendirilmesini zorlar. `pipe or plugin` yeterli değildir,
    # çünkü tek kanal kapandığında sessizce yeşil kalırdı.
    assert status == "successful", f"Runner status beklenen=successful olculen={status}"
    assert pipe_executed is True, (
        "lookup('pipe') controller'da calismadi. ADR-021 Kapi A'nin controller kod "
        "yuzeyi iddiasi yeniden olculmelidir."
    )
    assert plugin_executed is True, (
        "Project'in kendi lookup_plugins Python kodu calismadi. ADR-021 Kapi A'nin "
        "controller kod yuzeyi iddiasi yeniden olculmelidir."
    )
    assert pipe_marker.read_text(encoding="utf-8") == PIPE_MARKER_CONTENT
    assert plugin_marker.read_text(encoding="utf-8") == PLUGIN_MARKER_CONTENT
