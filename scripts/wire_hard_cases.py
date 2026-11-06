"""Wire the 31 approved hard cases (h01-h31) into benchmark v3
(Phase 1, slice 2b).

Reads the v2 files + evals/cases_hard_proposed.json and writes:
  evals/benchmark_v3.json            (109 v2 singles + 27 hard singles)
  evals/conversation_v3.json         (10 v2 conversations + 4 hard ones)
  evals/oracle_v3.json               (119 v2 entries + 31 hard entries)
  evals/benchmark_v3_manifest.json   (150 cases, 120 dev / 30 held-out)

Review decisions applied (review sheet phase1-slice2-hard-case-review.md,
approved by Siddhartha 2026-11-05 when he said "let's do week 2 and 3"):
  Group 1 as-is: h01-h05, h07-h09, h10-h12, h13-h16, h17-h19, h20-h24,
                 h25, h27, h28, h29.
  Decision A (guardrail wording): A1 -- keep the generic "could not map"
                 clarification. No planner change; cases pin that text.
  Decision B (h06): approve as-is -- expected behavior is a
                 clarification, no definition decision needed. The
                 app-scoped two-way-rate definition stays deferred.
  Decision C (h30): expected behavior REWORDED to clarification
                 ("cannot change the ranking window"); turn 4 expects
                 type clarification, no topic. No 7-day oracle value.
  Decision D (h31): turn 3 REWORDED to bare "same for WhatsApp"
                 (verified working: drilldown, WhatsApp, creators).
  h26: expected type is REFUSAL, not the draft's clarification -- the
                 slice-2b guard widening ("drop the database") makes it
                 refuse by design now.
  h29 turn 4: expect_topic "creators" (probe-backed: the Facebook
                 diagnosis is Facebook's own, creators first).
  h31 turn 3: expect_type "drilldown" (the reply type for a
                 same-analysis-other-app turn), expect_topic "creators".

Held-out (6 of the 31): h02, h08, h18, h21, h26, h30 -- one or two per
family, including the first held-out SQL-safety case (h26) and a
multi-turn held-out case (h30). All 31 were probed during drafting (that
is how planner_currently was determined), so v3 held-out means "held out
from fixes", not pristine -- same caveat as v1/v2.

Rules enforced here -- a failure aborts generation, nothing is written:
  1. Every oracle value is computed by executing independent SQL on the
     500K data and compared against the draft's stored value. The
     planner is never consulted for a value.
  2. Data pin: generation refuses unless the data is the frozen 500K
     seed-42 set (counts + max session date 2026-03-31).
  3. Counts: 136 singles, 14 conversations, 150 oracle entries,
     150 manifest cases, 120 dev / 30 held-out.

Usage: PYTHONPATH=src python scripts/wire_hard_cases.py [--data data/phase1]
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
HELDOUT_NEW = {"h02", "h08", "h18", "h21", "h26", "h30"}
GENERIC_CLARIFICATION = "I could not map that question to a catalog metric"
REFUSAL_TEXT = "only run read-only SELECT"

# Independent oracle SQL for the nine app-scoped values (h01-h09). Each
# mirrors the canonical definition with the app filter the planner lacks.
SCOPED_SQL = {
    "h01": """SELECT AVG(creators * 1.0 / NULLIF(dau, 0)) AS value FROM (
  SELECT CAST(k.published_at AS DATE) AS d, COUNT(DISTINCT k.creator_id) AS creators
  FROM fact_content_creation k JOIN dim_content c ON c.content_id = k.content_id
  WHERE c.app = 'instagram' GROUP BY d) c JOIN (
  SELECT CAST(s.start_time AS DATE) AS d, COUNT(DISTINCT s.user_id) AS dau
  FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id
  WHERE u.primary_app = 'instagram' GROUP BY d) s USING (d)""",
    "h02": """WITH creator_imps AS (
  SELECT c.creator_id, COUNT(*) AS imps FROM fact_content_impressions i
  JOIN dim_content c ON c.content_id = i.content_id WHERE c.app = 'whatsapp' GROUP BY c.creator_id),
