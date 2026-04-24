#!/usr/bin/env bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

# Backend
source "$ROOT/.venv/bin/activate"
# Load dev env vars from .env.local (copy from .env.template, never commit)
[ -f "$ROOT/.env.local" ] && source "$ROOT/.env.local"
AZURE_AUTH_DISABLED=true \
uvicorn zdrovena.api.main:app --reload --port 8000 &
BACKEND_PID=$!

# Frontend
cd "$ROOT/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "Backend:  http://localhost:8000"
echo "Frontend: http://localhost:5173"
echo ""
echo "Ctrl+C to stop both"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait
