#!/usr/bin/env bash
# Manage the single shared ariadne MCP daemon (launchd: com.ariadne.mcp).
# One HTTP server on 127.0.0.1:8848 serves every Codex/Claude session, so no
# coding agent ever spawns its own stdio copy (that was leaking ~2.7 GB each).
#
# Requires: your Terminal app has Full Disk Access (the code + DB live under
# ~/Documents, which macOS TCC protects). Grant once in
# System Settings > Privacy & Security > Full Disk Access.
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.ariadne.mcp.plist"
LABEL="com.ariadne.mcp"
UID_="$(id -u)"
PORT=8848

start() {
  launchctl bootstrap "gui/$UID_" "$PLIST" 2>/dev/null || true
  launchctl kickstart -k "gui/$UID_/$LABEL"
  echo "kickstarted $LABEL; waiting for port $PORT..."
  for _ in $(seq 1 25); do
    if lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then echo "listening ✓"; return 0; fi
    sleep 1
  done
  echo "did NOT come up — check log:"; tail -20 "$HOME/Library/Logs/ariadne-mcp.log"; return 1
}

stop() { launchctl bootout "gui/$UID_/$LABEL" 2>/dev/null || true; echo "stopped $LABEL"; }

status() {
  launchctl print "gui/$UID_/$LABEL" 2>/dev/null | grep -iE 'state =|pid =|last exit' || echo "not loaded"
  lsof -iTCP:$PORT -sTCP:LISTEN 2>/dev/null | tail -1 || echo "port $PORT not listening"
}

health() {
  curl -s -N -X POST "http://127.0.0.1:$PORT/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"health","version":"0"}}}' \
    | head -c 400; echo
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  health) health ;;
  *) echo "usage: $0 {start|stop|restart|status|health}"; exit 1 ;;
esac
