"""Wire the user probes (2026-11-10) into benchmark v7
(Phase 1, creator-share-of-MAU turn).

Reads the v6 files and writes:
  evals/benchmark_v7.json            (139 v6 singles + p08, p09)
  evals/conversation_v7.json         (18 v6 conversations + p10)
  evals/oracle_v7.json               (157 v6 entries + 3 probe entries)
  evals/benchmark_v7_manifest.json   (160 cases, 130 dev / 30 held-out)

The probes (live shell, Siddhartha's screenshots 2026-11-10):
  p08 (single, metric_scoping, dev): "What is the % of creators of
      MAU in Instagram". Before the build, the question answered with
      the MAU scalar 210,809 -- a wrong answer, not a clarification:
      it dropped the "creators" part and routed to the plain MAU turn.
      Expected: the creator_share turn, creators 31,297, MAU 210,809,
      share 14.85%. The creators definition is the same one the
      creators-by-country breakdown uses (p05 sums to the same 31,297
      for Instagram), so the share is a monthly share -- NOT the
      weather report's daily creator penetration (daily creators /
      DAU, ~10.4% for Instagram).
  p09 (single, metric_scoping, dev): "what is the number of creators
      and all users in Instagram for March 2026" -- the same question
      rephrased. Before the build it got the generic "could not map"
      clarification (no turn matched). Expected: the same
      creator_share turn and values as p08.
  p10 (conversation, multi_turn_context, dev): turn 1 is "What is
      the current MAU for Instagram?" (MAU turn, 210,809, sets the
      session app to Instagram); turn 2 is the bare "what is the %
      of creators of MAU" (no app named). After a MAU turn, the
      follow-up inherits the app from the sticky MAU context (same
      app, March 2026) and answers the share: creators 31,297, MAU
      210,809, 14.85%.

Rules enforced here -- a failure aborts generation, nothing is written:
  1. Every oracle value is computed by executing independent SQL on the
     500K data. The planner/chat is never consulted for a value. The
     creator counts are verified THREE independent ways: form A is
     the CTE shape the planner runs; form B is a DISTINCT-pairs CTE
     for creators plus a scalar MAU; form C is scalar subqueries
     (the oracle entry SQL). All three must agree exactly on the
     counts, and the percent within 0.01 of the stored 2-decimal
     value. A fourth check ties the definition to the breakdown: the
     Instagram creators-by-country counts (p05) must sum to the
     creator_share creators count (31,297).
  2. No aggregate table carries creator share (agg_user_daily has
     daily DAU only, agg_surface_daily has posts/impressions), so
     unlike p02/p03 there is no agg-vs-raw cross-check here; the
     cross-check is three independent raw formulations plus the
     p05 totals check. That limit is stated in the oracle note.
  3. Data pin: generation refuses unless the data is the frozen 500K
     seed-42 set (counts + max session date 2026-03-31).
  4. Counts: 141 singles, 19 conversations, 160 oracle entries,
     160 manifest cases, 130 dev / 30 held-out. The new cases are in
     dev; held-out is unchanged (30).

Usage: PYTHONPATH=src python scripts/wire_creator_share_cases.py [--data data/phase1]
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

# p08: creator share of MAU for Instagram, March 2026.
# Form A (CTE shape -- the shape the planner runs).
P08_SQL_A = """WITH creators AS (SELECT COUNT(DISTINCT f.creator_id) AS n FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND c.app = 'instagram'), mau AS (SELECT COUNT(DISTINCT user_id) AS n FROM fact_sessions WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND app = 'instagram') SELECT creators.n AS creators, mau.n AS mau, creators.n * 100.0 / NULLIF(mau.n, 0) AS share_pct FROM creators, mau"""
# Form B (independent formulation): DISTINCT-pairs CTE for creators,
# scalar MAU. Must agree with form A exactly on the counts.
P08_SQL_B_CREATORS = """SELECT COUNT(*) FROM (SELECT DISTINCT f.creator_id FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND c.app = 'instagram')"""
P08_SQL_B_MAU = """SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM fact_sessions WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND app = 'instagram')"""
# Form C (oracle entry SQL): scalar subqueries -- a third shape,
# genuinely different from the planner's CTE.
P08_SQL_C = """SELECT
  (SELECT COUNT(DISTINCT f.creator_id) FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND c.app = 'instagram') AS creators,
  (SELECT COUNT(DISTINCT user_id) FROM fact_sessions WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND app = 'instagram') AS mau,
  (SELECT COUNT(DISTINCT f.creator_id) FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND c.app = 'instagram') * 100.0 / NULLIF((SELECT COUNT(DISTINCT user_id) FROM fact_sessions WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND app = 'instagram'), 0) AS share_pct"""

# Oracle check SQLs for p10 (the follow-up turn): one scalar query
# per value, using the independent form-B formulation. Counts exact,
# percent within 0.01.
P10_CHECK_SQL = {
    "creator_share_creators": P08_SQL_B_CREATORS,
    "creator_share_mau": P08_SQL_B_MAU,
    "creator_share_pct": (
        f"SELECT ({P08_SQL_B_CREATORS}) * 100.0 / NULLIF(({P08_SQL_B_MAU}), 0)"),
}


def _fail(msg):
    print(f"REFUSED: {msg}")
    raise SystemExit(2)


def _verify_app(con, app, expected_creators, expected_mau, expected_pct, label):
    """Three independent formulations + the p05 totals check (Instagram
    only) for one app's creator share of MAU."""
    sql_a = P08_SQL_A.replace("'instagram'", f"'{app}'")
    sql_b_c = P08_SQL_B_CREATORS.replace("'instagram'", f"'{app}'")
    sql_b_m = P08_SQL_B_MAU.replace("'instagram'", f"'{app}'")
    sql_c = P08_SQL_C.replace("'instagram'", f"'{app}'")
    a = con.execute(sql_a).fetchone()
    b_c = con.execute(sql_b_c).fetchone()[0]
    b_m = con.execute(sql_b_m).fetchone()[0]
    c = con.execute(sql_c).fetchone()
    if a[0] != b_c or a[1] != b_m:
        _fail(f"{label}: form A {a} != form B ({b_c}, {b_m})")
    if c[0] != a[0] or c[1] != a[1]:
        _fail(f"{label}: form C {c} != form A {a}")
    if abs(a[2] - expected_pct) > 0.01 or abs(c[2] - expected_pct) > 0.01:
        _fail(f"{label}: share {a[2]:.4f} / {c[2]:.4f} != expected {expected_pct}")
    if a[0] != expected_creators or a[1] != expected_mau:
        _fail(f"{label}: creators/mau {a[0]}/{a[1]} != expected "
              f"{expected_creators}/{expected_mau}")
    print(f"Oracle verified: {label} creator share of MAU, March 2026 -- "
          f"forms A == B == C on counts, share {a[2]:.2f}%: "
          f"{a[0]:,} creators / {a[1]:,} MAU.")
    return a[0], a[1], a[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA_DIR_DEFAULT)
    args = ap.parse_args()

    v6_singles = json.loads((ROOT / "evals/benchmark_v6.json").read_text())
    v6_convs = json.loads((ROOT / "evals/conversation_v6.json").read_text())
    v6_oracle = json.loads((ROOT / "evals/oracle_v6.json").read_text())
    v6_manifest = json.loads((ROOT / "evals/benchmark_v6_manifest.json").read_text())

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

    # p08/p09: three forms agree; cross-check against the parent's
    # independent probe (creators 31,297, MAU 210,809, share 14.85%).
    creators_ig, mau_ig, _pct_ig = _verify_app(
        con, "instagram", 31297, 210809, 14.85, "p08/p09 Instagram")
    # Fourth check (definition tie): the p05 creators-by-country
    # counts must sum to the creator_share creators count -- the two
    # turns use the same creators definition, so the populations are
    # the same, not two different "creators" that happen to agree.
    p05 = {"US": 9286, "IN": 7843, "BR": 4785, "ID": 3149,
           "MX": 2488, "GB": 1897, "DE": 1849}
    by_country = con.execute(
        """SELECT u.country, COUNT(DISTINCT f.creator_id) FROM fact_content_creation f
           JOIN dim_content c ON c.content_id = f.content_id
           JOIN dim_users u ON u.user_id = f.creator_id
           WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'
           AND c.app = 'instagram' GROUP BY u.country""").fetchall()
    got_p05 = {r[0]: r[1] for r in by_country}
    if got_p05 != p05:
        _fail(f"p05 creators by country {got_p05} != stored {p05}")
    if sum(got_p05.values()) != creators_ig:
        _fail(f"p05 sum {sum(got_p05.values())} != creator_share creators {creators_ig}")
    print(f"Definition tie verified: p05 creators by country sums to {creators_ig:,} -- "
          "the creator_share creators count is the same population as the breakdown's.")
    # WhatsApp and Facebook, same method (probed, no cases).
    _verify_app(con, "whatsapp", 20718, 140378, 14.76, "WhatsApp (probe, no case)")
    _verify_app(con, "facebook", 17429, 117550, 14.83, "Facebook (probe, no case)")

    # --- build v7 files
    p08 = {
        "category": "probe",
        "description": ("User probe, live shell 2026-11-10: 'What is the % "
                        "of creators of MAU in Instagram'. Before the build, "
                        "the question answered with the MAU scalar 210,809 -- "
                        "a wrong answer, not a clarification: it dropped the "
                        "'creators' part and routed to the plain MAU turn. "
                        "Expected: the creator_share turn, creators 31,297, "
                        "MAU 210,809, share 14.85%. The creators definition "
                        "is the same one the creators-by-country breakdown "
                        "uses (p05 sums to the same 31,297), so the share is "
                        "a monthly share -- NOT the weather report's daily "
                        "creator penetration (daily creators / DAU, ~10.4% "
                        "for Instagram). Values verified three independent "
                        "ways (CTE form vs DISTINCT-pairs CTE vs scalar "
                        "subqueries), agreeing exactly on the counts; the "
                        "percent within 0.01 of 14.85."),
        "expected_creators": 31297,
        "expected_mau": 210809,
        "expected_metric": "creator_share",
        "expected_pct": 14.85,
        "expected_rows": 1,
        "expected_type": "creator_share",
        "family": "metric_scoping",
        "id": "p08",
        "origin": "user probe 2026-11-10",
        "question": "What is the % of creators of MAU in Instagram",
    }
    p09 = {
        "category": "probe",
        "description": ("User probe, live shell 2026-11-10 (same question, "
                        "rephrased): 'what is the number of creators and "
                        "all users in Instagram for March 2026'. Before the "
                        "build it got the generic 'could not map' "
                        "clarification (no turn matched). Expected: the same "
                        "creator_share turn and values as p08 -- creators "
                        "31,297, MAU 210,809, share 14.85%. Same monthly "
                        "share definition, same three-way oracle "
                        "verification as p08."),
        "expected_creators": 31297,
        "expected_mau": 210809,
        "expected_metric": "creator_share",
        "expected_pct": 14.85,
        "expected_rows": 1,
        "expected_type": "creator_share",
        "family": "metric_scoping",
        "id": "p09",
        "origin": "user probe 2026-11-10",
        "question": "what is the number of creators and all users in Instagram for March 2026",
    }
    singles = list(v6_singles) + [p08, p09]
    p10 = {
        "checks": {"creator_share_values": {"creators": 31297, "mau": 210809,
                                            "pct": 14.85},
                   # Per-value keys for the oracle's conversation_values
                   # branch, which looks up case["checks"][check_name]
                   # for each oracle check (same pattern as p07's
                   # per-country keys): without these the oracle raises
                   # KeyError on the check name.
                   "creator_share_creators": 31297,
                   "creator_share_mau": 210809,
                   "creator_share_pct": 14.85},
        "description": ("User probe, live shell 2026-11-10, follow-up form "
                        "(sticky MAU context): turn 1 is 'What is the "
                        "current MAU for Instagram?' (MAU turn, 210,809, "
                        "sets the session app to Instagram); turn 2 is the "
                        "bare 'what is the % of creators of MAU' (no app "
                        "named). After a MAU turn, the follow-up inherits "
                        "the app from the sticky MAU context (same app, "
                        "March 2026) and answers the share -- creators "
                        "31,297, MAU 210,809, 14.85% -- not the MAU scalar "
                        "again. Turn 2 values are the p08 values (same "
                        "session shape); the oracle checks them with "
                        "independent scalar queries (DISTINCT-pairs CTE "
                        "formulation)."),
        "family": "multi_turn_context",
        "id": "p10",
        "origin": "user probe 2026-11-10 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "mau",
             "user": "What is the current MAU for Instagram?"},
            {"expect_app": "instagram", "expect_type": "creator_share",
             "user": "what is the % of creators of MAU"}]}
    convs = v6_convs + [p10]
    oracle_entries = v6_oracle["entries"] + [
        {"id": "p08", "kind": "breakdown",
         "note": ("Probe (user probe 2026-11-10, metric_scoping). Creator "
                  "share of MAU for Instagram, March 2026: distinct "
                  "creators with >=1 content item published in March 2026 "
                  "over distinct users with >=1 session in March 2026 "
                  "(the MAU) -- 31,297 / 210,809 = 14.85%. The oracle SQL "
                  "is the scalar-subquery formulation (form C), genuinely "
                  "different in shape from the planner's CTE query; it "
                  "agrees exactly with the CTE form (A) and the "
                  "DISTINCT-pairs CTE form (B) on the counts, and within "
                  "0.01 on the percent. No aggregate table carries "
                  "creator share (agg_user_daily has daily DAU only, "
                  "agg_surface_daily has posts/impressions), so the "
                  "cross-check is three independent raw formulations "
                  "plus the p05 totals check (the creators-by-country "
                  "counts sum to the same 31,297), not agg-vs-raw. "
                  "Counts exact; percent within 0.01. Structural "
                  "checks: the SQL must be app-scoped to Instagram and "
                  "must not contain a country filter the question did "
                  "not ask for. These numbers are a different answer "
                  "from the plain MAU scalar (210,809 alone) -- the "
                  "metric check (creator_share, not mau_march) and the "
                  "1-row-x-3-col shape check are what stop the silent "
                  "substitution."),
         "sql": P08_SQL_C,
         "structural": [
             {"label": "Creator-share SQL filters creators to Instagram (app-scoped, not all-apps)",
              "contains": "c.app = 'instagram'"},
             {"label": "Creator-share SQL computes the share as a percent",
              "contains": "share_pct"},
             {"label": "Creator-share SQL must not contain a country filter the question did not ask for",
              "not_contains": "country = '"}]},
        {"id": "p09", "kind": "breakdown",
         "note": ("Probe (user probe 2026-11-10, metric_scoping, same "
                  "question rephrased). Same creator-share values as p08 "
                  "-- creators 31,297, MAU 210,809, share 14.85% -- "
                  "reached through the 'number of creators and all users' "
                  "phrasing, which previously got the generic 'could not "
                  "map' clarification. Same three-way oracle verification "
                  "as p08 (scalar-subquery form C as the entry SQL, "
                  "agreeing exactly with the CTE form A and the "
                  "DISTINCT-pairs CTE form B on the counts). Counts exact; "
                  "percent within 0.01."),
         "sql": P08_SQL_C,
         "structural": [
             {"label": "Creator-share SQL filters creators to Instagram (app-scoped, not all-apps)",
              "contains": "c.app = 'instagram'"},
             {"label": "Creator-share SQL computes the share as a percent",
              "contains": "share_pct"},
             {"label": "Creator-share SQL must not contain a country filter the question did not ask for",
              "not_contains": "country = '"}]},
        {"id": "p10", "kind": "conversation_values",
         "note": ("Probe (user probe 2026-11-10, multi_turn_context, "
                  "follow-up). Creator share of MAU for Instagram, March "
                  "2026, from the bare '% of creators of MAU' follow-up "
                  "after a MAU turn -- the same p08 values, reached "
                  "through the sticky MAU context instead of the "
                  "single-shot question. The oracle SQLs are independent "
                  "scalar queries (DISTINCT-pairs CTE formulation) that "
                  "agree exactly with the CTE form on the counts. Counts "
                  "exact; percent within 0.01. The case's "
                  "creator_share_values carry the same numbers for the "
                  "harness's check_values creator-share branch, which "
                  "scores the turn-2 reply's payload."),
         "checks": {
             "creator_share_creators": {"sql": P10_CHECK_SQL["creator_share_creators"],
                                        "tolerance": 0},
             "creator_share_mau": {"sql": P10_CHECK_SQL["creator_share_mau"],
                                   "tolerance": 0},
             "creator_share_pct": {"sql": P10_CHECK_SQL["creator_share_pct"],
                                   "tolerance": 0.01}},
         "structural": [
             # "app = 'instagram'" (not "c.app = ..."): the check runs
             # against EVERY turn's SQL, and turn 1 is the plain MAU
             # scalar ("app = 'instagram'", no dim_content join).
             # The substring is present in both turns' SQL; the
             # turn-2 values check is what pins the follow-up to the
             # creator_share turn.
             {"label": "Follow-up creator-share SQL is app-scoped to Instagram (the probe inherits the app from the MAU turn)",
              "contains": "app = 'instagram'"}]},
    ]
    manifest_cases = list(v6_manifest["cases"])
    manifest_cases.append({"family": "metric_scoping", "id": "p08",
                           "kind": "single", "split": "dev"})
    manifest_cases.append({"family": "metric_scoping", "id": "p09",
                           "kind": "single", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p10",
                           "kind": "conversation", "split": "dev"})

    assert len(singles) == 141, len(singles)
    assert len(convs) == 19, len(convs)
    assert len(oracle_entries) == 160, len(oracle_entries)
    assert len(manifest_cases) == 160, len(manifest_cases)
    dev = sum(1 for c in manifest_cases if c["split"] == "dev")
    held = sum(1 for c in manifest_cases if c["split"] == "heldout")
    assert (dev, held) == (130, 30), (dev, held)
    assert len({c["id"] for c in manifest_cases}) == 160
    assert {e["id"] for e in oracle_entries} == {c["id"] for c in manifest_cases}

    (ROOT / "evals/benchmark_v7.json").write_text(json.dumps(singles, indent=1) + "\n")
    (ROOT / "evals/conversation_v7.json").write_text(json.dumps(convs, indent=1) + "\n")
    (ROOT / "evals/oracle_v7.json").write_text(
        json.dumps({"entries": oracle_entries, "version": "v7"}, indent=1) + "\n")
    heldout_ids = sorted(c["id"] for c in manifest_cases if c["split"] == "heldout")
    manifest = {
        "benchmark": "socialpulse-nlq",
        "created": "2026-11-10",
        "data": v6_manifest["data"],
        "families": v6_manifest["families"],
        "heldout_proposal": {
            "heldout": heldout_ids,
            "note": ("v7 held-out (30): unchanged from v6 -- the new probe cases (p08, p09, p10) are "
                     "in dev. HONEST LIMITATION: the probes were run during the build "
                     "(that is how the turn was verified), so like v1-v6, v7 held-out "
                     "means 'held out from fixes', not pristine. Truly fresh held-out "
                     "cases (designed without probing) have not arrived yet. The 120/30 "
                     "target split is exceeded at 160 cases (130 dev / 30 held-out)."),
            "rationale": "Different (metric, app, window) combinations plus hard cases held back entirely."},
        "rules": v6_manifest["rules"] + [
            "Probe cases p08/p09 are labeled with their origin (user probe 2026-11-10) in the case file and in docs/benchmark.md; they pin behavior the user actually saw in the live shell, not behavior designed in advance.",
            "Probe case p10 (bare '% of creators of MAU' follow-up after a MAU turn) is labeled the same way; it pins the turn-2 behavior the user authorized as the follow-up form of the p08 probe."],
        "version": "v7",
        "cases": manifest_cases,
    }
    (ROOT / "evals/benchmark_v7_manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")
    print(f"Wrote v7: {len(singles)} singles, {len(convs)} conversations, "
          f"{len(oracle_entries)} oracle entries, {len(manifest_cases)} manifest cases "
          f"({dev} dev / {held} held-out).")


if __name__ == "__main__":
    main()