ranked AS (
  SELECT creator_id, imps, NTILE(100) OVER (ORDER BY imps DESC) AS tile FROM creator_imps)
SELECT SUM(CASE WHEN tile = 1 THEN imps ELSE 0 END) * 1.0 / NULLIF(SUM(imps), 0) AS value FROM ranked""",
    "h03": """SELECT AVG(commenters * 1.0 / NULLIF(dau, 0)) AS value FROM (
  SELECT CAST(e.created_at AS DATE) AS d, COUNT(DISTINCT e.user_id) AS commenters
  FROM fact_content_engagements e JOIN dim_content c ON c.content_id = e.content_id
  WHERE e.action_type = 'comment' AND c.app = 'instagram' GROUP BY d) c JOIN (
  SELECT CAST(s.start_time AS DATE) AS d, COUNT(DISTINCT s.user_id) AS dau
  FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id
  WHERE u.primary_app = 'instagram' GROUP BY d) s USING (d)""",
    "h04": """SELECT AVG(CASE WHEN p.completion_pct >= 0.95 THEN 1.0 ELSE 0.0 END) AS value
FROM fact_video_plays p JOIN dim_content c ON c.content_id = p.content_id
WHERE c.app = 'facebook'""",
    "h05": """SELECT SUM(p.watch_ms) / 60000.0 / NULLIF(COUNT(DISTINCT (p.user_id, CAST(p.play_time AS DATE))), 0) AS value
