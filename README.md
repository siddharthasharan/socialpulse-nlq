# Aqua Replica — Phase 1 (independent synthetic project)

**This is not Meta's tool, and it is not affiliated with Meta Platforms.**
It is an independent, clean-room portfolio project inspired by the *idea* of a
natural-language analytics tool: natural language is the interface, semantic
correctness is the product. All data is synthetic, all code in this repo is
original, and no internal code, data, dashboards, or metric definitions from
any employer have been copied into it.

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
```

Outputs: partitioned Parquet under `data/phase1/`, plus
`data/phase1/validation_report.json` and `validation_report.md`.

## Repo layout

- `scripts/generate_phase1.py` — seeded synthetic generator
- `scripts/validate_phase1.py` — 20 canonical metrics + reconciliation
- `sql/canonical_metrics.sql` — the hand-written metric queries
- `catalog/measures.yaml` — metric definitions, grains, denominators
- `catalog/experiments.yaml` — experiment registry (list setup for Phase 2)
- `docs/schema.md` — 24-table design, Phase 1 tables marked
- `docs/synthetic-assumptions.md` — planted relationships, and what is NOT encoded
- `docs/architecture-local.md` — local architecture (no cloud, no Copilot)

## Scale actually generated

Row counts are reported by the validation script after each run. Do not
quote a row count publicly until you have run the generator and read that
report; the report is the source of truth for this repo.
