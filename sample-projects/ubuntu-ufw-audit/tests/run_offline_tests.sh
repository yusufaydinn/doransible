#!/usr/bin/env bash
# tests/run_offline_tests.sh
#
# Kalıcı offline regresyon testleri: roles/ufw_audit/tasks/checks.yml
# mantığını sahte (fixture) `ufw status verbose` ve `/etc/default/ufw`
# içerikleriyle çalıştırır. Gerçek bir Ubuntu hostu, bağlantı veya sudo
# gerektirmez. `ansible-playbook`'un çıkış kodu (0 = tüm kontroller
# compliant/summary başarılı, 0-dışı = en az bir kontrol non-compliant
# veya hata) her senaryonun beklentisiyle karşılaştırılır; ayrıca fail_msg/
# success_msg metninde doğru kontrolün tetiklendiği grep ile doğrulanır.
#
# Kullanım: ./tests/run_offline_tests.sh  (repo kökünden veya bu dizinden)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES_DIR="${SCRIPT_DIR}/fixtures"
PLAYBOOK="${SCRIPT_DIR}/run_checks.yml"
COMPLIANT_STATUS="${FIXTURES_DIR}/status_compliant.txt"
COMPLIANT_DEFAULT_UFW="${FIXTURES_DIR}/default_ufw_compliant.txt"

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
  "Unable to convert"
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

# run_case_json_extra NAME STATUS_FIXTURE DEFAULT_UFW_FIXTURE EXPECTED[pass|fail] GREP_EXPECT JSON_EXTRA_VARS
#
# run_case ile aynıdır, ama tek bir ek `-e '<json>'` argümanı alır. Bir
# boolean/typed extra-var'ı `-e key=false` biçiminde geçmek Ansible'da
# STRING "false" üretir (Python'da truthy); gerçek boolean üretmek için
# JSON extra-vars biçimi ("-e '{\"key\": false}'") zorunludur.
run_case_json_extra() {
  local name="$1" status_fixture="$2" default_ufw_fixture="$3" expected="$4" grep_expect="$5" json_extra="$6"
  run_case "${name}" "${status_fixture}" "${default_ufw_fixture}" "${expected}" "${grep_expect}" \
    -e "${json_extra}"
}

