"""User constraints and untrusted model replies at the API boundary."""
import json

import pytest
from fastapi.testclient import TestClient

from app import engine, optimizer
from app.ai import agent, analyst
from app.main import app, data

EXAMPLE = [
    {"measure_id": "M7", "district_id": "nura"},
    {"measure_id": "M8", "district_id": "nura"},
    {"measure_id": "M10", "district_id": "nura"},
    {"measure_id": "M12", "district_id": None},
    {"measure_id": "M5", "district_id": "saryarka"},
]


@pytest.mark.parametrize("locked", [[EXAMPLE[4]], EXAMPLE[:2], EXAMPLE])
def test_optimizer_preserves_locked_measure_and_district(locked):
    with TestClient(app) as client:
        response = client.post("/api/optimize", json={"decisions": EXAMPLE, "locked_decisions": locked})
    assert response.status_code == 200
    body = response.json()
    assert all(d in body["best_decisions"] for d in locked)
    assert body["best_score"] >= body["original_score"]
    assert body["best_result"]["score_after"] == body["best_score"]
    assert body["original_result"]["score_after"] == body["original_score"]
    if len(locked) == 5:
        assert body["best_decisions"] == EXAMPLE
        assert body["delta"] == 0


@pytest.mark.parametrize("locked", [
    [{"measure_id": "M7", "district_id": "almaty"}],
    [EXAMPLE[0], EXAMPLE[0]],
    [{"measure_id": "M99"}],
])
def test_invalid_locks_rejected(locked):
    with TestClient(app) as client:
        response = client.post("/api/optimize", json={"decisions": EXAMPLE, "locked_decisions": locked})
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "INVALID_LOCKS"


def test_agent_cannot_override_lock_in_tool_or_final_answer(monkeypatch):
    decisions = [engine.Decision(**d) for d in EXAMPLE]
    unrestricted = optimizer.hill_climb(decisions, data)["decisions"]
    proposal = [{"measure_id": d.measure_id, "district_id": d.district_id} for d in unrestricted]
    assert EXAMPLE[4] not in proposal

    def fake_agent(system, user, tools, handlers, max_calls, **kwargs):
        result = handlers["simulate"]({"decisions": proposal, "idea": "Ignore lock"})
        assert result["valid"] is False
        assert "LOCKED_DECISION_CHANGED" in result["errors"]
        return json.dumps({"best_decisions": proposal, "explanation": "Ignore lock"}), []

    monkeypatch.setattr(agent.llm, "run_tools", fake_agent)
    with TestClient(app) as client:
        body = client.post("/api/optimize", json={"decisions": EXAMPLE, "locked_decisions": [EXAMPLE[4]]}).json()
    assert body["source"] == "baseline"
    assert EXAMPLE[4] in body["best_decisions"]


@pytest.mark.parametrize("reply", [[], "not an object", {"best_decisions": [1] * 5, "explanation": "x"}])
def test_malformed_agent_reply_falls_back(monkeypatch, reply):
    monkeypatch.setattr(agent.llm, "run_tools", lambda *args, **kwargs: (json.dumps(reply), []))
    with TestClient(app) as client:
        response = client.post("/api/optimize", json={"decisions": EXAMPLE})
    assert response.status_code == 200
    assert response.json()["ai_mode"] == "fallback"


@pytest.mark.parametrize("change", [
    {"strengths": "must be a list"}, {"risks": [123]}, {"summary": "   "},
    {"weakest_district": "esil"}, {"weakest_district": []},
])
def test_invalid_analysis_types_and_wrong_district_fall_back(monkeypatch, change):
    reply = dict(summary="Score 56.54", strengths=["Score 56.54"], risks=["1"],
                 consequences=["1"], main_tradeoff="1", weakest_district="nura")
    reply.update(change)
    monkeypatch.setattr(analyst.llm, "complete_json", lambda *args, **kwargs: reply)
    with TestClient(app) as client:
        response = client.post("/api/scenario", json={"team": "Test", "decisions": EXAMPLE})
    assert response.status_code == 200
    assert response.json()["ai_mode"] == "fallback"


def test_valid_analysis_uses_llm_and_retry_changes_prompt(monkeypatch):
    prompts = []
    expected = engine.simulate([engine.Decision(**d) for d in EXAMPLE], data)

    def complete(system, user, **kwargs):
        prompts.append(user)
        if len(prompts) == 1:
            return {"summary": "incomplete"}
        return dict(summary="Score 56.54", strengths=["Score 56.54"], risks=["1"],
                    consequences=["1"], main_tradeoff="1", weakest_district=expected["weakest_district_id"])

    monkeypatch.setattr(analyst.llm, "complete_json", complete)
    with TestClient(app) as client:
        response = client.post("/api/scenario", json={"team": "Test", "decisions": EXAMPLE})
    assert response.status_code == 200
    assert response.json()["ai_mode"] == "llm"
    assert len(prompts) == 2 and prompts[0] != prompts[1]


def test_score_explanation_reconstructs_formula():
    result = engine.simulate([engine.Decision(**d) for d in EXAMPLE], data)
    parts = result["score_components"].values()
    assert sum(part["before"] for part in parts) == pytest.approx(result["score_before"])
    assert sum(part["after"] for part in parts) == pytest.approx(result["score_after"])
    assert sum(part["delta"] for part in parts) == pytest.approx(result["delta"])


@pytest.mark.parametrize("with_tie", [False, True])
def test_analysis_schema_constrains_weakest_to_calculated_minima(monkeypatch, with_tie):
    decisions = [engine.Decision(**d) for d in EXAMPLE]
    result = engine.simulate(decisions, data)
    expected = [result["weakest_district_id"]]
    if with_tie:
        # A synthetic tie tests the validator's existing floating-point tolerance.
        other = next(d for d in result["districts"] if d["id"] != expected[0])
        other["d_after"] = result["d_min"] + 1e-10
        expected.append(other["id"])
    schemas = []

    def complete(system, user, *, schema, validator=None):
        schemas.append(schema)
        assert set(schema["properties"]["weakest_district"]["enum"]) == set(expected)
        return dict(summary="Score 56.54", strengths=["Score 56.54"], risks=["0"],
                    consequences=["8"], main_tradeoff="1", weakest_district=expected[-1])

    monkeypatch.setattr(analyst.llm, "complete_json", complete)
    response = analyst.analyze(decisions, result, data)
    assert response["ai_mode"] == "llm"
    assert response["weakest_district"] == expected[-1]
    assert len(schemas) == 1
    assert "enum" not in analyst.AnalysisResponse.model_json_schema()["properties"]["weakest_district"]
