"""The only module allowed to call the LLM. Swap providers here only.

Provider: OpenAI, Responses API (v1/responses). LLM_API_KEY holds the OpenAI
key, LLM_MODEL an OpenAI model id (default: gpt-6-luna, a reasoning model).

Why the Responses API and not Chat Completions: reasoning models in this
family reject the `temperature` parameter outright (only the default is
accepted), so speed/quality is steered with `reasoning.effort` instead.
Chat Completions only allows function/tool calling when reasoning_effort is
"none" — which defeats the point of a fast "low"-effort agent loop — while
the Responses API supports tool calling together with any reasoning effort,
so it is used uniformly for both complete_json and run_tools.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable

REASONING_EFFORT = "low"
MAX_OUTPUT_TOKENS = 2000


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
    return os.environ.get("LLM_MODEL", "gpt-6-luna")


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
        import openai
    except ImportError as e:  # pragma: no cover
        raise LLMUnavailable(str(e)) from e
    return openai.OpenAI(api_key=api_key, timeout=_timeout())


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def _tool_to_responses(tool: dict) -> dict:
    return {
        "type": "function",
        "name": tool["name"],
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
    }


def _item_to_dict(item: Any) -> Any:
    if hasattr(item, "to_dict"):
        return item.to_dict()
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return item


def complete_json(system: str, user: str) -> dict:
    """One-shot call that must return a JSON object. Raises LLMUnavailable on any failure."""
    key = _cache_key("complete_json", system, user)
    if key in _cache:
        return _cache[key]

    try:
        client = _client()
        resp = client.responses.create(
            model=_model(),
            instructions=system,
            input=user,
            reasoning={"effort": REASONING_EFFORT},
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        data = _extract_json(resp.output_text)
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
        responses_tools = [_tool_to_responses(t) for t in tools]
        input_items: list[Any] = [{"type": "message", "role": "user", "content": user}]
        trace: list[dict] = []
        calls = 0
        final_text = ""

        while True:
            resp = client.responses.create(
                model=model,
                instructions=system,
                input=input_items,
                tools=responses_tools,
                reasoning={"effort": REASONING_EFFORT},
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            input_items.extend(_item_to_dict(item) for item in resp.output)

            function_calls = [item for item in resp.output if getattr(item, "type", None) == "function_call"]
            if not function_calls:
                final_text = resp.output_text or ""
                break

            for fc in function_calls:
                name = fc.name
                try:
                    args = json.loads(fc.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                if calls >= max_calls:
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": fc.call_id,
                        "output": json.dumps({"error": "лимит вызовов инструментов исчерпан"}, ensure_ascii=False),
                    })
                    continue

                calls += 1
                handler = handlers.get(name)
                output = handler(args) if handler else {"error": f"неизвестный инструмент {name}"}
                trace.append({"tool": name, "input": args, "output": output})
                input_items.append({
                    "type": "function_call_output",
                    "call_id": fc.call_id,
                    "output": json.dumps(output, ensure_ascii=False),
                })

            if calls >= max_calls:
                resp2 = client.responses.create(
                    model=model,
                    instructions=system,
                    input=input_items,
                    reasoning={"effort": REASONING_EFFORT},
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                )
                final_text = resp2.output_text or ""
                break
    except LLMUnavailable:
        raise
    except Exception as e:
        raise LLMUnavailable(str(e)) from e

    _cache[key] = (final_text, trace)
    return final_text, trace
