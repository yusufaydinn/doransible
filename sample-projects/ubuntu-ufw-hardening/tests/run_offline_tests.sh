#!/usr/bin/env bash
# tests/run_offline_tests.sh
#
# Kalıcı offline regresyon testleri: gerçek bir SSH hostu, bağlantı veya
# sudo gerektirmeden bu project'in YAML'ini, playbook syntax'ını ve
# remediation mantığının saf-logic kısımlarını (profil kilidi; OS gate;
# passwordless sudo/root önkoşulunun fail-closed gate mantığı; firewalld
# fail-closed karar matrisi; SSH portu 1..65535 doğrulaması; ön-okuma
# karar mantığı -- would_*; enable-sonrası yeniden doğrulama; check-mode
# reset_connection sızıntısı; izin verilen komut yüzeyi VE yazma sırası)
# doğrular.
#
# Bu round KASITLI olarak gerçek bir hedefte ÇALIŞTIRMA yapmaz:
# system_checks.yml (sudo/root, ufw binary, firewalld) ve apply.yml'nin
# gerçek write/enable/reconnect zincirinin MODÜL ÇAĞRILARI (command'lar
# become gerektirir) gerçek bir Ubuntu hedef VE become gerektirir; bu
# script bunu KAPSAMAZ (bu ortamda passwordless sudo yoktur -- bkz.
# README.md "Sınırlar"). Bunun yerine SAF karar mantığı
# (apply_decisions.yml, compliance_reverify.yml, *_gate_assert.yml)
# gerçek modüllerin ÜRETMİŞ OLACAĞI sonuçları simüle eden sahte
# register/fixture değişkenleriyle doğrudan test edilir.
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

cd "${PROJECT_DIR}"

# --- 1. YAML parse: proje içindeki her .yml/.yaml dosyası saf YAML olarak
#        parse edilebiliyor mu VE bulunan dosya sayısı sıfır değil mi ---
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
  --syntax-check -i "${PROJECT_DIR}/inventory/hosts.yml" "${PROJECT_DIR}/ubuntu-ufw-hardening.yml"

# --- 3. Profil kilidi (profile_lock_check.yml) ---
run_case "profile_lock_default_passes" pass \
  "Reconnect timeout/sleep sabit değerlerle eşleşiyor (30/2)." \
  "${SCRIPT_DIR}/check_profile_lock.yml"

run_case "profile_lock_supported_versions_truncated_fails_closed" fail \
  "FAIL-CLOSED: ufw_hardening_supported_ubuntu_versions" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ufw_hardening_supported_ubuntu_versions": ["22.04"]}'

run_case "profile_lock_supported_versions_extra_entry_fails_closed" fail \
  "FAIL-CLOSED: ufw_hardening_supported_ubuntu_versions" \
  "${SCRIPT_DIR}/check_profile_lock.yml" \
  -e '{"ufw_hardening_supported_ubuntu_versions": ["22.04", "24.04", "20.04"]}'

run_case "profile_lock_input_policy_override_fails_closed" fail \
  "FAIL-CLOSED: bir veya daha fazla baseline değişkeni sabit audit değerinden sapmış" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ufw_hardening_expected_default_input_policy=ACCEPT

run_case "profile_lock_output_policy_override_fails_closed" fail \
  "FAIL-CLOSED: bir veya daha fazla baseline değişkeni sabit audit değerinden sapmış" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ufw_hardening_expected_default_output_policy=DROP

run_case "profile_lock_forward_policy_override_fails_closed" fail \
  "FAIL-CLOSED: bir veya daha fazla baseline değişkeni sabit audit değerinden sapmış" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ufw_hardening_expected_default_forward_policy=ACCEPT

run_case "profile_lock_logging_level_override_fails_closed" fail \
  "FAIL-CLOSED: bir veya daha fazla baseline değişkeni sabit audit değerinden sapmış" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ufw_hardening_logging_level=full

run_case "profile_lock_reconnect_timeout_override_fails_closed" fail \
  "FAIL-CLOSED: reconnect zaman aşımı ayarları" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ufw_hardening_reconnect_timeout_seconds=9999

