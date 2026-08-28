#!/usr/bin/env bash
# Backend ve frontend dependency vulnerability audit'i (Linux / macOS).
#
#   Backend  : pip-audit, backend/requirements.lock.txt üzerinde
#   Frontend : npm audit, frontend/package-lock.json üzerinde
#
# Frontend değerlendirmesi scripts/npm-audit-gate.mjs tarafından yapılır;
# aynı gate scripts/security-audit.ps1 tarafından da kullanılır ve
# scripts/tests/npm-audit-gate.test.mjs ile otomatik test edilir.
#
# FAIL-CLOSED: ağ/TLS/registry hatası, ayrıştırılamayan JSON veya beklenen
# şemanın karşılanmaması durumunda komut BAŞARISIZ olur. npm'in zafiyet
# nedeniyle exit 1 vermesi ile altyapı hatası, npm'in çıkış koduna değil
# çıktının JSON şemasına bakılarak ayrılır.
#
# Kullanım:
#   ./scripts/security-audit.sh
#   SKIP_FRONTEND=1 ./scripts/security-audit.sh
#   SKIP_BACKEND=1 ./scripts/security-audit.sh

set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
backend="${repo_root}/backend"
frontend="${repo_root}/frontend"
allow_file="${script_dir}/accepted-vulnerabilities.json"
gate_file="${script_dir}/npm-audit-gate.mjs"
failures=()

# ---------------------------------------------------------------------------
# Backend: pip-audit
# ---------------------------------------------------------------------------
if [[ "${SKIP_BACKEND:-0}" != "1" ]]; then
  echo
  echo '==> backend audit (pip-audit)'

  pip_audit=''
  if [[ -x "${backend}/.venv/bin/pip-audit" ]]; then
    pip_audit="${backend}/.venv/bin/pip-audit"
  elif command -v pip-audit >/dev/null 2>&1; then
    pip_audit="$(command -v pip-audit)"
  fi

  if [[ -z "${pip_audit}" ]]; then
    echo '    AUDIT ALTYAPI HATASI: pip-audit bulunamadı.' >&2
    echo '      cd backend && pip install -e ".[audit]"' >&2
    failures+=('backend audit (pip-audit kurulu değil)')
  else
    ignore_args=()
    while IFS= read -r vuln_id; do
      [[ -z "${vuln_id}" ]] && continue
      ignore_args+=(--ignore-vuln "${vuln_id}")
      echo "    kabul edilmiş: ${vuln_id}"
    done < <(node -e '
      const a = require(process.argv[1]);
      for (const e of a.pypi ?? []) console.log(e.id);
    ' "${allow_file}")

    if ! (cd "${backend}" && "${pip_audit}" -r requirements.lock.txt --strict \
          --progress-spinner off "${ignore_args[@]}"); then
      failures+=('backend audit (pip-audit başarısız veya bulgu var)')
      echo '    BAŞARISIZ: pip-audit sıfırdan farklı çıkış kodu döndürdü.' >&2
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Frontend: npm audit -> npm-audit-gate.mjs
# ---------------------------------------------------------------------------
if [[ "${SKIP_FRONTEND:-0}" != "1" ]]; then
  echo
  echo '==> frontend audit (npm audit)'

  if [[ ! -f "${gate_file}" ]]; then
    echo "    AUDIT ALTYAPI HATASI: gate bulunamadı (${gate_file})" >&2
    failures+=('frontend audit (gate dosyası yok)')
  else
    # npm'in çıkış kodu bilerek yok sayılır; karar JSON şemasına göre
    # gate tarafından verilir.
    audit_json="$(cd "${frontend}" && npm audit --json 2>/dev/null || true)"

    printf '%s' "${audit_json}" | node "${gate_file}" \
      --allowlist "${allow_file}" --frontend "${frontend}"
    gate_exit=$?

    case "${gate_exit}" in
      0) ;;
      1) failures+=('frontend audit (kabul edilmemiş bulgu veya guard ihlali)') ;;
      *) failures+=("frontend audit (ALTYAPI HATASI, gate exit ${gate_exit})") ;;
    esac
  fi
fi

echo
if ((${#failures[@]} > 0)); then
  echo "Güvenlik audit BAŞARISIZ: ${failures[*]}" >&2
  exit 1
fi

echo 'Güvenlik audit geçti.'
