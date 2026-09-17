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
    HTTP=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 15 "$BASE/health" || true)
    echo "  attempt $i: HTTP $HTTP"
    [[ "$HTTP" == "200" ]] && break
    sleep 10
done
[[ "$HTTP" == "200" ]] || fail "/health zwróciło $HTTP po 18 próbach (oczekiwano 200)"
pass "/health → 200 (healthy)"

# 2. /docs — routing + FastAPI bez crash
echo "--- /docs"
HTTP_DOCS=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/docs" || true)
[[ "$HTTP_DOCS" == "200" ]] \
    || fail "/docs zwróciło $HTTP_DOCS (oczekiwano 200)"
pass "/docs → 200"

# 3. /files bez tokenu → 401/403, nie 500
echo "--- /files (bez tokenu)"
HTTP_ANON=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/api/files")
[[ "$HTTP_ANON" == "401" || "$HTTP_ANON" == "403" ]] \
    || fail "/files bez tokenu zwróciło $HTTP_ANON (oczekiwano 401/403)"
pass "/files (anon) → $HTTP_ANON"

# 4. /files z tokenem CI → 200. Brak tokenu jest błędem konfiguracji, nie SKIP-em.
echo "--- /files (z tokenem CI)"
if TOKEN=$(az account get-access-token \
        --resource "api://$CLIENT_ID" \
        --query accessToken -o tsv 2>/dev/null) && [[ -n "$TOKEN" ]]; then
    HTTP_AUTH=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
        -H "Authorization: Bearer $TOKEN" "$BASE/api/files")
    [[ "$HTTP_AUTH" == "200" ]] || fail "/files z tokenem zwróciło $HTTP_AUTH (oczekiwano 200)"
    pass "/files (auth) → 200"
else
    fail "nie udało się pobrać tokenu dla api://$CLIENT_ID"
fi

echo ""
echo "Wszystkie smoke testy przeszły."
