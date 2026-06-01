#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-}"
PORT="${STARAGENT_NODE_PORT:-8081}"

if [[ -z "$TARGET" ]]; then
  echo "usage: $0 <node|ip|url>"
  echo "example: $0 100.118.196.66"
  exit 2
fi

if [[ "$TARGET" != http://* && "$TARGET" != https://* ]]; then
  if [[ "$TARGET" == *:* ]]; then
    TARGET="http://${TARGET}"
  else
    TARGET="http://${TARGET}:${PORT}"
  fi
fi

TARGET="${TARGET%/}"

echo "checking ${TARGET}/api/health"
curl -fsS "${TARGET}/api/health"
echo
