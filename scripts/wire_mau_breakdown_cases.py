"""Wire the user probe (2026-11-09) into benchmark v5
(Phase 1, MAU-to-breakdown wiring + sticky MAU context).

Reads the v4 files and writes:
  evals/benchmark_v5.json            (138 v4 singles, unchanged)
  evals/conversation_v5.json         (15 v4 conversations + p04 + p05)
  evals/oracle_v5.json               (153 v4 entries + 2 probe entries)
  evals/benchmark_v5_manifest.json   (155 cases, 125 dev / 30 held-out)

The probe (live shell, Siddhartha's screenshot 2026-11-09):
  p04 (conversation, multi_turn_context, dev): turn 1 is "What is
      the current MAU for Instagram?" (MAU turn, 210,809, sets the
      session app to Instagram); turn 2 is "do you have this data
      by surface". Before the wiring, turn 2 deflected to "ask for a
      weather report first": _breakdown_turn gates on self.weather,
      which the MAU turn never sets -- even though the MAU result
      was sitting right there. The v5 wiring lets the breakdown gate
      accept MAU context (March 2026, the MAU period, app set), with
      the topic defaulting to creators. Turn 2 expects the breakdown
      turn: creators by surface, Instagram, March 2026. The probe
      names no app and no topic, so the 2-turn shape is the session
      as asked, with app and topic inherited. "creators by surface"
      after MAU, and the bare "by surface", are the same turn.
  p05 (conversation, multi_turn_context, dev): the same session
      chained one turn further -- turn 3 is "creators by country".
      Before the sticky flag, turn 3 deflected to weather-first:
      the breakdown turn moves the stage off "mau", so MAU context
      (the current stage) was gone. The sticky flag (self.mau_context,
      set by the MAU turn, cleared by weather/daily-series turns)
      lets turn 3 run the breakdown too: creators by country,
      Instagram, March 2026.

Rules enforced here -- a failure aborts generation, nothing is written:
  1. Every oracle value is computed by executing independent SQL on the
     500K data. The planner/chat is never consulted for a value. The
     posts + creators per surface (p04) and per country (p05) are
     verified TWO independent ways: form A is the direct GROUP BY (the
     shape the turn itself runs); form B is posts via an IN-subquery
     on dim_content and creators via a DISTINCT-pairs CTE. The two must
     agree exactly, and the per-segment posts must sum to the 167,055
     total Instagram March posts. Stored values come from the agreeing
     forms.
  2. No aggregate table carries posts/creators by surface
     (agg_surface_daily has dau/impressions/engagements only), so
     unlike p02/p03 there is no agg-vs-raw cross-check here; the
     cross-check is two independent raw formulations plus the totals
     check. That limit is stated in the oracle note.
  3. Data pin: generation refuses unless the data is the frozen 500K
     seed-42 set (counts + max session date 2026-03-31).
  4. Counts: 138 singles, 17 conversations, 155 oracle entries,
     155 manifest cases, 125 dev / 30 held-out. The new cases are in
     dev; held-out is unchanged (30).

Usage: PYTHONPATH=src python scripts/wire_mau_breakdown_cases.py [--data data/phase1]
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

# p04: creators by surface for Instagram, March 2026.
# Form A (direct GROUP BY -- the shape the breakdown turn runs).
P04_SQL_A = """SELECT c.surface, COUNT(*) AS posts, COUNT(DISTINCT f.creator_id) AS creators
FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id
WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND c.app = 'instagram'
GROUP BY c.surface ORDER BY posts DESC"""
# Form B (independent formulations, used for the oracle checks):
# posts via an IN-subquery on dim_content; creators via a
# DISTINCT-pairs CTE. Both must agree with form A exactly.
P04_SQL_B_POSTS = """SELECT c.surface, COUNT(*) AS posts FROM dim_content c
WHERE c.app = 'instagram' AND c.content_id IN (
  SELECT content_id FROM fact_content_creation
  WHERE CAST(published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31')
GROUP BY c.surface ORDER BY posts DESC"""
P04_SQL_B_CREATORS = """WITH pairs AS (
  SELECT DISTINCT f.creator_id, c.surface
  FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id
  WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND c.app = 'instagram')
SELECT surface, COUNT(*) AS creators FROM pairs GROUP BY surface ORDER BY creators DESC"""
P04_SQL_TOTAL = """SELECT COUNT(*) FROM fact_content_creation f
JOIN dim_content c ON c.content_id = f.content_id
WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND c.app = 'instagram'"""

# Oracle check SQLs: one scalar query per (surface, metric), using the
# independent form-B formulations, exact tolerance (counts, not rates).
P04_CHECK_SQL = {}
for _surface in ("reels", "feed", "stories"):
    P04_CHECK_SQL[f"breakdown_{_surface}_posts"] = (
        "SELECT COUNT(*) AS value FROM dim_content c "
        f"WHERE c.app = 'instagram' AND c.surface = '{_surface}' AND c.content_id IN ("
        "SELECT content_id FROM fact_content_creation "
        "WHERE CAST(published_at AS DATE) BETWEEN DATE '2026-03-01' "
        "AND DATE '2026-03-31')")
    P04_CHECK_SQL[f"breakdown_{_surface}_creators"] = (
        "SELECT COUNT(*) AS value FROM ("
        "SELECT DISTINCT f.creator_id FROM fact_content_creation f "
        "JOIN dim_content c ON c.content_id = f.content_id "
        f"WHERE c.app = 'instagram' AND c.surface = '{_surface}' "
        "AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' "
        "AND DATE '2026-03-31')")

# p05: creators by country for Instagram, March 2026 (turn 3 of the
# chained session). Form A is the direct GROUP BY (the shape the
# breakdown turn runs); form B is creators via a DISTINCT-pairs CTE
# and posts via an IN-subquery on dim_content for the app filter.
P05_SQL_A = """SELECT u.country, COUNT(DISTINCT f.creator_id) AS creators, COUNT(*) AS posts
FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id
JOIN dim_users u ON u.user_id = f.creator_id
WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND c.app = 'instagram'
GROUP BY u.country ORDER BY creators DESC"""
P05_SQL_B_CREATORS = """WITH pairs AS (
  SELECT DISTINCT f.creator_id, u.country
  FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id
  JOIN dim_users u ON u.user_id = f.creator_id
  WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND c.app = 'instagram')
SELECT country, COUNT(*) AS creators FROM pairs GROUP BY country ORDER BY creators DESC"""
P05_SQL_B_POSTS = """SELECT u.country, COUNT(*) AS posts
FROM fact_content_creation f JOIN dim_users u ON u.user_id = f.creator_id
WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'
AND f.content_id IN (SELECT content_id FROM dim_content WHERE app = 'instagram')
GROUP BY u.country ORDER BY posts DESC"""
P05_SQL_TOTAL_CREATORS = """SELECT COUNT(DISTINCT f.creator_id) FROM fact_content_creation f
JOIN dim_content c ON c.content_id = f.content_id
WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND c.app = 'instagram'"""

# Oracle check SQLs: one scalar query per (country, metric), using the
# independent form-B formulations, exact tolerance (counts, not rates).
P05_CHECK_SQL = {}
for _country in ("US", "IN", "BR", "ID", "MX", "GB", "DE"):
    P05_CHECK_SQL[f"breakdown_country_{_country}_creators"] = (
        "SELECT COUNT(*) AS value FROM ("
        "SELECT DISTINCT f.creator_id FROM fact_content_creation f "
        "JOIN dim_content c ON c.content_id = f.content_id "
        "JOIN dim_users u ON u.user_id = f.creator_id "
        f"WHERE c.app = 'instagram' AND u.country = '{_country}' "
        "AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' "
        "AND DATE '2026-03-31')")
    P05_CHECK_SQL[f"breakdown_country_{_country}_posts"] = (
        "SELECT COUNT(*) AS value FROM fact_content_creation f "
        "JOIN dim_users u ON u.user_id = f.creator_id "
        f"WHERE u.country = '{_country}' "
        "AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' "
        "AND DATE '2026-03-31' "
        "AND f.content_id IN (SELECT content_id FROM dim_content "
        "WHERE app = 'instagram')")


def _fail(msg):
    print(f"REFUSED: {msg}")
    raise SystemExit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA_DIR_DEFAULT)
    args = ap.parse_args()

    v4_singles = json.loads((ROOT / "evals/benchmark_v4.json").read_text())
    v4_convs = json.loads((ROOT / "evals/conversation_v4.json").read_text())
    v4_oracle = json.loads((ROOT / "evals/oracle_v4.json").read_text())
    v4_manifest = json.loads((ROOT / "evals/benchmark_v4_manifest.json").read_text())

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

    # p04: form A and the two form-B queries must agree exactly, and
    # the per-surface posts must sum to the total Instagram March posts.
    rows_a = con.execute(P04_SQL_A).fetchall()
    rows_b_posts = con.execute(P04_SQL_B_POSTS).fetchall()
    rows_b_creators = con.execute(P04_SQL_B_CREATORS).fetchall()
    total = con.execute(P04_SQL_TOTAL).fetchone()[0]
    vals_a = {r[0]: {"posts": r[1], "creators": r[2]} for r in rows_a}
    vals_b_posts = {r[0]: r[1] for r in rows_b_posts}
    vals_b_creators = {r[0]: r[1] for r in rows_b_creators}
    if set(vals_a) != {"reels", "feed", "stories"}:
        _fail(f"p04 surfaces: {sorted(vals_a)}, expected reels/feed/stories")
    for surface, vals in vals_a.items():
        if vals["posts"] != vals_b_posts.get(surface):
            _fail(f"p04 {surface} posts: form A {vals['posts']} != form B {vals_b_posts.get(surface)}")
        if vals["creators"] != vals_b_creators.get(surface):
            _fail(f"p04 {surface} creators: form A {vals['creators']} != form B {vals_b_creators.get(surface)}")
    if sum(v["posts"] for v in vals_a.values()) != total:
        _fail(f"p04 posts sum {sum(v['posts'] for v in vals_a.values())} != total {total}")
    # Cross-check against the parent's independent turn probe of the
    # same question (reels 75,450 / 24,388; feed 58,096 / 22,096;
    # stories 33,509 / 17,145).
    expected = {"reels": {"posts": 75450, "creators": 24388},
                "feed": {"posts": 58096, "creators": 22096},
                "stories": {"posts": 33509, "creators": 17145}}
    if vals_a != expected:
        _fail(f"p04 values {vals_a} do not match the verified turn probe {expected}")
    print(f"Oracle verified: p04 creators by surface, Instagram March 2026 -- "
          f"form A == form B exactly, posts sum to total {total:,}: "
          + ", ".join(f"{s} {v['posts']:,}/{v['creators']:,}"
                      for s, v in vals_a.items()) + ".")

    # p05: form A and the two form-B queries must agree exactly; the
    # per-country posts must sum to the 167,055 total Instagram March
    # posts (same posts as p04, grouped by country instead of
    # surface), and the per-country creators must sum to the total
    # distinct Instagram March creators (each creator has exactly one
    # country in dim_users).
    rows_a5 = con.execute(P05_SQL_A).fetchall()
    rows_b5_creators = con.execute(P05_SQL_B_CREATORS).fetchall()
    rows_b5_posts = con.execute(P05_SQL_B_POSTS).fetchall()
    total_creators = con.execute(P05_SQL_TOTAL_CREATORS).fetchone()[0]
    vals_a5 = {r[0]: {"creators": r[1], "posts": r[2]} for r in rows_a5}
    vals_b5_creators = {r[0]: r[1] for r in rows_b5_creators}
    vals_b5_posts = {r[0]: r[1] for r in rows_b5_posts}
    if set(vals_a5) != {"US", "IN", "BR", "ID", "MX", "GB", "DE"}:
        _fail(f"p05 countries: {sorted(vals_a5)}, expected US/IN/BR/ID/MX/GB/DE")
    for country, vals in vals_a5.items():
        if vals["creators"] != vals_b5_creators.get(country):
            _fail(f"p05 {country} creators: form A {vals['creators']} != form B {vals_b5_creators.get(country)}")
        if vals["posts"] != vals_b5_posts.get(country):
            _fail(f"p05 {country} posts: form A {vals['posts']} != form B {vals_b5_posts.get(country)}")
    if sum(v["posts"] for v in vals_a5.values()) != total:
        _fail(f"p05 posts sum {sum(v['posts'] for v in vals_a5.values())} != total {total}")
    if sum(v["creators"] for v in vals_a5.values()) != total_creators:
        _fail(f"p05 creators sum {sum(v['creators'] for v in vals_a5.values())} != total distinct {total_creators}")
    # Cross-check against the parent's independent turn probe of the
    # same chained question (US 9,286 / 49,531; IN 7,843 / 42,372;
    # BR 4,785 / 25,720; ID 3,149 / 16,537; MX 2,488 / 13,344;
    # GB 1,897 / 9,884; DE 1,849 / 9,667).
    expected5 = {"US": {"creators": 9286, "posts": 49531},
                 "IN": {"creators": 7843, "posts": 42372},
                 "BR": {"creators": 4785, "posts": 25720},
                 "ID": {"creators": 3149, "posts": 16537},
                 "MX": {"creators": 2488, "posts": 13344},
                 "GB": {"creators": 1897, "posts": 9884},
                 "DE": {"creators": 1849, "posts": 9667}}
    if vals_a5 != expected5:
        _fail(f"p05 values {vals_a5} do not match the verified turn probe {expected5}")
    print(f"Oracle verified: p05 creators by country, Instagram March 2026 -- "
          f"form A == form B exactly, posts sum to total {total:,}, creators sum "
          f"to {total_creators:,}: "
          + ", ".join(f"{c} {v['creators']:,}/{v['posts']:,}"
                      for c, v in vals_a5.items()) + ".")

    # --- build v5 files
    checks = {"breakdown_values": vals_a}
    for surface, vals in vals_a.items():
        checks[f"breakdown_{surface}_posts"] = vals["posts"]
        checks[f"breakdown_{surface}_creators"] = vals["creators"]
    p04 = {
        "checks": checks,
        "description": ("User probe, live shell 2026-11-09: turn 1 is 'What is the current "
                        "MAU for Instagram?' (MAU turn, 210,809, sets the session app to "
                        "Instagram); turn 2 is 'do you have this data by surface'. Before "
                        "the wiring, turn 2 deflected to 'ask for a weather report first': "
                        "_breakdown_turn gates on self.weather, which the MAU turn never "
                        "sets. The v5 wiring lets the breakdown gate accept MAU context "
                        "(March 2026, the MAU period, app set), with the topic defaulting "
                        "to creators. Turn 2 expects the breakdown turn: creators by "
                        "surface, Instagram, March 2026. The probe names no app and no "
                        "topic, so the 2-turn shape is the session as asked, with app "
                        "and topic inherited. Values verified two independent ways "
                        "(direct GROUP BY vs IN-subquery / DISTINCT-pairs CTE), agreeing "
                        "exactly; per-surface posts sum to the 167,055 total."),
        "family": "multi_turn_context",
        "id": "p04",
        "origin": "user probe 2026-11-09",
        "turns": [
            {"expect_app": "instagram", "expect_type": "mau",
             "user": "What is the current MAU for Instagram?"},
            {"expect_app": "instagram", "expect_topic": "creators",
             "expect_type": "breakdown",
             "user": "do you have this data by surface"}]}
    singles = list(v4_singles)
    checks5 = {"breakdown_values": vals_a}
    for surface, vals in vals_a.items():
        checks5[f"breakdown_{surface}_posts"] = vals["posts"]
        checks5[f"breakdown_{surface}_creators"] = vals["creators"]
    checks5["breakdown_country_values"] = vals_a5
    for country, vals in vals_a5.items():
        checks5[f"breakdown_country_{country}_posts"] = vals["posts"]
        checks5[f"breakdown_country_{country}_creators"] = vals["creators"]
    p05 = {
        "checks": checks5,
        "description": ("User probe, live shell 2026-11-09, chained one turn "
                        "further (sticky MAU context): turn 1 is 'What is the "
                        "current MAU for Instagram?' (MAU turn, 210,809, sets "
                        "the session app to Instagram); turn 2 is 'do you have "
                        "this data by surface' (breakdown, creators by surface, "
                        "Instagram, March 2026 -- the p04 turn); turn 3 is "
                        "'creators by country'. Before the sticky flag, turn 3 "
                        "deflected to 'ask for a weather report first': the "
                        "breakdown turn moves the stage off 'mau', so MAU "
                        "context (the current stage) was gone after the first "
                        "breakdown. The sticky flag (self.mau_context, set by "
                        "the MAU turn, cleared by weather/daily-series turns) "
                        "lets turn 3 run the breakdown too: creators by "
                        "country, Instagram, March 2026. Turn 2 values are "
                        "the p04 values (same session shape); turn 3 values "
                        "verified two independent ways (direct GROUP BY vs "
                        "IN-subquery / DISTINCT-pairs CTE), agreeing exactly; "
                        "per-country posts sum to the 167,055 total and "
                        "per-country creators sum to the 31,297 distinct "
                        "Instagram March creators."),
        "family": "multi_turn_context",
        "id": "p05",
        "origin": "user probe 2026-11-09 (chained)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "mau",
             "user": "What is the current MAU for Instagram?"},
            {"expect_app": "instagram", "expect_topic": "creators",
             "expect_type": "breakdown",
             "user": "do you have this data by surface"},
            {"expect_app": "instagram", "expect_topic": "creators",
             "expect_type": "breakdown",
             "user": "creators by country"}]}
    convs = v4_convs + [p04, p05]
    oracle_entries = v4_oracle["entries"] + [
        {"id": "p04", "kind": "conversation_values",
         "note": ("Probe (user probe 2026-11-09, multi_turn_context). Posts + distinct "
                  "creators per surface for Instagram, March 2026, from the breakdown "
                  "turn the wiring enables after a MAU turn. The oracle SQLs are "
                  "independent formulations (posts via an IN-subquery on dim_content, "
                  "creators via a DISTINCT-pairs CTE) that agree exactly with the "
                  "direct GROUP BY form, and the per-surface posts sum to the 167,055 "
                  "total. No aggregate table carries posts/creators by surface "
                  "(agg_surface_daily has dau/impressions/engagements only), so the "
                  "cross-check is two independent raw formulations plus the totals "
                  "check, not agg-vs-raw. Exact tolerance (counts). The case's "
                  "breakdown_values carry the same numbers for the harness's "
                  "check_values breakdown branch, which scores the reply's rows."),
         "checks": {name: {"sql": sql, "tolerance": 0}
                    for name, sql in P04_CHECK_SQL.items()},
         "structural": [
             {"label": "MAU + breakdown SQLs are app-scoped to Instagram (the probe inherits the app from the MAU turn)",
              "contains": "app = 'instagram'"},
             {"label": "Breakdown SQL must not contain a country filter the question did not ask for",
              "not_contains": "country = '"}]},
        {"id": "p05", "kind": "conversation_values",
         "note": ("Probe (user probe 2026-11-09, multi_turn_context, chained). "
                  "Posts + distinct creators per country for Instagram, March "
                  "2026, from the second breakdown in the chained session -- "
                  "the turn the sticky MAU flag enables. The oracle SQLs are "
                  "independent formulations (posts via an IN-subquery on "
                  "dim_content for the app filter, creators via a "
                  "DISTINCT-pairs CTE) that agree exactly with the direct "
                  "GROUP BY form; per-country posts sum to the 167,055 total "
                  "and per-country creators sum to the 31,297 distinct "
                  "Instagram March creators. No aggregate table carries "
                  "posts/creators by country (agg tables have dau/impressions/ "
                  "engagements only), so the cross-check is two independent "
                  "raw formulations plus the totals checks, not agg-vs-raw. "
                  "Exact tolerance (counts). The case's breakdown_country_values "
                  "carry the same numbers for the harness's check_values "
                  "chained-breakdown branch, which scores the turn-3 reply's "
                  "rows; the turn-2 surface values are the p04 values and are "
                  "oracle-checked under the p04 entry's SQLs."),
         "checks": {name: {"sql": sql, "tolerance": 0}
                    for name, sql in P05_CHECK_SQL.items()},
         "structural": [
             {"label": "Chained breakdown SQL is app-scoped to Instagram (the probe inherits the app from the MAU turn)",
              "contains": "app = 'instagram'"}]},
    ]
    manifest_cases = list(v4_manifest["cases"])
    manifest_cases.append({"family": "multi_turn_context", "id": "p04",
                           "kind": "conversation", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p05",
                           "kind": "conversation", "split": "dev"})

    assert len(singles) == 138, len(singles)
    assert len(convs) == 17, len(convs)
    assert len(oracle_entries) == 155, len(oracle_entries)
    assert len(manifest_cases) == 155, len(manifest_cases)
    dev = sum(1 for c in manifest_cases if c["split"] == "dev")
    held = sum(1 for c in manifest_cases if c["split"] == "heldout")
    assert (dev, held) == (125, 30), (dev, held)
    assert len({c["id"] for c in manifest_cases}) == 155
    assert {e["id"] for e in oracle_entries} == {c["id"] for c in manifest_cases}

    (ROOT / "evals/benchmark_v5.json").write_text(json.dumps(singles, indent=1) + "\n")
    (ROOT / "evals/conversation_v5.json").write_text(json.dumps(convs, indent=1) + "\n")
    (ROOT / "evals/oracle_v5.json").write_text(
        json.dumps({"entries": oracle_entries, "version": "v5"}, indent=1) + "\n")
    heldout_ids = sorted(c["id"] for c in manifest_cases if c["split"] == "heldout")
    manifest = {
        "benchmark": "socialpulse-nlq",
        "created": "2026-11-09",
        "data": v4_manifest["data"],
        "families": v4_manifest["families"],
        "heldout_proposal": {
            "heldout": heldout_ids,
            "note": ("v5 held-out (30): unchanged from v4 -- the new probe cases (p04, p05) are "
                     "in dev. HONEST LIMITATION: the probes were run during the build "
                     "(that is how the wiring was verified), so like v1-v4, v5 held-out "
                     "means 'held out from fixes', not pristine. Truly fresh held-out "
                     "cases (designed without probing) have not arrived yet. The 120/30 "
                     "target split is exceeded at 155 cases (125 dev / 30 held-out)."),
            "rationale": "Different (metric, app, window) combinations plus hard cases held back entirely."},
        "rules": v4_manifest["rules"] + [
            "Probe case p04 is labeled with its origin (user probe 2026-11-09) in the case file and in docs/benchmark.md; it pins behavior the user actually saw in the live shell, not behavior designed in advance.",
            "Probe case p05 (chained breakdown, sticky MAU context) is labeled the same way; it pins the turn-3 behavior the user authorized as an extension of the p04 probe."],
        "version": "v5",
        "cases": manifest_cases,
    }
    (ROOT / "evals/benchmark_v5_manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")
    print(f"Wrote v5: {len(singles)} singles, {len(convs)} conversations, "
          f"{len(oracle_entries)} oracle entries, {len(manifest_cases)} manifest cases "
          f"({dev} dev / {held} held-out).")


if __name__ == "__main__":
    main()
