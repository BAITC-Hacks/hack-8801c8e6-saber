"""Deterministic search over decision sets. No LLM involved."""
from __future__ import annotations

import itertools
from typing import Any, Literal

from app.data_loader import AppData
from app.engine import Decision, simulate, validate

Objective = Literal["score", "weakest", "critical"]
OBJECTIVES: dict[Objective, dict[str, str]] = {
    "score": {
        "label": "Максимальный Score",
        "description": "Максимизировать общий Score.",
    },
    "weakest": {
        "label": "Поддержка слабейшего района",
        "description": "Сначала повысить минимальный балл среди всех районов; при равенстве — общий Score.",
    },
    "critical": {
        "label": "Меньше критических показателей",
        "description": "Сначала сократить число показателей ниже критического порога; при равенстве — повысить Score.",
    },
}
COMPARISON_TOLERANCE = 1e-9


def objective_values(result: dict[str, Any], objective: Objective = "score") -> tuple[float, ...]:
    """Unrounded values to maximize, ordered by lexicographic priority."""
    if objective == "score":
        return (result["score_after"],)
    if objective == "weakest":
        return (result["d_min"], result["score_after"])
    if objective == "critical":
        return (-result["n_crit"], result["score_after"])
    raise ValueError(f"Unknown objective: {objective}")


def compare_results(left: dict[str, Any], right: dict[str, Any], objective: Objective = "score") -> int:
    """Compare raw engine results with one tolerance for search and agent arbitration."""
    for a, b in zip(objective_values(left, objective), objective_values(right, objective)):
        if a > b + COMPARISON_TOLERANCE:
            return 1
        if a < b - COMPARISON_TOLERANCE:
            return -1
    return 0


def all_options(data: AppData) -> list[Decision]:
    """10 district measures x 5 districts + 4 city measures = 54 options."""
    options: list[Decision] = []
    for m in data.measures:
        if m["scope"] == "district":
            for d in data.districts:
                options.append(Decision(m["id"], d["id"]))
        else:
            options.append(Decision(m["id"], None))
    return options


def _single_swap_result(decisions: list[Decision], data: AppData, locked_decisions: list[Decision] | None, objective: Objective) -> tuple[list[Decision] | None, dict | None]:
    """Try every valid single replacement while retaining each locked measure and district."""
    options = all_options(data)
    best_decisions: list[Decision] | None = None
    best_result: dict | None = None

    for i in range(len(decisions)):
        if decisions[i] in (locked_decisions or []):
            continue
        for option in options:
            if option.measure_id == decisions[i].measure_id and option.district_id == decisions[i].district_id:
                continue
            candidate = list(decisions)
            candidate[i] = option
            if validate(candidate, data):
                continue
            result = simulate(candidate, data, _with_contributions=False)
            if best_result is None or compare_results(result, best_result, objective) > 0:
                best_result = result
                best_decisions = candidate

    return best_decisions, best_result


def single_swap_search(decisions: list[Decision], data: AppData, locked_decisions: list[Decision] | None = None, *, objective: Objective = "score") -> tuple[list[Decision] | None, float | None]:
    """Return the best valid single swap and its Score (legacy return shape)."""
    candidate, result = _single_swap_result(decisions, data, locked_decisions, objective)
    return candidate, result["score_after"] if result is not None else None


def hill_climb(decisions: list[Decision], data: AppData, max_rounds: int = 5, *, locked_decisions: list[Decision] | None = None, objective: Objective = "score") -> dict[str, Any]:
    """At most five improving single swaps by default; no global-optimum guarantee.

    The original scenario is always a candidate. Only the chosen objective must
    improve: a better minimum district score or fewer critical indicators can
    justify a lower total Score. Ties retain the existing deterministic order.
    """
    current = list(decisions)
    current_result = simulate(current, data, _with_contributions=False)
    objective_values(current_result, objective)  # Reject unsupported direct calls too.
    steps: list[dict] = []

    for _ in range(max_rounds):
        candidate, candidate_result = _single_swap_result(current, data, locked_decisions, objective)
        if candidate is None or candidate_result is None:
            break
        if compare_results(candidate_result, current_result, objective) > 0:
            steps.append({
                "from": [{"measure_id": d.measure_id, "district_id": d.district_id} for d in current],
                "to": [{"measure_id": d.measure_id, "district_id": d.district_id} for d in candidate],
                "score": candidate_result["score_after"],
                "d_min": candidate_result["d_min"],
                "n_crit": candidate_result["n_crit"],
                "objective_values": objective_values(candidate_result, objective),
            })
            current = candidate
            current_result = candidate_result
        else:
            break

    return {"decisions": current, "score": current_result["score_after"], "result": current_result, "steps": steps, "objective": objective}


