"""Token meter tests. Scale-agnostic: no frozen row counts or token
totals are asserted, only the arithmetic and the honesty labels, so
these pass on 50K data and on 500K data alike. Estimates only, never
exact API usage; no live LLM call anywhere in this file or the meter."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from socialpulse_nlq.planner import plan, run_plan, token_meter, estimate_tokens

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/phase1"


def test_estimate_tokens_math():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(4) == 1
    assert estimate_tokens(5) == 2
    assert estimate_tokens(400) == 100
    print("PASS estimate_tokens_math")


def test_routed_token_meter_math():
    p = plan("List all sessions")
    assert p.get("routed") is True
    cols, rows, ms = run_plan(p, DATA)
    m = token_meter(p, DATA, rows, ms)
    assert m["available"] is True and m["routed"] is True
    assert m["saved_tokens"] == m["raw_tokens"] - m["aggregate_tokens"]
    assert abs(m["saved_pct"] - 100.0 * m["saved_tokens"] / m["raw_tokens"]) < 0.001
    assert m["raw_tokens"] > m["aggregate_tokens"] > 0
    assert m["saved_tokens"] > 0
    # Aggregate route still costs tokens, so the saving is never a
    # literal 100%, even after rounding.
    assert m["saved_pct"] < 100.0
    assert "estimates only" in m["method"].lower()
    assert "no live llm" in m["method"].lower()
    assert isinstance(m["raw_runtime_ms"], int) and m["raw_runtime_ms"] >= 0
    assert isinstance(m["aggregate_runtime_ms"], int) and m["aggregate_runtime_ms"] >= 0
    assert isinstance(m["raw_row_count"], int) and m["raw_row_count"] > 0
    print("PASS routed_token_meter_math")


def test_routed_total_matches_raw_count():
    # The meter's raw row count must equal the raw table count computed
    # independently in the same test, on whatever data is present.
    import duckdb
    p = plan("List all sessions")
    cols, rows, ms = run_plan(p, DATA)
    m = token_meter(p, DATA, rows, ms)
    con = duckdb.connect()
    con.execute(f"CREATE OR REPLACE VIEW fact_sessions AS SELECT * FROM read_parquet('{DATA}/fact_sessions.parquet')")
    raw_count = int(con.execute("SELECT COUNT(*) FROM fact_sessions").fetchone()[0])
    assert m["raw_row_count"] == raw_count
    assert rows[0][0] == raw_count
    print("PASS routed_total_matches_raw_count")


def test_non_routed_claims_no_saving():
    p = plan("What was DAU for instagram by country over the last 28 days?")
    assert not p.get("routed")
    cols, rows, ms = run_plan(p, DATA)
    m = token_meter(p, DATA, rows, ms)
    assert m["available"] is True and m["routed"] is False
    assert m["saved_tokens"] is None and m["aggregate_tokens"] is None
    assert m["executed_tokens"] > 0
    print("PASS non_routed_claims_no_saving")


def test_clarification_has_no_meter():
    p = plan("List all messages")
    assert p["type"] == "clarification"
    m = token_meter(p, DATA, [], 0)
    assert m["available"] is False
    print("PASS clarification_has_no_meter")


def test_meter_never_runs_raw_sql():
    # token_meter must not execute the implied raw SQL: the raw SQL is
    # SELECT *, and running it would return millions of rows. The
    # meter only ever runs COUNT(*) and a LIMIT 100 sample, so its
    # work stays bounded regardless of table size.
    import socialpulse_nlq.planner as planner_mod
    src = open(planner_mod.__file__).read()
    meter_src = src[src.index("def token_meter"):]
    assert "SELECT COUNT(*)" in meter_src
    assert "LIMIT 100" in meter_src
    assert 'execute(plan' not in meter_src and 'execute(p["sql"])' not in meter_src
    print("PASS meter_never_runs_raw_sql")


if __name__ == "__main__":
    for fn in (test_estimate_tokens_math, test_routed_token_meter_math,
               test_routed_total_matches_raw_count, test_non_routed_claims_no_saving,
               test_clarification_has_no_meter, test_meter_never_runs_raw_sql):
        fn()
    print("all token meter tests pass")
