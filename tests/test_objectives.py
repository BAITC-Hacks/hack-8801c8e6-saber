"""Distinct management priorities, safe arbitration, and the three-strategy API."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from app import engine, optimizer
from app.ai import agent
from app.main import app, data

EXAMPLE = [
    {"measure_id": "M7", "district_id": "nura"},
    {"measure_id": "M8", "district_id": "nura"},
    {"measure_id": "M10", "district_id": "nura"},
    {"measure_id": "M12", "district_id": None},
    {"measure_id": "M5", "district_id": "saryarka"},
]
DECISIONS = [engine.Decision(**d) for d in EXAMPLE]


def pairs(decisions):
    return {(d["measure_id"], d.get("district_id")) for d in decisions}


@pytest.mark.parametrize("objective, candidate, expected", [
    ("score", {"score_after": 60.0, "d_min": 39.0, "n_crit": 5}, 1),
    ("weakest", {"score_after": 49.0, "d_min": 41.0, "n_crit": 5}, 1),
    ("weakest", {"score_after": 60.0, "d_min": 39.0, "n_crit": 0}, -1),
    ("weakest", {"score_after": 51.0, "d_min": 40.0, "n_crit": 5}, 1),
    ("critical", {"score_after": 49.0, "d_min": 39.0, "n_crit": 1}, 1),
    ("critical", {"score_after": 60.0, "d_min": 41.0, "n_crit": 3}, -1),
    ("critical", {"score_after": 51.0, "d_min": 39.0, "n_crit": 2}, 1),
])
def test_lexicographic_priorities_and_score_tiebreak(objective, candidate, expected):
    original = {"score_after": 50.0, "d_min": 40.0, "n_crit": 2}
    assert optimizer.compare_results(candidate, original, objective) == expected


def test_comparison_uses_unrounded_metrics_with_consistent_tolerance():
    original = {"score_after": 50.0, "d_min": 40.0, "n_crit": 2}
    better_min = {**original, "d_min": 40.0001, "score_after": 49.0}
    assert round(original["d_min"], 2) == round(better_min["d_min"], 2)
    assert optimizer.compare_results(better_min, original, "weakest") == 1
    near_tie = {**original, "d_min": 40.0 + 1e-10, "score_after": 49.0}
    assert optimizer.compare_results(near_tie, original, "weakest") == -1
    assert optimizer.compare_results({**original, "score_after": 50.0 + 1e-10}, original) == 0


def test_weakest_goal_can_choose_lower_score_to_raise_worst_district():
    original = engine.simulate(DECISIONS, data)
    score = optimizer.hill_climb(DECISIONS, data)["result"]
    weakest = optimizer.hill_climb(DECISIONS, data, objective="weakest")["result"]
    assert weakest["d_min"] > score["d_min"] > original["d_min"]
    assert weakest["score_after"] < original["score_after"] < score["score_after"]


def test_critical_goal_can_disagree_with_score_without_new_weights():
    # Reduce only the synthetic penalty to create a clear policy disagreement.
    # The objective implementation must honor critical counts independently of Score.
    synthetic = copy.deepcopy(data)
    synthetic.config["crit_penalty"] = 0.01
    score = optimizer.hill_climb(DECISIONS, synthetic)["result"]
    critical = optimizer.hill_climb(DECISIONS, synthetic, objective="critical")["result"]
    assert critical["n_crit"] < score["n_crit"]
    assert critical["score_after"] < score["score_after"]


@pytest.mark.parametrize("objective", ["score", "weakest", "critical"])
@pytest.mark.parametrize("locked", [[], EXAMPLE[:1], EXAMPLE])
def test_strategies_are_full_valid_results_with_identical_locks(objective, locked):
    with TestClient(app) as client:
        response = client.post("/api/optimize", json={"decisions": EXAMPLE, "locked_decisions": locked, "objective": objective})
    assert response.status_code == 200
    body = response.json()
    assert body["objective"] == objective
    assert [s["objective"] for s in body["strategies"]] == ["score", "weakest", "critical"]
    original = engine.simulate(DECISIONS, data)
    for strategy in body["strategies"]:
        decisions = [engine.Decision(**d) for d in strategy["decisions"]]
        assert engine.validate(decisions, data) == []
        assert pairs(locked).issubset(pairs(strategy["decisions"]))
        actual = engine.simulate(decisions, data)
        assert strategy["result"] == engine.round_result(actual)
        assert optimizer.compare_results(actual, original, strategy["objective"]) >= 0
        assert strategy["source"] == "baseline"
        assert strategy["label"] and strategy["description"] and strategy["explanation"]
        if strategy["objective"] == objective:
            assert strategy["decisions"] == body["best_decisions"]
            assert strategy["result"] == body["best_result"]
            assert strategy["source"] == body["source"]
            assert strategy["explanation"] == body["explanation"]
        if locked == EXAMPLE:
            assert strategy["decisions"] == EXAMPLE
    if objective == "weakest" and not locked:
        assert body["delta"] < 0
        assert "Score снизился" in body["explanation"]
        assert "компромисс" in body["explanation"]


def test_default_objective_compatibility_and_honest_coinciding_strategies():
    with TestClient(app) as client:
        implicit = client.post("/api/optimize", json={"decisions": EXAMPLE}).json()
        explicit = client.post("/api/optimize", json={"decisions": EXAMPLE, "objective": "score"}).json()
    assert implicit == explicit
    assert implicit["best_score"] == 57.21
    assert pairs(implicit["strategies"][0]["decisions"]) == pairs(implicit["strategies"][2]["decisions"])


@pytest.mark.parametrize("objective", ["not-a-goal", None, 1])
def test_unknown_objective_is_422(objective):
    with TestClient(app) as client:
        response = client.post("/api/optimize", json={"decisions": EXAMPLE, "objective": objective})
    assert response.status_code == 422


@pytest.mark.parametrize("objective", ["weakest", "critical"])
@pytest.mark.parametrize("proposal_goal, expected_source", [("score", "baseline"), ("selected", "agent")])
def test_agent_arbitration_uses_selected_goal_even_when_score_is_lower(monkeypatch, objective, proposal_goal, expected_source):
    synthetic = copy.deepcopy(data)
    if objective == "critical":
        synthetic.config["crit_penalty"] = 0.01
    selected = optimizer.hill_climb(DECISIONS, synthetic, objective=objective)
    score = optimizer.hill_climb(DECISIONS, synthetic, objective="score")
    assert selected["score"] < score["score"]
    proposal = selected if proposal_goal == "selected" else score
    calls = []

    def fake_agent(system, user, tools, handlers, max_calls, **kwargs):
        payload = json.loads(user)
        calls.append(payload["objective"])
        assert payload["objective"] == objective
        assert payload["d_min"] == engine.simulate(DECISIONS, synthetic)["d_min"]
        assert "[-n_crit, Score]" in system and "[d_min, Score]" in system
        raw = [{"measure_id": d.measure_id, "district_id": d.district_id} for d in proposal["decisions"]]
        result = handlers["simulate"]({"decisions": raw, "idea": "Проверка выбранной цели"})
        assert result["objective"] == objective
        assert result["score"] == proposal["result"]["score_after"]
        assert result["d_min"] == proposal["result"]["d_min"]
        assert result["n_crit"] == proposal["result"]["n_crit"]
        assert result["objective_values"] == optimizer.objective_values(proposal["result"], objective)
        trace = [{"tool": "simulate", "input": {"decisions": raw, "idea": "Проверка"}, "output": result}]
        return json.dumps({"best_decisions": raw, "explanation": "Score вырос до 999: непроверенное утверждение"}), trace

    monkeypatch.setattr(agent.llm, "run_tools", fake_agent)
    body = agent.optimize(DECISIONS, synthetic, objective=objective)
    assert calls == [objective]  # Only the requested goal consumes an LLM call.
    assert body["source"] == expected_source
    assert body["ai_mode"] == "llm"
    assert body["best_result"] == engine.round_result(engine.simulate(selected["decisions"], synthetic))
    assert "999" not in body["explanation"]
    for strategy in body["strategies"]:
        assert strategy["source"] == (expected_source if strategy["objective"] == objective else "baseline")


@pytest.mark.parametrize("objective", ["score", "weakest", "critical"])
def test_all_goals_reject_agent_changes_to_locked_district(monkeypatch, objective):
    proposal = copy.deepcopy(EXAMPLE)
    proposal[-1]["district_id"] = "nura"

    def fake_agent(system, user, tools, handlers, max_calls, **kwargs):
        payload = json.loads(user)
        assert payload["locked_decisions"] == EXAMPLE[-1:]
        result = handlers["simulate"]({"decisions": proposal})
        assert result["valid"] is False
        assert result["objective"] == objective
        assert "LOCKED_DECISION_CHANGED" in result["errors"]
        return json.dumps({"best_decisions": proposal, "explanation": "Нарушение закрепления"}), []

    monkeypatch.setattr(agent.llm, "run_tools", fake_agent)
    body = agent.optimize(DECISIONS, data, objective=objective, locked_decisions=DECISIONS[-1:])
    assert body["source"] == "baseline" and body["ai_mode"] == "fallback"
    assert all(EXAMPLE[-1] in strategy["decisions"] for strategy in body["strategies"])


@pytest.mark.parametrize("objective", ["score", "weakest", "critical"])
def test_malformed_llm_reply_keeps_three_safe_baselines(monkeypatch, objective):
    monkeypatch.setattr(agent.llm, "run_tools", lambda *args, **kwargs: ("{broken", []))
    body = agent.optimize(DECISIONS, data, objective=objective)
    assert body["ai_mode"] == "fallback"
    assert body["objective"] == objective
    assert len(body["strategies"]) == 3
    assert all(s["source"] == "baseline" for s in body["strategies"])


def test_all_locks_skip_llm_and_preserve_every_strategy(monkeypatch):
    def unexpected_call(*args, **kwargs):
        pytest.fail("No LLM call is needed for a fully locked scenario")

    monkeypatch.setattr(agent.llm, "run_tools", unexpected_call)
    body = agent.optimize(DECISIONS, data, objective="weakest", locked_decisions=DECISIONS)
    assert all(s["decisions"] == EXAMPLE for s in body["strategies"])
    assert body["delta"] == 0
