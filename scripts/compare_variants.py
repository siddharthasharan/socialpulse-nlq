"""Paired comparison of slice-3 planner variants on the frozen v3 benchmark.

Reads the trace files produced by scripts/run_benchmark_v3.py --variant
(one per variant x split) and prints the comparison tables from
docs/ablations.md: per-family task success, accuracy / coverage /
abstention (singles only, separately, never collapsed), and the paired
flip lists between variants. No naive confidence interval is computed:
templated cases from one template and turns sharing one conversation are
not independent observations; the family is the unit of uncertainty,
and counts + flip lists are the evidence.

Usage:
    PYTHONPATH=src python scripts/compare_variants.py <trace.json>...
    PYTHONPATH=src python scripts/compare_variants.py --runs-dir evals/runs
    (auto-picks the latest trace per variant x split)

Accuracy / coverage / abstention are computed on SINGLE-SHOT cases only
(136): conversations have clarification turns by design (h30 expects
one), so a case-level abstention rate over conversations would be
meaningless. Definitions (see docs/ablations.md):
  coverage             attempted / total singles
  abstention rate      1 - coverage
  accuracy             task success among attempted singles
  appropriate abst.    abstentions on cases expecting clarification/refusal
  wrong abst. (cost)   abstentions on cases expecting an answer
  missed abstention    cases expecting clarification/refusal that were answered
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FAMILIES = ["metric_scoping", "ambiguous_metric", "multi_turn_context",
            "complex_joins_breakdowns", "incorrect_assumption",
            "guardrail_adversarial", "sql_safety_syntax", "aggregation_grain"]
ATTEMPTED_TYPES = ("query", "weather_report", "experiment_list", "experiment_info")
EXPECT_ABSTAIN = ("clarification", "refusal")
VARIANTS = ("baseline", "catalog-assisted", "validated")


def _load_expected(root):
    """{case id: expected_type} for singles, from the frozen v3 files."""
    singles = json.loads((Path(root) / "evals" / "benchmark_v3.json").read_text())
    return {c["id"]: c["expected_type"] for c in singles}


def _rates(results, expected):
    """Accuracy / coverage / abstention on single-shot cases only."""
    singles = [r for r in results if r["kind"] == "single"]
    n = len(singles)
    attempted = [r for r in singles if r["plan_type"] in ATTEMPTED_TYPES]
    abstained = [r for r in singles if r["plan_type"] not in ATTEMPTED_TYPES]
    expect_abstain = {r["id"] for r in singles
                      if expected.get(r["id"]) in EXPECT_ABSTAIN}
    appropriate = [r for r in abstained if r["id"] in expect_abstain]
    wrong_abst = [r for r in abstained if r["id"] not in expect_abstain]
    missed = [r for r in attempted if r["id"] in expect_abstain]
    acc = (sum(int(r["task_success"]) for r in attempted) / len(attempted)
           if attempted else None)
    return {
        "n_singles": n,
        "attempted": len(attempted),
        "abstained": len(abstained),
        "coverage": len(attempted) / n if n else None,
        "abstention_rate": len(abstained) / n if n else None,
        "accuracy": acc,
        "appropriate_abstentions": [r["id"] for r in appropriate],
        "wrong_abstentions": [r["id"] for r in wrong_abst],
        "missed_abstentions": [r["id"] for r in missed],
    }


def _family_table(report):
    out = {}
    for fam in FAMILIES:
        slot = report["summary"]["by_family"].get(fam)
        out[fam] = {"n": slot["n"], "task": slot["task_success"]} if slot else {"n": 0, "task": 0}
    return out


def _flips(a_results, b_results):
    """Cases whose task success differs between two variant runs (same split)."""
    amap = {r["id"]: r for r in a_results}
    bmap = {r["id"]: r for r in b_results}
    fixed, broken = [], []
    for cid in sorted(set(amap) & set(bmap)):
        a, b = amap[cid]["task_success"], bmap[cid]["task_success"]
        if a and not b:
            broken.append({"id": cid, "family": amap[cid]["family"]})
        elif b and not a:
            fixed.append({"id": cid, "family": bmap[cid]["family"]})
    return fixed, broken


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="*", type=Path)
    ap.add_argument("--runs-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    paths = list(args.traces)
    if args.runs_dir:
        for variant in VARIANTS:
            for split in ("dev", "heldout"):
                cands = sorted(
                    (args.runs_dir / p for p in
                     glob.glob(f"benchmark_v3_{variant}_{split}_*.json",
                               root_dir=str(args.runs_dir))),
                    key=lambda p: p.stat().st_mtime)
                if cands:
                    paths.append(cands[-1])
    if len(paths) != 6:
        print(f"need 6 traces (3 variants x dev/heldout), got {len(paths)}: {paths}")
        return 2
    reports = {}
    for p in paths:
        rep = json.loads(p.read_text())
        key = (rep["variant"], rep["split"])
        if key in reports:
            print(f"duplicate trace for {key}: {p}")
            return 2
        reports[key] = rep
    for variant in VARIANTS:
        for split in ("dev", "heldout"):
            if (variant, split) not in reports:
                print(f"missing trace for {variant}/{split}")
                return 2
    expected = _load_expected(ROOT)
    summary = {"variants": {}, "flips": {}}
    for split in ("dev", "heldout"):
        print(f"\n## Split: {split}")
        print(f"\n{'family':<28}" + "".join(f"{v[:12]:>14}" for v in VARIANTS))
        for fam in FAMILIES:
            cells = []
            for v in VARIANTS:
                t = _family_table(reports[(v, split)])[fam]
                cells.append(f"{t['task']:>4}/{t['n']:<4}")
            print(f"{fam:<28}" + "".join(f"{c:>14}" for c in cells))
        print(f"\n{'':<28}" + "".join(f"{'task':>14}" for _ in VARIANTS))
        for v in VARIANTS:
            s = reports[(v, split)]["summary"]["overall"]
            print(f"  {v}: {s['task_success']}/{s['n']} task success")
        for v in VARIANTS:
            rates = _rates(reports[(v, split)]["results"], expected)
            summary["variants"].setdefault(v, {})[split] = {
                "overall": reports[(v, split)]["summary"]["overall"],
                "by_family": _family_table(reports[(v, split)]),
                "rates": rates,
            }
            print(f"\n### {v} ({split}) — singles only (n={rates['n_singles']})")
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
        for a, b in (("baseline", "catalog-assisted"),
                     ("baseline", "validated"),
                     ("catalog-assisted", "validated")):
            fixed, broken = _flips(reports[(a, split)]["results"],
                                   reports[(b, split)]["results"])
            summary["flips"].setdefault(split, {})[f"{a} -> {b}"] = {
                "fixed": fixed, "broken": broken}
            print(f"\n### Flips {a} -> {b} ({split}): "
                  f"{len(fixed)} fixed, {len(broken)} broken")
            for r in fixed:
                print(f"  FIXED  {r['id']} [{r['family']}]")
            for r in broken:
                print(f"  BROKEN {r['id']} [{r['family']}]")
    out = args.out or (ROOT / "evals/runs" /
                       f"variant_comparison_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nComparison written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
