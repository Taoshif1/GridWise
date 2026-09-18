#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

if docker compose version >/dev/null 2>&1; then
  docker compose logs -f --tail=150
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose logs -f --tail=150
else
  echo "Docker Compose not found."
  exit 1
fi
