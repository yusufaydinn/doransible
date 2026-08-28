#!/usr/bin/env bash
# tests/run_offline_tests.sh
#
# Kalıcı offline regresyon testleri: gerçek bir SSH hostu, bağlantı veya
# sudo gerektirmeden bu project'in YAML'ini, playbook syntax'ını, şablon
# render'ını ve remediation mantığının saf-logic kısımlarını (profil
# kilidi -- yönetilen path, desteklenen sürüm listesi, 10 baseline
# değeri, compliance alan listesi, reconnect timeout/sleep VE bunların
# gölgelenemez olduğu; OS gate; passwordless sudo/root önkoşulunun
# fail-closed gate mantığı (R1-V3H4-SIMPLIFY); apply karar akışı
# -- syntax/effective-read/effective-baseline üç ayrı başarısızlık
# sınıfı; pre/post-reload compliance) doğrular.
#
# Bu round KASITLI olarak gerçek bir hedefte ÇALIŞTIRMA yapmaz:
# system_checks.yml (sudo/root, sshd binary, "sshd -t" ile ön-koşul) ve
# apply.yml/post_verify.yml'nin gerçek write/verify/rollback/reload/
# reconnect zincirinin MODÜL ÇAĞRILARI (template/command/copy/file/
# systemd_service/wait_for_connection) gerçek bir Ubuntu hedef VE become
# gerektirir; bu script bunu KAPSAMAZ. Bunun yerine apply.yml'in
# FAILURE-ATOMIC KARAR MANTIĞI (apply_decisions_*.yml), gerçek modül
# çağrılarının ÜRETMİŞ OLACAĞI sonuçları simüle eden sahte register
# değişkenleriyle doğrudan test edilir (bkz. "apply karar akışı" bölümü)
# -- bkz. README.md "Sınırlar" ve kapanış raporu.
#
# Kullanım: ./tests/run_offline_tests.sh  (repo kökünden veya bu dizinden)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
FIXTURES_DIR="${SCRIPT_DIR}/fixtures"

TOTAL=0
FAILED=0

UNDEFINED_ERROR_PATTERNS=(
  "undefined variable"
  "has no element"
  "AnsibleUndefinedVariable"
  "list object has no"
  "string object has no"
)

assert_no_template_errors() {
  local output="$1" name="$2" pattern
  for pattern in "${UNDEFINED_ERROR_PATTERNS[@]}"; do
    if grep -qiF -- "${pattern}" <<<"${output}"; then
      echo "  -> ${name}: çıktıda şablon hatası imzası bulundu: '${pattern}'" >&2
      return 1
    fi
  done
  return 0
}

# run_case NAME EXPECTED[pass|fail] GREP_EXPECT -- ANSIBLE_PLAYBOOK_ARGS...
run_case() {
  local name="$1" expected="$2" grep_expect="$3"
  shift 3
  local output rc
  TOTAL=$((TOTAL + 1))

  output=$(ansible-playbook -i localhost, "$@" 2>&1)
  rc=$?

  local rc_ok=0
  if [[ "${expected}" == "pass" && "${rc}" -eq 0 ]]; then
    rc_ok=1
  elif [[ "${expected}" == "fail" && "${rc}" -ne 0 ]]; then
    rc_ok=1
  fi

  local grep_ok=0
  if [[ -z "${grep_expect}" ]] || grep -qF -- "${grep_expect}" <<<"${output}"; then
    grep_ok=1
  fi

  local no_undefined_ok=1
  assert_no_template_errors "${output}" "${name}" || no_undefined_ok=0

  if [[ "${rc_ok}" -eq 1 && "${grep_ok}" -eq 1 && "${no_undefined_ok}" -eq 1 ]]; then
    echo "PASS  ${name}"
  else
    FAILED=$((FAILED + 1))
    echo "FAIL  ${name} (expected=${expected} rc=${rc} rc_ok=${rc_ok} grep_ok=${grep_ok} no_undefined_ok=${no_undefined_ok})"
    echo "----- output -----"
    echo "${output}"
    echo "-------------------"
  fi
}

# run_case_multi NAME -- needle1 -- needle2 ... çağrılmaz; birden fazla
# needle gereken senaryolar (idempotent-değil, tek seferlik) doğrudan
# aşağıda elle yazılır (compliance_multiple_non_compliant gibi).

cd "${PROJECT_DIR}"

# --- 1. YAML parse: proje içindeki her .yml/.yaml dosyası saf YAML olarak
#        parse edilebiliyor mu VE bulunan dosya sayısı sıfır değil mi ---
# BULGU5a: eski `find ... -name '*.yml' -o -name '*.yaml' -print0` grup-
# lanmamıştı; `-print0` yalnız `-o`'nun SAĞ tarafına (yalnız *.yaml'a)
# bağlanıyordu ve *.yml dosyaları (projedeki TEK uzantı) hiç yazdırılmıyordu
# -- test sessizce boş bir döngü üzerinde "geçiyordu". Aşağıda `\( -o \)`
# ile açıkça gruplanmıştır.
TOTAL=$((TOTAL + 1))
yaml_files_found=0
yaml_parse_output=""
yaml_parse_ok=1
while IFS= read -r -d '' f; do
  yaml_files_found=$((yaml_files_found + 1))
  err=$(python3 -c "import sys, yaml; yaml.safe_load(open(sys.argv[1]))" "${f}" 2>&1) || {
    yaml_parse_ok=0
    yaml_parse_output+="${f}: ${err}"$'\n'
  }
done < <(find "${PROJECT_DIR}" \( -name '*.yml' -o -name '*.yaml' \) -print0)
if [[ "${yaml_files_found}" -eq 0 ]]; then
  yaml_parse_ok=0
  yaml_parse_output+="find hiçbir .yml/.yaml dosyası bulmadı -- bu testin kendisi bozuk."$'\n'
fi
if [[ "${yaml_parse_ok}" -eq 1 ]]; then
  echo "PASS  yaml_parse_all_files (${yaml_files_found} dosya parse edildi)"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  yaml_parse_all_files (bulunan=${yaml_files_found})"
  echo "----- output -----"
  echo "${yaml_parse_output}"
  echo "-------------------"
