from __future__ import annotations

import math

from .schemas import BatteryInput, DirectiveInterpretation


ALLOWED_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}

EXPECTED_KEYS = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}


def _validate_hours(hours) -> None:
    if not isinstance(hours, list) or not hours:
        raise ValueError("structured_adjustment.hours must be a non-empty list")
    if any(type(h) is not int for h in hours):
        raise ValueError("every directive hour must be an integer")
    if any(h < 0 or h > 23 for h in hours):
        raise ValueError("directive hours must be within 0 through 23")
    if hours != sorted(set(hours)):
        raise ValueError("directive hours must be unique and in ascending order")


def _validate_finite_number(value, field: str, *, minimum: float = 0.0, maximum: float | None = None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{field} is outside the allowed range")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} is outside the allowed range")


def validate_directives(
    directives: list[DirectiveInterpretation],
    note_count: int,
    battery: BatteryInput,
) -> list[DirectiveInterpretation]:
    if len(directives) != note_count:
        raise ValueError("every operator note must produce exactly one directive_interpretation entry")

    indexes = [item.note_index for item in directives]
    if indexes != list(range(note_count)):
        raise ValueError("directive_interpretation must be returned in note_index order with no duplicates")

    for item in directives:
        if item.directive_type not in ALLOWED_TYPES:
            raise ValueError(f"unsupported directive_type: {item.directive_type}")

        if item.directive_type == "no_op":
            if item.applies is not False:
                raise ValueError("no_op must use applies=false")
            if item.structured_adjustment is not None:
                raise ValueError("no_op must use structured_adjustment=null")
            continue

        if item.applies is not True:
            raise ValueError("every non-no_op directive must use applies=true")

        adjustment = item.structured_adjustment
        if not isinstance(adjustment, dict):
            raise ValueError("non-no_op directives require structured_adjustment")

        expected = EXPECTED_KEYS[item.directive_type]
        actual = set(adjustment.keys())
        if actual != expected:
            raise ValueError(
                f"{item.directive_type} structured_adjustment must contain exactly {sorted(expected)}"
            )

        _validate_hours(adjustment["hours"])

        if item.directive_type == "solar_reduction":
            _validate_finite_number(adjustment["factor"], "factor", minimum=0.0, maximum=1.0)

        elif item.directive_type == "minimum_battery_reserve":
            _validate_finite_number(
                adjustment["minimum_energy_kwh"],
                "minimum_energy_kwh",
                minimum=0.0,
                maximum=battery.capacity_kwh,
            )

        elif item.directive_type == "max_grid_window":
            _validate_finite_number(
                adjustment["max_grid_kwh"],
                "max_grid_kwh",
                minimum=0.0,
            )

    return directives
