#!/usr/bin/env bash
set -euo pipefail

SOCKET="${TAILSCALE_SOCKET:-}"
PORT="${STARAGENT_NODE_PORT:-8081}"

if [[ -z "$SOCKET" && -S /tmp/staragent-tailscaled.sock ]]; then
  SOCKET="/tmp/staragent-tailscaled.sock"
fi

if ! command -v tailscale >/dev/null 2>&1; then
  echo "tailscale: not installed"
  exit 1
fi

ts=(tailscale)
if [[ -n "$SOCKET" ]]; then
  ts+=(--socket="$SOCKET")
fi

echo "== Tailscale =="
"${ts[@]}" status || true

echo
echo "== Tailscale IP =="
"${ts[@]}" ip -4 || true

echo
echo "== Serve =="
"${ts[@]}" serve status || true

echo
echo "== Local StarAgent node =="
if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/tmp/staragent-node-health.json 2>/dev/null; then
  cat /tmp/staragent-node-health.json
  echo
else
  echo "not reachable at http://127.0.0.1:${PORT}/api/health"
fi
