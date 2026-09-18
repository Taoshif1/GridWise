from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _finite_nonnegative(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


class HourInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hour: int = Field(ge=0, le=23)
    demand_kwh: float
    solar_kwh: float
    tariff_bdt_per_kwh: float

    @field_validator("demand_kwh", "solar_kwh", "tariff_bdt_per_kwh")
    @classmethod
    def validate_nonnegative_number(cls, v: float, info):
        return _finite_nonnegative(v, info.field_name)


class BatteryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float
    initial_energy_kwh: float
    minimum_energy_kwh: float
    max_charge_kwh_per_hour: float
    max_discharge_kwh_per_hour: float

    @field_validator(
        "capacity_kwh",
        "initial_energy_kwh",
        "minimum_energy_kwh",
        "max_charge_kwh_per_hour",
        "max_discharge_kwh_per_hour",
    )
    @classmethod
    def validate_nonnegative_number(cls, v: float, info):
        return _finite_nonnegative(v, info.field_name)

    @model_validator(mode="after")
    def validate_relationships(self):
        if self.capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be greater than 0")
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        return self


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1, max_length=200)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourInput]
    battery: BatteryInput

    @field_validator("scenario_id")
    @classmethod
    def scenario_id_not_blank(cls, v: str):
        if not v.strip():
            raise ValueError("scenario_id must not be blank")
        return v

    @field_validator("operator_notes")
    @classmethod
    def notes_not_blank(cls, notes: list[str]):
        if not 1 <= len(notes) <= 3:
            raise ValueError("operator_notes must contain 1 to 3 notes")
        cleaned = []
        for note in notes:
            if not isinstance(note, str) or not note.strip():
                raise ValueError("operator_notes may not contain blank notes")
            cleaned.append(note.strip())
        return cleaned

    @model_validator(mode="after")
    def validate_hours(self):
        if len(self.hours) != 24:
            raise ValueError("hours must contain exactly 24 entries")
        hour_values = [item.hour for item in self.hours]
        if sorted(hour_values) != list(range(24)):
            raise ValueError("hours must contain each unique hour 0 through 23 exactly once")
        return self

    def ordered_hours(self) -> list[HourInput]:
        return sorted(self.hours, key=lambda x: x.hour)


DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]


class DirectiveInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: dict[str, Any] | None
    explanation: str = Field(min_length=1, max_length=500)


class LLMDirectiveEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directives: list[DirectiveInterpretation]


BatteryAction = Literal["charge", "discharge", "idle"]


class HourPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hour: int = Field(ge=0, le=23)
    grid_kwh: float = Field(ge=0)
    solar_used_kwh: float = Field(ge=0)
    battery_action: BatteryAction
    battery_kwh: float = Field(ge=0)
    battery_energy_after_kwh: float = Field(ge=0)


class OptimizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourPlan]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
