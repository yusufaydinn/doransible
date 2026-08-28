#!/usr/bin/env python3
"""tests/assert_play_contract.py

Yapısal kilit: `ubuntu-ufw-hardening.yml`'deki TEK play'in sözleşmesini
pinler -- `serial: 1`, `any_errors_fatal: true`, `become: false`,
`become_method: sudo`, `become_user: root`, `become_flags: "-H -S -n"`,
`hosts: all`, `gather_facts: true`, `roles: [ufw_hardening]`.

BULGU3 (AUDIT-FIX1): `serial: 1` TEK BAŞINA yalnız BATCH BÜYÜKLÜĞÜDÜR --
bir host'ta unhandled bir hata oluştuğunda Ansible'ın kalan host'lara
devam ETMEMESİNİ tek başına GARANTİ ETMEZ. Bu garanti `serial: 1` +
`any_errors_fatal: true` BİRLEŞİMİNDEN gelir; bu script ikisinin de AYNI
ANDA pinlendiğini doğrular -- biri eksik/yanlışsa test kırmızı olur.

`--self-test` bayrağı, checker'ın KENDİ karşılaştırma mantığının
bellek-içi sahte play sözlükleriyle hermetik regresyon kanıtını çalıştırır
(tracked hiçbir dosyaya yazmaz).
"""
import os
import sys

import yaml

PLAYBOOK_PATH = os.path.join(os.path.dirname(__file__), "..", "ubuntu-ufw-hardening.yml")

EXPECTED_SCALAR_FIELDS = {
    "hosts": "all",
    "gather_facts": True,
    "become": False,
    "become_method": "sudo",
    "become_user": "root",
    "become_flags": "-H -S -n",
    "serial": 1,
    "any_errors_fatal": True,
}

EXPECTED_ROLES = ["ufw_hardening"]


def check_play(play, errors, label):
    for key, expected_value in EXPECTED_SCALAR_FIELDS.items():
        actual = play.get(key, "__missing__")
        if actual != expected_value:
            errors.append(
                f"{label}: '{key}' beklenen {expected_value!r} değil, görülen {actual!r}."
            )
    if play.get("roles") != EXPECTED_ROLES:
        errors.append(
            f"{label}: 'roles' beklenen {EXPECTED_ROLES!r} değil, görülen {play.get('roles')!r}."
        )


def collect_errors():
    errors = []
    doc = yaml.safe_load(open(PLAYBOOK_PATH))
    if not isinstance(doc, list) or len(doc) != 1:
        found = len(doc) if isinstance(doc, list) else "liste değil"
        return [f"{PLAYBOOK_PATH}: tam olarak bir play bekleniyor, {found} bulundu."]
    check_play(doc[0], errors, os.path.basename(PLAYBOOK_PATH))
    return errors


def self_test():
    cases = []

    def compliant_play():
        p = dict(EXPECTED_SCALAR_FIELDS)
        p["roles"] = list(EXPECTED_ROLES)
        return p

    errs = []
    check_play(compliant_play(), errs, "self-test")
    cases.append(("fully compliant play passes", len(errs) == 0))

    p = compliant_play()
    del p["any_errors_fatal"]
    errs = []
    check_play(p, errs, "self-test")
    cases.append(("missing any_errors_fatal rejected", len(errs) == 1))

    p = compliant_play()
    p["any_errors_fatal"] = False
    errs = []
    check_play(p, errs, "self-test")
    cases.append(("any_errors_fatal: false rejected", len(errs) == 1))

    p = compliant_play()
    del p["serial"]
    errs = []
    check_play(p, errs, "self-test")
    cases.append(("missing serial rejected", len(errs) == 1))

    p = compliant_play()
    p["serial"] = 2
    errs = []
    check_play(p, errs, "self-test")
    cases.append(("serial != 1 rejected", len(errs) == 1))

    p = compliant_play()
    p["become"] = True
    errs = []
    check_play(p, errs, "self-test")
    cases.append(("play-level become:true rejected", len(errs) == 1))

    p = compliant_play()
    p["become_flags"] = "-H -S"
    errs = []
    check_play(p, errs, "self-test")
    cases.append(("become_flags missing -n rejected", len(errs) == 1))

    p = compliant_play()
    p["become_method"] = "su"
    errs = []
    check_play(p, errs, "self-test")
    cases.append(("become_method != sudo rejected", len(errs) == 1))

    p = compliant_play()
    p["become_user"] = "ubuntu"
    errs = []
    check_play(p, errs, "self-test")
    cases.append(("become_user != root rejected", len(errs) == 1))

    p = compliant_play()
    p["roles"] = ["ufw_hardening", "extra_role"]
    errs = []
    check_play(p, errs, "self-test")
    cases.append(("unexpected extra role rejected", len(errs) == 1))

    p = compliant_play()
    p["roles"] = []
    errs = []
    check_play(p, errs, "self-test")
    cases.append(("empty roles list rejected", len(errs) == 1))

    failed = [name for name, ok in cases if not ok]
    for name, ok in cases:
        print(f"{'PASS' if ok else 'FAIL'}  self-test: {name}")
    return len(failed) == 0


def main():
    if "--self-test" in sys.argv:
        sys.exit(0 if self_test() else 1)

    errors = collect_errors()
    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        sys.exit(1)
    print(
        "PASS: play sözleşmesi (serial:1 + any_errors_fatal:true + "
        "become/become_method/become_user/become_flags) pinlendi."
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
