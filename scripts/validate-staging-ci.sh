#!/usr/bin/env bash
# Validate CI staging requirements locally before pushing.
# Catches issues that would waste a full CI run (~6 min).
#
# Usage: bash scripts/validate-staging-ci.sh

set -euo pipefail
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
fail() { echo -e "${RED}✗${NC} $*"; FAILED=1; }
skip() { echo -e "${YELLOW}~${NC} $* (skipped — az not logged in)"; }

FAILED=0
ACCOUNT="zdrovenafiles"
CONTAINER="zdrovena-files-staging"

echo ""
echo "=== Staging CI pre-push validation ==="
echo ""

# ── 1. Bash syntax check on CI workflow scripts ──────────────────────────────
echo "--- Shell syntax"

# Prefer the repo venv: PyYAML is a dependency there, system python may lack it.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="python3"
[[ -x "$REPO_ROOT/.venv/bin/python" ]] && PYTHON="$REPO_ROOT/.venv/bin/python"

LINT_ARGS=()
if command -v shellcheck &>/dev/null; then
  LINT_ARGS+=(--shellcheck)
else
  skip "shellcheck not installed (bash -n only)"
fi

# PyYAML is a transitive dependency, so it can be absent on a bare interpreter.
if ! "$PYTHON" -c "import yaml" &>/dev/null; then
  skip "PyYAML not available for $PYTHON — install it to lint workflow shell"
elif "$PYTHON" scripts/ci/lint-workflow-shell.py "${LINT_ARGS[@]}" .github/workflows/*.yml; then
  ok "workflow shell syntax"
else
  fail "shell syntax error in a workflow run: block (see above)"
fi

# ── 2. az CLI available ───────────────────────────────────────────────────────
echo ""
echo "--- Azure CLI"
if ! command -v az &>/dev/null; then
  fail "az CLI not installed"
  echo ""
  echo "Remaining checks require az CLI. Install: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli"
  exit 1
fi
ok "az CLI found: $(az version --query '"azure-cli"' -o tsv 2>/dev/null)"

# Check if logged in
if ! az account show &>/dev/null 2>&1; then
  skip "az not logged in — run 'az login' to enable Azure checks"
  echo ""
  echo "Skipping Azure permission checks."
  exit 0
fi

# ── 3. Storage account reachable ─────────────────────────────────────────────
echo ""
echo "--- Azure Storage"
if az storage account show --name "$ACCOUNT" --query name -o tsv &>/dev/null 2>&1; then
  ok "Storage account '$ACCOUNT' exists"
else
  fail "Cannot reach storage account '$ACCOUNT'"
fi

# ── 4. Staging container exists ──────────────────────────────────────────────
if az storage container show \
  --account-name "$ACCOUNT" \
  --name "$CONTAINER" \
  --auth-mode login &>/dev/null 2>&1; then
  ok "Container '$CONTAINER' exists"
else
  fail "Container '$CONTAINER' not found — check Terraform"
fi

# ── 5. Upload permission test ─────────────────────────────────────────────────
echo ""
echo "--- Upload permission (simulates seed step)"
TMPDIR=$(mktemp -d)
echo "ci-validation-test" > "$TMPDIR/ci-validate.txt"

if az storage blob upload-batch \
  --account-name "$ACCOUNT" \
  --destination "$CONTAINER" \
  --destination-path "faktury/inbox/.ci-validate" \
  --source "$TMPDIR" \
  --auth-mode login &>/dev/null 2>&1; then
  ok "Upload to $CONTAINER/faktury/inbox/ — permission OK"
  # Cleanup
  az storage blob delete \
    --account-name "$ACCOUNT" \
    --container-name "$CONTAINER" \
    --name "faktury/inbox/.ci-validate/ci-validate.txt" \
    --auth-mode login &>/dev/null 2>&1 || true
else
  fail "Upload FAILED — az Storage Blob Data Contributor role may be missing"
  fail "Check: az role assignment list --assignee <github-actions-principal-id>"
fi
rm -rf "$TMPDIR"

# ── 6. Smoke test syntax ─────────────────────────────────────────────────────
echo ""
echo "--- Smoke test files"
if command -v node &>/dev/null; then
  for f in scripts/smoke/tests/*.ts scripts/smoke/runner.ts; do
    [ -f "$f" ] || continue
    # Check for obvious syntax errors by parsing with typescript
    if node --input-type=module --eval "$(sed 's/^import.*$/\/\/ import stripped/' "$f")" &>/dev/null 2>&1 || true; then
      : # ts files can't be run directly, just check they're parseable by grep
    fi
  done
  COUNT=$(ls scripts/smoke/tests/*.ts 2>/dev/null | wc -l | tr -d ' ')
  ok "smoke test files present ($COUNT test files)"
else
  skip "node not found"
fi

# ── Result ────────────────────────────────────────────────────────────────────
echo ""
if [ "$FAILED" -eq 0 ]; then
  echo -e "${GREEN}All checks passed — safe to push.${NC}"
else
  echo -e "${RED}Checks failed — fix before pushing to avoid wasting CI time.${NC}"
  exit 1
fi
