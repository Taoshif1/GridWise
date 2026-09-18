from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .guardrails import validate_directives
from .llm import LLMServiceError, build_interpreter
from .optimizer import OptimizationError, optimize_schedule
from .schemas import OptimizeRequest, OptimizeResponse
from .validator import PlanValidationError, validate_plan


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("gridwise")

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
SAMPLE_FILE = ROOT / "sample_request.json"

app = FastAPI(
    title="GridWise LLM Energy Optimizer",
    version="2.0.0",
    docs_url="/docs",
    redoc_url=None,
)

app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


@app.exception_handler(RequestValidationError)
async def request_validation_handler(_: Request, exc: RequestValidationError):
    safe_errors = []
    for err in exc.errors():
        safe_errors.append(
            {
                "loc": list(err.get("loc", [])),
                "type": err.get("type", "validation_error"),
                "msg": err.get("msg", "Invalid value"),
            }
        )
    return JSONResponse(
        status_code=400,
        content={"detail": "Malformed or structurally invalid request", "errors": safe_errors},
    )


@app.get("/", include_in_schema=False)
async def frontend():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/ui-config", include_in_schema=False)
async def ui_config():
    settings = get_settings()
    return {
        "model": settings.gemini_model,
        "mode": settings.llm_mode,
        "api_ready": bool(settings.gemini_api_keys) if settings.llm_mode == "gemini" else True,
        "configured_key_count": len(settings.gemini_api_keys),
        "port": settings.port,
    }


@app.get("/sample-request", include_in_schema=False)
async def sample_request():
    with SAMPLE_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(request: OptimizeRequest):
    settings = get_settings()

    try:
        interpreter = build_interpreter(settings)

        directives = await interpreter.interpret(request.operator_notes, request.battery)

        validate_directives(
            directives,
            note_count=len(request.operator_notes),
            battery=request.battery,
        )

        hourly_plan, total_grid, total_cost, peak_grid = optimize_schedule(
            request,
            directives,
        )

        validate_plan(
            request,
            directives,
            hourly_plan,
            total_grid,
            total_cost,
            peak_grid,
        )

        summary = (
            "The plan uses available solar and shifts energy through the battery "
            "to reduce grid cost while respecting all validated operator directives "
            "and returning the battery to its initial end-of-day energy."
        )

        return OptimizeResponse(
            scenario_id=request.scenario_id,
            directive_interpretation=directives,
            hourly_plan=hourly_plan,
            total_grid_kwh=total_grid,
            total_cost_bdt=total_cost,
            peak_grid_kwh=peak_grid,
            plan_summary=summary,
        )

    except LLMServiceError as exc:
        logger.warning("Controlled Gemini failure: %s", exc)
        raise HTTPException(status_code=500, detail="Controlled Gemini model-provider failure")

    except (OptimizationError, PlanValidationError, ValueError) as exc:
        logger.warning("Controlled semantic/optimization failure: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc))

    except Exception:
        logger.exception("Unexpected controlled internal failure")
        raise HTTPException(status_code=500, detail="Controlled internal error")
