#!/usr/bin/env bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

# Backend
source "$ROOT/.venv/bin/activate"
AZURE_AUTH_DISABLED=true \
AZURE_TENANT_ID=a2e78da5-20fe-4ebe-b625-02652d87fda6 \
AZURE_CLIENT_ID=7a690aca-4a5f-4317-bd89-93b71e0db012 \
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
