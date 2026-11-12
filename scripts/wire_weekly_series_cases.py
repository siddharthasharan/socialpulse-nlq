"""Wire the user probes (2026-11-12) into benchmark v9
(Phase 1, weekly-series turn).

Reads the v8 files and writes:
  evals/benchmark_v9.json            (142 v8 singles + p13)
  evals/conversation_v9.json         (20 v8 conversations + p14)
  evals/oracle_v9.json               (162 v8 entries + 2 probe entries)
  evals/benchmark_v9_manifest.json   (164 cases, 134 dev / 30 held-out)

The probes (live shell, Siddhartha's screenshot 2026-11-12):
  p13 (single, aggregation_grain, dev): "What's the weekly trend
      for active users for Instagram?" Before the build, the
      question answered with the LLM-labeled clarification "I could
      not map that question to a catalog metric" -- and the model
      never ran (fallback api_error, read timeout after 120s), so
      the deterministic baseline answered. Expected: the weekly
      WAU series -- distinct users per ISO week for Instagram,
      Jan 1-Mar 31 2026, 14 weeks: 63,764 / 90,699 / 99,611 /
      108,169 / 117,025 / 126,050 / 134,298 / 139,222 / 146,926 /
      147,939 / 147,048 / 147,057 / 146,510 / 69,683. The first week
      (Mon Dec 29 2025) covers only Jan 1-4 and the last week (Mon
      Mar 30 2026) only Mar 30-31, so both are partial weeks with
      lower counts -- not a drop. The per-week values are the same
      inner query wau_mean_weekly already computes inside its AVG()
      wrapper, returned as rows instead of averaged away.
  p14 (conversation, multi_turn_context, dev): turn 1 is the p13
      probe itself (weekly series for Instagram); turn 2 is "same
      for WhatsApp" (no app named in the question, app inherited
      from the series context). The follow-up re-runs the same
      weekly series for WhatsApp: 41,660 / 59,924 / 65,795 / 71,728
      / 77,911 / 84,107 / 89,297 / 92,651 / 97,829 / 98,372 /
      98,125 / 98,063 / 97,675 / 46,177. This pins the app-switch
      path for the new turn (series window + stage), not the
      sticky-MAU path: a MAU -> bare-weekly follow-up also works
      (verified by direct probe, not a case), but the series
      follow-up is the new machinery worth pinning.

Rules enforced here -- a failure aborts generation, nothing is written:
  1. Every oracle value is computed by executing independent SQL on the
     500K data. The planner/chat is never consulted for a value. The
     per-week values are verified TWO independent ways: form A is
     the grouped-COUNT(DISTINCT) shape the planner runs; form B is
     DISTINCT-pairs (a genuinely different formulation). Both must
     agree exactly, and the week labels must be the 14 Mondays from
     2025-12-29 to 2026-03-30. Two definition ties: the first
     partial week (Dec 29) must equal the distinct-user count for
     Jan 1-4 alone, and the last partial week (Mar 30) must equal
     the distinct-user count for Mar 30-31 alone.
  2. No aggregate table carries weekly distinct users
     (agg_user_daily has daily DAU only), so unlike p02/p03 there is
     no agg-vs-raw cross-check here; the cross-check is two
     independent raw formulations plus the partial-week ties. That
     limit is stated in the oracle note.
  3. Data pin: generation refuses unless the data is the frozen 500K
     seed-42 set (counts + max session date 2026-03-31).
  4. Counts: 143 singles, 21 conversations, 164 oracle entries,
     164 manifest cases, 134 dev / 30 held-out. The new cases are in
     dev; held-out is unchanged (30).

Usage: PYTHONPATH=src python scripts/wire_weekly_series_cases.py [--data data/phase1]
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

# Stored per-week values (parent-verified 2026-11-12: two independent
# DuckDB formulations agreeing exactly, plus the partial-week ties).
IG_VALUES = [63764, 90699, 99611, 108169, 117025, 126050, 134298,
             139222, 146926, 147939, 147048, 147057, 146510, 69683]
WA_VALUES = [41660, 59924, 65795, 71728, 77911, 84107, 89297,
             92651, 97829, 98372, 98125, 98063, 97675, 46177]
FB_VALUES = [35368, 50734, 55511, 60498, 65623, 70310, 75048,
             77696, 81882, 82466, 82164, 81690, 81697, 38883]

# Form A (grouped-COUNT(DISTINCT) -- the shape the planner runs).
def _sql_a(app):
    return (f"SELECT CAST(DATE_TRUNC('week', CAST(start_time AS DATE)) AS DATE) AS w, "
            f"COUNT(DISTINCT user_id) AS wau FROM fact_sessions "
            f"WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' "
            f"AND app = '{app}' GROUP BY w ORDER BY w")

# Form B (oracle entry SQL): DISTINCT-pairs -- a genuinely different
# formulation from the planner's grouped COUNT(DISTINCT).
def _sql_b(app):
    return (f"SELECT w, COUNT(*) AS wau FROM (SELECT DISTINCT user_id, "
            f"CAST(DATE_TRUNC('week', CAST(start_time AS DATE)) AS DATE) AS w FROM fact_sessions "
            f"WHERE app = '{app}' AND CAST(start_time AS DATE) BETWEEN DATE '2026-01-01' "
            f"AND DATE '2026-03-31') GROUP BY w ORDER BY w")


def _fail(msg):
    print(f"REFUSED: {msg}")
    raise SystemExit(2)


def _verify_app(con, app, stored, label):
    """Forms A == B exactly on week labels + values; the stored list
    matches; the two partial weeks tie to direct distinct-user counts
    over their in-window days alone."""
    a = [(str(w), v) for w, v in con.execute(_sql_a(app)).fetchall()]
    b = [(str(w), v) for w, v in con.execute(_sql_b(app)).fetchall()]
    if a != b:
        _fail(f"{label}: form A {a} != form B {b}")
    if [w for w, _ in a] != WEEKS:
        _fail(f"{label}: week labels {[w for w, _ in a]} != {WEEKS}")
    if [v for _, v in a] != stored:
        _fail(f"{label}: values {[v for _, v in a]} != stored {stored}")
    first = con.execute(
        f"SELECT COUNT(DISTINCT user_id) FROM fact_sessions WHERE app = '{app}' "
        f"AND CAST(start_time AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-01-04'"
        ).fetchone()[0]
    if first != stored[0]:
        _fail(f"{label}: first partial week {stored[0]} != Jan 1-4 distinct users {first}")
    last = con.execute(
        f"SELECT COUNT(DISTINCT user_id) FROM fact_sessions WHERE app = '{app}' "
        f"AND CAST(start_time AS DATE) BETWEEN DATE '2026-03-30' AND DATE '2026-03-31'"
        ).fetchone()[0]
    if last != stored[-1]:
        _fail(f"{label}: last partial week {stored[-1]} != Mar 30-31 distinct users {last}")
    print(f"Oracle verified: {label} weekly WAU -- forms A == B exactly, "
          f"14 weeks, first {stored[0]:,} (Jan 1-4) / last {stored[-1]:,} (Mar 30-31), "
          f"mean {sum(stored) / len(stored):,.0f}/week.")
    return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA_DIR_DEFAULT)
    args = ap.parse_args()

    v8_singles = json.loads((ROOT / "evals/benchmark_v8.json").read_text())
    v8_convs = json.loads((ROOT / "evals/conversation_v8.json").read_text())
    v8_oracle = json.loads((ROOT / "evals/oracle_v8.json").read_text())
    v8_manifest = json.loads((ROOT / "evals/benchmark_v8_manifest.json").read_text())

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

    _verify_app(con, "instagram", IG_VALUES, "p13 Instagram")
    _verify_app(con, "whatsapp", WA_VALUES, "p14 WhatsApp (turn 2)")
    _verify_app(con, "facebook", FB_VALUES, "Facebook (probe, no case)")

    # --- build v9 files
    p13 = {
        "category": "probe",
        "checks": {"weekly_series_chart_kind": "line"},
        "description": ("User probe, live shell 2026-11-12: 'What's the "
                        "weekly trend for active users for Instagram?' "
                        "Before the build, the question answered with the "
                        "LLM-labeled clarification 'I could not map that "
                        "question to a catalog metric' -- and the model "
                        "never ran (fallback api_error, read timeout "
                        "after 120s), so the deterministic baseline "
                        "answered. Expected: the weekly WAU series -- "
                        "distinct users per ISO week for Instagram, "
                        "Jan 1-Mar 31 2026, 14 weeks (63,764 / 90,699 / "
                        "99,611 / 108,169 / 117,025 / 126,050 / 134,298 "
                        "/ 139,222 / 146,926 / 147,939 / 147,048 / "
                        "147,057 / 146,510 / 69,683). The first week "
                        "(Mon Dec 29) covers only Jan 1-4 and the last "
                        "week (Mon Mar 30) only Mar 30-31, so both are "
                        "partial weeks with lower counts -- not a drop. "
                        "The per-week values are the same inner query "
                        "wau_mean_weekly already computes inside its "
                        "AVG() wrapper, returned as rows instead of "
                        "averaged away. Values verified two independent "
                        "ways (grouped COUNT(DISTINCT) vs "
                        "DISTINCT-pairs), agreeing exactly. The checks "
                        "field (chart kind) is carried for the record "
                        "but is NOT harness-evaluated for a single "
                        "(check_values runs for conversations only); "
                        "the chart kind is verified by the fresh-session "
                        "turn probe, not the harness."),
        "expected_metric": "wau_by_week",
        "expected_rows": 14,
        "expected_type": "weekly_series",
        "expected_values": IG_VALUES,
        "family": "aggregation_grain",
        "id": "p13",
        "min_rows": 14,
        "origin": "user probe 2026-11-12",
        "question": "What's the weekly trend for active users for Instagram?",
    }
    singles = list(v8_singles) + [p13]

    p14 = {
        "checks": {"weekly_series_chart_kind": "line",
                   "weekly_series_values": WA_VALUES},
        "description": ("User probe, live shell 2026-11-12, follow-up "
                        "form (series context): turn 1 is the p13 probe "
                        "itself -- 'What's the weekly trend for active "
                        "users for Instagram?' (weekly series, sets the "
                        "series window + stage to weekly_series); turn 2 "
                        "is 'same for WhatsApp' (no app named). The "
                        "follow-up re-runs the same weekly series for "
                        "WhatsApp with the same window -- 41,660 / "
                        "59,924 / 65,795 / 71,728 / 77,911 / 84,107 / "
                        "89,297 / 92,651 / 97,829 / 98,372 / 98,125 / "
                        "98,063 / 97,675 / 46,177 -- not a weather "
                        "report, not the all-apps series. This pins the "
                        "app-switch path for the new turn (series window "
                        "+ stage). A MAU -> bare-weekly follow-up "
                        "(sticky MAU context) also works and was "
                        "verified by direct probe, but is not a case: "
                        "the series follow-up is the new machinery "
                        "worth pinning."),
        "expected_rows": 14,
        "expected_type": "weekly_series",
        "expected_values": WA_VALUES,
        "family": "multi_turn_context",
        "id": "p14",
        "min_rows": 14,
        "origin": "user probe 2026-11-12 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "weekly_series",
             "user": "What's the weekly trend for active users for Instagram?"},
            {"expect_app": "whatsapp", "expect_type": "weekly_series",
             "user": "same for WhatsApp"}]}
    convs = v8_convs + [p14]

    oracle_entries = v8_oracle["entries"] + [
        {"id": "p13", "kind": "series",
         "note": ("Probe (user probe 2026-11-12, aggregation_grain). "
                  "The 14-week Instagram weekly WAU series (Jan 1-Mar "
                  "31 2026) from fact_sessions; the DISTINCT-pairs "
                  "formulation agrees exactly with the grouped "
                  "COUNT(DISTINCT) shape the planner runs, so the "
                  "stored values are verified two ways. First week "
                  "(Mon Dec 29) 63,764 covers only Jan 1-4 and last "
                  "week (Mon Mar 30) 69,683 covers only Mar 30-31 -- "
                  "both partial, verified against direct distinct-user "
                  "counts over those days alone. Mean 120,286/week, "
                  "low 63,764 (Dec 29), high 147,939 (Mar 2). No "
                  "aggregate table carries weekly distinct users "
                  "(agg_user_daily has daily DAU only), so the "
                  "cross-check is two independent raw formulations "
                  "plus the partial-week ties, not agg-vs-raw. Counts "
                  "exact. Structural checks: the SQL must be "
                  "app-scoped to Instagram, must not average the "
                  "weekly values into one scalar (that is "
                  "wau_mean_weekly), and must not contain a country "
                  "filter the question did not ask for."),
         "sql": _sql_b("instagram"),
         "structural": [
             {"label": "Weekly series SQL must be app-scoped to Instagram (the question names the app)",
              "contains": "app = 'instagram'"},
             {"label": "Weekly series SQL must not average the weekly values into one scalar (that is wau_mean_weekly)",
              "not_contains": "AVG("},
             {"label": "Weekly series SQL must not contain a country filter the question did not ask for",
              "not_contains": "country = '"}]},
        {"id": "p14", "kind": "series",
         "note": ("Probe (user probe 2026-11-12, multi_turn_context, "
                  "follow-up). The 14-week WhatsApp weekly WAU series "
                  "(Jan 1-Mar 31 2026) from the 'same for WhatsApp' "
                  "follow-up after the Instagram weekly series -- the "
                  "same values the harness's check_values weekly "
                  "branch scores against the turn-2 reply's rows, "
                  "verified two ways (DISTINCT-pairs oracle SQL vs "
                  "grouped COUNT(DISTINCT) planner shape, agreeing "
                  "exactly). First week (Mon Dec 29) 41,660 covers "
                  "only Jan 1-4 and last week (Mon Mar 30) 46,177 "
                  "covers only Mar 30-31 -- both partial, verified "
                  "against direct distinct-user counts over those days "
                  "alone. Mean 79,951/week. No structural app check "
                  "here: the structural checks run against EVERY "
                  "turn's SQL, and turn 1 is Instagram while turn 2 "
                  "is WhatsApp, so no single app substring is present "
                  "in both; the turn-2 app is pinned by check_turn "
                  "(expect_app whatsapp) plus the check_values weekly "
                  "branch, which scores the turn-2 reply's rows "
                  "against the WhatsApp values."),
         "sql": _sql_b("whatsapp"),
         "structural": [
             {"label": "Weekly series SQL must not average the weekly values into one scalar (that is wau_mean_weekly)",
              "not_contains": "AVG("},
             {"label": "Weekly series SQL must not contain a country filter the question did not ask for",
              "not_contains": "country = '"}]},
    ]
    manifest_cases = list(v8_manifest["cases"])
    manifest_cases.append({"family": "aggregation_grain", "id": "p13",
                           "kind": "single", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p14",
                           "kind": "conversation", "split": "dev"})

    assert len(singles) == 143, len(singles)
    assert len(convs) == 21, len(convs)
    assert len(oracle_entries) == 164, len(oracle_entries)
    assert len(manifest_cases) == 164, len(manifest_cases)
    dev = sum(1 for c in manifest_cases if c["split"] == "dev")
    held = sum(1 for c in manifest_cases if c["split"] == "heldout")
    assert (dev, held) == (134, 30), (dev, held)
    assert len({c["id"] for c in manifest_cases}) == 164
    assert {e["id"] for e in oracle_entries} == {c["id"] for c in manifest_cases}

    (ROOT / "evals/benchmark_v9.json").write_text(json.dumps(singles, indent=1) + "\n")
    (ROOT / "evals/conversation_v9.json").write_text(json.dumps(convs, indent=1) + "\n")
    (ROOT / "evals/oracle_v9.json").write_text(
        json.dumps({"entries": oracle_entries, "version": "v9"}, indent=1) + "\n")
    heldout_ids = sorted(c["id"] for c in manifest_cases if c["split"] == "heldout")
    manifest = {
        "benchmark": "socialpulse-nlq",
        "created": "2026-11-12",
        "data": v8_manifest["data"],
        "families": v8_manifest["families"],
        "heldout_proposal": {
            "heldout": heldout_ids,
            "note": ("v9 held-out (30): unchanged from v8 -- the new probe cases (p13, p14) are "
                     "in dev. HONEST LIMITATION: the probes were run during the build "
                     "(that is how the turn was verified), so like v1-v8, v9 held-out "
                     "means 'held out from fixes', not pristine. Truly fresh held-out "
                     "cases (designed without probing) have not arrived yet. The 120/30 "
                     "target split is exceeded at 164 cases (134 dev / 30 held-out)."),
            "rationale": "Different (metric, app, window) combinations plus hard cases held back entirely."},
        "rules": v8_manifest["rules"] + [
            "Probe cases p13/p14 are labeled with their origin (user probe 2026-11-12) in the case file and in docs/benchmark.md; they pin behavior the user actually saw in the live shell, not behavior designed in advance.",
            "Probe case p14 (bare 'same for WhatsApp' follow-up after a weekly-series turn) is labeled the same way; it pins the turn-2 behavior the user authorized as the follow-up form of the p13 probe."],
        "version": "v9",
        "cases": manifest_cases,
    }
    (ROOT / "evals/benchmark_v9_manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")
    print(f"Wrote v9: {len(singles)} singles, {len(convs)} conversations, "
          f"{len(oracle_entries)} oracle entries, {len(manifest_cases)} manifest cases "
          f"({dev} dev / {held} held-out).")


if __name__ == "__main__":
    main()