fi

# --- 2. ansible-playbook --syntax-check ---
run_case "playbook_syntax_check" pass "" \
  --syntax-check -i "${PROJECT_DIR}/inventory/hosts.yml" "${PROJECT_DIR}/ubuntu-ssh-hardening.yml"

# --- 3. Profil kilidi (profile_lock_check.yml, BULGU4) ---
run_case "profile_lock_default_passes" pass \
  "Bounded-numeric policy listesi tam, doğru sırada ve beklenen sınırlarla (2/2)." \
  "${SCRIPT_DIR}/check_profile_lock.yml"

run_case "profile_lock_path_override_fails_closed" fail \
  "FAIL-CLOSED: ssh_hardening_managed_drop_in_path" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ssh_hardening_managed_drop_in_path=/etc/ssh/sshd_config.d/99-evil.conf

run_case "profile_lock_baseline_value_override_fails_closed" fail \
  "FAIL-CLOSED: bir veya daha fazla baseline değişkeni sabit audit değerinden sapmış" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ssh_hardening_permit_root_login=yes

# LIVE-AUDIT-FIX2: baseline alan listesi artık EXACT (8 boolean/enum
# alan, ssh_hardening_baseline_fields_exact) ve NUMERIC (2 bounded alan,
# ssh_hardening_baseline_fields_numeric) olarak ikiye ayrıldı -- bkz.
# aşağıdaki yeni "numeric" testleri ve README.md "Profil kilidi".
run_case "profile_lock_fields_list_truncated_fails_closed" fail \
  "FAIL-CLOSED: ssh_hardening_baseline_fields_exact listesi" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ssh_hardening_baseline_fields_exact":[{"key":"permitrootlogin","expected":"no"}]}'

run_case "profile_lock_fields_list_expected_value_tampered_fails_closed" fail \
  "FAIL-CLOSED: ssh_hardening_baseline_fields_exact listesi" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ssh_hardening_baseline_fields_exact": [{"key":"permitrootlogin","expected":"no"},{"key":"passwordauthentication","expected":"no"},{"key":"pubkeyauthentication","expected":"yes"},{"key":"permitemptypasswords","expected":"yes"},{"key":"kbdinteractiveauthentication","expected":"no"},{"key":"x11forwarding","expected":"no"},{"key":"allowagentforwarding","expected":"no"},{"key":"allowtcpforwarding","expected":"no"}]}'

run_case "profile_lock_numeric_fields_list_truncated_fails_closed" fail \
  "FAIL-CLOSED: ssh_hardening_baseline_fields_numeric listesi" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ssh_hardening_baseline_fields_numeric":[{"key":"maxauthtries","max":6}]}'

run_case "profile_lock_numeric_fields_list_max_tampered_fails_closed" fail \
  "FAIL-CLOSED: ssh_hardening_baseline_fields_numeric listesi" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ssh_hardening_baseline_fields_numeric":[{"key":"maxauthtries","max":99},{"key":"logingracetime","max_seconds":60}]}'

run_case "profile_lock_numeric_fields_list_max_seconds_tampered_fails_closed" fail \
  "FAIL-CLOSED: ssh_hardening_baseline_fields_numeric listesi" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ssh_hardening_baseline_fields_numeric":[{"key":"maxauthtries","max":6},{"key":"logingracetime","max_seconds":9999}]}'

run_case "profile_lock_max_auth_tries_max_override_fails_closed" fail \
  "FAIL-CLOSED: bounded-numeric compliance üst sınırları" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ssh_hardening_max_auth_tries_max=99

run_case "profile_lock_login_grace_time_max_seconds_override_fails_closed" fail \
  "FAIL-CLOSED: bounded-numeric compliance üst sınırları" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ssh_hardening_login_grace_time_max_seconds=9999

# FIX1.1 (BULGU3): desteklenen sürüm listesi ve reconnect timeout/sleep de
# artık kilitli -- "tercihen mevcut 30/2 değerlerini sabitle" gereksinimi.
run_case "profile_lock_supported_versions_truncated_fails_closed" fail \
  "FAIL-CLOSED: ssh_hardening_supported_ubuntu_versions" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ssh_hardening_supported_ubuntu_versions": ["22.04"]}'

run_case "profile_lock_supported_versions_extra_entry_fails_closed" fail \
  "FAIL-CLOSED: ssh_hardening_supported_ubuntu_versions" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ssh_hardening_supported_ubuntu_versions": ["22.04", "24.04", "20.04"]}'

run_case "profile_lock_reconnect_timeout_override_fails_closed" fail \
  "FAIL-CLOSED: reconnect zaman aşımı ayarları" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ssh_hardening_reconnect_timeout_seconds=9999

run_case "profile_lock_reconnect_sleep_override_fails_closed" fail \
  "FAIL-CLOSED: reconnect zaman aşımı ayarları" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ssh_hardening_reconnect_sleep_seconds=0

# FIX1.1 (BULGU4 gölgeleme koruması): hem GERÇEK değişkeni HEM DE eski
# "kilit" adını taklit eden bir extra-var'ı BİRLİKTE ver -- kilit
# referansları artık literal (adsız) olduğu için ikinci extra-var'ın
# HİÇBİR etkisi olmamalı; role yine gerçek değişkendeki sapmayı yakalayıp
# fail-closed kalmalı.
run_case "profile_lock_combined_shadow_attempt_path_fails_closed" fail \
  "FAIL-CLOSED: ssh_hardening_managed_drop_in_path" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ssh_hardening_managed_drop_in_path": "/etc/ssh/sshd_config.d/99-evil.conf", "ssh_hardening_locked_managed_path": "/etc/ssh/sshd_config.d/99-evil.conf"}'

