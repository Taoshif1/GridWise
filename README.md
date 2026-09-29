# GridWise FuelOps Intelligence

**BUP CSE Fest 2026 Hackathon Finals**

GridWise is an operator-facing fuel-supply decision-support platform built on top of the official **BUP Fuel Supply Simulator**. It observes the simulator through REST/SSE, predicts station demand and stockout risk, generates constrained fuel-allocation recommendations, lets an operator apply decisions, survives injected failures, and exposes operational telemetry.

## Finals architecture

```text
Official BUP Fuel Supply Simulator
  REST + SSE + /admin self-test controls
                |
                v
GridWise FastAPI Backend
  resilient simulator client
  demand forecasting
  stockout detection
  LP allocation optimizer
  decision/audit layer
  telemetry + degraded mode
                |
                v
Operator Command Center
  overview | network | decisions | crisis lab | observability
```

## Intelligence

The prediction layer uses recent demand history with an exponentially weighted moving average plus a bounded trend estimate. It calculates coverage, stockout probability, and forecast confidence for every station/fuel pair.

The decision engine uses SciPy HiGHS linear programming to allocate fuel while respecting depot inventory, per-tick dispatch capacity, route availability/max shipment, station status/capacity, in-flight allocations, and predicted demand. Every recommendation shows the reason, before/after risk, ETA, confidence, and coverage change.

## Resilience and observability

GridWise retries transient simulator errors with backoff, uses cached state in degraded mode, detects the simulator stale-data header, reconnects SSE automatically, and uses a small circuit breaker after repeated failures. The Crisis Lab can inject domain events and engineering faults through the simulator's official admin API.

`/api/health` shows component health. `/api/observability` exposes application telemetry and decision events. `/metrics` emits Prometheus-compatible counters/gauges including request count, error rate, p95 latency, SSE reconnects, degraded reads, simulator failures, and allocation outcomes.

## Run locally

```bash
docker compose up --build
```

Open:
- GridWise: http://localhost:8080
- Simulator admin: http://localhost:8000/admin
- GridWise API docs: http://localhost:8080/docs

The Compose stack uses the organizer image `asifmahmoud414/bup-fuel-supply-simulator:1.0.0`.

## Main API

| Endpoint | Purpose |
|---|---|
| `GET /api/dashboard` | Full operator state, intelligence, risk, telemetry |
| `POST /api/recommendations` | Recompute constrained plan |
| `POST /api/allocations/apply` | Submit one allocation |
| `POST /api/allocations/apply-bulk` | Apply current plan |
| `POST /api/admin/run|pause|step|reset` | Demo controls |
| `POST /api/admin/events` | Inject crisis |
| `POST /api/admin/faults` | Inject engineering fault |
| `GET /api/observability` | Health and operational events |
| `GET /metrics` | Prometheus text metrics |

## Judge demo flow

1. Reset, show the normal network state.
2. Run/step the simulator and show demand history feeding forecasts.
3. Inject a demand spike or route disruption.
4. Show the shortage radar and explain one recommendation.
5. Apply the allocation and advance ticks.
6. Show allocation status/history and service-level impact.
7. Inject latency, stale data, or SSE disconnect.
8. Show degraded/recovery behavior and observability.
9. Open `/metrics` and `/api/health`.

## Configuration

| Variable | Default |
|---|---|
| `SIMULATOR_URL` | `http://simulator:8000` |
| `SIMULATOR_TIMEOUT_SECONDS` | `3` |
| `SIMULATOR_MAX_RETRIES` | `3` |
| `CACHE_TTL_SECONDS` | `1` |
| `FORECAST_HISTORY_LIMIT` | `768` |

This application controls only the supplied simulation environment. It does not connect to real fuel infrastructure or operational systems.
