"""Provider boundary, tool-loop limits and cache behavior without external calls."""
import copy
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.ai import llm
from app.main import app


SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "note": {"type": "string"},
        "items": {
            "type": "array",
            "items": {"type": "object", "properties": {"value": {"type": "integer"}}},
        },
    },
    "required": ["answer"],
}
TOOL = {
    "name": "simulate",
    "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
}


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(llm, "_cache", {})


def message(text):
    return SimpleNamespace(type="message", role="assistant", content=[SimpleNamespace(type="output_text", text=text)])


def response(text='{"answer": "ok"}', *, output=None, status="completed", reason=None):
    return SimpleNamespace(
        output_text=text,
        output=[message(text)] if output is None else output,
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
    )


def function_call(arguments='{"value": 1}', call_id="call-1"):
    return SimpleNamespace(type="function_call", name="simulate", arguments=arguments, call_id=call_id)


def provider(monkeypatch, responses):
    sequence = iter(responses)
    calls = []

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        item = next(sequence)
        if isinstance(item, Exception):
            raise item
        return item

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(llm, "_client", lambda: client)
    return calls


def is_nullable(schema):
    types = schema.get("type", [])
    return types == "null" or isinstance(types, list) and "null" in types or any(
        is_nullable(branch) for branch in schema.get("anyOf", [])
    )


def object_schema(schema):
    if schema.get("type") == "object":
        return schema
    return next(branch for branch in schema.get("anyOf", []) if branch.get("type") == "object")


@pytest.mark.parametrize("method", ["complete", "tools"])
def test_final_assistant_message_is_parsed_instead_of_combined_commentary(monkeypatch, method):
    final = '{"answer": "verified"}'
    calls = provider(monkeypatch, [response("Checking the plan...\n" + final, output=[message("Checking the plan..."), message(final)])])
    if method == "complete":
        assert llm.complete_json("system", "user") == {"answer": "verified"}
    else:
        text, trace = llm.run_tools("system", "user", [], {}, 1)
        assert json.loads(text) == {"answer": "verified"}
        assert trace == []
    assert len(calls) == 1


def test_structured_schema_is_strict_nullable_and_forwarded_on_every_tool_round(monkeypatch):
    original = copy.deepcopy(SCHEMA)
    calls = provider(monkeypatch, [response("", output=[function_call()]), response()])
    llm.run_tools("system", "user", [TOOL], {"simulate": lambda args: {"valid": True}}, 1, schema=SCHEMA)
    assert SCHEMA == original
    assert len(calls) == 2
    for call in calls:
        fmt = call["text"]["format"]
        assert fmt["type"] == "json_schema" and fmt["strict"] is True
        schema = fmt["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert is_nullable(schema["properties"]["note"])
        assert not is_nullable(schema["properties"]["answer"])
        items = schema["properties"]["items"]
        if "items" not in items:
            items = next(branch for branch in items["anyOf"] if branch.get("type") == "array")
        nested = object_schema(items["items"])
        assert nested["additionalProperties"] is False
        assert nested["required"] == ["value"]
        assert is_nullable(nested["properties"]["value"])


def test_one_shot_schema_and_default_json_format_are_forwarded(monkeypatch):
    calls = provider(monkeypatch, [response(), response()])
    llm.complete_json("system", "schema", schema=SCHEMA)
    llm.complete_json("system", "default")
    assert calls[0]["text"]["format"]["type"] == "json_schema"
    assert calls[0]["text"]["format"]["strict"] is True
    assert calls[1]["text"]["format"] == {"type": "json_object"}


@pytest.mark.parametrize("status, reason", [("incomplete", "max_output_tokens"), ("failed", None)])
def test_unfinished_response_cannot_be_accepted_or_cached_even_with_valid_json(monkeypatch, status, reason):
    calls = provider(monkeypatch, [response(status=status, reason=reason)])
    with pytest.raises(llm.LLMUnavailable) as caught:
        llm.complete_json("system", "user")
    assert caught.value.code != "provider_error"
    assert caught.value.stage != "unknown"
    assert llm._cache == {}
    assert len(calls) == 1


def test_refusal_is_distinct_from_invalid_json(monkeypatch):
    refusal = SimpleNamespace(type="message", role="assistant", content=[SimpleNamespace(type="refusal", refusal="private refusal details")])
    calls = provider(monkeypatch, [response(output=[refusal])])
    with pytest.raises(llm.LLMUnavailable) as caught:
        llm.complete_json("system", "user")
    assert caught.value.code == "refusal"
    assert "private refusal details" not in llm.failure_status(caught.value)["message"]
    assert llm._cache == {}
    assert len(calls) == 1


def test_provider_failure_is_typed_sanitized_and_not_retried(monkeypatch):
    calls = provider(monkeypatch, [RuntimeError("private-key-and-provider-payload")])
    with pytest.raises(llm.LLMUnavailable) as caught:
        llm.complete_json("system", "user")
    status = llm.failure_status(caught.value)
    assert status["code"] == "provider_error"
    assert status["message"] and "private-key-and-provider-payload" not in status["message"]
    assert caught.value.stage != "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("exception_name, expected_code", [
    ("APITimeoutError", "timeout"),
    ("RateLimitError", "rate_limited"),
    ("AuthenticationError", "authentication"),
])
def test_known_provider_errors_keep_distinct_actionable_codes(monkeypatch, exception_name, expected_code):
    provider_error = type(exception_name, (Exception,), {})
    calls = provider(monkeypatch, [provider_error("private provider details")])
    with pytest.raises(llm.LLMUnavailable) as caught:
        llm.complete_json("system", "user")
    assert caught.value.code == expected_code
    assert llm.failure_status(caught.value)["code"] == expected_code
    assert "private provider details" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize("budget", [-3, 0])
