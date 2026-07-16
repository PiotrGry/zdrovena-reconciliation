#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 scripts/export-openapi.py
npm --prefix frontend exec -- openapi-typescript contracts/openapi.json -o frontend/src/api/generated/schema.d.ts
