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

shell_quote() {
  printf "%q" "$1"
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

if [[ -z "$SOCKET" && -S /tmp/staragent-tailscaled.sock ]]; then
  SOCKET="/tmp/staragent-tailscaled.sock"
fi

ts=(tailscale)
if [[ -n "$SOCKET" ]]; then
  ts+=(--socket="$SOCKET")
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session already exists: $SESSION"
else
  env_prefix=""
  if [[ -n "${STARAGENT_NODE_TOKEN:-}" ]]; then
    env_prefix="STARAGENT_NODE_TOKEN=$(shell_quote "$STARAGENT_NODE_TOKEN") "
  elif [[ -n "${STARAGENT_AUTH_TOKEN:-}" ]]; then
    env_prefix="STARAGENT_AUTH_TOKEN=$(shell_quote "$STARAGENT_AUTH_TOKEN") "
  fi
  cmd="cd $(shell_quote "$STARAGENT_DIR") && ${env_prefix}$(shell_quote "$STARAGENT_CMD") node --host 127.0.0.1 --port $(shell_quote "$PORT")"
  tmux new -ds "$SESSION" "$cmd"
  sleep 1
fi

run_root "${ts[@]}" serve --bg --tcp="$PORT" "tcp://127.0.0.1:${PORT}"

echo
echo "Remote Node endpoint:"
run_root "${ts[@]}" ip -4 | sed "s/$/:${PORT}/"
