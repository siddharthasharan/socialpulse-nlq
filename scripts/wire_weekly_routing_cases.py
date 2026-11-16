"""Wire the weekly-routing probes (2026-11-16) into benchmark v12
(Phase 1, weekly routing widening).

Reads the v11 files and writes:
  evals/benchmark_v12.json            (144 v11 singles, unchanged)
  evals/conversation_v12.json         (29 v11 conversations + p24, p25)
  evals/oracle_v12.json               (173 v11 entries + 2 probe entries)
  evals/benchmark_v12_manifest.json   (175 cases, 145 dev / 30 held-out)

The probes (live shell, Siddhartha, 2026-11-16):
  p24 (conversation, multi_turn_context, dev): "no i mean a chart
      for every week pull distinct users" -> weekly_series. Turn 1
      is the fresh-with-app form (app named); turn 2 is the
      after-weekly-series form (no app, the user's verbatim
      screenshot text). Both turns return the same 14-week
      Instagram WAU series (63,764 / 90,699 / 99,611 / 108,169 /
      117,025 / 126,050 / 134,298 / 139,222 / 146,926 / 147,939 /
      147,048 / 147,057 / 146,510 / 69,683), mean 120,285.79.
      Before the build, both forms clarified. Two routing layers
      had to move: "every week" is now a series word and "distinct
      user" names the same population as "active user" (chat +
      planner), and the "no i mean" repair prefix is stripped (it
      contains the substring "mean", which the series branches
      exclude -- without the strip the repair form clarifies where
      the plain question answers).
  p25 (conversation, multi_turn_context, dev): the weekly series
      for Instagram, then "WAU over last 28 days" -> weekly_last4:
      the last 4 COMPLETE weeks (Mar 2/9/16/23), 147,939 / 147,048
      / 147,057 / 146,510, mean 147,138.5, with the reply stating
      the reading ("last 28 days" = the last 4 complete weeks --
      28 days is four weeks) and the usual note that the partial
      Mar 30 week is excluded and why. Before the build, the
      follow-up clarified. Two routing layers had to move: "last 28
      days" now matches the last-4 turn (chat + planner), and the
      planner forces the full window for the last-4 metric --
      _date_range narrows "last 28 days" to Mar 4-31, which holds
      only 3 complete weeks, so build_sql would have raised.

Rules enforced here -- a failure aborts generation, nothing is written:
  1. Every oracle value is computed by executing independent SQL on the
     500K data. The planner/chat is never consulted for a value. The
     per-week values are verified THREE ways: form A is the
     grouped-COUNT(DISTINCT) shape the planner runs; form B is
     DISTINCT-pairs (a genuinely different formulation); form C
     groups the daily distinct-user rows of agg_user_daily by ISO
     week (a different data path -- pre-aggregated daily -- that
     must agree exactly). All three must agree exactly, and the
     week labels must be the 14 Mondays from 2025-12-29 to
     2026-03-30. Two definition ties: the first partial week (Dec
     29) must equal the distinct-user count for Jan 1-4 alone, and
     the last partial week (Mar 30) must equal the distinct-user
     count for Mar 30-31 alone. The last-4 values must equal the
     weekly series at indices 9-12.
  2. Data pin: generation refuses unless the data is the frozen 500K
     seed-42 set (counts + max session date 2026-03-31).
  3. Counts: 144 singles, 31 conversations, 175 oracle entries,
     175 manifest cases, 145 dev / 30 held-out. The new cases are in
     dev; held-out is unchanged (30).

Usage: PYTHONPATH=src python scripts/wire_weekly_routing_cases.py [--data data/phase1]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb

DATA_DIR_DEFAULT = ROOT / "data/phase1"
PIN_COUNTS = {"dim_users": 500000, "fact_content_creation": 1759999,
              "fact_messages": 4654610, "fact_sessions": 13614220}

WEEKS = ["2025-12-29", "2026-01-05", "2026-01-12", "2026-01-19",
         "2026-01-26", "2026-02-02", "2026-02-09", "2026-02-16",
         "2026-02-23", "2026-03-02", "2026-03-09", "2026-03-16",
         "2026-03-23", "2026-03-30"]
LAST4_WEEKS = ["2026-03-02", "2026-03-09", "2026-03-16", "2026-03-23"]

# Stored per-week values (parent-verified 2026-11-16: three independent
# DuckDB formulations agreeing exactly, plus the partial-week ties).
IG_WAU = [63764, 90699, 99611, 108169, 117025, 126050, 134298,
          139222, 146926, 147939, 147048, 147057, 146510, 69683]
IG_LAST4 = [147939, 147048, 147057, 146510]


def _sql_wau_a(app):
    return (f"SELECT CAST(DATE_TRUNC('week', CAST(start_time AS DATE)) AS DATE) AS w, "
            f"COUNT(DISTINCT user_id) AS wau FROM fact_sessions "
            f"WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' "
            f"AND app = '{app}' GROUP BY w ORDER BY w")


def _sql_wau_b(app):
    return (f"SELECT w, COUNT(*) AS wau FROM (SELECT DISTINCT user_id, "
            f"CAST(DATE_TRUNC('week', CAST(start_time AS DATE)) AS DATE) AS w FROM fact_sessions "
            f"WHERE app = '{app}' AND CAST(start_time AS DATE) BETWEEN DATE '2026-01-01' "
            f"AND DATE '2026-03-31') GROUP BY w ORDER BY w")


def _sql_wau_c(app):
    # Third formulation: the pre-aggregated daily distinct-user rows
    # of agg_user_daily, grouped by ISO week. agg_user_daily carries
    # one row per (user, day, app), so COUNT(DISTINCT user_id) per
    # week is the same population through a different data path.
    return (f"SELECT CAST(DATE_TRUNC('week', CAST(date AS DATE)) AS DATE) AS w, "
            f"COUNT(DISTINCT user_id) AS wau FROM agg_user_daily "
            f"WHERE date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' "
            f"AND app = '{app}' GROUP BY w ORDER BY w")


def _sql_last4_a(app):
    in_list = ", ".join(f"DATE '{d}'" for d in LAST4_WEEKS)
    return (f"SELECT CAST(DATE_TRUNC('week', CAST(start_time AS DATE)) AS DATE) AS w, "
            f"COUNT(DISTINCT user_id) AS wau FROM fact_sessions "
            f"WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' "
            f"AND app = '{app}' "
            f"AND CAST(DATE_TRUNC('week', CAST(start_time AS DATE)) AS DATE) IN ({in_list}) "
            f"GROUP BY w ORDER BY w")


def _sql_last4_b(app):
    in_list = ", ".join(f"DATE '{d}'" for d in LAST4_WEEKS)
    return (f"SELECT w, COUNT(*) AS wau FROM (SELECT DISTINCT user_id, "
            f"CAST(DATE_TRUNC('week', CAST(start_time AS DATE)) AS DATE) AS w FROM fact_sessions "
            f"WHERE app = '{app}' AND CAST(start_time AS DATE) BETWEEN DATE '2026-01-01' "
            f"AND DATE '2026-03-31') WHERE w IN ({in_list}) GROUP BY w ORDER BY w")


def _fail(msg):
    print(f"REFUSED: {msg}")
    raise SystemExit(2)


def _verify_series(con, sql_a, sql_b, stored, weeks, label):
    a = [(str(w), v) for w, v in con.execute(sql_a).fetchall()]
    b = [(str(w), v) for w, v in con.execute(sql_b).fetchall()]
    if a != b:
        _fail(f"{label}: form A {a} != form B {b}")
    if [w for w, _ in a] != weeks:
        _fail(f"{label}: week labels {[w for w, _ in a]} != {weeks}")
    if [v for _, v in a] != stored:
        _fail(f"{label}: values {[v for _, v in a]} != stored {stored}")
    print(f"Oracle verified: {label} -- forms A == B exactly, {len(stored)} weeks.")
    return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA_DIR_DEFAULT)
    args = ap.parse_args()

    v11_singles = json.loads((ROOT / "evals/benchmark_v11.json").read_text())
    v11_convs = json.loads((ROOT / "evals/conversation_v11.json").read_text())
    v11_oracle = json.loads((ROOT / "evals/oracle_v11.json").read_text())
    v11_manifest = json.loads((ROOT / "evals/benchmark_v11_manifest.json").read_text())

    # --- data pin + oracle verification (aborts before anything is written)
    con = duckdb.connect()
    for t in ("dim_users", "dim_content", "fact_sessions", "fact_content_creation",
              "fact_content_impressions", "fact_content_engagements",
              "fact_video_plays", "fact_messages", "agg_user_daily",
              "agg_surface_daily"):
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

    # Weekly WAU series (IG): forms A == B == C exactly, week labels,
    # partial-week ties (Jan 1-4 alone, Mar 30-31 alone).
    _verify_series(con, _sql_wau_a("instagram"), _sql_wau_b("instagram"), IG_WAU,
                   WEEKS, "weekly WAU instagram")
    c = [(str(w), v) for w, v in con.execute(_sql_wau_c("instagram")).fetchall()]
    if [v for _, v in c] != IG_WAU or [w for w, _ in c] != WEEKS:
        _fail(f"weekly WAU instagram: form C (agg_user_daily) {[v for _, v in c]} != stored {IG_WAU}")
    print("  weekly WAU instagram: form C (agg_user_daily daily rows) agrees exactly.")
    first = con.execute(
        "SELECT COUNT(DISTINCT user_id) FROM fact_sessions WHERE app = 'instagram' "
        "AND CAST(start_time AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-01-04'"
        ).fetchone()[0]
    if first != IG_WAU[0]:
        _fail(f"weekly WAU instagram: first partial week {IG_WAU[0]} != Jan 1-4 distinct users {first}")
    last = con.execute(
        "SELECT COUNT(DISTINCT user_id) FROM fact_sessions WHERE app = 'instagram' "
        "AND CAST(start_time AS DATE) BETWEEN DATE '2026-03-30' AND DATE '2026-03-31'"
        ).fetchone()[0]
    if last != IG_WAU[-1]:
        _fail(f"weekly WAU instagram: last partial week {IG_WAU[-1]} != Mar 30-31 distinct users {last}")
    print(f"  weekly WAU instagram: partial ties hold (Jan 1-4 {first:,}, Mar 30-31 {last:,}), "
          f"mean {sum(IG_WAU) / len(IG_WAU):,.2f}/week.")

    # Last-4 series (IG): forms A == B exactly, and the values equal
    # the weekly series at indices 9-12 (the last 4 complete weeks).
    _verify_series(con, _sql_last4_a("instagram"), _sql_last4_b("instagram"), IG_LAST4,
                   LAST4_WEEKS, "last-4 instagram")
    if IG_LAST4 != IG_WAU[9:13]:
        _fail(f"last-4 instagram: {IG_LAST4} != weekly series weeks 9-12 {IG_WAU[9:13]}")
    print(f"  last-4 instagram ties to the weekly series (weeks 9-12), mean {sum(IG_LAST4) / 4:,.2f}.")

    # --- build v12 files
    singles = list(v11_singles)

    p24 = {
        "checks": {"weekly_series_chart_kind": "line",
                   "weekly_series_values": IG_WAU},
        "description": ("User probe, live shell 2026-11-16, two forms: "
                        "turn 1 is the fresh-with-app form -- 'no i mean "
                        "a chart for every week pull distinct users for "
                        "Instagram' -> weekly_series (14 weeks, mean "
                        "120,286/week); turn 2 is the after-weekly-series "
                        "form -- the user's verbatim screenshot text, "
                        "'no i mean a chart for every week pull distinct "
                        "users' (no app) -> weekly_series again for "
                        "Instagram via the session app. 'Every week' is "
                        "a series word and 'distinct user' names the "
                        "same population as 'active user'; the 'no i "
                        "mean' repair prefix is stripped (it contains "
                        "the substring 'mean', which the series "
                        "branches exclude). Before the build, both "
                        "forms clarified -- even with the app named and "
                        "even right after a weekly series. One case "
                        "pins both forms because they are the same "
                        "question asked in the two states the user was "
                        "in; the checks field scores the last "
                        "weekly_series reply (the after-series form)."),
        "expected_rows": 14,
        "expected_type": "weekly_series",
        "expected_values": IG_WAU,
        "family": "multi_turn_context",
        "id": "p24",
        "min_rows": 14,
        "origin": "user probe 2026-11-16 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "weekly_series",
             "user": "no i mean a chart for every week pull distinct users for Instagram"},
            {"expect_app": "instagram", "expect_type": "weekly_series",
             "user": "no i mean a chart for every week pull distinct users"}]}
    p25 = {
        "checks": {"weekly_last4_chart_kind": "line",
                   "weekly_last4_excluded": ["2026-03-30"],
                   "weekly_last4_values": IG_LAST4},
        "description": ("User probe, live shell 2026-11-16, follow-up "
                        "form: turn 1 is the p13 probe itself -- "
                        "'What's the weekly trend for active users for "
                        "Instagram?' (weekly series, sets the series "
                        "window + stage to weekly_series); turn 2 is "
                        "'WAU over last 28 days' -> weekly_last4: the "
                        "last 4 COMPLETE weeks (Mar 2/9/16/23), "
                        "147,939 / 147,048 / 147,057 / 146,510, mean "
                        "147,138.5, with the reply stating the reading "
                        "('last 28 days' = the last 4 complete weeks -- "
                        "28 days is four weeks) and the explicit note "
                        "that the partial Mar 30 week (Mar 30-31 only) "
                        "is excluded and why -- it would read as a "
                        "crash. Before the build, the follow-up "
                        "clarified. Fresh-session 'WAU over last 28 "
                        "days' (no weekly series first) still "
                        "clarifies by design -- same gate as p17."),
        "expected_rows": 4,
        "expected_type": "weekly_last4",
        "expected_values": IG_LAST4,
        "family": "multi_turn_context",
        "id": "p25",
        "min_rows": 4,
        "origin": "user probe 2026-11-16 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "weekly_series",
             "user": "What's the weekly trend for active users for Instagram?"},
            {"expect_app": "instagram", "expect_type": "weekly_last4",
             "user": "WAU over last 28 days"}]}
    convs = v11_convs + [p24, p25]

    oracle_entries = v11_oracle["entries"] + [
        {"id": "p24", "kind": "series",
         "note": ("Probe (user probe 2026-11-16, multi_turn_context, "
                  "follow-up). The 14-week Instagram weekly WAU series "
                  "(Jan 1-Mar 31 2026) from fact_sessions, for the "
                  "'chart for every week pull distinct users' "
                  "phrasing in both its forms (fresh with the app "
                  "named, and after a weekly series with no app). "
                  "Verified three ways: the DISTINCT-pairs formulation "
                  "agrees exactly with the grouped COUNT(DISTINCT) "
                  "shape the planner runs, and the agg_user_daily "
                  "daily-rows formulation agrees exactly too. First "
                  "week (Mon Dec 29) 63,764 covers only Jan 1-4 and "
                  "last week (Mon Mar 30) 69,683 covers only Mar "
                  "30-31 -- both partial, verified against direct "
                  "distinct-user counts over those days alone. Mean "
                  "120,286/week. Structural checks run against EVERY "
                  "turn's SQL, and both turns are the Instagram "
                  "series, so the app substring is present in both."),
         "sql": _sql_wau_b("instagram"),
         "structural": [
             {"label": "Weekly series SQL must be app-scoped to Instagram (the question names the app)",
              "contains": "app = 'instagram'"},
             {"label": "Weekly series SQL must not average the weekly values into one scalar (that is wau_mean_weekly)",
              "not_contains": "AVG("},
             {"label": "Weekly series SQL must not contain a country filter the question did not ask for",
              "not_contains": "country = '"}]},
        {"id": "p25", "kind": "series",
         "note": ("Probe (user probe 2026-11-16, multi_turn_context, "
                  "follow-up). The last 4 complete weeks of the "
                  "Instagram weekly WAU series (Mar 2/9/16/23 2026) "
                  "for the 'WAU over last 28 days' phrasing after a "
                  "weekly series -- 28 days is four weeks. The "
                  "DISTINCT-pairs formulation agrees exactly with the "
                  "grouped COUNT(DISTINCT) shape the planner runs, "
                  "and the values equal the weekly series at indices "
                  "9-12. The partial Mar 30 week is excluded by "
                  "construction (the IN list names only complete "
                  "weeks). Mean 147,138.5/week. Structural checks run "
                  "against EVERY turn's SQL, and turn 1 is the full "
                  "weekly series while turn 2 is the last-4 series, "
                  "so no IN-list substring is checked here -- the "
                  "exact 4 values are pinned by check_values instead."),
         "sql": _sql_last4_b("instagram"),
         "structural": [
             {"label": "Last-4 SQL must be app-scoped to Instagram (the question names the app)",
              "contains": "app = 'instagram'"},
             {"label": "Last-4 SQL must not average the weekly values into one scalar (that is wau_mean_weekly)",
              "not_contains": "AVG("},
             {"label": "Last-4 SQL must not contain a country filter the question did not ask for",
              "not_contains": "country = '"}]},
    ]
    manifest_cases = list(v11_manifest["cases"])
    manifest_cases.append({"family": "multi_turn_context", "id": "p24",
                           "kind": "conversation", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p25",
                           "kind": "conversation", "split": "dev"})

    assert len(singles) == 144, len(singles)
    assert len(convs) == 31, len(convs)
    assert len(oracle_entries) == 175, len(oracle_entries)
    assert len(manifest_cases) == 175, len(manifest_cases)
    dev = sum(1 for c in manifest_cases if c["split"] == "dev")
    held = sum(1 for c in manifest_cases if c["split"] == "heldout")
    assert (dev, held) == (145, 30), (dev, held)
    assert len({c["id"] for c in manifest_cases}) == 175
    assert {e["id"] for e in oracle_entries} == {c["id"] for c in manifest_cases}

    (ROOT / "evals/benchmark_v12.json").write_text(json.dumps(singles, indent=1) + "\n")
    (ROOT / "evals/conversation_v12.json").write_text(json.dumps(convs, indent=1) + "\n")
    (ROOT / "evals/oracle_v12.json").write_text(
        json.dumps({"entries": oracle_entries, "version": "v12"}, indent=1) + "\n")
    heldout_ids = sorted(c["id"] for c in manifest_cases if c["split"] == "heldout")
    manifest = {
        "benchmark": "socialpulse-nlq",
        "created": "2026-11-16",
        "data": v11_manifest["data"],
        "families": v11_manifest["families"],
        "heldout_proposal": {
            "heldout": heldout_ids,
            "note": ("v12 held-out (30): unchanged from v11 -- the new probe cases (p24, p25) are "
                     "in dev. HONEST LIMITATION: the probes were run during the build "
                     "(that is how the turns were verified), so like v1-v11, v12 held-out "
                     "means 'held out from fixes', not pristine. Truly fresh held-out "
                     "cases (designed without probing) have not arrived yet. The 120/30 "
                     "target split is exceeded at 175 cases (145 dev / 30 held-out)."),
            "rationale": "Different (metric, app, window) combinations plus hard cases held back entirely."},
        "rules": v11_manifest["rules"] + [
            "Probe cases p24-p25 are labeled with their origin (user probes 2026-11-16) in the case file and in docs/benchmark.md; they pin behavior the user actually saw in the live shell, not behavior designed in advance.",
            "Probe case p24 pins both forms of the same question in one conversation (fresh with the app named, then after a weekly series with no app), because those are the two states the user was in; the checks field scores the last weekly_series reply."],
        "version": "v12",
        "cases": manifest_cases,
    }
    (ROOT / "evals/benchmark_v12_manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")
    print(f"Wrote v12: {len(singles)} singles, {len(convs)} conversations, "
          f"{len(oracle_entries)} oracle entries, {len(manifest_cases)} manifest cases "
          f"({dev} dev / {held} held-out).")


if __name__ == "__main__":
    main()
