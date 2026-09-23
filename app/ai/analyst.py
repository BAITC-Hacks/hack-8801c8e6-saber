"""AI analysis of a scenario result, with a deterministic fallback (spec 7.1 / 7.4)."""
from __future__ import annotations

import json
import logging
from typing import Any

from app import engine
from app.ai import llm, prompts
from app.data_loader import AppData
from app.engine import Decision

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = ("summary", "strengths", "risks", "consequences", "main_tradeoff", "weakest_district")


class _BadAnalysis(ValueError):
    pass


def _decision_district(decisions: list[Decision], measure_id: str) -> str | None:
    for d in decisions:
        if d.measure_id == measure_id:
            return d.district_id
    return None


def _build_payload(decisions: list[Decision], result: dict[str, Any], data: AppData) -> dict[str, Any]:
    base = engine.simulate([], data, _with_contributions=False)
    contributions = result["contributions"]

    decisions_payload = []
    for d in decisions:
        m = data.measures_by_id.get(d.measure_id)
        if not m:
            continue
        share = next(
            (e["value"] for e in result["effective_share"] if e["measure_id"] == d.measure_id and e["district_id"] == d.district_id),
            None,
        )
        decisions_payload.append({
            "measure_name": m["name"],
            "direction_name": data.direction_name.get(m["direction"], m["direction"]),
            "district_name": data.districts_by_id[d.district_id]["name"] if d.district_id else None,
            "cost": m["cost"],
            "lag": m["lag"],
            "effective_share": round(share, 4) if share is not None else None,
            "contribution": round(contributions.get(d.measure_id, 0.0), 2),
        })

    critical_after = [
        {
            "district_name": data.districts_by_id[c["district_id"]]["name"],
            "indicator_name": data.indicator_name[c["indicator"]],
            "value": round(c["value"], 2),
        }
        for c in result["critical"]
    ]

    districts_payload = []
    for dd in result["districts"]:
        src = data.districts_by_id[dd["id"]]
        districts_payload.append({
            "name": dd["name"],
            "population_share": src["population_share"],
            "profile": src["profile"],
            "d_before": round(dd["d_before"], 2),
            "d_after": round(dd["d_after"], 2),
            "indicators_before": {data.indicator_name[k]: round(v, 2) for k, v in dd["indicators_before"].items()},
            "indicators_after": {data.indicator_name[k]: round(v, 2) for k, v in dd["indicators_after"].items()},
        })

    synergies_payload = [
        {
            "measures": [data.measures_by_id[m]["name"] for m in s["pair"]],
            "indicator": data.indicator_name[s["indicator"]],
            "bonus": s["bonus"],
        }
        for s in result["synergies_applied"]
    ]

    return {
        "budget": data.config["budget"],
        "total_cost": result["total_cost"],
        "score_before": round(result["score_before"], 2),
        "score_after": round(result["score_after"], 2),
        "n_crit_before": base["n_crit"],
        "n_crit_after": result["n_crit"],
        "critical_after": critical_after,
        "decisions": decisions_payload,
        "synergies_applied": synergies_payload,
        "districts": districts_payload,
        "weakest_district_id": result["weakest_district_id"],
    }


def _validate(response: Any, data: AppData) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise _BadAnalysis("response is not a JSON object")
    for field in REQUIRED_FIELDS:
        if field not in response or response[field] in (None, "", []):
            raise _BadAnalysis(f"missing or empty field: {field}")
    if response["weakest_district"] not in data.districts_by_id:
        raise _BadAnalysis("weakest_district unknown")
    return response


