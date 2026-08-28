#!/usr/bin/env python3
"""tests/assert_shared_gate_imports.py

Hermetik paylaşım kanıtı (ubuntu-ssh-hardening/tests/run_offline_tests.sh
"5b" bölümündeki AYNI desen): `system_checks.yml` VE karşılık gelen
offline test harness'inin, üç TEK KAYNAKLI gate dosyasının (
`uid_gate_assert.yml`, `firewalld_gate_assert.yml`,
`ssh_port_gate_assert.yml`) HER BİRİ için, GERÇEKTEN AYNI dosyaya
(`os.path.realpath` eşitliği) işaret eden bir `import_tasks` taşıdığını
doğrular -- yani assert mantığı iki yerde AYRI AYRI YAZILMAMIŞTIR. Hiçbir
dosya okumaktan başka bir şekilde dokunulmaz/değiştirilmez.
"""
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
TASKS_DIR = os.path.join(HERE, "..", "roles", "ufw_hardening", "tasks")

# (canonical dosya, [(çağıran dosya, çağıranın kendi task listesi mi yoksa
#  bir play/tasks: sarmalı mı)])
CASES = [
    (
        "uid_gate_assert.yml",
        [
            (os.path.join(TASKS_DIR, "system_checks.yml"), "tasklist"),
            (os.path.join(HERE, "check_system_checks_gate.yml"), "playbook"),
        ],
    ),
    (
        "firewalld_gate_assert.yml",
        [
            (os.path.join(TASKS_DIR, "system_checks.yml"), "tasklist"),
            (os.path.join(HERE, "check_firewalld_gate.yml"), "playbook"),
        ],
    ),
    (
        "ssh_port_gate_assert.yml",
        [
            (os.path.join(TASKS_DIR, "system_checks.yml"), "tasklist"),
            (os.path.join(HERE, "check_ssh_port_gate.yml"), "playbook"),
        ],
    ),
]


def iter_tasks(doc_file, kind):
    doc = yaml.safe_load(open(doc_file))
    if kind == "playbook":
        for play in doc:
            yield from play.get("tasks", [])
    else:
        yield from doc


def find_import_target(task_file, kind, needle):
    for t in iter_tasks(task_file, kind):
        for key in ("ansible.builtin.import_tasks", "ansible.builtin.include_tasks"):
            target = t.get(key)
            if target and os.path.basename(target) == needle:
                return target
    return None


def resolve(task_file, target):
    base_dir = os.path.dirname(os.path.abspath(task_file))
    return os.path.realpath(os.path.join(base_dir, target))


def main():
    errors = []
    for canonical_name, callers in CASES:
        canonical_path = os.path.realpath(os.path.join(TASKS_DIR, canonical_name))
        resolved = []
        for caller_file, kind in callers:
            target = find_import_target(caller_file, kind, canonical_name)
            if target is None:
                errors.append(f"{caller_file} '{canonical_name}'i import etmiyor")
                continue
            r = resolve(caller_file, target)
            resolved.append((caller_file, r))
            if r != canonical_path:
                errors.append(
                    f"{caller_file} import hedefi ({r}) canonical dosyaya "
                    f"({canonical_path}) eşleşmiyor"
                )
        if len(resolved) == 2 and resolved[0][1] != resolved[1][1]:
            errors.append(f"{canonical_name}: iki çağıranın import hedefleri birbirine eşit değil")

    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        sys.exit(1)
    print("PASS: uid/firewalld/ssh_port gate assert dosyaları her iki çağıranda da tek kaynaklı.")
    sys.exit(0)


if __name__ == "__main__":
    main()
