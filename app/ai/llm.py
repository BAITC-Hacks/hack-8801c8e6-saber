"""OpenAI Responses adapter: structured output, bounded tools/cache, safe failures.

Only this module calls the provider. Domain validation stays in analyst/agent.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable

logger = logging.getLogger(__name__)
REASONING_EFFORT = "low"
MAX_OUTPUT_TOKENS = 2000
_CACHE_MAX_ENTRIES = 128
_CACHE_TTL_SECONDS = 600
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


class LLMUnavailable(Exception):
    def __init__(self, message: str = "", *, code: str = "provider_error", stage: str = "unknown"):
        self.code = code
        self.stage = stage
        super().__init__(message or f"{stage}: {code}")


def failure_status(error: Exception) -> dict[str, str]:
    """Stable user-facing reason; never expose provider text, prompts, or secrets."""
    messages = {
        "not_configured": "AI не настроен. Использован резервный расчёт.",
        "timeout": "AI не успел ответить. Использован резервный расчёт.",
        "rate_limited": "AI временно перегружен. Использован резервный расчёт.",
        "authentication": "Не удалось подключиться к AI. Использован резервный расчёт.",
        "invalid_json": "AI вернул неполный ответ. Использован резервный расчёт.",
        "invalid_response": "Ответ AI не прошёл проверку. Использован резервный расчёт.",
        "token_limit": "AI не завершил ответ. Использован резервный расчёт.",
        "incomplete_response": "AI не завершил ответ. Использован резервный расчёт.",
        "refusal": "AI не предложил ответ для этого запроса. Использован резервный расчёт.",
        "provider_error": "AI временно недоступен. Использован резервный расчёт.",
    }
    code = getattr(error, "code", "invalid_response")
    if code not in messages:
        code = "provider_error"
    return {"code": code, "message": messages[code]}


def _cache_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in (_model(), *parts):
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _cache_get(key: str) -> Any:
    with _cache_lock:
        entry = _cache.pop(key, None)
        if entry is None or time.monotonic() - entry[0] >= _CACHE_TTL_SECONDS:
            return None
        _cache[key] = entry
        return copy.deepcopy(entry[1])


def _cache_put(key: str, value: Any) -> None:
    with _cache_lock:
        now = time.monotonic()
        for old_key, (created, _) in list(_cache.items()):
            if now - created >= _CACHE_TTL_SECONDS:
                _cache.pop(old_key)
        _cache.pop(key, None)
        while len(_cache) >= _CACHE_MAX_ENTRIES:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (now, copy.deepcopy(value))


def _validate_payload(key: str, payload: dict, validator: Callable[[dict], Any] | None) -> None:
    if validator is None:
        return
    try:
        validator(payload)
    except Exception:
        with _cache_lock:
            _cache.pop(key, None)
        raise


def _model() -> str:
    return os.environ.get("LLM_MODEL", "gpt-6-luna")


def _seconds(name: str, default: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        return min(value, maximum) if math.isfinite(value) and value > 0 else default
    except ValueError:
        return default


def _timeout() -> float:
    return _seconds("LLM_TIMEOUT_SECONDS", 20.0, 30.0)


def _client():
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise LLMUnavailable(code="not_configured", stage="configuration")
    try:
        import openai
        return openai.OpenAI(api_key=api_key, timeout=_timeout(), max_retries=0)
    except Exception as exc:
        raise LLMUnavailable(code="provider_error", stage="client_setup") from exc


@contextmanager
def _client_session():
    client = _client()
    try:
        yield client
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                logger.warning("llm_cleanup exception=%s", type(exc).__name__)


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    fence = "```"
    fenced = re.fullmatch(fence + r"(?:json)?\s*([\s\S]*?)\s*" + fence, text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


def _json_object(text: str) -> dict:
    try:
        data = _extract_json(text)
    except (ValueError, TypeError) as exc:
        raise LLMUnavailable(code="invalid_json", stage="parse_final") from exc
    if not isinstance(data, dict):
        raise LLMUnavailable(code="invalid_json", stage="parse_final")
    return data


def _strict_schema(schema: dict) -> dict:
    """Make optional fields explicitly nullable, as required by strict outputs."""
    result = copy.deepcopy(schema)

    def normalize(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        if node.get("type") == "object" or "properties" in node:
            properties = node.setdefault("properties", {})
            required = set(node.get("required", []))
            for name, child in properties.items():
                if name not in required:
                    properties[name] = {"anyOf": [child, {"type": "null"}]}
            node["required"] = list(properties)
            node["additionalProperties"] = False
        for key, value in node.items():
            if key in ("properties", "$defs", "definitions"):
                for child in value.values():
                    normalize(child)
            elif isinstance(value, dict):
                normalize(value)
            elif isinstance(value, list):
                for child in value:
                    normalize(child)

    normalize(result)
    return result


def _format(schema: dict | None) -> dict:
    if schema is None:
        return {"format": {"type": "json_object"}}
    return {"format": {"type": "json_schema", "name": "akim_response", "strict": True, "schema": _strict_schema(schema)}}


def _tool_to_responses(tool: dict) -> dict:
    return {
        "type": "function", "name": tool["name"],
        "description": tool.get("description", ""), "strict": True,
        "parameters": _strict_schema(tool.get("input_schema", {"type": "object", "properties": {}})),
    }


def _item_to_dict(item: Any) -> Any:
    if hasattr(item, "to_dict"):
        return item.to_dict()
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return item


def _request(client: Any, deadline: float, stage: str, **kwargs: Any) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LLMUnavailable(code="timeout", stage=stage)
    started = time.monotonic()
    try:
        response = client.responses.create(timeout=min(_timeout(), remaining), **kwargs)
    except Exception as exc:
        kind = type(exc).__name__
        code = "timeout" if "Timeout" in kind else "rate_limited" if kind == "RateLimitError" else "authentication" if kind in ("AuthenticationError", "PermissionDeniedError") else "provider_error"
        logger.warning("llm_failure stage=%s code=%s exception=%s", stage, code, kind)
        raise LLMUnavailable(code=code, stage=stage) from exc
    status = getattr(response, "status", "completed")
    logger.info("llm_response stage=%s status=%s duration_ms=%d", stage, status, (time.monotonic() - started) * 1000)
    if time.monotonic() >= deadline:
        raise LLMUnavailable(code="timeout", stage=stage)
    if status != "completed":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
        code = "token_limit" if reason == "max_output_tokens" else "incomplete_response"
        raise LLMUnavailable(code=code, stage=stage)
    _validate_output(response, stage)
    return response


def _validate_output(response: Any, stage: str) -> None:
    """SDK response models are not a substitute for checking the wire shape."""
    output = getattr(response, "output", None)
    valid = isinstance(output, list)
    for item in output if valid else []:
        kind = getattr(item, "type", None)
        if kind == "message":
            content = getattr(item, "content", None)
            if not isinstance(content, list):
                valid = False
                break
            for part in content:
                part_type = getattr(part, "type", None)
                if part_type not in ("output_text", "refusal") or (part_type == "output_text" and not isinstance(getattr(part, "text", None), str)):
                    valid = False
        elif kind == "function_call":
            if not all(isinstance(getattr(item, field, None), str) and getattr(item, field) for field in ("name", "call_id")):
                valid = False
    if not valid:
        raise LLMUnavailable(code="invalid_response", stage=stage)


def _final_text(response: Any) -> str:
    # output_text combines interim prose with the final answer if both exist.
    messages = [item for item in response.output if getattr(item, "type", None) == "message"]
    if messages:
        content = messages[-1].content
        if any(getattr(part, "type", None) == "refusal" for part in content):
            raise LLMUnavailable(code="refusal", stage="parse_final")
        return "".join(part.text for part in content if getattr(part, "type", None) == "output_text")
    text = getattr(response, "output_text", "")
    if not isinstance(text, str):
        raise LLMUnavailable(code="invalid_response", stage="parse_final")
    return text


def complete_json(system: str, user: str, *, schema: dict | None = None, validator: Callable[[dict], Any] | None = None) -> dict:
    key = _cache_key("complete_json", system, user, json.dumps(schema, sort_keys=True))
    cached = _cache_get(key)
    if cached is not None:
        _validate_payload(key, cached, validator)
        return cached
    deadline = time.monotonic() + _timeout()
    with _client_session() as client:
        response = _request(
            client, deadline, "analysis", model=_model(), instructions=system, input=user,
            reasoning={"effort": REASONING_EFFORT}, max_output_tokens=MAX_OUTPUT_TOKENS, text=_format(schema),
        )
        data = _json_object(_final_text(response))
        _validate_payload(key, data, validator)
    _cache_put(key, data)
    return data


def run_tools(
    system: str, user: str, tools: list[dict], handlers: dict[str, Callable[[dict], Any]],
    max_calls: int, *, schema: dict | None = None, validator: Callable[[dict], Any] | None = None,
) -> tuple[str, list[dict]]:
    max_calls = max(0, min(int(max_calls), 16))
    key = _cache_key("run_tools", system, user, json.dumps(tools, sort_keys=True), str(max_calls), json.dumps(schema, sort_keys=True))
    cached = _cache_get(key)
    if cached is not None:
        _validate_payload(key, _json_object(cached[0]), validator)
        return cached
    with _client_session() as client:
        return _run_tool_loop(client, system, user, tools, handlers, max_calls, schema, key, validator)


def _run_tool_loop(client: Any, system: str, user: str, tools: list[dict], handlers: dict[str, Callable[[dict], Any]], max_calls: int, schema: dict | None, key: str, validator: Callable[[dict], Any] | None) -> tuple[str, list[dict]]:
    deadline = time.monotonic() + _seconds("LLM_TOTAL_TIMEOUT_SECONDS", 60.0, 90.0)
    responses_tools = [_tool_to_responses(tool) for tool in tools]
    input_items: list[Any] = [{"type": "message", "role": "user", "content": user}]
    trace: list[dict] = []
    calls = 0
    while True:
        finalizing = calls >= max_calls
        options = {} if finalizing else {"tools": responses_tools}
        response = _request(
            client, deadline, "agent_final" if finalizing else "agent_tools", model=_model(),
            instructions=system, input=input_items, reasoning={"effort": REASONING_EFFORT},
            max_output_tokens=MAX_OUTPUT_TOKENS, text=_format(schema), **options,
        )
        function_calls = [item for item in response.output if getattr(item, "type", None) == "function_call"]
        if not function_calls:
            final_text = _final_text(response)
            _validate_payload(key, _json_object(final_text), validator)
            _cache_put(key, (final_text, trace))
            return final_text, trace
        if finalizing:
            raise LLMUnavailable(code="invalid_response", stage="agent_final")
        input_items.extend(_item_to_dict(item) for item in response.output)
        for fc in function_calls:
            if calls >= max_calls:
                output = {"error": "Лимит инструментов исчерпан; верни итоговый JSON."}
            else:
                calls += 1
                args = {}
                try:
                    args = _json_object(fc.arguments or "")
                    handler = handlers.get(fc.name)
                    output = handler(args) if handler else {"valid": False, "errors": ["UNKNOWN_TOOL"]}
                except (LLMUnavailable, ValueError, TypeError, KeyError, AttributeError):
                    output = {"valid": False, "errors": ["INVALID_TOOL_ARGUMENTS"], "error": "Проверь JSON-объект аргументов и обязательные поля инструмента."}
                trace.append({"tool": fc.name, "input": args, "output": output})
            input_items.append({
                "type": "function_call_output", "call_id": fc.call_id,
                "output": json.dumps(output, ensure_ascii=False),
            })
