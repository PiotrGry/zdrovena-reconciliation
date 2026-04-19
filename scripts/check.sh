#!/usr/bin/env bash
# scripts/check.sh — lokalna bramka jakości (odpowiednik CI)
# Uruchamiana ręcznie lub automatycznie przez .git/hooks/pre-push
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS="${GREEN}✓${NC}"; FAIL="${RED}✗${NC}"; SKIP="${YELLOW}~${NC}"

step() { echo -e "\n${YELLOW}▶ $*${NC}"; }
ok()   { echo -e "${PASS} $*"; }
fail() { echo -e "${FAIL} $*"; exit 1; }

step "Ruff lint"
if command -v ruff >/dev/null 2>&1; then
  ruff check . && ok "ruff check" || fail "ruff check failed"
  ruff format --check . && ok "ruff format" || fail "ruff format failed"
else
  echo -e "${SKIP} ruff not found — skipping lint"
fi

step "Pyright type check"
if command -v pyright >/dev/null 2>&1; then
  pyright && ok "pyright" || fail "pyright failed"
else
  echo -e "${SKIP} pyright not found — skipping type check"
fi

step "pytest (cov ≥ 80%)"
pytest tests/ -q --tb=short \
  --cov=zdrovena --cov-fail-under=80 \
  --cov-report=term-missing \
  && ok "tests passed" || fail "tests failed"

echo -e "\n${GREEN}All checks passed — safe to push.${NC}"
