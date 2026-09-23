"""Reproducible AI/engine smoke check. Offline by default; --live uses the key.

No scenario is saved and no leaderboard is modified. Exit 1 if any assertion
fails, including fallback during an explicitly requested live check.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import engine, optimizer
from app.ai import agent, analyst
from app.data_loader import load_data_or_exit

EXAMPLE = [engine.Decision("M7", "nura"), engine.Decision("M8", "nura"),
           engine.Decision("M10", "nura"), engine.Decision("M12"), engine.Decision("M5", "saryarka")]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use configured LLM_API_KEY; incurs provider usage")
    parser.add_argument("--output", type=Path, help="Write the verification report as JSON")
    args = parser.parse_args()
    if args.live and not os.environ.get("LLM_API_KEY"):
        parser.error("--live requires LLM_API_KEY; run inside the configured Docker container")
    if not args.live:
        os.environ.pop("LLM_API_KEY", None)
    data = load_data_or_exit()
    original = engine.simulate(EXAMPLE, data)
    expected_mode = "llm" if args.live else "fallback"
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "mode": expected_mode,
              "model": os.environ.get("LLM_MODEL", "gpt-6-luna") if args.live else None,
              "original_score": round(original["score_after"], 2), "checks": [], "passed": True}

    for objective in ("analysis", *optimizer.OBJECTIVES):
        started = time.monotonic()
        check = {"name": objective, "passed": False}
        try:
            if objective == "analysis":
                body = analyst.analyze(EXAMPLE, original, data)
                analyst._validate(body, data, original)
                check["analysis"] = {key: body[key] for key in analyst.REQUIRED_FIELDS}
            else:
                locks = EXAMPLE[:1] if objective == "score" else []
                body = agent.optimize(EXAMPLE, data, locked_decisions=locks, objective=objective)
                for strategy in body["strategies"]:
                    decisions = [engine.Decision(**item) for item in strategy["decisions"]]
                    if engine.validate(decisions, data) or not set(locks).issubset(decisions):
                        raise ValueError("A strategy violated constraints or locks")
                    actual = engine.simulate(decisions, data)
                    if engine.round_result(actual) != strategy["result"]:
                        raise ValueError("A strategy differs from engine recalculation")
                    if optimizer.compare_results(actual, original, strategy["objective"]) < 0:
                        raise ValueError("A strategy worsened its objective")
                check.update(source=body["source"], score=body["best_score"],
                             d_min=body["best_result"]["d_min"], n_crit=body["best_result"]["n_crit"],
                             budget=body["best_result"]["total_cost"], hypotheses=len(body["hypotheses"]),
                             locked_decisions=body["locked_decisions"])
            check.update(ai_mode=body["ai_mode"], ai_status=body.get("ai_status"))
            if body["ai_mode"] != expected_mode:
                raise ValueError("Live AI was requested but a fallback was returned")
            check["passed"] = True
        except Exception as exc:
            # Avoid arbitrary exception text: SDK errors may contain request data.
            check["error_type"] = type(exc).__name__
        check["seconds"] = round(time.monotonic() - started, 2)
        report["checks"].append(check)
        report["passed"] = report["passed"] and check["passed"]
        print(json.dumps({key: value for key, value in check.items() if key != "analysis"}, ensure_ascii=False), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