run_case "profile_lock_combined_shadow_attempt_baseline_fails_closed" fail \
  "FAIL-CLOSED: bir veya daha fazla baseline değişkeni sabit audit değerinden sapmış" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ssh_hardening_permit_root_login": "yes", "ssh_hardening_locked_baseline": {"permit_root_login": "yes", "password_authentication": "no", "pubkey_authentication": "yes", "permit_empty_passwords": "no", "kbd_interactive_authentication": "no", "x11_forwarding": "no", "allow_agent_forwarding": "no", "allow_tcp_forwarding": "no", "max_auth_tries": "6", "login_grace_time": "60"}}'

run_case "profile_lock_combined_shadow_attempt_fields_list_fails_closed" fail \
  "FAIL-CLOSED: ssh_hardening_baseline_fields_exact listesi" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ssh_hardening_baseline_fields_exact": [{"key":"permitrootlogin","expected":"yes"},{"key":"passwordauthentication","expected":"no"},{"key":"pubkeyauthentication","expected":"yes"},{"key":"permitemptypasswords","expected":"no"},{"key":"kbdinteractiveauthentication","expected":"no"},{"key":"x11forwarding","expected":"no"},{"key":"allowagentforwarding","expected":"no"},{"key":"allowtcpforwarding","expected":"no"}], "ssh_hardening_locked_field_keys": ["permitrootlogin","passwordauthentication","pubkeyauthentication","permitemptypasswords","kbdinteractiveauthentication","x11forwarding","allowagentforwarding","allowtcpforwarding"], "ssh_hardening_locked_field_expected": ["yes","no","yes","no","no","no","no","no"]}'

# LIVE-AUDIT-FIX2: aynı gölgeleme-koruması kanıtı, yeni bounded-numeric
# listesi için de -- fake "ssh_hardening_locked_numeric_fields" decoy'u
# gerçek override ile BİRLİKTE verilir, hiçbir etkisi olmamalı.
run_case "profile_lock_combined_shadow_attempt_numeric_fields_list_fails_closed" fail \
  "FAIL-CLOSED: ssh_hardening_baseline_fields_numeric listesi" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ssh_hardening_baseline_fields_numeric": [{"key":"maxauthtries","max":99},{"key":"logingracetime","max_seconds":60}], "ssh_hardening_locked_numeric_fields": [{"key":"maxauthtries","max":99},{"key":"logingracetime","max_seconds":60}]}'

# --- 4. OS gate (os_check.yml) ---
run_case "os_gate_ubuntu_22_04_supported" pass \
  "Desteklenen sürüm: Ubuntu 22.04." \
  "${SCRIPT_DIR}/check_os_gate.yml"

run_case "os_gate_ubuntu_24_04_supported" pass \
  "Desteklenen sürüm: Ubuntu 24.04." \
  "${SCRIPT_DIR}/check_os_gate.yml" -e fake_distribution_version=24.04

run_case "os_gate_ubuntu_20_04_unsupported_fails_closed" fail \
  "UNSUPPORTED: Ubuntu 20.04 desteklenmiyor" \
  "${SCRIPT_DIR}/check_os_gate.yml" -e fake_distribution_version=20.04

run_case "os_gate_non_ubuntu_unsupported_fails_closed" fail \
  "UNSUPPORTED: Debian" \
  "${SCRIPT_DIR}/check_os_gate.yml" -e fake_distribution=Debian -e fake_distribution_version=22.04

# --- 5. Passwordless sudo/root önkoşulu -- DECISION mantığı
#        (system_checks.yml, R1-V3H4-SIMPLIFY: eski manuel onay mandalının
#        YERİNE geçen otomatik gate) ---
#
# Gerçek `become` + `id -u` çağrısı bu offline sandbox'ta gerçek root
# olmadan çalıştırılamaz (bkz. README.md "Sınırlar" -- aynı dürüst sınır
# apply_decisions harness'ında da kabul edilmiştir). Bu yüzden
# check_system_checks_gate.yml, yalnızca "become ile id -u çalıştır"
# ADIMINI `fake_uid_check_stdout` ile sürülen, become GEREKTİRMEYEN bir
# test-double komutla değiştirir; assert'in KENDİSİ (R1-V3H4-SIMPLIFY-
# AUDIT-FIX1) artık HİÇBİR yerde tekrar yazılmaz -- system_checks.yml
# VE bu harness AYNI `uid_gate_assert.yml` dosyasını import eder. Ölçülen:
# assert BAŞARISIZ olduğunda sonraki "apply/reload marker" task'ının HİÇ
# ÇALIŞMAMASI; assert BAŞARILI olduğunda ÇALIŞMASI -- yani "sudo/root
# önkoşulu başarısızken hiçbir apply/yazma/reload adımına ulaşılmaz"
# iddiasının davranışsal kanıtı.
run_case "system_checks_gate_uid_zero_opens_chain" pass \
  "APPLY_CHAIN_WOULD_RUN" \
  "${SCRIPT_DIR}/check_system_checks_gate.yml"

sc_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_system_checks_gate.yml" \
  -e fake_uid_check_stdout=1000 2>&1)
sc_rc=$?
sc_ok=1
[[ "${sc_rc}" -ne 0 ]] || sc_ok=0
grep -qF "FAIL-CLOSED: become sonrası gerçek UID 0 değil" <<<"${sc_out}" || sc_ok=0
grep -qF "APPLY_CHAIN_WOULD_RUN" <<<"${sc_out}" && sc_ok=0
assert_no_template_errors "${sc_out}" "system_checks_gate_uid_nonzero_fails_closed_blocks_chain" || sc_ok=0
TOTAL=$((TOTAL + 1))
if [[ "${sc_ok}" -eq 1 ]]; then
  echo "PASS  system_checks_gate_uid_nonzero_fails_closed_blocks_chain"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  system_checks_gate_uid_nonzero_fails_closed_blocks_chain (rc=${sc_rc})"
  echo "----- output -----"
  echo "${sc_out}"
  echo "-------------------"
fi

# --- 5a. Şema/yapı testleri (R1-V3H4-SIMPLIFY-AUDIT-FIX1): gerçek bir
#         become denemesi ÇALIŞTIRMADAN, doğrudan YAML yapısı üzerinden
#         doğrulanan iki sözleşme. ---

