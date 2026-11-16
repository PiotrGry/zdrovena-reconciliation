#!/usr/bin/env bash
# Smoke test API na staging/prod.
#
# Użycie:
#   ./smoke-test.sh <base-url> <azure-api-client-id>
#
# <base-url> to host SWA. SWA przekazuje do backendu tylko /api/*; każda inna
# ścieżka (np. /health, /docs) dostaje index.html frontendu z HTTP 200, więc nie
# dowodzi niczego o API. Dlatego wszystkie sprawdzenia idą przez /api/*.
#
# Wymaga: curl, jq, az CLI zalogowany (do pobrania tokenu).
set -euo pipefail

BASE="${1:?Podaj base URL (np. https://staging.example.com)}"
CLIENT_ID="${2:?Podaj Azure API client ID}"
CLIENT_ID="${CLIENT_ID//[[:space:]]/}"  # a stray newline in the secret gives AADSTS500011

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

# 1. Token CI. Brak tokenu jest błędem konfiguracji, nie SKIP-em.
#    stderr goes to a file, not /dev/null: az's AADSTS message is the only clue
#    why CI cannot get a token. It never contains the token itself.
echo "--- token CI dla api://$CLIENT_ID"
TOKEN_ERR=$(mktemp)
BODY=$(mktemp)
trap 'rm -f "$TOKEN_ERR" "$BODY"' EXIT
TOKEN=$(az account get-access-token \
        --resource "api://$CLIENT_ID" \
        --query accessToken -o tsv 2>"$TOKEN_ERR") && [[ -n "$TOKEN" ]] \
    || fail "nie udało się pobrać tokenu dla api://$CLIENT_ID: $(tr '\n' ' ' < "$TOKEN_ERR")"
pass "token CI"

# 2. /api/ci/smoke z tokenem → 200 + JSON {status: ok, version}. To jest sprawdzenie
#    żywotności (z retries na cold start). Rola zdrovena-ci-smoke (tylko service
#    principal GitHub Actions) nie daje dostępu do danych.
echo "--- /api/ci/smoke (z tokenem CI, retries na cold start)"
for i in $(seq 1 18); do
    HTTP=$(curl -sS -o "$BODY" -w "%{http_code}" --max-time 15 \
        -H "Authorization: Bearer $TOKEN" "$BASE/api/ci/smoke" || true)
    echo "  attempt $i: HTTP $HTTP"
    [[ "$HTTP" == "200" ]] && break
    sleep 10
done
[[ "$HTTP" == "200" ]] || fail "/api/ci/smoke zwróciło $HTTP po 18 próbach (oczekiwano 200)"
jq -e '.status == "ok" and (.version | type == "string" and length > 0)' "$BODY" >/dev/null \
    || fail "/api/ci/smoke nie zwróciło JSON {status: ok, version} — odpowiedź nie pochodzi z API"
pass "/api/ci/smoke → 200, API version $(jq -r .version "$BODY")"

# 3. Bez tokenu → 401/403, nie 500 i nie index.html.
for path in /api/ci/smoke /api/files; do
    HTTP_ANON=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BASE$path" || true)
    [[ "$HTTP_ANON" == "401" || "$HTTP_ANON" == "403" ]] \
        || fail "$path bez tokenu zwróciło $HTTP_ANON (oczekiwano 401/403)"
    pass "$path (anon) → $HTTP_ANON"
done

echo ""
echo "Wszystkie smoke testy przeszły."
