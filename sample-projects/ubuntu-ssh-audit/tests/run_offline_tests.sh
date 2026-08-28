#!/usr/bin/env bash
# tests/run_offline_tests.sh
#
# Kalıcı offline regresyon testleri: roles/ssh_audit/tasks/checks.yml
# mantığını sahte (fixture) sshd -T çıktılarıyla çalıştırır. Gerçek bir
# SSH hostu, bağlantı veya sudo gerektirmez. `ansible-playbook`'un çıkış
# kodu (0 = tüm kontroller compliant/summary başarılı, 0-dışı = en az bir
# kontrol non-compliant veya hata) her senaryonun beklentisiyle
# karşılaştırılır; ayrıca fail_msg/success_msg metninde doğru kontrolün
# tetiklendiği grep ile doğrulanır.
#
# Kullanım: ./tests/run_offline_tests.sh  (repo kökünden veya bu dizinden)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES_DIR="${SCRIPT_DIR}/fixtures"
PLAYBOOK="${SCRIPT_DIR}/run_checks.yml"

TOTAL=0
FAILED=0

# Jinja/Ansible şablon hatalarının imzaları. Hiçbir senaryo (compliant,
# non-compliant veya malformed girdi) bunları üretmemeli: kontrollü bir
# assert fail_msg'i her zaman mümkündür, bir şablon istisnası asla değil.
UNDEFINED_ERROR_PATTERNS=(
  "undefined variable"
  "has no element"
  "AnsibleUndefinedVariable"
  "list object has no"
  "string object has no"
)

# assert_no_template_errors OUTPUT NAME -> 0 ise temiz, 1 ise en az bir
# şablon hatası imzası bulundu (ve bulunanı stderr'e yazar).
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

