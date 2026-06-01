#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" == "systemd" ]] && command -v systemctl >/dev/null 2>&1; then
  "${SCRIPT_DIR}/join-systemd.sh" "$@"
else
  "${SCRIPT_DIR}/join-userspace.sh" "$@"
fi

"${SCRIPT_DIR}/expose-agent.sh"
"${SCRIPT_DIR}/status.sh"