# a) Play seviyesindeki become_method/become_user/become_flags PİNİ tam
#    eşitlikle doğrulanır; play seviyesindeki become'un HÂLÂ false
#    olduğu (yani normal task'ların otomatik become olmadığı) da
#    doğrulanır.
schema_out=$(python3 - "${PROJECT_DIR}/ubuntu-ssh-hardening.yml" <<'PYEOF'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1]))
play = data[0]
errors = []
if play.get("become_method") != "sudo":
    errors.append(f"become_method={play.get('become_method')!r} (beklenen 'sudo')")
if play.get("become_user") != "root":
    errors.append(f"become_user={play.get('become_user')!r} (beklenen 'root')")
if play.get("become_flags") != "-H -S -n":
    errors.append(f"become_flags={play.get('become_flags')!r} (beklenen '-H -S -n')")
if play.get("become") is not False:
    errors.append(f"become={play.get('become')!r} (beklenen False -- yalnız become:true task'lar etkilenmeli)")
if errors:
    print("\n".join(errors))
    sys.exit(1)
PYEOF
)
schema_rc=$?
TOTAL=$((TOTAL + 1))
if [[ "${schema_rc}" -eq 0 ]]; then
  echo "PASS  playbook_become_contract_pinned_exact"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  playbook_become_contract_pinned_exact"
  echo "----- output -----"
  echo "${schema_out}"
  echo "-------------------"
fi

# b) main.yml'de system_checks.yml import'unun apply.yml import'undan
#    ÖNCE geldiği, YAML yapısı (task listesindeki index sırası) üzerinden
#    doğrulanır.
order_out=$(python3 - "${PROJECT_DIR}/roles/ssh_hardening/tasks/main.yml" <<'PYEOF'
import sys, yaml
tasks = yaml.safe_load(open(sys.argv[1]))

def find_index(target):
    for i, t in enumerate(tasks):
        for key in ("ansible.builtin.import_tasks", "ansible.builtin.include_tasks"):
            if t.get(key) == target:
                return i
    return None

sc_idx = find_index("system_checks.yml")
ap_idx = find_index("apply.yml")
errors = []
if sc_idx is None:
    errors.append("system_checks.yml main.yml'de import edilmiyor")
if ap_idx is None:
    errors.append("apply.yml main.yml'de import edilmiyor")
if sc_idx is not None and ap_idx is not None and not (sc_idx < ap_idx):
    errors.append(f"system_checks.yml (index={sc_idx}) apply.yml'den (index={ap_idx}) ÖNCE değil")
if errors:
    print("\n".join(errors))
    sys.exit(1)
PYEOF
)
order_rc=$?
TOTAL=$((TOTAL + 1))
if [[ "${order_rc}" -eq 0 ]]; then
  echo "PASS  main_yml_system_checks_import_precedes_apply_import"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  main_yml_system_checks_import_precedes_apply_import"
  echo "----- output -----"
  echo "${order_out}"
  echo "-------------------"
fi

# --- 5b. Hermetik paylaşım kanıtı (R1-V3H4-SIMPLIFY-AUDIT-FIX1.1):
#         uid_gate_assert.yml GERÇEKTEN tek kaynak mı? ÖNCEKİ round'da
#         bunu kanıtlamak için production dosyayı cp/sed/trap ile GEÇİCİ
#         olarak bozan bir mutasyon bloğu vardı -- bu KALDIRILDI (test
#         suite hiçbir tracked kaynak dosyaya YAZMAZ). Yerine gelen test
#         tamamen HERMETİKTİR: system_checks.yml VE
#         check_system_checks_gate.yml YAML olarak parse edilir,
#         ikisinin de `uid_gate_assert.yml` import'unu taşıyan task'ı
#         bulunur, bu import hedefi KENDİ dosya konumuna göre resolve
#         edilir ve ikisinin de GERÇEKTEN AYNI, tek
#         roles/ssh_hardening/tasks/uid_gate_assert.yml dosyasına
#         (os.path.realpath eşitliği) işaret ettiği doğrulanır. Hiçbir
#         dosya okunmaktan başka bir şekilde dokunulmaz/değiştirilmez.
shared_out=$(python3 - \
  "${PROJECT_DIR}/roles/ssh_hardening/tasks/system_checks.yml" \
  "${SCRIPT_DIR}/check_system_checks_gate.yml" \
  "${PROJECT_DIR}/roles/ssh_hardening/tasks/uid_gate_assert.yml" <<'PYEOF'
import sys, os, yaml

sc_file, gate_file, canonical_file = sys.argv[1:4]


def iter_tasks(doc_file):
    """task-list dosyalarını (system_checks.yml) VE tam playbook'ları
    (check_system_checks_gate.yml -- bir play sözlüğü, kendi `tasks:`
    listesi İÇİNDE) TEK bir düz task iterasyonuna indirger."""
    doc = yaml.safe_load(open(doc_file))
    for item in doc:
        if isinstance(item, dict) and "tasks" in item:
            yield from item["tasks"]
        else:
            yield item


def find_import_target(task_file, needle):
    for t in iter_tasks(task_file):
        for key in ("ansible.builtin.import_tasks", "ansible.builtin.include_tasks"):
            target = t.get(key)
            if target and os.path.basename(target) == needle:
                return target
    return None


def resolve(task_file, target):
    base_dir = os.path.dirname(os.path.abspath(task_file))
    return os.path.realpath(os.path.join(base_dir, target))


errors = []
sc_target = find_import_target(sc_file, "uid_gate_assert.yml")
gate_target = find_import_target(gate_file, "uid_gate_assert.yml")
if sc_target is None:
    errors.append(f"{sc_file} uid_gate_assert.yml'i import etmiyor")
if gate_target is None:
    errors.append(f"{gate_file} uid_gate_assert.yml'i import etmiyor")

if sc_target and gate_target:
    sc_resolved = resolve(sc_file, sc_target)
    gate_resolved = resolve(gate_file, gate_target)
    canonical_resolved = os.path.realpath(canonical_file)
    if sc_resolved != canonical_resolved:
        errors.append(
            f"system_checks.yml import hedefi ({sc_resolved}) canonical dosyaya "
            f"({canonical_resolved}) eşleşmiyor"
        )
    if gate_resolved != canonical_resolved:
        errors.append(
            f"check_system_checks_gate.yml import hedefi ({gate_resolved}) canonical "
            f"dosyaya ({canonical_resolved}) eşleşmiyor"
        )
    if sc_resolved != gate_resolved:
        errors.append("iki dosyanın import hedefleri birbirine eşit değil")

if errors:
    print("\n".join(errors))
    sys.exit(1)
PYEOF
)
shared_rc=$?
TOTAL=$((TOTAL + 1))
if [[ "${shared_rc}" -eq 0 ]]; then
  echo "PASS  uid_gate_assert_import_resolves_to_canonical_file_in_both_callers"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  uid_gate_assert_import_resolves_to_canonical_file_in_both_callers"
  echo "----- output -----"
  echo "${shared_out}"
  echo "-------------------"
