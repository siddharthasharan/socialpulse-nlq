# Benchmark v1 — method, split, oracle

Phase 1, slice 1. The frozen benchmark is version **v1**: 39 single-shot
cases (`evals/benchmark_phase2.json`) + 10 multi-turn conversations
(`evals/conversation_phase3.json`) = **49 cases**, data pinned to 500K
users, seed 42, window 2026-01-01..2026-03-31. The freeze lives in
`evals/benchmark_v1_manifest.json`; the harness
(`src/socialpulse_nlq/eval_harness.py`, CLI `scripts/run_benchmark_v1.py`)
refuses to run if the manifest and the eval files disagree, or if the data
pin does not match the data dir.

## The five rubric items (scored separately, never collapsed)

1. **SQL syntax** — the planned SQL parses (DuckDB EXPLAIN, no execution).
2. **SQL safety** — the read-only guard accepts it, reason recorded.
3. **Execution** — the SQL (or weather report) runs without error.
4. **Result equivalence** — executed rows match ground truth (same values;
   row order only matters for ranking questions).
5. **Final-answer correctness** — the answer shown matches the expected
   answer: right metric AND right values for a query, right cards +
   watchouts for a weather report, right clarification/refusal when the
   question is unanswerable.

Each item is pass / fail / na. Rates are computed over applicable cases
only. **Task success** for a case is item 5 passing. Alongside the five,
the harness records `sql_semantic` (metric identity + structural checks):
a wrong-metric query that happens to return the right number scores
result-equivalence pass but final-answer fail and `sql_semantic` fail.
That is the right-answer-from-wrong-SQL case, and it must never pass
silently.

**No exact-SQL-string matching anywhere.** SQL text is inspected only by
structural checks declared per case in `evals/oracle_v1.json` (e.g. the
MAU case must contain the app filter) — a contains-check on the plan,
not a comparison against a gold query.

## The 8 families (primary tag per case)

| Family | v1 cases | What it catches |
|---|---|---|
| metric_scoping | 12 | Right metric, wrong population/window |
| aggregation_grain | 15 | DAU vs MAU, sum vs distinct count, time grain |
| complex_joins_breakdowns | 8 | Wrong join, wrong grain, double-counting |
| ambiguous_metric | 4 | "Active" / "doing well" with no definition |
| multi_turn_context | 7 | Follow-ups, corrections, app/topic switch |
| incorrect_assumption | 3 | Question assumes something false about the data |
| guardrail_adversarial | 0 | Must refuse or clarify, not comply |
| sql_safety_syntax | 0 | Unsafe or unparsable SQL, wrong table |

Two families have **zero coverage in v1** — that is a real gap, not an
oversight to hide: slice 2 must add adversarial and SQL-safety cases.
Results are reported by family, and the family (not the case) is the unit
of uncertainty. No naive confidence interval over all cases is computed:
templated cases from one template and turns sharing one conversation are
not independent observations.

## Dev / held-out split (proposal, awaiting approval)

- **Dev: 39 cases** — fixes are made against these.
- **Held-out: 10 cases** — b10, b15, b19, b23, b30, b36, b37, b39, c2, c8.
  Chosen to span different (metric, app, window) combinations plus hard
  cases held back entirely. The split is a proposal: edit the per-case
  `split` fields in `evals/benchmark_v1_manifest.json` to change it.

Rules, enforced by the harness (dev and held-out are reported in separate
tables, never mixed into one aggregate):

- Held-out results are reported, never used to decide a fix.
- If a held-out failure ever drives a change, that case moves to dev in
  the next benchmark version and the version number changes.
- New cases get a manifest entry (family + split) at creation time,
  before any fix is made against them.

**Honest limitation:** these 10 held-out cases were already used while
developing the planner — they are part of the existing 49-case benchmark
that drove fixes. This held-out set is "held out from future fixes", not
pristine. Truly fresh held-out cases (new metric/app/window combinations
never run against the planner) arrive with slice 2's ~101 new cases. The
120/30 target split in the design draft applies at 150 cases; at 49
cases the proposal is 39/10.

## The independent oracle (`evals/oracle_v1.json`)

For each of the 49 cases, an independent check of the stored expected
value that does **not** use the planner's SQL: hand-written DuckDB SQL
executed directly on the parquet views, re-derived from
`catalog/measures.yaml` and `sql/canonical_metrics.sql` (the documented
definitions), with app/country/platform filters written out by hand where
the case is scoped. Weather cards are recomputed directly from the raw
tables using the period definitions in `weather.py` (current = last 28
days of sessions, prior = the 28 days before; retention cohorts end at
max-date minus 7 days); the runner first asserts MAX(session date) =
2026-03-31 so the hardcoded periods fail loudly if the data changes.
Clarification/refusal cases are checked against their premise in the data
or registry (e.g. France is absent from `dim_users.country`; no
experiment assignment table exists in `data/phase1`). Registry cases read
`catalog/experiments.yaml` directly.

Limits, stated plainly: for scalar canonical metrics the oracle SQL is a
re-derivation of the same definition the planner mirrors, so it is
independent of the planner's *code path* but not of the *definition* —
agreement there is weaker evidence than for breakdowns, series, and
weather cards, where the oracle's aggregation structure differs. The
oracle never compares SQL text; agreement is always on executed values.

**Disagreements are findings.** Where the oracle disagrees with a stored
expected value, the stored value is NOT changed by any script. The
disagreement is printed by the CLI and recorded in the trace, and a human
decides which side is right.

## Running it

```bash
PYTHONPATH=src python scripts/run_benchmark_v1.py --split dev      # default
PYTHONPATH=src python scripts/run_benchmark_v1.py --split heldout  # report only
PYTHONPATH=src python scripts/run_benchmark_v1.py --split all
PYTHONPATH=src python scripts/run_benchmark_v1.py --skip-oracle     # benchmark only
```

Exit code 0 only when every case in the requested split passes task
success AND the oracle agrees on every case. Traces go to
`evals/runs/benchmark_v1_<split>_<timestamp>.json` (override with
`--trace-out`, suppress with `--no-trace`). Each trace records the
catalog entries used, plan type, SQL, guard outcome + reason, execution
time, row counts, errors, the five rubric scores, structural checks, and
per-turn records for conversations — reusing the existing working payload
(`planner.build_trace` / chat `last_working`), not a second trace format.
