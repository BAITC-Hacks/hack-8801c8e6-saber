import itertools
import random

import pytest

from app.data_loader import load_data
from app.engine import (
    BUDGET_EXCEEDED,
    DIRECTION_LIMIT,
    DISTRICT_NOT_ALLOWED,
    DISTRICT_REQUIRED,
    DUPLICATE_MEASURE,
    INCOMPATIBLE,
    INVALID_COUNT,
    Decision,
    contributions,
    simulate,
    validate,
)
from app.optimizer import all_options, brute_force, hill_climb, single_swap_search

TOL = 0.01


@pytest.fixture(scope="module")
def data():
    return load_data()


def D(measure_district_pairs):
    return [Decision(m, d) for m, d in measure_district_pairs]


BASE = D([])
EXAMPLE = D([("M7", "nura"), ("M8", "nura"), ("M10", "nura"), ("M12", None), ("M5", "saryarka")])
CHEAP = D([("M9", "nura"), ("M11", "nura"), ("M10", "nura"), ("M12", None), ("M4", "nura")])
BEST = D([("M2", None), ("M3", "nura"), ("M8", "nura"), ("M9", "nura"), ("M14", None)])
PENALTY = D([("M11", "almaty"), ("M9", "nura"), ("M10", "nura"), ("M12", None), ("M4", "nura")])
OVER = D([("M3", "nura"), ("M13", "almaty"), ("M7", "nura"), ("M10", "nura"), ("M12", None)])
DIR = D([("M12", None), ("M13", "almaty"), ("M14", None), ("M9", "nura"), ("M10", "nura")])
CONFLICT_SITE = D([("M4", "nura"), ("M7", "nura"), ("M9", "nura"), ("M10", "nura"), ("M12", None)])
SITE_OK = D([("M4", "esil"), ("M7", "nura"), ("M9", "nura"), ("M10", "nura"), ("M12", None)])
CONFLICT_TRANSPORT = D([("M1", "esil"), ("M3", "nura"), ("M9", "nura"), ("M10", "nura"), ("M12", None)])


def test_base_district_d(data):
    result = simulate(BASE, data)
    expected = {"esil": 62.99, "almaty": 57.06, "saryarka": 54.65, "baikonur": 56.63, "nura": 49.18}
    by_id = {dd["id"]: dd["d_after"] for dd in result["districts"]}
    for did, exp in expected.items():
        assert by_id[did] == pytest.approx(exp, abs=TOL)


@pytest.mark.parametrize(
    "decisions,expected_score,expected_ncrit",
    [
        (BASE, 52.56, 2),
        (EXAMPLE, 56.54, 0),
        (CHEAP, 55.67, 1),
        (BEST, 57.24, None),
        (PENALTY, 54.48, 2),
    ],
)
def test_score_and_ncrit(data, decisions, expected_score, expected_ncrit):
    result = simulate(decisions, data)
    assert result["score_after"] == pytest.approx(expected_score, abs=TOL)
    if expected_ncrit is not None:
        assert result["n_crit"] == expected_ncrit


def test_base_score_is_5256(data):
    result = simulate(BASE, data)
    assert result["score_before"] == pytest.approx(52.56, abs=TOL)
    assert result["d_avg"] == pytest.approx(56.86, abs=TOL)


def test_over_budget(data):
    errors = validate(OVER, data)
    codes = {e["code"] for e in errors}
    assert BUDGET_EXCEEDED in codes


def test_direction_limit(data):
    errors = validate(DIR, data)
    codes = {e["code"] for e in errors}
    assert DIRECTION_LIMIT in codes


def test_conflict_site(data):
    errors = validate(CONFLICT_SITE, data)
    codes = {e["code"] for e in errors}
    assert INCOMPATIBLE in codes


def test_site_ok_is_valid(data):
    errors = validate(SITE_OK, data)
    assert errors == []
    result = simulate(SITE_OK, data)
    assert result["score_after"] == pytest.approx(55.54, abs=TOL)


def test_conflict_transport(data):
    errors = validate(CONFLICT_TRANSPORT, data)
    codes = {e["code"] for e in errors}
    assert INCOMPATIBLE in codes


def test_invalid_count(data):
    decisions = D([("M7", "nura"), ("M8", "nura"), ("M10", "nura"), ("M12", None)])
    errors = validate(decisions, data)
    codes = {e["code"] for e in errors}
    assert INVALID_COUNT in codes


def test_duplicate_measure(data):
    decisions = D([("M7", "nura"), ("M7", "almaty"), ("M10", "nura"), ("M12", None), ("M5", "saryarka")])
    errors = validate(decisions, data)
    codes = {e["code"] for e in errors}
    assert DUPLICATE_MEASURE in codes


def test_district_not_allowed(data):
    decisions = D([("M7", "nura"), ("M8", "nura"), ("M10", "nura"), ("M12", "nura"), ("M5", "saryarka")])
    errors = validate(decisions, data)
    codes = {e["code"] for e in errors}
    assert DISTRICT_NOT_ALLOWED in codes


