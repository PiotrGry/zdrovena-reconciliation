#!/usr/bin/env bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Mode selection ────────────────────────────────────────────────────────────
# DEV_MODE=local  → run backend natively (no Docker, no Azurite)
# DEV_MODE=docker → run backend + Azurite via docker compose (default)
DEV_MODE="${DEV_MODE:-docker}"
MOCK_COURIER="${MOCK_COURIER:-1}"  # 1 = pomija prawdziwe API kuriera (InPost/Apaczka)

if [ "$DEV_MODE" = "docker" ]; then
  echo "Starting Azurite + API via Docker Compose..."
  [ "$MOCK_COURIER" = "1" ] && echo "  ⚠  MOCK_COURIER=1 — kurierzy zamokowany"
  MOCK_COURIER="$MOCK_COURIER" docker compose up -d

  echo ""
  echo "Czekam aż API będzie gotowe..."
  until docker compose exec -T api python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" 2>/dev/null; do
    sleep 1
  done

  echo "Seeduję testowe dane wysyłek..."
  docker compose exec -T api python3 /app/scripts/seed-shipping-drafts.py

  echo ""
  echo "Azurite: http://127.0.0.1:10000"
  echo "API:     http://localhost:8000"
else
  # Native mode — no Docker, uses .env.local for storage config
  source "$ROOT/.venv/bin/activate"
  [ -f "$ROOT/.env.local" ] && source "$ROOT/.env.local"
  [ "$MOCK_COURIER" = "1" ] && echo "  ⚠  MOCK_COURIER=1 — kurierzy zamokowany"
  AZURE_AUTH_DISABLED=true MOCK_COURIER="$MOCK_COURIER" \
    uvicorn zdrovena.api.main:app --reload --port 8000 &
  BACKEND_PID=$!
  trap 'kill "$BACKEND_PID" 2>/dev/null; exit' INT TERM

  echo "Czekam aż API będzie gotowe..."
  until curl -sf http://localhost:8000/health > /dev/null 2>&1; do sleep 1; done

  echo "Seeduję testowe dane wysyłek..."
  python3 "$ROOT/scripts/seed-shipping-drafts.py"

  echo "API: http://localhost:8000"
  wait
  exit 0
fi

# Frontend (always native — Vite HMR doesn't work well in Docker)
cd "$ROOT/frontend"
# Ensure auth is disabled for local dev (file is gitignored)
grep -q "VITE_AUTH_DISABLED" .env.local 2>/dev/null || echo "VITE_AUTH_DISABLED=true" >>.env.local
npm run dev &
FRONTEND_PID=$!

echo "Frontend: http://localhost:5173"
echo ""
echo "Ctrl+C to stop frontend (Docker services keep running)"
echo "To stop everything: docker compose down"

trap 'kill $FRONTEND_PID 2>/dev/null; exit' INT TERM
wait
