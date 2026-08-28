#!/usr/bin/env python3
"""tests/assert_read_only_surface.py

Kalicilastirilmis yapisal denetim: production role'un (roles/ufw_audit/) ve
ust playbook'un (ubuntu-ufw-audit.yml) yalnizca beklenen salt-okunur
islemleri tasidigini kanitlar.

Bu, kaynak metindeki kelimelere (README/yorum) takilan kirilgan bir grep
degildir -- PyYAML ile gercek YAML AST'ini parse eder, Ansible'in kendi
kanonik task/block/play keyword listelerini (ansible.playbook.task.Task,
ansible.playbook.block.Block, ansible.playbook.play.Play fattributes)
kullanarak her task dict'indeki "geri kalan" anahtar(lar)i modul cagrisi
olarak tanir ve yalnizca acikca izin verilen modullerle karsilastirir.
`import_tasks`/`include_tasks` hedeflerini role tasks/ dizini icinde
guvenli bicimde takip eder (path traversal veya harici dosya YOK).

`command` modulu icin ayrica: yalnizca argv listesi (serbest string/`cmd`
DEGIL) ve TAM OLARAK asagidaki uc argv EXACT deseninden biri kabul edilir
-- baska hicbir argv[0]/alt komut/unit adi/fazla argument gecmez:
  - ["/usr/sbin/ufw", "status", "verbose"]
  - ["systemctl", "is-active", "firewalld"]
  - ["systemctl", "is-active", "ufw.service"]
Farkli bir unit adi, eksik/fazla argument veya Jinja/dinamik bir deger
(orn. "{{ unit_name }}") bu EXACT string karsilastirmasini gecemez ve
fail-closed reddedilir.

Cikis kodu 0 = yuzey temiz (yalnizca beklenen salt-okunur islemler var).
Cikis kodu != 0 = yasak bir modul/komut bulundu veya dosya
parse/okunamadi; sebep stderr/stdout'a yazilir.

`--self-test` bayragiyla cagrildiginda, bu dosyanin KENDI argv/modul
allowlist mantigini (`_check_command_task`, `_walk_task_list`) bellek-ici
sahte argv/task girdileriyle DOGRUDAN sinar -- tracked production
dosyalarini asla degistirmez, gecici disk fixture'i olusturmaz. Bu,
"iki izin verilen systemctl argv'si gecer / arbitrary unit, eksik/fazla
argument, Jinja/dinamik deger, farkli alt komut, `ufw allow`, `lineinfile`
reddedilir" iddialarinin kalicilastirilmis hermetik regresyon kanitidir
(bkz. tests/run_offline_tests.sh).

Bu dosya gercek urunun bir parcasi degildir; yalnizca test amaclidir.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

try:
    from ansible.playbook.block import Block
    from ansible.playbook.play import Play
    from ansible.playbook.task import Task
except ImportError as exc:  # pragma: no cover - ortam eksikse acikca hata ver
    print(f"FAIL: ansible-core import edilemedi: {exc}", file=sys.stderr)
    sys.exit(2)

CHECKER_PATH = Path(__file__).resolve()
SCRIPT_DIR = CHECKER_PATH.parent
PROJECT_ROOT = SCRIPT_DIR.parent
ROLE_TASKS_DIR = PROJECT_ROOT / "roles" / "ufw_audit" / "tasks"
TOP_PLAYBOOK = PROJECT_ROOT / "ubuntu-ufw-audit.yml"

# Ansible'in kendi kanonik keyword listeleri: bunlar MODUL DEGIL, task/
# block/play seviyesi kontrol anahtarlaridir. 'action' Task'ta ozel olarak
# modulu tasir; onu modul-anahtari tarafinda birakiyoruz (zaten kullanilmiyor
# olsa da 'action:' ile dogrudan modul cagirmak da bir modul cagrisidir).
_TASK_KEYWORDS = set(Task().fattributes.keys()) - {"action"}
_BLOCK_KEYWORDS = set(Block().fattributes.keys())
_PLAY_KEYWORDS = set(Play().fattributes.keys())
NON_MODULE_KEYS = _TASK_KEYWORDS | _BLOCK_KEYWORDS | _PLAY_KEYWORDS | {
    "tasks", "block", "rescue", "always", "roles", "hosts",
}

# Salt-okunur audit'in tasiyabilecegi TEK modul kumesi (allowlist -- bunun
# disindaki HER SEY, tanimadigimiz/gelecekte eklenebilecek bir modul dahil,
# fail-closed reddedilir).
ALLOWED_MODULES = {
    "ansible.builtin.assert", "assert",
    "ansible.builtin.stat", "stat",
    "ansible.builtin.command", "command",
    "ansible.builtin.slurp", "slurp",
    "ansible.builtin.set_fact", "set_fact",
    "ansible.builtin.debug", "debug",
    "ansible.builtin.import_tasks", "import_tasks",
}

# Acikca YASAK modul/eylemler -- ALLOWED_MODULES zaten default-deny
# oldugu icin bu liste sart degildir, ama net bir hata mesaji uretmek
# icin ayrica isimlendiriyoruz.
EXPLICITLY_FORBIDDEN_HINTS = {
    "ansible.builtin.copy": "dosya yazar",
    "copy": "dosya yazar",
    "ansible.builtin.template": "dosya yazar",
    "template": "dosya yazar",
    "ansible.builtin.lineinfile": "dosya duzenler",
    "lineinfile": "dosya duzenler",
    "ansible.builtin.blockinfile": "dosya duzenler",
    "blockinfile": "dosya duzenler",
    "ansible.builtin.file": "dosya/izin degistirir",
    "file": "dosya/izin degistirir",
    "ansible.builtin.replace": "dosya duzenler",
    "replace": "dosya duzenler",
    "ansible.builtin.service": "servis durumunu degistirir",
    "service": "servis durumunu degistirir",
    "ansible.builtin.systemd": "servis durumunu degistirir",
    "systemd": "servis durumunu degistirir",
    "ansible.builtin.systemd_service": "servis durumunu degistirir",
    "ansible.builtin.apt": "paket kurar/kaldirir",
    "apt": "paket kurar/kaldirir",
    "ansible.builtin.package": "paket kurar/kaldirir",
    "package": "paket kurar/kaldirir",
    "ansible.builtin.shell": "serbest komut yuzeyi acar",
    "shell": "serbest komut yuzeyi acar",
    "ansible.builtin.raw": "serbest komut yuzeyi acar",
    "raw": "serbest komut yuzeyi acar",
    "ansible.builtin.script": "serbest komut yuzeyi acar",
    "script": "serbest komut yuzeyi acar",
    "ansible.builtin.reboot": "hostu yeniden baslatir",
    "reboot": "hostu yeniden baslatir",
    "community.general.ufw": "UFW kural/durumunu degistirir",
    "ansible.posix.firewalld": "firewalld kural/durumunu degistirir",
    "ansible.builtin.user": "kullanici hesabini degistirir",
    "ansible.builtin.include_tasks": "dinamik/degiskene bagli dosya dahil eder",
    "include_tasks": "dinamik/degiskene bagli dosya dahil eder",
}

# command modulunun kabul ettigi TEK argv'ler -- EXACT esleme (prefix
# DEGIL): uzunluk, sira ve her eleman birebir uymali. Farkli bir unit adi,
# eksik/fazla argument veya bunlarin disindaki HER SEY reddedilir.
ALLOWED_COMMAND_ARGVS = (
    ("/usr/sbin/ufw", "status", "verbose"),
    ("systemctl", "is-active", "firewalld"),
    ("systemctl", "is-active", "ufw.service"),
)

violations: list[str] = []
visited_files: set[Path] = set()


def _fail(location: str, message: str) -> None:
    violations.append(f"{location}: {message}")


def _check_command_task(location: str, module_args) -> None:
    if not isinstance(module_args, dict):
        _fail(location, f"command modulu serbest-string/beklenmeyen bicimde cagrilmis: {module_args!r}")
        return
    if "cmd" in module_args or "_raw_params" in module_args:
        _fail(location, "command modulu argv yerine serbest string ('cmd'/free-form) kullaniyor -- yasak.")
        return
    argv = module_args.get("argv")
    if not isinstance(argv, list) or not argv:
        _fail(location, f"command modulu 'argv' listesi tasimiyor: {module_args!r}")
        return
    argv_str = [str(a) for a in argv]

    if tuple(argv_str) in ALLOWED_COMMAND_ARGVS:
        return

    _fail(location, f"desteklenmeyen/yasak command argv deseni (exact eslesme yok): {argv_str!r}")


def _module_hint(module_name: str) -> str:
    hint = EXPLICITLY_FORBIDDEN_HINTS.get(module_name)
    return f" ({hint})" if hint else ""


def _walk_task_list(tasks, location: str, source_file: Path) -> None:
    if tasks is None:
        return
    if not isinstance(tasks, list):
        _fail(location, f"task listesi bekleniyordu, bulunan: {type(tasks).__name__}")
        return

    for idx, task in enumerate(tasks):
        task_location = f"{location}[{idx}]"
        if not isinstance(task, dict):
            _fail(task_location, f"task dict bekleniyordu, bulunan: {type(task).__name__}")
            continue

        if "block" in task:
            for key in ("block", "rescue", "always"):
                if key in task:
                    _walk_task_list(task[key], f"{task_location}.{key}", source_file)
            continue

        module_keys = [k for k in task.keys() if k not in NON_MODULE_KEYS]

        if not module_keys:
            _fail(task_location, f"modul anahtari bulunamadi: {sorted(task.keys())!r}")
            continue
        if len(module_keys) > 1:
            _fail(task_location, f"birden fazla modul-benzeri anahtar: {module_keys!r}")
            continue

        module_name = module_keys[0]

        if module_name not in ALLOWED_MODULES:
            _fail(task_location, f"izin verilmeyen modul '{module_name}'{_module_hint(module_name)}")
            continue

        if module_name in ("ansible.builtin.command", "command"):
            _check_command_task(task_location, task[module_name])

        if module_name in ("ansible.builtin.import_tasks", "import_tasks"):
            target = task[module_name]
            if not isinstance(target, str):
                _fail(task_location, f"import_tasks hedefi string degil: {target!r}")
                continue
            if ".." in Path(target).parts or Path(target).is_absolute():
                _fail(task_location, f"import_tasks path traversal/absolute path iceriyor: {target!r}")
                continue
            target_path = (source_file.parent / target).resolve()
            if ROLE_TASKS_DIR.resolve() not in target_path.parents and target_path != ROLE_TASKS_DIR.resolve():
                _fail(task_location, f"import_tasks role tasks/ disinda bir dosyaya isaret ediyor: {target_path}")
                continue
            _scan_task_file(target_path, task_location)


def _scan_task_file(path: Path, referring_location: str) -> None:
    resolved = path.resolve()
    if resolved in visited_files:
        return
    visited_files.add(resolved)

    if not resolved.is_file():
        _fail(referring_location, f"import edilen dosya bulunamadi: {resolved}")
        return

    try:
        with resolved.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        _fail(str(resolved), f"YAML parse hatasi: {exc}")
        return

    _walk_task_list(data, str(resolved.relative_to(PROJECT_ROOT)), resolved)


def _scan_playbook(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            plays = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        _fail(str(path), f"YAML parse hatasi: {exc}")
        return

    if not isinstance(plays, list):
        _fail(str(path), f"play listesi bekleniyordu, bulunan: {type(plays).__name__}")
        return

    location = str(path.relative_to(PROJECT_ROOT))
    for idx, play in enumerate(plays):
        play_location = f"{location}[{idx}]"
        if not isinstance(play, dict):
            _fail(play_location, f"play dict bekleniyordu, bulunan: {type(play).__name__}")
            continue

        unexpected_keys = [
            k for k in play.keys()
            if k not in _PLAY_KEYWORDS and k != "tasks"
        ]
        if unexpected_keys:
            _fail(play_location, f"beklenmeyen play anahtari: {unexpected_keys!r}")

        if "roles" in play:
            roles = play["roles"]
            if roles != ["ufw_audit"]:
                _fail(play_location, f"beklenmeyen roles listesi: {roles!r}")

        if "tasks" in play:
            _walk_task_list(play["tasks"], f"{play_location}.tasks", path)
        if "pre_tasks" in play:
            _walk_task_list(play["pre_tasks"], f"{play_location}.pre_tasks", path)
        if "post_tasks" in play:
            _walk_task_list(play["post_tasks"], f"{play_location}.post_tasks", path)
        if "handlers" in play:
            _fail(play_location, "handlers taniml -- salt-okunur audit'te handler beklenmiyor")


def _reset_state() -> None:
    """violations/visited_files global durumunu her self-test senaryosu
    arasinda sifirlar (fonksiyonlar module-level listeye/kumeye yazar)."""
    global violations, visited_files
    violations = []
    visited_files = set()


def _run_self_test() -> int:
    """Kalicilastirilmis, hermetik regresyon kanitI: `_check_command_task`
    ve `_walk_task_list`'i DOGRUDAN, bellek-ici sahte argv/task
    girdileriyle cagirir. Tracked production dosyalarini (roles/ufw_audit/*)
    HICBIR ZAMAN degistirmez, gecici bir disk fixture'i da OLUSTURMAZ.

    Kapsam: `command` argv EXACT allowlist'inin (yalnizca
    "/usr/sbin/ufw status verbose", "systemctl is-active firewalld",
    "systemctl is-active ufw.service") ve modul allowlist'inin, farkli
    unit/eksik-fazla argument/Jinja-dinamik deger/farkli alt komut/yasak
    modul (`lineinfile` vb.) senaryolarinda dogru pass/fail ayrimi yaptigini
    kanitlar. `--self-test` bayragiyla cagrilir.

    Cikis kodu 0 = tum senaryolar beklendigi gibi calisti. 0-disi = en az
    bir senaryo beklenenden farkli sonuc uretti.
    """
    total = 0
    failed = 0

    def check_argv(name: str, argv, expected_clean: bool, *, module_args_override=None) -> None:
        nonlocal total, failed
        total += 1
        _reset_state()
        module_args = module_args_override if module_args_override is not None else {"argv": argv}
        _check_command_task("self_test", module_args)
        actual_clean = len(violations) == 0
        if actual_clean == expected_clean:
            print(f"PASS  {name}")
        else:
            failed += 1
            exp = "temiz/gecti" if expected_clean else "reddedildi"
            act = "temiz/gecti" if actual_clean else "reddedildi"
            print(f"FAIL  {name} (beklenen={exp} bulunan={act} violations={violations!r})")

    def check_task(name: str, task: dict, expected_clean: bool) -> None:
        nonlocal total, failed
        total += 1
        _reset_state()
        _walk_task_list([task], "self_test", CHECKER_PATH)
        actual_clean = len(violations) == 0
        if actual_clean == expected_clean:
            print(f"PASS  {name}")
        else:
            failed += 1
            exp = "temiz/gecti" if expected_clean else "reddedildi"
            act = "temiz/gecti" if actual_clean else "reddedildi"
            print(f"FAIL  {name} (beklenen={exp} bulunan={act} violations={violations!r})")

    # -- İki izin verilen systemctl argv'si + ufw status verbose geçer -----
    check_argv("systemctl_is_active_firewalld_is_allowed",
               ["systemctl", "is-active", "firewalld"], True)
    check_argv("systemctl_is_active_ufw_service_is_allowed",
               ["systemctl", "is-active", "ufw.service"], True)
    check_argv("ufw_status_verbose_is_allowed",
               ["/usr/sbin/ufw", "status", "verbose"], True)

    # -- Arbitrary unit reddedilir -------------------------------------------
    check_argv("systemctl_is_active_arbitrary_unit_sshd_is_rejected",
               ["systemctl", "is-active", "sshd"], False)
    check_argv("systemctl_is_active_arbitrary_unit_cron_is_rejected",
               ["systemctl", "is-active", "cron"], False)

    # -- Eksik unit reddedilir ------------------------------------------------
    check_argv("systemctl_is_active_missing_unit_is_rejected",
               ["systemctl", "is-active"], False)

    # -- Fazla argüman reddedilir ----------------------------------------------
    check_argv("systemctl_is_active_firewalld_with_extra_argument_is_rejected",
               ["systemctl", "is-active", "firewalld", "--quiet"], False)
    check_argv("systemctl_is_active_ufw_service_with_extra_argument_is_rejected",
               ["systemctl", "is-active", "ufw.service", "extra"], False)
    check_argv("ufw_status_verbose_with_extra_argument_is_rejected",
               ["/usr/sbin/ufw", "status", "verbose", "extra"], False)

    # -- Jinja/dinamik unit reddedilir -----------------------------------------
    check_argv("systemctl_is_active_jinja_templated_unit_is_rejected",
               ["systemctl", "is-active", "{{ unit_name }}"], False)

    # -- Farklı systemctl yolu/alt komut reddedilir ----------------------------
    check_argv("systemctl_absolute_path_binary_is_rejected",
               ["/usr/bin/systemctl", "is-active", "firewalld"], False)
    check_argv("systemctl_restart_subcommand_is_rejected",
               ["systemctl", "restart", "firewalld"], False)
    check_argv("systemctl_start_subcommand_is_rejected",
               ["systemctl", "start", "ufw.service"], False)

    # -- ufw allow reddedilir ----------------------------------------------------
    check_argv("ufw_allow_is_rejected",
               ["/usr/sbin/ufw", "allow", "22/tcp"], False)
    check_argv("ufw_disable_is_rejected",
               ["/usr/sbin/ufw", "disable"], False)

    # -- command modülü serbest string/cmd ile çağrılırsa reddedilir ------------
    check_argv("command_free_form_cmd_key_is_rejected", None, False,
               module_args_override={"cmd": "systemctl is-active firewalld"})
    check_argv("command_raw_free_form_string_is_rejected", None, False,
               module_args_override="systemctl is-active firewalld")

    # -- lineinfile gibi allowlist dışı bir modül reddedilir ---------------------
    check_task(
        "lineinfile_module_is_rejected",
        {
            "name": "evil injected task",
            "ansible.builtin.lineinfile": {
                "path": "/etc/default/ufw",
                "line": "IPV6=yes",
            },
        },
        False,
    )

    # -- Pozitif kontrol: allowlist'teki bir modül (assert) hâlâ geçer ---------
    check_task(
        "assert_module_is_allowed",
        {"name": "benign assert", "ansible.builtin.assert": {"that": ["true"]}},
        True,
    )

    _reset_state()  # gerçek main() taramasını kirletmemek için temiz bırak

    print()
    print(f"{total} test, {total - failed} geçti, {failed} başarısız.")
    return 0 if failed == 0 else 1


def main() -> int:
    if not TOP_PLAYBOOK.is_file():
        print(f"FAIL: playbook bulunamadi: {TOP_PLAYBOOK}", file=sys.stderr)
        return 2
    if not (ROLE_TASKS_DIR / "main.yml").is_file():
        print(f"FAIL: role tasks/main.yml bulunamadi: {ROLE_TASKS_DIR}", file=sys.stderr)
        return 2

    _scan_playbook(TOP_PLAYBOOK)
    _scan_task_file(ROLE_TASKS_DIR / "main.yml", "ubuntu-ufw-audit.yml (roles)")

    # checks.yml yalnizca main.yml'nin import_tasks'i uzerinden degil,
    # ayrica dogrudan da taranmali -- import zinciri kopsa bile bu dosya
    # taramadan disari sizmasin.
    _scan_task_file(ROLE_TASKS_DIR / "checks.yml", "roles/ufw_audit/tasks/checks.yml (dogrudan)")

    if violations:
        print("FAIL: salt-okunur yuzey ihlali bulundu:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    scanned = ", ".join(sorted(str(p.relative_to(PROJECT_ROOT)) for p in visited_files))
    print(f"OK: salt-okunur yuzey dogrulandi. Taranan dosyalar: {scanned}")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_run_self_test())
    sys.exit(main())
