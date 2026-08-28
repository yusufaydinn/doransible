#!/usr/bin/env python3
"""tests/assert_command_surface_and_order.py

Yapısal kilit: `roles/ufw_hardening/` içindeki HER task dosyasının YAML
AST'ini PyYAML ile parse eder ve dört şeyi doğrular:

1. **Default-deny modül yüzeyi.** Her task'ın TEK bir modül anahtarı
   olmalı ve bu anahtar sabit bir allowlist'te (`ALLOWED_MODULE_KEYS`)
   bulunmalıdır -- allowlist DIŞI bir modül (`ansible.builtin.lineinfile`,
   `ansible.builtin.copy`, `ansible.builtin.service`/`systemd`, vb.)
   SESSİZCE geçemez. `shell`/`raw` -- kısa, FQCN veya `ansible.legacy.*`
   biçimlerinin HİÇBİRİ -- hiçbir task'ta GÖRÜNEMEZ (hem default-deny hem
   ayrı bir `FORBIDDEN_MODULES` kontrolüyle, savunma amaçlı iki kere).

2. **İzin verilen komut yüzeyi.** `ansible.builtin.command`,
   `ansible.legacy.command` VEYA kısa `command` biçimindeki HER task
   (argv biçiminde) kullanılabilir; argv'si aşağıdaki SABİT
   allowlist'teki desenlerden BİRİYLE birebir eşleşmelidir. Bu, kaynak
   metindeki yorum kelimelerine bakan kırılgan bir grep DEĞİLDİR; yeni
   bir task allowlist dışına çıkan bir argv üretirse (veya bir task
   allowlist'e hiç girmemiş bir komut/modül çalıştırırsa) bu script'i
   (dolayısıyla run_offline_tests.sh'i) kırar.

3. **Yazma sırası.** `roles/ufw_hardening/tasks/apply.yml` içinde SSH
   allow kuralı task'ının index'i, UFW enable task'ının index'inden KÜÇÜK
   (yani ÖNCE) olmalıdır -- "SSH allow kuralı her zaman UFW enable'dan
   önce uygulanmalı" güvenlik sözleşmesinin yapısal kanıtı.

4. **Her yazma task'ının kendine ait `would_*` koşulu** (BULGU1/
   AUDIT-FIX1): apply.yml'deki altı UFW yazma komutunun HER BİRİNİN
   `when:` ifadesi hem `not ansible_check_mode` HEM DE kendine ait
   `ufw_hardening_would_*` bayrağını TAŞIMALIDIR -- yalnız
   `not ansible_check_mode` ile koşulsuz (would_* OLMADAN) çalışan bir
   yazma task'ı BULGU1'in gerçek idempotency ihlalini yeniden açar.

`--self-test` bayrağı, checker'ın KENDİ mantığının bellek-içi sahte task
girdileriyle hermetik regresyon kanıtını çalıştırır (tracked hiçbir
dosyaya yazmaz) -- ubuntu-ufw-audit/tests/assert_read_only_surface.py'deki
AYNI desen.
"""
import glob
import os
import sys

import yaml

ROLE_TASKS_DIR = os.path.join(os.path.dirname(__file__), "..", "roles", "ufw_hardening", "tasks")
APPLY_FILE = os.path.join(ROLE_TASKS_DIR, "apply.yml")

FORBIDDEN_MODULES = {
    "ansible.builtin.shell",
    "shell",
    "ansible.builtin.raw",
    "raw",
    "ansible.legacy.shell",
    "ansible.legacy.raw",
}

# Komut ailesi: argv biçiminde kullanılabilecek TÜM kısa/FQCN/legacy
# anahtarlar -- BULGU4: yalnız `ansible.builtin.command`'ı denetlemek
# kısa `command:` veya `ansible.legacy.command:` ile bypass edilebilirdi.
COMMAND_MODULE_KEYS = ("ansible.builtin.command", "ansible.legacy.command", "command")

