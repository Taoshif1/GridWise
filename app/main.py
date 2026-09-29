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
EMBEDDED_SIMULATOR = os.getenv("EMBEDDED_SIMULATOR", "false").lower() in {"1", "true", "yes", "on"}
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



class DemoSimulator:
    """Deterministic API-compatible demo world for hosted judging previews.

    The production/local Compose path still uses the official organizer image.
    This fallback exists because some PaaS hosts cannot run a second Docker image.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.tick = 0
        self.status = "PAUSED"
        self.started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.regions = [
            {"id": "region-dhaka", "name": "Dhaka Division", "demand_factor": 1.00},
            {"id": "region-chattogram", "name": "Chattogram Division", "demand_factor": 1.08},
        ]
        self.depots = [
            {"id": "depot-gazipur", "name": "Gazipur Depot", "region_id": "region-dhaka", "status": "OPEN", "dispatch_capacity_per_tick": 12000, "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000}, "inventory": {"DIESEL": 60000, "PETROL": 45000, "OCTANE": 26000}},
            {"id": "depot-patiya", "name": "Patiya Depot", "region_id": "region-chattogram", "status": "OPEN", "dispatch_capacity_per_tick": 11000, "capacity": {"DIESEL": 85000, "PETROL": 65000, "OCTANE": 40000}, "inventory": {"DIESEL": 55000, "PETROL": 42000, "OCTANE": 24000}},
        ]
        self.stations = [
            {"id": "station-mirpur", "name": "Mirpur Fuel Station", "region_id": "region-dhaka", "status": "OPEN", "demand_profile": "urban_high", "demand_multiplier": 1.0, "capacity": {"DIESEL": 15000, "PETROL": 14000, "OCTANE": 9000}, "inventory": {"DIESEL": 9000, "PETROL": 9000, "OCTANE": 5000}},
            {"id": "station-tongi", "name": "Tongi Fuel Station", "region_id": "region-dhaka", "status": "OPEN", "demand_profile": "urban_medium", "demand_multiplier": 1.0, "capacity": {"DIESEL": 14000, "PETROL": 12000, "OCTANE": 8000}, "inventory": {"DIESEL": 8500, "PETROL": 7600, "OCTANE": 4400}},
            {"id": "station-karnaphuli", "name": "Karnaphuli Fuel Station", "region_id": "region-chattogram", "status": "OPEN", "demand_profile": "industrial", "demand_multiplier": 1.0, "capacity": {"DIESEL": 16000, "PETROL": 13000, "OCTANE": 8500}, "inventory": {"DIESEL": 10000, "PETROL": 7600, "OCTANE": 4600}},
            {"id": "station-coxsbazar", "name": "Cox's Bazar Fuel Station", "region_id": "region-chattogram", "status": "OPEN", "demand_profile": "tourism", "demand_multiplier": 1.0, "capacity": {"DIESEL": 12000, "PETROL": 11000, "OCTANE": 7500}, "inventory": {"DIESEL": 7000, "PETROL": 6500, "OCTANE": 3900}},
        ]
        self.routes = [
            {"id": "route-gazipur-mirpur", "source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur", "transit_ticks": 2, "max_shipment": 7000, "status": "AVAILABLE"},
            {"id": "route-gazipur-tongi", "source_depot_id": "depot-gazipur", "destination_station_id": "station-tongi", "transit_ticks": 2, "max_shipment": 6500, "status": "AVAILABLE"},
            {"id": "route-patiya-karnaphuli", "source_depot_id": "depot-patiya", "destination_station_id": "station-karnaphuli", "transit_ticks": 2, "max_shipment": 7000, "status": "AVAILABLE"},
            {"id": "route-patiya-coxsbazar", "source_depot_id": "depot-patiya", "destination_station_id": "station-coxsbazar", "transit_ticks": 3, "max_shipment": 6000, "status": "AVAILABLE"},
            {"id": "route-gazipur-karnaphuli", "source_depot_id": "depot-gazipur", "destination_station_id": "station-karnaphuli", "transit_ticks": 4, "max_shipment": 5000, "status": "AVAILABLE"},
            {"id": "route-patiya-mirpur", "source_depot_id": "depot-patiya", "destination_station_id": "station-mirpur", "transit_ticks": 4, "max_shipment": 5000, "status": "AVAILABLE"},
        ]
        self.supply_arrivals = [
            {"id": "supply-001", "depot_id": "depot-gazipur", "fuel_type": "DIESEL", "quantity": 18000, "planned_tick": 12, "actual_tick": None, "status": "SCHEDULED"},
            {"id": "supply-002", "depot_id": "depot-patiya", "fuel_type": "DIESEL", "quantity": 16000, "planned_tick": 14, "actual_tick": None, "status": "SCHEDULED"},
            {"id": "supply-003", "depot_id": "depot-gazipur", "fuel_type": "PETROL", "quantity": 14000, "planned_tick": 20, "actual_tick": None, "status": "SCHEDULED"},
            {"id": "supply-004", "depot_id": "depot-patiya", "fuel_type": "OCTANE", "quantity": 9000, "planned_tick": 24, "actual_tick": None, "status": "SCHEDULED"},
        ]
        self.events: list[dict[str, Any]] = []
        self.allocations: list[dict[str, Any]] = []
        self.faults: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self.demand_history: list[dict[str, Any]] = []
        self.served = 0.0
        self.unmet = 0.0
        self.allocation_failures = 0
        self._request_counter = 0
        self._event_seq = 1
        self._allocation_seq = 1
        self._demand_seq = 1
        self._bases = {
            "station-mirpur": {"DIESEL": 420.0, "PETROL": 390.0, "OCTANE": 260.0},
            "station-tongi": {"DIESEL": 300.0, "PETROL": 280.0, "OCTANE": 180.0},
            "station-karnaphuli": {"DIESEL": 350.0, "PETROL": 320.0, "OCTANE": 220.0},
            "station-coxsbazar": {"DIESEL": 250.0, "PETROL": 230.0, "OCTANE": 160.0},
        }
        for t in range(-48, 0):
            for station in self.stations:
                for fuel in FUELS:
                    wave = 1 + 0.08 * math.sin((t + len(fuel)) / 5)
                    demand = self._bases[station["id"]][fuel] * wave
                    self.demand_history.append(self._demand_row(station["id"], fuel, t, demand, demand))

    def _clone(self, value: Any) -> Any:
        return json.loads(json.dumps(value))

    def _now_sim(self) -> str:
        from datetime import timedelta
        return (self.started + timedelta(minutes=15 * self.tick)).isoformat()

    def _demand_row(self, station_id: str, fuel: str, tick: int, demand: float, served: float) -> dict[str, Any]:
        row = {"id": self._demand_seq, "station_id": station_id, "fuel_type": fuel, "tick": tick, "sim_time": self._now_sim(), "demand_liters": round(demand, 3), "served_liters": round(served, 3), "unmet_liters": round(max(0.0, demand - served), 3)}
        self._demand_seq += 1
        return row

    def _active_fault(self, kind: str) -> dict[str, Any] | None:
        now = time.monotonic()
        for fault in self.faults:
            if fault["type"] == kind and fault["end"] > now:
                return fault
        return None

    async def _fault_gate(self, path: str) -> bool:
        if not path.startswith("/v1/") or path == "/v1/health":
            return False
        self._request_counter += 1
        latency = self._active_fault("latency")
        if latency:
            await asyncio.sleep(float(latency["parameters"].get("delay_ms", 500)) / 1000)
        if self._active_fault("unavailable"):
            raise SimulatorError("Injected simulator unavailable fault", 503, {"error": {"code": "FAULT_INJECTED"}})
        rate = self._active_fault("error_rate")
        if rate:
            threshold = max(1, int(round(1 / max(float(rate["parameters"].get("rate", 0.25)), 0.01))))
            if self._request_counter % threshold == 0:
                raise SimulatorError("Injected transient simulator error", 503, {"error": {"code": "FAULT_INJECTED"}})
        return self._active_fault("stale_data") is not None

    def _find(self, rows: list[dict[str, Any]], entity_id: str) -> dict[str, Any] | None:
        return next((row for row in rows if row.get("id") == entity_id), None)

    def _record(self, action: str, entity_type: str = "system", entity_id: str = "") -> None:
        self.audit.insert(0, {"id": len(self.audit) + 1, "wall_time": utc_now(), "sim_time": self._now_sim(), "tick": self.tick, "action": action, "entity_type": entity_type, "entity_id": entity_id, "result": "OK", "metadata_json": {}})

    def _start_event(self, event: dict[str, Any]) -> None:
        p = event["parameters"]
        if event["type"] == "demand_spike":
            ids = set(p.get("station_ids") or [])
            regions = set(p.get("region_ids") or [])
            mult = float(p.get("multiplier", 1.5))
            for station in self.stations:
                if (not ids and not regions) or station["id"] in ids or station["region_id"] in regions:
                    station["demand_multiplier"] *= mult
        elif event["type"] == "route_disruption":
            ids = set(p.get("route_ids") or [])
            for route in self.routes:
                if not ids or route["id"] in ids:
                    route["status"] = "DISRUPTED"
        elif event["type"] == "station_outage":
            ids = set(p.get("station_ids") or [])
            for station in self.stations:
                if not ids or station["id"] in ids:
                    station["status"] = "OUTAGE"
        elif event["type"] == "depot_constraint":
            ids = set(p.get("depot_ids") or [])
            for depot in self.depots:
                if not ids or depot["id"] in ids:
                    depot["status"] = "CONSTRAINED"
        elif event["type"] == "shipment_delay":
            ids = set(p.get("depot_ids") or [])
            fuels = set(p.get("fuel_types") or [])
            delay = int(p.get("delay_ticks", 2))
            for supply in self.supply_arrivals:
                if supply["status"] == "SCHEDULED" and (not ids or supply["depot_id"] in ids) and (not fuels or supply["fuel_type"] in fuels):
                    supply["planned_tick"] += delay
                    supply["status"] = "DELAYED"
        elif event["type"] == "supply_shortfall":
            ids = set(p.get("depot_ids") or [])
            fuels = set(p.get("fuel_types") or [])
            factor = float(p.get("factor", 0.5))
            for supply in self.supply_arrivals:
                if supply["status"] in {"SCHEDULED", "DELAYED"} and (not ids or supply["depot_id"] in ids) and (not fuels or supply["fuel_type"] in fuels):
                    supply["quantity"] = round(float(supply["quantity"]) * factor, 3)
        event["status"] = "ACTIVE"
        self._record("event.started", "event", str(event["id"]))

    def _resolve_event(self, event: dict[str, Any]) -> None:
        p = event["parameters"]
        if event["type"] == "demand_spike":
            ids = set(p.get("station_ids") or [])
            regions = set(p.get("region_ids") or [])
            mult = max(float(p.get("multiplier", 1.5)), 0.01)
            for station in self.stations:
                if (not ids and not regions) or station["id"] in ids or station["region_id"] in regions:
                    station["demand_multiplier"] /= mult
        elif event["type"] == "route_disruption":
            ids = set(p.get("route_ids") or [])
            for route in self.routes:
                if not ids or route["id"] in ids:
                    route["status"] = "AVAILABLE"
        elif event["type"] == "station_outage":
            ids = set(p.get("station_ids") or [])
            for station in self.stations:
                if not ids or station["id"] in ids:
                    station["status"] = "OPEN"
        elif event["type"] == "depot_constraint":
            ids = set(p.get("depot_ids") or [])
            for depot in self.depots:
                if not ids or depot["id"] in ids:
                    depot["status"] = "OPEN"
        event["status"] = "RESOLVED"
        self._record("event.resolved", "event", str(event["id"]))

    def step(self) -> None:
        self.tick += 1
        for event in self.events:
            if event["status"] == "SCHEDULED" and event["start_tick"] <= self.tick:
                self._start_event(event)
            if event["status"] == "ACTIVE" and event["end_tick"] <= self.tick:
                self._resolve_event(event)

        for supply in self.supply_arrivals:
            if supply["status"] in {"SCHEDULED", "DELAYED"} and supply["planned_tick"] <= self.tick:
                depot = self._find(self.depots, supply["depot_id"])
                if depot:
                    fuel = supply["fuel_type"]
                    depot["inventory"][fuel] = min(depot["capacity"][fuel], depot["inventory"][fuel] + float(supply["quantity"]))
                supply["actual_tick"] = self.tick
                supply["status"] = "ARRIVED"
                self._record("supply.arrived", "supply", supply["id"])

        for allocation in self.allocations:
            if allocation["status"] == "PENDING" and allocation["created_tick"] < self.tick:
                route = self._find(self.routes, allocation["route_id"])
                depot = self._find(self.depots, allocation["source_depot_id"])
                if not route or route["status"] != "AVAILABLE":
                    allocation["status"] = "FAILED"
                    allocation["failure_reason"] = "ROUTE_DISRUPTED"
                    self.allocation_failures += 1
                    self._record("allocation.failed", "allocation", str(allocation["id"]))
                elif depot and depot["inventory"][allocation["fuel_type"]] >= allocation["quantity"]:
                    depot["inventory"][allocation["fuel_type"]] -= allocation["quantity"]
                    allocation["departure_tick"] = self.tick
                    allocation["expected_arrival_tick"] = self.tick + int(route["transit_ticks"])
                    allocation["status"] = "IN_TRANSIT"
                    self._record("allocation.departed", "allocation", str(allocation["id"]))

            if allocation["status"] == "IN_TRANSIT" and allocation["expected_arrival_tick"] <= self.tick:
                station = self._find(self.stations, allocation["destination_station_id"])
                if station:
                    fuel = allocation["fuel_type"]
                    station["inventory"][fuel] = min(station["capacity"][fuel], station["inventory"][fuel] + allocation["quantity"])
                allocation["actual_arrival_tick"] = self.tick
                allocation["status"] = "ARRIVED"
                self._record("allocation.arrived", "allocation", str(allocation["id"]))

        for station in self.stations:
            for fuel in FUELS:
                wave = 1 + 0.06 * math.sin((self.tick + len(station["id"]) + len(fuel)) / 4)
                demand = self._bases[station["id"]][fuel] * station["demand_multiplier"] * wave
                served = 0.0 if station["status"] != "OPEN" else min(float(station["inventory"][fuel]), demand)
                if station["status"] == "OPEN":
                    station["inventory"][fuel] = max(0.0, float(station["inventory"][fuel]) - served)
                self.served += served
                self.unmet += max(0.0, demand - served)
                self.demand_history.append(self._demand_row(station["id"], fuel, self.tick, demand, served))
        self._record("simulation.tick")

    async def get(self, path: str) -> tuple[Any, dict[str, Any]]:
        stale = await self._fault_gate(path)
        base = path.split("?", 1)[0]
        if base == "/v1/health":
            data = {"status": "ok", "database": "ok", "simulation": {"status": self.status, "tick": self.tick}}
        elif base == "/v1/instance":
            data = {"id": 1, "scenario_id": "baseline", "scenario_version": "1.0", "seed": 12345, "sim_time": self._now_sim(), "tick": self.tick, "tick_minutes": 15, "status": self.status}
        elif base == "/v1/regions":
            data = self.regions
        elif base == "/v1/depots":
            data = self.depots
        elif base == "/v1/stations":
            data = self.stations
        elif base == "/v1/routes":
            data = self.routes
        elif base == "/v1/supply-arrivals":
            data = sorted(self.supply_arrivals, key=lambda x: x["planned_tick"])
        elif base == "/v1/events":
            data = sorted(self.events, key=lambda x: x["id"], reverse=True)
        elif base == "/v1/allocations":
            data = sorted(self.allocations, key=lambda x: x["id"], reverse=True)
        elif base == "/v1/demand-history":
            limit = 200
            if "limit=" in path:
                try:
                    limit = max(1, min(2000, int(path.split("limit=", 1)[1].split("&", 1)[0])))
                except Exception:
                    pass
            data = self.demand_history[-limit:]
        elif base == "/v1/metrics":
            total = self.served + self.unmet
            data = {"served_demand_liters": round(self.served, 3), "unmet_demand_liters": round(self.unmet, 3), "service_level": round(self.served / total, 6) if total else 1.0, "allocation_liters": round(sum(float(a["quantity"]) for a in self.allocations if a["status"] in {"IN_TRANSIT", "ARRIVED"}), 3), "allocation_failures": self.allocation_failures}
        elif base == "/admin/audit":
            data = self.audit
        else:
            raise SimulatorError("Demo endpoint not found", 404, {"detail": {"code": "NOT_FOUND"}})
        return self._clone(data), {"cached": False, "degraded": False, "stale": stale}

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        if path == "/admin/run":
            self.status = "RUNNING"; self._record("admin.run"); return {"status": self.status}
        if path == "/admin/pause":
            self.status = "PAUSED"; self._record("admin.pause"); return {"status": self.status}
        if path == "/admin/step":
            self.step(); return {"tick": self.tick, "sim_time": self._now_sim()}
        if path == "/admin/reset":
            self.reset(); return {"status": "reset"}
        if path == "/admin/faults/clear":
            self.faults.clear(); self._record("fault.clear_all"); return {"status": "cleared"}
        if path == "/admin/faults":
            fault = {"id": len(self.faults) + 1, "type": body["type"], "parameters": body.get("parameters") or {}, "end": time.monotonic() + int(body["duration_seconds"])}
            self.faults.append(fault); self._record("fault.created", "fault", str(fault["id"])); return self._clone(fault)
        if path == "/admin/events":
            event = {"id": self._event_seq, "type": body["type"], "start_tick": int(body["start_tick"]), "end_tick": int(body["start_tick"]) + int(body["duration_ticks"]), "status": "SCHEDULED", "parameters": body.get("parameters") or {}}
            self._event_seq += 1; self.events.append(event); self._record("event.created", "event", str(event["id"])); return self._clone(event)
        if path.startswith("/v1/allocations/") and path.endswith("/cancel"):
            try:
                allocation_id = int(path.split("/")[3])
            except Exception:
                raise SimulatorError("Allocation not found", 404)
            allocation = next((a for a in self.allocations if a["id"] == allocation_id), None)
            if not allocation:
                raise SimulatorError("Allocation not found", 404)
            if allocation["status"] != "PENDING":
                raise SimulatorError("Only pending allocations can be cancelled", 409)
            allocation["status"] = "CANCELLED"; self._record("allocation.cancelled", "allocation", str(allocation_id)); return self._clone(allocation)
        if path == "/v1/allocations":
            route = self._find(self.routes, body["route_id"])
            depot = self._find(self.depots, body["source_depot_id"])
            station = self._find(self.stations, body["destination_station_id"])
            fuel = body["fuel_type"]; quantity = float(body["quantity"])
            if not route or not depot or not station:
                raise SimulatorError("Unknown depot, station, or route", 404, {"detail": {"code": "NOT_FOUND"}})
            if route["source_depot_id"] != depot["id"] or route["destination_station_id"] != station["id"]:
                raise SimulatorError("Route mismatch", 409, {"detail": {"code": "ROUTE_MISMATCH"}})
            if route["status"] != "AVAILABLE":
                raise SimulatorError("Route disrupted", 409, {"detail": {"code": "ROUTE_DISRUPTED"}})
            if station["status"] != "OPEN":
                raise SimulatorError("Station closed", 409, {"detail": {"code": "STATION_CLOSED"}})
            if quantity > float(route["max_shipment"]):
                raise SimulatorError("Route capacity exceeded", 409, {"detail": {"code": "ROUTE_CAPACITY_EXCEEDED"}})
            if depot["inventory"][fuel] < quantity:
                raise SimulatorError("Insufficient depot inventory", 409, {"detail": {"code": "INSUFFICIENT_INVENTORY"}})
            inbound = sum(float(a["quantity"]) for a in self.allocations if a["destination_station_id"] == station["id"] and a["fuel_type"] == fuel and a["status"] in {"PENDING", "IN_TRANSIT"})
            if station["inventory"][fuel] + inbound + quantity > station["capacity"][fuel]:
                raise SimulatorError("Destination capacity exceeded", 409, {"detail": {"code": "DESTINATION_CAPACITY_EXCEEDED"}})
            key = body["idempotency_key"]
            existing = next((a for a in self.allocations if a["idempotency_key"] == key), None)
            if existing:
                comparable = {k: existing[k] for k in ("source_depot_id", "destination_station_id", "route_id", "fuel_type", "quantity")}
                requested = {k: body[k] for k in comparable}
                if comparable == requested:
                    return self._clone(existing)
                raise SimulatorError("Idempotency key mismatch", 409, {"detail": {"code": "IDEMPOTENCY_KEY_MISMATCH"}})
            allocation = {"id": self._allocation_seq, "idempotency_key": key, "source_depot_id": depot["id"], "destination_station_id": station["id"], "route_id": route["id"], "fuel_type": fuel, "quantity": quantity, "created_tick": self.tick, "departure_tick": None, "expected_arrival_tick": None, "actual_arrival_tick": None, "status": "PENDING", "failure_reason": None}
            self._allocation_seq += 1; self.allocations.append(allocation); self._record("allocation.created", "allocation", str(allocation["id"])); return self._clone(allocation)
        raise SimulatorError("Demo endpoint not found", 404)

    async def stream(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        while True:
            if self._active_fault("stream_disconnect"):
                raise SimulatorError("Injected SSE disconnect", 503)
            await asyncio.sleep(0.75)
            if self.status == "RUNNING":
                self.step()
            yield "simulation.tick", {"tick": self.tick, "sim_time": self._now_sim()}


class SimulatorClient:
    def __init__(self) -> None:
        self.demo = DemoSimulator() if EMBEDDED_SIMULATOR else None
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
        if self.demo is not None:
            return await self.demo.get(path)
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
        if self.demo is not None:
            return await self.demo.post(path, body or {})
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
        if self.demo is not None:
            async for item in self.demo.stream():
                yield item
            return
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
