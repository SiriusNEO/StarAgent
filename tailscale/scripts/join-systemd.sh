#!/usr/bin/env bash
set -euo pipefail

run_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

run_root systemctl enable --now tailscaled
run_root tailscale up --ssh "$@"

echo
echo "Tailscale IPv4:"
tailscale ip -4
