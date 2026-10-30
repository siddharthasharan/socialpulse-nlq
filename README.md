# SocialPulse NLQ (independent synthetic project)

An independently developed, deployed analytics application that enables natural-language exploration of synthetic social-platform data. Built with Python, DuckDB, deterministic query planning, multi-turn conversational context, and evaluation workflows.

Project scope: 50K synthetic users, 12 tables, 90 days of activity, and 20 validated product metrics.

Independent portfolio project using synthetic data; not affiliated with Meta Platforms.

## What Phase 1 is

- 50,000 synthetic users, 12 warehouse tables, 90 days of activity
  (2026-01-01 to 2026-03-31), fully reproducible from a single seed.
- A semantic catalog (`catalog/measures.yaml`) with 20 canonical metrics,
  each with a definition, grain, denominator, and hand-written SQL.
- A validation report proving the daily aggregates reconcile to the
  underlying events, instead of just asserting that they do.
- A schema designed for the full 24-table version from day one
  (see `docs/schema.md`); Phase 1 materialises 12 of those tables.

## What Phase 1 is not

- Not a billion-row warehouse. Event tables are sampled / selectively
  materialised, and most questions are answered from daily aggregates.
  The full-scale 500K-user, 12-month version is an opt-in generator run,
  not data committed to this repo.
- Not a live LLM product. The deterministic planner and web UI arrive in
  Phase 2. Phase 1 is the data foundation that makes those later claims
  checkable.
- Not a claim about real Meta users, products, or dashboards. Metric names
  such as DAU or creator retention are standard product-analytics concepts;
  the definitions here are this project's own, documented in the catalog.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/generate_phase1.py --seed 42 --out data/phase1
python scripts/validate_phase1.py --data data/phase1
python scripts/ask.py "What was DAU by app over the last 28 days?"
python scripts/app.py   # web UI at http://localhost:8000
```

Outputs: partitioned Parquet under `data/phase1/`, plus
`data/phase1/validation_report.json` and `validation_report.md`.

## Phase 2: deterministic planner (no LLM, no model key)

Ask in plain English. The planner maps the question to a canonical metric
from `catalog/measures.yaml`, builds SQL from an allowlisted template
(never from free-form user text), checks it with the read-only SQL guard,
runs it on DuckDB, and returns the result plus a receipt: metric, grain,
filters, tables used, and runtime. If a question is ambiguous, asks for a
metric that does not exist, or uses a filter value that is not in the
synthetic data, it asks for clarification instead of inventing an answer.
Run the benchmark with `python scripts/run_benchmark.py --data data/phase1`.

## Repo layout

- `scripts/generate_phase1.py` — seeded synthetic generator
- `scripts/validate_phase1.py` — 20 canonical metrics + reconciliation
- `sql/canonical_metrics.sql` — the hand-written metric queries
- `catalog/measures.yaml` — metric definitions, grains, denominators
- `catalog/experiments.yaml` — experiment registry (descriptive in Phase 2)
- `src/socialpulse_nlq/planner.py` — deterministic planner + SQL guard
- `scripts/ask.py` — CLI for asking questions
- `scripts/app.py` + `app/index.html` — local web UI (stdlib only, no CDN)
- `evals/benchmark_phase2.json` + `scripts/run_benchmark.py` — 32-question benchmark, 32/32 on seed-42 data
- `docs/schema.md` — 24-table design, Phase 1 tables marked
- `docs/synthetic-assumptions.md` — planted relationships, and what is NOT encoded
- `docs/architecture-local.md` — local architecture (no cloud, no Copilot)
- `docs/phase2.md` — planner, guard, UI, benchmark details and limits
- `docs/interview-prep.md` — stage-by-stage interview guide

## Scale actually generated

Row counts are reported by the validation script after each run. Do not
quote a row count publicly until you have run the generator and read that
report; the report is the source of truth for this repo.
