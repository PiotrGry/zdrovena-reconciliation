#!/usr/bin/env bash
# Seeds the staging inbox with minimal fake invoices for smoke/E2E tests.
# Required env: AZURE_STORAGE_ACCOUNT, AZURE_RESOURCE_GROUP
set -euo pipefail

ACCOUNT="${AZURE_STORAGE_ACCOUNT:?AZURE_STORAGE_ACCOUNT is required}"
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"

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
rm -rf "$TMPDIR"
echo "Seeded 6 files to zdrovena-files-staging/faktury/inbox/"