# Rol içinde GERÇEKTEN kullanılan modüllerin default-deny allowlist'i.
# Buradaki HER anahtar, ya bilinen bir Ansible task-seviyesi anahtar
# KEYWORDS kümesinde OLMAYAN (dolayısıyla "bu bir modül çağrısı" sayılan)
# bir isimdir. Yeni bir modül eklenmek istenirse BİLEREK buraya
# eklenmelidir -- eklenmemiş bir modül anahtarı allowlist dışı sayılır.
ALLOWED_MODULE_KEYS = {
    "ansible.builtin.command",
    "ansible.legacy.command",
    "command",
    "ansible.builtin.assert",
    "ansible.builtin.set_fact",
    "set_fact",
    "ansible.builtin.slurp",
    "ansible.builtin.stat",
    "ansible.builtin.debug",
    "ansible.builtin.fail",
    "ansible.builtin.import_tasks",
    "import_tasks",
    "ansible.builtin.include_tasks",
    "include_tasks",
    "ansible.builtin.meta",
    "ansible.builtin.wait_for_connection",
}

# Bilinen Ansible task-seviyesi anahtar kelimeler -- bunların HİÇBİRİ bir
# "modül çağrısı" sayılmaz. Bu listede OLMAYAN her anahtar (yukarıdaki
# ALLOWED_MODULE_KEYS'te olsun ya da olmasın) bir modül çağrısı adayı
# olarak değerlendirilir -- default-deny mantığının temeli budur.
KNOWN_TASK_KEYWORDS = {
    "name",
    "become",
    "become_user",
    "become_method",
    "become_flags",
    "when",
    "register",
    "vars",
    "changed_when",
    "failed_when",
    "ignore_errors",
    "check_mode",
    "environment",
    "loop",
    "loop_control",
    "notify",
    "tags",
    "delegate_to",
    "no_log",
    "run_once",
    "any_errors_fatal",
    "until",
    "retries",
    "delay",
    "with_items",
    "block",
    "rescue",
    "always",
}

# Sabit, EXACT argv allowlist. Her giriş bir liste; her eleman ya literal
# bir string ya da "{{ ... }}" içeren, yalnız BİLİNEN değişkenlere izin
# veren bir şablon parçasıdır.
ALLOWED_ARGV_PATTERNS = [
    ["/usr/bin/id", "-u"],
    ["systemctl", "is-active", "firewalld"],
    ["/usr/sbin/ufw", "show", "added"],
    ["/usr/sbin/ufw", "status", "verbose"],
    ["/usr/sbin/ufw", "allow", "{{ ufw_hardening_ssh_port_numeric }}/tcp"],
    ["/usr/sbin/ufw", "default", "deny", "incoming"],
    ["/usr/sbin/ufw", "default", "allow", "outgoing"],
    ["/usr/sbin/ufw", "default", "deny", "routed"],
    ["/usr/sbin/ufw", "logging", "{{ ufw_hardening_logging_level }}"],
    ["/usr/sbin/ufw", "--force", "enable"],
]

# apply.yml'deki her yazma task'ının adı içinde ARANAN alt-string VE bu
# task'ın `when:` ifadesinde BULUNMASI gereken kendine ait would_* bayrağı
# (BULGU1/AUDIT-FIX1, madde 4 -- bkz. dosya başındaki docstring).
WRITER_TASK_EXPECTATIONS = [
    ("SSH portu için TCP ALLOW kuralı ekle", "ufw_hardening_would_add_ssh_allow"),
    ("Default incoming policy'yi deny yap", "ufw_hardening_would_set_incoming"),
    ("Default outgoing policy'yi allow yap", "ufw_hardening_would_set_outgoing"),
    ("Default routed/forward policy'yi deny yap", "ufw_hardening_would_set_forward"),
    ("Logging seviyesini ayarla", "ufw_hardening_would_set_logging"),
    ("UFW'yi etkinleştir", "ufw_hardening_would_enable"),
]