def test_district_required(data):
    decisions = D([("M7", None), ("M8", "nura"), ("M10", "nura"), ("M12", None), ("M5", "saryarka")])
    errors = validate(decisions, data)
    codes = {e["code"] for e in errors}
    assert DISTRICT_REQUIRED in codes


def test_critical_boundary(data):
    result = simulate(BASE, data)
    almaty = next(dd for dd in result["districts"] if dd["id"] == "almaty")
    assert almaty["indicators_after"]["T1"] == pytest.approx(40.0, abs=1e-9)
    assert not any(c["district_id"] == "almaty" and c["indicator"] == "T1" for c in result["critical"])


def test_lag_share(data):
    decisions = D([("M3", "nura")])
    result = simulate(decisions, data)
    nura = next(dd for dd in result["districts"] if dd["id"] == "nura")
    assert nura["indicators_after"]["T1"] == pytest.approx(63.0, abs=TOL)
    assert nura["indicators_after"]["T2"] == pytest.approx(50.0, abs=TOL)
    assert nura["indicators_after"]["E2"] == pytest.approx(67.0, abs=TOL)


def test_synergy_applied(data):
    result = simulate(EXAMPLE, data)
    nura = next(dd for dd in result["districts"] if dd["id"] == "nura")
    assert nura["indicators_after"]["B1"] == pytest.approx(67.5, abs=TOL)
    pairs = [tuple(s["pair"]) for s in result["synergies_applied"]]
    assert ("M10", "M12") in pairs

    decisions2 = D([("M7", "nura"), ("M8", "nura"), ("M10", "nura"), ("M14", None), ("M5", "saryarka")])
    result2 = simulate(decisions2, data)
    nura2 = next(dd for dd in result2["districts"] if dd["id"] == "nura")
    assert nura2["indicators_after"]["B1"] == pytest.approx(65.5, abs=TOL)
    pairs2 = [tuple(s["pair"]) for s in result2["synergies_applied"]]
    assert ("M10", "M12") not in pairs2


def test_order_independent(data):
    scores = set()
    for perm in itertools.permutations(EXAMPLE):
        result = simulate(list(perm), data)
        scores.add(round(result["score_after"], 6))
    assert len(scores) == 1


def test_sensitivity(data):
    base_result = simulate(EXAMPLE, data)
    base_score = base_result["score_after"]
    options = all_options(data)
    example_set = {(d.measure_id, d.district_id) for d in EXAMPLE}

    diffs = []
    for i in range(len(EXAMPLE)):
        for option in options:
            if (option.measure_id, option.district_id) == (EXAMPLE[i].measure_id, EXAMPLE[i].district_id):
                continue
            candidate = list(EXAMPLE)
            candidate[i] = option
            if validate(candidate, data):
                continue
            score = simulate(candidate, data, _with_contributions=False)["score_after"]
            diff = abs(score - base_score)
            assert diff > 1e-9
            diffs.append(diff)

    assert len(diffs) == 117
    assert min(diffs) == pytest.approx(0.00007, abs=1e-5)


def test_indicators_in_range(data):
    rng = random.Random(42)
    options = all_options(data)
    found = 0
    attempts = 0
    while found < 1000 and attempts < 200000:
        attempts += 1
        combo = rng.sample(options, 5)
        if validate(combo, data):
            continue
        result = simulate(combo, data, _with_contributions=False)
        for dd in result["districts"]:
            for v in dd["indicators_after"].values():
                assert 0.0 <= v <= 100.0
        found += 1
    assert found == 1000


def test_contributions_example(data):
    contribs = contributions(EXAMPLE, data)
    expected = {"M7": 1.45, "M8": 1.40, "M10": 0.53, "M12": 0.51, "M5": 0.17}
    for mid, exp in expected.items():
        assert contribs[mid] == pytest.approx(exp, abs=TOL)


def test_hill_climb(data):
    result = hill_climb(EXAMPLE, data)
    assert result["score"] == pytest.approx(57.21, abs=TOL)
    assert len(result["steps"]) == 1
    step = result["steps"][0]
    to_pairs = {(d["measure_id"], d["district_id"]) for d in step["to"]}
    assert ("M3", "nura") in to_pairs
    from_pairs = {(d["measure_id"], d["district_id"]) for d in step["from"]}
    assert ("M5", "saryarka") in from_pairs

    result_cheap = hill_climb(CHEAP, data)
    assert result_cheap["score"] == pytest.approx(57.21, abs=TOL)


@pytest.mark.slow
def test_brute_force(data):
    result = brute_force(data)
    assert result["count"] == 694395
    assert result["min_score"] == pytest.approx(52.04, abs=TOL)
    assert result["max_score"] == pytest.approx(57.24, abs=TOL)
    best_score, best_decisions = result["top5"][0]
    best_set = {(d.measure_id, d.district_id) for d in best_decisions}
    expected_set = {(d.measure_id, d.district_id) for d in BEST}
    assert best_set == expected_set
