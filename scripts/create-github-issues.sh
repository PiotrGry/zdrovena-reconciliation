#!/usr/bin/env bash
set -euo pipefail
REPO="${1:-PiotrGry/zdrovena-reconciliation}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v gh >/dev/null || { echo "Brak gh CLI"; exit 1; }
gh auth status

echo "Tworzenie: R4-A: make staging smoke validation strict and eliminate false-green 401/SKIP results"
gh issue create --repo "$REPO" --title "R4-A: make staging smoke validation strict and eliminate false-green 401/SKIP results" --body-file "$ROOT/issues/01-r4a-smoke-strict.md"

echo "Tworzenie: R4-B: harden correlation ID propagation and fail-closed production auth configuration"
gh issue create --repo "$REPO" --title "R4-B: harden correlation ID propagation and fail-closed production auth configuration" --body-file "$ROOT/issues/02-r4b-observability-auth.md"

echo "Tworzenie: R4-C: verify Azure monitoring delivery and make DLQ alert operational"
gh issue create --repo "$REPO" --title "R4-C: verify Azure monitoring delivery and make DLQ alert operational" --body-file "$ROOT/issues/03-r4c-monitoring.md"

echo "Tworzenie: R4.1: recover partially created Allegro invoices instead of looping on HTTP 502"
gh issue create --repo "$REPO" --title "R4.1: recover partially created Allegro invoices instead of looping on HTTP 502" --body-file "$ROOT/issues/04-r4-1-invoice-502.md"

echo "Tworzenie: R4.2: unify Allegro deposit calculation and keep monetary values Decimal-safe"
gh issue create --repo "$REPO" --title "R4.2: unify Allegro deposit calculation and keep monetary values Decimal-safe" --body-file "$ROOT/issues/05-r4-2-deposit.md"

echo "Tworzenie: R4.3: make invoice preview match the final invoice and Allegro totalToPay"
gh issue create --repo "$REPO" --title "R4.3: make invoice preview match the final invoice and Allegro totalToPay" --body-file "$ROOT/issues/06-r4-3-preview-parity.md"

echo "Tworzenie: R5-A: add atomic draft execution claim and enforce shipment state transitions"
gh issue create --repo "$REPO" --title "R5-A: add atomic draft execution claim and enforce shipment state transitions" --body-file "$ROOT/issues/07-r5a-shipping-state-machine.md"

echo "Tworzenie: R5-B: add LABEL_NOT_READY handling and batch label printing"
gh issue create --repo "$REPO" --title "R5-B: add LABEL_NOT_READY handling and batch label printing" --body-file "$ROOT/issues/08-r5b-labels.md"

echo "Tworzenie: R6: add frontend tests, generated API contracts, fake providers and critical E2E flows"
gh issue create --repo "$REPO" --title "R6: add frontend tests, generated API contracts, fake providers and critical E2E flows" --body-file "$ROOT/issues/09-r6-test-foundation.md"