run_case "profile_lock_reconnect_sleep_override_fails_closed" fail \
  "FAIL-CLOSED: reconnect zaman aşımı ayarları" \
  "${SCRIPT_DIR}/check_profile_lock.yml" -e ufw_hardening_reconnect_sleep_seconds=0

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

# --- 5. firewalld fail-closed karar matrisi (firewalld_gate_assert.yml) ---
run_case "firewalld_active_fails_closed" fail \
  "FAIL-CLOSED: firewalld aktif" \
  "${SCRIPT_DIR}/check_firewalld_gate.yml" -e fake_firewalld_rc=0 -e fake_firewalld_stdout=active

run_case "firewalld_rc_stdout_conflict_fails_closed" fail \
  "FAIL-CLOSED: firewalld durumu güvenle inactive/kurulu-değil olarak doğrulanamadı" \
  "${SCRIPT_DIR}/check_firewalld_gate.yml" -e fake_firewalld_rc=0 -e fake_firewalld_stdout=inactive

run_case "firewalld_unrecognized_stdout_fails_closed" fail \
  "FAIL-CLOSED: firewalld durumu güvenle inactive/kurulu-değil olarak doğrulanamadı" \
  "${SCRIPT_DIR}/check_firewalld_gate.yml" -e fake_firewalld_rc=1 -e fake_firewalld_stdout=failed

run_case "firewalld_installed_but_stopped_rc3_recognized_safe" pass \
  "APPLY_CHAIN_WOULD_RUN" \
  "${SCRIPT_DIR}/check_firewalld_gate.yml" -e fake_firewalld_rc=3 -e fake_firewalld_stdout=inactive

run_case "firewalld_not_installed_rc4_recognized_safe" pass \
  "APPLY_CHAIN_WOULD_RUN" \
  "${SCRIPT_DIR}/check_firewalld_gate.yml" -e fake_firewalld_rc=4 -e fake_firewalld_stdout=inactive

run_case "firewalld_not_installed_rc4_unknown_recognized_safe" pass \
  "APPLY_CHAIN_WOULD_RUN" \
  "${SCRIPT_DIR}/check_firewalld_gate.yml" -e fake_firewalld_rc=4 -e fake_firewalld_stdout=unknown

# --- 6. SSH portu gate (ssh_port_gate_assert.yml) -- geçersiz port YAZMA
#        KOMUTLARINDAN ÖNCE reddedilir ---
run_case "ssh_port_gate_default_22_when_unset" pass \
  "APPLY_CHAIN_WOULD_RUN (port=22)" \
  "${SCRIPT_DIR}/check_ssh_port_gate.yml"

run_case "ssh_port_gate_valid_custom_port" pass \
  "APPLY_CHAIN_WOULD_RUN (port=2222)" \
  "${SCRIPT_DIR}/check_ssh_port_gate.yml" -e ansible_port=2222

run_case "ssh_port_gate_zero_out_of_range_fails_closed" fail \
  "ansible_port='0' geçerli bir 1..65535 port numarası değil" \
  "${SCRIPT_DIR}/check_ssh_port_gate.yml" -e ansible_port=0

run_case "ssh_port_gate_65536_out_of_range_fails_closed" fail \
  "ansible_port='65536' geçerli bir 1..65535 port numarası değil" \
  "${SCRIPT_DIR}/check_ssh_port_gate.yml" -e ansible_port=65536

# DÜRÜST MODELLEME (ubuntu-ufw-audit ile aynı desen): `ansible_port`
# Ansible'ın ÖZEL bir bağlantı anahtarıdır; sayıya çevrilemeyen bir değer
# role'ün kendi task'larından biri bile çalışmadan Ansible'ın kendi
# bağlantı katmanı tarafından reddedilir.
run_case "ssh_port_gate_non_numeric_rejected_by_ansible_connection_layer" fail \
  "The value 'notanumber' could not be converted to 'int'" \
  "${SCRIPT_DIR}/check_ssh_port_gate.yml" -e ansible_port=notanumber

