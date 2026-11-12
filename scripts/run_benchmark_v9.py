"""Run the frozen benchmark v9 (Phase 1, weekly-series turn).

Usage:
    PYTHONPATH=src python scripts/run_benchmark_v9.py [--split dev|heldout|all]
        [--data data/phase1] [--skip-oracle] [--trace-out PATH] [--no-trace]

v9 = v8's 162 cases plus the user probes p13 ("What's the weekly
trend for active users for Instagram?", single) and p14 (the same
question as a follow-up -- "same for WhatsApp" after the weekly
series, conversation), wired in by
scripts/wire_weekly_series_cases.py. Totals: 164 cases,
134 dev / 30 held-out. v1-v8 stay frozen and runnable for comparison.

--split dev (default) runs only the dev set. Held-out runs only on an
explicit --split heldout, and its results are reported separately — never
mixed into the dev aggregate, never used to decide a fix (see
docs/benchmark.md and evals/benchmark_v2_manifest.json).

The oracle runs first (unless --skip-oracle): every stored expected value
is checked against an independent computation in evals/oracle_v9.json.
An oracle disagreement is a finding, not a system failure — stored values
are never rewritten by this script. The run still exits non-zero when
there is a disagreement, so it cannot be ignored silently.

Exit code 0 only when every case in the requested split passes task
success AND the oracle agrees on every case.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from socialpulse_nlq.eval_harness import FAMILIES, ManifestError, run_benchmark

VERSION = "v9"
RUBRIC = ["sql_syntax", "sql_safety", "execution", "result_equiv", "final_answer"]


def _item_str(item):
    if item["pass"] is None:
        return "na"
    return "pass" if item["pass"] else "FAIL"


def _print_summary(report):
    summary = report["summary"]
    print(f"\nBenchmark v9 [{report.get('variant', 'baseline')}] — split {report['split']} — "
          f"{summary['overall']['task_success']}/{summary['overall']['n']} task success")
    print(f"{'family':<28}{'n':>3}  {'task':>6}  " + "  ".join(f"{r[:9]:>9}" for r in RUBRIC))
    for fam in FAMILIES:
        slot = summary["by_family"].get(fam)
        if not slot:
            print(f"{fam:<28}{0:>3}  {'-':>6}  " + "  ".join("      -  " for _ in RUBRIC))
            continue
        cells = []
        for name in RUBRIC:
            s = slot["items"].get(name, {"pass": 0, "applicable": 0})
            cells.append(f"{s['pass']:>4}/{s['applicable']:<4}" if s["applicable"] else "   -   ")
        print(f"{fam:<28}{slot['n']:>3}  {slot['task_success']:>3}/{slot['n']:<3}  " + "  ".join(cells))
    failures = [r for r in report["results"] if not r["task_success"]]
    if failures:
        print("\nTask-success failures:")
        for r in failures:
            detail = r["items"]["final_answer"]["reason"]
            print(f"  FAIL {r['id']} [{r['family']}] {detail}")
    sem_fail = [r for r in report["results"]
                if not r["sql_semantic"] and r["task_success"]]
    if sem_fail:
        print("\nRight answer, wrong SQL (task success but sql_semantic fail):")
        for r in sem_fail:
            print(f"  WARN {r['id']} [{r['family']}] metric={r.get('metric')}")


def _print_llm_accounting(report):
    """LLM-variant accounting from the traces: how many cases the model
    actually ran vs fell back, real tokens, paid-rate cost vs billed
    cost, and latency. A run with fallbacks is labeled as such — it is
    not a pure LLM comparison."""
    llm_fields = []
    for r in report["results"]:
        if r.get("llm"):
            llm_fields.append(r["llm"])
        for t in r.get("turns") or []:
            if t.get("llm"):
                llm_fields.append(t["llm"])
    ran = [f for f in llm_fields if not f.get("fallback") and not f.get("not_consulted")]
    fell_back = [f for f in llm_fields if f.get("fallback")]
    not_consulted = [f for f in llm_fields if f.get("not_consulted")]
    prompt_tokens = sum(f.get("prompt_tokens") or 0 for f in ran)
    completion_tokens = sum(f.get("completion_tokens") or 0 for f in ran)
    paid_rate = sum(f.get("cost_usd_paid_rate") or 0.0 for f in ran)
    billed = sum(f.get("billed_usd") or 0.0 for f in ran)
    latencies = sorted(f.get("latency_ms") for f in ran if f.get("latency_ms") is not None)
    tier = (report.get("llm") or {}).get("tier", "free")
    print("\nLLM accounting (tier: "
          f"{tier}; paid-rate cost is at $0.75 input / $3.75 output per 1M; "
          f"billed is $0 on the free tier):")
    print(f"  model calls made: {len(ran)}; fallbacks: {len(fell_back)}; "
          f"pre-checks (model not consulted): {len(not_consulted)}")
    for f in fell_back:
        print(f"  FALLBACK {f.get('fallback')}: {f.get('fallback_reason')}")
    print(f"  real tokens: {prompt_tokens} prompt + {completion_tokens} completion")
    print(f"  cost at paid rates: ${paid_rate:.4f}; billed: ${billed:.4f}")
    if latencies:
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        print(f"  model-call latency ms: P50 {p50}, P95 {p95} "
              f"(n={len(latencies)}; SQL execution time is separate, in runtime_ms)")
    if fell_back:
        print("  WARNING: this run contains fallbacks — it is NOT a pure "
              "LLM comparison. See scripts/compare_llm.py, which refuses "
              "to compare a fallback run as if the model ran it.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data/phase1")
    ap.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    ap.add_argument("--variant", choices=["baseline", "catalog-assisted", "validated", "llm"],
                    default="baseline",
                    help="slice-3 planner variant (see docs/ablations.md); "
                         "default baseline is the current planner exactly as-is; "
                         "'llm' plans through the Gemini LLM planner "
                         "(see docs/llm-planner.md)")
    ap.add_argument("--limit", type=int, default=None,
                    help="LLM variant only: run only the first N cases of the "
                         "ordered split (singles in file order, then "
                         "conversations). The free tier's daily request cap "
                         "makes a full 164-case run a multi-day job; partial "
                         "runs are labeled as such in the trace.")
    ap.add_argument("--offset", type=int, default=0,
                    help="LLM variant only: skip the first N cases of the "
                         "ordered split (resume a paced run the next day).")
    ap.add_argument("--tier", choices=["free", "paid"], default="free",
                    help="LLM variant only: the AI Studio project's tier. "
                         "Free bills $0; paid bills the paid-rate cost. "
                         "The code cannot detect this — declare it honestly.")
    ap.add_argument("--pace", type=float, default=1.0,
                    help="LLM variant only: seconds to sleep between LLM "
                         "calls (default 1.0). The free tier is rate-limited "
                         "per minute as well as per day.")
    ap.add_argument("--skip-oracle", action="store_true")
    ap.add_argument("--trace-out", type=Path, default=None)
    ap.add_argument("--no-trace", action="store_true")
    args = ap.parse_args()
    if args.variant == "llm" and not os.environ.get("GEMINI_API_KEY"):
        print("WARNING: variant=llm with no GEMINI_API_KEY set. Every case "
              "will fall back to the deterministic baseline plan, labeled "
              "fallback=no_key in the trace. Those numbers are baseline "
              "numbers, NOT LLM numbers — do not compare them as if the "
              "model ran.")
    if args.variant == "llm" and (args.limit is not None or args.offset):
        print(f"NOTE: partial LLM run — limit={args.limit} offset={args.offset}. "
              "The trace records this; a partial run is never a complete "
              "comparison.")
    try:
        report = run_benchmark(root=ROOT, data_dir=args.data, split=args.split,
                               with_oracle=not args.skip_oracle, version=VERSION,
                               variant=args.variant, llm_tier=args.tier,
                               llm_pace_s=args.pace, limit=args.limit,
                               offset=args.offset)
    except ManifestError as e:
        print(f"REFUSED TO RUN: {e}")
        return 2
    if report.get("oracle") is not None:
        agree = sum(1 for o in report["oracle"] if o["agree"])
        print(f"Oracle: {agree}/{len(report['oracle'])} agree with stored expectations")
        for o in report["oracle"]:
            if not o["agree"]:
                print(f"  ORACLE-DISAGREE {o['id']}: {o['detail']}")
            elif o.get("inexact"):
                print(f"  ORACLE-INEXACT {o['id']}: {o['detail']}")
    for r in report["results"]:
        marks = " ".join(f"{n}={_item_str(r['items'][n])}" for n in RUBRIC)
        print(f"{'PASS' if r['task_success'] else 'FAIL'} {r['id']} [{r['family']}] {marks}")
    _print_summary(report)
    if report.get("variant") == "llm":
        _print_llm_accounting(report)
    if not args.no_trace:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = args.trace_out or (ROOT / "evals/runs" /
                                 f"benchmark_v9_{args.variant}_{args.split}_{stamp}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nTrace written to {out}")
    oracle_ok = report.get("oracle") is None or all(
        o["agree"] for o in report["oracle"])
    all_ok = all(r["task_success"] for r in report["results"]) and oracle_ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
