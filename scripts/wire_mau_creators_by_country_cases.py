"""Wire the user probes (2026-11-11) into benchmark v8
(Phase 1, MAU-and-creators-by-country turn).

Reads the v7 files and writes:
  evals/benchmark_v8.json            (141 v7 singles + p11)
  evals/conversation_v8.json         (19 v7 conversations + p12)
  evals/oracle_v8.json               (160 v7 entries + 2 probe entries)
  evals/benchmark_v8_manifest.json   (162 cases, 132 dev / 30 held-out)

The probes (live shell, Siddhartha's screenshot 2026-11-11):
  p11 (single, metric_scoping, dev): "What are the monthly active
      users and creators by country for Instagram?" Before the build,
      the question answered with MAU by country only -- a wrong
      answer, not a clarification: it dropped the "creators" half and
      routed to the mau_by_country turn. Expected: the combined
      mau_creators_by_country turn -- one breakdown with MAU,
      creators, and share per country: US 63,372 / 9,286 (14.65%),
      IN 52,686 / 7,843 (14.89%), BR 31,623 / 4,785 (15.13%),
      ID 21,081 / 3,149 (14.94%), MX 16,946 / 2,488 (14.68%),
      DE 12,611 / 1,849 (14.66%), GB 12,490 / 1,897 (15.19%);
      totals 210,809 / 31,297 (14.85%). The MAU column is the
      mau_by_country definition (p06) and the creators column is the
      EXACT creators-by-country definition the p05 oracle uses, so
      the two columns sum to the mau_march scalar and the
      creator_share creators count respectively. A monthly cut --
      NOT the weather report's daily creator penetration (daily
      creators / DAU, ~10.4% for Instagram).
  p12 (conversation, multi_turn_context, dev): turn 1 is "What is
      the MAU for Instagram by country?" (mau_by_country, sets the
      sticky MAU context to Instagram); turn 2 is the live probe
      itself -- "I need the Monthly active users and creators by
      country" (no app named). The follow-up inherits the app from
      the sticky MAU context and answers the combined breakdown with
      the same p11 values, not the MAU column alone.

Rules enforced here -- a failure aborts generation, nothing is written:
  1. Every oracle value is computed by executing independent SQL on the
     500K data. The planner/chat is never consulted for a value. The
     per-country values are verified TWO independent ways: form A is
     the grouped-COUNT(DISTINCT) CTE shape the planner runs; form B
     is DISTINCT-pairs CTEs. Both must agree exactly on MAU and
     creators per country, and the share within 0.01 of the stored
     2-decimal percent. Two definition ties: the per-country MAU
     counts must equal the p06 values exactly, and the per-country
     creators counts must equal the p05 values exactly; the totals
     must tie to the mau_march scalar (210,809) and the creator_share
     creators count (31,297).
  2. No aggregate table carries MAU or creators by country
     (agg_user_daily has daily DAU only, agg_surface_daily has
     posts/impressions), so unlike p02/p03 there is no agg-vs-raw
     cross-check here; the cross-check is two independent raw
     formulations plus the p05/p06 ties. That limit is stated in the
     oracle note.
  3. Data pin: generation refuses unless the data is the frozen 500K
     seed-42 set (counts + max session date 2026-03-31).
  4. Counts: 142 singles, 20 conversations, 162 oracle entries,
     162 manifest cases, 132 dev / 30 held-out. The new cases are in
     dev; held-out is unchanged (30).

Usage: PYTHONPATH=src python scripts/wire_mau_creators_by_country_cases.py [--data data/phase1]
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

# p11: MAU and creators by country for Instagram, March 2026.
# Form A (grouped-COUNT(DISTINCT) CTE shape -- the shape the planner runs).
P11_SQL_A = """WITH mau_by_country AS (SELECT u.country, COUNT(DISTINCT s.user_id) AS mau FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id WHERE CAST(s.start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND s.app = 'instagram' GROUP BY u.country), creators_by_country AS (SELECT u.country, COUNT(DISTINCT f.creator_id) AS creators FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id JOIN dim_users u ON u.user_id = f.creator_id WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND c.app = 'instagram' GROUP BY u.country) SELECT COALESCE(m.country, c.country) AS country, COALESCE(m.mau, 0) AS mau, COALESCE(c.creators, 0) AS creators, COALESCE(c.creators, 0) * 100.0 / NULLIF(COALESCE(m.mau, 0), 0) AS share_pct FROM mau_by_country m FULL OUTER JOIN creators_by_country c ON c.country = m.country ORDER BY mau DESC"""
# Form B (oracle entry SQL): DISTINCT-pairs CTEs -- a genuinely
# different formulation from the planner's grouped COUNT(DISTINCT).
P11_SQL_B = """WITH mau_pairs AS (SELECT DISTINCT s.user_id, u.country FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id WHERE CAST(s.start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND s.app = 'instagram'), creator_pairs AS (SELECT DISTINCT f.creator_id, u.country FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id JOIN dim_users u ON u.user_id = f.creator_id WHERE c.app = 'instagram' AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'), mau_by_country AS (SELECT country, COUNT(*) AS mau FROM mau_pairs GROUP BY country), creators_by_country AS (SELECT country, COUNT(*) AS creators FROM creator_pairs GROUP BY country) SELECT COALESCE(m.country, c.country) AS country, COALESCE(m.mau, 0) AS mau, COALESCE(c.creators, 0) AS creators, COALESCE(c.creators, 0) * 100.0 / NULLIF(COALESCE(m.mau, 0), 0) AS share_pct FROM mau_by_country m FULL OUTER JOIN creators_by_country c ON c.country = m.country ORDER BY mau DESC"""

# Stored per-country values (parent-verified: MAU from the p06 oracle,
# creators from the p05 oracle). Share is the 2-decimal percent.
P11_VALUES = {
    "US": {"mau": 63372, "creators": 9286, "share_pct": 14.65},
    "IN": {"mau": 52686, "creators": 7843, "share_pct": 14.89},
    "BR": {"mau": 31623, "creators": 4785, "share_pct": 15.13},
    "ID": {"mau": 21081, "creators": 3149, "share_pct": 14.94},
    "MX": {"mau": 16946, "creators": 2488, "share_pct": 14.68},
    "DE": {"mau": 12611, "creators": 1849, "share_pct": 14.66},
    "GB": {"mau": 12490, "creators": 1897, "share_pct": 15.19},
}
# WhatsApp / Facebook per-country values, probed the same way (no cases).
OTHER_APPS = {
    "whatsapp": {
        "US": {"mau": 42129, "creators": 6223, "share_pct": 14.77},
        "IN": {"mau": 35121, "creators": 5197, "share_pct": 14.80},
        "BR": {"mau": 20996, "creators": 3187, "share_pct": 15.18},
        "ID": {"mau": 14114, "creators": 2090, "share_pct": 14.81},
        "MX": {"mau": 11170, "creators": 1625, "share_pct": 14.55},
        "GB": {"mau": 8425, "creators": 1218, "share_pct": 14.46},
        "DE": {"mau": 8423, "creators": 1178, "share_pct": 13.99},
    },
    "facebook": {
        "US": {"mau": 35449, "creators": 5221, "share_pct": 14.73},
        "IN": {"mau": 29287, "creators": 4384, "share_pct": 14.97},
        "BR": {"mau": 17663, "creators": 2626, "share_pct": 14.87},
        "ID": {"mau": 11728, "creators": 1707, "share_pct": 14.55},
        "MX": {"mau": 9258, "creators": 1379, "share_pct": 14.90},
        "GB": {"mau": 7157, "creators": 1066, "share_pct": 14.89},
        "DE": {"mau": 7008, "creators": 1046, "share_pct": 14.93},
    },
}


def _fail(msg):
    print(f"REFUSED: {msg}")
    raise SystemExit(2)


def _verify_app(con, app, stored, label):
    """Forms A == B exactly on MAU and creators per country, share
    within 0.01 of the stored 2-decimal percent; totals tie to the
    mau_march scalar and the creator_share creators count."""
    a = con.execute(P11_SQL_A.replace("'instagram'", f"'{app}'")).fetchall()
    b = con.execute(P11_SQL_B.replace("'instagram'", f"'{app}'")).fetchall()
    if a != b:
        _fail(f"{label}: form A {a} != form B {b}")
    got = {r[0]: {"mau": r[1], "creators": r[2], "share_pct": round(r[3], 2)} for r in a}
    if got != stored:
        _fail(f"{label}: values {got} != stored {stored}")
    for country, vals in stored.items():
        pct = [r[3] for r in a if r[0] == country][0]
        if abs(pct - vals["share_pct"]) > 0.01:
            _fail(f"{label} {country}: share {pct:.4f} != {vals['share_pct']}")
    tot_mau = sum(r[1] for r in a)
    tot_cre = sum(r[2] for r in a)
    print(f"Oracle verified: {label} MAU + creators by country, March 2026 -- "
          f"forms A == B exactly, {len(a)} countries, "
          f"totals {tot_mau:,} users / {tot_cre:,} creators "
          f"({100 * tot_cre / tot_mau:.2f}%).")
    return tot_mau, tot_cre


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA_DIR_DEFAULT)
    args = ap.parse_args()

    v7_singles = json.loads((ROOT / "evals/benchmark_v7.json").read_text())
    v7_convs = json.loads((ROOT / "evals/conversation_v7.json").read_text())
    v7_oracle = json.loads((ROOT / "evals/oracle_v7.json").read_text())
    v7_manifest = json.loads((ROOT / "evals/benchmark_v7_manifest.json").read_text())

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

    # p11: two forms agree; cross-check against the parent's
    # independent probe (p05 + p06 values, totals 210,809 / 31,297).
    tot_mau_ig, tot_cre_ig = _verify_app(con, "instagram", P11_VALUES, "p11 Instagram")
    if (tot_mau_ig, tot_cre_ig) != (210809, 31297):
        _fail(f"p11 totals {tot_mau_ig}/{tot_cre_ig} != 210809/31297")
    # Definition ties: per-country MAU must equal the p06 values and
    # per-country creators must equal the p05 values (same populations
    # the two earlier turns use, not two new ones that happen to agree).
    p06_mau = {c: v["mau"] for c, v in P11_VALUES.items()}
    p05_cre = {c: v["creators"] for c, v in P11_VALUES.items()}
    by_country_mau = dict(con.execute(
        """SELECT u.country, COUNT(DISTINCT s.user_id) FROM fact_sessions s
           JOIN dim_users u ON u.user_id = s.user_id
           WHERE CAST(s.start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'
           AND s.app = 'instagram' GROUP BY u.country""").fetchall())
    by_country_cre = dict(con.execute(
        """SELECT u.country, COUNT(DISTINCT f.creator_id) FROM fact_content_creation f
           JOIN dim_content c ON c.content_id = f.content_id
           JOIN dim_users u ON u.user_id = f.creator_id
           WHERE CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'
           AND c.app = 'instagram' GROUP BY u.country""").fetchall())
    if by_country_mau != p06_mau:
        _fail(f"p06 tie: MAU by country {by_country_mau} != stored {p06_mau}")
    if by_country_cre != p05_cre:
        _fail(f"p05 tie: creators by country {by_country_cre} != stored {p05_cre}")
    print("Definition ties verified: MAU column == p06 values, creators column == p05 values.")
    # WhatsApp and Facebook, same method (probed, no cases).
    tot_mau_wa, tot_cre_wa = _verify_app(con, "whatsapp", OTHER_APPS["whatsapp"], "WhatsApp (probe, no case)")
    if (tot_mau_wa, tot_cre_wa) != (140378, 20718):
        _fail(f"WhatsApp totals {tot_mau_wa}/{tot_cre_wa} != 140378/20718")
    tot_mau_fb, tot_cre_fb = _verify_app(con, "facebook", OTHER_APPS["facebook"], "Facebook (probe, no case)")
    if (tot_mau_fb, tot_cre_fb) != (117550, 17429):
        _fail(f"Facebook totals {tot_mau_fb}/{tot_cre_fb} != 117550/17429")

    # --- build v8 files
    p11 = {
        "category": "probe",
        "description": ("User probe, live shell 2026-11-11: 'I need the "
                        "Monthly active users and creators by country'. "
                        "Before the build, the question answered with MAU "
                        "by country only -- a wrong answer, not a "
                        "clarification: it dropped the 'creators' half "
                        "and routed to the mau_by_country turn. Expected: "
                        "the combined mau_creators_by_country turn -- one "
                        "breakdown with MAU, creators, and share per "
                        "country (US 63,372 / 9,286 14.65%, IN 52,686 / "
                        "7,843 14.89%, BR 31,623 / 4,785 15.13%, ID "
                        "21,081 / 3,149 14.94%, MX 16,946 / 2,488 "
                        "14.68%, DE 12,611 / 1,849 14.66%, GB 12,490 / "
                        "1,897 15.19%; totals 210,809 / 31,297 14.85%). "
                        "The MAU column is the mau_by_country "
                        "definition (p06) and the creators column is the "
                        "EXACT creators-by-country definition the p05 "
                        "oracle uses, so the columns sum to the "
                        "mau_march scalar and the creator_share creators "
                        "count. A monthly cut -- NOT the weather "
                        "report's daily creator penetration (daily "
                        "creators / DAU, ~10.4% for Instagram). Values "
                        "verified two independent ways (grouped "
                        "COUNT(DISTINCT) CTE vs DISTINCT-pairs CTE), "
                        "agreeing exactly on both counts per country."),
        "expected_metric": "mau_creators_by_country",
        "expected_rows": 7,
        "expected_type": "mau_creators_by_country",
        "expected_values": P11_VALUES,
        "family": "metric_scoping",
        "id": "p11",
        "origin": "user probe 2026-11-11",
        "question": "What are the monthly active users and creators by country for Instagram?",
    }
    singles = list(v7_singles) + [p11]

    # p12 turn-2 values: same per-country numbers as p11, share as a
    # fraction (the reply payload carries share as a fraction, like
    # mau_by_country's rows); the per-value keys are for the oracle's
    # conversation_values branch, which looks up case["checks"]
    # [check_name] for each oracle check (same pattern as p07/p10).
    p12_values = {c: {"mau": v["mau"], "creators": v["creators"],
                      "share": round(v["creators"] / v["mau"], 4)}
                  for c, v in P11_VALUES.items()}
    p12_checks = {"mau_creators_by_country_values": p12_values}
    for c, v in P11_VALUES.items():
        p12_checks[f"mau_creators_{c}_mau"] = v["mau"]
        p12_checks[f"mau_creators_{c}_creators"] = v["creators"]
    p12 = {
        "checks": p12_checks,
        "description": ("User probe, live shell 2026-11-11, follow-up "
                        "form (sticky MAU context): turn 1 is 'What is "
                        "the MAU for Instagram by country?' "
                        "(mau_by_country, sets the sticky MAU context "
                        "to Instagram); turn 2 is the live probe itself "
                        "-- 'I need the Monthly active users and "
                        "creators by country' (no app named). The "
                        "follow-up inherits the app from the sticky MAU "
                        "context and answers the combined breakdown "
                        "with the same p11 values -- MAU, creators, and "
                        "share per country -- not the MAU column alone. "
                        "Turn 2 values are the p11 values (same session "
                        "shape); the oracle checks them with independent "
                        "per-country scalar queries (DISTINCT-pairs "
                        "formulation for MAU, p05 formulation for "
                        "creators)."),
        "family": "multi_turn_context",
        "id": "p12",
        "origin": "user probe 2026-11-11 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "mau_by_country",
             "user": "What is the MAU for Instagram by country?"},
            {"expect_app": "instagram", "expect_type": "mau_creators_by_country",
             "user": "I need the Monthly active users and creators by country"}]}
    convs = v7_convs + [p12]

    # Oracle checks for p12: one scalar query per country per
    # population, using the independent formulations (p07's
    # DISTINCT-pairs form for MAU, p05's form for creators).
    p12_oracle_checks = {}
    for c in ("US", "IN", "BR", "ID", "MX", "GB", "DE"):
        p12_oracle_checks[f"mau_creators_{c}_mau"] = {
            "sql": (f"SELECT COUNT(*) AS value FROM (SELECT DISTINCT s.user_id FROM fact_sessions s "
                    f"JOIN dim_users u ON u.user_id = s.user_id WHERE s.app = 'instagram' AND "
                    f"u.country = '{c}' AND CAST(s.start_time AS DATE) BETWEEN DATE '2026-03-01' "
                    f"AND DATE '2026-03-31')"),
            "tolerance": 0}
        p12_oracle_checks[f"mau_creators_{c}_creators"] = {
            "sql": (f"SELECT COUNT(*) AS value FROM (SELECT DISTINCT f.creator_id FROM "
                    f"fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
                    f"JOIN dim_users u ON u.user_id = f.creator_id WHERE c.app = 'instagram' AND "
                    f"u.country = '{c}' AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-01' "
                    f"AND DATE '2026-03-31')"),
            "tolerance": 0}
    oracle_entries = v7_oracle["entries"] + [
        {"id": "p11", "kind": "breakdown",
         "note": ("Probe (user probe 2026-11-11, metric_scoping). MAU "
                  "and creators by country for Instagram, March 2026: "
                  "distinct users with >=1 session in March 2026 (the "
                  "MAU) and distinct creators with >=1 content item "
                  "published in March 2026, per country, plus the share "
                  "of MAU who are creators -- US 63,372 / 9,286 "
                  "(14.65%), IN 52,686 / 7,843 (14.89%), BR 31,623 / "
                  "4,785 (15.13%), ID 21,081 / 3,149 (14.94%), MX "
                  "16,946 / 2,488 (14.68%), DE 12,611 / 1,849 (14.66%), "
                  "GB 12,490 / 1,897 (15.19%); totals 210,809 / 31,297 "
                  "(14.85%). The oracle SQL is the DISTINCT-pairs CTE "
                  "formulation (form B), genuinely different in shape "
                  "from the planner's grouped-COUNT(DISTINCT) CTE "
                  "query (form A); the two agree exactly on both "
                  "counts per country, and the share within 0.01 of "
                  "the stored 2-decimal percent. The columns tie to "
                  "the earlier turns: per-country MAU equals the p06 "
                  "values exactly, per-country creators equals the "
                  "p05 values exactly, and the totals tie to the "
                  "mau_march scalar (210,809) and the creator_share "
                  "creators count (31,297). No aggregate table carries "
                  "MAU or creators by country (agg_user_daily has "
                  "daily DAU only), so the cross-check is two "
                  "independent raw formulations plus the p05/p06 ties, "
                  "not agg-vs-raw. Counts exact; share within the "
                  "harness 2% tolerance. Structural checks: the SQL "
                  "must be app-scoped on both populations and must "
                  "group by country, with no country filter the "
                  "question did not ask for. These numbers are a "
                  "different answer from MAU by country alone -- the "
                  "metric check (mau_creators_by_country, not "
                  "mau_by_country) and the 7-row-x-4-col shape check "
                  "are what stop the silent substitution."),
         "sql": P11_SQL_B,
         "structural": [
             {"label": "Combined SQL filters MAU to Instagram (app-scoped, not all-apps)",
              "contains": "s.app = 'instagram'"},
             {"label": "Combined SQL filters creators to Instagram (app-scoped, not all-apps)",
              "contains": "c.app = 'instagram'"},
             {"label": "Combined SQL groups by country (the cut the question asks for)",
              "contains": "GROUP BY u.country"},
             {"label": "Combined SQL must not contain a country filter the question did not ask for",
              "not_contains": "country = '"}]},
        {"id": "p12", "kind": "conversation_values",
         "note": ("Probe (user probe 2026-11-11, multi_turn_context, "
                  "follow-up). MAU and creators by country for "
                  "Instagram, March 2026, from the bare 'I need the "
                  "Monthly active users and creators by country' "
                  "follow-up after a MAU-by-country turn -- the same "
                  "p11 values, reached through the sticky MAU context "
                  "instead of the single-shot question. The oracle "
                  "SQLs are independent per-country scalar queries "
                  "(DISTINCT-pairs formulation for MAU, p05 "
                  "formulation for creators) that agree exactly with "
                  "the grouped forms, and the rows sum to the MAU "
                  "total of 210,809 and the creators total of 31,297. "
                  "Exact tolerance (counts). The case's "
                  "mau_creators_by_country_values carry the same "
                  "numbers for the harness's check_values combined "
                  "branch, which scores the turn-2 reply's rows."),
         "checks": p12_oracle_checks,
         "structural": [
             # "app = 'instagram'" (not "s.app = ..." or "c.app =
             # ..."): the check runs against EVERY turn's SQL, and
             # turn 1 is the mau_by_country query ("s.app =
             # 'instagram'") while turn 2 is the combined CTE
             # ("s.app = ..." and "c.app = ..."). The substring is
             # present in both turns' SQL; the turn-2 values check is
             # what pins the follow-up to the combined turn.
             {"label": "Follow-up combined SQL is app-scoped to Instagram (the probe inherits the app from the MAU-by-country turn)",
              "contains": "app = 'instagram'"}]},
    ]
    manifest_cases = list(v7_manifest["cases"])
    manifest_cases.append({"family": "metric_scoping", "id": "p11",
                           "kind": "single", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p12",
                           "kind": "conversation", "split": "dev"})

    assert len(singles) == 142, len(singles)
    assert len(convs) == 20, len(convs)
    assert len(oracle_entries) == 162, len(oracle_entries)
    assert len(manifest_cases) == 162, len(manifest_cases)
    dev = sum(1 for c in manifest_cases if c["split"] == "dev")
    held = sum(1 for c in manifest_cases if c["split"] == "heldout")
    assert (dev, held) == (132, 30), (dev, held)
    assert len({c["id"] for c in manifest_cases}) == 162
    assert {e["id"] for e in oracle_entries} == {c["id"] for c in manifest_cases}

    (ROOT / "evals/benchmark_v8.json").write_text(json.dumps(singles, indent=1) + "\n")
    (ROOT / "evals/conversation_v8.json").write_text(json.dumps(convs, indent=1) + "\n")
    (ROOT / "evals/oracle_v8.json").write_text(
        json.dumps({"entries": oracle_entries, "version": "v8"}, indent=1) + "\n")
    heldout_ids = sorted(c["id"] for c in manifest_cases if c["split"] == "heldout")
    manifest = {
        "benchmark": "socialpulse-nlq",
        "created": "2026-11-11",
        "data": v7_manifest["data"],
        "families": v7_manifest["families"],
        "heldout_proposal": {
            "heldout": heldout_ids,
            "note": ("v8 held-out (30): unchanged from v7 -- the new probe cases (p11, p12) are "
                     "in dev. HONEST LIMITATION: the probes were run during the build "
                     "(that is how the turn was verified), so like v1-v7, v8 held-out "
                     "means 'held out from fixes', not pristine. Truly fresh held-out "
                     "cases (designed without probing) have not arrived yet. The 120/30 "
                     "target split is exceeded at 162 cases (132 dev / 30 held-out)."),
            "rationale": "Different (metric, app, window) combinations plus hard cases held back entirely."},
        "rules": v7_manifest["rules"] + [
            "Probe cases p11/p12 are labeled with their origin (user probe 2026-11-11) in the case file and in docs/benchmark.md; they pin behavior the user actually saw in the live shell, not behavior designed in advance.",
            "Probe case p12 (bare 'I need the Monthly active users and creators by country' follow-up after a MAU-by-country turn) is labeled the same way; it pins the turn-2 behavior the user authorized as the follow-up form of the p11 probe."],
        "version": "v8",
        "cases": manifest_cases,
    }
    (ROOT / "evals/benchmark_v8_manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")
    print(f"Wrote v8: {len(singles)} singles, {len(convs)} conversations, "
          f"{len(oracle_entries)} oracle entries, {len(manifest_cases)} manifest cases "
          f"({dev} dev / {held} held-out).")


if __name__ == "__main__":
    main()
