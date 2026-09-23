"""The only module allowed to call the LLM. Swap providers here only."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable


class LLMUnavailable(Exception):
    pass


_cache: dict[str, Any] = {}


def _cache_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _model() -> str:
    return os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")


def _timeout() -> float:
    try:
        return float(os.environ.get("LLM_TIMEOUT_SECONDS", "20"))
    except ValueError:
        return 20.0


def _client():
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise LLMUnavailable("LLM_API_KEY не задан")
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise LLMUnavailable(str(e)) from e
    return anthropic.Anthropic(api_key=api_key, timeout=_timeout())


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def complete_json(system: str, user: str) -> dict:
    """One-shot call that must return a JSON object. Raises LLMUnavailable on any failure."""
    key = _cache_key("complete_json", system, user)
    if key in _cache:
        return _cache[key]

    try:
        client = _client()
        resp = client.messages.create(
            model=_model(),
            max_tokens=1500,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in resp.content if getattr(block, "type", None) == "text")
        data = _extract_json(text)
    except LLMUnavailable:
        raise
    except Exception as e:
        raise LLMUnavailable(str(e)) from e

    if not isinstance(data, dict):
        raise LLMUnavailable("LLM вернул не JSON-объект")

    _cache[key] = data
    return data


def run_tools(
    system: str,
    user: str,
    tools: list[dict],
    handlers: dict[str, Callable[[dict], Any]],
    max_calls: int,
) -> tuple[str, list[dict]]:
    """Agentic loop: the model may call tools up to max_calls times, then must answer with text."""
    key = _cache_key("run_tools", system, user, json.dumps(tools, sort_keys=True), str(max_calls))
    if key in _cache:
        return _cache[key]

    try:
        client = _client()
        model = _model()
        messages: list[dict] = [{"role": "user", "content": user}]
        trace: list[dict] = []
        calls = 0
        final_text = ""

        while True:
            resp = client.messages.create(
                model=model,
                max_tokens=1500,
                temperature=0.3,
                system=system,
                tools=tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                final_text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
                break

            tool_results = []
            for tu in tool_uses:
                if calls >= max_calls:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps({"error": "лимит вызовов инструментов исчерпан"}),
                    })
                    continue
                calls += 1
                handler = handlers.get(tu.name)
                output = handler(tu.input) if handler else {"error": f"неизвестный инструмент {tu.name}"}
                trace.append({"tool": tu.name, "input": tu.input, "output": output})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(output, ensure_ascii=False),
                })
            messages.append({"role": "user", "content": tool_results})

            if calls >= max_calls:
                resp2 = client.messages.create(
                    model=model,
                    max_tokens=1500,
                    temperature=0.3,
                    system=system,
                    messages=messages,
                )
                final_text = "".join(b.text for b in resp2.content if getattr(b, "type", None) == "text")
                break
    except LLMUnavailable:
        raise
    except Exception as e:
        raise LLMUnavailable(str(e)) from e

    _cache[key] = (final_text, trace)
    return final_text, trace
