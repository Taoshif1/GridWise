import json
from pathlib import Path

from app.guardrails import validate_directives
from app.optimizer import optimize_schedule
from app.schemas import DirectiveInterpretation, OptimizeRequest
from app.validator import validate_plan


ROOT = Path(__file__).resolve().parents[1]


def load_request():
    return OptimizeRequest.model_validate(
        json.loads((ROOT / "sample_request.json").read_text(encoding="utf-8"))
    )


def test_optimizer_with_all_major_constraints():
    request = load_request()

    directives = [
        DirectiveInterpretation(
            note_index=0,
            applies=True,
            directive_type="solar_reduction",
            structured_adjustment={"hours": [13, 14], "factor": 0.2},
            explanation="Solar reduced.",
        ),
        DirectiveInterpretation(
            note_index=1,
            applies=True,
            directive_type="no_charge_window",
            structured_adjustment={"hours": [14, 15]},
            explanation="No charging.",
        ),
        DirectiveInterpretation(
            note_index=2,
            applies=False,
            directive_type="no_op",
            structured_adjustment=None,
            explanation="Irrelevant.",
        ),
    ]

    validate_directives(directives, 3, request.battery)

    plan, total_grid, total_cost, peak = optimize_schedule(request, directives)

    assert len(plan) == 24
    assert [p.hour for p in plan] == list(range(24))
    assert plan[-1].battery_energy_after_kwh == request.battery.initial_energy_kwh

    validate_plan(
        request,
        directives,
        plan,
        total_grid,
        total_cost,
        peak,
    )


def test_reserve_and_grid_cap():
    request = load_request()

    directives = [
        DirectiveInterpretation(
            note_index=0,
            applies=True,
            directive_type="minimum_battery_reserve",
            structured_adjustment={"hours": [18, 19, 20], "minimum_energy_kwh": 150},
            explanation="Maintain reserve.",
        ),
        DirectiveInterpretation(
            note_index=1,
            applies=True,
            directive_type="max_grid_window",
            structured_adjustment={"hours": [17], "max_grid_kwh": 250},
            explanation="Grid cap.",
        ),
    ]

    validate_directives(directives, 2, request.battery)
    plan, tg, tc, pg = optimize_schedule(request, directives)
    validate_plan(request, directives, plan, tg, tc, pg)

    for h in [18, 19, 20]:
        assert plan[h].battery_energy_after_kwh >= 150 - 0.01
    assert plan[17].grid_kwh <= 250 + 0.01
