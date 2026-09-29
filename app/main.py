from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, AsyncIterator

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from scipy.optimize import linprog

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
SIMULATOR_URL = os.getenv("SIMULATOR_URL", "http://simulator:8000").rstrip("/")
TIMEOUT = float(os.getenv("SIMULATOR_TIMEOUT_SECONDS", "3"))
MAX_RETRIES = int(os.getenv("SIMULATOR_MAX_RETRIES", "3"))
CACHE_TTL = float(os.getenv("CACHE_TTL_SECONDS", "1"))
FORECAST_LIMIT = int(os.getenv("FORECAST_HISTORY_LIMIT", "768"))
FUELS = ("DIESEL", "PETROL", "OCTANE")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Telemetry:
    def __init__(self) -> None:
        self.samples = deque(maxlen=3000)
        self.events = deque(maxlen=1000)
        self.sse_reconnects = 0
        self.cache_hits = 0
        self.degraded_reads = 0
        self.simulator_failures = 0
        self.allocations_submitted = 0
        self.allocations_failed = 0
        self.last_sse_event: dict[str, Any] | None = None

    def request(self, path: str, method: str, status: int, latency_ms: float) -> None:
        self.samples.append((path, method, status, latency_ms, utc_now()))

    def event(self, kind: str, message: str, **data: Any) -> None:
        self.events.appendleft({"at": utc_now(), "kind": kind, "message": message, "data": data})

    def snapshot(self) -> dict[str, Any]:
        values = sorted(x[3] for x in self.samples)
        count = len(values)
        errors = sum(1 for x in self.samples if x[2] >= 500)

        def pct(p: float) -> float:
            if not values:
                return 0.0
            return round(values[min(len(values) - 1, round((len(values) - 1) * p))], 2)

        return {
            "request_count": count,
            "error_rate": round(errors / count, 4) if count else 0.0,
            "latency_ms": {
                "p50": pct(0.50),
                "p95": pct(0.95),
                "p99": pct(0.99),
                "median": round(median(values), 2) if values else 0.0,
            },
            "sse_reconnects": self.sse_reconnects,
            "cache_hits": self.cache_hits,
            "degraded_reads": self.degraded_reads,
            "simulator_failures": self.simulator_failures,
            "allocations_submitted": self.allocations_submitted,
            "allocations_failed": self.allocations_failed,
            "last_sse_event": self.last_sse_event,
        }


telemetry = Telemetry()


