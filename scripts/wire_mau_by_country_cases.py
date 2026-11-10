"""Wire the user probe (2026-11-10) into benchmark v6
(Phase 1, MAU-by-country turn).

Reads the v5 files and writes:
  evals/benchmark_v6.json            (138 v5 singles + p06)
  evals/conversation_v6.json         (17 v5 conversations + p07)
  evals/oracle_v6.json               (155 v5 entries + 2 probe entries)
  evals/benchmark_v6_manifest.json   (157 cases, 127 dev / 30 held-out)

The probe (live shell, Siddhartha's screenshot 2026-11-10):
  p06 (single, metric_scoping, dev): "What's the MAU for Instagram
      by country?" Before the build, the MAU turn clarified ("MAU is
      only defined per app, not broken down by country or surface")
      -- a hold from Nov 5. The user reversed it: MAU by country is
      well-defined (distinct users per country, March 2026,
      app-scoped, summing exactly to the MAU total), so the turn
      runs the query and shows the result for him to judge right or
      wrong. Expected: the mau_by_country turn, 7 rows, US 63,372,
      IN 52,686, BR 31,623, ID 21,081, MX 16,946, DE 12,611,
      GB 12,490, total 210,809. The MAU-by-country numbers are a
      different population from the creators-by-country breakdown
      (US 9,286, total 31,297) -- the system must not silently
      substitute one for the other.
  p07 (conversation, multi_turn_context, dev): turn 1 is "What is
      the current MAU for Instagram?" (MAU turn, 210,809, sets the
      session app to Instagram); turn 2 is the bare "by country".
      After a MAU turn, a bare "by country" (no topic named) cuts
      the MAU number just shown, using the sticky MAU context
      (same app, March 2026). A named topic ("creators by country")
      still takes the breakdown turn, as does "by country" after a
      breakdown.

Rules enforced here -- a failure aborts generation, nothing is written:
  1. Every oracle value is computed by executing independent SQL on the
     500K data. The planner/chat is never consulted for a value. The
     per-country MAU counts are verified TWO independent ways: form A
     is the direct GROUP BY (the shape the planner runs); form B is a
     DISTINCT-pairs CTE. The two must agree exactly, and the
     per-country counts must sum to the mau_march scalar for the same
     app (210,809 Instagram / 140,378 WhatsApp / 117,550 Facebook).
  2. No aggregate table carries MAU by country (agg_user_daily has
     daily DAU only), so unlike p02/p03 there is no agg-vs-raw
     cross-check here; the cross-check is two independent raw
     formulations plus the totals check. That limit is stated in the
     oracle note.
  3. Data pin: generation refuses unless the data is the frozen 500K
     seed-42 set (counts + max session date 2026-03-31).
  4. Counts: 139 singles, 18 conversations, 157 oracle entries,
     157 manifest cases, 127 dev / 30 held-out. The new cases are in
     dev; held-out is unchanged (30).

Usage: PYTHONPATH=src python scripts/wire_mau_by_country_cases.py [--data data/phase1]
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

# p06: MAU by country for Instagram, March 2026.
# Form A (direct GROUP BY -- the shape the planner runs).
P06_SQL_A = """SELECT u.country, COUNT(DISTINCT s.user_id) AS n FROM fact_sessions s
JOIN dim_users u ON u.user_id = s.user_id
WHERE CAST(s.start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND s.app = 'instagram'
GROUP BY u.country ORDER BY n DESC"""
# Form B (independent formulation): a DISTINCT-pairs CTE. Must agree
# with form A exactly.
P06_SQL_B = """WITH pairs AS (
  SELECT DISTINCT s.user_id, u.country FROM fact_sessions s
  JOIN dim_users u ON u.user_id = s.user_id
  WHERE CAST(s.start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND s.app = 'instagram')
SELECT country, COUNT(*) AS n FROM pairs GROUP BY country ORDER BY n DESC"""
P06_SQL_TOTAL = """SELECT COUNT(DISTINCT user_id) FROM fact_sessions
WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' AND app = 'instagram'"""

# Oracle check SQLs for p07 (the follow-up turn): one scalar query per
# country, using the independent DISTINCT-pairs CTE formulation,
# exact tolerance (counts, not rates).
P07_CHECK_SQL = {}
for _country in ("US", "IN", "BR", "ID", "MX", "GB", "DE"):
    P07_CHECK_SQL[f"mau_by_country_{_country}"] = (
        "SELECT COUNT(*) AS value FROM ("
        "SELECT DISTINCT s.user_id FROM fact_sessions s "
        "JOIN dim_users u ON u.user_id = s.user_id "
        f"WHERE s.app = 'instagram' AND u.country = '{_country}' "
        "AND CAST(s.start_time AS DATE) BETWEEN DATE '2026-03-01' "
        "AND DATE '2026-03-31')")


def _fail(msg):
    print(f"REFUSED: {msg}")
    raise SystemExit(2)


def _verify_app(con, app, expected, label):
    """Two independent formulations + the MAU-scalar totals check
    for one app's MAU-by-country split."""
    sql_a = P06_SQL_A.replace("'instagram'", f"'{app}'")
    sql_b = P06_SQL_B.replace("'instagram'", f"'{app}'")
    sql_total = P06_SQL_TOTAL.replace("'instagram'", f"'{app}'")
    rows_a = con.execute(sql_a).fetchall()
    rows_b = con.execute(sql_b).fetchall()
    total = con.execute(sql_total).fetchone()[0]
    vals_a = {r[0]: r[1] for r in rows_a}
    vals_b = {r[0]: r[1] for r in rows_b}
    if set(vals_a) != {"US", "IN", "BR", "ID", "MX", "GB", "DE"}:
        _fail(f"{label} countries: {sorted(vals_a)}, expected US/IN/BR/ID/MX/GB/DE")
    for country, val in vals_a.items():
        if val != vals_b.get(country):
            _fail(f"{label} {country}: form A {val} != form B {vals_b.get(country)}")
    if sum(vals_a.values()) != total:
        _fail(f"{label} sum {sum(vals_a.values())} != MAU total {total}")
    if vals_a != expected:
        _fail(f"{label} values {vals_a} do not match the verified turn probe {expected}")
    print(f"Oracle verified: {label} MAU by country, March 2026 -- "
          f"form A == form B exactly, rows sum to MAU total {total:,}: "
          + ", ".join(f"{c} {v:,}" for c, v in vals_a.items()) + ".")
    return vals_a, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA_DIR_DEFAULT)
    args = ap.parse_args()

    v5_singles = json.loads((ROOT / "evals/benchmark_v5.json").read_text())
    v5_convs = json.loads((ROOT / "evals/conversation_v5.json").read_text())
    v5_oracle = json.loads((ROOT / "evals/oracle_v5.json").read_text())
    v5_manifest = json.loads((ROOT / "evals/benchmark_v5_manifest.json").read_text())

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

    # p06: form A == form B exactly, rows sum to the MAU total.
    # Cross-check against the parent's independent turn probe of the
    # same question (US 63,372; IN 52,686; BR 31,623; ID 21,081;
    # MX 16,946; DE 12,611; GB 12,490; total 210,809).
    expected_ig = {"US": 63372, "IN": 52686, "BR": 31623, "ID": 21081,
                   "MX": 16946, "DE": 12611, "GB": 12490}
    vals_ig, total_ig = _verify_app(con, "instagram", expected_ig, "p06 Instagram")
    # WhatsApp is probed the same way (the chat tests pin it, and the
    # turn's app-switch follow-up re-runs the same query for it):
    # US 42,129; IN 35,121; BR 20,996; ID 14,114; MX 11,170;
    # GB 8,425; DE 8,423; total 140,378.
    expected_wa = {"US": 42129, "IN": 35121, "BR": 20996, "ID": 14114,
                   "MX": 11170, "GB": 8425, "DE": 8423}
    _verify_app(con, "whatsapp", expected_wa, "WhatsApp (probe, no case)")
    # Facebook, same method, for the record (total 117,550).
    expected_fb = {"US": 35449, "IN": 29287, "BR": 17663, "ID": 11728,
                   "MX": 9258, "GB": 7157, "DE": 7008}
    _verify_app(con, "facebook", expected_fb, "Facebook (probe, no case)")

    # --- build v6 files
    p06 = {
        "category": "probe",
        "description": ("User probe, live shell 2026-11-10: 'What's the MAU "
                        "for Instagram by country?' Before the build, the MAU "
                        "turn clarified ('MAU is only defined per app, not "
                        "broken down by country or surface') -- a hold from "
                        "Nov 5. The user reversed it: MAU by country is "
                        "well-defined (distinct users per country, March 2026, "
                        "app-scoped, summing exactly to the MAU total), so the "
                        "turn runs the query and shows the result for him to "
                        "judge right or wrong. Expected: the mau_by_country "
                        "turn, 7 rows, US 63,372, IN 52,686, BR 31,623, "
                        "ID 21,081, MX 16,946, DE 12,611, GB 12,490, total "
                        "210,809. These are a different population from the "
                        "creators-by-country breakdown (US 9,286, total "
                        "31,297) -- the system must not silently substitute "
                        "one for the other. Values verified two independent "
                        "ways (direct GROUP BY vs DISTINCT-pairs CTE), "
                        "agreeing exactly; rows sum to the MAU total."),
        "expected_metric": "mau_by_country",
        "expected_rows": 7,
        "expected_type": "mau_by_country",
        "expected_values": vals_ig,
        "family": "metric_scoping",
        "id": "p06",
        "origin": "user probe 2026-11-10",
        "question": "What's the MAU for Instagram by country?",
    }
    singles = list(v5_singles) + [p06]
    checks7 = {"mau_by_country_values": vals_ig}
    for country, val in vals_ig.items():
        checks7[f"mau_by_country_{country}"] = val
    p07 = {
        "checks": checks7,
        "description": ("User probe, live shell 2026-11-10, follow-up form "
                        "(sticky MAU context): turn 1 is 'What is the current "
                        "MAU for Instagram?' (MAU turn, 210,809, sets the "
                        "session app to Instagram); turn 2 is the bare 'by "
                        "country'. After a MAU turn, a bare 'by country' (no "
                        "topic named) cuts the MAU number just shown, using "
                        "the sticky MAU context (same app, March 2026): "
                        "distinct users per country, US 63,372, IN 52,686, "
                        "BR 31,623, ID 21,081, MX 16,946, DE 12,611, "
                        "GB 12,490, total 210,809. A named topic ('creators "
                        "by country') still takes the breakdown turn, as does "
                        "'by country' after a breakdown. Turn 2 values are "
                        "the p06 values (same session shape); the oracle "
                        "checks them with independent per-country scalar "
                        "queries (DISTINCT-pairs CTE formulation)."),
        "family": "multi_turn_context",
        "id": "p07",
        "origin": "user probe 2026-11-10 (follow-up)",
        "turns": [
            {"expect_app": "instagram", "expect_type": "mau",
             "user": "What is the current MAU for Instagram?"},
            {"expect_app": "instagram", "expect_type": "mau_by_country",
             "user": "by country"}]}
    convs = v5_convs + [p07]
    oracle_entries = v5_oracle["entries"] + [
        {"id": "p06", "kind": "breakdown",
         "note": ("Probe (user probe 2026-11-10, metric_scoping). Distinct "
                  "users per country for Instagram, March 2026, from the "
                  "mau_by_country planner template -- the same session-app "
                  "definition as mau_march, so the rows add up to the MAU "
                  "total of 210,809. The oracle SQL is the direct GROUP BY "
                  "form (the shape the turn runs); it agrees exactly with "
                  "the independent DISTINCT-pairs CTE formulation, and the "
                  "rows sum to the mau_march scalar. No aggregate table "
                  "carries MAU by country (agg_user_daily has daily DAU "
                  "only), so the cross-check is two independent raw "
                  "formulations plus the totals check, not agg-vs-raw. "
                  "Exact tolerance (counts). Structural checks: the SQL "
                  "must be app-scoped to Instagram and must not contain a "
                  "country filter the question did not ask for. These "
                  "numbers are a different population from the "
                  "creators-by-country breakdown (US 9,286, total 31,297) "
                  "-- the metric check (mau_by_country, not a breakdown "
                  "metric) is what stops a silent substitution."),
         "sql": P06_SQL_A,
         "structural": [
             {"label": "MAU-by-country SQL filters to Instagram (app-scoped, not all-apps)",
              "contains": "app = 'instagram'"},
             {"label": "MAU-by-country SQL groups by country (the cut the question asks for)",
              "contains": "GROUP BY u.country"},
             {"label": "MAU-by-country SQL must not contain a country filter the question did not ask for",
              "not_contains": "country = '"}]},
        {"id": "p07", "kind": "conversation_values",
         "note": ("Probe (user probe 2026-11-10, multi_turn_context, "
                  "follow-up). Distinct users per country for Instagram, "
                  "March 2026, from the bare 'by country' follow-up after "
                  "a MAU turn -- the same p06 values, reached through the "
                  "sticky MAU context instead of the single-shot question. "
                  "The oracle SQLs are independent per-country scalar "
                  "queries (DISTINCT-pairs CTE formulation) that agree "
                  "exactly with the direct GROUP BY form, and the rows "
                  "sum to the MAU total of 210,809. No aggregate table "
                  "carries MAU by country, so the cross-check is two "
                  "independent raw formulations plus the totals check, "
                  "not agg-vs-raw. Exact tolerance (counts). The case's "
                  "mau_by_country_values carry the same numbers for the "
                  "harness's check_values mau-by-country branch, which "
                  "scores the turn-2 reply's rows."),
         "checks": {name: {"sql": sql, "tolerance": 0}
                    for name, sql in P07_CHECK_SQL.items()},
         "structural": [
             {"label": "Follow-up MAU-by-country SQL is app-scoped to Instagram (the probe inherits the app from the MAU turn)",
              "contains": "app = 'instagram'"}]},
    ]
    manifest_cases = list(v5_manifest["cases"])
    manifest_cases.append({"family": "metric_scoping", "id": "p06",
                           "kind": "single", "split": "dev"})
    manifest_cases.append({"family": "multi_turn_context", "id": "p07",
                           "kind": "conversation", "split": "dev"})

    assert len(singles) == 139, len(singles)
    assert len(convs) == 18, len(convs)
    assert len(oracle_entries) == 157, len(oracle_entries)
    assert len(manifest_cases) == 157, len(manifest_cases)
    dev = sum(1 for c in manifest_cases if c["split"] == "dev")
    held = sum(1 for c in manifest_cases if c["split"] == "heldout")
    assert (dev, held) == (127, 30), (dev, held)
    assert len({c["id"] for c in manifest_cases}) == 157
    assert {e["id"] for e in oracle_entries} == {c["id"] for c in manifest_cases}

    (ROOT / "evals/benchmark_v6.json").write_text(json.dumps(singles, indent=1) + "\n")
    (ROOT / "evals/conversation_v6.json").write_text(json.dumps(convs, indent=1) + "\n")
    (ROOT / "evals/oracle_v6.json").write_text(
        json.dumps({"entries": oracle_entries, "version": "v6"}, indent=1) + "\n")
    heldout_ids = sorted(c["id"] for c in manifest_cases if c["split"] == "heldout")
    manifest = {
        "benchmark": "socialpulse-nlq",
        "created": "2026-11-10",
        "data": v5_manifest["data"],
        "families": v5_manifest["families"],
        "heldout_proposal": {
            "heldout": heldout_ids,
            "note": ("v6 held-out (30): unchanged from v5 -- the new probe cases (p06, p07) are "
                     "in dev. HONEST LIMITATION: the probes were run during the build "
                     "(that is how the turn was verified), so like v1-v5, v6 held-out "
                     "means 'held out from fixes', not pristine. Truly fresh held-out "
                     "cases (designed without probing) have not arrived yet. The 120/30 "
                     "target split is exceeded at 157 cases (127 dev / 30 held-out)."),
            "rationale": "Different (metric, app, window) combinations plus hard cases held back entirely."},
        "rules": v5_manifest["rules"] + [
            "Probe case p06 is labeled with its origin (user probe 2026-11-10) in the case file and in docs/benchmark.md; it pins behavior the user actually saw in the live shell, not behavior designed in advance.",
            "Probe case p07 (bare 'by country' follow-up after a MAU turn) is labeled the same way; it pins the turn-2 behavior the user authorized as the follow-up form of the p06 probe."],
        "version": "v6",
        "cases": manifest_cases,
    }
    (ROOT / "evals/benchmark_v6_manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")
    print(f"Wrote v6: {len(singles)} singles, {len(convs)} conversations, "
          f"{len(oracle_entries)} oracle entries, {len(manifest_cases)} manifest cases "
          f"({dev} dev / {held} held-out).")


if __name__ == "__main__":
    main()
