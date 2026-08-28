#!/usr/bin/env bash
# EPIC 0 kabul kontrollerini çalıştırır (Linux / macOS).
#
# Kullanım:
#   ./scripts/verify.sh
#   SKIP_FRONTEND=1 ./scripts/verify.sh
#
# Backend sanal ortamının aktif olduğu varsayılır.

set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backend="${repo_root}/backend"
frontend="${repo_root}/frontend"
failures=()

run_step() {
  local name="$1"
  local workdir="$2"
  shift 2

  echo "==> ${name}"
  if ! (cd "${workdir}" && "$@"); then
    failures+=("${name}")
    echo "    BAŞARISIZ (${name})" >&2
  fi
}

run_step 'backend lint (ruff check)' "${backend}" ruff check .
run_step 'backend type check (mypy)' "${backend}" mypy
run_step 'backend test (pytest)' "${backend}" pytest
run_step 'backend migration (alembic upgrade head)' "${backend}" alembic upgrade head

if [[ "${SKIP_FRONTEND:-0}" != "1" ]]; then
  run_step 'frontend type check (tsc)' "${frontend}" npm run typecheck
  run_step 'frontend test (vitest)' "${frontend}" npm test
  run_step 'frontend build (vite)' "${frontend}" npm run build
fi

# Güvenlik gate'inin kendi regresyon testleri. Ağ gerektirmez (tek istisna,
# kapalı bir porta yapılan yerel "erişilemeyen registry" denemesidir).
run_step 'security gate test (node --test)' "${repo_root}" node --test 'scripts/tests/*.test.mjs'

if ((${#failures[@]} > 0)); then
  echo
  echo "Başarısız adımlar: ${failures[*]}" >&2
  exit 1
fi

echo
echo 'Bütün kontroller geçti.'