# --- 7. Passwordless sudo/root önkoşulu -- DECISION mantığı
#        (system_checks.yml, DORAnsible sudo yetkisi vermez sözleşmesi) ---
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

# --- 8. Ön-okuma karar mantığı (apply_decisions.yml, would_*) ---
run_case "apply_decisions_idempotent_second_run_no_changes_planned" pass "" \
  "${SCRIPT_DIR}/check_apply_decisions.yml"
ad_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT would_add_ssh_allow=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_incoming=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_outgoing=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_forward=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_logging=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_enable=False" <<<"${ad_out}" \
  && grep -qF "RESULT any_change=False" <<<"${ad_out}"; then
  echo "PASS  apply_decisions_idempotent_second_run_flags_all_false"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decisions_idempotent_second_run_flags_all_false"
  echo "${ad_out}"
fi

ad_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_show_added_lines": [], "fake_status_lines": ["Status: inactive"], "fake_default_ufw_raw": "DEFAULT_INPUT_POLICY=\"ACCEPT\"\nDEFAULT_OUTPUT_POLICY=\"ACCEPT\"\nDEFAULT_FORWARD_POLICY=\"ACCEPT\"", "fake_ufw_conf_raw": "LOGLEVEL=off"}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT would_add_ssh_allow=True" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_incoming=True" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_outgoing=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_forward=True" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_logging=True" <<<"${ad_out}" \
  && grep -qF "RESULT would_enable=True" <<<"${ad_out}" \
  && grep -qF "RESULT any_change=True" <<<"${ad_out}"; then
  echo "PASS  apply_decisions_fresh_host_plans_all_needed_changes"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decisions_fresh_host_plans_all_needed_changes"
  echo "${ad_out}"
fi

ad_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_show_added_lines": ["ufw allow 2222/tcp"], "fake_ssh_port": 2222}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT would_add_ssh_allow=False" <<<"${ad_out}"; then
  echo "PASS  apply_decisions_nonstandard_port_matching_rule_no_add_needed"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decisions_nonstandard_port_matching_rule_no_add_needed"
  echo "${ad_out}"
fi

ad_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_show_added_lines": ["ufw allow 22/tcp"], "fake_ssh_port": 2222}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT would_add_ssh_allow=True" <<<"${ad_out}"; then
  echo "PASS  apply_decisions_nonstandard_port_without_matching_rule_add_needed"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decisions_nonstandard_port_without_matching_rule_add_needed"
  echo "${ad_out}"
fi

# --- 8b. Ön-okuma fail-closed doğrulaması (apply_decisions.yml, BULGU2)
#         -- rc!=0 veya duplicate/eksik/tanınmayan bir alan ASLA sessizce
#         "değişiklik gerekli" sayılmaz; hiçbir "RESULT" (dolayısıyla
#         apply.yml'de hiçbir yazma komutu) üretilmeden play burada durur ---
check_apply_decisions_blocks_all_writers() {
  local name="$1"
  shift
  local out rc ok
  out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" "$@" 2>&1)
  rc=$?
  ok=1
  [[ "${rc}" -ne 0 ]] || ok=0
  grep -qF "FAIL-CLOSED (ön-okuma doğrulaması)" <<<"${out}" || ok=0
  grep -qF "RESULT" <<<"${out}" && ok=0
  assert_no_template_errors "${out}" "${name}" || ok=0
  TOTAL=$((TOTAL + 1))
  if [[ "${ok}" -eq 1 ]]; then
    echo "PASS  ${name}"
  else
    FAILED=$((FAILED + 1))
    echo "FAIL  ${name} (rc=${rc})"
    echo "----- output -----"
    echo "${out}"
    echo "-------------------"
  fi
}

check_apply_decisions_blocks_all_writers \
  apply_decisions_show_added_command_failure_blocks_all_writers \
  -e fake_show_added_rc=1

check_apply_decisions_blocks_all_writers \
  apply_decisions_status_command_failure_blocks_all_writers \
  -e fake_status_rc=1

