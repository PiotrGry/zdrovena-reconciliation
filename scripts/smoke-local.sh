#!/usr/bin/env bash
# Run smoke tests locally against the staging environment.
# No build, no deploy — tests run against the already-deployed staging in seconds.
#
# Usage:
#   bash scripts/smoke-local.sh            # runs all tests
#   bash scripts/smoke-local.sh --verbose  # verbose output
#
# First run: copy .env.smoke.example → .env.smoke and fill in secrets.
# Secrets that never change (tenant, client IDs) are auto-read from az CLI.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env.smoke"

# ── Load .env.smoke if present ─────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -o allexport && source "$ENV_FILE" && set +o allexport
fi

# ── Auto-detect staging API URL from Azure ─────────────────────────────────
if [[ -z "${API_URL:-}" ]]; then
  echo "▶ Resolving staging API URL from Azure..."
  API_URL=$(az containerapp show \
    --name zdrovena-api-staging \
    --resource-group zdrovena-rg \
    --query "properties.configuration.ingress.fqdn" \
    -o tsv 2>/dev/null | sed 's|^|https://|')
  if [[ -z "$API_URL" ]]; then
    echo "✗ Cannot resolve staging URL. Set API_URL in .env.smoke or run: az login"
    exit 1
  fi
  echo "  API_URL=$API_URL"
fi

# ── Auto-detect SWA URL if not set ─────────────────────────────────────────
if [[ -z "${SWA_URL:-}" ]]; then
  SWA_URL=$(az staticwebapp list --resource-group zdrovena-rg \
    --query "[0].defaultHostname" -o tsv 2>/dev/null | sed 's|^|https://|') || SWA_URL=""
fi

# ── Auth: SP secrets or az CLI fallback ───────────────────────────────────
MISSING_INFRA=()
[[ -z "${AZURE_TENANT_ID:-}" ]]     && MISSING_INFRA+=("AZURE_TENANT_ID")
[[ -z "${AZURE_API_CLIENT_ID:-}" ]] && MISSING_INFRA+=("AZURE_API_CLIENT_ID")

if [[ ${#MISSING_INFRA[@]} -gt 0 ]]; then
  echo "✗ Missing required values in .env.smoke:"
  for v in "${MISSING_INFRA[@]}"; do echo "  $v"; done
  echo ""
  echo "  Copy scripts/.env.smoke.example → .env.smoke and fill in AZURE_TENANT_ID / AZURE_API_CLIENT_ID."
  exit 1
fi

# If SP client secrets are missing, fall back to az CLI (az login)
if [[ -z "${SMOKE_SP_CLIENT_SECRET:-}" ]] || [[ -z "${SMOKE_ACCOUNTANT_SP_CLIENT_SECRET:-}" ]]; then
  echo "▶ No SP client secrets — using az CLI token (az login)..."
  AZ_TOKEN=$(az account get-access-token \
    --resource "api://${AZURE_API_CLIENT_ID}" \
    --query accessToken -o tsv 2>/dev/null) || true
  if [[ -z "$AZ_TOKEN" ]]; then
    echo "✗ az CLI token failed. Run: az login"
    exit 1
  fi
  echo "  ✓ Token obtained via az CLI"
  # Use the same token for both viewer and accountant — your az identity has all roles
  export SMOKE_VIEWER_TOKEN="$AZ_TOKEN"
  export SMOKE_ACCOUNTANT_TOKEN="$AZ_TOKEN"
fi

# ── Run tests ──────────────────────────────────────────────────────────────
echo ""
echo "▶ Running smoke tests against $API_URL"
echo ""

cd "$REPO_ROOT/scripts/smoke"

export API_URL SWA_URL AZURE_TENANT_ID AZURE_API_CLIENT_ID \
       SMOKE_SP_CLIENT_ID SMOKE_SP_CLIENT_SECRET \
       SMOKE_ACCOUNTANT_SP_CLIENT_ID SMOKE_ACCOUNTANT_SP_CLIENT_SECRET

OUTPUT_FILE="/tmp/smoke-local-$(date +%Y%m%d-%H%M%S).json"
npx tsx runner.ts --output "$OUTPUT_FILE" "$@"

# ── Summary ────────────────────────────────────────────────────────────────
python3 -c "
import json, sys
d = json.load(open('$OUTPUT_FILE'))
tests = d.get('tests', [])
passed = sum(1 for t in tests if t.get('status') == 'PASS')
failed = sum(1 for t in tests if t.get('status') == 'FAIL')
skipped = sum(1 for t in tests if t.get('status') == 'SKIP')
print(f'\n  {passed} passed, {failed} failed, {skipped} skipped')
for t in tests:
    if t.get('status') == 'FAIL':
        print(f\"  ❌ {t.get('title','?')}\")
        print(f\"     {t.get('error','')}\")
        ev = t.get('evidence','')
        if ev: print(f\"     {str(ev)[:200]}\")
sys.exit(0 if failed == 0 else 1)
"