def test_zero_tool_budget_goes_directly_to_final_without_handler_calls(monkeypatch, budget):
    calls = provider(monkeypatch, [response()])
    handler_calls = []
    text, trace = llm.run_tools("system", "user", [TOOL], {"simulate": lambda args: handler_calls.append(args)}, budget)
    assert json.loads(text) == {"answer": "ok"}
    assert trace == [] and handler_calls == []
    assert len(calls) == 1
    assert not calls[0].get("tools")


def test_excessive_requested_budget_is_capped_and_parallel_tool_excess_is_rejected(monkeypatch):
    batch = [function_call(call_id=f"call-{i}") for i in range(20)]
    calls = provider(monkeypatch, [response("", output=batch), response()])
    handler_calls = []

    def handle(args):
        handler_calls.append(args)
        return {"valid": True}

    llm.run_tools("system", "user", [TOOL], {"simulate": handle}, 1000)
    assert len(handler_calls) == 16
    assert len(calls) == 2
    assert not calls[1].get("tools")
    outputs = [item for item in calls[1]["input"] if isinstance(item, dict) and item.get("type") == "function_call_output"]
    assert len(outputs) == 20
    assert all("error" in json.loads(item["output"]) for item in outputs[16:])


@pytest.mark.parametrize("arguments", ["{broken", "[]", "null"])
def test_invalid_tool_arguments_return_error_and_allow_a_valid_final_answer(monkeypatch, arguments):
    calls = provider(monkeypatch, [response("", output=[function_call(arguments)]), response()])
    handler_calls = []
    final, _ = llm.run_tools("system", "user", [TOOL], {"simulate": lambda args: handler_calls.append(args)}, 1)
    assert json.loads(final) == {"answer": "ok"}
    assert handler_calls == []
    outputs = [item for item in calls[1]["input"] if isinstance(item, dict) and item.get("type") == "function_call_output"]
    assert len(outputs) == 1 and "error" in json.loads(outputs[0]["output"])


def test_handler_validation_failure_returns_error_without_exposing_exception(monkeypatch):
    calls = provider(monkeypatch, [response("", output=[function_call()]), response()])

    def invalid(args):
        raise ValueError("private validation context")

    final, _ = llm.run_tools("system", "user", [TOOL], {"simulate": invalid}, 1)
    assert json.loads(final) == {"answer": "ok"}
    outputs = [item for item in calls[1]["input"] if isinstance(item, dict) and item.get("type") == "function_call_output"]
    assert "error" in json.loads(outputs[0]["output"])
    assert "private validation context" not in outputs[0]["output"]