# BULGU2 (AUDIT-FIX1) -- fail-closed ön-okuma sözleşmesi: apply.yml'deki
# dört ön-okuma task'ının HİÇBİRİ `failed_when: false` TAŞIMAYABİLİR (rc!=0
# veya dosya okunamazsa play burada durmalı). Tanınan bu dört register
# ismine karşı yapısal olarak ölçülür.
PRE_READ_REGISTER_NAMES = {
    "ufw_hardening_pre_show_added",
    "ufw_hardening_pre_status",
    "ufw_hardening_pre_default_ufw_slurp",
    "ufw_hardening_pre_ufw_conf_slurp",
}


def iter_tasks(doc):
    """Task-list dosyalarını (main.yml, apply.yml, ...) düz bir task
    iterasyonuna indirger. Bu role'de `block:` kullanılmaz; yine de
    ileriye dönük güvenlik için block/rescue/always da taranır."""
    for item in doc:
        if not isinstance(item, dict):
            continue
        yield item
        for key in ("block", "rescue", "always"):
            if key in item and isinstance(item[key], list):
                yield from iter_tasks(item[key])


def check_module_surface_default_deny(task, file_label, errors):
    """Task'ın TEK bir modül anahtarı taşıdığını VE bu anahtarın sabit
    allowlist'te olduğunu doğrular -- allowlist dışı bir mutating modül
    (lineinfile/copy/service gibi) SESSİZCE geçemez."""
    module_keys = [k for k in task.keys() if k not in KNOWN_TASK_KEYWORDS]
    if not module_keys:
        errors.append(f"{file_label}: task '{task.get('name')}' hiçbir modül anahtarı taşımıyor.")
        return
    if len(module_keys) > 1:
        errors.append(
            f"{file_label}: task '{task.get('name')}' birden fazla modül anahtarı taşıyor: {module_keys}"
        )
    for key in module_keys:
        if key not in ALLOWED_MODULE_KEYS:
            errors.append(
                f"{file_label}: task '{task.get('name')}' allowlist DIŞI modül kullanıyor: {key}"
            )


def check_command_argv_allowlist(task, file_label, errors):
    """`command` ailesindeki (kısa/FQCN/legacy) HER anahtarı argv
    allowlist'ine karşı EXACT eşleştirir -- BULGU4: yalnız
    `ansible.builtin.command` kontrol edilseydi kısa `command:` veya
    `ansible.legacy.command:` yüzeyi bypass edebilirdi."""
    for key in COMMAND_MODULE_KEYS:
        if key not in task:
            continue
        cmd = task[key]
        if not isinstance(cmd, dict) or "argv" not in cmd:
            errors.append(
                f"{file_label}: task '{task.get('name')}' {key}'i argv OLMADAN "
                "kullanıyor (serbest komut string'i olabilir)."
            )
            continue
        argv = cmd["argv"]
        if argv not in ALLOWED_ARGV_PATTERNS:
            errors.append(
                f"{file_label}: task '{task.get('name')}' allowlist dışı argv kullanıyor: {argv}"
            )


def check_task_module_surface(task, file_label, errors):
    """Tek bir task için TÜM modül-yüzeyi kontrollerini çalıştırır --
    self-test'in doğrudan çağırdığı, geriye dönük uyumlu giriş noktası."""
    for forbidden in FORBIDDEN_MODULES:
        if forbidden in task:
            errors.append(
                f"{file_label}: task '{task.get('name')}' yasak modül kullanıyor: {forbidden}"
            )
    check_module_surface_default_deny(task, file_label, errors)
    check_command_argv_allowlist(task, file_label, errors)


def _when_clauses_as_text(when_value):
    if when_value is None:
        return ""
    if isinstance(when_value, list):
        return " ".join(str(c) for c in when_value)
    return str(when_value)


def check_writer_when_condition(task, required_would_var, file_label, errors):
    """Yazma task'ının `when:` ifadesinin HEM `not ansible_check_mode`
    HEM DE kendine ait `required_would_var`'ı TAŞIDIĞINI doğrular --
    BULGU1 (AUDIT-FIX1): altı UFW yazma komutunun HİÇBİRİ, kendi
    doğrulanmış would_* kararı OLMADAN (yalnız `not ansible_check_mode`
    ile) normal modda koşulsuz çalışamaz."""
    when_text = _when_clauses_as_text(task.get("when"))
    if "not ansible_check_mode" not in when_text:
        errors.append(
            f"{file_label}: task '{task.get('name')}' when ifadesi "
            f"'not ansible_check_mode' içermiyor: {task.get('when')!r}"
        )
    if required_would_var not in when_text:
        errors.append(
            f"{file_label}: task '{task.get('name')}' when ifadesi kendine ait "
            f"'{required_would_var}' kararını İÇERMİYOR (koşulsuz çalışabilir): {task.get('when')!r}"
        )


