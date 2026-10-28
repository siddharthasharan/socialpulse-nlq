"""Run the Phase 2 benchmark (35 questions) against Phase 1 data.

Scoring: a question passes if the planner picks the expected metric (or
expected plan type), the SQL guard passes, execution returns the expected
shape, and scalar/breakdown values match gold values within 2% (or 0.01
absolute for rates, whichever is larger). Ambiguous/invalid questions pass
by clarifying, not by returning a number. Gold scalar values come from
scripts/validate_phase1.py (canonical queries on seed 42 data).
Weather-report cases are scored the same way, but against the report's
cards: expected_cards maps a card id to its expected current value, and
expected_watchouts_contains lists phrases that must appear in the
watchouts. Weather gold values come from src/socialpulse_nlq/weather.py
on seed 42 data, and differ from the canonical whole-window values where
the report uses different periods (for example, weather D7 uses recent
complete cohorts, not the Jan 1–Feb 21 cohort window).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from socialpulse_nlq.planner import guard_sql, plan, run_plan
from socialpulse_nlq.weather import compute_weather


def close(actual, expected):
    tol = max(0.02 * abs(expected), 0.01)
    return abs(actual - expected) <= tol


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data/phase1")
    ap.add_argument("--benchmark", type=Path, default=ROOT / "evals/benchmark_phase2.json")
    args = ap.parse_args()
    if not (args.data / "dim_users.parquet").exists():
        print(f"Data not found at {args.data}. Run scripts/generate_phase1.py first.")
        raise SystemExit(2)
    cases = json.loads(args.benchmark.read_text())
    results = []
    for c in cases:
        p = plan(c["question"])
        ok, detail = True, ""
        if c["expected_type"] in ("clarification", "refusal"):
            ok = p["type"] == c["expected_type"] and p["sql"] is None
            if "expected_contains" in c and c["expected_contains"].lower() not in (p.get("clarification") or "").lower():
                ok, detail = False, f"clarification did not contain {c['expected_contains']!r}"
        elif c["expected_type"] == "experiment_list":
            ok = p["type"] == "experiment_list"
        elif c["expected_type"] == "experiment_info":
            ok = p["type"] == "experiment_info" and p.get("metric") == c["expected_metric"]
        elif c["expected_type"] == "weather_report":
            if p["type"] != "weather_report" or p["sql"] is not None:
                ok, detail = False, f"type {p['type']} != weather_report"
            elif "expected_cards" in c or "expected_watchouts_contains" in c:
                w = compute_weather(args.data)
                by_id = {card["id"]: card for card in w["cards"]}
                for card_id, expected in c.get("expected_cards", {}).items():
                    if card_id not in by_id:
                        ok, detail = False, f"weather card {card_id} missing"
                        break
                    if not close(float(by_id[card_id]["current"]), expected):
                        ok, detail = False, f"weather {card_id}: {by_id[card_id]['current']} != {expected}"
                        break
                if ok:
                    joined = " ".join(w["watchouts"]).lower()
                    for phrase in c.get("expected_watchouts_contains", []):
                        if phrase.lower() not in joined:
                            ok, detail = False, f"watchout missing {phrase!r}"
                            break
        else:
            if p["type"] != "query" or p.get("metric") != c["expected_metric"]:
                ok, detail = False, f"metric {p.get('metric')} != {c['expected_metric']} (type {p['type']})"
            else:
                gok, greason = guard_sql(p["sql"])
                if not gok:
                    ok, detail = False, f"guard failed: {greason}"
                else:
                    cols, rows, ms = run_plan(p, args.data)
                    if c["expected_type"] == "scalar":
                        if len(rows) != 1 or len(cols) != 1:
                            ok, detail = False, f"expected 1x1, got {len(rows)} rows x {len(cols)} cols"
                        elif "expected_value" in c and not close(float(rows[0][0]), c["expected_value"]):
                            ok, detail = False, f"value {rows[0][0]} != {c['expected_value']}"
                    elif c["expected_type"] == "series":
                        if len(rows) < c["min_rows"]:
                            ok, detail = False, f"only {len(rows)} rows, expected >= {c['min_rows']}"
                        elif "expected_values" in c:
                            got = [float(r[-1]) for r in rows]
                            if len(got) != len(c["expected_values"]) or any(
                                    not close(g, e) for g, e in zip(got, c["expected_values"])):
                                ok, detail = False, f"series values {got} != {c['expected_values']}"
                    elif c["expected_type"] == "series_by_dim":
                        dims = {r[0] for r in rows}
                        if len(rows) < c["min_rows"] or len(dims) != c["expected_dim_count"]:
                            ok, detail = False, f"rows={len(rows)} dims={len(dims)}"
                    elif c["expected_type"] == "breakdown":
                        if len(rows) != c["expected_rows"]:
                            ok, detail = False, f"rows={len(rows)} != {c['expected_rows']}"
                        elif "expected_values" in c:
                            got = {str(r[0]): float(r[-1]) for r in rows}
                            for k, v in c["expected_values"].items():
                                if k not in got or not close(got[k], v):
                                    ok, detail = False, f"{k}: {got.get(k)} != {v}"
        results.append({"id": c["id"], "question": c["question"], "pass": bool(ok), "detail": detail,
                        "metric": p.get("metric"), "type": p["type"]})
        print(f"{'PASS' if ok else 'FAIL'} {c['id']} {c['question']} {detail}")
    passed = sum(r["pass"] for r in results)
    print(f"\nBenchmark: {passed}/{len(results)} pass")
    (args.data / "benchmark_report.json").write_text(json.dumps({"passed": passed, "total": len(results), "results": results}, indent=2))
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