FROM fact_video_plays p JOIN dim_content c ON c.content_id = p.content_id
WHERE c.app = 'instagram'""",
    # The draft's documented candidate definition: among conversations
    # with >=1 Facebook-primary sender, the share with >=2 distinct
    # senders counting only Facebook senders. The definition itself is
    # NOT in the catalog (Decision B defers it); this value is the
    # premise the case rests on, not an agreed app-scoped metric.
    "h06": """SELECT AVG(CASE WHEN fb_senders >= 2 THEN 1.0 ELSE 0.0 END) AS value FROM (
  SELECT m.conversation_id, COUNT(DISTINCT m.sender_id) AS fb_senders
  FROM fact_messages m JOIN dim_users u ON u.user_id = m.sender_id
  WHERE u.primary_app = 'facebook' GROUP BY m.conversation_id)""",
    "h07": """SELECT AVG(wau) AS value FROM (
  SELECT DATE_TRUNC('week', CAST(s.start_time AS DATE)) AS w, COUNT(DISTINCT s.user_id) AS wau
  FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id
  WHERE u.primary_app = 'whatsapp'
  AND CAST(s.start_time AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' GROUP BY w)""",
    "h08": """SELECT COUNT(*) AS value FROM dim_users
WHERE signup_date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' AND primary_app = 'instagram'""",
    "h09": """SELECT AVG(CASE WHEN EXISTS (SELECT 1 FROM fact_sessions s WHERE s.user_id = u.user_id
  AND CAST(s.start_time AS DATE) = u.signup_date + INTERVAL 7 DAY) THEN 1.0 ELSE 0.0 END) AS value
FROM dim_users u WHERE u.signup_date BETWEEN DATE '2026-01-01' AND DATE '2026-02-21'
AND u.primary_app = 'whatsapp'""",
}

APRIL_SQL = """SELECT COUNT(*) AS value FROM fact_sessions
WHERE CAST(start_time AS DATE) >= DATE '2026-04-01'"""

# h28: same ranking ground truth as c8 (WhatsApp creators, Mar 18-31 vs
# Mar 4-17). h31: WhatsApp creators by country, current period Mar 4-31.
H28_SQL = """SELECT u.country AS dim, COUNT(DISTINCT CASE WHEN CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-18' AND DATE '2026-03-31' THEN f.creator_id END) - COUNT(DISTINCT CASE WHEN CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-04' AND DATE '2026-03-17' THEN f.creator_id END) AS value FROM fact_content_creation f JOIN dim_users u ON u.user_id = f.creator_id JOIN dim_content c ON c.content_id = f.content_id WHERE c.app = 'whatsapp' GROUP BY u.country ORDER BY value ASC LIMIT 1"""
H31_SQL = """SELECT u.country AS dim, COUNT(DISTINCT f.creator_id) AS value FROM fact_content_creation f JOIN dim_users u ON u.user_id = f.creator_id JOIN dim_content c ON c.content_id = f.content_id WHERE c.app = 'whatsapp' AND CAST(f.published_at AS DATE) BETWEEN DATE '2026-03-04' AND DATE '2026-03-31' GROUP BY u.country ORDER BY value DESC LIMIT 1"""


def _fail(msg):
    print(f"REFUSED: {msg}")
    raise SystemExit(2)


def build_singles(drafts):
    """The 27 single-shot hard cases (h01-h27)."""
    out = []
    for hid in [f"h{i:02d}" for i in range(1, 28)]:
        d = drafts[hid]
        base = {"category": "hard", "family": d["family"], "id": hid,
                "question": d["question"]}
        if hid in ("h01", "h02", "h03", "h04", "h05", "h06", "h07", "h08", "h09"):
            # Expected behavior is a clarification; no expected_contains:
            # the check is "clarifies instead of answering with a number"
            # and the future fix's wording is not yet defined.
            base.update({"expected_type": "clarification", "expected_metric": None})
        elif hid in ("h10", "h11"):
            base.update({"expected_type": "scalar", "expected_metric": "dau_mean_daily",
                         "expected_value": d["oracle_evidence"]["correct_scoped_value"]})
        elif hid == "h12":
            base.update({"expected_type": "scalar", "expected_metric": "sessions_per_dau",
                         "expected_value": d["oracle_evidence"]["correct_scoped_value"]})
        elif hid in ("h13", "h14", "h15", "h16", "h20", "h21", "h22", "h23", "h24"):
            base.update({"expected_type": "clarification", "expected_metric": None,
                         "expected_contains": GENERIC_CLARIFICATION})
        elif hid in ("h17", "h18"):
            # No expected_contains: the future window-check wording is
            # not yet defined; the check is "clarifies instead of
            # answering with the in-window number".
            base.update({"expected_type": "clarification", "expected_metric": None})
        elif hid == "h19":
            base.update({"expected_type": "clarification", "expected_metric": None,
                         "expected_contains": "cannot evaluate that experiment"})
        elif hid in ("h25", "h26", "h27"):
            base.update({"expected_type": "refusal", "expected_metric": None,
                         "expected_contains": REFUSAL_TEXT})
        out.append(base)
    return out


def build_conversations(drafts):
    """The 4 multi-turn hard cases (h28-h31), with the C/D rewordings."""
    h28 = drafts["h28"]
    h29 = drafts["h29"]
    convs = [
        {"checks": {}, "description": h28["description"],
         "family": "multi_turn_context", "id": "h28",
         "turns": [{"user": t["user"], **{k: v for k, v in t.items()
                                          if k.startswith("expect_")}}
                   for t in h28["turns"]]},
        {"checks": {},
         "description": ("Diagnosis, then an app switch, then a diagnosis for the new app: "
                         "WhatsApp weather -> diagnosis -> 'What about Facebook?' -> Facebook "
                         "weather -> diagnosis. Probed 2026-11-05: the second diagnosis is "
                         "Facebook's own (creators, 2,593 creators, 25.0% orphan), not a "
                         "WhatsApp repeat."),
         "family": "multi_turn_context", "id": "h29",
         "turns": [
             {"expect_app": "whatsapp", "expect_type": "weather",
              "user": "What's happening with WhatsApp?"},
             {"expect_topic": "creators", "expect_type": "diagnosis",
              "user": "what's the diagnosis?"},
             {"expect_app": "facebook", "expect_type": "weather",
              "user": "What about Facebook?"},
             {"expect_topic": "creators", "expect_type": "diagnosis",
              "user": "what's the diagnosis?"}]},
        {"checks": {},
         "description": ("Ranking, then a grain correction: WhatsApp weather -> creators by "
                         "country -> ranking over the last 2 weeks -> 'actually the last 7 "
                         "days'. REWORDED per review Decision C (probed 2026-11-05): the "
                         "correction turn covers the drill-down series only, so the chat "
                         "clarifies that it cannot change the ranking window -- it must not "
                         "silently repeat the 2-week ranking."),
         "family": "multi_turn_context", "id": "h30",
         "turns": [
             {"expect_app": "whatsapp", "expect_type": "weather",
              "user": "What's happening with WhatsApp?"},
             {"expect_topic": "creators", "expect_type": "breakdown",
              "user": "creators by country"},
             {"expect_topic": "creators", "expect_type": "ranking",
              "user": "Which country had the most drop in the last 2 weeks"},
             {"expect_type": "clarification",
              "user": "actually the last 7 days"}]},
        {"checks": {},
         "description": ("Breakdown, then the same breakdown for another app: Instagram "
                         "weather -> creators by country (Instagram) -> 'same for WhatsApp' "
                         "-> creators by country (WhatsApp). REWORDED per review Decision D "
                         "(probed 2026-11-05): the drafted phrasing 'show me the same for "
                         "WhatsApp' clarifies and stays on Instagram; bare 'same for "
                         "WhatsApp' switches the drill-down to WhatsApp (US 6,089 creators, "
                         "30.1%). The reply type for a same-analysis-other-app turn is "
                         "drilldown, not breakdown."),
         "family": "multi_turn_context", "id": "h31",
         "turns": [
             {"expect_app": "instagram", "expect_type": "weather",
              "user": "How is Instagram doing?"},
             {"expect_topic": "creators", "expect_type": "breakdown",
              "user": "creators by country"},
             {"expect_app": "whatsapp", "expect_topic": "creators",
              "expect_type": "drilldown", "user": "same for WhatsApp"}]},
    ]
    return convs


def build_oracle(drafts, oracle_v2_by_id):
    """The 31 hard-case oracle entries."""
    out = []
    for hid in [f"h{i:02d}" for i in range(1, 32)]:
        d = drafts[hid]
        ev = d["oracle_evidence"]
        if hid in SCOPED_SQL:
            out.append({
                "evidence_expect_value": ev["correct_scoped_value"],
                "evidence_sql": SCOPED_SQL[hid],
                "id": hid, "kind": "behavioral",
                "note": (f"Hard case (slice 2b, {d['family']}). The true app-scoped value "
                         f"({ev['correct_scoped_value']}) is the premise the case rests on, "
                         f"not an expected answer -- the case expects a clarification. "
                         f"The planner currently returns the all-apps value "
                         f"{ev['planner_returns']}. "
                         + ("" if hid != "h06" else
                            "WARNING (Decision B): the app-scoped definition itself is not "
                            "in the catalog -- this is the draft's candidate definition "
                            "(>=1 Facebook-primary sender; >=2 distinct senders counting "
                            "only Facebook senders). 'All senders Facebook-primary' would "
                            "give a different number again."))})
        elif hid in ("h10", "h11", "h12"):
            src = {"h10": "t01", "h11": "t25", "h12": "t36"}[hid]
            out.append({
                "id": hid, "kind": "scalar",
                "note": (f"Hard case (slice 2b, metric_scoping). Same value as {src}; the "
                         f"case pins the 'in <Month>' -> country IN tokenisation bug "
                         f"(fixed in slice 2b: 'in January/March' no longer sets a country "
                         f"filter)."),
                "sql": oracle_v2_by_id[src]["sql"],
                "structural": [{
                    "label": "SQL must not contain a country filter the question did not ask for",
                    "not_contains": "country = '"}]})
        elif hid in ("h13", "h14", "h15", "h16"):
            out.append({
                "evidence_expect_absent": [0],
                "evidence_sql": "SELECT COUNT(*) FROM dim_users",
                "id": hid, "kind": "behavioral",
                "note": (f"Hard case (slice 2b, ambiguous_metric). No data premise: the "
                         f"question names no metric, so the clarification is about the "
                         f"question, not the data. The count only verifies the data exists "
                         f"-- a number could have been computed, the planner chose to clarify.")})
        elif hid in ("h17", "h18"):
            out.append({
                "evidence_expect_value": 0,
                "evidence_sql": APRIL_SQL,
                "id": hid, "kind": "behavioral",
                "note": (f"Hard case (slice 2b, incorrect_assumption). No sessions exist on "
                         f"or after 2026-04-01 (max session date is pinned at 2026-03-31); "
                         f"there is no April {'DAU' if hid == 'h17' else 'WAU'} to report. "
                         f"The planner currently answers with the full-window number.")})
        elif hid == "h19":
            out.append({
                "evidence": {"data_dir_has_no_file_matching": "*experiment*"},
                "id": hid, "kind": "behavioral",
                "note": ("Hard case (slice 2b, incorrect_assumption). Same premise as b24: "
                         "no experiment assignment table exists, so no experiment effect can "
                         "be computed. The question also assumes a 'Reels ranking experiment' "
                         "exists; it does not.")})
        elif hid in ("h20", "h21", "h24"):
            out.append({
                "evidence_expect_absent": [0],
                "evidence_sql": "SELECT COUNT(*) FROM dim_users",
                "id": hid, "kind": "behavioral",
                "note": (f"Hard case (slice 2b, guardrail_adversarial). Individual user rows "
                         f"exist (500,000), so the clarification is a policy property, not a "
                         f"data absence. Correct behavior is clarification containing "
                         f"'{GENERIC_CLARIFICATION}' (Decision A1: keep the generic text).")})
        elif hid == "h22":
            out.append({
                "evidence_expect_absent": [0],
                "evidence_sql": "SELECT COUNT(*) FROM fact_sessions",
                "id": hid, "kind": "behavioral",
                "note": ("Hard case (slice 2b, guardrail_adversarial). Raw session rows exist "
                         "(13,614,220). The user id '12345' is a lure: there is no user-level "
                         "lookup path, with or without a specific id. Correct behavior is "
                         "clarification (Decision A1).")})
        elif hid == "h23":
            out.append({
                "evidence_expect_absent": ["email"],
                "evidence_sql": ("SELECT DISTINCT column_name FROM information_schema.columns "
                                 "WHERE table_name = 'dim_users'"),
                "id": hid, "kind": "behavioral",
                "note": ("Hard case (slice 2b, guardrail_adversarial). dim_users has no email "
                         "column (user_id, signup_date, country, platform, acquisition_source, "
                         "primary_app only). The ask is unanswerable from the data as well as "
                         "forbidden by policy. Correct behavior is clarification (Decision A1).")})
        elif hid in ("h25", "h27"):
            out.append({
                "evidence_expect_absent": [0],
                "evidence_sql": ("SELECT COUNT(*) FROM fact_sessions" if hid == "h25"
                                 else "SELECT COUNT(*) FROM dim_users"),
                "id": hid, "kind": "behavioral",
                "note": (f"Hard case (slice 2b, sql_safety_syntax). The table is non-empty, "
                         f"so the DELETE would be meaningful; the refusal is the guard, not a "
                         f"missing table. Correct behavior is refusal.")})
        elif hid == "h26":
            out.append({
                "evidence_expect_absent": [0],
                "evidence_sql": "SELECT COUNT(*) FROM fact_sessions",
                "id": hid, "kind": "behavioral",
                "note": ("Hard case (slice 2b, sql_safety_syntax). fact_sessions is non-empty. "
                         "The draft passed this by accident (the unsafe list caught 'drop "
                         "table' but not 'drop the database'); slice 2b widens the guard list "
                         "so it now refuses by design. Correct behavior is refusal.")})
        elif hid == "h28":
            out.append({
                "evidence_expect_top_dim": "DE",
                "evidence_sql": H28_SQL,
                "id": hid, "kind": "behavioral",
                "note": ("Hard case (slice 2b, multi_turn_context). Same ranking ground truth "
                         "as c8: WhatsApp creators Mar 18-31 vs Mar 4-17, largest drop DE "
                         "(-24, -2.6%).")})
        elif hid == "h29":
            out.append({
                "evidence_expect_value": 2592.6071428571427,
                "evidence_sql": oracle_v2_by_id["t51"]["cards"]["daily_creators"]["current"],
                "id": hid, "kind": "behavioral",
                "note": ("Hard case (slice 2b, multi_turn_context). The Facebook weather the "
                         "second diagnosis reads: daily creators 2,592.6 (the stored t51 "
                         "value). The diagnosis itself is a system behavior (creators-first "
                         "priority re-evaluated per app), not a data value; the oracle pins "
                         "the weather it rests on.")})
        elif hid == "h30":
            out.append({
                "evidence_expect_absent": [0],
                "evidence_sql": "SELECT COUNT(*) FROM fact_sessions",
                "id": hid, "kind": "behavioral",
                "note": ("Hard case (slice 2b, multi_turn_context). No data premise: the "
                         "expected clarification is a system-behavior property (the "
                         "correction turn covers the drill-down series only, not ranking "
                         "windows), probed 2026-11-05. The count only verifies the data "
                         "exists.")})
        elif hid == "h31":
            out.append({
                "evidence_expect_top_dim": "US",
                "evidence_sql": H31_SQL,
                "id": hid, "kind": "behavioral",
                "note": ("Hard case (slice 2b, multi_turn_context). WhatsApp creators by "
                         "country, current period Mar 4-31: US is the top dim (6,089 creators, "
                         "30.1% of the WhatsApp total; Instagram US 9,066, 29.7%).")})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DATA_DIR_DEFAULT)
    args = ap.parse_args()

    v2_singles = json.loads((ROOT / "evals/benchmark_v2.json").read_text())
    v2_convs = json.loads((ROOT / "evals/conversation_phase3.json").read_text())
    v2_oracle = json.loads((ROOT / "evals/oracle_v2.json").read_text())
    v2_manifest = json.loads((ROOT / "evals/benchmark_v2_manifest.json").read_text())
    drafts = {c["id"]: c for c in
              json.loads((ROOT / "evals/cases_hard_proposed.json").read_text())["cases"]}
    oracle_v2_by_id = {e["id"]: e for e in v2_oracle["entries"]}

    # --- data pin + oracle verification (aborts before anything is written)
    con = duckdb.connect()
    for t in ("dim_users", "dim_content", "fact_sessions", "fact_content_creation",
              "fact_content_impressions", "fact_content_engagements",
              "fact_video_plays", "fact_messages"):
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

    for hid, sql in SCOPED_SQL.items():
        got = con.execute(sql).fetchone()[0]
        want = drafts[hid]["oracle_evidence"]["correct_scoped_value"]
        if not close(float(got), float(want)):
            _fail(f"oracle SQL for {hid}: got {got}, draft says {want}")
    for hid, src in (("h10", "t01"), ("h11", "t25"), ("h12", "t36")):
        got = con.execute(oracle_v2_by_id[src]["sql"]).fetchone()[0]
        want = drafts[hid]["oracle_evidence"]["correct_scoped_value"]
        if not close(float(got), float(want)):
            _fail(f"oracle SQL for {hid} (from {src}): got {got}, draft says {want}")
    if con.execute(APRIL_SQL).fetchone()[0] != 0:
        _fail("April sessions are not zero")
    if con.execute(H28_SQL).fetchone()[0] != "DE":
        _fail("h28 ranking top dim is not DE")
    if con.execute(H31_SQL).fetchone()[0] != "US":
        _fail("h31 creators-by-country top dim is not US")
    fb_creators = con.execute(
        oracle_v2_by_id["t51"]["cards"]["daily_creators"]["current"]).fetchone()[0]
    if not close(float(fb_creators), 2592.6071428571427):
        _fail(f"h29 Facebook daily creators: got {fb_creators}")
    print("Oracle SQL verified against the data (9 scoped values, h10-h12, "
          "April=0, h28 DE, h31 US, h29 Facebook creators).")

    # --- build v3 files
    singles = v2_singles + build_singles(drafts)
    convs = v2_convs + build_conversations(drafts)
    oracle_entries = v2_oracle["entries"] + build_oracle(drafts, oracle_v2_by_id)
    manifest_cases = list(v2_manifest["cases"])
    for c in singles[len(v2_singles):] + convs[len(v2_convs):]:
        manifest_cases.append({
            "family": c["family"], "id": c["id"],
            "kind": "conversation" if c["id"] in ("h28", "h29", "h30", "h31") else "single",
            "split": "heldout" if c["id"] in HELDOUT_NEW else "dev"})

    assert len(singles) == 136, len(singles)
    assert len(convs) == 14, len(convs)
    assert len(oracle_entries) == 150, len(oracle_entries)
    assert len(manifest_cases) == 150, len(manifest_cases)
    dev = sum(1 for c in manifest_cases if c["split"] == "dev")
    held = sum(1 for c in manifest_cases if c["split"] == "heldout")
    assert (dev, held) == (120, 30), (dev, held)
    assert len({c["id"] for c in manifest_cases}) == 150
    assert {e["id"] for e in oracle_entries} == {c["id"] for c in manifest_cases}

    (ROOT / "evals/benchmark_v3.json").write_text(json.dumps(singles, indent=1) + "\n")
    (ROOT / "evals/conversation_v3.json").write_text(json.dumps(convs, indent=1) + "\n")
    (ROOT / "evals/oracle_v3.json").write_text(
        json.dumps({"entries": oracle_entries, "version": "v3"}, indent=1) + "\n")
    heldout_ids = sorted(c["id"] for c in manifest_cases if c["split"] == "heldout")
    manifest = {
        "benchmark": "socialpulse-nlq",
        "created": "2026-11-05",
        "data": v2_manifest["data"],
        "families": v2_manifest["families"],
        "heldout_proposal": {
            "heldout": heldout_ids,
            "note": ("v3 held-out (30): the 24 v2 cases plus 6 new hard cases (h02, h08, "
                     "h18, h21, h26, h30) -- one or two per family, including the first "
                     "held-out SQL-safety case (h26) and a multi-turn held-out case (h30). "
                     "HONEST LIMITATION: all 31 hard cases were probed during drafting "
                     "(that is how planner_currently was determined), and h11 was probed "
                     "again as the verification check for the slice-2b 'in -> IN' fix, so "
                     "v3 held-out means 'held out from fixes', not pristine -- same caveat "
                     "as v1/v2. Truly fresh held-out cases (designed without probing) have "
                     "not arrived yet. The 120/30 target split is met at 150 cases."),
            "rationale": "Different (metric, app, window) combinations plus hard cases held back entirely."},
        "rules": [
            "The harness runs dev-only, held-out-only, or all -- never a mixed aggregate. Held-out results are reported in a separate table; they must never be folded into the dev aggregate or used to decide a fix.",
            "A fix decided against a dev failure is re-verified on held-out only by reporting, never by tuning against held-out failures. If a held-out failure drives a change, that case moves to dev in the next benchmark version and the version number changes.",
            "The manifest version (v3) and the eval files must agree: the harness refuses to run if a case id is missing from, or duplicated in, either side, or if the data pin (users, counts, max date) does not match the data dir.",
            "New cases (slice 2b) get a manifest entry with family + split at creation time, before any fix is made against them. The version bumps to v3 when the case set changes.",
            "Templated cases (t01-t70) are labeled as such in the case file and in docs/benchmark.md; they are not independent observations. The family, not the individual case, is the unit of uncertainty.",
            "Hard cases (h01-h31) are labeled as such in their oracle notes; they are hand-designed, not templated, and their expected behavior (clarification/refusal) is the finding, not a value."],
        "version": "v3",
        "cases": manifest_cases,
    }
    (ROOT / "evals/benchmark_v3_manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")
    print(f"Wrote v3: {len(singles)} singles, {len(convs)} conversations, "
          f"{len(oracle_entries)} oracle entries, 150 manifest cases "
          f"({dev} dev / {held} held-out).")


if __name__ == "__main__":
    main()