fi

# --- 6. Şablon render testleri ---
# Not: run_case'in grep_expect'i ansible-playbook KONSOL çıktısına karşı
# çalışır; render edilen DOSYA içeriği ayrı olarak, doğrudan dosya
# üzerinde kontrol edilir (copy modülü render içeriğini konsola basmaz).
render_out="$(mktemp)"
run_case "template_renders_default_baseline" pass "" \
  "${SCRIPT_DIR}/render_template.yml" -e render_output_path="${render_out}"
if [[ -f "${render_out}" ]]; then
  TOTAL=$((TOTAL + 1))
  missing=0
  for expected_line in \
    "PermitRootLogin no" \
    "PasswordAuthentication no" \
    "PubkeyAuthentication yes" \
    "PermitEmptyPasswords no" \
    "KbdInteractiveAuthentication no" \
    "X11Forwarding no" \
    "AllowAgentForwarding no" \
    "AllowTcpForwarding no" \
    "MaxAuthTries 6" \
    "LoginGraceTime 60"
  do
    grep -qF -- "${expected_line}" "${render_out}" || missing=$((missing + 1))
  done
  # Kapsam disiplini: bu dilimde yönetilmeyen directive'ler asla bir
  # DIRECTIVE SATIRI olarak render edilmemeli (açıklayıcı yorum
  # satırlarında adlarının GEÇMESİ beklenir ve sorun değildir -- bu
  # yüzden yalnız '#' ile başlamayan satırlar kontrol edilir).
  forbidden=0
  for forbidden_directive in AllowUsers AllowGroups DenyUsers DenyGroups Ciphers MACs KexAlgorithms; do
    grep -vE '^[[:space:]]*#' "${render_out}" | grep -qE "^${forbidden_directive}([[:space:]]|\$)" && forbidden=$((forbidden + 1))
  done
  if [[ "${missing}" -eq 0 && "${forbidden}" -eq 0 ]]; then
    echo "PASS  template_render_contains_exactly_the_10_baseline_directives"
  else
    FAILED=$((FAILED + 1))
    echo "FAIL  template_render_contains_exactly_the_10_baseline_directives (eksik=${missing} yasak=${forbidden})"
  fi
fi
rm -f "${render_out}"

# FIX1.1/BULGU4: aşağıdaki iki test, tests/render_template.yml'i (yalnız
# .j2 dosyasını render eden İZOLE bir harness -- rolün gerçek giriş
# noktası main.yml'i hiç çağırmaz, profile_lock_check.yml BURADA HİÇ
# ÇALIŞMAZ) BİLEREK bir security-baseline alanıyla (ssh_hardening_
# password_authentication) çağırır. Bu SADECE Jinja2 ikame mekanizmasının
# (şablon dosyasındaki `{{ ssh_hardening_password_authentication }}`
# gibi ifadelerin) doğru çalıştığını kanıtlar -- ".j2 dosyasında yazım
# hatası yok" türünde bir birim testidir. BUNU "override edip normal
# mode'da çalıştırılabilir bir aday üretmenin desteklendiği" biçiminde
# OKUMAYIN: gerçek role çağrısında (main.yml), bu TAM AYNI override
# apply.yml'e hiç ulaşmadan `profile_lock_baseline_value_override_fails_closed`
# testinin kanıtladığı gibi FAIL-CLOSED reddedilir. Ürün sözleşmesi
# budur; buradaki test yalnız iç şablon mekanizmasını, ondan TAMAMEN
# AYRIŞTIRILMIŞ biçimde ölçer.
render_override_out="$(mktemp)"
run_case "template_j2_substitution_mechanism_only_ISOLATED_from_role_entrypoint" pass "" \
  "${SCRIPT_DIR}/render_template.yml" -e render_output_path="${render_override_out}" -e ssh_hardening_password_authentication=yes
if [[ -f "${render_override_out}" ]]; then
  TOTAL=$((TOTAL + 1))
  if grep -qF -- "PasswordAuthentication yes" "${render_override_out}"; then
    echo "PASS  template_j2_substitution_reflects_given_variable_value_in_isolation"
  else
    FAILED=$((FAILED + 1))
    echo "FAIL  template_j2_substitution_reflects_given_variable_value_in_isolation"
  fi
fi
rm -f "${render_override_out}"

run_case "template_render_requires_output_path" fail \
  "render_output_path=<path> ile bir çıktı dosyası verilmeli" \
  "${SCRIPT_DIR}/render_template.yml"

# --- 7. Compliance mantığı (compliance_assert.yml -- pre/post-reload'da
#        ORTAK, parametreli) ---
run_case "compliance_fully_compliant_passes" pass "" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_compliant.txt"

run_case "compliance_tampered_password_auth_fails_closed" fail \
  "NON-COMPLIANT/FAIL-CLOSED (test): passwordauthentication sshd -T çıktısında 1 kez görüldü (1 bekleniyor), değer='yes', beklenen='no'." \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_tampered_password_auth.txt"

