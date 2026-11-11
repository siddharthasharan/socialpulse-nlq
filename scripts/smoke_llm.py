"""Live smoke test for the LLM planner (chunk B, slice 1).

Runs 5 cases against the LIVE Gemini API — p11 (MAU + creators by
country), p08 (creator share of MAU), b01 (mean daily DAU), h20
(guardrail-override attempt, must clarify, never execute), and h30
(the ranking-window conversation, must clarify on turn 4) — through
the same harness code the benchmark runner uses, and prints the real
llm receipt per case (model, tokens, paid-rate cost, latency,
fallback).

Requires GEMINI_API_KEY in the environment. WITHOUT a key this script
prints LIVE VERIFICATION PENDING and exits 2: it never invents live
results, and a fallback run is not a smoke test. A full benchmark run
is scripts/run_benchmark_v8.py --variant llm; this script is the
5-case check that the key, the model id, and the prompt contract work
at all before spending a full run's request budget.

A case only counts as a live pass when the MODEL ran it (real tokens
in the receipt, no fallback). When the model call falls back — e.g.
HTTP 403 PERMISSION_DENIED, which means the key is valid but the
Google free-tier project/account is not allowed to generate with
gemini-3.8-flash — the case is reported BLOCKED, not failed: the
planner fell back to the deterministic baseline (labeled in the
trace), and that is a key/project restriction, not a planner failure.
Fixes are on the Google side: check the project status in AI Studio,
complete Google age verification, create a new key, or attach
billing/prepay (paid tier restores access).

Usage:
    PYTHONPATH=src python scripts/smoke_llm.py [--data data/phase1]
        [--tier free|paid] [--pace SECONDS]
Exit code: 0 all 5 pass live; 1 a case failed live; 2 no key (pending);
3 blocked (the model did not run — e.g. 403).
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from socialpulse_nlq.eval_harness import (
    load_cases, load_manifest, open_connection, score_conversation,
    validate_case_set, validate_data_pin, _run_single)

VERSION = "v8"
CASE_IDS = ["p11", "p08", "b01", "h20"]  # singles, in order
CONV_ID = "h30"


def _llm_line(llm):
    if not llm:
        return "llm: (no receipt)"
    if llm.get("fallback"):
        return (f"llm: FALLBACK {llm['fallback']} — {llm.get('fallback_reason')}")
    return (f"llm: model={llm.get('model')} prompt={llm.get('prompt_tokens')} "
            f"completion={llm.get('completion_tokens')} "
            f"paid-rate=${(llm.get('cost_usd_paid_rate') or 0):.5f} "
            f"billed=${(llm.get('billed_usd') or 0):.5f} "
            f"latency={llm.get('latency_ms')}ms "
            f"retried={llm.get('retried')}({llm.get('retry_status')})")


def _blocked_note(llm):
    """The plain-language blocked-key explanation, when the fallback
    reason is a 403 — the key is valid, the Google project/account is
    not allowed to generate. Returns None for other fallbacks."""
    reason = (llm or {}).get("fallback_reason") or ""
    if "403" in reason or "PERMISSION_DENIED" in reason:
        return ("BLOCKED KEY: the API returned HTTP 403 PERMISSION_DENIED "
                "('The caller does not have permission'). The key is valid "
                "(list-models works) — this Google free-tier project/account "
                "is not allowed to generate with gemini-3.8-flash. This is "
                "a key/project restriction, NOT a planner failure: the "
                "planner fell back to the deterministic baseline, labeled "
                "in the trace. Fixes are on the Google side: check the "
                "project status in AI Studio, complete Google age "
                "verification, create a new key, or attach billing/prepay "
                "(paid tier restores access).")
    return None


def _live_status_single(case_id, result):
    """(status, note) for one single-shot case. 'live-pass' only when
    the model ran the case; 'blocked' when it fell back (the baseline
    result is not a live pass); 'failed' when the live answer was
    wrong. h20 is the exception by design: the guardrail pre-check
    answers it without consulting the model."""
    llm = result.get("llm") or {}
    if llm.get("fallback"):
        return "blocked", _blocked_note(llm) or (
            f"model call fell back ({llm['fallback']}: "
            f"{llm.get('fallback_reason')}); not a live result")
    if not result["task_success"]:
        return "failed", result["items"]["final_answer"]["reason"]
    if case_id == "h20":
        if llm.get("not_consulted") == "override_attempt":
            return "live-pass", "pre-check answered it; model correctly not consulted"
        return "blocked", "h20 answered without the override pre-check"
    if llm.get("not_consulted"):
        return "blocked", f"model not consulted ({llm['not_consulted']})"
    if llm.get("prompt_tokens") is None:
        return "blocked", "no token receipt — the model did not run"
    return "live-pass", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data/phase1")
    ap.add_argument("--tier", choices=["free", "paid"], default="free")
    ap.add_argument("--pace", type=float, default=1.0)
    args = ap.parse_args()
    if not os.environ.get("GEMINI_API_KEY"):
        print("LIVE VERIFICATION PENDING — no GEMINI_API_KEY in the environment.")
        print("Set it (export GEMINI_API_KEY=... in the shell that runs this "
              "script; never commit it) and re-run. Nothing below is a live result.")
        return 2
    manifest = load_manifest(ROOT, version=VERSION)
    singles, convs, oracle_by_id = load_cases(ROOT, version=VERSION)
    validate_case_set(manifest, singles, convs)
    con = open_connection(args.data)
    try:
        validate_data_pin(con, args.data, manifest)
    finally:
        con.close()
    structural = {cid: e["structural"] for cid, e in oracle_by_id.items()
                  if e.get("structural")}
    llm_kwargs = {"tier": args.tier}
    single_by_id = {c["id"]: c for c in singles}
    conv_by_id = {c["id"]: c for c in convs}
    results = []
    con = open_connection(args.data)
    try:
        for i, cid in enumerate(CASE_IDS):
            if i > 0 and args.pace:
                import time
                time.sleep(args.pace)
            case = single_by_id[cid]
            r = _run_single(case, con, args.data, structural.get(cid),
                            "llm", llm_kwargs)
            status, note = _live_status_single(cid, r)
            results.append(status)
            print(f"{status.upper()} {cid} "
                  f"[{r['family']}] type={r['plan_type']} metric={r.get('metric')}")
            print(f"  {_llm_line(r.get('llm'))}")
            if note:
                print(f"  {note}")
    finally:
        con.close()
    r = score_conversation(conv_by_id[CONV_ID], args.data,
                           structural.get(CONV_ID), "llm", llm_kwargs, args.pace)
    conv_fallbacks = [t.get("llm") for t in (r.get("turns") or [])
                      if (t.get("llm") or {}).get("fallback")]
    if conv_fallbacks:
        status = "blocked"
        note = _blocked_note(conv_fallbacks[0]) or (
            f"a turn fell back ({conv_fallbacks[0].get('fallback')}); "
            "not a live result")
    elif not r["task_success"]:
        status, note = "failed", r["items"]["final_answer"]["reason"]
    else:
        # h30's turns route deterministically (weather, breakdown,
        # ranking, correction) — the llm variant must not break them,
        # and any turn that did consult the planner must not have
        # fallen back (checked above).
        status, note = "live-pass", ""
    results.append(status)
    print(f"{status.upper()} {CONV_ID} "
          f"[{r['family']}] conversation")
    for t in r.get("turns") or []:
        print(f"  turn type={t.get('type')} app={t.get('app')} "
              f"{_llm_line(t.get('llm'))}")
    if note:
        print(f"  {note}")
    n_live = sum(1 for s in results if s == "live-pass")
    print(f"\nSmoke: {n_live}/{len(results)} pass live "
          f"(tier {args.tier}); statuses: {results}")
    if "failed" in results:
        return 1
    if "blocked" in results:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
