import pytest

from app.guardrails import validate_directives
from app.schemas import BatteryInput, DirectiveInterpretation


BATTERY = BatteryInput(
    capacity_kwh=500,
    initial_energy_kwh=200,
    minimum_energy_kwh=50,
    max_charge_kwh_per_hour=100,
    max_discharge_kwh_per_hour=100,
)


def test_valid_no_charge():
    directives = [
        DirectiveInterpretation(
            note_index=0,
            applies=True,
            directive_type="no_charge_window",
            structured_adjustment={"hours": [14, 15]},
            explanation="No charging.",
        )
    ]
    assert validate_directives(directives, 1, BATTERY) == directives


def test_no_op_semantics():
    directives = [
        DirectiveInterpretation(
            note_index=0,
            applies=False,
            directive_type="no_op",
            structured_adjustment=None,
            explanation="Irrelevant.",
        )
    ]
    validate_directives(directives, 1, BATTERY)


def test_unsorted_hours_rejected():
    directives = [
        DirectiveInterpretation(
            note_index=0,
            applies=True,
            directive_type="no_discharge_window",
            structured_adjustment={"hours": [15, 14]},
            explanation="No discharge.",
        )
    ]
    with pytest.raises(ValueError):
        validate_directives(directives, 1, BATTERY)
