"""AI agent that searches for a better decision set using engine tools, with a hill_climb fallback (spec 7.2 / 7.4)."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app import engine, optimizer
from app.ai import llm, prompts
from app.data_loader import AppData
from app.engine import Decision
from app.models import DecisionIn

logger = logging.getLogger(__name__)


class _BadAgent(ValueError):
    pass


class AgentResponse(BaseModel):
    model_config = ConfigDict(strict=True, str_strip_whitespace=True)
    best_decisions: list[DecisionIn] = Field(min_length=5, max_length=5)
    explanation: str = Field(min_length=1, max_length=8000)


def _parse_decisions(raw: Any) -> list[Decision]:
    items = TypeAdapter(list[DecisionIn]).validate_python(raw, strict=True)
    return [Decision(item.measure_id, item.district_id) for item in items]


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


def _handler_simulate(inp: dict, data: AppData, locked_decisions: list[Decision] | None = None, objective: optimizer.Objective = "score") -> dict:
    raw_decisions = inp.get("decisions") or []
    candidate = _parse_decisions(raw_decisions)
    errors = engine.validate(candidate, data)
    if not set(locked_decisions or []).issubset(candidate):
        errors.append({"code": "LOCKED_DECISION_CHANGED"})
    if errors:
        return {
            "valid": False,
            "score": None,
            "d_min": None,
            "n_crit": None,
            "objective": objective,
            "objective_values": None,
            "total_cost": None,
            "errors": [e["code"] for e in errors],
            "weakest_district": None,
            "synergies_applied": [],
        }
    result = engine.simulate(candidate, data, _with_contributions=False)
    return {
        "valid": True,
        "score": result["score_after"],
        "d_min": result["d_min"],
        "n_crit": result["n_crit"],
        "objective": objective,
        "objective_values": optimizer.objective_values(result, objective),
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


def _build_agent_user(decisions: list[Decision], result: dict[str, Any], data: AppData, objective: optimizer.Objective = "score", locked_decisions: list[Decision] | None = None) -> str:
    payload = {
        "objective": objective,
        "objective_description": optimizer.OBJECTIVES[objective]["description"],
        "objective_values": optimizer.objective_values(result, objective),
        "locked_decisions": _decision_dicts(locked_decisions or []),
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
        "score": result["score_after"],
        "d_min": result["d_min"],
        "n_crit": result["n_crit"],
        "critical": [
            {
                "district_name": data.districts_by_id[c["district_id"]]["name"],
                "indicator_name": data.indicator_name[c["indicator"]],
                "value": c["value"],
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
            "d_min": output.get("d_min"),
            "n_crit": output.get("n_crit"),
            "objective": output.get("objective"),
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
            "d_min": round(step["d_min"], 2),
            "n_crit": step["n_crit"],
            "valid": True,
            "error": None,
        })
    return out


def _explain_strategy(original_decisions: list[Decision], decisions: list[Decision], original: dict, result: dict, data: AppData, objective: optimizer.Objective, source: str) -> str:
    """Only report engine-verified metric changes, including an intentional Score loss."""
    label = optimizer.OBJECTIVES[objective]["label"]
    improved = optimizer.compare_results(result, original, objective) > 0
    status = "Найдено улучшение по выбранной цели." if improved else "Улучшение по выбранной цели в проверенных вариантах не найдено."
    delta = result["score_after"] - original["score_after"]
    score_verb = "снизился" if delta < -optimizer.COMPARISON_TOLERANCE else "вырос" if delta > optimizer.COMPARISON_TOLERANCE else "не изменился"
    parts = [
        f"Цель «{label}». {status}",
        f"Минимальный балл районов: {original['d_min']:.2f} → {result['d_min']:.2f}; критических показателей: {original['n_crit']} → {result['n_crit']}.",
        f"Score {score_verb}: {original['score_after']:.2f} → {result['score_after']:.2f} ({delta:+.2f}).",
    ]
    if objective != "score" and delta < -optimizer.COMPARISON_TOLERANCE:
        parts.append("Это компромисс: приоритет выбранной цели выше общего Score.")
    removed = [d for d in original_decisions if d not in decisions]
    added = [d for d in decisions if d not in original_decisions]
    if removed:
        parts.append("Убраны: " + ", ".join(_decision_label(d, data) for d in _decision_dicts(removed)) + ".")
    if added:
        parts.append("Добавлены: " + ", ".join(_decision_label(d, data) for d in _decision_dicts(added)) + ".")
    if source == "baseline":
        parts.append("Детерминированный поиск проверяет одиночные замены до пяти шагов; глобальный оптимум не гарантирован.")
    else:
        parts.append("Предложение AI проверено движком по выбранной цели и закреплённым решениям.")
    return " ".join(parts)


def optimize(decisions: list[Decision], data: AppData, *, locked_decisions: list[Decision] | None = None, objective: optimizer.Objective = "score") -> dict[str, Any]:
    locked_decisions = locked_decisions or []
    original_result = engine.simulate(decisions, data, _with_contributions=False)
    original_score = original_result["score_after"]
    optimizer.objective_values(original_result, objective)
    baselines = {
        goal: optimizer.hill_climb(decisions, data, locked_decisions=locked_decisions, objective=goal)
        for goal in optimizer.OBJECTIVES
    }
    baseline = baselines[objective]
    baseline_score = baseline["score"]
    baseline_decisions = baseline["decisions"]
    baseline_hyp = _baseline_hypotheses(baseline["steps"], data)

    agent_decisions: list[Decision] | None = None
    agent_result: dict | None = None
    agent_hyp: list[dict] = []
    ai_mode = "fallback"

    try:
        if len(locked_decisions) == len(decisions):
            raise _BadAgent("all decisions are locked; no search needed")
        max_calls = int(os.environ.get("AGENT_MAX_TOOL_CALLS", "8"))
        user = _build_agent_user(decisions, original_result, data, objective, locked_decisions)
        handlers = {
            "simulate": lambda inp: _handler_simulate(inp, data, locked_decisions, objective),
            "list_measures": lambda inp: _handler_list_measures(inp, data),
            "get_district": lambda inp: _handler_get_district(inp, data),
        }
        final_text, trace = llm.run_tools(prompts.AGENT_SYSTEM, user, prompts.AGENT_TOOLS, handlers, max_calls)
        parsed = AgentResponse.model_validate(_extract_json(final_text))
        candidate = [Decision(d.measure_id, d.district_id) for d in parsed.best_decisions]
        if engine.validate(candidate, data):
            raise _BadAgent("agent decisions failed validation")
        if not set(locked_decisions).issubset(candidate):
            raise _BadAgent("agent changed a locked decision")
        agent_result = engine.simulate(candidate, data, _with_contributions=False)
        agent_decisions = candidate
        agent_hyp = _hypotheses_from_trace(trace)
        ai_mode = "llm"
    except (llm.LLMUnavailable, _BadAgent, ValueError, KeyError, TypeError) as e:
        logger.warning("agent LLM path fell back to hill_climb: %s", e)
        agent_decisions = None
        agent_result = None
        ai_mode = "fallback"

    if agent_decisions is not None and agent_result is not None and optimizer.compare_results(agent_result, baseline["result"], objective) >= 0:
        source = "agent"
        best_decisions = agent_decisions
        best_result = agent_result
        hypotheses = agent_hyp or baseline_hyp
    else:
        source = "baseline"
        best_decisions = baseline_decisions
        best_result = baseline["result"]
        hypotheses = (agent_hyp + baseline_hyp) if agent_hyp else baseline_hyp

    if optimizer.compare_results(best_result, original_result, objective) < 0:
        source = "baseline"
        best_decisions = decisions
        best_result = original_result

    best_score = best_result["score_after"]
    delta = best_score - original_score
    explanation = _explain_strategy(decisions, best_decisions, original_result, best_result, data, objective, source)

    if len(locked_decisions) == len(decisions):
        explanation = "Все пять решений закреплены. Снимите закрепление хотя бы с одного, чтобы искать улучшение."

    strategies = []
    for goal, details in optimizer.OBJECTIVES.items():
        selected = goal == objective
        strategy_decisions = best_decisions if selected else baselines[goal]["decisions"]
        strategy_source = source if selected else "baseline"
        result = engine.simulate(strategy_decisions, data)
        strategies.append({
            "objective": goal,
            **details,
            "decisions": _decision_dicts(strategy_decisions),
            "result": engine.round_result(result),
            "source": strategy_source,
            "explanation": explanation if selected else _explain_strategy(decisions, strategy_decisions, original_result, result, data, goal, strategy_source),
        })
    selected_strategy = next(s for s in strategies if s["objective"] == objective)

    return {
        "objective": objective,
        "strategies": strategies,
        "original_score": round(original_score, 2),
        "best_decisions": _decision_dicts(best_decisions),
        "best_score": round(best_score, 2),
        "delta": round(delta, 2),
        "explanation": explanation,
        "hypotheses": hypotheses,
        "baseline": {"method": "hill_climb", "score": round(baseline_score, 2), "objective": objective},
        "source": source,
        "ai_mode": ai_mode,
        "locked_decisions": _decision_dicts(locked_decisions),
        "original_result": engine.round_result(engine.simulate(decisions, data)),
        "best_result": selected_strategy["result"],
    }
