#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PORT="${STARAGENT_NODE_PORT:-8081}"
SESSION="${STARAGENT_NODE_SESSION:-staragent-node}"
STARAGENT_DIR="${STARAGENT_DIR:-$REPO_DIR}"
STARAGENT_CMD="${STARAGENT_CMD:-staragent}"
SOCKET="${TAILSCALE_SOCKET:-}"

run_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

if [[ "$STARAGENT_CMD" == "staragent" && ! $(command -v staragent || true) && -x "${REPO_DIR}/.conda/bin/staragent" ]]; then
  STARAGENT_CMD="${REPO_DIR}/.conda/bin/staragent"
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is required"
  exit 1
fi

if ! command -v tailscale >/dev/null 2>&1; then
  echo "tailscale is required"
  exit 1
fi

if [[ -z "${STARAGENT_NODE_TOKEN:-}" && -z "${STARAGENT_AUTH_TOKEN:-}" ]]; then
  echo "STARAGENT_NODE_TOKEN is required before exposing a StarAgent node"
  echo "Use the same value on the Hub and this Remote Node."
  exit 1
fi

if [[ -z "$SOCKET" && -S "${REPO_DIR}/.staragent/tailscaled.sock" ]]; then
  SOCKET="${REPO_DIR}/.staragent/tailscaled.sock"
fi

ts=(tailscale)
if [[ -n "$SOCKET" ]]; then
  ts+=(--socket="$SOCKET")
fi

cmd=("$STARAGENT_CMD" node-ts --host 127.0.0.1 --port "$PORT" --session "$SESSION")
if [[ -n "$SOCKET" ]]; then
  cmd+=(--tailscale-socket "$SOCKET")
fi
if [[ "${EUID}" -ne 0 ]]; then
  cmd+=(--sudo)
fi

(cd "$STARAGENT_DIR" && "${cmd[@]}")

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "StarAgent node session did not start: $SESSION"
  exit 1
fi

echo
echo "Remote Node endpoint:"
run_root "${ts[@]}" ip -4 | sed "s/$/:${PORT}/"
