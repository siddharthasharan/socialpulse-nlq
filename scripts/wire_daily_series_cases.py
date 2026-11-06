"""Wire the two user probes (2026-11-06) into benchmark v4
(Phase 1, daily-series turn).

Reads the v3 files and writes:
  evals/benchmark_v4.json            (136 v3 singles + p01)
  evals/conversation_v4.json         (14 v3 conversations + p02)
  evals/oracle_v4.json               (150 v3 entries + 2 probe entries)
  evals/benchmark_v4_manifest.json   (152 cases, 122 dev / 30 held-out)

The two probes (live shell, Siddhartha's screenshots 2026-11-06):
  p01 (single, metric_scoping, dev): "Give me Instagram MAU trend for
      the last 2 weeks" -- live answered with the March MAU scalar
      210,809 + the March-only explanation. MAU is a monthly scalar;
      there is no 2-week MAU trend. Current behavior is honest and
      stays as-is; the case pins it (planner maps "trend" to mau_march).
  p02 (conversation, aggregation_grain, dev): turn 1 is p01's question
      (MAU turn, sets the session app to Instagram); turn 2 is "show
      me the DAU for month of Mar 2026 by individual day in a line
      chart". The probe names NO app, so as a first turn it would
      (correctly) clarify "which app's daily DAU?" -- the case is a
      2-turn conversation because the probe was asked after the MAU
      turn in the same shell session, so the app is inherited. Turn 2
      expects the new daily-series turn with the 31-day March series
      and a line chart.

Rules enforced here -- a failure aborts generation, nothing is written:
  1. Every oracle value is computed by executing independent SQL on the
     500K data. The planner is never consulted for a value. The p02
     series is verified TWO ways: the aggregate table (agg_user_daily)
     and the raw tables (fact_sessions JOIN dim_users on primary_app);
     the two must agree exactly, and the stored values come from the
     aggregate form.
  2. Data pin: generation refuses unless the data is the frozen 500K
     seed-42 set (counts + max session date 2026-03-31).
  3. Counts: 137 singles, 15 conversations, 152 oracle entries,
     152 manifest cases, 122 dev / 30 held-out. Both new cases in dev;
     held-out is unchanged (30).

Usage: PYTHONPATH=src python scripts/wire_daily_series_cases.py [--data data/phase1]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb

from socialpulse_nlq.eval_harness import close

DATA_DIR_DEFAULT = ROOT / "data/phase1"
PIN_COUNTS = {"dim_users": 500000, "fact_content_creation": 1759999,
              "fact_messages": 4654610, "fact_sessions": 13614220}

# p01: same hand-written SQL as b38 (app-scoped MAU, NOT build_sql).
P01_SQL = ("SELECT COUNT(DISTINCT user_id) AS value FROM fact_sessions "
           "WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' "
           "AND DATE '2026-03-31' AND app = 'instagram'")

# p02: the March Instagram daily DAU series, two independent forms.
# Aggregate form (stored values come from this one).
P02_SQL_AGG = """SELECT date, COUNT(DISTINCT user_id) AS dau FROM agg_user_daily
WHERE date BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND app = 'instagram'
GROUP BY date ORDER BY date"""
# Raw form (cross-check only): sessions joined to users on primary_app.
P02_SQL_RAW = """SELECT CAST(s.start_time AS DATE) AS d, COUNT(DISTINCT s.user_id) AS dau
FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id
WHERE CAST(s.start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'
AND u.primary_app = 'instagram' GROUP BY d ORDER BY d"""


def _fail(msg):
    print(f"REFUSED: {msg}")
    raise SystemExit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA_DIR_DEFAULT)
    args = ap.parse_args()

    v3_singles = json.loads((ROOT / "evals/benchmark_v3.json").read_text())
    v3_convs = json.loads((ROOT / "evals/conversation_v3.json").read_text())
    v3_oracle = json.loads((ROOT / "evals/oracle_v3.json").read_text())
    v3_manifest = json.loads((ROOT / "evals/benchmark_v3_manifest.json").read_text())
    oracle_v3_by_id = {e["id"]: e for e in v3_oracle["entries"]}

    # --- data pin + oracle verification (aborts before anything is written)
    con = duckdb.connect()
    for t in ("dim_users", "dim_content", "fact_sessions", "fact_content_creation",
              "fact_content_impressions", "fact_content_engagements",
              "fact_video_plays", "fact_messages", "agg_user_daily"):
        p = args.data / f"{t}.parquet"
        if not p.exists():
            _fail(f"data file missing: {p}")
        con.execute(f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM read_parquet('{p}')")
    for table, expected in PIN_COUNTS.items():
        got = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if got != expected:
            _fail(f"data pin mismatch: {table} has {got} rows, pin is {expected}")
    max_date = con.execute(
        "SELECT MAX(CAST(start_time AS DATE)) FROM fact_sessions").fetchone()[0]
    if str(max_date) != "2026-03-31":
        _fail(f"max session date {max_date}, pin is 2026-03-31")

    # p01: b38's SQL must still give 210,809.
    p01_value = con.execute(P01_SQL).fetchone()[0]
    if p01_value != 210809:
        _fail(f"p01 MAU oracle SQL: got {p01_value}, expected 210809")
    if oracle_v3_by_id["b38"]["sql"] != P01_SQL:
        _fail("p01 SQL differs from b38's oracle SQL; reuse must be exact")

    # p02: aggregate and raw forms must agree exactly, 31 March days.
    agg_rows = con.execute(P02_SQL_AGG).fetchall()
    raw_rows = con.execute(P02_SQL_RAW).fetchall()
    if len(agg_rows) != 31 or len(raw_rows) != 31:
        _fail(f"p02 series: agg {len(agg_rows)} rows, raw {len(raw_rows)} rows, expected 31 each")
    agg_vals = [r[1] for r in agg_rows]
    raw_vals = [r[1] for r in raw_rows]
    if agg_vals != raw_vals:
        _fail(f"p02 series: agg and raw forms disagree: {agg_vals} vs {raw_vals}")
    if str(agg_rows[0][0]) != "2026-03-01" or str(agg_rows[-1][0]) != "2026-03-31":
        _fail(f"p02 series: dates run {agg_rows[0][0]}..{agg_rows[-1][0]}, expected 2026-03-01..2026-03-31")
    print(f"Oracle verified: p01 MAU 210,809; p02 series 31 days, "
          f"agg == raw exactly (Mar 1 {agg_vals[0]:,}, Mar 31 {agg_vals[-1]:,}, "
          f"mean {sum(agg_vals) / len(agg_vals):,.0f}).")

    # --- build v4 files
    p01 = {"category": "probe", "expected_metric": "mau_march",
           "expected_type": "scalar", "expected_value": 210809.0,
           "family": "metric_scoping", "id": "p01",
           "origin": "user probe 2026-11-06",
           "question": "Give me Instagram MAU trend for the last 2 weeks"}
    p02 = {"checks": {"daily_series_chart_kind": "line"},
           "description": ("User probe pair, live shell 2026-11-06: turn 1 is the MAU-trend "
                           "probe (sets the session app to Instagram); turn 2 is 'show me the "
                           "DAU for month of Mar 2026 by individual day in a line chart'. The "
                           "probe names no app, so as a first turn it would correctly clarify "
                           "which app -- the 2-turn shape is the session as asked, with the app "
                           "inherited. Turn 2 expects the daily-series turn: the 31-day March "
                           "Instagram series with a line chart. Values verified two ways "
                           "(agg_user_daily and raw fact_sessions JOIN dim_users), agreeing exactly."),
           "expected_type": "series", "expected_values": agg_vals, "min_rows": 31,
           "family": "aggregation_grain", "id": "p02",
           "origin": "user probe 2026-11-06",
           "turns": [
               {"expect_app": "instagram", "expect_type": "mau",
                "user": "Give me Instagram MAU trend for the last 2 weeks"},
               {"expect_app": "instagram", "expect_type": "daily_series",
                "user": "show me the DAU for month of Mar 2026 by individual day in a line chart"}]}
    singles = v3_singles + [p01]
    convs = v3_convs + [p02]
    oracle_entries = v3_oracle["entries"] + [
        {"id": "p01", "kind": "scalar",
         "note": ("Probe (user probe 2026-11-06, metric_scoping). Same oracle SQL as b38: "
                  "the planner maps 'trend' to mau_march, and MAU is a monthly scalar -- "
                  "there is no 2-week MAU trend. The case pins the honest scalar answer "
                  "(210,809) rather than a trend the data cannot support."),
         "sql": P01_SQL,
         "structural": oracle_v3_by_id["b38"]["structural"]},
        {"id": "p02", "kind": "series",
         "note": ("Probe (user probe 2026-11-06, aggregation_grain). The 31-day March 2026 "
                  "Instagram daily DAU series from agg_user_daily; the raw-table form "
                  "(fact_sessions JOIN dim_users on primary_app) agrees exactly, so the "
                  "stored values are verified two ways. The conversation case carries the "
                  "same values as expected_values for the harness's check_values branch, "
                  "which also pins chart kind 'line'."),
         "sql": P02_SQL_AGG,
         "structural": [{
             "label": "Series SQL must not contain a country filter the question did not ask for ('in a line chart' is presentation phrasing, not India)",
             "not_contains": "country = '"}]},
    ]
    manifest_cases = list(v3_manifest["cases"])
    for c in (p01, p02):
        manifest_cases.append({
            "family": c["family"], "id": c["id"],
            "kind": "conversation" if c["id"] == "p02" else "single",
            "split": "dev"})

    assert len(singles) == 137, len(singles)
    assert len(convs) == 15, len(convs)
    assert len(oracle_entries) == 152, len(oracle_entries)
    assert len(manifest_cases) == 152, len(manifest_cases)
    dev = sum(1 for c in manifest_cases if c["split"] == "dev")
    held = sum(1 for c in manifest_cases if c["split"] == "heldout")
    assert (dev, held) == (122, 30), (dev, held)
    assert len({c["id"] for c in manifest_cases}) == 152
    assert {e["id"] for e in oracle_entries} == {c["id"] for c in manifest_cases}

    (ROOT / "evals/benchmark_v4.json").write_text(json.dumps(singles, indent=1) + "\n")
    (ROOT / "evals/conversation_v4.json").write_text(json.dumps(convs, indent=1) + "\n")
    (ROOT / "evals/oracle_v4.json").write_text(
        json.dumps({"entries": oracle_entries, "version": "v4"}, indent=1) + "\n")
    heldout_ids = sorted(c["id"] for c in manifest_cases if c["split"] == "heldout")
    manifest = {
        "benchmark": "socialpulse-nlq",
        "created": "2026-11-06",
        "data": v3_manifest["data"],
        "families": v3_manifest["families"],
        "heldout_proposal": {
            "heldout": heldout_ids,
            "note": ("v4 held-out (30): unchanged from v3 -- both new probe cases (p01, p02) "
                     "are in dev. HONEST LIMITATION: both probes were run during the build "
                     "(that is how the daily-series turn was verified), so like v1-v3, v4 "
                     "held-out means 'held out from fixes', not pristine. Truly fresh "
                     "held-out cases (designed without probing) have not arrived yet. The "
                     "120/30 target split is exceeded at 152 cases (122 dev / 30 held-out)."),
            "rationale": "Different (metric, app, window) combinations plus hard cases held back entirely."},
        "rules": v3_manifest["rules"] + [
            "Probe cases (p01-p02) are labeled with their origin (user probe 2026-11-06) in the case file and in docs/benchmark.md; they pin behavior the user actually saw in the live shell, not behavior designed in advance."],
        "version": "v4",
        "cases": manifest_cases,
    }
    (ROOT / "evals/benchmark_v4_manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")
    print(f"Wrote v4: {len(singles)} singles, {len(convs)} conversations, "
          f"{len(oracle_entries)} oracle entries, 152 manifest cases "
          f"({dev} dev / {held} held-out).")


if __name__ == "__main__":
    main()
