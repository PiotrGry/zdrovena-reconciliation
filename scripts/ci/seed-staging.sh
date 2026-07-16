#!/usr/bin/env bash
# Seeds the staging inbox with minimal fake invoices for smoke/E2E tests.
# Required env: AZURE_STORAGE_ACCOUNT, AZURE_RESOURCE_GROUP
set -euo pipefail

ACCOUNT="${AZURE_STORAGE_ACCOUNT:?AZURE_STORAGE_ACCOUNT is required}"
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
FAKE_PROVIDER_URL="${FAKE_PROVIDER_URL:?FAKE_PROVIDER_URL is required}"
FAKE_PROVIDER_URL="${FAKE_PROVIDER_URL%/}"

NOW=$(date -u +"%Y-%m")
YEAR="${NOW%-*}"
MONTH_NUM=$(date -u +"%m" | sed 's/^0//')
PREV_MONTH=$(( MONTH_NUM == 1 ? 12 : MONTH_NUM - 1 ))
PREV_YEAR=$(( MONTH_NUM == 1 ? YEAR - 1 : YEAR ))
echo "Seeding staging inbox for ${PREV_YEAR}/${PREV_MONTH}..."

az storage blob delete-batch \
  --account-name "$ACCOUNT" \
  --source zdrovena-files-staging \
  --pattern "faktury/inbox/*" \
  --auth-mode login 2>/dev/null || true

TMPDIR=$(mktemp -d)
FAKE_PDF='%PDF-1.4 seed'
FAKE_XML='<?xml version="1.0" encoding="UTF-8"?><root><test>seed</test></root>'

if [ "$PREV_MONTH" -eq 12 ]; then
  NEXT_MONTH=1
  NEXT_YEAR=$(( PREV_YEAR + 1 ))
else
  NEXT_MONTH=$(( PREV_MONTH + 1 ))
  NEXT_YEAR=$PREV_YEAR
fi
BANK_DATE=$(printf "%04d%02d01" "$NEXT_YEAR" "$NEXT_MONTH")
MONTH_PAD=$(printf "%02d" "$PREV_MONTH")

echo "$FAKE_PDF" > "$TMPDIR/Wyciag_na_zadanie_${BANK_DATE}001.pdf"
echo "$FAKE_PDF" > "$TMPDIR/invoice-12345-${PREV_YEAR}${MONTH_PAD}15.pdf"
echo "$FAKE_PDF" > "$TMPDIR/3849995102.pdf"
echo "$FAKE_XML" > "$TMPDIR/zdrovena-${PREV_YEAR}-${MONTH_PAD}-01-jpk_fa.xml"
echo "$FAKE_XML" > "$TMPDIR/zdrovena-${PREV_YEAR}-${MONTH_PAD}-01-jpkv7m.xml"
echo "$FAKE_PDF" > "$TMPDIR/zdrovena-${PREV_YEAR}-${MONTH_PAD}-01_wykaz_sprzedazy.pdf"

CLIENT_ID=$(az account show --query "user.name" -o tsv)
SUB_ID=$(az account show --query "id" -o tsv)
OBJECT_ID=$(az ad sp show --id "$CLIENT_ID" --query "id" -o tsv 2>/dev/null || echo "LOOKUP_FAILED")
echo "=== principal object ID: $OBJECT_ID ==="
az role assignment list \
  --assignee "$OBJECT_ID" \
  --scope "/subscriptions/$SUB_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$ACCOUNT" \
  --query "[].roleDefinitionName" -o tsv 2>&1 || true

az storage blob upload-batch \
  --account-name "$ACCOUNT" \
  --destination zdrovena-files-staging \
  --destination-path "faktury/inbox" \
  --source "$TMPDIR" \
  --auth-mode login
az storage blob upload-batch \
  --account-name "$ACCOUNT" \
  --destination zdrovena-files-staging \
  --destination-path "faktury/inbox/${PREV_YEAR}-${MONTH_PAD}" \
  --source "$TMPDIR" \
  --auth-mode login
rm -rf "$TMPDIR"
echo "Seeded 6 files to the legacy and period-scoped staging inboxes."

echo "Resetting and seeding fake Fakturownia for ${PREV_YEAR}-${MONTH_PAD}..."
curl --fail --silent --show-error \
  --request POST \
  "$FAKE_PROVIDER_URL/__fake__/reset" >/dev/null

seed_fakturownia_invoice() {
  local number="$1"
  local oid="$2"
  local income="$3"
  local buyer_name="$4"
  local price_gross="$5"
  local has_attachments="${6:-false}"

  curl --fail --silent --show-error \
    --request POST \
    --header "Content-Type: application/json" \
    --data "{\"invoice\":{\"number\":\"$number\",\"oid\":\"$oid\",\"income\":\"$income\",\"buyer_name\":\"$buyer_name\",\"sell_date\":\"${PREV_YEAR}-${MONTH_PAD}-15\",\"issue_date\":\"${PREV_YEAR}-${MONTH_PAD}-15\",\"price_gross\":\"$price_gross\",\"has_attachments\":$has_attachments}}" \
    "$FAKE_PROVIDER_URL/fakturownia/invoices.json?api_token=fake" >/dev/null
}

seed_fakturownia_invoice "1/SMOKE-${PREV_YEAR}-${MONTH_PAD}" "smoke-sale" "yes" "HUMIO smoke sale" "123.00"
seed_fakturownia_invoice "COST-1/SMOKE-${PREV_YEAR}-${MONTH_PAD}" "smoke-shopify" "no" "Shopify" "49.00"
seed_fakturownia_invoice "COST-2/SMOKE-${PREV_YEAR}-${MONTH_PAD}" "smoke-allegro" "no" "Allegro" "49.00"
seed_fakturownia_invoice "COST-3/SMOKE-${PREV_YEAR}-${MONTH_PAD}" "smoke-payu" "no" "PayU" "49.00"
seed_fakturownia_invoice "COST-4/SMOKE-${PREV_YEAR}-${MONTH_PAD}" "smoke-inpost" "no" "InPost" "49.00"
seed_fakturownia_invoice "COST-5/SMOKE-${PREV_YEAR}-${MONTH_PAD}" "smoke-apaczka" "no" "Alsendo Apaczka" "49.00"
seed_fakturownia_invoice "COST-6/SMOKE-${PREV_YEAR}-${MONTH_PAD}" "smoke-pulsepure" "no" "PulsePure" "49.00" true
seed_fakturownia_invoice "COST-7/SMOKE-${PREV_YEAR}-${MONTH_PAD}" "smoke-accounting" "no" "Ogorzalek accounting" "49.00"

echo "Seeded 1 sales and 7 cost invoices in fake Fakturownia."