check_apply_decisions_blocks_all_writers \
  apply_decisions_duplicate_policy_field_blocks_all_writers \
  -e '{"fake_default_ufw_raw": "DEFAULT_INPUT_POLICY=\"DROP\"\nDEFAULT_INPUT_POLICY=\"ACCEPT\"\nDEFAULT_OUTPUT_POLICY=\"ACCEPT\"\nDEFAULT_FORWARD_POLICY=\"DROP\""}'

check_apply_decisions_blocks_all_writers \
  apply_decisions_malformed_policy_value_blocks_all_writers \
  -e '{"fake_default_ufw_raw": "DEFAULT_INPUT_POLICY=\"MAYBE\"\nDEFAULT_OUTPUT_POLICY=\"ACCEPT\"\nDEFAULT_FORWARD_POLICY=\"DROP\""}'

check_apply_decisions_blocks_all_writers \
  apply_decisions_missing_loglevel_blocks_all_writers \
  -e fake_ufw_conf_raw=""

check_apply_decisions_blocks_all_writers \
  apply_decisions_malformed_loglevel_blocks_all_writers \
  -e fake_ufw_conf_raw=LOGLEVEL=verbose

# --- 8c. Tek alan değişiklikleri (BULGU1 -- yalnız ilgili writer/would_*
#         true olmalı, geri kalanı ve any_change tutarlı olmalı) ---
ad_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_default_ufw_raw": "DEFAULT_INPUT_POLICY=\"ACCEPT\"\nDEFAULT_OUTPUT_POLICY=\"ACCEPT\"\nDEFAULT_FORWARD_POLICY=\"DROP\""}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT would_add_ssh_allow=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_incoming=True" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_outgoing=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_forward=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_logging=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_enable=False" <<<"${ad_out}" \
  && grep -qF "RESULT any_change=True" <<<"${ad_out}"; then
  echo "PASS  apply_decisions_only_incoming_differs_only_that_writer_needed"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decisions_only_incoming_differs_only_that_writer_needed"
  echo "${ad_out}"
fi

ad_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_status_lines": ["Status: inactive"]}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT would_add_ssh_allow=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_incoming=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_outgoing=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_forward=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_logging=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_enable=True" <<<"${ad_out}" \
  && grep -qF "RESULT any_change=True" <<<"${ad_out}"; then
  echo "PASS  apply_decisions_inactive_only_only_enable_needed"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decisions_inactive_only_only_enable_needed"
  echo "${ad_out}"
fi

ad_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_apply_decisions.yml" \
  -e '{"fake_show_added_lines": []}' 2>&1)
TOTAL=$((TOTAL + 1))
if grep -qF "RESULT would_add_ssh_allow=True" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_incoming=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_outgoing=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_forward=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_set_logging=False" <<<"${ad_out}" \
  && grep -qF "RESULT would_enable=False" <<<"${ad_out}" \
  && grep -qF "RESULT any_change=True" <<<"${ad_out}"; then
  echo "PASS  apply_decisions_missing_ssh_rule_only_ssh_allow_needed"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  apply_decisions_missing_ssh_rule_only_ssh_allow_needed"
  echo "${ad_out}"
fi

# --- 9. Enable-sonrası yeniden doğrulama (compliance_reverify.yml) ---
run_case "reverify_fully_compliant_passes" pass "" \
  "${SCRIPT_DIR}/check_compliance.yml" \
  -e status_fixture_path="${FIXTURES_DIR}/status_verbose_compliant.txt" \
  -e default_ufw_fixture_path="${FIXTURES_DIR}/default_ufw_compliant.txt"

run_case "reverify_inactive_fails_closed" fail \
  "değer='inactive' ('active' bekleniyor)" \
  "${SCRIPT_DIR}/check_compliance.yml" \
  -e status_fixture_path="${FIXTURES_DIR}/status_verbose_inactive.txt" \
  -e default_ufw_fixture_path="${FIXTURES_DIR}/default_ufw_compliant.txt"

run_case "reverify_logging_off_fails_closed" fail \
  "NON-COMPLIANT (reverify): 'Logging:' satırı" \
  "${SCRIPT_DIR}/check_compliance.yml" \
  -e status_fixture_path="${FIXTURES_DIR}/status_verbose_logging_off.txt" \
  -e default_ufw_fixture_path="${FIXTURES_DIR}/default_ufw_compliant.txt"

