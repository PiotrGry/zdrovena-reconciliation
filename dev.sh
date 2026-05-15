#!/usr/bin/env bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Mode selection ────────────────────────────────────────────────────────────
# DEV_MODE=local  → run backend natively (no Docker, no Azurite)
# DEV_MODE=docker → run backend + Azurite via docker compose (default)
DEV_MODE="${DEV_MODE:-docker}"

if [ "$DEV_MODE" = "docker" ]; then
  echo "Starting Azurite + API via Docker Compose..."
  docker compose up -d
  echo ""
  echo "Azurite: http://127.0.0.1:10000"
  echo "API:     http://localhost:8000"
  echo ""
  echo "Seed storage with test files (first time):"
  echo "  python3 scripts/seed-local-storage.py"
else
  # Native mode — no Docker, uses .env.local for storage config
  source "$ROOT/.venv/bin/activate"
  [ -f "$ROOT/.env.local" ] && source "$ROOT/.env.local"
  AZURE_AUTH_DISABLED=true \
  uvicorn zdrovena.api.main:app --reload --port 8000 &
  BACKEND_PID=$!
  trap "kill $BACKEND_PID 2>/dev/null; exit" INT TERM
  echo "API: http://localhost:8000"
  wait
  exit 0
fi

# Frontend (always native — Vite HMR doesn't work well in Docker)
cd "$ROOT/frontend"
# Ensure auth is disabled for local dev (file is gitignored)
grep -q "VITE_AUTH_DISABLED" .env.local 2>/dev/null || echo "VITE_AUTH_DISABLED=true" >> .env.local
npm run dev &
FRONTEND_PID=$!

echo "Frontend: http://localhost:5173"
echo ""
echo "Ctrl+C to stop frontend (Docker services keep running)"
echo "To stop everything: docker compose down"

trap "kill $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait
