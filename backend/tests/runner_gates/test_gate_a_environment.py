"""Kapı A — Environment ve descriptor izolasyonu ölçümü.

PRODUCTION KODU DEĞİLDİR (bkz. paket docstring'i).

Ölçülen iddia: Runner ayrı bir child process'te, environment'ı allowlist ile
SIFIRDAN kurulmuş olarak çalıştırıldığında, parent'ın (API prosesinin)
environment'ından hiçbir değer ne Runner child'ına ne de yönetilen task'a geçer.

"env sözlüğüne eklemedik" kontrolü bilinçli olarak yeterli sayılmaz: ölçüm,
task'ın `os.environ` üzerinden GERÇEKTEN gördüğü değerler üzerinden yapılır.
"""

from __future__ import annotations

import json
import os
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

data = {"environment": dict(os.environ), "descriptors": {}}
fd_dir = "/proc/self/fd"
for name in sorted(os.listdir(fd_dir)):
    try:
        data["descriptors"][name] = os.readlink(os.path.join(fd_dir, name))
    except OSError:
        pass
with open(sys.argv[1], "w") as handle:
    json.dump(data, handle, indent=2)
"""

PLAYBOOK = """
- name: gate-a environment probe
  hosts: probe
  gather_facts: false
  tasks:
    - name: task ortamini dok
      ansible.builtin.command:
        argv:
          - "{{ probe_python }}"
          - "-c"
          - "{{ probe_script }}"
          - "{{ probe_dump_path }}"
      changed_when: false