run_case "reverify_no_ssh_rule_fails_closed" fail \
  'kuralı bulunamadı (0 satır eşleşti)' \
  "${SCRIPT_DIR}/check_compliance.yml" \
  -e status_fixture_path="${FIXTURES_DIR}/status_verbose_no_ssh_rule.txt" \
  -e default_ufw_fixture_path="${FIXTURES_DIR}/default_ufw_compliant.txt"

run_case "reverify_input_policy_wrong_fails_closed" fail \
  "DEFAULT_INPUT_POLICY (görülen=1x, değer='ACCEPT'" \
  "${SCRIPT_DIR}/check_compliance.yml" \
  -e status_fixture_path="${FIXTURES_DIR}/status_verbose_compliant.txt" \
  -e default_ufw_fixture_path="${FIXTURES_DIR}/default_ufw_input_wrong.txt"

run_case "reverify_forward_policy_wrong_fails_closed" fail \
  "DEFAULT_FORWARD_POLICY (görülen=1x, değer='ACCEPT'" \
  "${SCRIPT_DIR}/check_compliance.yml" \
  -e status_fixture_path="${FIXTURES_DIR}/status_verbose_compliant.txt" \
  -e default_ufw_fixture_path="${FIXTURES_DIR}/default_ufw_forward_wrong.txt"

run_case "reverify_nonstandard_port_matching_rule_passes" pass "" \
  "${SCRIPT_DIR}/check_compliance.yml" \
  -e status_fixture_path="${FIXTURES_DIR}/status_verbose_nonstandard_port_compliant.txt" \
  -e default_ufw_fixture_path="${FIXTURES_DIR}/default_ufw_compliant.txt" \
  -e port=2222

run_case "reverify_nonstandard_port_without_matching_rule_fails_closed" fail \
  'kuralı bulunamadı (0 satır eşleşti)' \
  "${SCRIPT_DIR}/check_compliance.yml" \
  -e status_fixture_path="${FIXTURES_DIR}/status_verbose_compliant.txt" \
  -e default_ufw_fixture_path="${FIXTURES_DIR}/default_ufw_compliant.txt" \
  -e port=2222

# Birden fazla uygunsuzlukta TÜM kontrollerin çalıştığını doğrula (ilk
# hatada durmamalı): bu senaryoda UFW active (geçer) ama logging kapalı,
# 3 default policy'nin tümü uygunsuz ve SSH allow kuralı yok -- hepsi AYNI
# ANDA raporlanmalı.
multi_output=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_compliance.yml" \
  -e status_fixture_path="${FIXTURES_DIR}/status_verbose_multiple_wrong.txt" \
  -e default_ufw_fixture_path="${FIXTURES_DIR}/default_ufw_multiple_wrong.txt" 2>&1)
multi_rc=$?
multi_ok=1
[[ "${multi_rc}" -ne 0 ]] || multi_ok=0
for needle in \
  "COMPLIANT (reverify): UFW active." \
  "DEFAULT_INPUT_POLICY (görülen=1x, değer='ACCEPT'" \
  "DEFAULT_OUTPUT_POLICY (görülen=1x, değer='DROP'" \
  "DEFAULT_FORWARD_POLICY (görülen=1x, değer='ACCEPT'" \
  "NON-COMPLIANT (reverify): 'Logging:' satırı" \
  'kuralı bulunamadı (0 satır eşleşti)'
do
  grep -qF -- "${needle}" <<<"${multi_output}" || multi_ok=0
done
assert_no_template_errors "${multi_output}" "reverify_multiple_non_compliant_evaluates_all_checks" || multi_ok=0
TOTAL=$((TOTAL + 1))
if [[ "${multi_ok}" -eq 1 ]]; then
  echo "PASS  reverify_multiple_non_compliant_evaluates_all_checks"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  reverify_multiple_non_compliant_evaluates_all_checks (rc=${multi_rc})"
  echo "----- output -----"
  echo "${multi_output}"
  echo "-------------------"
