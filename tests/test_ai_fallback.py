import json

import pytest
from fastapi.testclient import TestClient

from app.ai import agent, analyst
from app.ai import llm as llm_module
from app.main import app

TOL = 0.01

EXAMPLE = [
    {"measure_id": "M7", "district_id": "nura"},
    {"measure_id": "M8", "district_id": "nura"},
    {"measure_id": "M10", "district_id": "nura"},
    {"measure_id": "M12", "district_id": None},
    {"measure_id": "M5", "district_id": "saryarka"},
]


@pytest.fixture
def client():
    return TestClient(app)


def test_scenario_falls_back_when_llm_unavailable(client, monkeypatch):
    def raise_unavailable(system, user):
        raise llm_module.LLMUnavailable("simulated outage")

    monkeypatch.setattr(analyst.llm, "complete_json", raise_unavailable)

    r = client.post("/api/scenario", json={"team": "Тест Fallback 1", "decisions": EXAMPLE})
    assert r.status_code == 200
    body = r.json()
    assert body["ai_mode"] == "fallback"
    for field in ("summary", "strengths", "risks", "consequences", "main_tradeoff", "weakest_district"):
        assert body["analysis"][field]


def test_scenario_falls_back_when_llm_returns_invalid_json(client, monkeypatch):
    def bad_response(system, user):
        return {"summary": "неполный ответ"}

    monkeypatch.setattr(analyst.llm, "complete_json", bad_response)

    r = client.post("/api/scenario", json={"team": "Тест Fallback 2", "decisions": EXAMPLE})
    assert r.status_code == 200
    body = r.json()
    assert body["ai_mode"] == "fallback"
    for field in ("summary", "strengths", "risks", "consequences", "main_tradeoff", "weakest_district"):
        assert body["analysis"][field]


def test_optimize_falls_back_to_hill_climb_when_agent_breaks_rules(client, monkeypatch):
    def bad_run_tools(system, user, tools, handlers, max_calls):
        final = json.dumps({
            "best_decisions": [{"measure_id": "M7", "district_id": "nura"}] * 5,
            "explanation": "Некорректный ответ агента",
        })
        return final, []

    monkeypatch.setattr(agent.llm, "run_tools", bad_run_tools)

    r = client.post("/api/optimize", json={"decisions": EXAMPLE})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "baseline"
    assert body["best_score"] >= 57.21 - TOL
    assert len(body["hypotheses"]) > 0
