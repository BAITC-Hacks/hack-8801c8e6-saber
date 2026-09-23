"""FastAPI app: routes, static/ serving, error handlers."""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import engine, storage
from app.ai import agent, analyst
from app.data_loader import BASE_DIR, load_data_or_exit
from app.engine import BLOCKING_ERRORS, Decision
from app.models import OptimizeRequest, ScenarioRequest, SimulateRequest

data = load_data_or_exit()

app = FastAPI(title="Аким на 5 часов")


def _ai_mode_from_env() -> str:
    return "llm" if os.environ.get("LLM_API_KEY") else "fallback"


def _to_decisions(items: list) -> list[Decision]:
    return [Decision(i.measure_id, i.district_id) for i in items]


def _decisions_out(decisions: list[Decision]) -> list[dict]:
    return [{"measure_id": d.measure_id, "district_id": d.district_id} for d in decisions]


def _error_response(errors: list[dict]) -> HTTPException:
    return HTTPException(status_code=422, detail={"errors": errors})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "errors" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"errors": [{"code": "ERROR", "message": str(exc.detail), "detail": {}}]})


@app.get("/api/health")
def api_health() -> dict[str, str]:
    return {"status": "ok", "ai_mode": _ai_mode_from_env()}


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    base_result = engine.simulate([], data)
    return {
        "config": data.config,
        "districts": data.districts,
        "measures": data.measures,
        "result": engine.round_result(base_result),
    }


@app.post("/api/simulate")
def api_simulate(body: SimulateRequest) -> dict[str, Any]:
    decisions = _to_decisions(body.decisions)
    errors = engine.validate(decisions, data, partial=True)
    codes = {e["code"] for e in errors}

    result = None
    if not (codes & BLOCKING_ERRORS):
        valid_decisions = [d for d in decisions if d.measure_id in data.measures_by_id]
        result = engine.round_result(engine.simulate(valid_decisions, data))

    return {"result": result, "errors": errors}


@app.post("/api/scenario")
def api_scenario(body: ScenarioRequest) -> dict[str, Any]:
    decisions = _to_decisions(body.decisions)
    errors = engine.validate(decisions, data, partial=False)
    if errors:
        raise _error_response(errors)

    raw_result = engine.simulate(decisions, data)
    analysis = analyst.analyze(decisions, raw_result, data)
    ai_mode = analysis.pop("ai_mode")

    rounded_result = engine.round_result(raw_result)
    scenario_id = storage.save_scenario(
        body.team,
        _decisions_out(decisions),
        score=raw_result["score_after"],
        total_cost=rounded_result["total_cost"],
    )

    return {
        "id": scenario_id,
        "result": rounded_result,
        "analysis": analysis,
        "ai_mode": ai_mode,
    }


@app.post("/api/optimize")
def api_optimize(body: OptimizeRequest) -> dict[str, Any]:
    decisions = _to_decisions(body.decisions)
    errors = engine.validate(decisions, data, partial=False)
    if errors:
        raise _error_response(errors)

    locked = _to_decisions(body.locked_decisions)
    if len(set(locked)) != len(locked) or not set(locked).issubset(decisions):
        raise _error_response([{
            "code": "INVALID_LOCKS",
            "message": "Закреплять можно только уникальные решения текущего сценария вместе с их районом.",
            "detail": {},
        }])
    return agent.optimize(decisions, data, locked_decisions=locked)


@app.get("/api/leaderboard")
def api_leaderboard() -> dict[str, Any]:
    return {"leaderboard": storage.leaderboard(50)}


app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")
