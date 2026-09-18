# GridWise

GridWise is our BUP CSE Fest 2026 preliminary solution for the **Smart Campus Energy Optimization Challenge**.

## Architecture

```text
Browser UI
   ↓
FastAPI
   ↓
Gemini 3.5 Flash-Lite
   ↓
Deterministic Guardrails
   ↓
SciPy / HiGHS Optimizer
   ↓
24-hour Replay Validator
   ↓
JSON Response + Frontend
```

## Required endpoints

- `GET /health`
- `POST /optimize-energy`

The frontend is served from `/`.

## Gemini

The backend uses **Gemini 3.5 Flash-Lite** for operator-note interpretation.

Set these environment variables on the hosting platform:

```env
LLM_MODE=gemini
GEMINI_API_KEYS=your_key_1,your_key_2
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta
LLM_TIMEOUT_SECONDS=10
LLM_MAX_RETRIES=1
GEMINI_MAX_KEY_ATTEMPTS=5
```

Do **not** commit `.env`.

## Render deployment

Build command:

```bash
bash render_build.sh
```

Start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Local Docker

```bash
docker compose up --build
```

Then open:

```text
http://127.0.0.1:1337/
```

## Correctness

The service produces exactly one directive interpretation per operator note, deterministically validates model output, applies supported directives to the optimizer, and independently replays all 24 hours before returning a response.

The replay checks energy balance, effective solar, battery limits, charge/discharge rate limits, grid caps, totals, peak grid use, and end-of-day battery neutrality.