class SimulatorError(RuntimeError):
    def __init__(self, message: str, status_code: int = 503, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class SimulatorClient:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(timeout=TIMEOUT)
        self.cache: dict[str, tuple[Any, float, bool]] = {}
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def breaker_state(self) -> str:
        if self.opened_at is None:
            return "CLOSED"
        if time.monotonic() - self.opened_at >= 5:
            return "HALF_OPEN"
        return "OPEN"

    def _success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def _failure(self) -> None:
        self.failures += 1
        if self.failures >= 4:
            self.opened_at = time.monotonic()

    async def close(self) -> None:
        await self.http.aclose()

    async def get(self, path: str, cached_fallback: bool = True) -> tuple[Any, dict[str, Any]]:
        key = f"GET:{path}"
        now = time.monotonic()
        cached = self.cache.get(key)
        if cached and now - cached[1] <= CACHE_TTL:
            telemetry.cache_hits += 1
            return cached[0], {"cached": True, "degraded": False, "stale": cached[2]}

        if self.breaker_state == "OPEN":
            if cached_fallback and cached:
                telemetry.degraded_reads += 1
                return cached[0], {"cached": True, "degraded": True, "stale": True}
            raise SimulatorError("Simulator circuit breaker is open")

        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            start = time.perf_counter()
            try:
                r = await self.http.get(f"{SIMULATOR_URL}{path}")
                telemetry.request(path, "GET", r.status_code, (time.perf_counter() - start) * 1000)

                if r.status_code >= 500:
                    raise SimulatorError(
                        f"Simulator returned HTTP {r.status_code}",
                        r.status_code,
                        self._safe_json(r),
                    )
                if r.status_code >= 400:
                    self._success()
                    raise SimulatorError(
                        f"Simulator rejected HTTP {r.status_code}",
                        r.status_code,
                        self._safe_json(r),
                    )

                payload = self._safe_json(r)
                stale = r.headers.get("X-Simulator-Stale", "").lower() == "true"
                self.cache[key] = (payload, time.monotonic(), stale)
                self._success()
                return payload, {"cached": False, "degraded": False, "stale": stale}
            except SimulatorError as exc:
                last = exc
                if exc.status_code < 500:
                    raise
                self._failure()
                telemetry.simulator_failures += 1
            except (httpx.RequestError, asyncio.TimeoutError) as exc:
                telemetry.request(path, "GET", 599, (time.perf_counter() - start) * 1000)
                last = exc
                self._failure()
                telemetry.simulator_failures += 1

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(0.15 * (2**attempt))

        if cached_fallback and cached:
            telemetry.degraded_reads += 1
            return cached[0], {"cached": True, "degraded": True, "stale": True}
        if isinstance(last, SimulatorError):
            raise last
        raise SimulatorError(f"Simulator request failed: {last}")

    async def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            start = time.perf_counter()
            try:
                r = await self.http.post(f"{SIMULATOR_URL}{path}", json=body or {})
                telemetry.request(path, "POST", r.status_code, (time.perf_counter() - start) * 1000)

                if r.status_code >= 500:
                    raise SimulatorError(
                        f"Simulator returned HTTP {r.status_code}",
                        r.status_code,
                        self._safe_json(r),
                    )
                if r.status_code >= 400:
                    self._success()
                    raise SimulatorError(
                        f"Simulator rejected HTTP {r.status_code}",
                        r.status_code,
                        self._safe_json(r),
                    )

                self.cache.clear()
                self._success()
                return self._safe_json(r)
            except SimulatorError as exc:
                last = exc
                if exc.status_code < 500:
                    raise
                self._failure()
                telemetry.simulator_failures += 1
            except (httpx.RequestError, asyncio.TimeoutError) as exc:
                telemetry.request(path, "POST", 599, (time.perf_counter() - start) * 1000)
                last = exc
                self._failure()
                telemetry.simulator_failures += 1

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(0.15 * (2**attempt))

        if isinstance(last, SimulatorError):
            raise last
        raise SimulatorError(f"Simulator request failed: {last}")

    @staticmethod
    def _safe_json(r: httpx.Response) -> Any:
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    async def snapshot(self) -> dict[str, Any]:
        paths = {
            "health": "/v1/health",
            "instance": "/v1/instance",
            "regions": "/v1/regions",
            "depots": "/v1/depots",
            "stations": "/v1/stations",
            "routes": "/v1/routes",
            "supply_arrivals": "/v1/supply-arrivals",
            "events": "/v1/events",
            "allocations": "/v1/allocations",
            "demand_history": f"/v1/demand-history?limit={FORECAST_LIMIT}",
            "metrics": "/v1/metrics",
        }
        results = await asyncio.gather(
            *(self.get(path) for path in paths.values()),
            return_exceptions=True,
        )
        out: dict[str, Any] = {
            "meta": {
                "degraded": False,
                "stale": False,
                "errors": [],
                "circuit_breaker": self.breaker_state,
            }
        }
        for (name, _), result in zip(paths.items(), results):
            if isinstance(result, Exception):
                out[name] = None
                out["meta"]["degraded"] = True
                out["meta"]["errors"].append({"resource": name, "error": str(result)})
            else:
                payload, meta = result
                out[name] = payload
                out["meta"]["degraded"] |= bool(meta.get("degraded"))
                out["meta"]["stale"] |= bool(meta.get("stale"))
        out["meta"]["circuit_breaker"] = self.breaker_state
        return out

    async def stream(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        async with self.http.stream("GET", f"{SIMULATOR_URL}/v1/stream", timeout=None) as r:
            if r.status_code >= 400:
                raw = await r.aread()
                try:
                    payload = json.loads(raw.decode())
                except Exception:
                    payload = {"raw": raw.decode(errors="replace")}
                raise SimulatorError(f"SSE HTTP {r.status_code}", r.status_code, payload)

            event = "message"
            data: list[str] = []
            async for line in r.aiter_lines():
                if line.startswith(":"):
                    continue
                if not line:
                    if data:
                        raw = "\n".join(data)
                        try:
                            obj = json.loads(raw)
                        except Exception:
                            obj = {"raw": raw}
                        yield event, obj
                    event = "message"
                    data = []
                    continue
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data.append(line.split(":", 1)[1].strip())


sim = SimulatorClient()


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30, min(30, x))))


