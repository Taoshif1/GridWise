from __future__ import annotations

from dataclasses import dataclass, field

from .schemas import OptimizeRequest, DirectiveInterpretation


@dataclass
class EffectiveRules:
    effective_solar: dict[int, float]
    reserve: dict[int, float]
    no_charge: set[int] = field(default_factory=set)
    no_discharge: set[int] = field(default_factory=set)
    grid_cap: dict[int, float] = field(default_factory=dict)


def build_effective_rules(
    request: OptimizeRequest,
    directives: list[DirectiveInterpretation],
) -> EffectiveRules:
    hour_map = {item.hour: item for item in request.hours}

    effective_solar = {h: float(hour_map[h].solar_kwh) for h in range(24)}
    reserve = {h: float(request.battery.minimum_energy_kwh) for h in range(24)}
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    grid_cap: dict[int, float] = {}

    # Keep the most restrictive solar factor for overlapping equivalent rules.
    solar_factor = {h: 1.0 for h in range(24)}

    for directive in directives:
        if not directive.applies:
            continue

        adj = directive.structured_adjustment or {}
        hours = adj.get("hours", [])

        if directive.directive_type == "solar_reduction":
            factor = float(adj["factor"])
            for h in hours:
                solar_factor[h] = min(solar_factor[h], factor)

        elif directive.directive_type == "minimum_battery_reserve":
            minimum = float(adj["minimum_energy_kwh"])
            for h in hours:
                reserve[h] = max(reserve[h], minimum)

        elif directive.directive_type == "no_charge_window":
            no_charge.update(hours)

        elif directive.directive_type == "no_discharge_window":
            no_discharge.update(hours)

        elif directive.directive_type == "max_grid_window":
            maximum = float(adj["max_grid_kwh"])
            for h in hours:
                grid_cap[h] = min(grid_cap.get(h, maximum), maximum)

    for h in range(24):
        effective_solar[h] *= solar_factor[h]

    return EffectiveRules(
        effective_solar=effective_solar,
        reserve=reserve,
        no_charge=no_charge,
        no_discharge=no_discharge,
        grid_cap=grid_cap,
    )
