# GridWise — 3-Minute Architecture & Solution Video Script

Target duration: **2:40–2:55**.

## 0:00–0:20 — Problem

> Hello everyone. This is GridWise, our solution for the BUP CSE Fest 2026 Smart Campus Energy Optimization challenge. The service receives a 24-hour campus energy scenario and one to three natural-language operator notes. It must understand every note, apply any supported operational directive, and return a valid low-cost schedule.

Show the frontend and the live Render URL.

## 0:20–0:45 — Architecture

> Our pipeline separates language understanding from deterministic scheduling. FastAPI first validates the request. Gemini 3.5 Flash-Lite converts each operator note into a structured directive. Deterministic guardrails validate that output before it can affect the optimization model. SciPy HiGHS then computes the schedule, and an independent replay validator verifies all 24 hours before the API returns the result.

Show:

```text
FastAPI
  -> Gemini 3.5 Flash-Lite
  -> deterministic guardrails
  -> directive application
  -> SciPy / HiGHS optimizer
  -> 24-hour replay validator
  -> JSON response + frontend
```

## 0:45–1:15 — LLM interpretation

> The LLM is directly in the operator-note interpretation path. Every input note produces exactly one `directive_interpretation` entry in note-index order. For example, “Do not charge the battery between 2 PM and 4 PM” becomes a `no_charge_window` with hours 14 and 15. An irrelevant note becomes `no_op` with `applies=false` and a null adjustment.

Show a frontend operator note and the resulting directive card.

## 1:15–1:40 — Guardrails

> We treat the LLM output as untrusted. Deterministic validation checks the directive type, note mapping, exact structured-adjustment shape, hours, numeric ranges, and applies semantics. Unsupported or malformed model output is rejected instead of silently inventing a constraint.

Show `app/guardrails.py`.

## 1:40–2:05 — Optimization

> Validated directives become mathematical constraints. The optimizer minimizes the sum of hourly grid energy multiplied by tariff. It enforces effective solar, battery capacity and reserve, hourly charge and discharge limits, no-charge and no-discharge windows, grid caps, and end-of-day battery neutrality.

Show the result cards and chart.

## 2:05–2:30 — Final replay

> Before the result leaves the API, we independently replay all 24 hours. We verify energy balance, solar limits, battery transitions, reserves, rate limits, directive-specific constraints, and the final battery state. We also recalculate total grid energy, total cost, and peak grid use from the returned plan.

Show `app/validator.py` and the hourly table.

## 2:30–2:52 — Deployment and reproduction

> The required public endpoints are `GET /health` and `POST /optimize-energy`. The live service is deployed on Render. The repository also includes Docker fallback instructions and a GitHub Actions workflow for publishing the fallback image to GHCR. Secrets are supplied only through runtime environment variables and are not baked into the image.

Show:
- https://gridwise-bup.onrender.com/health
- https://gridwise-bup.onrender.com/docs
- GitHub README
- Docker fallback section

## 2:52–2:58 — Closing

> GridWise uses the LLM for language understanding, deterministic code for safety and correctness, and mathematical optimization for the final schedule. Thank you.
