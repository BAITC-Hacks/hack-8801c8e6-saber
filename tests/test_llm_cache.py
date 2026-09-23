"""A transient invalid provider response must not poison subsequent user retries."""
import json
from types import SimpleNamespace

import pytest

from app.ai import llm


@pytest.fixture(autouse=True)
def isolated_llm_cache(monkeypatch):
    monkeypatch.setattr(llm, "_cache", {})


def fake_provider(monkeypatch, responses):
    pending = iter(responses)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return next(pending)

    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(responses=SimpleNamespace(create=create)))
    return calls


def response(text, output=None):
    return SimpleNamespace(output_text=text, output=output or [])


def invoke(method):
    if method == "complete_json":
        return llm.complete_json("system", "user")
    return llm.run_tools("system", "user", [], {}, max_calls=1)


@pytest.mark.parametrize("method", ["complete_json", "run_tools"])
@pytest.mark.parametrize("invalid", ["", "   ", "{broken", "[]", "null", '"string"', "12"])
def test_invalid_final_response_is_not_cached_and_explicit_retry_calls_provider(monkeypatch, method, invalid):
    valid = '{"ok": true}'
    calls = fake_provider(monkeypatch, [response(invalid), response(valid)])
    with pytest.raises(llm.LLMUnavailable):
        invoke(method)
    assert calls and len(calls) == 1  # No automatic retry is introduced.
    assert llm._cache == {}

    result = invoke(method)
    expected = {"ok": True} if method == "complete_json" else (valid, [])
    assert result == expected
    assert len(calls) == 2
    assert invoke(method) == expected
    assert len(calls) == 2  # Valid responses retain the previous cache contract.


@pytest.mark.parametrize("invalid", ["", "{broken", "[]"])
def test_invalid_response_after_tool_limit_is_not_cached(monkeypatch, invalid):
    tool_call = SimpleNamespace(type="function_call", name="simulate", arguments='{"value": 1}', call_id="tool-1")
    calls = fake_provider(monkeypatch, [response("", [tool_call]), response(invalid), response('{"ok": true}')])
    tool_calls = []

    def simulate(inp):
        tool_calls.append(inp)
        return {"valid": True}

    def run():
        return llm.run_tools("system", "user", [], {"simulate": simulate}, max_calls=1)

    with pytest.raises(llm.LLMUnavailable):
        run()
    assert tool_calls == [{"value": 1}]
    assert len(calls) == 2
    assert llm._cache == {}
    assert run() == ('{"ok": true}', [])
    assert len(calls) == 3
    assert run() == ('{"ok": true}', [])
    assert len(calls) == 3


@pytest.mark.parametrize("method", ["complete_json", "run_tools"])
def test_valid_fenced_json_remains_supported_and_cached(monkeypatch, method):
    text = '```json\n{"ok": true}\n```'
    calls = fake_provider(monkeypatch, [response(text)])
    result = invoke(method)
    assert result == ({"ok": True} if method == "complete_json" else (text, []))
    assert invoke(method) == result
    assert len(calls) == 1


def test_valid_tool_result_keeps_trace_in_cache(monkeypatch):
    tool_call = SimpleNamespace(type="function_call", name="simulate", arguments='{"value": 1}', call_id="tool-1")
    valid = json.dumps({"best_decisions": [], "explanation": "Test"})
    calls = fake_provider(monkeypatch, [response("", [tool_call]), response(valid)])
    handlers = {"simulate": lambda inp: {"valid": True}}
    expected_trace = [{"tool": "simulate", "input": {"value": 1}, "output": {"valid": True}}]
    result = llm.run_tools("system", "user", [], handlers, max_calls=1)
    assert result == (valid, expected_trace)
    assert llm.run_tools("system", "user", [], handlers, max_calls=1) == result
    assert len(calls) == 2