fi

# --- 10. post_verify include-gate (meta: reset_connection check mode'a
#         sızmaz) ---
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

# --- 10b. BULGU1 (AUDIT-FIX1): normal modda AMA ufw_hardening_any_change
#          false ise (idempotent ikinci çalıştırma -- hiçbir yazma komutu
#          çalışmadı) reset_connection'a YİNE ulaşılmamalı ---
pv_out=$(ansible-playbook -i localhost, "${SCRIPT_DIR}/check_post_verify_gate.yml" \
  -e '{"fake_any_change": false}' 2>&1)
pv_rc=$?
pv_ok=1
[[ "${pv_rc}" -eq 0 ]] || pv_ok=0
grep -qF "Bağlantıyı sıfırla" <<<"${pv_out}" && pv_ok=0
grep -qiF "does not support when conditional" <<<"${pv_out}" && pv_ok=0
assert_no_template_errors "${pv_out}" "post_verify_gate_no_change_never_includes_reset_connection" || pv_ok=0
TOTAL=$((TOTAL + 1))
if [[ "${pv_ok}" -eq 1 ]]; then
  echo "PASS  post_verify_gate_no_change_never_includes_reset_connection"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  post_verify_gate_no_change_never_includes_reset_connection (rc=${pv_rc})"
  echo "----- output -----"
  echo "${pv_out}"
  echo "-------------------"
fi

# --- 11. Yapısal kilit: izin verilen komut yüzeyi + yazma sırası ---
struct_out=$(python3 "${SCRIPT_DIR}/assert_command_surface_and_order.py" 2>&1)
struct_rc=$?
TOTAL=$((TOTAL + 1))
if [[ "${struct_rc}" -eq 0 ]]; then
  echo "PASS  command_surface_allowlisted_and_ssh_allow_precedes_enable"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  command_surface_allowlisted_and_ssh_allow_precedes_enable"
  echo "${struct_out}"
fi

selftest_out=$(python3 "${SCRIPT_DIR}/assert_command_surface_and_order.py" --self-test 2>&1)
selftest_rc=$?
TOTAL=$((TOTAL + 1))
if [[ "${selftest_rc}" -eq 0 ]]; then
  echo "PASS  command_surface_allowlist_logic_is_hermetically_regression_tested"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  command_surface_allowlist_logic_is_hermetically_regression_tested"
  echo "${selftest_out}"
fi

# --- 12. Yapısal kilit: paylaşılan gate dosyaları tek kaynaklı mı ---
shared_out=$(python3 "${SCRIPT_DIR}/assert_shared_gate_imports.py" 2>&1)
shared_rc=$?
TOTAL=$((TOTAL + 1))
if [[ "${shared_rc}" -eq 0 ]]; then
  echo "PASS  shared_gate_assert_files_are_single_sourced"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  shared_gate_assert_files_are_single_sourced"
  echo "${shared_out}"
fi

# --- 12b. Yapısal kilit: play sözleşmesi (serial:1 + any_errors_fatal:true
#          + become/become_method/become_user/become_flags -- BULGU3) ---
play_out=$(python3 "${SCRIPT_DIR}/assert_play_contract.py" 2>&1)
play_rc=$?
TOTAL=$((TOTAL + 1))
if [[ "${play_rc}" -eq 0 ]]; then
  echo "PASS  play_contract_serial_and_any_errors_fatal_pinned"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  play_contract_serial_and_any_errors_fatal_pinned"
  echo "${play_out}"
fi

play_selftest_out=$(python3 "${SCRIPT_DIR}/assert_play_contract.py" --self-test 2>&1)
play_selftest_rc=$?
TOTAL=$((TOTAL + 1))
if [[ "${play_selftest_rc}" -eq 0 ]]; then
  echo "PASS  play_contract_check_logic_is_hermetically_regression_tested"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  play_contract_check_logic_is_hermetically_regression_tested"
  echo "${play_selftest_out}"
fi

# --- 13. ansible-lint (varsa) ---
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
