# Phase 2: deterministic planner + local web UI

## What was built

- `src/socialpulse_nlq/planner.py` — deterministic (rule-based, no LLM)
  planner, SQL builder, and read-only SQL guard.
- `scripts/ask.py` — CLI: ask a question, see the plan, SQL, and result.
- `scripts/app.py` + `app/index.html` — local web UI, standard library
  only, no CDN and no external JavaScript. Charts are drawn on a canvas
  from the returned rows.
- `evals/benchmark_phase2.json` + `scripts/run_benchmark.py` — benchmark.
- `tests/test_planner.py` — planner and guard unit tests (no data needed).
- `docs/interview-prep.md` — interview guide, updated for Phase 2.

## How the planner works

1. Detect a metric from `catalog/measures.yaml` by keyword rules,
   specific phrases before generic ones (for example, "two-way" before
   any generic messaging metric, WAU/MAU before DAU).
2. Extract only allowlisted filters: date range, app, country, platform,
   surface, acquisition source. Country names map to codes (India -> IN);
   a country not in the synthetic data (for example France) is a
   clarification, not a guessed filter.
3. Fill a pre-written SQL template for that metric. User text is never
   interpolated into SQL except through those allowlisted values.
4. Run the SQL guard: SELECT/WITH only, no write or control statements,
   single statement only, only allowlisted tables (CTE names excluded).
5. Run on DuckDB and return a receipt: metric, grain, filters, breakdown,
   tables, runtime, row count, and the SQL itself.

Ambiguous, invalid, unsafe, and un-evaluable experiment questions return
a clarification and no SQL. That is intentional: abstention is a feature.

## Supported breakdowns (Phase 2)

- DAU: by app, country, platform (daily series per group)
- Sessions per DAU: by platform, app, country
- Daily creators: by app (daily series per app)
- Engagement rate: by surface, app
- Messaging DAU: by app, country, platform (daily series per group)
- D7 retention: by acquisition source (and single-source filters)

Asking for any other breakdown returns a clarification explaining it is
not supported in Phase 2, rather than silently dropping the filter.

## Experiment handling in Phase 2

The registry in `catalog/experiments.yaml` is descriptive only.
The UI lists experiments and the planner can report an experiment's
primary metric and guardrails, but it will not evaluate a lift:
experiment assignment tables arrive in Phase 3, and a causal claim
without assignments would be invented evidence.

## Benchmark result (verified)

Run: `python scripts/run_benchmark.py --data data/phase1`

Result on Phase 1 seed-42 data: **32/32 pass**.

Breakdown: 8 simple metrics, 8 joins/segments, 5 cohorts, 3 experiments,
6 diagnostics, 2 ambiguous/invalid. The target in the original plan was
30; the benchmark has 32 because Reels completion and watch time were
added as diagnostics. Gold scalar values come from
`scripts/validate_phase1.py` (canonical queries on the same data).

Three bugs found by the first benchmark run, all fixed before release:
guard flagging CTE names as tables; "mean daily DAU" read as a series;
a paid-cohort retention question mapped to the wrong metric.

## Limitations, stated plainly

- The planner only understands the 20 catalog metrics and the
  combinations listed above. It is not a general text-to-SQL system.
- It does not use an LLM, so phrasing outside its keyword rules will
  clarify rather than answer. That is a deliberate tradeoff for
  reproducibility and safety in a portfolio project.
- The web UI is local only. It does not deploy to GitHub Pages as a live
  backend; Pages can host docs and sample outputs, not a DuckDB server.
