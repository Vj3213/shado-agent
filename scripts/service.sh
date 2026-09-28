#!/usr/bin/env bash
# Manage Shado Agent as macOS launchd services (start at boot, auto-restart).
#
#   bash scripts/service.sh install     # stop manual runs first, then install + start
#   bash scripts/service.sh uninstall   # stop + remove services
#   bash scripts/service.sh status      # are they running?
#   bash scripts/service.sh logs        # tail all logs (Ctrl+C to exit)
#
# Three services: com.shado.postgres · com.shado.agent · com.shado.gateway
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
GATEWAY="$PROJECT/gateway"
LOGS="$HOME/Library/Logs/shado"
LAUNCH="$HOME/Library/LaunchAgents"
PG_BIN="$(cd /usr/local/opt/postgresql@15/bin && pwd)"
PG_DATA="/usr/local/var/postgresql@15"
NODE_BIN="$(dirname "$(readlink -f "$(which node)")" 2>/dev/null || echo /usr/local/opt/node-lts/bin)"

render() { # template → real plist, substituting machine paths
  sed -e "s|__PROJECT__|$PROJECT|g" \
      -e "s|__GATEWAY__|$GATEWAY|g" \
      -e "s|__LOGS__|$LOGS|g" \
      -e "s|__PG_BIN__|$PG_BIN|g" \
      -e "s|__PG_DATA__|$PG_DATA|g" \
      -e "s|__NODE_BIN__|$NODE_BIN|g" \
      "$PROJECT/service/$1" > "$LAUNCH/${1%.tpl}"
}

load() { launchctl unload "$LAUNCH/$1" 2>/dev/null || true; launchctl load "$LAUNCH/$1"; }

case "${1:-}" in
  install)
    echo "Stopping any manual instance on :8100 first..."
    lsof -ti:8100 | xargs kill -9 2>/dev/null || true
    pkill -f "tsx src/index.ts" 2>/dev/null || true
    mkdir -p "$LOGS" "$LAUNCH"
    render com.shado.postgres.plist.tpl
    render com.shado.agent.plist.tpl
    render com.shado.gateway.plist.tpl
    for svc in postgres agent gateway; do
      plutil -lint "$LAUNCH/com.shado.$svc.plist" > /dev/null && echo "✔ com.shado.$svc plist valid"
      load "com.shado.$svc.plist"
    done
    sleep 2
    "$0" status
    echo
    echo "Logs: $LOGS — stop command: bash scripts/service.sh uninstall"
    echo "To keep the bot alive when you walk away, also prevent system sleep:"
    echo "  sudo pmset -a sleep 0        (display may still sleep; system won't)"
    ;;
  uninstall)
    for svc in postgres agent gateway; do
      launchctl unload "$LAUNCH/com.shado.$svc.plist" 2>/dev/null || true
      rm -f "$LAUNCH/com.shado.$svc.plist"
      echo "✔ com.shado.$svc removed"
    done
    ;;
  status)
    launchctl list | grep com.shado || echo "No shado services loaded."
    pg_isready -q -h 127.0.0.1 -p 5432 && echo "✔ PostgreSQL accepting connections" || echo "✘ PostgreSQL down"
    # The agent takes a few seconds to bind after a restart — retry before
    # declaring it down so a fresh restart never shows a false negative.
    healthy=""
    for _ in 1 2 3; do
      if curl -s --max-time 2 http://127.0.0.1:8100/health | grep -q ok; then
        healthy=1
        break
      fi
      sleep 2
    done
    [ -n "$healthy" ] && echo "✔ Agent healthy" || echo "✘ Agent down"
    ;;
  logs)
    tail -f "$LOGS/gateway.log" "$LOGS/agent.log" "$LOGS/postgres.log"
    ;;
  restart)
    for svc in postgres agent gateway; do
      load "com.shado.$svc.plist"
      echo "✔ com.shado.$svc restarted"
    done
    "$0" status
    ;;
  *)
    grep -E "^#   bash" "$0" | sed 's/^# *//'
    ;;
esac
