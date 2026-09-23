"""Pure engine: no LLM, no randomness, no I/O. simulate(), validate(), contributions()."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.data_loader import AppData

INVALID_COUNT = "INVALID_COUNT"
DUPLICATE_MEASURE = "DUPLICATE_MEASURE"
UNKNOWN_MEASURE = "UNKNOWN_MEASURE"
DISTRICT_REQUIRED = "DISTRICT_REQUIRED"
DISTRICT_NOT_ALLOWED = "DISTRICT_NOT_ALLOWED"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
DIRECTION_LIMIT = "DIRECTION_LIMIT"
INCOMPATIBLE = "INCOMPATIBLE"

# Errors that make simulate() impossible to run on the raw decisions.
BLOCKING_ERRORS = {UNKNOWN_MEASURE, DISTRICT_REQUIRED}


@dataclass(frozen=True)
class Decision:
    measure_id: str
    district_id: Optional[str] = None


def _err(code: str, message: str, detail: dict | None = None) -> dict:
    return {"code": code, "message": message, "detail": detail or {}}


def validate(decisions: list[Decision], data: AppData, partial: bool = False) -> list[dict]:
    errors: list[dict] = []
    n = len(decisions)
    expected = data.config["decisions_count"]

    if partial:
        if n > expected:
            errors.append(_err(INVALID_COUNT, f"Решений больше {expected}", {"count": n, "expected": expected}))
    else:
        if n != expected:
            errors.append(_err(INVALID_COUNT, f"Нужно выбрать ровно {expected} решений, выбрано {n}", {"count": n, "expected": expected}))

    seen: set[str] = set()
    dupes: set[str] = set()
    for d in decisions:
        if d.measure_id in seen:
            dupes.add(d.measure_id)
        seen.add(d.measure_id)
    if dupes:
        errors.append(_err(DUPLICATE_MEASURE, f"Мероприятие выбрано более одного раза: {', '.join(sorted(dupes))}", {"measure_ids": sorted(dupes)}))

    unknown = sorted({d.measure_id for d in decisions if d.measure_id not in data.measures_by_id})
    if unknown:
        errors.append(_err(UNKNOWN_MEASURE, f"Неизвестное мероприятие: {', '.join(unknown)}", {"measure_ids": unknown}))

    valid_decisions = [d for d in decisions if d.measure_id in data.measures_by_id]

    for d in valid_decisions:
        m = data.measures_by_id[d.measure_id]
        if m["scope"] == "district":
            if not d.district_id or d.district_id not in data.districts_by_id:
                errors.append(_err(DISTRICT_REQUIRED, f"Для меры «{m['name']}» нужно указать район", {"measure_id": d.measure_id}))
        elif m["scope"] == "city":
            if d.district_id:
                errors.append(_err(DISTRICT_NOT_ALLOWED, f"Мера «{m['name']}» городская, район не указывается", {"measure_id": d.measure_id}))

    budget = data.config["budget"]
    total_cost = sum(data.measures_by_id[d.measure_id]["cost"] for d in valid_decisions)
    if total_cost > budget:
        errors.append(_err(
            BUDGET_EXCEEDED,
            f"Превышение бюджета на {total_cost - budget} усл. ед.",
            {"total_cost": total_cost, "budget": budget},
        ))

    max_per_direction = data.config["max_per_direction"]
    direction_counts: dict[str, int] = {}
    for d in valid_decisions:
        direction = data.measures_by_id[d.measure_id]["direction"]
        direction_counts[direction] = direction_counts.get(direction, 0) + 1
    for direction, count in direction_counts.items():
        if count > max_per_direction:
            name = data.direction_name.get(direction, direction)
            errors.append(_err(
                DIRECTION_LIMIT,
                f"Не больше {max_per_direction} мер направления «{name}», выбрано {count}",
                {"direction": direction, "count": count, "limit": max_per_direction},
            ))

    chosen_district = {d.measure_id: d.district_id for d in valid_decisions}
    chosen_ids = set(chosen_district.keys())
    for inc in data.config.get("incompatibilities", []):
        a, b = inc["pair"]
        if a in chosen_ids and b in chosen_ids:
            if inc.get("same_district_only"):
                if chosen_district.get(a) is not None and chosen_district.get(a) == chosen_district.get(b):
                    errors.append(_err(
                        INCOMPATIBLE,
                        f"Несовместимы {a} и {b} в одном районе: {inc.get('reason', '')}",
                        {"pair": [a, b], "reason": inc.get("reason", "")},
                    ))
            else:
                errors.append(_err(
                    INCOMPATIBLE,
                    f"Несовместимы {a} и {b}: {inc.get('reason', '')}",
                    {"pair": [a, b], "reason": inc.get("reason", "")},
                ))

    return errors


def district_score(indicators: dict[str, float], data: AppData) -> float:
    weight = data.indicator_weight
    return sum(weight[k] * v for k, v in indicators.items())


_district_score = district_score


def _base_indicators(data: AppData) -> dict[str, dict[str, float]]:
    return {d["id"]: dict(d["indicators"]) for d in data.districts}


def _score_from_indicators(indicators: dict[str, dict[str, float]], data: AppData) -> tuple[float, float, float, int, list[dict]]:
    """Returns (score, d_avg, d_min, n_crit, critical) for a fully-formed indicators map."""
    pop = {d["id"]: d["population_share"] for d in data.districts}
    d_scores = {t: _district_score(ind, data) for t, ind in indicators.items()}
    d_avg = sum(pop[t] * d_scores[t] for t in d_scores)
    d_min = min(d_scores.values())

    threshold = data.config["crit_threshold"]
    critical: list[dict] = []
    for t, ind in indicators.items():
        for k, v in ind.items():
            if v < threshold:
                critical.append({"district_id": t, "indicator": k, "value": v})
    n_crit = len(critical)

    lam = data.config["lambda"]
    penalty = data.config["crit_penalty"]
    score = lam * d_avg + (1 - lam) * d_min - penalty * n_crit
    return score, d_avg, d_min, n_crit, critical, d_scores


def simulate(decisions: list[Decision], data: AppData, _with_contributions: bool = True) -> dict[str, Any]:
    H = data.config["horizon_quarters"]
    indicators_before = _base_indicators(data)
    I = {t: dict(ind) for t, ind in indicators_before.items()}

    effective_share = []
    for d in decisions:
        m = data.measures_by_id[d.measure_id]
        f = (H - m["lag"]) / H
        targets = list(I.keys()) if m["scope"] == "city" else [d.district_id]
        for t in targets:
            if t not in I:
                continue
            for k, v in m["effects"].items():
                I[t][k] += v * f
        effective_share.append({"measure_id": d.measure_id, "district_id": d.district_id, "value": f})

    chosen = {d.measure_id: d.district_id for d in decisions if d.measure_id in data.measures_by_id}
    synergies_applied = []
    for s in data.config.get("synergies", []):
        a, b = s["pair"]
        if a in chosen and b in chosen:
            host_district = chosen[s["host"]]
            if host_district in I:
                I[host_district][s["indicator"]] += s["bonus"]
            synergies_applied.append({"pair": s["pair"], "host": s["host"], "indicator": s["indicator"], "bonus": s["bonus"]})

    for t in I:
        for k in I[t]:
            I[t][k] = min(100.0, max(0.0, I[t][k]))

    score_after, d_avg, d_min, n_crit, critical, d_after = _score_from_indicators(I, data)
    score_before, d_avg_before, d_min_before, n_crit_before, critical_before, d_before = _score_from_indicators(indicators_before, data)

    budget = data.config["budget"]
    total_cost = sum(data.measures_by_id[d.measure_id]["cost"] for d in decisions if d.measure_id in data.measures_by_id)

    direction_counts: dict[str, int] = {}
    for d in decisions:
        if d.measure_id not in data.measures_by_id:
            continue
        direction = data.measures_by_id[d.measure_id]["direction"]
        direction_counts[direction] = direction_counts.get(direction, 0) + 1

    weakest_district_id = min(d_after, key=lambda t: d_after[t]) if d_after else None

    districts_out = []
    for d in data.districts:
        t = d["id"]
        districts_out.append({
            "id": t,
            "name": d["name"],
            "indicators_before": indicators_before[t],
            "indicators_after": I[t],
            "d_before": d_before[t],
            "d_after": d_after[t],
        })

    contributions_out: dict[str, float] = {}
    if _with_contributions:
        contributions_out = contributions(decisions, data)

    return {
        "score_before": score_before,
        "score_after": score_after,
        "delta": score_after - score_before,
        "d_avg": d_avg,
        "d_min": d_min,
        "n_crit": n_crit,
        "critical": critical,
        "districts": districts_out,
        "contributions": contributions_out,
        "synergies_applied": synergies_applied,
        "effective_share": effective_share,
        "total_cost": total_cost,
        "budget_left": budget - total_cost,
        "direction_counts": direction_counts,
        "weakest_district_id": weakest_district_id,
    }


def contributions(decisions: list[Decision], data: AppData) -> dict[str, float]:
    valid_decisions = [d for d in decisions if d.measure_id in data.measures_by_id]
    if not valid_decisions:
        return {}
    full_score = simulate(valid_decisions, data, _with_contributions=False)["score_after"]
    out: dict[str, float] = {}
    for i, d in enumerate(valid_decisions):
        subset = valid_decisions[:i] + valid_decisions[i + 1:]
        sub_score = simulate(subset, data, _with_contributions=False)["score_after"]
        out[d.measure_id] = full_score - sub_score
    return out


def round_result(result: dict[str, Any]) -> dict[str, Any]:
    """Rounds a Result dict to 2 decimals for the API response. Does not mutate input."""

    def r2(v: float) -> float:
        return round(v, 2)

    out = dict(result)
    out["score_before"] = r2(result["score_before"])
    out["score_after"] = r2(result["score_after"])
    out["delta"] = r2(result["delta"])
    out["d_avg"] = r2(result["d_avg"])
    out["d_min"] = r2(result["d_min"])
    out["critical"] = [{**c, "value": r2(c["value"])} for c in result["critical"]]
    out["districts"] = [
        {
            **dd,
            "indicators_before": {k: r2(v) for k, v in dd["indicators_before"].items()},
            "indicators_after": {k: r2(v) for k, v in dd["indicators_after"].items()},
            "d_before": r2(dd["d_before"]),
            "d_after": r2(dd["d_after"]),
        }
        for dd in result["districts"]
    ]
    out["contributions"] = {k: r2(v) for k, v in result["contributions"].items()}
    out["effective_share"] = [{**e, "value": r2(e["value"])} for e in result["effective_share"]]
    out["budget_left"] = result["budget_left"]
    out["total_cost"] = result["total_cost"]
    return out