# run_case NAME STATUS_FIXTURE DEFAULT_UFW_FIXTURE EXPECTED[pass|fail] GREP_EXPECT [EXTRA_ANSIBLE_ARGS...]
run_case() {
  local name="$1" status_fixture="$2" default_ufw_fixture="$3" expected="$4" grep_expect="$5"
  shift 5
  local output rc
  TOTAL=$((TOTAL + 1))

  output=$(ansible-playbook -i localhost, "${PLAYBOOK}" \
    -e "status_fixture_path=${FIXTURES_DIR}/${status_fixture}" \
    -e "default_ufw_fixture_path=${FIXTURES_DIR}/${default_ufw_fixture}" \
    "$@" 2>&1)
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
  "status_compliant.txt" "default_ufw_compliant.txt" pass \
  "COMPLIANT: tüm UFW kontrolleri"

# Boolean girdiler -e key=false (string "false") DEĞİL, JSON extra-vars
# (-e '{"key": false}') ile geçirilir -- aksi halde Ansible bunu gerçek
# boolean yerine string olarak şablonlar ve assert koşulu role mantığına
# ulaşmadan "Conditionals must have a boolean result" ile reddeder (bkz.
# run_case_json_extra). "missing" (exists=false) ve "not executable"
# (executable=false, exists=true) durumları ayrı ayrı, birbirinden
# bağımsız NON-COMPLIANT sonucuna ulaştığını kanıtlamak için AYRI test
# edilir.
run_case_json_extra "ufw_binary_missing_fails_closed" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "NON-COMPLIANT/FAIL-CLOSED: /usr/sbin/ufw bulunamadı veya çalıştırılabilir değil (exists=False, executable=True)" \
  '{"ufw_audit_binary_exists": false}'

run_case_json_extra "ufw_binary_not_executable_fails_closed" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "NON-COMPLIANT/FAIL-CLOSED: /usr/sbin/ufw bulunamadı veya çalıştırılabilir değil (exists=True, executable=False)" \
  '{"ufw_audit_binary_executable": false}'

run_case "ufw_status_verbose_nonzero_rc_fails_closed" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "ufw status verbose rc=1" \
  -e "ufw_audit_status_rc=1"

# ufw.service AKTİF olsa bile gerçek karar yalnız `ufw status verbose`
# çıktısına dayanır: bu senaryoda Status: inactive olduğu için sonuç
# NON-COMPLIANT olmalı, ufw.service'in "active" olması hiçbir işe
# yaramamalı (yalnız bilgi amaçlı debug satırında görünür).
run_case "ufw_service_active_but_real_status_inactive_is_non_compliant" \
  "status_inactive.txt" "default_ufw_compliant.txt" fail \
  "değer='inactive' ('active' bekleniyor)" \
  -e "ufw_audit_service_status_stdout=active"

run_case "duplicate_status_line_fails_closed" \
  "status_duplicate_status_line.txt" "default_ufw_compliant.txt" fail \
  "'Status:' satırı 2 kez görüldü"

# --- Firewalld karar matrisi: rc + stdout birlikte değerlendirilir -------
# Kabul edilen TEK güvenli kombinasyonlar: rc=3/stdout=inactive (kurulu,
# çalışmıyor) veya rc=4/stdout=inactive|unknown (unit hiç bulunamadı).
# Bunların dışındaki HER kombinasyon -- rc ne olursa olsun stdout=active,
# command hatası, rc/stdout çelişkisi, malformed/boş stdout -- fail-closed
# NON-COMPLIANT'tır. Detaylı matris için README.md.

run_case "firewalld_active_is_conflict_fails_closed" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "NON-COMPLIANT/FAIL-CLOSED: firewalld aktif" \
  -e "ufw_audit_firewalld_status_stdout=active" \
  -e "ufw_audit_firewalld_status_rc=0"

run_case "firewalld_inactive_rc3_is_compliant" \
  "status_compliant.txt" "default_ufw_compliant.txt" pass \
  "COMPLIANT: firewalld güvenle inactive/kurulu-değil" \
  -e "ufw_audit_firewalld_status_stdout=inactive" \
  -e "ufw_audit_firewalld_status_rc=3"

run_case "firewalld_unit_not_found_rc4_unknown_is_compliant" \
  "status_compliant.txt" "default_ufw_compliant.txt" pass \
  "COMPLIANT: firewalld güvenle inactive/kurulu-değil" \
  -e "ufw_audit_firewalld_status_stdout=unknown" \
  -e "ufw_audit_firewalld_status_rc=4"

run_case "firewalld_unit_not_found_rc4_inactive_variant_is_compliant" \
  "status_compliant.txt" "default_ufw_compliant.txt" pass \
  "COMPLIANT: firewalld güvenle inactive/kurulu-değil" \
  -e "ufw_audit_firewalld_status_stdout=inactive" \
  -e "ufw_audit_firewalld_status_rc=4"

run_case "firewalld_command_error_fails_closed" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "firewalld durumu güvenle inactive/kurulu-değil olarak doğrulanamadı" \
  -e "ufw_audit_firewalld_status_stdout=" \
  -e "ufw_audit_firewalld_status_rc=1"

run_case "firewalld_rc_stdout_mismatch_fails_closed" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "firewalld durumu güvenle inactive/kurulu-değil olarak doğrulanamadı" \
  -e "ufw_audit_firewalld_status_stdout=inactive" \
  -e "ufw_audit_firewalld_status_rc=0"

run_case "firewalld_malformed_stdout_fails_closed" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "firewalld durumu güvenle inactive/kurulu-değil olarak doğrulanamadı" \
  -e "ufw_audit_firewalld_status_stdout=reloading" \
  -e "ufw_audit_firewalld_status_rc=3"

run_case "ipv6_off_fails_closed" \
  "status_compliant.txt" "default_ufw_ipv6_off.txt" fail \
  "IPV6 /etc/default/ufw içinde 1 kez görüldü (1 bekleniyor), değer='no'"

run_case "default_ufw_missing_field_fails_closed" \
  "status_compliant.txt" "default_ufw_missing_field.txt" fail \
  "DEFAULT_FORWARD_POLICY /etc/default/ufw içinde 0 kez görüldü"

run_case "default_ufw_duplicate_field_fails_closed" \
  "status_compliant.txt" "default_ufw_duplicate_field.txt" fail \
  "DEFAULT_INPUT_POLICY /etc/default/ufw içinde 2 kez görüldü"

run_case "default_ufw_malformed_value_fails_closed" \
  "status_compliant.txt" "default_ufw_malformed.txt" fail \
  "NON-COMPLIANT/FAIL-CLOSED: DEFAULT_INPUT_POLICY /etc/default/ufw içinde 1 kez görüldü (1 bekleniyor)"

run_case "default_input_policy_wrong_fails_closed" \
  "status_compliant.txt" "default_ufw_input_policy_wrong.txt" fail \
  "DEFAULT_INPUT_POLICY /etc/default/ufw içinde 1 kez görüldü (1 bekleniyor), değer='ACCEPT', beklenen='DROP'"

run_case "default_output_policy_wrong_fails_closed" \
  "status_compliant.txt" "default_ufw_output_policy_wrong.txt" fail \
  "DEFAULT_OUTPUT_POLICY /etc/default/ufw içinde 1 kez görüldü (1 bekleniyor), değer='DROP', beklenen='ACCEPT'"

run_case "default_forward_policy_wrong_fails_closed" \
  "status_compliant.txt" "default_ufw_forward_policy_wrong.txt" fail \
  "DEFAULT_FORWARD_POLICY /etc/default/ufw içinde 1 kez görüldü (1 bekleniyor), değer='ACCEPT', beklenen='DROP'"

run_case "logging_off_fails_closed" \
  "status_logging_off.txt" "default_ufw_compliant.txt" fail \
  "satır='Logging: off'"

run_case "logging_malformed_missing_level_fails_closed" \
  "status_logging_malformed.txt" "default_ufw_compliant.txt" fail \
  "satır='Logging: on'"

run_case "logging_medium_level_is_compliant" \
  "status_logging_medium.txt" "default_ufw_compliant.txt" pass \
  "COMPLIANT: Logging: on (medium)."

run_case "ssh_allow_rule_missing_fails_closed" \
  "status_no_ssh_rule.txt" "default_ufw_compliant.txt" fail \
  "SSH portu 22/tcp için"

# Uygulama profili tabanlı kural ("OpenSSH ALLOW IN Anywhere") belirsizdir:
# hangi port(lar)ı kapsadığı bu çıktıdan doğrudan anlaşılamaz. Parser bunu
# KASITLI olarak eşleştirmez -- compliant SAYMAZ.
run_case "ssh_allow_rule_app_profile_is_ambiguous_and_not_accepted" \
  "status_ssh_rule_ambiguous_app_profile.txt" "default_ufw_compliant.txt" fail \
  "SSH portu 22/tcp için"

run_case "ssh_allow_rule_udp_only_does_not_satisfy_tcp_requirement" \
  "status_ssh_rule_udp_only.txt" "default_ufw_compliant.txt" fail \
  "SSH portu 22/tcp için"

run_case "ssh_allow_rule_limit_only_is_not_allow" \
  "status_ssh_rule_limit_only.txt" "default_ufw_compliant.txt" fail \
  "SSH portu 22/tcp için"

run_case "ssh_allow_rule_port_range_is_not_supported" \
  "status_ssh_rule_port_range_only.txt" "default_ufw_compliant.txt" fail \
  "SSH portu 22/tcp için"

run_case "nonstandard_ansible_port_with_matching_rule_is_compliant" \
  "status_ssh_rule_nonstandard_port.txt" "default_ufw_compliant.txt" pass \
  "COMPLIANT: SSH portu 2222/tcp için" \
  -e "ansible_port=2222"

run_case "nonstandard_ansible_port_without_matching_rule_fails_closed" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "SSH portu 2222/tcp için" \
  -e "ansible_port=2222"

run_case "ssh_port_out_of_range_fails_closed" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "ansible_port='70000' geçerli bir 1..65535 port numarası değil" \
  -e "ansible_port=70000"

# Role'ün kendi 1..65535 sınır kontrolünün erişilebilir sınırlarda
# davranışsal olarak çalıştığını kanıtla: 0 ve 65536 Ansible'ın
# `ansible_port` bağlantı anahtarı için `int()` dönüşümünü GEÇER (ikisi de
# geçerli tamsayıdır), bu yüzden gerçek task'lara ulaşır ve role'ün kendi
# 1..65535 aralık assert'i tarafından NON-COMPLIANT sayılır -- bkz. altındaki
# "ansible_port_non_numeric" testi, ki o Ansible'ın bağlantı katmanında
# role'e hiç ulaşmadan reddedilir.
run_case "ssh_port_zero_is_out_of_range_fails_closed" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "ansible_port='0' geçerli bir 1..65535 port numarası değil" \
  -e "ansible_port=0"

run_case "ssh_port_65536_is_out_of_range_fails_closed" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "ansible_port='65536' geçerli bir 1..65535 port numarası değil" \
  -e "ansible_port=65536"

# DÜRÜST MODELLEME: `ansible_port=notanumber` role'ün KENDİ port
# kontrolüne hiçbir zaman ulaşmaz. Ansible, `ansible_port`'u ÖZEL bir
# bağlantı anahtarı olarak tanır ve play'in İLK task'ı çalışmadan ÖNCE,
# tüm audit mantığından bağımsız olarak, kendi `int()` dönüşümünü dener;
# dönüşüm başarısız olursa play fail-closed biçimde hemen durur (rc≠0).
# Bu test role'ün mesajını DEĞİL, Ansible'ın kendi ret mesajını arar --
# gizleme veya role'e yeni bir public override değişkeni ekleme YOKTUR;
# bkz. README.md "Non-numeric SSH portu".
run_case "ansible_port_non_numeric_is_rejected_by_ansible_connection_layer_before_any_audit_task_runs" \
  "status_compliant.txt" "default_ufw_compliant.txt" fail \
  "The value 'notanumber' could not be converted to 'int'" \
  -e "ansible_port=notanumber"

# Birden fazla uygunsuzlukta tüm kontrollerin çalıştığını doğrula: bu
# senaryoda ufw active ama logging kapalı, default policy'lerin tümü
# uygunsuz, SSH allow kuralı yok ve firewalld aktif -- hepsi AYNI ANDA
# non-compliant olmalı ve hepsi çalışmış olmalı (ilk uygunsuzlukta
# durmamalı). Yalnız binary ve ssh-port-geçerliliği kontrolleri bu
# senaryoda compliant kalır (kasıtlı: hangi kontrollerin bağımsız
# değerlendirildiğini göstermek için).
TOTAL=$((TOTAL + 1))
multi_output=$(ansible-playbook -i localhost, "${PLAYBOOK}" \
  -e "status_fixture_path=${FIXTURES_DIR}/status_multiple_non_compliant.txt" \
  -e "default_ufw_fixture_path=${FIXTURES_DIR}/default_ufw_multiple_non_compliant.txt" \
  -e "ufw_audit_firewalld_status_stdout=active" 2>&1)
multi_rc=$?
multi_ok=1
[[ "${multi_rc}" -ne 0 ]] || multi_ok=0
for needle in \
  "NON-COMPLIANT/FAIL-CLOSED: firewalld aktif" \
  "IPV6 /etc/default/ufw içinde 1 kez görüldü (1 bekleniyor), değer='no', beklenen='yes'" \
  "DEFAULT_INPUT_POLICY /etc/default/ufw içinde 1 kez görüldü (1 bekleniyor), değer='ACCEPT', beklenen='DROP'" \
  "DEFAULT_FORWARD_POLICY /etc/default/ufw içinde 1 kez görüldü (1 bekleniyor), değer='ACCEPT', beklenen='DROP'" \
  "COMPLIANT: DEFAULT_OUTPUT_POLICY=ACCEPT." \
  "satır='Logging: off'" \
  "SSH portu 22/tcp için" \
  "COMPLIANT: /usr/sbin/ufw mevcut ve çalıştırılabilir." \
  "COMPLIANT: SSH portu=22." \
  "NON-COMPLIANT: bir veya daha fazla UFW kontrolü uygunsuz."
do
  grep -qF -- "${needle}" <<<"${multi_output}" || multi_ok=0
done
assert_no_template_errors "${multi_output}" "multiple_non_compliant_runs_all_checks" || multi_ok=0
if [[ "${multi_ok}" -eq 1 ]]; then
  echo "PASS  multiple_non_compliant_runs_all_checks"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  multiple_non_compliant_runs_all_checks (rc=${multi_rc})"
  echo "----- output -----"
  echo "${multi_output}"
  echo "-------------------"
fi

# Salt-okunur yüzeyin yapısal kilidi: production role'un (roles/ufw_audit/)
# ve üst playbook'un (ubuntu-ufw-audit.yml) yalnızca beklenen salt-okunur
# modülleri/komutları taşıdığını PyYAML tabanlı bir AST incelemesiyle
# kanıtlar -- kırılgan kaynak-metin grep'i DEĞİLDİR (bkz.
# tests/assert_read_only_surface.py).
TOTAL=$((TOTAL + 1))
structure_output=$(python3 "${SCRIPT_DIR}/assert_read_only_surface.py" 2>&1)
structure_rc=$?
if [[ "${structure_rc}" -eq 0 ]]; then
  echo "PASS  read_only_surface_is_structurally_locked"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  read_only_surface_is_structurally_locked (rc=${structure_rc})"
  echo "----- output -----"
  echo "${structure_output}"
  echo "-------------------"
fi

# assert_read_only_surface.py'nin KENDİ argv/modül allowlist mantığının
# (yalnızca 2 izin verilen systemctl argv'si + 1 ufw argv'si; arbitrary
# unit/eksik-fazla argument/Jinja-dinamik değer/farklı alt komut/yasak
# modül reddi) kalıcılaştırılmış, hermetik regresyon kanıtı: checker'ın
# `--self-test` modu, kendi fonksiyonlarını bellek-içi sahte argv/task
# girdileriyle doğrudan çağırır -- tracked production dosyalarını
# değiştirmez, disk fixture'ı oluşturmaz.
TOTAL=$((TOTAL + 1))
argv_allowlist_output=$(python3 "${SCRIPT_DIR}/assert_read_only_surface.py" --self-test 2>&1)
argv_allowlist_rc=$?
if [[ "${argv_allowlist_rc}" -eq 0 ]]; then
  echo "PASS  read_only_surface_argv_allowlist_is_hermetically_regression_tested"
else
  FAILED=$((FAILED + 1))
  echo "FAIL  read_only_surface_argv_allowlist_is_hermetically_regression_tested (rc=${argv_allowlist_rc})"
  echo "----- output -----"
  echo "${argv_allowlist_output}"
  echo "-------------------"
fi

echo
echo "${TOTAL} test, $((TOTAL - FAILED)) geçti, ${FAILED} başarısız."
[[ "${FAILED}" -eq 0 ]]