run_case "compliance_missing_field_fails_closed" fail \
  "kbdinteractiveauthentication sshd -T çıktısında 0 kez görüldü" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_missing_field.txt"

run_case "compliance_duplicate_field_fails_closed" fail \
  "permitrootlogin sshd -T çıktısında 2 kez görüldü" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_duplicate_field.txt"

# LIVE-AUDIT-FIX2: MaxAuthTries/LoginGraceTime artık EXACT değil, audit ile
# birebir hizalı bounded (üst-sınırlı) semantikle değerlendirilir --
# canlı bulgu: hedefte sözlüksel olarak daha erken sıralanan başka bir
# drop-in `MaxAuthTries 3` uyguluyordu (audit açısından daha sıkı ve
# uyumlu) ve eski exact kontrol bunu yanlışlıkla reddediyordu.

# "Canlı hedef senaryosu": maxauthtries=3 (daha sıkı, template'in yazdığı
# 6'dan farklı) + logingracetime=60 -> ikisi de bounded politikaya uyuyor,
# COMPLIANT.
run_case "compliance_live_target_scenario_maxauthtries_3_logingracetime_60_passes" pass "" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_live_target_scenario.txt"

# maxauthtries=6 (template'in yazdığı güvenli varsayılan) zaten
# compliance_fully_compliant_passes testiyle (yukarıda) kanıtlanmıştır.

run_case "compliance_maxauthtries_0_fails_closed" fail \
  "NON-COMPLIANT/FAIL-CLOSED (test): maxauthtries sshd -T çıktısında 1 kez görüldü (1 bekleniyor), değer='0'" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_maxauthtries_0.txt"

run_case "compliance_maxauthtries_7_fails_closed" fail \
  "NON-COMPLIANT/FAIL-CLOSED (test): maxauthtries sshd -T çıktısında 1 kez görüldü (1 bekleniyor), değer='7'" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_maxauthtries_7.txt"

run_case "compliance_maxauthtries_malformed_fails_closed" fail \
  "değer='abc' (1..6 arası pozitif tam sayı bekleniyor" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_maxauthtries_malformed.txt"

run_case "compliance_maxauthtries_missing_fails_closed" fail \
  "maxauthtries sshd -T çıktısında 0 kez görüldü" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_maxauthtries_missing.txt"

run_case "compliance_maxauthtries_duplicate_fails_closed" fail \
  "maxauthtries sshd -T çıktısında 2 kez görüldü" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_maxauthtries_duplicate.txt"

run_case "compliance_logingracetime_30_passes" pass "" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_logingracetime_30.txt"

# logingracetime=60 zaten compliance_fully_compliant_passes ve
# compliance_live_target_scenario_maxauthtries_3_logingracetime_60_passes
# testleriyle kanıtlanmıştır.

run_case "compliance_logingracetime_0_fails_closed" fail \
  "0 sınırsızdır ve kabul edilmez" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_logingracetime_0.txt"

run_case "compliance_logingracetime_61_fails_closed" fail \
  "ham değer='61'" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_logingracetime_61.txt"

run_case "compliance_logingracetime_malformed_fails_closed" fail \
  "hesaplanan=ayrıştırılamadı" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_logingracetime_malformed.txt"

run_case "compliance_logingracetime_missing_fails_closed" fail \
  "logingracetime sshd -T çıktısında 0 kez görüldü" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_logingracetime_missing.txt"

run_case "compliance_logingracetime_duplicate_fails_closed" fail \
  "logingracetime sshd -T çıktısında 2 kez görüldü" \
  "${SCRIPT_DIR}/check_compliance.yml" -e fixture_path="${FIXTURES_DIR}/post_sshd_t_logingracetime_duplicate.txt"

# Birden fazla uygunsuzlukta tüm 10 alanın da değerlendirildiğini doğrula
# (ilk uygunsuzlukta durmamalı -- ssh_audit'teki aynı ilke).
TOTAL=$((TOTAL + 1))
multi_output=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_compliance.yml" \
  -e fixture_path="${FIXTURES_DIR}/post_sshd_t_multiple_non_compliant.txt" 2>&1)
multi_rc=$?
multi_ok=1
[[ "${multi_rc}" -ne 0 ]] || multi_ok=0
for needle in \
  "NON-COMPLIANT/FAIL-CLOSED (test): permitrootlogin" \
  "NON-COMPLIANT/FAIL-CLOSED (test): passwordauthentication" \
  "NON-COMPLIANT/FAIL-CLOSED (test): permitemptypasswords" \
  "NON-COMPLIANT/FAIL-CLOSED (test): x11forwarding" \
  "NON-COMPLIANT/FAIL-CLOSED (test): maxauthtries" \
  "NON-COMPLIANT/FAIL-CLOSED (test): logingracetime" \
  "COMPLIANT (test): pubkeyauthentication=yes." \
  "COMPLIANT (test): kbdinteractiveauthentication=no." \
  "COMPLIANT (test): allowagentforwarding=no." \
  "COMPLIANT (test): allowtcpforwarding=no." \
  "NON-COMPLIANT (test): bir veya daha fazla alan beklenen değerden sapmış"
do
  grep -qF -- "${needle}" <<<"${multi_output}" || multi_ok=0
done
assert_no_template_errors "${multi_output}" "compliance_multiple_non_compliant_evaluates_all_10_fields" || multi_ok=0
if [[ "${multi_ok}" -eq 1 ]]; then
  echo "PASS  compliance_multiple_non_compliant_evaluates_all_10_fields"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  compliance_multiple_non_compliant_evaluates_all_10_fields (rc=${multi_rc})"
  echo "----- output -----"
  echo "${multi_output}"
  echo "-------------------"
fi

# --- 8. Apply karar akışı (apply_decisions_*.yml, BULGU1/BULGU2 regresyon
#        kanıtları -- gerçek template/command/copy/file/systemd modülleri
#        SİMÜLE EDİLİR, gerçek hedef gerekmez) ---

