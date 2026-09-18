from __future__ import annotations

import math

from .rules import build_effective_rules
from .schemas import DirectiveInterpretation, HourPlan, OptimizeRequest


class PlanValidationError(RuntimeError):
    pass


TOL = 0.009


def _close(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= TOL


def validate_plan(
    request: OptimizeRequest,
    directives: list[DirectiveInterpretation],
    plan: list[HourPlan],
    total_grid_kwh: float,
    total_cost_bdt: float,
    peak_grid_kwh: float,
) -> None:
    if len(plan) != 24:
        raise PlanValidationError("hourly_plan must contain exactly 24 entries")

    if [p.hour for p in plan] != list(range(24)):
        raise PlanValidationError("hourly_plan hours must be exactly 0 through 23 in order")

    hour_map = {h.hour: h for h in request.hours}
    rules = build_effective_rules(request, directives)
    battery = request.battery

    previous_energy = battery.initial_energy_kwh
    recalculated_grid = 0.0
    recalculated_cost = 0.0
    recalculated_peak = 0.0

    for p in plan:
        h = p.hour
        values = [
            p.grid_kwh,
            p.solar_used_kwh,
            p.battery_kwh,
            p.battery_energy_after_kwh,
        ]
        if any(not math.isfinite(v) or v < -TOL for v in values):
            raise PlanValidationError(f"hour {h}: non-finite or negative output")

        if p.solar_used_kwh > rules.effective_solar[h] + TOL:
            raise PlanValidationError(f"hour {h}: solar usage exceeds effective solar")

        if p.battery_energy_after_kwh < rules.reserve[h] - TOL:
            raise PlanValidationError(f"hour {h}: battery reserve violated")
        if p.battery_energy_after_kwh > battery.capacity_kwh + TOL:
            raise PlanValidationError(f"hour {h}: battery capacity violated")

        charge = 0.0
        discharge = 0.0

        if p.battery_action == "charge":
            charge = p.battery_kwh
            if h in rules.no_charge:
                raise PlanValidationError(f"hour {h}: no-charge directive violated")
            if charge > battery.max_charge_kwh_per_hour + TOL:
                raise PlanValidationError(f"hour {h}: charge rate violated")

        elif p.battery_action == "discharge":
            discharge = p.battery_kwh
            if h in rules.no_discharge:
                raise PlanValidationError(f"hour {h}: no-discharge directive violated")
            if discharge > battery.max_discharge_kwh_per_hour + TOL:
                raise PlanValidationError(f"hour {h}: discharge rate violated")

        elif p.battery_action == "idle":
            if abs(p.battery_kwh) > TOL:
                raise PlanValidationError(f"hour {h}: idle must have battery_kwh=0")

        expected_energy = previous_energy + charge - discharge
        if not _close(p.battery_energy_after_kwh, expected_energy):
            raise PlanValidationError(f"hour {h}: battery transition mismatch")

        expected_demand = hour_map[h].demand_kwh + charge
        supplied = p.grid_kwh + p.solar_used_kwh + discharge
        if not _close(supplied, expected_demand):
            raise PlanValidationError(f"hour {h}: energy balance violated")

        cap = rules.grid_cap.get(h)
        if cap is not None and p.grid_kwh > cap + TOL:
            raise PlanValidationError(f"hour {h}: max-grid directive violated")

        previous_energy = p.battery_energy_after_kwh
        recalculated_grid += p.grid_kwh
        recalculated_cost += p.grid_kwh * hour_map[h].tariff_bdt_per_kwh
        recalculated_peak = max(recalculated_peak, p.grid_kwh)

    if not _close(previous_energy, battery.initial_energy_kwh):
        raise PlanValidationError("end-of-day battery neutrality violated")

    if not _close(total_grid_kwh, recalculated_grid):
        raise PlanValidationError("total_grid_kwh does not match hourly_plan")

    if not _close(total_cost_bdt, recalculated_cost):
        raise PlanValidationError("total_cost_bdt does not match hourly_plan")

    if not _close(peak_grid_kwh, recalculated_peak):
        raise PlanValidationError("peak_grid_kwh does not match hourly_plan")