"""

# Parent'a (pytest prosesine) enjekte edilen sentetik kirlilik. Değerlerin hepsi
# ortak bir alt dize taşır; böylece child ve task ortamında tek bir aramayla
# sızıntı denetlenebilir.
SENTINEL = "AOPS-SENTINEL-8f2c1d"

POLLUTION: dict[str, str] = {
    "ANSIBLEOPS_MASTER_KEY": f"{SENTINEL}-masterkey",
    "ANSIBLEOPS_DATABASE_URL": f"postgresql+psycopg://u:{SENTINEL}-dbpass@127.0.0.1/db",
    "HTTP_PROXY": f"http://{SENTINEL}-proxy:3128",
    "HTTPS_PROXY": f"http://{SENTINEL}-proxy:3128",
    "ALL_PROXY": f"socks5://{SENTINEL}-proxy:1080",
    "NO_PROXY": f"{SENTINEL}-noproxy.internal",
    "SSH_AUTH_SOCK": f"/run/user/1000/{SENTINEL}-agent.sock",
    "ANSIBLE_VAULT_PASSWORD_FILE": f"/tmp/{SENTINEL}-vault.txt",  # noqa: S108
    "ANSIBLE_CONFIG": f"/tmp/{SENTINEL}-ansible.cfg",  # noqa: S108
    "ANSIBLE_PROBE_RANDOM": f"{SENTINEL}-randomansible",
    "AOPS_PROBE_TOKEN": f"{SENTINEL}-token",
    "AOPS_PROBE_PASSWORD": f"{SENTINEL}-password",
    # Ansible'in gercek kullanici home'una yazan temp/kontrol yuzeyleri.
    # Bunlar allowlist tarafindan BILINCLI olarak tanimlanir; burada parent
    # degerlerinin sizmadigi ayrica olculur.
    "ANSIBLE_LOCAL_TEMP": f"/tmp/{SENTINEL}-local-temp",  # noqa: S108
    "ANSIBLE_REMOTE_TEMP": f"/tmp/{SENTINEL}-remote-temp",  # noqa: S108
    "ANSIBLE_REMOTE_TMP": f"/tmp/{SENTINEL}-remote-tmp",  # noqa: S108
    "ANSIBLE_ASYNC_DIR": f"/tmp/{SENTINEL}-async",  # noqa: S108
    "ANSIBLE_SSH_CONTROL_PATH_DIR": f"/tmp/{SENTINEL}-cp",  # noqa: S108
    # Parent HOME'u da KİRLETİLİR. Amaç, iki kavramın ayrıldığını göstermektir:
    # environment HOME (burada kirli) ile passwd home (pwd kaydından gelen,
    # override edilemeyen gerçek yol). Kaçak taraması passwd home'a göre yapılır.
    "HOME": f"/tmp/{SENTINEL}-home",  # noqa: S108
}


def test_gate_a_parent_environment_does_not_reach_runner_or_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent kirliliği ne child'a ne task'a geçmeli; sentinel fd de sızmamalı."""
    for key, value in POLLUTION.items():
        monkeypatch.setenv(key, value)

    # İKİ AYRI KAVRAM:
    #  - parent_home_env: parent sürecin (kirletilmiş) environment HOME'u.
    #  - passwd_home:     pwd kaydındaki gerçek home. Ansible'ın `~` çözümü
    #                     BUNU kullanır; HOME override edilse bile değişmez.
    parent_home_env = os.environ["HOME"]
    passwd_home = str(ps.passwd_home())

    workspace = tmp_path / "ws"
    workspace.mkdir()
    pdd = ps.make_private_data_dir(workspace)
    (pdd / "project" / "gate_a.yml").write_text(PLAYBOOK, encoding="utf-8")

    dump_path = workspace / "task_view.json"
    child_report = workspace / "child_view.json"
    result_path = workspace / "result.json"
    config_path = workspace / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "private_data_dir": str(pdd),
                "playbook": "gate_a.yml",
                "settings": {"job_timeout": 120, "suppress_ansible_output": True},
                "extravars": {
                    "probe_python": sys.executable,
                    "probe_script": DUMP_SCRIPT,
                    "probe_dump_path": str(dump_path),
                },
                "result_path": str(result_path),
                "self_report_path": str(child_report),
            }
        ),
        encoding="utf-8",
    )

    # Kasıtlı olarak inheritable bırakılmış sentinel descriptor. close_fds
    # gerçekten çalışmıyorsa child'ın fd tablosunda görünmelidir.
    sentinel_file = workspace / "sentinel-descriptor.txt"
    sentinel_file.write_text(SENTINEL, encoding="utf-8")
    sentinel_fd = os.open(sentinel_file, os.O_RDONLY)
    os.set_inheritable(sentinel_fd, True)

    env = ps.build_isolated_environment(workspace=workspace, venv_bin=ps.venv_bin_dir())
    child_script = Path(__file__).parent / "runner_child.py"

    try:
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
    finally:
        os.close(sentinel_fd)

    assert completed.returncode == 0, f"child stderr: {completed.stderr[-2000:]}"
    outcome = json.loads(result_path.read_text(encoding="utf-8"))
    assert outcome["status"] == "successful", outcome

    child_view = json.loads(child_report.read_text(encoding="utf-8"))
    task_view = json.loads(dump_path.read_text(encoding="utf-8"))

    child_env: dict[str, str] = child_view["environment"]
    task_env: dict[str, str] = task_view["environment"]

    # 1. Sentetik değerlerin hiçbiri iki katmanın hiçbirine geçmemeli.
    child_leaks = {k: v for k, v in child_env.items() if SENTINEL in v}
    task_leaks = {k: v for k, v in task_env.items() if SENTINEL in v}
    assert child_leaks == {}, f"Runner child'ina sizan degerler: {child_leaks}"
    assert task_leaks == {}, f"Yonetilen task'a sizan degerler: {task_leaks}"

    # 2. Kirletilen anahtarların kendisi de bulunmamalı. Tek istisna,
    #    allowlist'in BİLİNÇLİ olarak kendi değeriyle tanımladığı anahtardır
    #    (`ANSIBLE_CONFIG`): burada anahtarın yokluğu değil, parent'ın değerinin
    #    kullanılmaması aranır.
    deliberately_defined = {"ANSIBLE_CONFIG", "HOME", *ps.CONTROLLED_TEMP_KEYS}
    assert [k for k in POLLUTION if k in child_env and k not in deliberately_defined] == []
    assert [k for k in POLLUTION if k in task_env and k not in deliberately_defined] == []
    for key in sorted(deliberately_defined & set(POLLUTION)):
        assert child_env[key] != POLLUTION[key], f"{key} parent degerini tasiyor"
        assert task_env[key] != POLLUTION[key], f"{key} parent degerini tasiyor"

    # 3. HOME parent'tan miras alınmamalı; kontrollü alan kullanılmalı. Parent
    #    environment HOME'u ve passwd home AYRI AYRI dışlanır.
    assert child_env["HOME"] == str(workspace / "home")
    assert task_env["HOME"] == str(workspace / "home")
    assert child_env["HOME"] != parent_home_env, "parent HOME kirliligi mirasla gecti"
    assert child_env["HOME"] != passwd_home, "child gercek passwd home'unu kullaniyor"

    # 4. Sentinel descriptor ne child'a ne task'a geçmeli.
    leaked_child_fds = {
        fd: target
        for fd, target in child_view["descriptors"].items()
        if sentinel_file.name in target
    }
    leaked_task_fds = {
        fd: target
        for fd, target in task_view["descriptors"].items()
        if sentinel_file.name in target
    }
    assert leaked_child_fds == {}, f"child fd sizintisi: {leaked_child_fds}"
    assert leaked_task_fds == {}, f"task fd sizintisi: {leaked_task_fds}"

    # 5. Süreç sınırı: child kendi session'ında, stdin /dev/null, umask 0077.
    identity = child_view["identity"]
    assert identity["pid"] == identity["sid"], "child kendi session lideri olmali"
    assert identity["pid"] == identity["pgid"], "child kendi process group lideri olmali"
    assert child_view["stdin_closed"] is True
    assert child_view["umask"] == oct(0o077), f"child umask: {child_view['umask']}"

    # 6. Ansible yapılandırması da parent'tan değil workspace'ten gelmeli.
    for key in ("ANSIBLE_CONFIG", "ANSIBLE_HOME"):
        assert key in child_env, f"{key} allowlist'te olmali"
        assert child_env[key].startswith(str(workspace)), (
            f"{key} workspace disina isaret ediyor: {child_env[key]}"
        )
    assert ps.mode_of(Path(child_env["ANSIBLE_CONFIG"])) == oct(0o600)
    assert task_env["ANSIBLE_CONFIG"] == child_env["ANSIBLE_CONFIG"]

    # 7. Child environment'inin TAM anahtar kumesi. Sayi saymak yerine kume
    #    esitligi: yeni bir anahtar eklenir/cikarilirsa test duser.
    assert set(child_env) == ps.EXPECTED_ENV_KEYS, (
        f"beklenen={sorted(ps.EXPECTED_ENV_KEYS)}\nolculen={sorted(child_env)}"
    )

    # 8. DORT temp/kontrol yuzeyi de workspace altinda, 0700 ve parent
    #    kirliligini tasimiyor. `ANSIBLE_LOCAL_TEMP` tek basina YETERLI
    #    DEGILDIR; `remote_tmp` varsayilani gercek kullanici home'una yazar.
    expected_paths = ps.controlled_temp_paths(workspace)
    for key in ps.CONTROLLED_TEMP_KEYS:
        value = child_env[key]
        assert value == str(expected_paths[key]), f"{key}={value}"
        assert value.startswith(str(workspace)), f"{key} workspace disinda: {value}"
        assert value != POLLUTION[key], f"{key} parent degerini tasiyor"
        assert SENTINEL not in value, f"{key} sentinel tasiyor: {value}"
        assert ps.mode_of(Path(value)) == oct(0o700), f"{key} modu {ps.mode_of(Path(value))}"

    # 9. `remote_tmp` alias'lari TEK secenegin iki adidir; ayni degere bagli.
    assert child_env["ANSIBLE_REMOTE_TEMP"] == child_env["ANSIBLE_REMOTE_TMP"]

    # 10. PASSWD home varsayilanlari HICBIR degerde gecmemeli. Tarama environment
    #     HOME'a degil passwd kaydina gore yapilir: Ansible'in `~` cozumu de
    #     oyledir, ve HOME override edilmis olsa bile kacak yakalanabilmelidir.
    passwd_home_defaults = tuple(
        f"{passwd_home}/{suffix}" for suffix in ps.PASSWD_HOME_DEFAULT_SUFFIXES
    )
    passwd_home_hits = sorted(
        {
            f"{scope}:{key}={value}"
            for scope, observed in (("child", child_env), ("task", task_env))
            for key, value in observed.items()
            for default in passwd_home_defaults
            if default in value
        }
    )
    assert passwd_home_hits == [], f"passwd home varsayilani ortamda gorundu: {passwd_home_hits}"

    print(
        "\nGATE-A MEASUREMENT "
        + json.dumps(
            {
                "child_env_keys": sorted(child_env),
                "task_env_key_count": len(task_env),
                "task_ansible_injected": sorted(
                    k for k in task_env if k.startswith("ANSIBLE_") or k.startswith("AWX_")
                ),
                "child_identity": identity,
                "child_stdin_devnull": child_view["stdin_closed"],
                "child_fd_targets": child_view["descriptors"],
                "sentinel_leaks_child": child_leaks,
                "sentinel_leaks_task": task_leaks,
                "parent_home_env": parent_home_env,
                "passwd_home": passwd_home,
                "controlled_home": child_env["HOME"],
                "task_home": task_env.get("HOME"),
                "passwd_home_default_hits": passwd_home_hits,
                "controlled_temp_dirs": {key: child_env[key] for key in ps.CONTROLLED_TEMP_KEYS},
                "controlled_temp_modes": {
                    key: ps.mode_of(Path(child_env[key])) for key in ps.CONTROLLED_TEMP_KEYS
                },
                "task_host_key_checking": task_env.get("ANSIBLE_HOST_KEY_CHECKING"),
            },
            indent=2,
        )
    )


