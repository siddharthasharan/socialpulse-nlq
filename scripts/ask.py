"""CLI for SocialPulse NLQ Phase 2: ask a question, see the plan, SQL, and result.

Usage: python scripts/ask.py "What was DAU by app over the last 28 days?" [--data data/phase1] [--json]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from socialpulse_nlq.planner import plan, run_plan
from socialpulse_nlq.weather import compute_weather


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--data", type=Path, default=Path("data/phase1"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    p = plan(args.question)
    if p["type"] in ("clarification", "refusal"):
        if args.json:
            print(json.dumps(p, indent=2))
        else:
            print(f"Clarification: {p['clarification']}")
        return
    if p["type"] in ("experiment_list", "experiment_info"):
        if args.json:
            print(json.dumps(p, indent=2))
        else:
            print(p["explanation"])
        return
    if p["type"] == "weather_report":
        w = compute_weather(args.data)
        if args.json:
            print(json.dumps(w, indent=2, default=str))
            return
        print(w["headline"])
        print()
        for c in w["cards"]:
            cur = f"{c['current'] * 100:.1f}%" if c["unit"] == "share" else f"{c['current']:,.1f}"
            print(f"{c['label']}: {cur} ({c['direction']}, {c['good_bad']})")
        print("\nWatchouts:")
        for x in w["watchouts"]:
            print(f"- {x}")
        return
    cols, rows, ms = run_plan(p, args.data)
    if args.json:
        print(json.dumps({**p, "columns": cols, "rows": [[str(v) for v in r] for r in rows[:50]], "runtime_ms": ms}, indent=2))
        return
    print(f"Metric: {p['metric']}")
    print(f"Grain: {p['grain']}")
    print(f"Filters: {p['filters']}  Breakdown: {p['breakdown']}")
    print(f"Tables: {', '.join(p['tables'])}  Runtime: {ms} ms  Rows: {len(rows)}")
    print("SQL:")
    print(p["sql"])
    print("Result:")
    print(" | ".join(cols))
    for r in rows[:20]:
        print(" | ".join(str(v) for v in r))
    if len(rows) > 20:
        print(f"... ({len(rows) - 20} more rows)")


if __name__ == "__main__":
    main()