def test_cache_keeps_copies_separate_across_model_and_schema(monkeypatch):
    calls = provider(monkeypatch, [response('{"answer": ["original"]}') for _ in range(3)])
    monkeypatch.setenv("LLM_MODEL", "model-a")
    first = llm.complete_json("system", "user")
    first["answer"].append("mutated")
    assert llm.complete_json("system", "user") == {"answer": ["original"]}
    assert len(calls) == 1
    monkeypatch.setenv("LLM_MODEL", "model-b")
    llm.complete_json("system", "user")
    llm.complete_json("system", "user", schema=SCHEMA)
    assert len(calls) == 3


def test_cache_storage_copies_values_expires_ttl_and_bounds_memory():
    value = {"items": ["original"]}
    llm._cache_put("copy", value)
    value["items"].append("caller mutation")
    fetched = llm._cache_get("copy")
    assert fetched == {"items": ["original"]}
    fetched["items"].append("reader mutation")
    assert llm._cache_get("copy") == {"items": ["original"]}

    timestamp, cached_value = llm._cache["copy"]
    llm._cache["copy"] = (timestamp - llm._CACHE_TTL_SECONDS - 1, cached_value)
    assert llm._cache_get("copy") is None
    assert "copy" not in llm._cache

    for i in range(llm._CACHE_MAX_ENTRIES + 1):
        llm._cache_put(f"key-{i}", {"index": i})
    assert len(llm._cache) <= llm._CACHE_MAX_ENTRIES
    assert llm._cache_get(f"key-{llm._CACHE_MAX_ENTRIES}") == {"index": llm._CACHE_MAX_ENTRIES}


def test_request_cannot_start_after_deadline_and_caps_timeout_to_remaining_time(monkeypatch):
    calls = provider(monkeypatch, [response()])
    client = llm._client()
    with pytest.raises(llm.LLMUnavailable) as caught:
        llm._request(client, 0, "deadline_test", model="fake", input="user")
    assert caught.value.stage == "deadline_test"
    assert caught.value.code in {"timeout", "deadline", "deadline_exceeded"}
    assert calls == []
    llm._request(client, time.monotonic() + 1, "deadline_test", model="fake", input="user")
    assert len(calls) == 1
    assert 0 < calls[0]["timeout"] <= 1


