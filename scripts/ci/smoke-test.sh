#!/usr/bin/env bash
# Smoke test API na staging/prod.
#
# Użycie:
#   ./smoke-test.sh <base-url> <azure-api-client-id>
#
# Wymaga: curl, az CLI zalogowany (do pobrania tokenu).
set -euo pipefail

BASE="${1:?Podaj base URL (np. https://staging.example.com)}"
CLIENT_ID="${2:?Podaj Azure API client ID}"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

# 1. /health — liveness (z retries na cold start)
echo "--- /health (z retries na cold start)"
for i in $(seq 1 18); do
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$BASE/health")
    echo "  attempt $i: HTTP $HTTP"
    # 200 = healthy public endpoint; 401 = app responding, auth wired (older image
    # may have /health behind auth — still proves liveness, no 5xx, no connect error)
    [[ "$HTTP" == "200" || "$HTTP" == "401" ]] && break
    sleep 10
done
[[ "$HTTP" == "200" || "$HTTP" == "401" ]] || fail "/health zwróciło $HTTP po 18 próbach"
pass "/health → $HTTP (alive)"

# 2. /docs — routing + FastAPI bez crash
echo "--- /docs"
HTTP_DOCS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/docs")
# 200 = public swagger; 401 = auth-gated swagger (older image) — both prove app is up
[[ "$HTTP_DOCS" == "200" || "$HTTP_DOCS" == "401" ]] \
    || fail "/docs zwróciło $HTTP_DOCS (oczekiwano 200/401, nie 5xx)"
pass "/docs → $HTTP_DOCS"

# 3. /files bez tokenu → 401/403, nie 500
echo "--- /files (bez tokenu)"
HTTP_ANON=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/files")
[[ "$HTTP_ANON" == "401" || "$HTTP_ANON" == "403" ]] \
    || fail "/files bez tokenu zwróciło $HTTP_ANON (oczekiwano 401/403)"
pass "/files (anon) → $HTTP_ANON"

# 4. /files z tokenem CI → 200
echo "--- /files (z tokenem CI)"
TOKEN=$(az account get-access-token \
    --resource "api://$CLIENT_ID" \
    --query accessToken -o tsv)
[[ -n "$TOKEN" ]] \
    || fail "Nie udało się pobrać tokenu dla api://$CLIENT_ID — sprawdź rolę zdrovena-viewer na GitHub Actions SP"
HTTP_AUTH=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "Authorization: Bearer $TOKEN" "$BASE/files")
[[ "$HTTP_AUTH" == "200" ]] || fail "/files z tokenem zwróciło $HTTP_AUTH (oczekiwano 200)"
pass "/files (auth) → 200"

echo ""
echo "Wszystkie smoke testy przeszły."
