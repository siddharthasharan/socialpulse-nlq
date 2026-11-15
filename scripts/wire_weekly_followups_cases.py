"""Wire the weekly follow-up probes (2026-11-13) into benchmark v11
(Phase 1, weekly follow-ups).

Reads the v10 files and writes:
  evals/benchmark_v11.json            (143 v10 singles + p19)
  evals/conversation_v11.json         (23 v10 conversations + p17, p18, p20, p21, p22, p23)
  evals/oracle_v11.json               (166 v10 entries + 7 probe entries)
  evals/benchmark_v11_manifest.json   (173 cases, 143 dev / 30 held-out)

The probes (live shell, Siddhartha, 2026-11-13):
  p17 (conversation, multi_turn_context, dev): the weekly series
      for Instagram, then "what about the last 4 weeks only?" ->
      weekly_last4: the last 4 COMPLETE weeks (Mar 2/9/16/23),
      147,939 / 147,048 / 147,057 / 146,510, mean 147,138.5, with
      the explicit note that the partial Mar 30 week (Mar 30-31
      only) is excluded and why -- it would read as a crash. "Last
      4 weeks" means the last 4 complete weeks, NOT the literal
      last 4 rows. Before the build, the follow-up clarified.
  p18 (conversation, multi_turn_context, dev): the weekly series
      for Instagram, then the last-4 turn, then "same for WhatsApp"
      -> weekly_last4 for WhatsApp: 98,372 / 98,125 / 98,063 /
      97,675, mean 98,058.75. Pins the app-switch path for the new
      turn (stage weekly_last4 re-runs last-4, not the full series).
  p19 (single, metric_scoping, dev): "compare weekly Instagram vs
      WhatsApp" -> weekly_compare: both series side by side with
      the comparison stats -- both peak in the week of Mar 2
      (147,939 / 98,372) and plateau together (IG -0.97% Mar 2->23,
      WA -0.71%); growth first complete week -> peak near-identical
      (IG +63.1%, 90,699 -> 147,939; WA +64.2%, 59,924 -> 98,372);
      Instagram runs ~1.50x WhatsApp in the last 4 weeks
      (147,138.5 / 98,058.75). One combined SQL (FULL OUTER JOIN
      of the two weekly-series subqueries); two-series line chart.
  p20 (conversation, multi_turn_context, dev): the weekly series
      for Instagram, then "compare with WhatsApp" -> weekly_compare
      with the same numbers. Pins the follow-up form of the compare
      turn (one app named, series app supplies the other).
  p21 (conversation, multi_turn_context, dev): the weekly series
      for Instagram, then "can you give me a similar trend for
      creators on Instagram?" -> creators_by_week: per-week
      DISTINCT creators, 14 weeks: 26,410 / 31,979 / 31,832 /
      31,995 / 31,898 / 31,955 / 31,289 / 18,120 / 17,942 / 18,142
      / 18,186 / 18,057 / 18,073 / 8,252, mean 23,866.43. His live
      text had a typo ("simialr"); the case uses the corrected
      spelling. Before the build, the follow-up clarified -- the
      weekly-series turn covers active users only, and the
      watch-outs turn computes weekly creators as diagnosis
      evidence but no turn served them as a series.
  p22 (conversation, multi_turn_context, dev): the creators series
      for Instagram, then "same for WhatsApp" -> creators_by_week
      for WhatsApp: 17,429 / 21,308 / 21,250 / 21,270 / 21,245 /
      21,219 / 20,690 / 11,880 / 11,925 / 11,820 / 11,970 / 11,934
      / 11,970 / 5,458, mean 15,812.0. Pins the app-switch path for
      the creators turn. Same shock, same weeks: WhatsApp 20,690
      (Feb 9) -> 11,880 (Feb 16), -42.6%.
  p23 (conversation, multi_turn_context, dev): "daily creators for
      Instagram?" -> the daily-creators scalar (type query, metric
      daily_creators_mean, mean 7,493.02 distinct creators/day),
      then "can you give me a weekly trend for this from
      2026-01-01 to 2026-03-31" -> creators_by_week for Instagram
      (the full 14-week series; the stated dates are the data
      window, not a sub-range). "This" resolves against the last
      answered metric (self.last_metric). Before the build, turn 1
      was answered in the live shell only by the LLM fallback turn
      (the model never ran; the deterministic baseline plan
      answered), and turn 2 clarified.

Rules enforced here -- a failure aborts generation, nothing is written:
  1. Every oracle value is computed by executing independent SQL on the
     500K data. The planner/chat is never consulted for a value. The
     per-week values are verified TWO independent ways: form A is
     the grouped-COUNT(DISTINCT) shape the planner runs; form B is
     DISTINCT-pairs (a genuinely different formulation). Both must
     agree exactly, and the week labels must be the 14 Mondays from
     2025-12-29 to 2026-03-30. Two definition ties: the first
     partial week (Dec 29) must equal the distinct-creator count for
     Jan 1-4 alone, and the last partial week (Mar 30) must equal
     the distinct-creator count for Mar 30-31 alone. The IG creators
     series must also match the v10 p15 expected_values exactly (a
     third-path cross-check: the watch-outs oracle derived the same
     series). The last-4 values must equal the v9 series values at
     indices 9-12. The daily-creators mean is verified two ways
     (planner AVG-wrapper SQL vs AVG of daily distinct counts).
     The compare stats are recomputed from the two verified series.
  2. No aggregate table carries weekly distinct users or weekly
     creators, so unlike p02/p03 there is no agg-vs-raw cross-check
     here; the cross-check is two independent raw formulations plus
     the partial-week ties (and the p15 cross-check for IG
     creators). That limit is stated in the oracle notes.
  3. Data pin: generation refuses unless the data is the frozen 500K
     seed-42 set (counts + max session date 2026-03-31).
  4. Counts: 144 singles, 29 conversations, 173 oracle entries,
     173 manifest cases, 143 dev / 30 held-out. The new cases are in
     dev; held-out is unchanged (30).

Usage: PYTHONPATH=src python scripts/wire_weekly_followups_cases.py [--data data/phase1]
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

# Stored per-week values (parent-verified 2026-11-13: two independent
# DuckDB formulations agreeing exactly, plus the partial-week ties).
IG_WAU = [63764, 90699, 99611, 108169, 117025, 126050, 134298,
          139222, 146926, 147939, 147048, 147057, 146510, 69683]
WA_WAU = [41660, 59924, 65795, 71728, 77911, 84107, 89297,
          92651, 97829, 98372, 98125, 98063, 97675, 46177]
FB_WAU = [35368, 50734, 55511, 60498, 65623, 70310, 75048,
          77696, 81882, 82466, 82164, 81690, 81697, 38883]
IG_LAST4 = [147939, 147048, 147057, 146510]
WA_LAST4 = [98372, 98125, 98063, 97675]
IG_CREATORS = [26410, 31979, 31832, 31995, 31898, 31955, 31289,
               18120, 17942, 18142, 18186, 18057, 18073, 8252]
WA_CREATORS = [17429, 21308, 21250, 21270, 21245, 21219, 20690,
               11880, 11925, 11820, 11970, 11934, 11970, 5458]
FB_CREATORS = [14606, 17744, 17744, 17655, 17767, 17796, 17382,
               10020, 9965, 10021, 10017, 10002, 10088, 4600]
DAILY_CREATORS_IG = 7493.02

# Compare stats, recomputed from IG_WAU / WA_WAU (verified below):
# peaks Mar 2 (147,939 / 98,372); growth first complete week -> peak
# IG +63.11%, WA +64.16%; Mar 2 -> Mar 23 IG -0.97%, WA -0.71%;
# last-4 means 147,138.5 / 98,058.75; ratio 1.50x.
COMPARE_STATS = {"change_a_pct": -0.97, "change_b_pct": -0.71,
                 "growth_a_pct": 63.11, "growth_b_pct": 64.16,
                 "mean4_a": 147138.5, "mean4_b": 98058.75,
                 "peak_a": 147939, "peak_a_week": "2026-03-02",
                 "peak_b": 98372, "peak_b_week": "2026-03-02",
                 "ratio": 1.50}


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


def _sql_creators_a(app):
    return (f"SELECT CAST(DATE_TRUNC('week', CAST(f.published_at AS DATE)) AS DATE) AS w, "
            f"COUNT(DISTINCT f.creator_id) AS creators FROM fact_content_creation f "
            f"JOIN dim_content c ON c.content_id = f.content_id "
            f"WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' "
            f"AND c.app = '{app}' GROUP BY w ORDER BY w")


def _sql_creators_b(app):
    return (f"SELECT w, COUNT(*) AS creators FROM (SELECT DISTINCT f.creator_id AS cid, "
            f"CAST(DATE_TRUNC('week', CAST(f.published_at AS DATE)) AS DATE) AS w "
            f"FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
            f"WHERE c.app = '{app}' AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-01-01' "
            f"AND DATE '2026-03-31') GROUP BY w ORDER BY w")


def _sql_daily_creators_a(app):
    return (f"SELECT AVG(creators) AS value FROM (SELECT CAST(k.published_at AS DATE) AS date, "
            f"COUNT(DISTINCT k.creator_id) AS creators FROM fact_content_creation k "
            f"JOIN dim_content c ON c.content_id = k.content_id "
            f"WHERE CAST(k.published_at AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' "
            f"AND c.app = '{app}' GROUP BY date)")


def _sql_daily_creators_b(app):
    return (f"SELECT AVG(d) FROM (SELECT CAST(f.published_at AS DATE) AS d0, "
            f"COUNT(DISTINCT f.creator_id) AS d FROM fact_content_creation f "
            f"JOIN dim_content c ON c.content_id = f.content_id "
            f"WHERE c.app = '{app}' AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-01-01' "
            f"AND DATE '2026-03-31' GROUP BY d0)")


def _sql_compare_a():
    def _sub(app):
        return (f"(SELECT CAST(DATE_TRUNC('week', CAST(start_time AS DATE)) AS DATE) AS w, "
                f"COUNT(DISTINCT user_id) AS wau FROM fact_sessions "
                f"WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' "
                f"AND app = '{app}' GROUP BY w)")
    return (f"SELECT COALESCE(a.w, b.w) AS w, a.wau AS instagram, b.wau AS whatsapp "
            f"FROM {_sub('instagram')} a FULL OUTER JOIN {_sub('whatsapp')} b ON a.w = b.w "
            f"ORDER BY w")


def _sql_compare_b():
    def _sub(app):
        return (f"(SELECT w, COUNT(*) AS wau FROM (SELECT DISTINCT user_id, "
                f"CAST(DATE_TRUNC('week', CAST(start_time AS DATE)) AS DATE) AS w FROM fact_sessions "
                f"WHERE app = '{app}' AND CAST(start_time AS DATE) BETWEEN DATE '2026-01-01' "
                f"AND DATE '2026-03-31') GROUP BY w)")
    return (f"SELECT COALESCE(a.w, b.w) AS w, a.wau AS instagram, b.wau AS whatsapp "
            f"FROM {_sub('instagram')} a FULL OUTER JOIN {_sub('whatsapp')} b ON a.w = b.w "
            f"ORDER BY w")


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

    v10_singles = json.loads((ROOT / "evals/benchmark_v10.json").read_text())
    v10_convs = json.loads((ROOT / "evals/conversation_v10.json").read_text())
    v10_oracle = json.loads((ROOT / "evals/oracle_v10.json").read_text())
    v10_manifest = json.loads((ROOT / "evals/benchmark_v10_manifest.json").read_text())

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

    # Last-4 series: form A (planner shape, IN list) == form B
    # (DISTINCT-pairs + IN list), and the values equal the v9 series
    # at indices 9-12 (the last 4 complete weeks).
    for app, stored, full in (("instagram", IG_LAST4, IG_WAU),
                              ("whatsapp", WA_LAST4, WA_WAU)):
        _verify_series(con, _sql_last4_a(app), _sql_last4_b(app), stored,
                       LAST4_WEEKS, f"last-4 {app}")
        if stored != full[9:13]:
            _fail(f"last-4 {app}: {stored} != v9 series weeks 9-12 {full[9:13]}")
        print(f"  last-4 {app} ties to the v9 series (weeks 9-12), mean {sum(stored) / 4:,.2f}.")

    # Creators series: forms A == B exactly, week labels, partial
    # ties, and the IG series must match the v10 p15
    # expected_values exactly (third-path cross-check).
    p15 = [c for c in v10_convs if c["id"] == "p15"][0]
    for app, stored in (("instagram", IG_CREATORS), ("whatsapp", WA_CREATORS),
                        ("facebook", FB_CREATORS)):
        _verify_series(con, _sql_creators_a(app), _sql_creators_b(app), stored,
                       WEEKS, f"creators {app}")
        first = con.execute(
            f"SELECT COUNT(DISTINCT f.creator_id) FROM fact_content_creation f "
            f"JOIN dim_content c ON c.content_id = f.content_id WHERE c.app = '{app}' "
            f"AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-01-04'"
            ).fetchone()[0]
        if first != stored[0]:
            _fail(f"creators {app}: first partial week {stored[0]} != Jan 1-4 distinct creators {first}")
        last = con.execute(
            f"SELECT COUNT(DISTINCT f.creator_id) FROM fact_content_creation f "
            f"JOIN dim_content c ON c.content_id = f.content_id WHERE c.app = '{app}' "
            f"AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-30' AND DATE '2026-03-31'"
            ).fetchone()[0]
        if last != stored[-1]:
            _fail(f"creators {app}: last partial week {stored[-1]} != Mar 30-31 distinct creators {last}")
        print(f"  creators {app}: partial ties hold (Jan 1-4 {first:,}, Mar 30-31 {last:,}), "
              f"mean {sum(stored) / len(stored):,.2f}/week.")
    if IG_CREATORS != p15["expected_values"]:
        _fail(f"creators instagram: {IG_CREATORS} != v10 p15 expected_values {p15['expected_values']}")
    print("  creators instagram matches v10 p15 expected_values exactly (third-path cross-check).")

    # Daily creators (IG): planner AVG-wrapper SQL == AVG of daily
    # distinct counts, and the mean matches the stored value.
    va = con.execute(_sql_daily_creators_a("instagram")).fetchone()[0]
    vb = con.execute(_sql_daily_creators_b("instagram")).fetchone()[0]
    if va != vb:
        _fail(f"daily creators instagram: form A {va} != form B {vb}")
    if abs(va - DAILY_CREATORS_IG) > 0.01:
        _fail(f"daily creators instagram: {va} != stored {DAILY_CREATORS_IG}")
    print(f"Oracle verified: daily creators instagram -- forms A == B exactly, mean {va:,.2f}/day.")

    # Compare: combined form A == form B exactly on all 14 rows x 3
    # cols, and the stats recompute from the verified series.
    ca = con.execute(_sql_compare_a()).fetchall()
    cb = con.execute(_sql_compare_b()).fetchall()
    if ca != cb:
        _fail(f"compare: form A != form B")
    if len(ca) != 14 or [str(r[0]) for r in ca] != WEEKS:
        _fail(f"compare: {len(ca)} rows, weeks {[str(r[0]) for r in ca]} != {WEEKS}")
    if [r[1] for r in ca] != IG_WAU or [r[2] for r in ca] != WA_WAU:
        _fail("compare: series columns != the verified IG/WA series")
    ig4, wa4 = IG_WAU[9:13], WA_WAU[9:13]
    stats = {
        "change_a_pct": round((ig4[-1] - max(ig4)) / max(ig4) * 100, 2),
        "change_b_pct": round((wa4[-1] - max(wa4)) / max(wa4) * 100, 2),
        "growth_a_pct": round((max(IG_WAU[1:13]) - IG_WAU[1]) / IG_WAU[1] * 100, 2),
        "growth_b_pct": round((max(WA_WAU[1:13]) - WA_WAU[1]) / WA_WAU[1] * 100, 2),
        "mean4_a": sum(ig4) / 4, "mean4_b": sum(wa4) / 4,
        "peak_a": max(IG_WAU[1:13]), "peak_a_week": WEEKS[1 + IG_WAU[1:13].index(max(IG_WAU[1:13]))],
        "peak_b": max(WA_WAU[1:13]), "peak_b_week": WEEKS[1 + WA_WAU[1:13].index(max(WA_WAU[1:13]))],
        "ratio": round((sum(ig4) / 4) / (sum(wa4) / 4), 2),
    }
    if stats != COMPARE_STATS:
        _fail(f"compare stats: recomputed {stats} != stored {COMPARE_STATS}")
    print(f"Oracle verified: compare -- forms A == B exactly, 14 weeks, "
          f"peaks {stats['peak_a']:,} / {stats['peak_b']:,} (Mar 2), ratio {stats['ratio']:.2f}x.")

    # --- build v11 files
    p19 = {
        "category": "probe",
        "checks": {"weekly_compare_chart_kind": "line"},
        "description": ("User probe, live shell 2026-11-13: 'compare "
                        "weekly Instagram vs WhatsApp' (single-shot, "
                        "both apps named). Before the build, the "
                        "question clarified -- no compare turn "
                        "existed. Expected: the weekly_compare turn "
                        "-- both series side by side with the "
                        "comparison stats: both peak in the week of "
                        "Mar 2 (147,939 / 98,372) and plateau "
                        "together (IG -0.97% Mar 2->23, WA -0.71%); "
                        "growth first complete week -> peak "
                        "near-identical (IG +63.1%, 90,699 -> "
                        "147,939; WA +64.2%, 59,924 -> 98,372); "
                        "Instagram runs ~1.50x WhatsApp in the last "
                        "4 weeks (147,138.5 / 98,058.75). One "
                        "combined SQL (FULL OUTER JOIN of the two "
                        "weekly-series subqueries); two-series line "
                        "chart. Values verified two independent ways "
                        "(grouped COUNT(DISTINCT) vs DISTINCT-pairs "
                        "subqueries), agreeing exactly. The checks "
                        "field (chart kind) is carried for the record "
                        "but is NOT harness-evaluated for a single "
                        "(check_values runs for conversations only); "
                        "the chart kind is verified by the "
                        "fresh-session turn probe, not the harness."),
        "expected_metric": "weekly_compare",
        "expected_rows": 14,
        "expected_type": "weekly_compare",
        "expected_values": {"instagram": IG_WAU, "whatsapp": WA_WAU},
        "family": "metric_scoping",
        "id": "p19",
        "min_rows": 14,
        "origin": "user probe 2026-11-13",
        "question": "compare weekly Instagram vs WhatsApp",
    }
    singles = list(v10_singles) + [p19]

    p17 = {
        "checks": {"weekly_last4_chart_kind": "line",
                   "weekly_last4_excluded": ["2026-03-30"],
                   "weekly_last4_values": IG_LAST4},
        "description": ("User probe, live shell 2026-11-13, "
                        "follow-up form: turn 1 is the p13 probe "
                        "itself -- 'What's the weekly trend for "
                        "active users for Instagram?' (weekly series, "
                        "sets the series window + stage to "
                        "weekly_series); turn 2 is 'what about the "
                        "last 4 weeks only?' -> weekly_last4: the "
                        "last 4 COMPLETE weeks (Mar 2/9/16/23), "
                        "147,939 / 147,048 / 147,057 / 146,510, mean "
                        "147,138.5, with the explicit note that the "
                        "partial Mar 30 week (Mar 30-31 only) is "
                        "excluded and why -- it would read as a "
                        "crash. 'Last 4 weeks' means the last 4 "
                        "complete weeks, NOT the literal last 4 rows. "
                        "Before the build, the follow-up clarified."),
        "expected_rows": 4,
        "expected_type": "weekly_last4",
        "expected_values": IG_LAST4,
        "family": "multi_turn_context",
        "id": "p17",
        "min_rows": 4,
        "origin": "user probe 2026-11-13 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "weekly_series",
             "user": "What's the weekly trend for active users for Instagram?"},
            {"expect_app": "instagram", "expect_type": "weekly_last4",
             "user": "what about the last 4 weeks only?"}]}
    p18 = {
        "checks": {"weekly_last4_chart_kind": "line",
                   "weekly_last4_excluded": ["2026-03-30"],
                   "weekly_last4_values": WA_LAST4},
        "description": ("User probe, live shell 2026-11-13, "
                        "follow-up form: turn 1 is the p13 probe "
                        "(weekly series for Instagram); turn 2 is "
                        "the last-4 turn (p17); turn 3 is 'same for "
                        "WhatsApp' -> weekly_last4 for WhatsApp: "
                        "98,372 / 98,125 / 98,063 / 97,675, mean "
                        "98,058.75. Pins the app-switch path for the "
                        "new turn (stage weekly_last4 re-runs last-4, "
                        "not the full series)."),
        "expected_rows": 4,
        "expected_type": "weekly_last4",
        "expected_values": WA_LAST4,
        "family": "multi_turn_context",
        "id": "p18",
        "min_rows": 4,
        "origin": "user probe 2026-11-13 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "weekly_series",
             "user": "What's the weekly trend for active users for Instagram?"},
            {"expect_app": "instagram", "expect_type": "weekly_last4",
             "user": "what about the last 4 weeks only?"},
            {"expect_app": "whatsapp", "expect_type": "weekly_last4",
             "user": "same for WhatsApp"}]}
    p20 = {
        "checks": {"weekly_compare_chart_kind": "line",
                   "weekly_compare_stats": COMPARE_STATS,
                   "weekly_compare_values": {"instagram": IG_WAU, "whatsapp": WA_WAU}},
        "description": ("User probe, live shell 2026-11-13, "
                        "follow-up form: turn 1 is the p13 probe "
                        "(weekly series for Instagram); turn 2 is "
                        "'compare with WhatsApp' -> weekly_compare "
                        "with the same numbers as p19 (both peak Mar "
                        "2, plateau together, growth +63.1% / +64.2%, "
                        "ratio ~1.50x). Pins the follow-up form of "
                        "the compare turn (one app named, the series "
                        "app supplies the other)."),
        "expected_rows": 14,
        "expected_type": "weekly_compare",
        "expected_values": {"instagram": IG_WAU, "whatsapp": WA_WAU},
        "family": "multi_turn_context",
        "id": "p20",
        "min_rows": 14,
        "origin": "user probe 2026-11-13 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "weekly_series",
             "user": "What's the weekly trend for active users for Instagram?"},
            {"expect_app": "instagram", "expect_type": "weekly_compare",
             "user": "compare with WhatsApp"}]}
    p21 = {
        "checks": {"creators_series_chart_kind": "line",
                   "creators_series_values": IG_CREATORS},
        "description": ("User probe, live shell 2026-11-13, "
                        "follow-up form: turn 1 is the p13 probe "
                        "(weekly series for Instagram); turn 2 is "
                        "'can you give me a similar trend for "
                        "creators on Instagram?' -> creators_series: "
                        "per-week DISTINCT creators, 14 weeks "
                        "(26,410 / 31,979 / 31,832 / 31,995 / 31,898 "
                        "/ 31,955 / 31,289 / 18,120 / 17,942 / "
                        "18,142 / 18,186 / 18,057 / 18,073 / 8,252), "
                        "mean 23,866.43. His live text had a typo "
                        "('simialr'); the case uses the corrected "
                        "spelling. Before the build, the follow-up "
                        "clarified -- the weekly-series turn covers "
                        "active users only, and the watch-outs turn "
                        "computes weekly creators as diagnosis "
                        "evidence but no turn served them as a "
                        "series."),
        "expected_rows": 14,
        "expected_type": "creators_series",
        "expected_values": IG_CREATORS,
        "family": "multi_turn_context",
        "id": "p21",
        "min_rows": 14,
        "origin": "user probe 2026-11-13 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "weekly_series",
             "user": "What's the weekly trend for active users for Instagram?"},
            {"expect_app": "instagram", "expect_type": "creators_series",
             "user": "can you give me a similar trend for creators on Instagram?"}]}
    p22 = {
        "checks": {"creators_series_chart_kind": "line",
                   "creators_series_values": WA_CREATORS},
        "description": ("User probe, live shell 2026-11-13, "
                        "follow-up form: turn 1 is the creators "
                        "series for Instagram (p21 turn 2); turn 2 "
                        "is 'same for WhatsApp' -> creators_series "
                        "for WhatsApp: 17,429 / 21,308 / 21,250 / "
                        "21,270 / 21,245 / 21,219 / 20,690 / 11,880 "
                        "/ 11,925 / 11,820 / 11,970 / 11,934 / "
                        "11,970 / 5,458, mean 15,812.0. Pins the "
                        "app-switch path for the creators turn. Same "
                        "shock, same weeks: WhatsApp 20,690 (Feb 9) "
                        "-> 11,880 (Feb 16), -42.6%."),
        "expected_rows": 14,
        "expected_type": "creators_series",
        "expected_values": WA_CREATORS,
        "family": "multi_turn_context",
        "id": "p22",
        "min_rows": 14,
        "origin": "user probe 2026-11-13 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "creators_series",
             "user": "weekly trend for creators on Instagram"},
            {"expect_app": "whatsapp", "expect_type": "creators_series",
             "user": "same for WhatsApp"}]}
    p23 = {
        "checks": {"creators_series_chart_kind": "line",
                   "creators_series_values": IG_CREATORS,
                   "daily_creators_value": DAILY_CREATORS_IG},
        "description": ("User probe, live shell 2026-11-13, new "
                        "entry point for the creators series: turn 1 "
                        "is 'daily creators for Instagram?' -> the "
                        "daily-creators scalar (type query, metric "
                        "daily_creators_mean, mean 7,493.02 distinct "
                        "creators/day); turn 2 is 'can you give me a "
                        "weekly trend for this from 2026-01-01 to "
                        "2026-03-31' -> creators_series for "
                        "Instagram (the full 14-week series; the "
                        "stated dates are the data window, not a "
                        "sub-range). 'This' resolves against the last "
                        "answered metric (self.last_metric). Before "
                        "the build, turn 1 was answered in the live "
                        "shell only by the LLM fallback turn (the "
                        "model never ran; the deterministic baseline "
                        "plan answered) and turn 2 clarified."),
        "expected_rows": 14,
        "expected_type": "creators_series",
        "expected_values": IG_CREATORS,
        "family": "multi_turn_context",
        "id": "p23",
        "min_rows": 14,
        "origin": "user probe 2026-11-13 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "query",
             "user": "daily creators for Instagram?"},
            {"expect_app": "instagram", "expect_type": "creators_series",
             "user": "can you give me a weekly trend for this from 2026-01-01 to 2026-03-31"}]}
    convs = v10_convs + [p17, p18, p20, p21, p22, p23]

    oracle_entries = v10_oracle["entries"] + [
        {"id": "p17", "kind": "series",
         "note": ("Probe (user probe 2026-11-13, multi_turn_context, "
                  "follow-up). The last 4 complete weeks of the "
                  "Instagram weekly WAU series (Mar 2/9/16/23 2026) "
                  "from fact_sessions; the DISTINCT-pairs formulation "
                  "agrees exactly with the grouped COUNT(DISTINCT) "
                  "shape the planner runs, and the values equal the "
                  "v9 series at indices 9-12. The partial Mar 30 "
                  "week is excluded by construction (the IN list "
                  "names only complete weeks). Mean 147,138.5/week. "
                  "Structural checks run against EVERY turn's SQL, "
                  "and turn 1 is the full weekly series while turn 2 "
                  "is the last-4 series, so no IN-list substring is "
                  "checked here -- the exact 4 values are pinned by "
                  "check_values instead."),
         "sql": _sql_last4_b("instagram"),
         "structural": [
             {"label": "Last-4 SQL must be app-scoped to Instagram (the question names the app)",
              "contains": "app = 'instagram'"},
             {"label": "Last-4 SQL must not average the weekly values into one scalar (that is wau_mean_weekly)",
              "not_contains": "AVG("},
             {"label": "Last-4 SQL must not contain a country filter the question did not ask for",
              "not_contains": "country = '"}]},
        {"id": "p18", "kind": "series",
         "note": ("Probe (user probe 2026-11-13, multi_turn_context, "
                  "follow-up). The last 4 complete weeks of the "
                  "WhatsApp weekly WAU series (Mar 2/9/16/23 2026) "
                  "from the 'same for WhatsApp' follow-up after the "
                  "Instagram last-4 turn -- the same values the "
                  "harness's check_values last-4 branch scores "
                  "against the turn-3 reply's rows, verified two ways "
                  "(DISTINCT-pairs oracle SQL vs grouped "
                  "COUNT(DISTINCT) planner shape, agreeing exactly) "
                  "and tied to the v9 series at indices 9-12. Mean "
                  "98,058.75/week. No structural app check here: the "
                  "structural checks run against EVERY turn's SQL, "
                  "and turn 1/2 are Instagram while turn 3 is "
                  "WhatsApp, so no single app substring is present "
                  "in all three; the turn-3 app is pinned by "
                  "check_turn (expect_app whatsapp) plus the "
                  "check_values last-4 branch."),
         "sql": _sql_last4_b("whatsapp"),
         "structural": [
             {"label": "Last-4 SQL must not average the weekly values into one scalar (that is wau_mean_weekly)",
              "not_contains": "AVG("},
             {"label": "Last-4 SQL must not contain a country filter the question did not ask for",
              "not_contains": "country = '"}]},
        {"id": "p19", "kind": "series",
         "note": ("Probe (user probe 2026-11-13, metric_scoping). "
                  "The two-app weekly comparison from fact_sessions: "
                  "the combined FULL OUTER JOIN query with "
                  "DISTINCT-pairs subqueries (the oracle form) "
                  "agrees exactly with the grouped COUNT(DISTINCT) "
                  "form the turn runs, on all 14 weeks x 3 columns. "
                  "Both series are the v9-verified series (IG "
                  "147,939 peak Mar 2, WA 98,372 peak Mar 2). The "
                  "harness's weekly_compare branch checks each app's "
                  "column against its own stored series, so the two "
                  "series can never be swapped silently."),
         "sql": _sql_compare_b(),
         "structural": [
             {"label": "Compare SQL must be app-scoped to Instagram (the question names the app)",
              "contains": "app = 'instagram'"},
             {"label": "Compare SQL must be app-scoped to WhatsApp (the question names the app)",
              "contains": "app = 'whatsapp'"},
             {"label": "Compare SQL must not average the weekly values into one scalar",
              "not_contains": "AVG("}]},
        {"id": "p20", "kind": "series",
         "note": ("Probe (user probe 2026-11-13, multi_turn_context, "
                  "follow-up). The two-app weekly comparison from "
                  "the 'compare with WhatsApp' follow-up after the "
                  "Instagram weekly series -- the same combined SQL "
                  "as p19 (DISTINCT-pairs oracle form agreeing "
                  "exactly with the turn's grouped COUNT(DISTINCT) "
                  "form). The harness's check_values compare branch "
                  "scores the turn-2 reply's payload (both series + "
                  "stats) against the same stored values. No "
                  "structural WhatsApp check here: the structural "
                  "checks run against EVERY turn's SQL, and turn 1 "
                  "is the Instagram-only weekly series, so only the "
                  "Instagram substring is present in both."),
         "sql": _sql_compare_b(),
         "structural": [
             {"label": "Compare SQL must be app-scoped to Instagram (the series app)",
              "contains": "app = 'instagram'"},
             {"label": "Compare SQL must not average the weekly values into one scalar",
              "not_contains": "AVG("}]},
        {"id": "p21", "kind": "series",
         "note": ("Probe (user probe 2026-11-13, multi_turn_context, "
                  "follow-up). The 14-week Instagram weekly creators "
                  "series (Jan 1-Mar 31 2026) from fact_content_creation "
                  "JOIN dim_content; the DISTINCT-pairs formulation "
                  "agrees exactly with the grouped COUNT(DISTINCT) "
                  "shape the planner runs, so the stored values are "
                  "verified two ways. First week (Mon Dec 29) 26,410 "
                  "covers only Jan 1-4 and last week (Mon Mar 30) "
                  "8,252 covers only Mar 30-31 -- both partial, "
                  "verified against direct distinct-creator counts "
                  "over those days alone. Mean 23,866.43/week. The "
                  "same series is the v10 p15 expected_values "
                  "(third-path cross-check). No aggregate table "
                  "carries weekly creators, so the cross-check is two "
                  "independent raw formulations plus the partial-week "
                  "ties, not agg-vs-raw."),
         "sql": _sql_creators_b("instagram"),
         "structural": [
             {"label": "Creators series SQL must be app-scoped to Instagram (the question names the app)",
              "contains": "app = 'instagram'"},
             {"label": "Creators series SQL must not average the weekly values into one scalar",
              "not_contains": "AVG("}]},
        {"id": "p22", "kind": "series",
         "note": ("Probe (user probe 2026-11-13, multi_turn_context, "
                  "follow-up). The 14-week WhatsApp weekly creators "
                  "series (Jan 1-Mar 31 2026) from the 'same for "
                  "WhatsApp' follow-up after the Instagram creators "
                  "series -- the same values the harness's "
                  "check_values creators branch scores against the "
                  "turn-2 reply's rows, verified two ways "
                  "(DISTINCT-pairs oracle SQL vs grouped "
                  "COUNT(DISTINCT) planner shape, agreeing exactly) "
                  "plus the partial-week ties. Mean 15,812.0/week. "
                  "No structural app check here: the structural "
                  "checks run against EVERY turn's SQL, and turn 1 "
                  "is Instagram while turn 2 is WhatsApp; the "
                  "turn-2 app is pinned by check_turn (expect_app "
                  "whatsapp) plus the check_values creators branch."),
         "sql": _sql_creators_b("whatsapp"),
         "structural": [
             {"label": "Creators series SQL must count distinct creators (the population is creators, not sessions)",
              "contains": "creator_id"},
             {"label": "Creators series SQL must not average the weekly values into one scalar",
              "not_contains": "AVG("}]},
        {"id": "p23", "kind": "conversation_values",
         "note": ("Probe (user probe 2026-11-13, multi_turn_context, "
                  "follow-up). The daily-creators scalar for "
                  "Instagram (turn 1): mean distinct creators per "
                  "day, 7,493.02, from fact_content_creation JOIN "
                  "dim_content -- the planner AVG-wrapper SQL and "
                  "the AVG-of-daily-distinct-counts formulation "
                  "agree exactly. The turn-2 creators series values "
                  "are verified in the wiring script (two independent "
                  "formulations + partial-week ties + the v10 p15 "
                  "cross-check) and scored by the harness's "
                  "check_values creators branch; this oracle entry "
                  "covers the scalar because run_oracle's "
                  "conversation_values kind checks single values "
                  "only, and the series kind would need a second "
                  "entry for the same case id."),
         "checks": {
             "daily_creators_value": {
                 "sql": _sql_daily_creators_b("instagram"),
                 "tolerance": 0.01}}},
    ]
    manifest_cases = list(v10_manifest["cases"])
    manifest_cases.append({"family": "metric_scoping", "id": "p19",
                           "kind": "single", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p17",
                           "kind": "conversation", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p18",
                           "kind": "conversation", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p20",
                           "kind": "conversation", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p21",
                           "kind": "conversation", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p22",
                           "kind": "conversation", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p23",
                           "kind": "conversation", "split": "dev"})

    assert len(singles) == 144, len(singles)
    assert len(convs) == 29, len(convs)
    assert len(oracle_entries) == 173, len(oracle_entries)
    assert len(manifest_cases) == 173, len(manifest_cases)
    dev = sum(1 for c in manifest_cases if c["split"] == "dev")
    held = sum(1 for c in manifest_cases if c["split"] == "heldout")
    assert (dev, held) == (143, 30), (dev, held)
    assert len({c["id"] for c in manifest_cases}) == 173
    assert {e["id"] for e in oracle_entries} == {c["id"] for c in manifest_cases}

    (ROOT / "evals/benchmark_v11.json").write_text(json.dumps(singles, indent=1) + "\n")
    (ROOT / "evals/conversation_v11.json").write_text(json.dumps(convs, indent=1) + "\n")
    (ROOT / "evals/oracle_v11.json").write_text(
        json.dumps({"entries": oracle_entries, "version": "v11"}, indent=1) + "\n")
    heldout_ids = sorted(c["id"] for c in manifest_cases if c["split"] == "heldout")
    manifest = {
        "benchmark": "socialpulse-nlq",
        "created": "2026-11-13",
        "data": v10_manifest["data"],
        "families": v10_manifest["families"],
        "heldout_proposal": {
            "heldout": heldout_ids,
            "note": ("v11 held-out (30): unchanged from v10 -- the new probe cases (p17-p23) are "
                     "in dev. HONEST LIMITATION: the probes were run during the build "
                     "(that is how the turns were verified), so like v1-v10, v11 held-out "
                     "means 'held out from fixes', not pristine. Truly fresh held-out "
                     "cases (designed without probing) have not arrived yet. The 120/30 "
                     "target split is exceeded at 173 cases (143 dev / 30 held-out)."),
            "rationale": "Different (metric, app, window) combinations plus hard cases held back entirely."},
        "rules": v10_manifest["rules"] + [
            "Probe cases p17-p23 are labeled with their origin (user probes 2026-11-13) in the case file and in docs/benchmark.md; they pin behavior the user actually saw in the live shell, not behavior designed in advance.",
            "Probe case p21 uses the corrected spelling of the user's live text ('similar', not 'simialr'); the live typo is recorded in the case description, not in the question."],
        "version": "v11",
        "cases": manifest_cases,
    }
    (ROOT / "evals/benchmark_v11_manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")
    print(f"Wrote v11: {len(singles)} singles, {len(convs)} conversations, "
          f"{len(oracle_entries)} oracle entries, {len(manifest_cases)} manifest cases "
          f"({dev} dev / {held} held-out).")


if __name__ == "__main__":
    main()