def test_agent_workflow_has_one_deadline_across_provider_calls_and_tools(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(llm, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setenv("LLM_TOTAL_TIMEOUT_SECONDS", "60")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        now[0] += 30
        return response("", output=[function_call()])

    def handle(args):
        now[0] += 31
        return {"valid": True}

    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(responses=SimpleNamespace(create=create)))
    with pytest.raises(llm.LLMUnavailable) as caught:
        llm.run_tools("system", "user", [TOOL], {"simulate": handle}, 2)
    assert caught.value.code == "timeout"
    assert len(calls) == 1  # The second request must not reset the workflow timer.
    assert llm._cache == {}


def test_client_disables_sdk_retries(monkeypatch):
    import openai

    kwargs = {}
    sentinel = object()

    def construct(**options):
        kwargs.update(options)
        return sentinel

    monkeypatch.setenv("LLM_API_KEY", "test-only-placeholder")
    monkeypatch.setattr(openai, "OpenAI", construct)
    assert llm._client() is sentinel
    assert kwargs["max_retries"] == 0


def malformed_response(shape):
    result = response()
    if shape == "output_none":
        result.output = None
    elif shape == "missing_content":
        result.output = [SimpleNamespace(type="message", role="assistant")]
    else:
        result.output = [SimpleNamespace(type="message", role="assistant", content=[SimpleNamespace(type="output_text", text=123)])]
    return result


@pytest.mark.parametrize("shape", ["output_none", "missing_content", "nonstring_text"])
@pytest.mark.parametrize("method", ["complete", "tools"])
def test_malformed_sdk_output_is_normalized_and_never_cached(monkeypatch, shape, method):
    calls = provider(monkeypatch, [malformed_response(shape)])
    with pytest.raises(llm.LLMUnavailable) as caught:
        if method == "complete":
            llm.complete_json("system", "malformed_sdk")
        else:
            llm.run_tools("system", "malformed_sdk", [TOOL], {}, 1)
    assert caught.value.code == "invalid_response"
    assert caught.value.stage != "unknown"
    assert llm._cache == {}
    assert len(calls) == 1


def test_malformed_sdk_response_keeps_scenario_api_available(monkeypatch):
    calls = provider(monkeypatch, [malformed_response("output_none"), malformed_response("output_none")])
    decisions = [
        {"measure_id": "M7", "district_id": "nura"},
        {"measure_id": "M8", "district_id": "nura"},
        {"measure_id": "M10", "district_id": "nura"},
        {"measure_id": "M12", "district_id": None},
        {"measure_id": "M5", "district_id": "saryarka"},
    ]
    with TestClient(app) as client:
        result = client.post("/api/scenario", json={"team": "Malformed SDK response test", "decisions": decisions})
    assert result.status_code == 200
    body = result.json()
    assert body["ai_mode"] == "fallback"
    assert body["ai_status"]["code"] == "invalid_response"
    assert body["analysis"]["summary"]
    assert body["result"]["score_after"] == 56.54
    assert llm._cache == {}
    assert 1 <= len(calls) <= 2


@pytest.mark.parametrize("method", ["complete", "tools"])
@pytest.mark.parametrize("request_fails", [False, True])
@pytest.mark.parametrize("close_fails", [False, True])
def test_provider_client_closes_on_success_and_failure_without_masking_outcome(monkeypatch, method, request_fails, close_fails):
    created = []
    closed = []
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        if request_fails:
            raise RuntimeError("private provider failure")
        return response()

    def close():
        closed.append(True)
        if close_fails:
            raise RuntimeError("cleanup must not replace response")

    def make_client():
        created.append(True)
        return SimpleNamespace(responses=SimpleNamespace(create=create), close=close)

    def invoke():
        if method == "complete":
            return llm.complete_json("system", "closure")
        return llm.run_tools("system", "closure", [], {}, 0)

    monkeypatch.setattr(llm, "_client", make_client)
    if request_fails:
        with pytest.raises(llm.LLMUnavailable) as caught:
            invoke()
        assert caught.value.code == "provider_error"
        assert "cleanup" not in str(caught.value)
    else:
        expected = {"answer": "ok"} if method == "complete" else ('{"answer": "ok"}', [])
        assert invoke() == expected
        assert invoke() == expected  # A cache hit does not construct another client.
    assert len(created) == len(requests) == len(closed) == 1


@pytest.mark.parametrize("method", ["complete", "tools"])
def test_domain_invalid_json_is_not_cached_and_next_user_retry_calls_provider(monkeypatch, method):
    calls = provider(monkeypatch, [response('{"answer": "invalid"}'), response()])

    def validate(payload):
        if payload["answer"] != "ok":
            raise ValueError("domain validation failed")

    def invoke():
        if method == "complete":
            return llm.complete_json("system", "domain-check", validator=validate)
        return llm.run_tools("system", "domain-check", [], {}, 0, validator=validate)

    with pytest.raises(ValueError, match="domain validation failed"):
        invoke()
    assert llm._cache == {}
    assert len(calls) == 1
    result = invoke()
    assert result == ({"answer": "ok"} if method == "complete" else ('{"answer": "ok"}', []))
    assert len(calls) == 2
    assert invoke() == result
    assert len(calls) == 2


@pytest.mark.parametrize("method", ["complete", "tools"])
def test_cache_hit_is_revalidated_and_evicts_domain_invalid_value(monkeypatch, method):
    calls = provider(monkeypatch, [response('{"answer": "invalid"}'), response()])
    validated = []

    def validate(payload):
        validated.append(payload["answer"])
        if payload["answer"] != "ok":
            raise ValueError("domain validation failed")

    def invoke(validator=None):
        if method == "complete":
            return llm.complete_json("system", "revalidation", validator=validator)
        return llm.run_tools("system", "revalidation", [], {}, 0, validator=validator)

    invoke()
    assert len(calls) == 1 and llm._cache
    with pytest.raises(ValueError, match="domain validation failed"):
        invoke(validate)
    assert llm._cache == {}
    assert len(calls) == 1  # Revalidation itself makes no paid retry.
    result = invoke(validate)
    assert result == ({"answer": "ok"} if method == "complete" else ('{"answer": "ok"}', []))
    assert len(calls) == 2
    assert validated == ["invalid", "ok"]
