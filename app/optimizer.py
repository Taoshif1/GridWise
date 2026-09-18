from __future__ import annotations

import math

import numpy as np
from scipy.optimize import linprog

from .rules import build_effective_rules
from .schemas import DirectiveInterpretation, HourPlan, OptimizeRequest


class OptimizationError(RuntimeError):
    pass


# Variable layout:
# 0..23   grid_kwh
# 24..47  solar_used_kwh
# 48..71  battery_flow_kwh   (+ = discharge, - = charge)
# 72..95  battery_energy_after_kwh
N = 24
GRID = 0
SOLAR = 24
FLOW = 48
ENERGY = 72
VAR_COUNT = 96


def _idx(base: int, hour: int) -> int:
    return base + hour


def optimize_schedule(
    request: OptimizeRequest,
    directives: list[DirectiveInterpretation],
) -> tuple[list[HourPlan], float, float, float]:
    ordered = request.ordered_hours()
    hour_map = {item.hour: item for item in ordered}
    battery = request.battery
    rules = build_effective_rules(request, directives)

    c = np.zeros(VAR_COUNT, dtype=float)
    for h in range(N):
        c[_idx(GRID, h)] = hour_map[h].tariff_bdt_per_kwh

    bounds: list[tuple[float | None, float | None]] = []

    # grid
    for h in range(N):
        upper = rules.grid_cap.get(h)
        bounds.append((0.0, upper))

    # solar_used
    for h in range(N):
        bounds.append((0.0, rules.effective_solar[h]))

    # battery_flow: positive = discharge, negative = charge
    for h in range(N):
        low = -battery.max_charge_kwh_per_hour
        high = battery.max_discharge_kwh_per_hour

        if h in rules.no_charge:
            low = 0.0
        if h in rules.no_discharge:
            high = 0.0

        bounds.append((low, high))

    # battery energy after hour
    for h in range(N):
        bounds.append((rules.reserve[h], battery.capacity_kwh))

    a_eq: list[np.ndarray] = []
    b_eq: list[float] = []

    for h in range(N):
        # grid + solar + battery_flow = demand
        row = np.zeros(VAR_COUNT, dtype=float)
        row[_idx(GRID, h)] = 1.0
        row[_idx(SOLAR, h)] = 1.0
        row[_idx(FLOW, h)] = 1.0
        a_eq.append(row)
        b_eq.append(hour_map[h].demand_kwh)

        # E_after = E_before - battery_flow
        # hour 0: E0 + flow0 = initial
        # later: Eh - E(h-1) + flowh = 0
        row = np.zeros(VAR_COUNT, dtype=float)
        row[_idx(ENERGY, h)] = 1.0
        row[_idx(FLOW, h)] = 1.0

        if h == 0:
            a_eq.append(row)
            b_eq.append(battery.initial_energy_kwh)
        else:
            row[_idx(ENERGY, h - 1)] = -1.0
            a_eq.append(row)
            b_eq.append(0.0)

    # End-of-day neutrality
    row = np.zeros(VAR_COUNT, dtype=float)
    row[_idx(ENERGY, 23)] = 1.0
    a_eq.append(row)
    b_eq.append(battery.initial_energy_kwh)

    result = linprog(
        c,
        A_eq=np.vstack(a_eq),
        b_eq=np.asarray(b_eq, dtype=float),
        bounds=bounds,
        method="highs",
        options={"presolve": True},
    )

    if not result.success or result.x is None:
        raise OptimizationError(
            f"No feasible optimal schedule found ({result.message if result.message else 'solver failure'})"
        )

    x = result.x
    plan: list[HourPlan] = []

    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0

    def clean(value: float) -> float:
        if abs(value) < 5e-9:
            value = 0.0
        return round(float(value), 6)

    for h in range(N):
        grid = max(0.0, float(x[_idx(GRID, h)]))
        solar = max(0.0, float(x[_idx(SOLAR, h)]))
        flow = float(x[_idx(FLOW, h)])
        energy = float(x[_idx(ENERGY, h)])

        if flow > 5e-7:
            action = "discharge"
            magnitude = flow
        elif flow < -5e-7:
            action = "charge"
            magnitude = -flow
        else:
            action = "idle"
            magnitude = 0.0

        plan.append(
            HourPlan(
                hour=h,
                grid_kwh=clean(grid),
                solar_used_kwh=clean(solar),
                battery_action=action,
                battery_kwh=clean(magnitude),
                battery_energy_after_kwh=clean(energy),
            )
        )

        total_grid += grid
        total_cost += grid * hour_map[h].tariff_bdt_per_kwh
        peak_grid = max(peak_grid, grid)

    return (
        plan,
        clean(total_grid),
        clean(total_cost),
        clean(peak_grid),
    )