def test_gate_a_runner_inherits_environment_when_launched_in_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Karşıt ölçüm: Runner in-process çağrıldığında environment SIZAR.

    Bu test, Kapı A'nın neden ayrı child process şart koştuğunu kanıtlar.
    `env/envvars` mevcut environment'ın yerine geçmez, üzerine ekler; bu yüzden
    API prosesi içinde thread'de Runner çağırmak kabul edilebilir bir tasarım
    değildir (ADR-021 Kapı A, reddedilen alternatif).

    Burada gerçek bir playbook çalıştırılmaz; yalnız Runner'ın çocuğa vereceği
    environment'ın nasıl kurulduğu ölçülür.
    """
    monkeypatch.setenv("AOPS_INPROCESS_SENTINEL", f"{SENTINEL}-inprocess")

    from ansible_runner.config.runner import RunnerConfig

    workspace = tmp_path / "ws"
    workspace.mkdir()
    pdd = ps.make_private_data_dir(workspace)
    (pdd / "project" / "gate_a.yml").write_text(PLAYBOOK, encoding="utf-8")

    config = RunnerConfig(private_data_dir=str(pdd), playbook="gate_a.yml")
    config.prepare()
    prepared: dict[str, str] = dict(config.env)

    leaked = {k: v for k, v in prepared.items() if SENTINEL in v}
    assert leaked != {}, (
        "Runner in-process calistirildiginda parent environment'i miras almadi; "
        "bu olcum ADR-021 Kapi A'nin dayanagini gecersiz kilar ve yeniden "
        "degerlendirilmelidir."
    )

    print(
        "\nGATE-A COUNTER-MEASUREMENT "
        + json.dumps(
            {
                "in_process_leaked_keys": sorted(leaked),
                "host_key_checking_default": prepared.get("ANSIBLE_HOST_KEY_CHECKING"),
                "prepared_env_key_count": len(prepared),
            },
            indent=2,
        )
    )


REAL_MODULE_PLAYBOOK = """
- name: gate-a real module temp-path probe
  hosts: probe
  gather_facts: false
  tasks:
    - name: gercek command module task
      ansible.builtin.command:
        argv:
          - "/bin/true"
          - "gate-a-remote-tmp-probe"
      changed_when: false
