#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

echo
echo "============================================================"
echo " GridWise BUP Hackathon - Linux Docker"
echo " Frontend : http://127.0.0.1:1337/"
echo " API Docs : http://127.0.0.1:1337/docs"
echo " Health   : http://127.0.0.1:1337/health"
echo "============================================================"
echo

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed."
  echo "Install Docker Engine + Docker Compose plugin, then run ./run.sh again."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running or your user cannot access it."
  echo "Try: sudo systemctl start docker"
  echo "Or run this script with a Docker-enabled user."
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "ERROR: Docker Compose was not found."
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "ERROR: .env is missing."
  echo "This Linux package expects the .env that came with your uploaded project."
  echo "Copy .env.example to .env and add your Gemini API keys."
  exit 1
fi

# Confirm keys exist WITHOUT printing secret values.
KEY_COUNT="$(
python3 - <<'PY' 2>/dev/null || true
from pathlib import Path
import re
p = Path(".env")
text = p.read_text(encoding="utf-8-sig", errors="ignore")
vals = []
for line in text.splitlines():
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    k, v = s.split("=", 1)
    if k.strip() == "GEMINI_API_KEYS":
        vals.extend(x.strip() for x in re.split(r"[,;\s]+", v.strip()) if x.strip())
    elif k.strip() == "GEMINI_API_KEY" and v.strip():
        vals.append(v.strip())
print(len(dict.fromkeys(vals)))
PY
)"

if [[ -z "${KEY_COUNT}" ]]; then
  # python3 is not required on the host for Docker operation, so use a simple fallback.
  if grep -Eq '^[[:space:]]*GEMINI_API_KEYS=[^[:space:]]+' .env || \
     grep -Eq '^[[:space:]]*GEMINI_API_KEY=[^[:space:]]+' .env; then
    KEY_COUNT="configured"
  else
    KEY_COUNT="0"
  fi
fi

if [[ "${KEY_COUNT}" == "0" ]]; then
  echo "ERROR: Gemini API keys are missing from .env."
  echo "Set GEMINI_API_KEYS=key1,key2,..."
  exit 1
fi

echo "Gemini API key configuration: ${KEY_COUNT} key(s) detected."
echo "Model: gemini-3.5-flash-lite"
echo
echo "Building and starting the container..."
"${COMPOSE[@]}" up --build -d

echo
echo "Waiting for the API..."
READY=0
for i in $(seq 1 40); do
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS http://127.0.0.1:1337/health >/dev/null 2>&1; then
      READY=1
      break
    fi
  else
    STATUS="$(docker inspect --format='{{.State.Health.Status}}' gridwise-bup 2>/dev/null || true)"
    if [[ "${STATUS}" == "healthy" ]]; then
      READY=1
      break
    fi
  fi
  sleep 1
done

if [[ "${READY}" != "1" ]]; then
  echo
  echo "Container started but health check did not become ready."
  echo "Recent logs:"
  "${COMPOSE[@]}" logs --tail=80
  exit 1
fi

echo
echo "GridWise is READY."
echo
echo "Frontend : http://127.0.0.1:1337/"
echo "API Docs : http://127.0.0.1:1337/docs"
echo "Health   : http://127.0.0.1:1337/health"
echo
echo "API configuration check:"
if command -v curl >/dev/null 2>&1; then
  curl -fsS http://127.0.0.1:1337/ui-config || true
  echo
fi
echo
echo "Use ./logs.sh to view logs."
echo "Use ./stop.sh to stop the application."
