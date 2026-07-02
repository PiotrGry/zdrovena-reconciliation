#!/usr/bin/env bash
# stop.sh — stop all dev services (Docker + native processes)
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${YELLOW}▶${NC} $*"; }

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Docker (Azurite + API) ────────────────────────────────────────────────────
if docker compose -f "$ROOT/docker-compose.yml" ps -q 2>/dev/null | grep -q .; then
    info "Stopping Docker (azurite + api)..."
    docker compose -f "$ROOT/docker-compose.yml" down
    ok "Docker stopped"
else
    ok "Docker — no running containers"
fi

# ── Cloudflared tunnel ───────────────────────────────────────────────────────
if pgrep -f "cloudflared tunnel" > /dev/null 2>&1; then
    info "Stopping cloudflared..."
    pkill -f "cloudflared tunnel" 2>/dev/null || true
    ok "Cloudflared stopped"
else
    ok "Cloudflared — not running"
fi

# ── Native backend (uvicorn) ──────────────────────────────────────────────────
if pgrep -f "uvicorn zdrovena" > /dev/null 2>&1; then
    info "Stopping uvicorn..."
    pkill -9 -f "uvicorn zdrovena" 2>/dev/null || true
    ok "Uvicorn stopped"
else
    ok "Uvicorn — not running"
fi

# ── Frontend (Vite / npm run dev) ─────────────────────────────────────────────
if pgrep -f "vite\|npm run dev" > /dev/null 2>&1; then
    info "Stopping Vite (frontend)..."
    pkill -9 -f "vite" 2>/dev/null || true
    pkill -9 -f "npm run dev" 2>/dev/null || true
    ok "Vite stopped"
else
    ok "Vite — not running"
fi

# ── Force-kill anything still holding dev ports ───────────────────────────────
echo ""
for port in 8000 5173 10000 10001 10002; do
    pids=$(lsof -ti :"$port" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        info "Port $port still occupied — killing PID(s) $pids"
        echo "$pids" | xargs kill -9 2>/dev/null || true
        ok "Port $port freed"
    fi
done

echo ""
ok "Dev environment stopped."
