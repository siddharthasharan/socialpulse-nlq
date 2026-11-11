"""Paired comparison: deterministic baseline vs LLM planner on the frozen v8 benchmark.

Reads two trace files produced by scripts/run_benchmark_v8.py (one
--variant baseline, one --variant llm, same split) and prints the
comparison: per-family task success, accuracy / coverage / abstention
(same definitions as scripts/compare_variants.py, singles only), the
paired flip list, P50/P95 turn latency, and real-token cost per
successful task.

Cost method (see docs/llm-planner.md): tokens are the REAL values from
the API usageMetadata in the traces. Cost is computed at the published
paid rates for gemini-3.8-flash ($0.75 input / $3.75 output per 1M
tokens through Dec 31 2026) and labeled as paid-rate cost; on the free
tier the actual billed cost is $0 — both are printed, and the
paid-rate figure is never presented as billed.

A trace that contains ANY fallback (no key, API error, malformed
reply) is NOT a pure LLM comparison: this script refuses to compare
it and exits non-zero, unless --allow-fallbacks is passed — in which
case the output is labeled as containing fallbacks throughout. A
partial run (limit/offset) is likewise labeled, never compared as if
complete.

Usage:
    PYTHONPATH=src python scripts/compare_llm.py <baseline-trace.json> <llm-trace.json>
    PYTHONPATH=src python scripts/compare_llm.py --runs-dir evals/runs [--allow-fallbacks]
"""
import argparse
import glob
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FAMILIES = ["metric_scoping", "ambiguous_metric", "multi_turn_context",
            "complex_joins_breakdowns", "incorrect_assumption",
            "guardrail_adversarial", "sql_safety_syntax", "aggregation_grain"]