def forecast(
    history: list[dict[str, Any]] | None,
    stations: list[dict[str, Any]] | None,
    horizon: int = 16,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    for row in history or []:
        try:
            grouped[(row["station_id"], row["fuel_type"])].append(
                (int(row["tick"]), float(row["demand_liters"]))
            )
        except Exception:
            pass

    out = []
    for station in stations or []:
        for fuel in FUELS:
            points = sorted(grouped.get((station.get("id"), fuel), []))[-96:]
            values = np.array([value for _, value in points], dtype=float)

            if len(values) >= 2:
                ewma = float(values[0])
                for value in values[1:]:
                    ewma = 0.28 * float(value) + 0.72 * ewma
                trend = (
                    float(np.polyfit(np.arange(len(values), dtype=float), values, 1)[0])
                    if len(values) >= 6
                    else 0.0
                )
                trend = float(np.clip(trend, -0.25 * max(ewma, 1), 0.25 * max(ewma, 1)))
                per_tick = max(0.0, ewma + max(0.0, trend) * 2)
                confidence = max(
                    0.35,
                    min(
                        0.98,
                        1 - float(np.std(values)) / max(ewma * 3, 1),
                    ),
                )
            elif len(values) == 1:
                per_tick = float(values[0])
                trend = 0.0
                confidence = 0.45
            else:
                per_tick = 0.0
                trend = 0.0
                confidence = 0.2

            inventory = float((station.get("inventory") or {}).get(fuel, 0) or 0)
            coverage = inventory / per_tick if per_tick > 0.001 else 9999.0
            probability = _sigmoid(
                (horizon - coverage) / max(horizon * 0.22, 1)
            )
            out.append(
                {
                    "station_id": station.get("id"),
                    "fuel_type": fuel,
                    "liters_per_tick": round(per_tick, 3),
                    "trend_per_tick": round(trend, 3),
                    "horizon_demand": round(per_tick * horizon, 1),
                    "coverage_ticks": round(coverage, 2),
                    "stockout_probability": round(probability, 4),
                    "confidence": round(confidence, 4),
                }
            )
    return out


def optimize(
    snapshot: dict[str, Any],
    horizon: int = 16,
    safety: int = 8,
) -> dict[str, Any]:
    stations = snapshot.get("stations") or []
    depots = snapshot.get("depots") or []
    routes = snapshot.get("routes") or []
    allocations = snapshot.get("allocations") or []

    forecasts = forecast(snapshot.get("demand_history"), stations, horizon)
    forecast_map = {
        (item["station_id"], item["fuel_type"]): item for item in forecasts
    }

    inbound: dict[tuple[str, str], float] = defaultdict(float)
    for allocation in allocations:
        if allocation.get("status") in {"PENDING", "IN_TRANSIT"}:
            inbound[
                (
                    allocation.get("destination_station_id"),
                    allocation.get("fuel_type"),
                )
            ] += float(allocation.get("quantity", 0) or 0)

    depot_map = {depot["id"]: depot for depot in depots}
    station_map = {station["id"]: station for station in stations}
    usable_routes = [
        route
        for route in routes
        if route.get("status") == "AVAILABLE"
        and route.get("source_depot_id") in depot_map
        and route.get("destination_station_id") in station_map
    ]

    variables: list[dict[str, Any]] = []
    for route in usable_routes:
        depot = depot_map[route["source_depot_id"]]
        station = station_map[route["destination_station_id"]]
        if depot.get("status") not in {"OPEN", "CONSTRAINED"}:
            continue
        if station.get("status") != "OPEN":
            continue

        for fuel in FUELS:
            item = forecast_map.get((station["id"], fuel))
            if not item:
                continue
            inventory = float((station.get("inventory") or {}).get(fuel, 0) or 0)
            capacity = float((station.get("capacity") or {}).get(fuel, 0) or 0)
            incoming = inbound[(station["id"], fuel)]
            deficit = max(
                0,
                item["liters_per_tick"] * (horizon + safety)
                - inventory
                - incoming,
            )
            room = max(0, capacity - inventory - incoming)
            max_candidate = min(
                deficit,
                room,
                float(route.get("max_shipment", 0) or 0),
            )
            if max_candidate < 1:
                continue

            priority = (
                8 * float(item["stockout_probability"])
                + 2 / max(float(item["coverage_ticks"]), 0.5)
                + 0.25
                * max(0, float(item["trend_per_tick"]))
                / max(float(item["liters_per_tick"]), 1)
            )
            variables.append(
                {
                    "route": route,
                    "fuel": fuel,
                    "max": max_candidate,
                    "priority": priority,
                    "cost": -(
                        priority
                        - 0.08 * float(route.get("transit_ticks", 1) or 1)
                    ),
                    "forecast": item,
                }
            )

    if not variables:
        return {
            "solver": "scipy-highs",
            "status": "NO_ACTION",
            "forecast": forecasts,
            "recommendations": [],
            "objective": 0.0,
        }

    constraints = []
    limits = []

    for depot in depots:
        for fuel in FUELS:
            row = [
                1.0
                if variable["route"]["source_depot_id"] == depot["id"]
                and variable["fuel"] == fuel
                else 0.0
                for variable in variables
            ]
            if any(row):
                constraints.append(row)
                limits.append(
                    float((depot.get("inventory") or {}).get(fuel, 0) or 0)
                )

    for depot in depots:
        row = [
            1.0
            if variable["route"]["source_depot_id"] == depot["id"]
            else 0.0
            for variable in variables
        ]
        if any(row):
            constraints.append(row)
            limits.append(
                float(depot.get("dispatch_capacity_per_tick", 0) or 0)
            )

    for route in usable_routes:
        row = [
            1.0 if variable["route"]["id"] == route["id"] else 0.0
            for variable in variables
        ]
        if any(row):
            constraints.append(row)
            limits.append(float(route.get("max_shipment", 0) or 0))

    result = linprog(
        np.array([variable["cost"] for variable in variables]),
        A_ub=np.array(constraints),
        b_ub=np.array(limits),
        bounds=[(0, variable["max"]) for variable in variables],
        method="highs",
    )

    if not result.success:
        return {
            "solver": "scipy-highs",
            "status": "SOLVER_FAILED",
            "message": result.message,
            "forecast": forecasts,
            "recommendations": [],
        }

    recommendations = []
    for value, variable in zip(result.x, variables):
        quantity = int(value // 50 * 50)
        if quantity < 100:
            continue

        route = variable["route"]
        item = variable["forecast"]
        station = station_map[route["destination_station_id"]]
        inventory = float(
            (station.get("inventory") or {}).get(variable["fuel"], 0) or 0
        )
        per_tick = max(float(item["liters_per_tick"]), 0.001)
        after_coverage = (
            inventory
            + inbound[(station["id"], variable["fuel"])]
            + quantity
        ) / per_tick
        after_probability = _sigmoid(
            (horizon - after_coverage) / max(horizon * 0.22, 1)
        )
        before_probability = float(item["stockout_probability"])

        recommendations.append(
            {
                "source_depot_id": route["source_depot_id"],
                "destination_station_id": route["destination_station_id"],
                "route_id": route["id"],
                "fuel_type": variable["fuel"],
                "quantity": quantity,
                "priority_score": round(variable["priority"], 3),
                "transit_ticks": route.get("transit_ticks"),
                "stockout_probability_before": round(before_probability, 4),
                "stockout_probability_after": round(after_probability, 4),
                "risk_reduction_pct": round(
                    max(0, before_probability - after_probability) * 100,
                    1,
                ),
                "forecast_confidence": item["confidence"],
                "coverage_ticks_before": item["coverage_ticks"],
                "coverage_ticks_after": round(after_coverage, 2),
                "reason": (
                    f"{station.get('name', station['id'])} "
                    f"{variable['fuel']} coverage is about "
                    f"{item['coverage_ticks']} ticks. Ship "
                    f"{quantity:,} L via {route['id']} to raise coverage "
                    f"to about {after_coverage:.1f} ticks while respecting "
                    "depot, route, and station limits."
                ),
            }
        )

    recommendations.sort(
        key=lambda item: (
            item["stockout_probability_before"],
            item["priority_score"],
        ),
        reverse=True,
    )

    return {
        "solver": "scipy-highs",
        "status": "OPTIMAL",
        "objective": round(float(result.fun), 6),
        "horizon_ticks": horizon,
        "safety_stock_ticks": safety,
        "forecast": forecasts,
        "recommendations": recommendations,
    }


def risks(
    snapshot: dict[str, Any],
    intelligence: dict[str, Any],
) -> dict[str, Any]:
    forecasts = intelligence.get("forecast") or []
    critical = [
        item for item in forecasts if item["stockout_probability"] >= 0.75
    ]
    warning = [
        item
        for item in forecasts
        if 0.45 <= item["stockout_probability"] < 0.75
    ]

    return {
        "critical_shortage_risks": len(critical),
        "warning_shortage_risks": len(warning),
        "active_events": sum(
            1
            for event in snapshot.get("events") or []
            if event.get("status") == "ACTIVE"
        ),
        "disrupted_routes": sum(
            1
            for route in snapshot.get("routes") or []
            if route.get("status") == "DISRUPTED"
        ),
        "station_outages": sum(
            1
            for station in snapshot.get("stations") or []
            if station.get("status") == "OUTAGE"
        ),
        "constrained_depots": sum(
            1
            for depot in snapshot.get("depots") or []
            if depot.get("status") == "CONSTRAINED"
        ),
        "critical_items": sorted(
            critical,
            key=lambda item: item["stockout_probability"],
            reverse=True,
        )[:8],
    }


async def sse_watch() -> None:
    backoff = 0.5
    while True:
        try:
            async for event_name, payload in sim.stream():
                telemetry.last_sse_event = {
                    "event": event_name,
                    "payload": payload,
                }
                telemetry.event("sse", event_name, payload=payload)
                sim.cache.clear()
                backoff = 0.5
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            telemetry.sse_reconnects += 1
            telemetry.event(
                "sse_reconnect",
                "SSE lost; retrying",
                error=str(exc),
                backoff=backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 8)


app = FastAPI(
    title="GridWise FuelOps Intelligence",
    version="2.0.0",
    description=(
        "BUP CSE Fest 2026 Hackathon Finals decision-support platform "
        "for the official BUP Fuel Supply Simulator."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

watcher_task: asyncio.Task | None = None


class RecReq(BaseModel):
    horizon_ticks: int = Field(default=16, ge=4, le=96)
    safety_stock_ticks: int = Field(default=8, ge=0, le=48)


class AllocationReq(BaseModel):
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: str
    quantity: float = Field(gt=0)
    idempotency_key: str | None = None


class BulkReq(BaseModel):
    recommendations: list[AllocationReq]


class EventReq(BaseModel):
    type: str
    start_tick: int = Field(ge=0)
    duration_ticks: int = Field(gt=0)
    parameters: dict[str, Any] = Field(default_factory=dict)


class FaultReq(BaseModel):
    type: str
    duration_seconds: int = Field(gt=0, le=3600)
    parameters: dict[str, Any] = Field(default_factory=dict)


@app.on_event("startup")
async def startup():
    global watcher_task
    telemetry.event(
        "system",
        "GridWise FuelOps started",
        simulator_url=SIMULATOR_URL,
    )
    watcher_task = asyncio.create_task(sse_watch())


@app.on_event("shutdown")
async def shutdown():
    if watcher_task:
        watcher_task.cancel()
    await sim.close()


@app.middleware("http")
async def app_metrics(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        telemetry.request(
            f"app:{request.url.path}",
            request.method,
            status,
            (time.perf_counter() - start) * 1000,
        )


@app.exception_handler(SimulatorError)
async def sim_error(_: Request, exc: SimulatorError):
    return JSONResponse(
        status_code=exc.status_code if exc.status_code < 500 else 503,
        content={
            "error": "SIMULATOR_ERROR",
            "message": str(exc),
            "payload": exc.payload,
            "degraded": True,
        },
    )


@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/health")
async def api_health():
    try:
        simulator_health, _ = await sim.get("/v1/health", False)
        simulator_ok = simulator_health.get("status") == "ok"
    except Exception as exc:
        simulator_health = {
            "status": "unavailable",
            "error": str(exc),
        }
        simulator_ok = False

    return {
        "status": "healthy" if simulator_ok else "degraded",
        "components": {
            "backend_api": {"status": "healthy"},
            "fuel_simulator": {
                "status": "healthy" if simulator_ok else "unavailable",
                "details": simulator_health,
            },
            "prediction_service": {
                "status": "healthy",
                "model": "EWMA + trend regression",
            },
            "decision_engine": {
                "status": "healthy",
                "solver": "SciPy HiGHS LP",
            },
            "sse_watcher": {
                "status": (
                    "healthy"
                    if telemetry.sse_reconnects == 0
                    else "recovering"
                )
            },
        },
        "circuit_breaker": sim.breaker_state,
        "timestamp": utc_now(),
    }


@app.get("/api/dashboard")
async def dashboard():
    snapshot = await sim.snapshot()
    intelligence = optimize(snapshot)
    return {
        "snapshot": snapshot,
        "intelligence": intelligence,
        "risks": risks(snapshot, intelligence),
        "observability": telemetry.snapshot(),
        "generated_at": utc_now(),
    }


@app.post("/api/recommendations")
async def recommendations(req: RecReq):
    snapshot = await sim.snapshot()
    intelligence = optimize(
        snapshot,
        req.horizon_ticks,
        req.safety_stock_ticks,
    )
    telemetry.event(
        "decision",
        "Allocation plan generated",
        count=len(intelligence.get("recommendations", [])),
    )
    return intelligence


async def apply_one(req: AllocationReq) -> dict[str, Any]:
    body = {
        "idempotency_key": (
            req.idempotency_key or f"gridwise-{uuid.uuid4()}"
        ),
        "source_depot_id": req.source_depot_id,
        "destination_station_id": req.destination_station_id,
        "route_id": req.route_id,
        "fuel_type": req.fuel_type,
        "quantity": req.quantity,
    }
    try:
        output = await sim.post("/v1/allocations", body)
        telemetry.allocations_submitted += 1
        telemetry.event(
            "allocation",
            "Allocation submitted",
            allocation=output,
        )
        return {"ok": True, "allocation": output}
    except SimulatorError as exc:
        telemetry.allocations_failed += 1
        telemetry.event(
            "allocation_failed",
            "Allocation rejected",
            request=body,
            error=str(exc),
            payload=exc.payload,
        )
        return {
            "ok": False,
            "status_code": exc.status_code,
            "error": str(exc),
            "payload": exc.payload,
        }


@app.post("/api/allocations/apply")
async def apply(req: AllocationReq):
    result = await apply_one(req)
    if not result["ok"]:
        raise HTTPException(
            status_code=(
                409 if result.get("status_code") == 409 else 503
            ),
            detail=result,
        )
    return result


@app.post("/api/allocations/apply-bulk")
async def bulk(req: BulkReq):
    output = []
    for item in req.recommendations:
        output.append(await apply_one(item))
    return {
        "results": output,
        "accepted": sum(1 for item in output if item["ok"]),
        "failed": sum(1 for item in output if not item["ok"]),
    }


@app.post("/api/allocations/{allocation_id}/cancel")
async def cancel(allocation_id: int):
    return await sim.post(
        f"/v1/allocations/{allocation_id}/cancel",
        {},
    )


@app.post("/api/admin/{action}")
async def admin_action(action: str):
    if action not in {"run", "pause", "step", "reset"}:
        raise HTTPException(404)
    output = await sim.post(f"/admin/{action}", {})
    telemetry.event("admin", f"Simulation {action}")
    return output


@app.post("/api/admin/events")
async def admin_event(req: EventReq):
    output = await sim.post("/admin/events", req.model_dump())
    telemetry.event("crisis", "Crisis event injected", event=output)
    return output


@app.post("/api/admin/faults")
async def admin_fault(req: FaultReq):
    output = await sim.post("/admin/faults", req.model_dump())
    telemetry.event("fault", "Engineering fault injected", fault=output)
    return output


@app.post("/api/admin/faults/clear")
async def clear_faults():
    output = await sim.post("/admin/faults/clear", {})
    telemetry.event("fault", "All faults cleared")
    return output


@app.get("/api/admin/audit")
async def simulator_audit(
    limit: int = Query(default=100, ge=1, le=1000),
):
    output, _ = await sim.get(f"/admin/audit?limit={limit}")
    return output


@app.get("/api/audit")
async def audit(limit: int = Query(default=100, ge=1, le=500)):
    return list(telemetry.events)[:limit]


@app.get("/api/observability")
async def observability():
    return {
        "health": await api_health(),
        "telemetry": telemetry.snapshot(),
        "events": list(telemetry.events)[:50],
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    snapshot = telemetry.snapshot()
    return (
        "\n".join(
            [
                "# TYPE gridwise_requests_total counter",
                f"gridwise_requests_total {snapshot['request_count']}",
                "# TYPE gridwise_request_error_rate gauge",
                f"gridwise_request_error_rate {snapshot['error_rate']}",
                "# TYPE gridwise_request_latency_p95_ms gauge",
                (
                    "gridwise_request_latency_p95_ms "
                    f"{snapshot['latency_ms']['p95']}"
                ),
                (
                    "gridwise_sse_reconnects_total "
                    f"{snapshot['sse_reconnects']}"
                ),
                (
                    "gridwise_degraded_reads_total "
                    f"{snapshot['degraded_reads']}"
                ),
                (
                    "gridwise_simulator_failures_total "
                    f"{snapshot['simulator_failures']}"
                ),
                (
                    "gridwise_allocations_submitted_total "
                    f"{snapshot['allocations_submitted']}"
                ),
                (
                    "gridwise_allocations_failed_total "
                    f"{snapshot['allocations_failed']}"
                ),
            ]
        )
        + "\n"
    )
