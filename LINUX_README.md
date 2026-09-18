# GridWise Linux Docker + Gemini + Frontend

This package was converted directly from your uploaded Windows project.

Your existing `.env` is preserved in this package so the Gemini configuration is available to Docker at runtime.

## Run

```bash
chmod +x run.sh stop.sh logs.sh rebuild.sh
./run.sh
```

Docker Compose reads:

```text
.env
```

through:

```yaml
env_file:
  - .env
```

So the Gemini keys are injected into the backend container at runtime. They are **not copied into the Docker image**.

## Open

Frontend:

```text
http://127.0.0.1:1337/
```

Swagger:

```text
http://127.0.0.1:1337/docs
```

Health:

```text
http://127.0.0.1:1337/health
```

UI configuration status:

```text
http://127.0.0.1:1337/ui-config
```

The `/ui-config` response shows whether keys were detected and the configured key count. It never returns the key values.

## Useful commands

Logs:

```bash
./logs.sh
```

Stop:

```bash
./stop.sh
```

Force a clean rebuild:

```bash
./rebuild.sh
```

Direct Compose commands:

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f
docker compose down
```

## If the UI says API key missing

Run:

```bash
docker compose exec gridwise python -c "from app.config import get_settings; s=get_settings(); print('mode=',s.llm_mode,'keys=',len(s.gemini_api_keys),'model=',s.gemini_model)"
```

Expected output should look like:

```text
mode= gemini keys= 4 model= gemini-3.5-flash-lite
```

The exact key count depends on how many unique keys are present.

Then check:

```bash
curl http://127.0.0.1:1337/ui-config
```

You should see:

```json
{
  "model": "gemini-3.5-flash-lite",
  "mode": "gemini",
  "api_ready": true,
  "configured_key_count": 4,
  "port": 1337
}
```

If you edit `.env`, restart the container because environment variables are loaded when the container starts:

```bash
docker compose down
docker compose up -d
```

or:

```bash
./rebuild.sh
```

## Security

This package contains the `.env` from your uploaded project.

Do not:
- commit `.env` to Git,
- upload this package publicly,
- share it with people who should not receive the API keys.

`.gitignore` and `.dockerignore` already exclude `.env`.
