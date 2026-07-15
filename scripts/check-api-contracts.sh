#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

scripts/generate-api-contracts.sh

if ! git diff --exit-code -- contracts/openapi.json frontend/src/api/generated/schema.d.ts; then
    cat >&2 <<'EOF'

OpenAPI contract drift detected.
Regenerate and commit contracts with:

  scripts/generate-api-contracts.sh

EOF
    exit 1
fi
