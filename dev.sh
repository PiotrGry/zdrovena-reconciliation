#!/usr/bin/env bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Mode selection ────────────────────────────────────────────────────────────
# DEV_MODE=local  → run backend natively (no Docker, no Azurite)
# DEV_MODE=docker → run backend + Azurite via docker compose (default)
DEV_MODE="${DEV_MODE:-docker}"
MOCK_COURIER="${MOCK_COURIER:-1}"  # 1 = pomija prawdziwe API kuriera (InPost/Apaczka)

if [ "$DEV_MODE" = "docker" ]; then
  [ -f "$ROOT/.env.local" ] && source "$ROOT/.env.local"
  echo "Starting Azurite + API via Docker Compose..."
  [ "$MOCK_COURIER" = "1" ] && echo "  ⚠  MOCK_COURIER=1 — kurierzy zamokowany"
  MOCK_COURIER="$MOCK_COURIER" docker compose up -d

  echo ""
  echo "Czekam aż API będzie gotowe..."
  until docker compose exec -T api python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" 2>/dev/null; do
    sleep 1
  done

  echo "Seeduję blob storage (Azurite)..."
  docker compose exec -T api python3 /app/scripts/seed-local-storage.py

  echo "Seeduję testowe dane wysyłek..."
  docker compose exec -T api python3 /app/scripts/seed-shipping-drafts.py

  echo ""
  echo "Azurite: http://127.0.0.1:10000"
  echo "API:     http://localhost:8000"

  # ── Cloudflared tunnel (Shopify webhooks) ──────────────────────────────────
  CLOUDFLARED_LOG="/tmp/zdrovena-cloudflared.log"
  CLOUDFLARED_PID=""
  if command -v cloudflared >/dev/null 2>&1; then
    echo ""
    echo "Starting cloudflared tunnel for Shopify webhooks..."
    rm -f "$CLOUDFLARED_LOG"
    cloudflared tunnel --url http://localhost:8000 --logfile "$CLOUDFLARED_LOG" 2>/dev/null &
    CLOUDFLARED_PID=$!

    TUNNEL_URL=""
    for i in $(seq 1 15); do
      TUNNEL_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$CLOUDFLARED_LOG" 2>/dev/null | head -1)
      [ -n "$TUNNEL_URL" ] && break
      sleep 1
    done

    if [ -n "$TUNNEL_URL" ]; then
      WEBHOOK_URL="${TUNNEL_URL}/api/webhooks/shopify/order-create"
      echo "Tunnel URL: $WEBHOOK_URL"

      # Auto-register/update Shopify webhook if credentials are available
      SHOPIFY_TOKEN="${SHOPIFY_ACCESS_TOKEN:-}"
      SHOPIFY_DOMAIN="${SHOPIFY_SHOP_DOMAIN:-}"
      if [ -n "$SHOPIFY_TOKEN" ] && [ -n "$SHOPIFY_DOMAIN" ]; then
        EXISTING_ID=$(curl -sf \
          "https://${SHOPIFY_DOMAIN}/admin/api/2024-01/webhooks.json?topic=orders/create" \
          -H "X-Shopify-Access-Token: ${SHOPIFY_TOKEN}" \
          | python3 -c "import sys,json; wh=json.load(sys.stdin)['webhooks']; print(wh[0]['id'] if wh else '')" 2>/dev/null || true)

        if [ -n "$EXISTING_ID" ]; then
          HTTP=$(curl -sf -o /dev/null -w "%{http_code}" -X PUT \
            "https://${SHOPIFY_DOMAIN}/admin/api/2024-01/webhooks/${EXISTING_ID}.json" \
            -H "X-Shopify-Access-Token: ${SHOPIFY_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "{\"webhook\":{\"id\":${EXISTING_ID},\"address\":\"${WEBHOOK_URL}\"}}" 2>/dev/null || echo "000")
          [ "$HTTP" = "200" ] && SHOPIFY_STATUS="updated (id=${EXISTING_ID})" || SHOPIFY_STATUS="update failed (HTTP $HTTP)"
        else
          HTTP=$(curl -sf -o /dev/null -w "%{http_code}" -X POST \
            "https://${SHOPIFY_DOMAIN}/admin/api/2024-01/webhooks.json" \
            -H "X-Shopify-Access-Token: ${SHOPIFY_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "{\"webhook\":{\"topic\":\"orders/create\",\"address\":\"${WEBHOOK_URL}\",\"format\":\"json\"}}" 2>/dev/null || echo "000")
          [ "$HTTP" = "201" ] && SHOPIFY_STATUS="created" || SHOPIFY_STATUS="create failed (HTTP $HTTP)"
        fi

        echo ""
        echo "┌────────────────────────────────────────────────────────────────────┐"
        echo "│  Shopify webhook: ${SHOPIFY_STATUS}"
        echo "│  ${WEBHOOK_URL}"
        echo "│                                                                    │"
        echo "│  ⚠  URL zmienia się przy każdym restarcie (auto-update działa)    │"
        echo "└────────────────────────────────────────────────────────────────────┘"
      else
        echo ""
        echo "┌────────────────────────────────────────────────────────────────────┐"
        echo "│  Shopify webhook URL (zaktualizuj ręcznie):                        │"
        echo "│  ${WEBHOOK_URL}"
        echo "│  (brak SHOPIFY_ACCESS_TOKEN w .env.local — auto-update wyłączony) │"
        echo "└────────────────────────────────────────────────────────────────────┘"
      fi
    else
      echo "⚠  cloudflared uruchomiony, ale URL jeszcze niedostępny"
      echo "   Sprawdź: grep trycloudflare $CLOUDFLARED_LOG"
    fi
  else
    echo "⚠  cloudflared nie znaleziony — test webhook z Shopify nie dotrze do API"
    echo "   Zainstaluj z: https://github.com/cloudflare/cloudflared"
  fi
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

trap 'kill $FRONTEND_PID 2>/dev/null; [ -n "${CLOUDFLARED_PID:-}" ] && kill "$CLOUDFLARED_PID" 2>/dev/null; exit' INT TERM
wait