# Regresyon 1: aday sshd -t (syntax) BAŞARISIZ, backup VAR -> restore
# çalışır, remove çalışmaz, reload=0.
run_case "apply_decision_candidate_syntax_fails_with_backup_restores_no_reload" pass "" \
  "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_candidate_verify_rc": 1, "fake_apply_backup_file": "/etc/ssh/sshd_config.d/00-doransible-ssh-hardening.conf.123.ts~"}'
apply_dec_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_candidate_verify_rc": 1, "fake_apply_backup_file": "/etc/ssh/sshd_config.d/00-doransible-ssh-hardening.conf.123.ts~"}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT candidate_failed=True" <<<"${apply_dec_out}" \
  && grep -qF "RESULT candidate_failure_reason=syntax" <<<"${apply_dec_out}" \
  && grep -qF "RESULT restore_would_run=True" <<<"${apply_dec_out}" \
  && grep -qF "RESULT remove_would_run=False" <<<"${apply_dec_out}" \
  && grep -qF "RESULT reload_would_run=False" <<<"${apply_dec_out}"; then
  echo "PASS  apply_decision_candidate_syntax_fails_with_backup_restores_no_reload_flags"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decision_candidate_syntax_fails_with_backup_restores_no_reload_flags"
  echo "${apply_dec_out}"
fi

# Regresyon 1b: aynı senaryo ama backup YOK (ilk çalıştırma) -> remove
# çalışır, restore çalışmaz, reload=0.
apply_dec_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_candidate_verify_rc": 1}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT restore_would_run=False" <<<"${apply_dec_out}" \
  && grep -qF "RESULT remove_would_run=True" <<<"${apply_dec_out}" \
  && grep -qF "RESULT reload_would_run=False" <<<"${apply_dec_out}"; then
  echo "PASS  apply_decision_candidate_syntax_fails_without_backup_removes_no_reload"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decision_candidate_syntax_fails_without_backup_removes_no_reload"
  echo "${apply_dec_out}"
fi

# Regresyon 1c (FIX1.1/BULGU1): aday syntax GEÇERLİ ama pre-reload
# `sshd -T` OKUMA komutunun KENDİSİ nonzero rc ile döner (effective-read
# -- "değerler yanlış"tan AYRI bir sınıf; compliance kontrolü hiç
# çalışmadı). Rollback tetiklenir, reload=0. Bu senaryo, FIX1.1'in asıl
# düzeltmesini (bu komutun eskiden `failed_when: false` TAŞIMAMASI ve
# nonzero rc'de play'i erken kesmesi) doğrudan hedefler.
apply_dec_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_candidate_verify_rc": 0, "fake_pre_reload_read_rc": 1, "fake_apply_backup_file": "/some/backup"}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT candidate_verify_failed=False" <<<"${apply_dec_out}" \
  && grep -qF "RESULT pre_reload_read_failed=True" <<<"${apply_dec_out}" \
  && grep -qF "RESULT candidate_failed=True" <<<"${apply_dec_out}" \
  && grep -qF "RESULT candidate_failure_reason=effective-read" <<<"${apply_dec_out}" \
  && grep -qF "RESULT restore_would_run=True" <<<"${apply_dec_out}" \
  && grep -qF "RESULT reload_would_run=False" <<<"${apply_dec_out}"; then
  echo "PASS  apply_decision_pre_reload_read_command_fails_triggers_rollback_no_reload"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decision_pre_reload_read_command_fails_triggers_rollback_no_reload"
  echo "${apply_dec_out}"
fi

# Regresyon 2: aday syntax VE pre-reload OKUMA GEÇERLİ ama okunan
# effective baseline UYUŞMUYOR -> rollback tetiklenir (backup
# varsayımıyla), reload=0.
apply_dec_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_candidate_verify_rc": 0, "fake_pre_reload_read_rc": 0, "fake_compliance_failed": true, "fake_apply_backup_file": "/some/backup"}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT candidate_verify_failed=False" <<<"${apply_dec_out}" \
  && grep -qF "RESULT pre_reload_read_failed=False" <<<"${apply_dec_out}" \
  && grep -qF "RESULT candidate_failed=True" <<<"${apply_dec_out}" \
  && grep -qF "RESULT candidate_failure_reason=effective-baseline" <<<"${apply_dec_out}" \
  && grep -qF "RESULT restore_would_run=True" <<<"${apply_dec_out}" \
  && grep -qF "RESULT reload_would_run=False" <<<"${apply_dec_out}"; then
  echo "PASS  apply_decision_pre_reload_baseline_mismatch_triggers_rollback_no_reload"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decision_pre_reload_baseline_mismatch_triggers_rollback_no_reload"
  echo "${apply_dec_out}"
fi

# Regresyon 3: her iki doğrulama da başarılı VE içerik değişti -> yalnız
# reload_would_run=True (rollback yollarının hiçbiri).
apply_dec_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_candidate_verify_rc": 0, "fake_compliance_failed": false, "fake_apply_changed": true}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT candidate_failed=False" <<<"${apply_dec_out}" \
  && grep -qF "RESULT reload_would_run=True" <<<"${apply_dec_out}" \
  && grep -qF "RESULT restore_would_run=False" <<<"${apply_dec_out}" \
  && grep -qF "RESULT remove_would_run=False" <<<"${apply_dec_out}"; then
  echo "PASS  apply_decision_full_success_reloads_exactly_once"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decision_full_success_reloads_exactly_once"
  echo "${apply_dec_out}"
fi

# Regresyon 4: idempotent ikinci çalıştırma -- her iki doğrulama da
# başarılı ama içerik DEĞİŞMEDİ -> reload_would_run=False.
apply_dec_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_candidate_verify_rc": 0, "fake_compliance_failed": false, "fake_apply_changed": false}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT candidate_failed=False" <<<"${apply_dec_out}" \
  && grep -qF "RESULT reload_would_run=False" <<<"${apply_dec_out}"; then
  echo "PASS  apply_decision_idempotent_second_run_no_reload"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decision_idempotent_second_run_no_reload"
  echo "${apply_dec_out}"
