#!/usr/bin/env bash
# Reusable test: agent HTTP flow in DRY_RUN (gateway not needed).
# Usage: bash scripts/test_agent_flow.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="/usr/local/opt/postgresql@15/bin:$PATH"
export GROUP_JID="120363test@g.us"
export PEER_BOT_SENDER="919999000111@s.whatsapp.net"
export BOT_TO_BOT_MAX_TURNS=2
export AGENT_DRY_RUN=1

psql -d meal_agent -q -c "TRUNCATE messages, meal_log, bot_loop_state;"

.venv/bin/python -m uvicorn agent.app.main:app --host 127.0.0.1 --port 8100 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for _ in $(seq 1 15); do curl -s --max-time 1 http://127.0.0.1:8100/health | grep -q ok && break; sleep 1; done

post() { curl -s -X POST "http://127.0.0.1:8100/messages/incoming" -H 'content-type: application/json' -d "$1"; echo; }
HUMAN='{"group_id":"120363test@g.us","sender":"918888877777@s.whatsapp.net","text":"ved: kya breakfast banau? hungry yaar","timestamp":"2026-09-08T09:00:00Z"}'
PEER='{"group_id":"120363test@g.us","sender":"919999000111@s.whatsapp.net","text":"Suggestion: try aloo paratha today!","timestamp":"2026-09-08T09:05:00Z"}'

echo "1) human prefix  -> expect trigger_prefix reply"; post "$HUMAN"
echo "2) no trigger    -> expect no_trigger"; post '{"group_id":"120363test@g.us","sender":"918888877777@s.whatsapp.net","text":"good morning all","timestamp":"2026-09-08T09:10:00Z"}'
echo "3) peer #1       -> expect reply (turns 1)"; post "$PEER"
echo "4) peer #2       -> expect reply (turns 2)"; post "$PEER"
echo "5) peer #3       -> expect bot_loop_cap_reached"; post "$PEER"
echo "6) human         -> expect reply (resets cap)"; post "$HUMAN"
echo "7) peer #4       -> expect reply again"; post "$PEER"
echo "8) wrong group   -> expect 403"
curl -s -X POST http://127.0.0.1:8100/messages/incoming -H 'content-type: application/json' \
  -d '{"group_id":"999@g.us","sender":"x@s.whatsapp.net","text":"hi","timestamp":"2026-09-08T09:30:00Z"}'; echo

echo; echo "DB state:"
psql -d meal_agent -c "SELECT direction, count(*) FROM messages GROUP BY direction; SELECT item, meal_type FROM meal_log; SELECT consecutive_turns FROM bot_loop_state;"