def check_pre_read_fail_closed(task, file_label, errors):
    """apply.yml'deki dört ön-okuma task'ının (register adına göre
    tanınır) `failed_when: false` TAŞIMADIĞINI doğrular -- BULGU2
    (AUDIT-FIX1): bu okumalar başarısız olduğunda role fail-closed
    durmalı, sessizce devam ETMEMELİDİR."""
    register = task.get("register")
    if register in PRE_READ_REGISTER_NAMES and task.get("failed_when") is False:
        errors.append(
            f"{file_label}: task '{task.get('name')}' (register={register}) "
            "'failed_when: false' TAŞIYOR -- BULGU2 fail-closed ön-okuma "
            "sözleşmesini ihlal eder."
        )


def collect_errors():
    errors = []
    task_files = sorted(glob.glob(os.path.join(ROLE_TASKS_DIR, "*.yml")))
    if not task_files:
        return ["Hiçbir task dosyası bulunamadı -- bu testin kendisi bozuk."]

    apply_ssh_allow_index = None
    apply_enable_index = None
    found_writer_names = set()

    for path in task_files:
        doc = yaml.safe_load(open(path))
        if doc is None:
            continue
        label = os.path.relpath(path, os.path.dirname(__file__))
        is_apply_file = os.path.abspath(path) == os.path.abspath(APPLY_FILE)
        for idx, task in enumerate(iter_tasks(doc)):
            check_task_module_surface(task, label, errors)
            if is_apply_file:
                check_pre_read_fail_closed(task, label, errors)
                name = task.get("name", "")
                for substr, required_var in WRITER_TASK_EXPECTATIONS:
                    if substr in name:
                        found_writer_names.add(substr)
                        check_writer_when_condition(task, required_var, label, errors)
                if "SSH portu için TCP ALLOW kuralı ekle" in name:
                    apply_ssh_allow_index = idx
                if "UFW'yi etkinleştir" in name:
                    apply_enable_index = idx

    if apply_ssh_allow_index is None:
        errors.append("apply.yml içinde SSH allow task'ı bulunamadı.")
    if apply_enable_index is None:
        errors.append("apply.yml içinde enable task'ı bulunamadı.")
    if apply_ssh_allow_index is not None and apply_enable_index is not None:
        if not (apply_ssh_allow_index < apply_enable_index):
            errors.append(
                f"apply.yml: SSH allow task'ı (index={apply_ssh_allow_index}) enable "
                f"task'ından (index={apply_enable_index}) ÖNCE değil."
            )

    for substr, _ in WRITER_TASK_EXPECTATIONS:
        if substr not in found_writer_names:
            errors.append(f"apply.yml içinde beklenen yazma task'ı bulunamadı: '{substr}'")

    return errors


