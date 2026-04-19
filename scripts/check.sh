#!/usr/bin/env bash
# scripts/check.sh — lokalna bramka jakości (odpowiednik CI)
# Uruchamiana ręcznie lub automatycznie przez .git/hooks/pre-push
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS="${GREEN}✓${NC}"; FAIL="${RED}✗${NC}"; SKIP="${YELLOW}~${NC}"

step() { echo -e "\n${YELLOW}▶ $*${NC}"; }
ok()   { echo -e "${PASS} $*"; }
fail() { echo -e "${FAIL} $*"; exit 1; }

# Aktywuj .venv jeśli istnieje i nie jesteśmy jeszcze w venv
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"

if [[ -z "${VIRTUAL_ENV:-}" && -f "$REPO_ROOT/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.venv/bin/activate"
fi

# Fallback: jeśli pytest nadal nie w PATH (np. git hook bez venv), użyj venv pythona
PYTEST_CMD="pytest"
PYRIGHT_CMD="pyright"
if ! command -v pytest >/dev/null 2>&1 && [[ -f "$VENV_PYTHON" ]]; then
  PYTEST_CMD="$VENV_PYTHON -m pytest"
fi
if ! command -v pyright >/dev/null 2>&1 && [[ -f "$REPO_ROOT/.venv/bin/pyright" ]]; then
  PYRIGHT_CMD="$REPO_ROOT/.venv/bin/pyright"
fi

step "Ruff lint"
if command -v ruff >/dev/null 2>&1; then
  ruff check . && ok "ruff check" || fail "ruff check failed"
  ruff format --check . && ok "ruff format" || fail "ruff format failed"
else
  echo -e "${SKIP} ruff not found — skipping lint"
fi

step "Pyright type check"
# Pyright jest wolny (cold start ~30s) — domyślnie pomijany w hooku.
# Włącz przez: CHECK_TYPECHECK=1 git push  lub  bash scripts/check.sh --typecheck
if [[ "${CHECK_TYPECHECK:-0}" == "1" || "${1:-}" == "--typecheck" ]]; then
  $PYRIGHT_CMD && ok "pyright" || fail "pyright failed"
else
  echo -e "${SKIP} pyright pominięty (użyj CHECK_TYPECHECK=1 aby włączyć)"
fi

step "pytest (cov ≥ 34%)"
$PYTEST_CMD tests/ -q --tb=short \
  --cov=zdrovena --cov-fail-under=34 \
  --cov-report=term-missing \
  && ok "tests passed" || fail "tests failed"

echo -e "\n${GREEN}All checks passed — safe to push.${NC}"
