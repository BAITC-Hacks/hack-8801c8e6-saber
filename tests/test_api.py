import pytest
from fastapi.testclient import TestClient

from app.main import app

TOL = 0.01


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


EXAMPLE = [
    {"measure_id": "M7", "district_id": "nura"},
    {"measure_id": "M8", "district_id": "nura"},
    {"measure_id": "M10", "district_id": "nura"},
    {"measure_id": "M12", "district_id": None},
    {"measure_id": "M5", "district_id": "saryarka"},
]

OVER = [
    {"measure_id": "M3", "district_id": "nura"},
    {"measure_id": "M13", "district_id": "almaty"},
    {"measure_id": "M7", "district_id": "nura"},
    {"measure_id": "M10", "district_id": "nura"},
    {"measure_id": "M12", "district_id": None},
]


def test_state(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert len(body["districts"]) == 5
    assert len(body["measures"]) == 14
    assert body["config"]["budget"] == 100
    assert body["result"]["score_after"] == pytest.approx(52.56, abs=TOL)


def test_simulate_over_budget(client):
    r = client.post("/api/simulate", json={"decisions": OVER})
    assert r.status_code == 200
    body = r.json()
    codes = {e["code"] for e in body["errors"]}
    assert "BUDGET_EXCEEDED" in codes


def test_simulate_partial_two_decisions(client):
    r = client.post("/api/simulate", json={"decisions": EXAMPLE[:2]})
    assert r.status_code == 200
    body = r.json()
    codes = {e["code"] for e in body["errors"]}
    assert "INVALID_COUNT" not in codes
    assert body["result"] is not None


def test_simulate_district_required_gives_null_result(client):
    decisions = [{"measure_id": "M7", "district_id": None}]
    r = client.post("/api/simulate", json={"decisions": decisions})
    assert r.status_code == 200
    body = r.json()
    codes = {e["code"] for e in body["errors"]}
    assert "DISTRICT_REQUIRED" in codes
    assert body["result"] is None


def test_simulate_unknown_measure_gives_null_result(client):
    decisions = [{"measure_id": "M99", "district_id": None}]
    r = client.post("/api/simulate", json={"decisions": decisions})
    assert r.status_code == 200
    body = r.json()
    codes = {e["code"] for e in body["errors"]}
    assert "UNKNOWN_MEASURE" in codes
    assert body["result"] is None


def test_scenario_over_budget_is_422(client):
    r = client.post("/api/scenario", json={"team": "Команда OVER", "decisions": OVER})
    assert r.status_code == 422
    body = r.json()
    codes = {e["code"] for e in body["errors"]}
    assert "BUDGET_EXCEEDED" in codes


def test_scenario_example(client):
    r = client.post("/api/scenario", json={"team": "Команда 1", "decisions": EXAMPLE})
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["score_after"] == pytest.approx(56.54, abs=TOL)
    analysis = body["analysis"]
    for field in ("summary", "strengths", "risks", "consequences", "main_tradeoff", "weakest_district"):
        assert analysis[field]
    assert body["ai_mode"] == "fallback"

    r2 = client.get("/api/leaderboard")
    assert r2.status_code == 200
    teams = [row["team"] for row in r2.json()["leaderboard"]]
    assert "Команда 1" in teams


def test_optimize_example(client):
    r = client.post("/api/optimize", json={"decisions": EXAMPLE})
    assert r.status_code == 200
    body = r.json()
    assert body["best_score"] >= 57.21 - TOL
    assert len(body["hypotheses"]) > 0
    assert body["source"] == "baseline"