def self_test():
    """Checker'ın KENDİ mantığının hermetik regresyon kanıtı -- bellek-içi
    sahte task girdileriyle, tracked hiçbir dosyaya dokunmadan."""
    cases = []

    # 1) İzin verilen iki argv geçmeli.
    for good_argv in (
        ["/usr/sbin/ufw", "status", "verbose"],
        ["/usr/sbin/ufw", "--force", "enable"],
    ):
        errs = []
        check_task_module_surface(
            {"name": "ok", "ansible.builtin.command": {"argv": good_argv}}, "self-test", errs
        )
        cases.append((f"allowed argv {good_argv} passes", len(errs) == 0))

    # 2) Keyfi bir unit adı reddedilmeli.
    errs = []
    check_task_module_surface(
        {"name": "bad", "ansible.builtin.command": {"argv": ["systemctl", "is-active", "sshd"]}},
        "self-test",
        errs,
    )
    cases.append(("arbitrary unit name rejected", len(errs) == 1))

    # 3) Eksik/fazla argüman reddedilmeli.
    errs = []
    check_task_module_surface(
        {"name": "bad", "ansible.builtin.command": {"argv": ["/usr/sbin/ufw", "status"]}},
        "self-test",
        errs,
    )
    cases.append(("missing argument rejected", len(errs) == 1))

    errs = []
    check_task_module_surface(
        {
            "name": "bad",
            "ansible.builtin.command": {"argv": ["/usr/sbin/ufw", "status", "verbose", "extra"]},
        },
        "self-test",
        errs,
    )
    cases.append(("extra argument rejected", len(errs) == 1))

    # 4) Farklı bir alt komut (ör. `ufw delete`) reddedilmeli.
    errs = []
    check_task_module_surface(
        {"name": "bad", "ansible.builtin.command": {"argv": ["/usr/sbin/ufw", "delete", "1"]}},
        "self-test",
        errs,
    )
    cases.append(("different subcommand (ufw delete) rejected", len(errs) == 1))

    # 4b) `ufw disable`/`ufw reset`/`ufw reload` de reddedilmeli.
    for bad_subcommand in (["/usr/sbin/ufw", "disable"], ["/usr/sbin/ufw", "reset"], ["/usr/sbin/ufw", "reload"]):
        errs = []
        check_task_module_surface(
            {"name": "bad", "ansible.builtin.command": {"argv": bad_subcommand}}, "self-test", errs
        )
        cases.append((f"ufw {bad_subcommand[1]} rejected", len(errs) == 1))

    # 4c) Farklı bir systemctl unit/operation reddedilmeli.
    errs = []
    check_task_module_surface(
        {"name": "bad", "ansible.builtin.command": {"argv": ["systemctl", "restart", "firewalld"]}},
        "self-test",
        errs,
    )
    cases.append(("different systemctl operation rejected", len(errs) == 1))

    # 5) shell/raw -- kısa, FQCN VE legacy biçimlerin HEPSİ reddedilmeli
    #    (FORBIDDEN_MODULES kontrolü VE default-deny modül-yüzeyi kontrolü
    #    AYNI anda tetiklenebilir -- savunma amaçlı iki kere -- bu yüzden
    #    burada tam olarak 1 DEĞİL, EN AZ 1 hata bekleniyor).
    for key, value in (
        ("ansible.builtin.shell", "ufw allow 22/tcp"),
        ("shell", "ufw allow 22/tcp"),
        ("ansible.legacy.shell", "ufw allow 22/tcp"),
        ("ansible.builtin.raw", "ufw enable"),
        ("raw", "ufw enable"),
        ("ansible.legacy.raw", "ufw enable"),
    ):
        errs = []
        check_task_module_surface({"name": "bad", key: value}, "self-test", errs)
        cases.append((f"{key} rejected", len(errs) >= 1))

    # 6) argv OLMAYAN bir command (serbest string) reddedilmeli.
    errs = []
    check_task_module_surface(
        {"name": "bad", "ansible.builtin.command": "ufw allow 22/tcp"}, "self-test", errs
    )
    cases.append(("command without argv rejected", len(errs) == 1))

    # 6b) BULGU4: kısa `command:` (argv olmadan) da reddedilmeli --
    #     yalnız ansible.builtin.command denetlenseydi bu bypass ederdi.
    errs = []
    check_task_module_surface({"name": "bad", "command": "ufw disable"}, "self-test", errs)
    cases.append(("short 'command' key without argv rejected", len(errs) == 1))

    # 6c) BULGU4: `ansible.legacy.command` allowlist dışı argv ile de
    #     reddedilmeli.
    errs = []
    check_task_module_surface(
        {
            "name": "bad",
            "ansible.legacy.command": {"argv": ["/usr/sbin/ufw", "disable"]},
        },
        "self-test",
        errs,
    )
    cases.append(("ansible.legacy.command with disallowed argv rejected", len(errs) == 1))

    # 7) Bilinmeyen bir Jinja değişkeni içeren argv reddedilmeli
    #    (kullanıcıdan/extra-var'dan arbitrary bir port/level enjekte
    #    edilmesi girişimini taklit eder).
    errs = []
    check_task_module_surface(
        {
            "name": "bad",
            "ansible.builtin.command": {
                "argv": ["/usr/sbin/ufw", "allow", "{{ attacker_supplied_value }}/tcp"]
            },
        },
        "self-test",
        errs,
    )
    cases.append(("unknown templated argv rejected", len(errs) == 1))

    # 8) BULGU4: allowlist dışı mutating modüller (lineinfile/copy/
    #    service/systemd) default-deny ile reddedilmeli.
    for bad_task in (
        {"name": "bad", "ansible.builtin.lineinfile": {"path": "/etc/ufw/ufw.conf", "line": "LOGLEVEL=off"}},
        {"name": "bad", "ansible.builtin.copy": {"dest": "/etc/default/ufw", "content": "x"}},
        {"name": "bad", "ansible.builtin.service": {"name": "ufw", "state": "stopped"}},
        {"name": "bad", "ansible.builtin.systemd": {"name": "ufw", "state": "stopped"}},
    ):
        errs = []
        check_task_module_surface(bad_task, "self-test", errs)
        module_key = [k for k in bad_task if k != "name"][0]
        cases.append((f"disallowed mutating module {module_key} rejected", len(errs) == 1))

    # 9) BULGU4/madde4: kendine ait would_* koşulu OLMAYAN (yalnız
    #    `not ansible_check_mode` taşıyan) bir yazma task'ı reddedilmeli.
    errs = []
    check_writer_when_condition(
        {
            "name": "UFW Hardening | 6/6: UFW'yi etkinleştir (non-interaktif)",
            "ansible.builtin.command": {"argv": ["/usr/sbin/ufw", "--force", "enable"]},
            "when": "not ansible_check_mode",
        },
        "ufw_hardening_would_enable",
        "self-test",
        errs,
    )
    cases.append(("unconditional enable writer (missing would_enable) rejected", len(errs) == 1))

    # 9c) BULGU2: bilinen bir ön-okuma register'ı ile `failed_when: false`
    #     TAŞIYAN bir task reddedilmeli.
    errs = []
    check_pre_read_fail_closed(
        {
            "name": "bad",
            "register": "ufw_hardening_pre_show_added",
            "failed_when": False,
        },
        "self-test",
        errs,
    )
    cases.append(("pre-read task with failed_when:false rejected", len(errs) == 1))

    # 9d) Aynı register, `failed_when: false` OLMADAN geçmeli.
    errs = []
    check_pre_read_fail_closed(
        {"name": "ok", "register": "ufw_hardening_pre_show_added"}, "self-test", errs
    )
    cases.append(("pre-read task without failed_when:false passes", len(errs) == 0))

    # 9b) Doğru would_* koşulunu taşıyan bir yazma task'ı geçmeli.
    errs = []
    check_writer_when_condition(
        {
            "name": "UFW Hardening | 6/6: UFW'yi etkinleştir (non-interaktif)",
            "ansible.builtin.command": {"argv": ["/usr/sbin/ufw", "--force", "enable"]},
            "when": ["not ansible_check_mode", "ufw_hardening_would_enable"],
        },
        "ufw_hardening_would_enable",
        "self-test",
        errs,
    )
    cases.append(("correctly gated enable writer passes", len(errs) == 0))

    failed = [name for name, ok in cases if not ok]
    for name, ok in cases:
        print(f"{'PASS' if ok else 'FAIL'}  self-test: {name}")
    return len(failed) == 0


def main():
    if "--self-test" in sys.argv:
        ok = self_test()
        sys.exit(0 if ok else 1)

    errors = collect_errors()
    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        sys.exit(1)
    print(
        "PASS: modül yüzeyi default-deny allowlist'e uygun, komut argv'leri EXACT "
        "eşleşiyor, SSH allow enable'dan ÖNCE geliyor, her yazma task'ının kendine "
        "ait would_* koşulu var."
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
