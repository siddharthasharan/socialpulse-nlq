"""Wire the watch-outs probes (2026-11-12) into benchmark v10
(Phase 1, watch-outs turn after a weekly series).

Reads the v9 files and writes:
  evals/benchmark_v10.json           (143 v9 singles, unchanged)
  evals/conversation_v10.json        (21 v9 conversations + p15 + p16)
  evals/oracle_v10.json              (164 v9 entries + 2 probe entries)
  evals/benchmark_v10_manifest.json  (166 cases, 136 dev / 30 held-out)

The probes (the second half of the 2026-11-11 weekly probe --
"can you put it in a chart for weekly users and tell me what
things to watch out"; the chart half shipped as v9):
  p15 (conversation, multi_turn_context, dev): turn 1 is the p13
      probe itself (weekly series for Instagram); turn 2 is
      "tell me what to watch out" (verbatim). Expected: the
      watch-outs turn -- the creator-supply shock as the top
      finding (weekly creators 31,289 in the week of Feb 9, then
      18,120 in the week of Feb 16, down 42.1% in one week, staying
      near 18,000 in every complete week after), the WAU plateau
      (147,939 peak in the week of Mar 2, then 147,048 / 147,057 /
      146,510 for the last three complete weeks), retention down
      (D7 19.6%, -1.8 points), DAU up (45,249, +4.0%) -- no
      forecast, partial weeks flagged.
  p16 (conversation, multi_turn_context, dev): turn 1 is the p13
      probe; turn 2 is "what drove the plateau?" (verbatim). Same
      watch-outs reply: the plateau is answered by the shock
      timing, with the explicit caveat that timing does not prove
      cause.

Rules enforced here -- a failure aborts generation, nothing is written:
  1. Every oracle value is computed by executing independent SQL on the
     500K data. The planner/chat is never consulted for a value. The
     weekly creators values are verified TWO independent ways: form A
     is the grouped-COUNT(DISTINCT) shape the turn runs; form B is
     DISTINCT-pairs (a genuinely different formulation). Both must
     agree exactly, and the week labels must be the 14 Mondays from
     2025-12-29 to 2026-03-30. Two definition ties: the first
     partial week (Dec 29) must equal the distinct-creator count
     for Jan 1-4 alone, and the last partial week (Mar 30) must
     equal the distinct-creator count for Mar 30-31 alone.
  2. The daily-means anchors (Feb 5-14 vs Feb 15-28) are verified
     against the same AVG-over-daily-counts SQL the oracle entry
     carries; the stored values are the exact computed floats.
  3. Data pin: generation refuses unless the data is the frozen 500K
     seed-42 set (counts + max session date 2026-03-31).
  4. Counts: 143 singles, 23 conversations, 166 oracle entries,
     166 manifest cases, 136 dev / 30 held-out. The new cases are in
     dev; held-out is unchanged (30).

Usage: PYTHONPATH=src python scripts/wire_watchouts_cases.py [--data data/phase1]
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

# Stored per-week creator values (parent-verified 2026-11-12: two
# independent DuckDB formulations agreeing exactly, plus the
# partial-week ties).
IG_CREATORS = [26410, 31979, 31832, 31995, 31898, 31955, 31289,
               18120, 17942, 18142, 18186, 18057, 18073, 8252]
WA_CREATORS = [17429, 21308, 21250, 21270, 21245, 21219, 20690,
               11880, 11925, 11820, 11970, 11934, 11970, 5458]


# Form A (grouped-COUNT(DISTINCT) -- the shape the turn runs).
def _sql_a(app):
    return (f"SELECT CAST(DATE_TRUNC('week', CAST(f.published_at AS DATE)) AS DATE) AS w, "
            f"COUNT(DISTINCT f.creator_id) AS n "
            f"FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
            f"WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' "
            f"AND c.app = '{app}' GROUP BY w ORDER BY w")


# Form B (oracle entry SQL): DISTINCT-pairs -- a genuinely different
# formulation from the turn's grouped COUNT(DISTINCT).
def _sql_b(app):
    return (f"SELECT w, COUNT(*) AS n FROM (SELECT DISTINCT f.creator_id, "
            f"CAST(DATE_TRUNC('week', CAST(f.published_at AS DATE)) AS DATE) AS w "
            f"FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
            f"WHERE c.app = '{app}' AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-01-01' "
            f"AND DATE '2026-03-31') GROUP BY w ORDER BY w")


def _fail(msg):
    print(f"REFUSED: {msg}")
    raise SystemExit(2)


def _verify_app(con, app, stored, label):
    """Forms A == B exactly on week labels + values; the stored list
    matches; the two partial weeks tie to direct distinct-creator
    counts over their in-window days alone."""
    a = [(str(w), v) for w, v in con.execute(_sql_a(app)).fetchall()]
    b = [(str(w), v) for w, v in con.execute(_sql_b(app)).fetchall()]
    if a != b:
        _fail(f"{label}: form A {a} != form B {b}")
    if [w for w, _ in a] != WEEKS:
        _fail(f"{label}: week labels {[w for w, _ in a]} != {WEEKS}")
    if [v for _, v in a] != stored:
        _fail(f"{label}: values {[v for _, v in a]} != stored {stored}")
    first = con.execute(
        f"SELECT COUNT(DISTINCT f.creator_id) FROM fact_content_creation f "
        f"JOIN dim_content c ON c.content_id = f.content_id WHERE c.app = '{app}' "
        f"AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-01-04'"
        ).fetchone()[0]
    if first != stored[0]:
        _fail(f"{label}: first partial week {stored[0]} != Jan 1-4 distinct creators {first}")
    last = con.execute(
        f"SELECT COUNT(DISTINCT f.creator_id) FROM fact_content_creation f "
        f"JOIN dim_content c ON c.content_id = f.content_id WHERE c.app = '{app}' "
        f"AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-30' AND DATE '2026-03-31'"
        ).fetchone()[0]
    if last != stored[-1]:
        _fail(f"{label}: last partial week {stored[-1]} != Mar 30-31 distinct creators {last}")
    print(f"Oracle verified: {label} weekly creators -- forms A == B exactly, "
          f"14 weeks, first {stored[0]:,} (Jan 1-4) / last {stored[-1]:,} (Mar 30-31), "
          f"break {stored[6]:,} -> {stored[7]:,} in the Feb 9 -> Feb 16 weeks.")
    return a


def _daily_mean(con, app, start, end):
    return con.execute(
        f"SELECT AVG(c) AS value FROM (SELECT CAST(f.published_at AS DATE) AS d, "
        f"COUNT(DISTINCT f.creator_id) AS c FROM fact_content_creation f "
        f"JOIN dim_content c ON c.content_id = f.content_id "
        f"WHERE CAST(f.published_at AS DATE) BETWEEN DATE '{start}' AND DATE '{end}' "
        f"AND c.app = '{app}' GROUP BY d)").fetchone()[0]


def _oracle_checks():
    """The oracle entry checks (kind conversation_values): one SQL
    per stored number, run directly on DuckDB by the harness."""
    return {
        "creators_before": {
            "sql": ("SELECT COUNT(DISTINCT f.creator_id) FROM fact_content_creation f "
                    "JOIN dim_content c ON c.content_id = f.content_id WHERE c.app = 'instagram' "
                    "AND CAST(DATE_TRUNC('week', CAST(f.published_at AS DATE)) AS DATE) "
                    "= DATE '2026-02-09'"),
            "tolerance": 0},
        "creators_after": {
            "sql": ("SELECT COUNT(DISTINCT f.creator_id) FROM fact_content_creation f "
                    "JOIN dim_content c ON c.content_id = f.content_id WHERE c.app = 'instagram' "
                    "AND CAST(DATE_TRUNC('week', CAST(f.published_at AS DATE)) AS DATE) "
                    "= DATE '2026-02-16'"),
            "tolerance": 0},
        "daily_creators_before_mean": {
            "sql": ("SELECT AVG(c) AS value FROM (SELECT CAST(f.published_at AS DATE) AS d, "
                    "COUNT(DISTINCT f.creator_id) AS c FROM fact_content_creation f "
                    "JOIN dim_content c ON c.content_id = f.content_id "
                    "WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-02-05' "
                    "AND DATE '2026-02-14' AND c.app = 'instagram' GROUP BY d)"),
            "tolerance": 1.0},
        "daily_creators_after_mean": {
            "sql": ("SELECT AVG(c) AS value FROM (SELECT CAST(f.published_at AS DATE) AS d, "
                    "COUNT(DISTINCT f.creator_id) AS c FROM fact_content_creation f "
                    "JOIN dim_content c ON c.content_id = f.content_id "
                    "WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-02-15' "
                    "AND DATE '2026-02-28' AND c.app = 'instagram' GROUP BY d)"),
            "tolerance": 1.0},
        "wau_peak": {
            "sql": ("SELECT MAX(wau) FROM (SELECT CAST(DATE_TRUNC('week', CAST(start_time AS DATE)) AS DATE) AS w, "
                    "COUNT(DISTINCT user_id) AS wau FROM fact_sessions "
                    "WHERE app = 'instagram' AND CAST(start_time AS DATE) BETWEEN DATE '2026-01-01' "
                    "AND DATE '2026-03-31' GROUP BY w) WHERE w BETWEEN DATE '2026-01-05' "
                    "AND DATE '2026-03-23'"),
            "tolerance": 0},
        "wau_plateau_last": {
            "sql": ("SELECT COUNT(DISTINCT user_id) FROM fact_sessions WHERE app = 'instagram' "
                    "AND CAST(DATE_TRUNC('week', CAST(start_time AS DATE)) AS DATE) "
                    "= DATE '2026-03-23'"),
            "tolerance": 0},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA_DIR_DEFAULT)
    args = ap.parse_args()

    v9_singles = json.loads((ROOT / "evals/benchmark_v9.json").read_text())
    v9_convs = json.loads((ROOT / "evals/conversation_v9.json").read_text())
    v9_oracle = json.loads((ROOT / "evals/oracle_v9.json").read_text())
    v9_manifest = json.loads((ROOT / "evals/benchmark_v9_manifest.json").read_text())

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

    _verify_app(con, "instagram", IG_CREATORS, "p15/p16 Instagram")
    _verify_app(con, "whatsapp", WA_CREATORS, "WhatsApp (probe, no case)")

    before_mean = _daily_mean(con, "instagram", "2026-02-05", "2026-02-14")
    after_mean = _daily_mean(con, "instagram", "2026-02-15", "2026-02-28")
    if abs(before_mean - 10314.2) > 0.05:
        _fail(f"daily creators before mean {before_mean} != anchor 10314.2")
    if abs(after_mean - 4666.7) > 0.05:
        _fail(f"daily creators after mean {after_mean} != anchor 4666.7")
    print(f"Oracle verified: Instagram daily creators Feb 5-14 mean {before_mean:.1f}, "
          f"Feb 15-28 mean {after_mean:.1f} -- the break lands Feb 15, sustained.")

    checks = _oracle_checks()
    for name, spec in checks.items():
        value = con.execute(spec["sql"]).fetchone()[0]
        print(f"Oracle check {name}: {value}")

    # WAU peak / plateau-last from the oracle SQL (same numbers the
    # p13 case stores for the weekly series).
    wau_peak = con.execute(checks["wau_peak"]["sql"]).fetchone()[0]
    wau_last = con.execute(checks["wau_plateau_last"]["sql"]).fetchone()[0]
    if wau_peak != 147939:
        _fail(f"WAU peak {wau_peak} != 147939")
    if wau_last != 146510:
        _fail(f"WAU plateau last {wau_last} != 146510")

    # --- build v10 files (singles unchanged from v9)
    singles = list(v9_singles)

    watchouts_values = {
        "primary_topic": "creators",
        "creators_before": 31289, "creators_before_week": "2026-02-09",
        "creators_after": 18120, "creators_after_week": "2026-02-16",
        "daily_creators_before_mean": before_mean,
        "daily_creators_after_mean": after_mean,
        "wau_peak": 147939, "wau_peak_week": "2026-03-02",
        "wau_plateau_last3": [147048, 147057, 146510],
    }
    flat_checks = {
        "creators_before": 31289,
        "creators_after": 18120,
        "daily_creators_before_mean": before_mean,
        "daily_creators_after_mean": after_mean,
        "wau_peak": 147939,
        "wau_plateau_last": 146510,
    }
    case_checks = dict(flat_checks)
    case_checks["watchouts_values"] = watchouts_values
    case_checks["watchouts_text_must_contain"] = ["creator supply", "plateau"]
    case_checks["watchouts_text_must_not_contain"] = "will "

    p15 = {
        "checks": case_checks,
        "description": ("User probe, live shell 2026-11-11, second half "
                        "(the chart half shipped as v9): turn 1 is the p13 "
                        "probe itself -- 'What's the weekly trend for "
                        "active users for Instagram?' (weekly series, sets "
                        "the series window + stage to weekly_series); turn "
                        "2 is 'tell me what to watch out' (verbatim). "
                        "Expected: the watch-outs turn -- the "
                        "creator-supply shock as the top finding (weekly "
                        "creators 31,289 in the week of Feb 9, then "
                        "18,120 in the week of Feb 16, down 42.1% in one "
                        "week, staying near 18,000 in every complete week "
                        "after; daily about 10,314 a day Feb 5-14, then "
                        "about 4,667 a day Feb 15-28; the drop lands Feb "
                        "15 and is sustained), the WAU plateau (147,939 "
                        "peak in the week of Mar 2, then 147,048 / "
                        "147,057 / 146,510 for the last three complete "
                        "weeks), retention down (D7 19.6%, -1.8 points), "
                        "DAU up (45,249, +4.0%) -- no forecast, partial "
                        "weeks flagged. The turn reuses the diagnosis "
                        "evidence (weather cards, same priority) plus "
                        "the weekly series; it sets no weather state, so "
                        "a recommendation still asks for a weather "
                        "report first."),
        "expected_rows": 14,
        "expected_type": "watchouts",
        "expected_values": IG_CREATORS,
        "family": "multi_turn_context",
        "id": "p15",
        "min_rows": 14,
        "origin": "user probe 2026-11-11 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "weekly_series",
             "user": "What's the weekly trend for active users for Instagram?"},
            {"expect_app": "instagram", "expect_type": "watchouts",
             "expect_topic": "creators",
             "user": "tell me what to watch out"}]}
    p16 = {
        "checks": case_checks,
        "description": ("User probe, live shell 2026-11-11, second half: "
                        "turn 1 is the p13 probe (weekly series for "
                        "Instagram); turn 2 is 'what drove the plateau?' "
                        "(verbatim). Expected: the same watch-outs reply "
                        "as p15 -- the plateau is answered by the shock "
                        "timing (weekly creators 31,289 -> 18,120 in the "
                        "Feb 9 -> Feb 16 weeks, WAU peak 147,939 in the "
                        "week of Mar 2, then flat 147,048 / 147,057 / "
                        "146,510), with the explicit caveat that timing "
                        "does not prove the creator drop caused the "
                        "plateau."),
        "expected_rows": 14,
        "expected_type": "watchouts",
        "expected_values": IG_CREATORS,
        "family": "multi_turn_context",
        "id": "p16",
        "min_rows": 14,
        "origin": "user probe 2026-11-11 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "weekly_series",
             "user": "What's the weekly trend for active users for Instagram?"},
            {"expect_app": "instagram", "expect_type": "watchouts",
             "expect_topic": "creators",
             "user": "what drove the plateau?"}]}
    convs = v9_convs + [p15, p16]

    oracle_note = ("Probe (user probe 2026-11-11, multi_turn_context, "
                   "follow-up). The watch-outs numbers for the Instagram "
                   "weekly series, each from an independent oracle SQL: "
                   "weekly creators in the Feb 9 week (31,289) and the "
                   "Feb 16 week (18,120) -- the shock, down 42.1% in one "
                   "week, verified two ways (grouped COUNT(DISTINCT) vs "
                   "DISTINCT-pairs, agreeing exactly on all 14 weeks); "
                   "daily creator means Feb 5-14 (10,314.2/day) vs Feb "
                   "15-28 (4,666.7/day) -- the break lands Feb 15 and is "
                   "sustained; WAU peak over complete weeks (147,939, "
                   "week of Mar 2) and the last complete week (146,510, "
                   "week of Mar 23). The harness's check_values "
                   "watchouts branch scores the reply payload against "
                   "the same stored values, plus the plateau list "
                   "[147,048, 147,057, 146,510] and the text checks "
                   "('creator supply', 'plateau' present; 'will ' "
                   "absent -- no forecast language).")
    oracle_entries = v9_oracle["entries"] + [
        {"id": "p15", "kind": "conversation_values",
         "note": oracle_note, "checks": checks},
        {"id": "p16", "kind": "conversation_values",
         "note": oracle_note, "checks": checks},
    ]
    manifest_cases = list(v9_manifest["cases"])
    manifest_cases.append({"family": "multi_turn_context", "id": "p15",
                           "kind": "conversation", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p16",
                           "kind": "conversation", "split": "dev"})

    assert len(singles) == 143, len(singles)
    assert len(convs) == 23, len(convs)
    assert len(oracle_entries) == 166, len(oracle_entries)
    assert len(manifest_cases) == 166, len(manifest_cases)
    dev = sum(1 for c in manifest_cases if c["split"] == "dev")
    held = sum(1 for c in manifest_cases if c["split"] == "heldout")
    assert (dev, held) == (136, 30), (dev, held)
    assert len({c["id"] for c in manifest_cases}) == 166
    assert {e["id"] for e in oracle_entries} == {c["id"] for c in manifest_cases}

    (ROOT / "evals/benchmark_v10.json").write_text(json.dumps(singles, indent=1) + "\n")
    (ROOT / "evals/conversation_v10.json").write_text(json.dumps(convs, indent=1) + "\n")
    (ROOT / "evals/oracle_v10.json").write_text(
        json.dumps({"entries": oracle_entries, "version": "v10"}, indent=1) + "\n")
    heldout_ids = sorted(c["id"] for c in manifest_cases if c["split"] == "heldout")
    manifest = {
        "benchmark": "socialpulse-nlq",
        "created": "2026-11-12",
        "data": v9_manifest["data"],
        "families": v9_manifest["families"],
        "heldout_proposal": {
            "heldout": heldout_ids,
            "note": ("v10 held-out (30): unchanged from v9 -- the new probe cases (p15, p16) are "
                     "in dev. HONEST LIMITATION: the probes were run during the build "
                     "(that is how the turn was verified), so like v1-v9, v10 held-out "
                     "means 'held out from fixes', not pristine. Truly fresh held-out "
                     "cases (designed without probing) have not arrived yet. The 120/30 "
                     "target split is exceeded at 166 cases (136 dev / 30 held-out)."),
            "rationale": "Different (metric, app, window) combinations plus hard cases held back entirely."},
        "rules": v9_manifest["rules"] + [
            "Probe cases p15/p16 are labeled with their origin (user probe 2026-11-11, second half) in the case file and in docs/benchmark.md; they pin behavior the user actually saw in the live shell, not behavior designed in advance.",
            "The watch-outs turn is deliberately standalone: it sets no weather state, so the recommendation / brief / drill-down turns still ask for a weather report first. A case that expects a recommendation after watch-outs would be a scope change, not a regression."],
        "version": "v10",
        "cases": manifest_cases,
    }
    (ROOT / "evals/benchmark_v10_manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")
    print(f"Wrote v10: {len(singles)} singles, {len(convs)} conversations, "
          f"{len(oracle_entries)} oracle entries, {len(manifest_cases)} manifest cases "
          f"({dev} dev / {held} held-out).")


if __name__ == "__main__":
    main()