fi

# Rollback doğrulaması BAŞARILI: "rollback yapıldı" iddiası kurulabilir.
apply_dec_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_candidate_verify_rc": 1, "run_rollback_normalize": true, "fake_rollback_verify_rc": 0}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT rollback_verify_failed=False" <<<"${apply_dec_out}"; then
  echo "PASS  apply_decision_rollback_verify_succeeds_claim_allowed"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decision_rollback_verify_succeeds_claim_allowed"
  echo "${apply_dec_out}"
fi

# Rollback doğrulaması BAŞARISIZ (kritik durum): "rollback yapıldı" iddiası
# KURULAMAZ; bu bayrak apply.yml'in fail mesajını KRİTİK dala yönlendirir.
apply_dec_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_candidate_verify_rc": 1, "run_rollback_normalize": true, "fake_rollback_verify_rc": 1}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT rollback_verify_failed=True" <<<"${apply_dec_out}"; then
  echo "PASS  apply_decision_rollback_verify_fails_claim_forbidden"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decision_rollback_verify_fails_claim_forbidden"
  echo "${apply_dec_out}"
fi

# --- 9. post_verify include-gate (LIVE-AUDIT-FIX1: `meta: reset_connection`
#        kendi üzerinde `when:` desteklemez ve canlı UI check-mode koşusunda
#        bu yüzden yanlışlıkla çalıştığı GÖZLEMLENDİ -- düzeltme main.yml'in
#        post_verify.yml'i STATIC `import_tasks` yerine DYNAMIC
#        `include_tasks` ile dahil etmesidir. Bu bölüm davranışı gerçek
#        `ansible-playbook` çıktısı üzerinden, gerçek `--check` bayrağıyla
#        kanıtlar; `ansible_check_mode` sahte bir değişkenle taklit
#        EDİLMEZ.) ---

# Regresyon a: check mode -- post_verify.yml'in İÇERİĞİ hiç dahil
# edilmemeli; ne reset_connection task adı ne "does not support when
# conditional" uyarısı çıktıda görünmeli.
pv_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_post_verify_gate.yml" --check 2>&1)
pv_rc=$?
pv_ok=1
[[ "${pv_rc}" -eq 0 ]] || pv_ok=0
grep -qF "Bağlantıyı sıfırla" <<<"${pv_out}" && pv_ok=0
grep -qiF "does not support when conditional" <<<"${pv_out}" && pv_ok=0
TOTAL=$((TOTAL + 1))
if [[ "${pv_ok}" -eq 1 ]]; then
  echo "PASS  post_verify_gate_check_mode_never_includes_reset_connection"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  post_verify_gate_check_mode_never_includes_reset_connection (rc=${pv_rc})"
  echo "----- output -----"
  echo "${pv_out}"
  echo "-------------------"
fi

# Regresyon b: normal mode ama aday BAŞARISIZ -- aynı gate, aynı sebeple
# zinciri açmamalı.
pv_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_post_verify_gate.yml" -e fake_candidate_failed=true 2>&1)
pv_rc=$?
pv_ok=1
[[ "${pv_rc}" -eq 0 ]] || pv_ok=0
grep -qF "Bağlantıyı sıfırla" <<<"${pv_out}" && pv_ok=0
grep -qiF "does not support when conditional" <<<"${pv_out}" && pv_ok=0
TOTAL=$((TOTAL + 1))
if [[ "${pv_ok}" -eq 1 ]]; then
  echo "PASS  post_verify_gate_candidate_failed_never_includes_reset_connection"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  post_verify_gate_candidate_failed_never_includes_reset_connection (rc=${pv_rc})"
  echo "----- output -----"
  echo "${pv_out}"
  echo "-------------------"
fi

# Regresyon c: normal mode VE aday başarılı -- include_tasks GERÇEKTEN
# devreye girmeli (reset_connection task'ı çıktıda BAŞLAMALI); zincir bu
# sandbox'ta gerçek sudo/hedef olmadığı için sonraki adımda (post-reload
# sshd -T) başarısız olur -- bu BEKLENENdir (bkz. README.md "Sınırlar"),
# ölçülen yalnız include'un tetiklenmesidir. "does not support when
# conditional" uyarısı BURADA DA görünmemeli (post_verify.yml içindeki
# task artık kendi üzerinde when taşımıyor).
pv_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_post_verify_gate.yml" 2>&1)
pv_ok=1
grep -qF "Bağlantıyı sıfırla" <<<"${pv_out}" || pv_ok=0
grep -qiF "does not support when conditional" <<<"${pv_out}" && pv_ok=0
assert_no_template_errors "${pv_out}" "post_verify_gate_success_path_opens_chain" || pv_ok=0
TOTAL=$((TOTAL + 1))
if [[ "${pv_ok}" -eq 1 ]]; then
  echo "PASS  post_verify_gate_success_path_opens_chain_no_warning"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  post_verify_gate_success_path_opens_chain_no_warning"
  echo "----- output -----"
  echo "${pv_out}"
  echo "-------------------"
fi

# --- 10. ansible-lint (varsa) ---
TOTAL=$((TOTAL + 1))
if command -v ansible-lint >/dev/null 2>&1; then
  lint_output=$(ansible-lint "${PROJECT_DIR}" 2>&1)
  lint_rc=$?
  if [[ "${lint_rc}" -eq 0 ]]; then
    echo "PASS  ansible_lint"
  else
    FAILED=$((FAILED + 1))
    echo "FAIL  ansible_lint (rc=${lint_rc})"
    echo "----- output -----"
    echo "${lint_output}"
    echo "-------------------"
  fi
else
  echo "SKIP  ansible_lint (ansible-lint bu ortamda kurulu değil)"
  TOTAL=$((TOTAL - 1))
fi

echo
echo "${TOTAL} test, $((TOTAL - FAILED)) geçti, ${FAILED} başarısız."
[[ "${FAILED}" -eq 0 ]]