# run_case NAME FIXTURE_FILE EXPECTED[pass|fail] GREP_EXPECT [EXTRA_ANSIBLE_ARGS...]
run_case() {
  local name="$1" fixture="$2" expected="$3" grep_expect="$4"
  shift 4
  local output rc
  TOTAL=$((TOTAL + 1))

  output=$(ansible-playbook -i localhost, "${PLAYBOOK}" \
    -e "fixture_path=${FIXTURES_DIR}/${fixture}" "$@" 2>&1)
  rc=$?

  local rc_ok=0
  if [[ "${expected}" == "pass" && "${rc}" -eq 0 ]]; then
    rc_ok=1
  elif [[ "${expected}" == "fail" && "${rc}" -ne 0 ]]; then
    rc_ok=1
  fi

  local grep_ok=0
  if grep -qF -- "${grep_expect}" <<<"${output}"; then
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

run_case "fully_compliant" \
  "fully_compliant.txt" pass \
  "COMPLIANT (yalnızca global/default sshd baseline): tüm kontroller beklenen profille uyumlu."

# 0 satırlı varsayılan durum: AllowUsers/AllowGroups/DenyUsers/DenyGroups
# sshd -T çıktısında hiç görünmüyor (gerçek sshd -T davranışı, boş liste
# ayarlanmamış directive için hiçbir satır üretmez). Bu geçerli "liste
# boş" durumudur; fail-closed hata DEĞİLDİR ve varsayılan profil de boş
# beklediği için yalnız raporlanır.
run_case "user_group_restrictions_zero_lines_is_valid_empty_list" \
  "fully_compliant.txt" pass \
  "RAPOR: AllowUsers=[] AllowGroups=[] DenyUsers=[] DenyGroups=[]."

run_case "missing_required_field_fails_closed" \
  "missing_required_field.txt" fail \
  "kbdinteractiveauthentication sshd -T çıktısında 0 kez görüldü"

run_case "duplicate_field_fails_closed" \
  "duplicate_field.txt" fail \
  "permitrootlogin sshd -T çıktısında 2 kez görüldü"

run_case "login_grace_time_zero_is_unlimited_and_fails" \
  "login_grace_time_zero.txt" fail \
  "üst sınır=60s (0 sınırsızdır ve kabul edilmez)"

run_case "login_grace_time_unit_converted_and_compliant" \
  "login_grace_time_unit.txt" pass \
  "COMPLIANT: LoginGraceTime=1m (~60s)."

run_case "login_grace_time_malformed_fails_closed" \
  "login_grace_time_malformed.txt" fail \
  "hesaplanan=ayrıştırılamadı"

run_case "allow_tcp_forwarding_local_non_compliant_under_default_profile" \
  "allow_tcp_forwarding_local.txt" fail \
  "allowtcpforwarding sshd -T çıktısında 1 kez görüldü (1 bekleniyor), değer='local'"

run_case "allow_tcp_forwarding_remote_non_compliant_under_default_profile" \
  "allow_tcp_forwarding_remote.txt" fail \
  "allowtcpforwarding sshd -T çıktısında 1 kez görüldü (1 bekleniyor), değer='remote'"

run_case "allow_tcp_forwarding_local_compliant_under_bastion_profile_override" \
  "allow_tcp_forwarding_local.txt" pass \
  "COMPLIANT: AllowTcpForwarding=local." \
  -e '{"ssh_audit_tcp_forwarding_allowed_values": ["no", "local"]}'

# Birden fazla AllowUsers/AllowGroups satırı (OpenSSH'ın gerçek biçimi:
# her değer kendi satırında) duplicate sayılmamalı; toplanan liste
# beklenen profille sort edilerek karşılaştırılmalı.
run_case "user_group_restrictions_multi_line_collected_and_matches_profile" \
  "user_group_restrictions_multi_line_matches_profile.txt" pass \
  "RAPOR: AllowUsers=['alice', 'bob'] AllowGroups=['ops', 'sre'] DenyUsers=[] DenyGroups=[]." \
  -e '{"ssh_audit_expected_allow_users": ["bob", "alice"], "ssh_audit_expected_allow_groups": ["sre", "ops"]}'

# Aynı fixture, beklenen profil verilmeden: yalnızca raporlama, actual
# liste dolu olsa bile compliant (erişim kısıtlaması isteğe bağlıdır).
run_case "user_group_restrictions_multi_line_reported_without_profile" \
  "user_group_restrictions_multi_line_matches_profile.txt" pass \
  "RAPOR: AllowUsers=['alice', 'bob'] AllowGroups=['ops', 'sre'] DenyUsers=[] DenyGroups=[]."

# Keyword var, değer yok (sshd -T'nin asla üretmediği bozuk biçim) ->
# fail-closed. 0 satır (yok) ile karıştırılmamalı.
run_case "user_group_restrictions_malformed_line_fails_closed" \
  "user_group_restrictions_malformed_line.txt" fail \
  "bozuk (değersiz) satır sayıları (allowusers=0, allowgroups=0, denyusers=1, denygroups=0, her biri için 0 bekleniyor)"

# Birden fazla uygunsuzlukta tüm kontrollerin çalıştığını doğrula: bu
# senaryoda 6 kontrol non-compliant, 5 kontrol compliant (kullanıcı/grup
# kontrolü dahil) olmalı ve hepsi çalışmış olmalı (ilk uygunsuzlukta
# durmamalı).
TOTAL=$((TOTAL + 1))
multi_output=$(ansible-playbook -i localhost, "${PLAYBOOK}" \
  -e "fixture_path=${FIXTURES_DIR}/multiple_non_compliant.txt" 2>&1)
multi_rc=$?
multi_ok=1
[[ "${multi_rc}" -ne 0 ]] || multi_ok=0
for needle in \
  "NON-COMPLIANT/FAIL-CLOSED: permitrootlogin" \
  "NON-COMPLIANT/FAIL-CLOSED: passwordauthentication" \
  "NON-COMPLIANT/FAIL-CLOSED: permitemptypasswords" \
  "NON-COMPLIANT/FAIL-CLOSED: x11forwarding" \
  "NON-COMPLIANT/FAIL-CLOSED: maxauthtries" \
  "NON-COMPLIANT/FAIL-CLOSED: logingracetime" \
  "COMPLIANT: PubkeyAuthentication=yes." \
  "COMPLIANT: KbdInteractiveAuthentication=no." \
  "COMPLIANT: AllowAgentForwarding=no." \
  "COMPLIANT: AllowTcpForwarding=no." \
  "RAPOR: AllowUsers=[] AllowGroups=[] DenyUsers=[] DenyGroups=[]." \
  "NON-COMPLIANT: bir veya daha fazla SSH kontrolü uygunsuz."
do
  grep -qF -- "${needle}" <<<"${multi_output}" || multi_ok=0
done
assert_no_template_errors "${multi_output}" "multiple_non_compliant_runs_all_11_checks" || multi_ok=0
if [[ "${multi_ok}" -eq 1 ]]; then
  echo "PASS  multiple_non_compliant_runs_all_11_checks"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  multiple_non_compliant_runs_all_11_checks (rc=${multi_rc})"
  echo "----- output -----"
  echo "${multi_output}"
  echo "-------------------"
fi

echo
echo "${TOTAL} test, $((TOTAL - FAILED)) geçti, ${FAILED} başarısız."
[[ "${FAILED}" -eq 0 ]]
