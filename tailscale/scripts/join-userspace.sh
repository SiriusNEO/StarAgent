#!/usr/bin/env bash
set -euo pipefail

SESSION="${TAILSCALE_TMUX_SESSION:-staragent-tailscaled}"
SOCKET="${TAILSCALE_SOCKET:-/tmp/staragent-tailscaled.sock}"
STATE_DIR="${TAILSCALE_STATE_DIR:-/var/lib/tailscale}"

run_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is required"
  exit 1
fi

if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

run_root mkdir -p "$STATE_DIR"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session already exists: $SESSION"
else
  tmux new -ds "$SESSION" "sudo tailscaled --tun=userspace-networking --socket=${SOCKET} --statedir=${STATE_DIR}"
  sleep 1
fi

run_root tailscale --socket="$SOCKET" up --ssh "$@"

echo
echo "Tailscale IPv4:"
run_root tailscale --socket="$SOCKET" ip -4
