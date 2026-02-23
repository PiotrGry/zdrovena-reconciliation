#!/usr/bin/env bash
set -euo pipefail

echo "[check] Running quality gate..."

# Optional: ruff if available
if command -v ruff >/dev/null 2>&1; then
  echo "[check] ruff check ."
  ruff check .
else
  echo "[check] ruff not found (skipping lint). Install ruff if you want linting."
fi

# Run tests (pytest)
if command -v pytest >/dev/null 2>&1; then
  echo "[check] pytest"
  pytest -q
else
  echo "[check] pytest not found. Install it or adjust scripts/check.sh."
  exit 1
fi

echo "[check] OK"