def brute_force(data: AppData) -> dict[str, Any]:
    """All combinations of 5 out of 54 options that pass validate(). Fast path, no LLM.

    Returns {"count", "min_score", "max_score", "top5": [(score, decisions)]}.
    Must finish within about a minute.
    """
    options = all_options(data)
    n_options = len(options)

    indicator_codes = data.indicator_codes
    n_ind = len(indicator_codes)
    ind_index = {code: i for i, code in enumerate(indicator_codes)}
    weight = [data.indicator_weight[c] for c in indicator_codes]

    district_ids = [d["id"] for d in data.districts]
    n_dist = len(district_ids)
    dist_index = {did: i for i, did in enumerate(district_ids)}
    pop = [d["population_share"] for d in data.districts]

    base = [[float(d["indicators"][c]) for c in indicator_codes] for d in data.districts]

    measure_ids = [m["id"] for m in data.measures]
    measure_index = {mid: i for i, mid in enumerate(measure_ids)}
    n_measures = len(measure_ids)

    H = data.config["horizon_quarters"]
    budget = data.config["budget"]
    max_per_direction = data.config["max_per_direction"]
    threshold = data.config["crit_threshold"]
    lam = data.config["lambda"]
    penalty = data.config["crit_penalty"]

    direction_names = list(data.config["directions"].keys())
    direction_index = {name: i for i, name in enumerate(direction_names)}
    n_dir = len(direction_names)

    opt_measure_idx = []
    opt_cost = []
    opt_dir_idx = []
    opt_dist_idx = []  # -1 for city measures
    opt_targets = []  # list of district indices affected
    opt_effects = []  # list of (indicator_idx, value*f)

    for opt in options:
        m = data.measures_by_id[opt.measure_id]
        mi = measure_index[opt.measure_id]
        opt_measure_idx.append(mi)
        opt_cost.append(m["cost"])
        opt_dir_idx.append(direction_index[m["direction"]])
        f = (H - m["lag"]) / H
        if m["scope"] == "city":
            opt_dist_idx.append(-1)
            opt_targets.append(list(range(n_dist)))
        else:
            di = dist_index[opt.district_id]
            opt_dist_idx.append(di)
            opt_targets.append([di])
        opt_effects.append([(ind_index[k], v * f) for k, v in m["effects"].items()])

    synergy_specs = []
    for s in data.config.get("synergies", []):
        a, b = s["pair"]
        synergy_specs.append((measure_index[a], measure_index[b], measure_index[s["host"]], ind_index[s["indicator"]], s["bonus"]))

    incompat_specs = []
    for inc in data.config.get("incompatibilities", []):
        a, b = inc["pair"]
        incompat_specs.append((measure_index[a], measure_index[b], bool(inc.get("same_district_only"))))

    count = 0
    min_score = None
    max_score = None
    top5: list[tuple[float, tuple[int, ...]]] = []

    combos = itertools.combinations(range(n_options), 5)

    for combo in combos:
        mask = 0
        dup = False
        total_cost = 0
        dir_counts = [0] * n_dir
        for idx in combo:
            mi = opt_measure_idx[idx]
            bit = 1 << mi
            if mask & bit:
                dup = True
                break
            mask |= bit
            total_cost += opt_cost[idx]
            if total_cost > budget:
                dup = True  # reuse flag to bail out early
                break
            di = opt_dir_idx[idx]
            dir_counts[di] += 1
            if dir_counts[di] > max_per_direction:
                dup = True
                break
        if dup:
            continue

        bad = False
        for a, b, same_district in incompat_specs:
            bit_a = 1 << a
            bit_b = 1 << b
            if (mask & bit_a) and (mask & bit_b):
                if not same_district:
                    bad = True
                    break
                dist_a = dist_b = None
                for idx in combo:
                    mi = opt_measure_idx[idx]
                    if mi == a:
                        dist_a = opt_dist_idx[idx]
                    elif mi == b:
                        dist_b = opt_dist_idx[idx]
                if dist_a is not None and dist_a == dist_b:
                    bad = True
                    break
        if bad:
            continue

        indicators = [row[:] for row in base]
        for idx in combo:
            for di in opt_targets[idx]:
                row = indicators[di]
                for ii, val in opt_effects[idx]:
                    row[ii] += val

        for a, b, host, ii, bonus in synergy_specs:
            bit_a = 1 << a
            bit_b = 1 << b
            if (mask & bit_a) and (mask & bit_b):
                for idx in combo:
                    if opt_measure_idx[idx] == host:
                        indicators[opt_dist_idx[idx]][ii] += bonus
                        break

        n_crit = 0
        d_scores = [0.0] * n_dist
        for di in range(n_dist):
            row = indicators[di]
            s = 0.0
            for ii in range(n_ind):
                v = row[ii]
                if v < 0.0:
                    v = 0.0
                elif v > 100.0:
                    v = 100.0
                row[ii] = v
                if v < threshold:
                    n_crit += 1
                s += weight[ii] * v
            d_scores[di] = s

        d_avg = 0.0
        for di in range(n_dist):
            d_avg += pop[di] * d_scores[di]
        d_min = min(d_scores)

        score = lam * d_avg + (1 - lam) * d_min - penalty * n_crit

        count += 1
        if min_score is None or score < min_score:
            min_score = score
        if max_score is None or score > max_score:
            max_score = score

        top5.append((score, combo))
        if len(top5) > 200:
            top5.sort(key=lambda t: t[0], reverse=True)
            top5 = top5[:5]

    top5.sort(key=lambda t: t[0], reverse=True)
    top5 = top5[:5]

    top5_decisions = [
        (score, [Decision(options[i].measure_id, options[i].district_id) for i in combo])
        for score, combo in top5
    ]

    return {
        "count": count,
        "min_score": min_score,
        "max_score": max_score,
        "top5": top5_decisions,
    }
