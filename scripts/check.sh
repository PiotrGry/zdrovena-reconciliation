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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Smart Detection: Skip Python tests if only infra/docs changed
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Check which files changed (compare HEAD with remote)
if git rev-parse --verify origin/develop >/dev/null 2>&1; then
  CHANGED_FILES=$(git diff --name-only HEAD origin/develop 2>/dev/null || true)
  
  # If no changes detected, check staged files instead
  if [[ -z "$CHANGED_FILES" ]]; then
    CHANGED_FILES=$(git diff --name-only --cached 2>/dev/null || true)
  fi
  
  # Check if ALL changes are non-Python files (infra, docs, workflows, scripts, TODOS)
  if [[ -n "$CHANGED_FILES" ]]; then
    # Remove infra/docs/workflow/scripts changes from the list
    PYTHON_CHANGES=$(echo "$CHANGED_FILES" | grep -vE '^(infra/|docs/|scripts/|\.github/workflows/|README\.md|TODOS\.md|CLAUDE\.md|\.gitignore)' || true)
    
    if [[ -z "$PYTHON_CHANGES" ]]; then
      echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
      echo -e "${YELLOW}Smart Skip: Only infrastructure/docs changed, skipping Python tests${NC}"
      echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
      echo ""
      echo "Changed files:"
      echo "$CHANGED_FILES" | sed 's/^/  - /'
      echo ""
      echo -e "${GREEN}Fast-forwarding push (no Python code changed)${NC}"
      echo ""
      exit 0
    fi
  fi
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
# Pomiń tylko gdy CHECK_TYPECHECK=0 (opt-out). Domyślnie zawsze biegnie.
if [[ "${CHECK_TYPECHECK:-1}" == "0" ]]; then
  echo -e "${SKIP} pyright pominięty (CHECK_TYPECHECK=0)"
else
  $PYRIGHT_CMD && ok "pyright" || fail "pyright failed"
fi

step "bandit — SAST (Python source)"
BANDIT_CMD="bandit"
if ! command -v bandit >/dev/null 2>&1 && [[ -f "$REPO_ROOT/.venv/bin/bandit" ]]; then
  BANDIT_CMD="$REPO_ROOT/.venv/bin/bandit"
fi
if command -v "$BANDIT_CMD" >/dev/null 2>&1 || [[ -f "$REPO_ROOT/.venv/bin/bandit" ]]; then
  $BANDIT_CMD -r zdrovena/ -ll -ii -q && ok "bandit" || fail "bandit found security issues"
else
  echo -e "${SKIP} bandit not found — skipping (pip install bandit[toml])"
fi

step "pytest (cov ≥ 80%)"
if [[ "${CHECK_TESTS:-1}" == "0" ]]; then
  echo -e "${SKIP} pytest pominięty (CHECK_TESTS=0)"
else
  $PYTEST_CMD tests/ -q --tb=short \
    --cov=zdrovena --cov-fail-under=80 \
    --cov-report=term-missing \
    && ok "tests passed" || fail "tests failed"
fi

step "pip-audit — zależności Python"
# Ignorowane CVE (transitive deps, brak fix version):
#   PYSEC-2026-89  — markdown 3.10.2 (via cloudsplaining), najnowsza wersja, brak fixa
#   PYSEC-2025-183 — pyjwt 2.12.1 (via msal/azure-identity), najnowsza wersja, brak fixa
_AUDIT_IGNORE="--ignore-vuln PYSEC-2026-89 --ignore-vuln PYSEC-2025-183"
# Użyj uv run żeby skanować tylko pakiety projektu (nie globalny Python)
if command -v uv >/dev/null 2>&1 && [ -d "$REPO_ROOT/.venv" ]; then
  PIPAPI_PYTHON_LOCATION="$REPO_ROOT/.venv/bin/python3" uv run pip-audit --local $_AUDIT_IGNORE 2>&1 \
    && ok "pip-audit" || fail "pip-audit: znaleziono podatności — uruchom: uv lock --upgrade-package <pkg>"
elif command -v pip-audit >/dev/null 2>&1; then
  pip-audit --local $_AUDIT_IGNORE 2>&1 && ok "pip-audit" || fail "pip-audit: znaleziono podatności"
else
  echo -e "${SKIP} pip-audit nie znaleziony — uruchom: uv add --dev pip-audit"
fi

step "gitleaks — skanowanie sekretów"
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --no-banner 2>&1 | tail -1 && ok "gitleaks" || fail "gitleaks: wykryto sekrety w kodzie"
else
  echo -e "${SKIP} gitleaks nie znaleziony — zainstaluj: https://github.com/gitleaks/gitleaks"
fi

step "trivy — podatności w zależnościach"
if command -v trivy >/dev/null 2>&1; then
  trivy fs --severity HIGH,CRITICAL --quiet --exit-code 1 . && ok "trivy" || fail "trivy: podatności HIGH/CRITICAL w zależnościach"
else
  echo -e "${SKIP} trivy nie znaleziony — zainstaluj: https://aquasecurity.github.io/trivy"
fi

step "terraform fmt — IaC formatting"
if command -v terraform >/dev/null 2>&1 && [ -d "$REPO_ROOT/infra/terraform" ]; then
  terraform fmt -check -recursive "$REPO_ROOT/infra/terraform" && ok "terraform fmt" || fail "terraform fmt: uruchom 'terraform fmt -recursive infra/terraform'"
else
  echo -e "${SKIP} terraform not found — skipping fmt check"
fi

step "checkov — IaC security scan"
if command -v checkov >/dev/null 2>&1 || docker image inspect ghcr.io/bridgecrewio/checkov:latest &>/dev/null 2>&1; then
  CHECKOV_CMD="checkov"
  if ! command -v checkov >/dev/null 2>&1; then
    CHECKOV_CMD="docker run --rm -v $(pwd):/github/workspace -w /github/workspace ghcr.io/bridgecrewio/checkov:latest"
  fi
  $CHECKOV_CMD -d infra/terraform --quiet 2>&1 | grep -E "Passed|Failed|Error" | tail -3 && ok "checkov" || fail "checkov: problemy z IaC"
else
  echo -e "${SKIP} checkov nie znaleziony — zainstaluj: pip install checkov"
fi

step "Frontend lint (ESLint)"
FRONTEND_DIR="$REPO_ROOT/frontend"
if [ -d "$FRONTEND_DIR/node_modules" ]; then
  (cd "$FRONTEND_DIR" && npm run lint 2>&1) && ok "eslint" || fail "eslint failed — run: cd frontend && npm run lint"
else
  echo -e "${SKIP} frontend/node_modules missing — run 'cd frontend && npm install' first"
fi

step "Frontend TypeScript build"
if [ -d "$FRONTEND_DIR/node_modules" ]; then
  (cd "$FRONTEND_DIR" && npm run build 2>&1 | tail -5) && ok "vite build" || fail "frontend build failed — run: cd frontend && npm run build"
else
  echo -e "${SKIP} frontend/node_modules missing — skipping build"
fi

echo -e "\n${GREEN}All checks passed — safe to push.${NC}"