"""


def test_gate_a_real_module_uses_controlled_remote_tmp(tmp_path: Path) -> None:
    """`remote_tmp` GERÇEKTEN kontrollü workspace yolunu mu kullanıyor?

    Environment sözlüğünde anahtarın bulunması yeterli sayılmaz: `-vvv` ile
    gerçek bir `command` modülü çalıştırılır ve Runner artifact'ındaki
    `mkdir -p ...` komutu ölçülür.

    ÖLÇÜLEN KÖK NEDEN: `remote_tmp` varsayılanı `~/.ansible/tmp`'dir ve `~`
    **$HOME'dan değil**, hedef kullanıcının passwd kaydından çözülür.
    `ansible_connection=local` altında "hedef" controller'ın kendisidir; bu
    yüzden `HOME` workspace'e kurulmuş olsa bile Ansible gerçek kullanıcı
    home'una yazar. `ANSIBLE_LOCAL_TEMP` bunu KAPATMAZ — ayrı bir seçenektir.

    Bu test yalnız "unrestricted filesystem'de yeşil" olmaya güvenmez: workspace
    dışına yazma sessizce gerçekleşirse assertion düşer.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pdd = ps.make_private_data_dir(workspace)
    ps.write_project_file(pdd, "gate_a_real.yml", REAL_MODULE_PLAYBOOK)

    result_path = workspace / "result.json"
    config_path = workspace / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "private_data_dir": str(pdd),
                "playbook": "gate_a_real.yml",
                "settings": {"job_timeout": 120, "suppress_ansible_output": True},
                # Oluşturulan remote temp komutunu görebilmek için verbosity.
                "cmdline": "-vvv",
                "result_path": str(result_path),
            }
        ),
        encoding="utf-8",
    )

    env = ps.build_isolated_environment(workspace=workspace, venv_bin=ps.venv_bin_dir())
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(Path(__file__).parent / "runner_child.py"), str(config_path)],
        env=env,
        close_fds=True,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        cwd=str(workspace),
    )
    assert completed.returncode == 0, f"child stderr: {completed.stderr[-2000:]}"

    status = json.loads(result_path.read_text(encoding="utf-8"))["status"]
    expected_remote_tmp = str(ps.controlled_temp_paths(workspace)["ANSIBLE_REMOTE_TMP"])

    # Ham artifact'ın tamamı taranır: stdout ve job_events birlikte.
    blobs: list[str] = []
    for path in ps.iter_all_files(pdd / "artifacts"):
        try:
            blobs.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    haystack = "\n".join(blobs)

    # Tarama PASSWD home'a gore yapilir: Ansible'in `~` cozumu environment
    # HOME'u degil passwd kaydini kullanir (olculen kok neden). Parent HOME
    # baska bir degere ayarlanmis olsa bile gercek kacak yakalanir.
    parent_home_env = os.environ.get("HOME", "")
    passwd_home = str(ps.passwd_home())
    passwd_home_hits = [
        suffix
        for suffix in ps.PASSWD_HOME_DEFAULT_SUFFIXES
        if f"{passwd_home}/{suffix}" in haystack
    ]
    mkdir_lines = sorted(
        {
            line.strip()
            for line in haystack.splitlines()
            if "mkdir" in line and "ansible-tmp-" in line
        }
    )

    print(
        "\nGATE-A MEASUREMENT [real-module-remote-tmp] "
        + json.dumps(
            {
                "runner_status": status,
                "expected_remote_tmp": expected_remote_tmp,
                "controlled_remote_tmp_used": expected_remote_tmp in haystack,
                "parent_home_env": parent_home_env,
                "passwd_home": passwd_home,
                "controlled_home": str(workspace / "home"),
                "passwd_home_default_hits": passwd_home_hits,
                "mkdir_command_sample": mkdir_lines[:1],
            },
            indent=2,
        )
    )

    assert status == "successful", f"runner status: {status}"
    assert expected_remote_tmp in haystack, (
        "Kontrollu remote_tmp yolu artifact'ta gorunmedi; Ansible baska bir yol "
        f"kullanmis olabilir. Beklenen: {expected_remote_tmp}"
    )
    assert mkdir_lines != [], "ansible-tmp mkdir komutu olculemedi; -vvv ciktisi beklenmiyordu"
    assert all(expected_remote_tmp in line for line in mkdir_lines), (
        f"ansible-tmp mkdir komutlari kontrollu yolun disinda: {mkdir_lines}"
    )
    assert passwd_home_hits == [], (
        f"Passwd home varsayilani kullanilmis: {passwd_home_hits} (passwd_home={passwd_home})"
    )
