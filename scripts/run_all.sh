#!/usr/bin/env bash
# Start everything for local development: Postgres (if needed) + agent + gateway.
#
# Phases, chosen automatically from .env:
#   discovery — GROUP_JID or PEER_BOT_SENDER still TODO/unset:
#               starts only Postgres + gateway so you can discover JIDs
#   full      — .env complete: starts the agent too and goes live
#
# Ctrl+C stops the agent and gateway; Postgres keeps running (harmless).
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="/usr/local/opt/node-lts/bin:/usr/local/opt/postgresql@15/bin:$PATH"

unconfigured() { # name — true if var missing or still the TODO placeholder
  grep -qE "^$1=(TODO)?$" .env 2>/dev/null
}

DISCOVERY=0
if unconfigured GROUP_JID; then
  DISCOVERY=1
  cat <<'EOF'
══════════════════════════════════════════════════════════════════
 DISCOVERY PHASE — GROUP_JID not configured yet.
 Starting Postgres + gateway only (agent not needed for discovery).

  1. Scan the QR below with the SPARE phone (WhatsApp > Linked devices).
  2. Send any message in the target GROUP (or have someone do it).
     Note: group chats only — 1:1 DMs are ignored by design.
  3. Watch for:  [discovery] group seen: 123...@g.us
     → copy that JID into .env as GROUP_JID and re-run this script.
══════════════════════════════════════════════════════════════════
EOF
else
  # GROUP_JID known → the agent is needed. PEER_BOT_SENDER may still be
  # TODO; the agent then runs with peer-bot logic dormant.
  if unconfigured GEMINI_API_KEY && [ -z "${AGENT_DRY_RUN:-}" ]; then
    echo "⚠ GEMINI_API_KEY not set — starting agent in DRY_RUN (canned replies)."
    echo "  Add your key to .env for real AI replies, or set AGENT_DRY_RUN=1 to silence this."
    export AGENT_DRY_RUN=1
  fi
fi

# 1. Postgres — start only if it isn't already accepting connections.
if ! pg_isready -q -h 127.0.0.1 -p 5432; then
  echo "▶ Starting PostgreSQL..."
  LC_ALL="C" postgres -D /usr/local/var/postgresql@15 &
  for _ in $(seq 1 10); do pg_isready -q -h 127.0.0.1 -p 5432 && break; sleep 1; done
fi
echo "✔ PostgreSQL ready"

# 1b. Keep DB schema + food KB current (idempotent, safe every start).
echo "▶ Applying schema + food KB seed..."
.venv/bin/python scripts/init_db.py

if [ "$DISCOVERY" -eq 0 ]; then
  # 2. Agent — background; .env supplies GEMINI_API_KEY / AGENT_DRY_RUN etc.
  #    Clear any stale instance first (crashed runs can leave port 8100 bound).
  lsof -ti:8100 | xargs kill -9 2>/dev/null || true
  echo "▶ Starting agent on http://127.0.0.1:8100 ..."
  .venv/bin/python -m uvicorn agent.app.main:app --host 127.0.0.1 --port 8100 &
  AGENT_PID=$!
  trap 'kill $AGENT_PID 2>/dev/null || true' EXIT
  for _ in $(seq 1 15); do curl -s --max-time 1 http://127.0.0.1:8100/health | grep -q ok && break; sleep 1; done
  curl -s http://127.0.0.1:8100/health | grep -q ok || { echo "✘ agent failed to start — see error above"; exit 1; }
  echo "✔ Agent ready"
fi

# 3. Gateway — foreground so you can scan the QR and watch logs live.
#    (Discovery phase: no GROUP_JID → gateway logs group JIDs, forwards nothing.)
echo "▶ Starting gateway (Ctrl+C to stop)..."
cd gateway && exec npm run dev
