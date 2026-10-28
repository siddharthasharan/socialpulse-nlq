# Local architecture (Phase 1, redrawn)

No cloud warehouse, no Copilot, no model API is required to run Phase 1.

```
Natural-language question (Phase 2)
  -> semantic catalog (catalog/measures.yaml)
  -> query planner (Phase 2: route to aggregate or event table)
  -> read-only DuckDB SQL (SELECT / CTE guard)
  -> result + receipt (tables used, grain, filters, runtime)
  -> chart + narrative (Phase 2)
```

Phase 1 layers that already exist:

- Generator: `scripts/generate_phase1.py`, seeded, writes Parquet.
- Warehouse: Parquet files under `data/phase1/`, read by DuckDB.
- Catalog: `catalog/measures.yaml` (definitions) and
  `catalog/experiments.yaml` (experiment list).
- Validation: `scripts/validate_phase1.py` runs the 20 canonical queries
  in `sql/canonical_metrics.sql` and checks aggregates against events.

Phase 2 adds the planner, SQL guard, job/receipt store, and web UI on top
of exactly these files, without changing the Phase 1 tables.