def _fallback(decisions: list[Decision], result: dict[str, Any], data: AppData) -> dict[str, Any]:
    contributions = result["contributions"]
    sorted_contribs = sorted(contributions.items(), key=lambda kv: kv[1], reverse=True)

    strengths: list[str] = []
    for mid, val in sorted_contribs[:2]:
        m = data.measures_by_id[mid]
        district_id = _decision_district(decisions, mid)
        where = f" в {data.districts_by_id[district_id]['name']}" if district_id else ""
        sign = "+" if val >= 0 else ""
        strengths.append(f"«{m['name']}»{where}: {sign}{val:.2f} к Score")
    for s in result["synergies_applied"]:
        a, b = s["pair"]
        strengths.append(
            f"Сработала синергия «{data.measures_by_id[a]['name']}» и «{data.measures_by_id[b]['name']}»: "
            f"+{s['bonus']} к показателю «{data.indicator_name[s['indicator']]}»"
        )

    risks: list[str] = []
    for c in result["critical"]:
        risks.append(f"Критично: {data.districts_by_id[c['district_id']]['name']}, «{data.indicator_name[c['indicator']]}» = {c['value']:.2f}")
    for d in decisions:
        m = data.measures_by_id.get(d.measure_id)
        if m and m["lag"] >= 3:
            share = (data.config["horizon_quarters"] - m["lag"]) / data.config["horizon_quarters"]
            risks.append(f"«{m['name']}» реализовалась на {share * 100:.0f}% эффекта за горизонт из-за лага {m['lag']} кв.")
    for d in decisions:
        m = data.measures_by_id.get(d.measure_id)
        if not m:
            continue
        for k, v in m["effects"].items():
            if v < 0:
                risks.append(f"«{m['name']}» снижает «{data.indicator_name[k]}» на {abs(v)} пункта")

    consequences: list[str] = []
    growth_sorted = sorted(result["districts"], key=lambda dd: dd["d_after"] - dd["d_before"], reverse=True)
    if growth_sorted:
        best = growth_sorted[0]
        worst = growth_sorted[-1]
        consequences.append(f"{best['name']} вырос сильнее всех: D {best['d_before']:.2f} → {best['d_after']:.2f}")
        if worst["id"] != best["id"]:
            consequences.append(f"{worst['name']} вырос меньше всех: D {worst['d_before']:.2f} → {worst['d_after']:.2f}")
    lagged = [d for d in decisions if data.measures_by_id.get(d.measure_id, {}).get("lag", 0) > 0]
    if lagged:
        names = ", ".join(f"«{data.measures_by_id[d.measure_id]['name']}»" for d in lagged)
        consequences.append(f"После горизонта полностью заработают меры с лагом: {names}")

    main_tradeoff = "Недостаточно данных для сравнения эффективности."
    valid_decisions = [d for d in decisions if d.measure_id in data.measures_by_id]
    if valid_decisions:
        most_expensive = max(valid_decisions, key=lambda d: data.measures_by_id[d.measure_id]["cost"])
        efficiency = {
            d.measure_id: contributions.get(d.measure_id, 0.0) / data.measures_by_id[d.measure_id]["cost"]
            for d in valid_decisions
        }
        most_efficient_id = max(efficiency, key=efficiency.get)
        me = data.measures_by_id[most_expensive.measure_id]
        mf = data.measures_by_id[most_efficient_id]
        main_tradeoff = (
            f"«{me['name']}» стоит {me['cost']} усл. ед. и даёт {contributions.get(most_expensive.measure_id, 0.0):.2f} к Score, "
            f"а «{mf['name']}» самая эффективная — {efficiency[most_efficient_id]:.3f} Score на 1 усл. ед."
        )

    delta = result["delta"]
    summary = (
        f"Score изменился с {result['score_before']:.2f} до {result['score_after']:.2f} "
        f"({'+' if delta >= 0 else ''}{delta:.2f}). Критических значений: {result['n_crit']}."
    )

    return {
        "summary": summary,
        "strengths": strengths or ["Нет решений с положительным вкладом."],
        "risks": risks or ["Критических значений и явных рисков не найдено."],
        "consequences": consequences or ["Существенных изменений после горизонта не ожидается."],
        "main_tradeoff": main_tradeoff,
        "weakest_district": result.get("weakest_district_id"),
    }


def analyze(decisions: list[Decision], result: dict[str, Any], data: AppData) -> dict[str, Any]:
    payload = _build_payload(decisions, result, data)
    user = json.dumps(payload, ensure_ascii=False)

    response = None
    for _ in range(2):
        try:
            candidate = llm.complete_json(prompts.ANALYST_SYSTEM, user)
            response = _validate(candidate, data)
            break
        except (llm.LLMUnavailable, _BadAnalysis) as e:
            logger.warning("analyst LLM attempt failed, retrying/falling back: %s", e)
            response = None
            continue

    if response is not None:
        response = {k: response[k] for k in REQUIRED_FIELDS}
        response["ai_mode"] = "llm"
        return response

    fallback = _fallback(decisions, result, data)
    fallback["ai_mode"] = "fallback"
    return fallback
