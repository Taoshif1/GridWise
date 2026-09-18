# Windows build

For the Windows frontend version, just double-click:

```text
RUN_GRIDWISE_WINDOWS.bat
```

The frontend opens at:

```text
http://127.0.0.1:1337/
```

For API-key setup, use:

```text
CONFIGURE_GEMINI_KEYS.bat
```

See `WINDOWS_README.md` for the Windows-specific guide.

---

# GridWise — Linux Docker + Gemini 3.5 Flash-Lite + Frontend

This package contains the full GridWise hackathon service:

```text
Browser frontend
      |
      v
FastAPI POST /optimize-energy
      |
      v
Google Gemini 3.5 Flash-Lite
      |
      v
Deterministic directive guardrails
      |
      v
SciPy/HiGHS linear optimizer
      |
      v
Independent 24-hour replay validator
      |
      v
User-friendly result + exact JSON API
```

The required judging endpoints remain exactly:

```text
GET  /health
POST /optimize-energy
```

The UI is an additional convenience layer at `/` and does not replace the judge API.

## Gemini

Default model:

```text
gemini-3.5-flash-lite
```

The backend calls the Google Gemini `generateContent` API directly. Your API key stays server-side in `.env`; it is never sent to the browser and `.dockerignore` excludes `.env` from the image.

## Linux quick start

Requirements:

```text
Docker Engine
Docker Compose v2 (`docker compose`)
```

Extract the project and enter it:

```bash
cd GridWise_BUP_Hackathon_Linux_Docker_Gemini_UI
```

Run:

```bash
bash run.sh
```

On first run, the script asks for your Gemini API key, builds the Docker image, starts it, waits for `/health`, and prints the URLs.

Open:

```text
Frontend : http://127.0.0.1:1337/
API Docs : http://127.0.0.1:1337/docs
Health   : http://127.0.0.1:1337/health
```

## Manual setup

```bash
cp .env.example .env
nano .env
```

Set:

```env
LLM_MODE=gemini
GEMINI_API_KEY=YOUR_KEY
GEMINI_MODEL=gemini-3.5-flash-lite
PORT=1337
```

Then:

```bash
docker compose up --build -d
```

Check:

```bash
curl http://127.0.0.1:1337/health
```

Logs:

```bash
bash logs.sh
```

Stop:

```bash
bash stop.sh
```

## Frontend

The frontend lets a normal user:

- load a ready sample scenario,
- enter 1–3 operator notes,
- edit battery parameters,
- edit all 24 hours of demand/solar/tariff data,
- import or export scenario JSON,
- run the optimizer,
- see total cost, total grid and peak grid,
- read Gemini's interpreted directives,
- inspect a visual 24-hour chart,
- inspect the hourly schedule,
- download the exact response JSON.

No Node/npm build is required. The frontend is plain HTML/CSS/JavaScript served by FastAPI.

## Offline UI/API testing

If you temporarily have no Gemini API key, change `.env` to:

```env
LLM_MODE=demo
```

That uses a deterministic development parser so you can test the interface and optimizer. **Do not submit in demo mode.** Final judging must use `LLM_MODE=gemini`.

## Security

Never commit `.env`. The key is read only inside the backend container.

The image intentionally excludes `.env` through `.dockerignore`.

## Submission Docker image

Build locally:

```bash
docker build -t gridwise-bup:latest .
```

Run:

```bash
docker run --rm --env-file .env -p 1337:1337 gridwise-bup:latest
```

The repository also contains a GitHub Actions workflow that can publish the image to GHCR without storing the Gemini key in the image.
