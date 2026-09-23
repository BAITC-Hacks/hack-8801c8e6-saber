"""Invalid previews must not invoke the quadratic contribution calculation."""
import pytest
from fastapi.testclient import TestClient

from app import engine
from app.main import app


@pytest.mark.parametrize("count", [6, 1000])
def test_oversized_preview_does_not_run_simulation(monkeypatch, count):
    def unexpected_simulation(*args, **kwargs):
        pytest.fail("A preview larger than the configured decision count was simulated")

    monkeypatch.setattr(engine, "simulate", unexpected_simulation)
    with TestClient(app) as client:
        response = client.post("/api/simulate", json={
            "decisions": [{"measure_id": "M12"}] * count,
        })
    assert response.status_code == 200
    body = response.json()
    assert "INVALID_COUNT" in {error["code"] for error in body["errors"]}
    assert body["result"] is None
