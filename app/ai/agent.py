"""AI agent that searches for a better decision set using engine tools, with a hill_climb fallback (spec 7.2 / 7.4)."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from app import engine, optimizer
from app.ai import llm, prompts
from app.data_loader import AppData
from app.engine import Decision

logger = logging.getLogger(__name__)


class _BadAgent(ValueError):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def _decision_label(d: dict, data: AppData) -> str:
    measure_id = d.get("measure_id")
    district_id = d.get("district_id")
    name = data.measures_by_id.get(measure_id, {}).get("name", measure_id)
    if district_id and district_id in data.districts_by_id:
        return f"«{name}» в {data.districts_by_id[district_id]['name']}"
    return f"«{name}»"


def _decision_dicts(decisions: list[Decision]) -> list[dict]:
    return [{"measure_id": d.measure_id, "district_id": d.district_id} for d in decisions]


def _handler_simulate(inp: dict, data: AppData) -> dict:
    raw_decisions = inp.get("decisions") or []
    candidate = [Decision(d.get("measure_id"), d.get("district_id")) for d in raw_decisions]
    errors = engine.validate(candidate, data)
    if errors:
        return {
            "valid": False,
            "score": None,
            "n_crit": None,
            "total_cost": None,
            "errors": [e["code"] for e in errors],
            "weakest_district": None,
            "synergies_applied": [],
        }
    result = engine.simulate(candidate, data, _with_contributions=False)
    return {
        "valid": True,
        "score": round(result["score_after"], 2),
        "n_crit": result["n_crit"],
        "total_cost": result["total_cost"],
        "errors": [],
        "weakest_district": result["weakest_district_id"],
        "synergies_applied": [s["pair"] for s in result["synergies_applied"]],
    }


def _handler_list_measures(inp: dict, data: AppData) -> dict:
    direction = (inp or {}).get("direction")
    measures = data.measures if not direction else [m for m in data.measures if m["direction"] == direction]
    out = []
    for m in measures:
        synergies = [s for s in data.config.get("synergies", []) if m["id"] in s["pair"]]
        incompatibilities = [s for s in data.config.get("incompatibilities", []) if m["id"] in s["pair"]]
        out.append({
            "id": m["id"],
            "name": m["name"],
            "scope": m["scope"],
            "cost": m["cost"],
            "lag": m["lag"],
            "effects": m["effects"],
            "synergies": synergies,
            "incompatibilities": incompatibilities,
        })
    return {"measures": out}


def _handler_get_district(inp: dict, data: AppData) -> dict:
    district_id = (inp or {}).get("district_id")
    d = data.districts_by_id.get(district_id)
    if not d:
        return {"error": "unknown district_id"}
    return {
        "name": d["name"],
        "population_share": d["population_share"],
        "profile": d["profile"],
        "indicators": d["indicators"],
        "D": round(engine.district_score(d["indicators"], data), 2),
    }


def _build_agent_user(decisions: list[Decision], result: dict[str, Any], data: AppData) -> str:
    payload = {
        "current_decisions": [
            {
                "measure_id": d.measure_id,
                "measure_name": data.measures_by_id[d.measure_id]["name"],
                "district_id": d.district_id,
                "district_name": data.districts_by_id[d.district_id]["name"] if d.district_id else None,
                "cost": data.measures_by_id[d.measure_id]["cost"],
            }
            for d in decisions
            if d.measure_id in data.measures_by_id
        ],
        "score": round(result["score_after"], 2),
        "n_crit": result["n_crit"],
        "critical": [
            {
                "district_name": data.districts_by_id[c["district_id"]]["name"],
                "indicator_name": data.indicator_name[c["indicator"]],
                "value": round(c["value"], 2),
            }
            for c in result["critical"]
        ],
        "weakest_district_id": result["weakest_district_id"],
        "budget": data.config["budget"],
        "total_cost": result["total_cost"],
        "budget_left": result["budget_left"],
    }
    return json.dumps(payload, ensure_ascii=False)


def _hypotheses_from_trace(trace: list[dict]) -> list[dict]:
    out = []
    for t in trace:
        if t["tool"] != "simulate":
            continue
        output = t["output"]
        valid = bool(output.get("valid"))
        out.append({
            "idea": t["input"].get("idea", ""),
            "decisions": t["input"].get("decisions", []),
            "score": output.get("score"),
            "valid": valid,
            "error": None if valid else (output.get("errors") or [None])[0],
        })
    return out


def _baseline_hypotheses(steps: list[dict], data: AppData) -> list[dict]:
    out = []
    for step in steps:
        removed = [d for d in step["from"] if d not in step["to"]]
        added = [d for d in step["to"] if d not in step["from"]]
        if removed and added:
            idea = f"Заменить {_decision_label(removed[0], data)} на {_decision_label(added[0], data)}"
        else:
            idea = "Изменить набор решений"
        out.append({
            "idea": idea,
            "decisions": step["to"],
            "score": round(step["score"], 2),
            "valid": True,
            "error": None,
        })
    return out


def _explain_baseline(steps: list[dict], original_score: float, best_score: float, data: AppData) -> str:
    if not steps:
        return "Улучшение не найдено: соседние допустимые варианты не дают прироста Score."
    parts = []
    for step in steps:
        removed = [d for d in step["from"] if d not in step["to"]]
        added = [d for d in step["to"] if d not in step["from"]]
        if removed and added:
            parts.append(f"{_decision_label(removed[0], data)} заменена на {_decision_label(added[0], data)}")
    delta = best_score - original_score
    joined = "; ".join(parts) if parts else "набор скорректирован"
    sign = "+" if delta >= 0 else ""
    return f"Метод hill_climb нашёл улучшение: {joined}. Score вырос с {original_score:.2f} до {best_score:.2f} ({sign}{delta:.2f})."


def optimize(decisions: list[Decision], data: AppData) -> dict[str, Any]:
    original_result = engine.simulate(decisions, data, _with_contributions=False)
    original_score = original_result["score_after"]

    baseline = optimizer.hill_climb(decisions, data)
    baseline_score = baseline["score"]
    baseline_decisions = baseline["decisions"]
    baseline_hyp = _baseline_hypotheses(baseline["steps"], data)

    agent_decisions: list[Decision] | None = None
    agent_score: float | None = None
    agent_explanation: str | None = None
    agent_hyp: list[dict] = []
    ai_mode = "fallback"

    try:
        max_calls = int(os.environ.get("AGENT_MAX_TOOL_CALLS", "8"))
        user = _build_agent_user(decisions, original_result, data)
        handlers = {
            "simulate": lambda inp: _handler_simulate(inp, data),
            "list_measures": lambda inp: _handler_list_measures(inp, data),
            "get_district": lambda inp: _handler_get_district(inp, data),
        }
        final_text, trace = llm.run_tools(prompts.AGENT_SYSTEM, user, prompts.AGENT_TOOLS, handlers, max_calls)
        parsed = _extract_json(final_text)
        raw_decisions = parsed.get("best_decisions")
        if not raw_decisions:
            raise _BadAgent("no best_decisions in agent response")
        candidate = [Decision(d.get("measure_id"), d.get("district_id")) for d in raw_decisions]
        if engine.validate(candidate, data):
            raise _BadAgent("agent decisions failed validation")
        explanation = (parsed.get("explanation") or "").strip()
        if not explanation:
            raise _BadAgent("empty explanation")

        agent_result = engine.simulate(candidate, data, _with_contributions=False)
        agent_decisions = candidate
        agent_score = agent_result["score_after"]
        agent_explanation = explanation
        agent_hyp = _hypotheses_from_trace(trace)
        ai_mode = "llm"
    except (llm.LLMUnavailable, _BadAgent, ValueError, KeyError, TypeError) as e:
        logger.warning("agent LLM path fell back to hill_climb: %s", e)
        agent_decisions = None
        agent_score = None
        ai_mode = "fallback"

    if agent_decisions is not None and agent_score is not None and (baseline_score is None or agent_score >= baseline_score - 1e-9):
        source = "agent"
        best_decisions = agent_decisions
        best_score = agent_score
        explanation = agent_explanation or ""
        hypotheses = agent_hyp or baseline_hyp
    else:
        source = "baseline"
        best_decisions = baseline_decisions
        best_score = baseline_score if baseline_score is not None else original_score
        explanation = _explain_baseline(baseline["steps"], original_score, best_score, data)
        hypotheses = (agent_hyp + baseline_hyp) if agent_hyp else baseline_hyp

    if best_score is None or best_score < original_score - 1e-9:
        best_decisions = decisions
        best_score = original_score
        delta = 0.0
        explanation = "Улучшение не найдено, возвращён исходный набор."
    else:
        delta = best_score - original_score

    return {
        "original_score": round(original_score, 2),
        "best_decisions": _decision_dicts(best_decisions),
        "best_score": round(best_score, 2),
        "delta": round(delta, 2),
        "explanation": explanation,
        "hypotheses": hypotheses,
        "baseline": {"method": "hill_climb", "score": round(baseline_score, 2) if baseline_score is not None else None},
        "source": source,
        "ai_mode": ai_mode,
    }
