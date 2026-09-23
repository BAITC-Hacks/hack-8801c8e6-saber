"""Tests never call paid services or write to the user's leaderboard."""
import pytest

from app import storage


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    connect = storage.get_connection
    monkeypatch.setattr(storage, "get_connection", lambda *args, **kwargs: connect(tmp_path / "test.db"))
