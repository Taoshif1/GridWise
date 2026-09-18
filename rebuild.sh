#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

if docker compose version >/dev/null 2>&1; then
  docker compose down
  docker compose build --no-cache
  docker compose up -d
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose down
  docker-compose build --no-cache
  docker-compose up -d
else
  echo "Docker Compose not found."
  exit 1
fi
echo "Rebuild complete: http://127.0.0.1:1337/"