def _load_compare_variants():
    """Reuse the rate / family-table / flip definitions from
    compare_variants.py instead of a second copy that can drift."""
    spec = importlib.util.spec_from_file_location(
        "compare_variants", ROOT / "scripts/compare_variants.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_expected(root):
    """{case id: expected_type} for v8 singles, from the frozen file."""
    singles = json.loads((Path(root) / "evals" / "benchmark_v8.json").read_text())
    return {c["id"]: c["expected_type"] for c in singles}


def _case_latency_ms(result):
    """Total turn latency for one case: SQL runtime plus, for the LLM
    variant, the model-call latency (singles: the plan's llm fields;
    conversations: the sum over turns)."""
    total = result.get("runtime_ms") or 0
    llm = result.get("llm")
    if llm and llm.get("latency_ms") is not None:
        total += llm["latency_ms"]
    for turn in result.get("turns") or []:
        total += turn.get("runtime_ms") or 0
        tllm = turn.get("llm") or {}
        if tllm.get("latency_ms") is not None:
            total += tllm["latency_ms"]
    return total


def _llm_totals(report):
    """(calls, fallbacks, not_consulted, prompt_tokens,
    completion_tokens, paid_rate_usd, billed_usd) over all llm
    receipts in a trace (singles + conversation turns)."""
    fields = []
    for r in report["results"]:
        if r.get("llm"):
            fields.append(r["llm"])
        for t in r.get("turns") or []:
            if t.get("llm"):
                fields.append(t["llm"])
    ran = [f for f in fields if not f.get("fallback") and not f.get("not_consulted")]
    fell = [f for f in fields if f.get("fallback")]
    notc = [f for f in fields if f.get("not_consulted")]
    return {
        "calls": len(ran),
        "fallbacks": fell,
        "not_consulted": len(notc),
        "prompt_tokens": sum(f.get("prompt_tokens") or 0 for f in ran),
        "completion_tokens": sum(f.get("completion_tokens") or 0 for f in ran),
        "paid_rate_usd": sum(f.get("cost_usd_paid_rate") or 0.0 for f in ran),
        "billed_usd": sum(f.get("billed_usd") or 0.0 for f in ran),
    }


def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * pct))
    return sorted_values[idx]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="*", type=Path)
    ap.add_argument("--runs-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--allow-fallbacks", action="store_true",
                    help="compare even when the LLM trace contains "
                         "fallbacks; the output is labeled as such")
    args = ap.parse_args()
    paths = list(args.traces)
    if args.runs_dir:
        for variant in ("baseline", "llm"):
            cands = sorted(
                (args.runs_dir / p for p in
                 glob.glob(f"benchmark_v8_{variant}_*.json",
                           root_dir=str(args.runs_dir))),
                key=lambda p: p.stat().st_mtime)
            if cands:
                paths.append(cands[-1])
    if len(paths) != 2:
        print(f"need 2 traces (baseline + llm, same split), got {len(paths)}: {paths}")
        return 2
    reports = {}
    for p in paths:
        rep = json.loads(p.read_text())
        key = rep.get("variant")
        if key not in ("baseline", "llm"):
            print(f"{p}: variant {key!r} is not baseline/llm")
            return 2
        if key in reports:
            print(f"duplicate trace for {key}: {p}")
            return 2
        reports[key] = rep
    if "baseline" not in reports or "llm" not in reports:
        print("need one baseline trace and one llm trace")
        return 2
    base, llm_rep = reports["baseline"], reports["llm"]
    if base["version"] != llm_rep["version"] or base["split"] != llm_rep["split"]:
        print(f"traces differ: baseline {base['version']}/{base['split']} vs "
              f"llm {llm_rep['version']}/{llm_rep['split']}; same version and split required")
        return 2
    totals = _llm_totals(llm_rep)
    llm_meta = llm_rep.get("llm") or {}
    if totals["fallbacks"] and not args.allow_fallbacks:
        print("REFUSED: the LLM trace contains "
              f"{len(totals['fallbacks'])} fallback(s) "
              f"({sorted({f.get('fallback') for f in totals['fallbacks']})}) — "
              "it is not a pure LLM comparison. Re-run with a key, or "
              "pass --allow-fallbacks to compare it labeled as such.")
        for f in totals["fallbacks"]:
            print(f"  FALLBACK {f.get('fallback')}: {f.get('fallback_reason')}")
        return 2
    if llm_meta.get("partial"):
        print(f"NOTE: LLM trace is a PARTIAL run (limit={llm_meta.get('limit')} "
              f"offset={llm_meta.get('offset')}, {llm_meta.get('cases_run')}/"
              f"{llm_meta.get('cases_in_split')} cases). This comparison "
              "covers only the cases run, not the full split.")
    cv = _load_compare_variants()
    expected = _load_expected(ROOT)
    split = base["split"]
    print(f"\n## Deterministic baseline vs LLM planner — v8, split {split}")
    print(f"\n{'family':<28}{'baseline':>14}{'llm':>14}")
    for fam in FAMILIES:
        b = cv._family_table(base)[fam]
        l = cv._family_table(llm_rep)[fam]
        print(f"{fam:<28}{b['task']:>4}/{b['n']:<4}      {l['task']:>4}/{l['n']:<4}")
    for name, rep in (("baseline", base), ("llm", llm_rep)):
        s = rep["summary"]["overall"]
        print(f"  {name}: {s['task_success']}/{s['n']} task success")
    summary = {"version": base["version"], "split": split,
               "variants": {}, "flips": {}, "llm_totals": totals,
               "llm_meta": llm_meta,
               "contains_fallbacks": bool(totals["fallbacks"]),
               "partial": bool(llm_meta.get("partial"))}
    for name, rep in (("baseline", base), ("llm", llm_rep)):
        rates = cv._rates(rep["results"], expected)
        lat = sorted(_case_latency_ms(r) for r in rep["results"])
        entry = {"overall": rep["summary"]["overall"],
                 "by_family": cv._family_table(rep),
                 "rates": rates,
                 "latency_ms": {"p50": _percentile(lat, 0.5),
                                "p95": _percentile(lat, 0.95),
                                "n": len(lat)}}
        summary["variants"][name] = entry
        print(f"\n### {name} ({split}) — singles only (n={rates['n_singles']})")
        print(f"  coverage {rates['attempted']}/{rates['n_singles']} "
              f"({rates['coverage']:.1%}); abstention {rates['abstention_rate']:.1%}; "
              f"accuracy among attempted: "
              f"{rates['accuracy']:.1%}" if rates["accuracy"] is not None else "  accuracy n/a")
        print(f"  appropriate abstentions ({len(rates['appropriate_abstentions'])}): "
              f"{rates['appropriate_abstentions']}")
        print(f"  wrong abstentions / coverage cost ({len(rates['wrong_abstentions'])}): "
              f"{rates['wrong_abstentions']}")
        print(f"  missed abstentions ({len(rates['missed_abstentions'])}): "
              f"{rates['missed_abstentions']}")
        print(f"  turn latency ms (SQL + model call): P50 {entry['latency_ms']['p50']}, "
              f"P95 {entry['latency_ms']['p95']} (n={len(lat)})")
    fixed, broken = cv._flips(base["results"], llm_rep["results"])
    summary["flips"] = {"fixed": fixed, "broken": broken}
    print(f"\n### Flips baseline -> llm ({split}): {len(fixed)} fixed, {len(broken)} broken")
    for r in fixed:
        print(f"  FIXED  {r['id']} [{r['family']}]")
    for r in broken:
        print(f"  BROKEN {r['id']} [{r['family']}]")
    successes = sum(int(r["task_success"]) for r in llm_rep["results"])
    print(f"\n### Cost (real tokens from usageMetadata; paid rates "
          f"$0.75 input / $3.75 output per 1M; tier {llm_meta.get('tier', 'free')})")
    print(f"  model calls: {totals['calls']}; "
          f"tokens: {totals['prompt_tokens']} prompt + "
          f"{totals['completion_tokens']} completion")
    print(f"  cost at paid rates: ${totals['paid_rate_usd']:.4f}; "
          f"billed: ${totals['billed_usd']:.4f}")
    if successes:
        print(f"  cost per successful task (paid-rate): "
              f"${totals['paid_rate_usd'] / successes:.4f} "
              f"({successes} successes); billed per successful task: "
              f"${totals['billed_usd'] / successes:.4f}")
    else:
        print("  cost per successful task: n/a (no successful tasks)")
    summary["cost_per_successful_task_paid_rate"] = (
        totals["paid_rate_usd"] / successes if successes else None)
    summary["cost_per_successful_task_billed"] = (
        totals["billed_usd"] / successes if successes else None)
    out = args.out or (ROOT / "evals/runs" /
                       f"llm_comparison_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nComparison written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
