"""Generate the v2 templated benchmark cases (Phase 1, slice 2, part A).

Reads the frozen v1 eval files, adds ~70 templated single-shot cases
(t01-t70), and writes the v2 files alongside v1:

    evals/benchmark_v2.json            (109 single-shot cases: 39 v1 + 70 new)
    evals/oracle_v2.json               (119 oracle entries: 49 v1 + 70 new)
    evals/benchmark_v2_manifest.json   (119 cases, 95 dev / 24 held-out)

The v1 files are read, never modified. The conversations file is reused
as-is (it already carries the corrected c8 description); v2 therefore
references evals/conversation_phase3.json, not a copy.

Rules this script enforces (a failure aborts generation, nothing is
written):

  1. Expected values come ONLY from the independent oracle SQL written
     in this file. The planner is never consulted for a value.
  2. plan() is used ONLY as a wording guard: the question must map to
     the intended metric / plan type. A question that maps elsewhere
     means the wording is wrong, and generation stops. (This is a
     case-design check, not a value check.)
  3. Mean/ratio scalars get a second-form cross-check: the same
     definition computed a different way (a different SQL shape, or
     the pre-aggregated agg_user_daily / agg_surface_daily tables)
     must agree within tolerance. A disagreement is a finding about
     the oracle SQL, and generation stops.
  4. The data pin must match the v1 manifest (500K users, seed 42,
     max session date 2026-03-31) or generation refuses.

Template matrix (70 cases; see docs/benchmark.md for the full table):
  metric_scoping            A1-A8   22 cases  (DAU/MAU/stickiness/sessions/
                                                median/messaging by app,
                                                DAU trend by app, D7 by
                                                acquisition)
  aggregation_grain         B1-B3, B5-B7  14   (all-apps means x windows)
  complex_joins_breakdowns  C1-C9   13   (by country/platform/app series
                                                and breakdowns)
  ambiguous_metric          D1-D2    2   (weather for instagram/facebook)
  incorrect_assumption      E1-E5    5   (TikTok, churn, invalid country)
  guardrail_adversarial     F1-F8    8   (individual-level / raw-data asks)
  sql_safety_syntax         G1-G6    6   (write / DDL asks -> refusal)
  multi_turn_context          -      0   (cannot be templated; hand-designed
                                                drafts only, see
                                                evals/cases_hard_proposed.json)

Split rule: held-out is drawn from different (metric, app, window)
combinations than dev, and is fixed in the template definitions below
(not random, so a re-run produces the same split). 14 of the 70 new
cases are held-out, bringing the v2 totals to 95 dev / 24 held-out.

Phrasing rule, learned from the probe: NEVER write "in <Month>" in a
question for a metric whose planner SQL does country filtering
(dau, sessions_per_dau, median_session_duration, messaging_dau).
The word "in" is read as the country code IN (India), so "What is the
mean daily DAU for instagram in January?" silently returns
India-only Instagram DAU. Windowed questions here therefore use
"for January" or "the January mean daily DAU". The "in <Month>" trap
itself is a hand-designed hard draft (h10-h12), not a templated case.

Usage:
    PYTHONPATH=src python scripts/generate_cases.py [--data data/phase1]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb  # noqa: E402

from socialpulse_nlq.planner import plan  # noqa: E402  (wording guard only)

WINDOWS = {
    "january": ("2026-01-01", "2026-01-31", 31),
    "february": ("2026-02-01", "2026-02-28", 28),
    "march": ("2026-03-01", "2026-03-31", 31),
    "last7": ("2026-03-25", "2026-03-31", 7),
    "last28": ("2026-03-04", "2026-03-31", 28),
}
MARCH = ("2026-03-01", "2026-03-31", 31)
APPS = ["instagram", "whatsapp", "facebook"]
# Weather windows, relative to the data end (2026-03-31), mirroring b37.
WX_CUR = ("2026-03-04", "2026-03-31")
WX_PRI = ("2026-02-04", "2026-03-03")
D7_CUR = ("2026-02-25", "2026-03-24")
D7_PRI = ("2026-01-28", "2026-02-23")
D7_COHORT = ("2026-01-01", "2026-02-21")

TABLES = ["fact_sessions", "fact_content_impressions", "fact_content_engagements",
          "fact_video_plays", "fact_messages", "fact_content_creation",
          "dim_users", "dim_content", "agg_user_daily", "agg_surface_daily"]


class GenerationError(Exception):
    """A template, oracle SQL, or cross-check disagrees. Nothing is written."""


def close(a, b, rel=0.02):
    return abs(float(a) - float(b)) <= max(rel * abs(float(b)), 0.01)


# --------------------------------------------------------------------------
# Oracle SQL builders (hand-written from the metric definitions, never
# from planner.build_sql). Each returns (oracle_sql, crosscheck_sql|None).
# --------------------------------------------------------------------------

def _join_u(app):
    return " JOIN dim_users u ON u.user_id = s.user_id" if app else ""


def _where_u(app, ds, de):
    f = f" AND u.primary_app = '{app}'" if app else ""
    return (f"WHERE CAST(s.start_time AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}'{f}")


def dau_scalar(app, ds, de, ndays):
    join = " JOIN dim_users u ON u.user_id = s.user_id" if app else ""
    filt = f" AND u.primary_app = '{app}'" if app else ""
    base = (f"FROM fact_sessions s{join} WHERE CAST(s.start_time AS DATE) "
            f"BETWEEN DATE '{ds}' AND DATE '{de}'{filt}")
    oracle = (f"SELECT COUNT(DISTINCT (s.user_id, CAST(s.start_time AS DATE))) "
              f"* 1.0 / {ndays} AS value {base}")
    # Second form: mean of the daily distinct counts. Equal to form A
    # exactly when every day in the window has at least one user (true
    # at 500K scale); a disagreement fails generation, it is not papered over.
    cross = (f"SELECT AVG(dau) AS value FROM (SELECT CAST(s.start_time AS DATE) AS d, "
             f"COUNT(DISTINCT s.user_id) AS dau {base} GROUP BY d)")
    return oracle, cross


def wau_scalar(ds, de):
    sql = (f"SELECT AVG(wau) AS value FROM (SELECT DATE_TRUNC('week', "
           f"CAST(start_time AS DATE)) AS w, COUNT(DISTINCT user_id) AS wau "
           f"FROM fact_sessions WHERE CAST(start_time AS DATE) BETWEEN DATE '{ds}' "
           f"AND DATE '{de}' GROUP BY w)")
    return sql, None  # the alternative form is algebraically identical; no real check


def new_users(ds, de):
    return (f"SELECT COUNT(*) AS value FROM dim_users WHERE signup_date "
            f"BETWEEN DATE '{ds}' AND DATE '{de}'", None)


def mau(app):
    return (f"SELECT COUNT(DISTINCT user_id) AS value FROM fact_sessions WHERE "
            f"CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' "
            f"AND app = '{app}'", None)


def stickiness(app):
    # Numerator as user-days / 31 (independent of the AVG-of-daily form).
    oracle = (f"SELECT (SELECT COUNT(DISTINCT (user_id, CAST(start_time AS DATE))) "
              f"* 1.0 / 31 FROM fact_sessions WHERE CAST(start_time AS DATE) BETWEEN "
              f"DATE '2026-03-01' AND DATE '2026-03-31' AND app = '{app}') / NULLIF("
              f"(SELECT COUNT(DISTINCT user_id) FROM fact_sessions WHERE "
              f"CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' "
              f"AND app = '{app}'), 0) AS value")
    # Cross-check: mean of daily DAU over the MAU (the b39 shape).
    cross = (f"SELECT AVG(dau) / NULLIF((SELECT COUNT(DISTINCT user_id) FROM "
             f"fact_sessions WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' "
             f"AND DATE '2026-03-31' AND app = '{app}'), 0) AS value FROM "
             f"(SELECT CAST(start_time AS DATE) AS d, COUNT(DISTINCT user_id) AS dau "
             f"FROM fact_sessions WHERE CAST(start_time AS DATE) BETWEEN "
             f"DATE '2026-03-01' AND DATE '2026-03-31' AND app = '{app}' GROUP BY d)")
    return oracle, cross


def sessions_per_dau(app, ds, de):
    join = " JOIN dim_users u ON u.user_id = s.user_id" if app else ""
    filt = f" AND u.primary_app = '{app}'" if app else ""
    oracle = (f"SELECT COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT (s.user_id, "
              f"CAST(s.start_time AS DATE))), 0) AS value FROM fact_sessions s{join} "
              f"WHERE CAST(s.start_time AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}'{filt}")
    # Cross-check from the pre-aggregated table: sessions summed over
    # user-days, divided by the user-day count. A different path to the
    # same ratio; tolerance is the harness 2% (agg values are stored as
    # doubles), not float-exact.
    afilt = f" AND app = '{app}'" if app else ""
    cross = (f"SELECT SUM(sessions) * 1.0 / NULLIF(COUNT(*), 0) AS value "
             f"FROM agg_user_daily WHERE date BETWEEN DATE '{ds}' AND DATE '{de}'{afilt}")
    return oracle, cross


def median_duration(app, ds, de):
    join = " JOIN dim_users u ON u.user_id = s.user_id" if app else ""
    filt = f" AND u.primary_app = '{app}'" if app else ""
    return (f"SELECT MEDIAN(s.duration_minutes) AS value FROM fact_sessions s{join} "
            f"WHERE CAST(s.start_time AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}'{filt}",
            None)  # MEDIAN has one canonical computation; no second form


def daily_creators(app, ds, de, ndays):
    join = " JOIN dim_content c ON c.content_id = k.content_id" if app else ""
    filt = f" AND c.app = '{app}'" if app else ""
    base = (f"FROM fact_content_creation k{join} WHERE CAST(k.published_at AS DATE) "
            f"BETWEEN DATE '{ds}' AND DATE '{de}'{filt}")
    oracle = (f"SELECT COUNT(DISTINCT (k.creator_id, CAST(k.published_at AS DATE))) "
              f"* 1.0 / {ndays} AS value {base}")
    cross = (f"SELECT AVG(creators) AS value FROM (SELECT CAST(k.published_at AS DATE) "
             f"AS d, COUNT(DISTINCT k.creator_id) AS creators {base} GROUP BY d)")
    return oracle, cross


def messaging_dau(app, ds, de, ndays):
    join = " JOIN dim_users u ON u.user_id = m.sender_id" if app else ""
    filt = f" AND u.primary_app = '{app}'" if app else ""
    base = (f"FROM fact_messages m{join} WHERE CAST(m.sent_at AS DATE) "
            f"BETWEEN DATE '{ds}' AND DATE '{de}'{filt}")
    oracle = (f"SELECT COUNT(DISTINCT (m.sender_id, CAST(m.sent_at AS DATE))) "
              f"* 1.0 / {ndays} AS value {base}")
    cross = (f"SELECT AVG(senders) AS value FROM (SELECT CAST(m.sent_at AS DATE) AS d, "
             f"COUNT(DISTINCT m.sender_id) AS senders {base} GROUP BY d)")
    return oracle, cross


def d7_by_acquisition(source):
    # Same cohort window as b19 (signup Jan 1 - Feb 21, so D7 lands by Feb 28).
    return (f"SELECT AVG(CASE WHEN retained THEN 1.0 ELSE 0.0 END) AS value FROM "
            f"(SELECT u.user_id, EXISTS (SELECT 1 FROM fact_sessions s WHERE "
            f"s.user_id = u.user_id AND CAST(s.start_time AS DATE) = "
            f"u.signup_date + INTERVAL 7 DAY) AS retained FROM dim_users u WHERE "
            f"u.signup_date BETWEEN DATE '{D7_COHORT[0]}' AND DATE '{D7_COHORT[1]}' "
            f"AND u.acquisition_source = '{source}')", None)


def dau_series(app, ds, de):
    join = " JOIN dim_users u ON u.user_id = s.user_id" if app else ""
    filt = f" AND u.primary_app = '{app}'" if app else ""
    return (f"SELECT CAST(s.start_time AS DATE) AS date, COUNT(DISTINCT s.user_id) "
            f"AS value FROM fact_sessions s{join} WHERE CAST(s.start_time AS DATE) "
            f"BETWEEN DATE '{ds}' AND DATE '{de}'{filt} GROUP BY date ORDER BY date")


def dau_series_by_dim(dim, app, ds, de):
    col = {"app": "u.primary_app", "country": "u.country",
           "platform": "u.platform"}[dim]
    filt = f" AND u.primary_app = '{app}'" if app else ""
    return (f"SELECT {col} AS dim, CAST(s.start_time AS DATE) AS d, "
            f"COUNT(DISTINCT s.user_id) AS value FROM fact_sessions s "
            f"JOIN dim_users u ON u.user_id = s.user_id WHERE "
            f"CAST(s.start_time AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}'{filt} "
            f"GROUP BY {col}, d ORDER BY dim, d")


def messaging_series_by_dim(dim, ds, de):
    col = {"app": "u.primary_app", "country": "u.country",
           "platform": "u.platform"}[dim]
    return (f"SELECT {col} AS dim, CAST(m.sent_at AS DATE) AS d, "
            f"COUNT(DISTINCT m.sender_id) AS value FROM fact_messages m "
            f"JOIN dim_users u ON u.user_id = m.sender_id WHERE "
            f"CAST(m.sent_at AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}' "
            f"GROUP BY {col}, d ORDER BY dim, d")


def creators_series_by_dim(ds, de):
    return (f"SELECT c.app AS dim, CAST(k.published_at AS DATE) AS d, "
            f"COUNT(DISTINCT k.creator_id) AS value FROM fact_content_creation k "
            f"JOIN dim_content c ON c.content_id = k.content_id WHERE "
            f"CAST(k.published_at AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}' "
            f"GROUP BY c.app, d ORDER BY dim, d")


def sessions_breakdown(dim):
    col = {"country": "u.country", "app": "u.primary_app"}[dim]
    return (f"SELECT dim, SUM(sessions) * 1.0 / NULLIF(SUM(user_days), 0) "
            f"AS value FROM (SELECT {col} AS dim, CAST(s.start_time AS DATE) AS d, "
            f"COUNT(*) AS sessions, COUNT(DISTINCT s.user_id) AS user_days "
            f"FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id WHERE "
            f"CAST(s.start_time AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' "
            f"GROUP BY {col}, d) GROUP BY dim ORDER BY dim")


def sessions_breakdown_cross(dim):
    col = {"country": "country", "app": "app"}[dim]
    return (f"SELECT {col} AS dim, SUM(sessions) * 1.0 / NULLIF(COUNT(*), 0) "
            f"AS value FROM agg_user_daily GROUP BY {col} ORDER BY dim")


def engagement_breakdown():
    return (f"SELECT app, SUM(eng) * 1.0 / NULLIF(SUM(imp), 0) AS value FROM "
            f"(SELECT i.app AS app, COUNT(*) AS imp, 0 AS eng FROM "
            f"fact_content_impressions i GROUP BY i.app UNION ALL "
            f"SELECT c.app AS app, 0 AS imp, COUNT(*) AS eng FROM "
            f"fact_content_engagements e JOIN dim_content c ON c.content_id = e.content_id "
            f"GROUP BY c.app) GROUP BY app ORDER BY app")


def engagement_breakdown_cross():
    return ("SELECT app, SUM(engagements) * 1.0 / NULLIF(SUM(impressions), 0) "
            "AS value FROM agg_surface_daily GROUP BY app ORDER BY app")


def weather_cards(app):
    """The five b37 card SQLs with the app substituted."""
    uf = f" AND u.primary_app = '{app}'"
    cf = f" AND c.app = '{app}'"
    cards = {
        "dau": {
            "current": (f"SELECT AVG(dau) AS value FROM (SELECT CAST(s.start_time AS DATE) "
                        f"AS d, COUNT(DISTINCT s.user_id) AS dau FROM fact_sessions s "
                        f"JOIN dim_users u ON u.user_id = s.user_id WHERE "
                        f"CAST(s.start_time AS DATE) BETWEEN DATE '{WX_CUR[0]}' AND "
                        f"DATE '{WX_CUR[1]}'{uf} GROUP BY d)"),
            "prior": (f"SELECT AVG(dau) AS value FROM (SELECT CAST(s.start_time AS DATE) "
                      f"AS d, COUNT(DISTINCT s.user_id) AS dau FROM fact_sessions s "
                      f"JOIN dim_users u ON u.user_id = s.user_id WHERE "
                      f"CAST(s.start_time AS DATE) BETWEEN DATE '{WX_PRI[0]}' AND "
                      f"DATE '{WX_PRI[1]}'{uf} GROUP BY d)"),
        },
        "d7_retention": {
            "current": (f"SELECT AVG(CASE WHEN retained THEN 1.0 ELSE 0.0 END) AS value "
                        f"FROM (SELECT u.user_id, EXISTS (SELECT 1 FROM fact_sessions s "
                        f"WHERE s.user_id = u.user_id AND CAST(s.start_time AS DATE) = "
                        f"u.signup_date + INTERVAL 7 DAY) AS retained FROM dim_users u "
                        f"WHERE u.signup_date BETWEEN DATE '{D7_CUR[0]}' AND "
                        f"DATE '{D7_CUR[1]}'{uf})"),
            "prior": (f"SELECT AVG(CASE WHEN retained THEN 1.0 ELSE 0.0 END) AS value "
                      f"FROM (SELECT u.user_id, EXISTS (SELECT 1 FROM fact_sessions s "
                      f"WHERE s.user_id = u.user_id AND CAST(s.start_time AS DATE) = "
                      f"u.signup_date + INTERVAL 7 DAY) AS retained FROM dim_users u "
                      f"WHERE u.signup_date BETWEEN DATE '{D7_PRI[0]}' AND "
                      f"DATE '{D7_PRI[1]}'{uf})"),
        },
        "daily_creators": {
            "current": (f"SELECT AVG(c) AS value FROM (SELECT CAST(f.published_at AS DATE) "
                        f"AS d, COUNT(DISTINCT f.creator_id) AS c FROM fact_content_creation f "
                        f"JOIN dim_content c ON c.content_id = f.content_id WHERE "
                        f"CAST(f.published_at AS DATE) BETWEEN DATE '{WX_CUR[0]}' AND "
                        f"DATE '{WX_CUR[1]}'{cf} GROUP BY d)"),
        },
        "messaging_dau": {
            "current": (f"SELECT AVG(s) AS value FROM (SELECT CAST(m.sent_at AS DATE) AS d, "
                        f"COUNT(DISTINCT m.sender_id) AS s FROM fact_messages m "
                        f"JOIN dim_users u ON u.user_id = m.sender_id WHERE "
                        f"CAST(m.sent_at AS DATE) BETWEEN DATE '{WX_CUR[0]}' AND "
                        f"DATE '{WX_CUR[1]}'{uf} GROUP BY d)"),
        },
        "orphan_post_rate": {
            "current": (f"SELECT AVG(CASE WHEN engaged THEN 0.0 ELSE 1.0 END) AS value "
                        f"FROM (SELECT c.content_id, EXISTS (SELECT 1 FROM "
                        f"fact_content_engagements e WHERE e.content_id = c.content_id) "
                        f"AS engaged FROM dim_content c WHERE c.app = '{app}')"),
        },
    }
    return cards


def derive_watchouts(cards):
    """Watchout phrases from oracle card values, using the documented
    rules in weather.py (counts move on 2% relative, rates on 1 point;
    orphan > 20%). Re-implemented here, not imported, so the oracle does
    not share code with the thing it checks. Only the watchouts
    derivable from the five oracle cards are returned -- exactly the
    b37 limitation (no creators prior, so no 'Creator supply softening';
    no engagement/top-1% cards)."""
    def direction(kind, cur, pri):
        if cur is None or pri in (None, 0):
            return "flat"
        if kind == "rate":
            pp = (cur - pri) * 100
            return "up" if pp >= 1.0 else ("down" if pp <= -1.0 else "flat")
        pct = (cur - pri) / pri * 100
        return "up" if pct >= 2.0 else ("down" if pct <= -2.0 else "flat")

    out = []
    dau, d7 = cards.get("dau"), cards.get("d7_retention")
    if dau and direction("count", dau.get("current"), dau.get("prior")) == "down":
        out.append("Growth falling")
    if d7 and direction("rate", d7.get("current"), d7.get("prior")) == "down":
        out.append("Retention falling")
    orph = cards.get("orphan_post_rate", {})
    if orph.get("current") and orph["current"] > 0.20:
        out.append("Creator problem")
    return out


# --------------------------------------------------------------------------
# Template definitions.
# Each spec: id, question, family, split, category, expected_type,
# expected_metric, oracle_kind, oracle_sql (+crosscheck), and the stored
# expectation shape. "scalar" stores expected_value; "series" stores
# expected_values (in row order); "breakdown" stores expected_rows +
# expected_values; "weather" stores expected_cards + watchouts;
# "behavioral" stores expected_contains (clarification) or nothing
# (refusal: type check only -- see the note in the harness, a refusal
# has no clarification text to match against).
# --------------------------------------------------------------------------

def _scalar(cid, q, family, split, metric, sql, cross=None, structural=None):
    return {"id": cid, "question": q, "family": family, "split": split,
            "category": "simple", "expected_type": "scalar",
            "expected_metric": metric, "oracle_kind": "scalar",
            "oracle_sql": sql, "crosscheck_sql": cross,
            "structural": structural,
            "note": f"Templated slice-2 case ({family})."}


def _series(cid, q, family, split, metric, sql, min_rows):
    return {"id": cid, "question": q, "family": family, "split": split,
            "category": "simple", "expected_type": "series",
            "expected_metric": metric, "oracle_kind": "series",
            "oracle_sql": sql, "min_rows": min_rows,
            "note": f"Templated slice-2 case ({family})."}


def _series_by_dim(cid, q, family, split, metric, sql, min_rows, dims):
    return {"id": cid, "question": q, "family": family, "split": split,
            "category": "segments", "expected_type": "series_by_dim",
            "expected_metric": metric, "oracle_kind": "series",
            "oracle_sql": sql, "min_rows": min_rows, "expected_dim_count": dims,
            "note": f"Templated slice-2 case ({family})."}


def _breakdown(cid, q, family, split, metric, sql, rows, cross=None):
    return {"id": cid, "question": q, "family": family, "split": split,
            "category": "segments", "expected_type": "breakdown",
            "expected_metric": metric, "oracle_kind": "breakdown",
            "oracle_sql": sql, "crosscheck_sql": cross, "expected_rows": rows,
            "note": f"Templated slice-2 case ({family})."}


def _behavioral(cid, q, family, split, etype, contains, evidence_sql=None,
                evidence_absent=None, evidence_note="", category="ambiguous_invalid"):
    return {"id": cid, "question": q, "family": family, "split": split,
            "category": category,
            "expected_type": etype, "expected_metric": None,
            "oracle_kind": "behavioral", "expected_contains": contains,
            "evidence_sql": evidence_sql, "evidence_expect_absent": evidence_absent,
            "note": f"Templated slice-2 case ({family}). {evidence_note}"}


def _app_needle(metric, app):
    """Structural needle for the app filter in the planner's SQL, per
    metric (verified against the planner's SQL shapes, 2026-11-05)."""
    if metric in ("dau_mean_daily", "sessions_per_dau", "median_session_duration_min",
                  "messaging_dau_mean"):
        return f"u.primary_app = '{app}'"
    if metric in ("mau_march", "dau_mau_stickiness"):
        return f"app = '{app}'"
    return None


def _struct(metric, app, min_count=1):
    needle = _app_needle(metric, app)
    if not needle:
        return None
    return [{"contains": needle,
             "label": f"{metric} SQL filters to {app} (app-scoped, not all-apps)",
             "min_count": min_count}]


def build_specs():
    specs = []
    # -- A. metric_scoping (22) -------------------------------------------
    # A1: DAU scalar, app x window (6). "for January" phrasing: never
    # "in January" (the "in" -> country IN trap; see module docstring).
    for i, (app, win) in enumerate([("instagram", "january"), ("instagram", "march"),
                                    ("whatsapp", "january"), ("whatsapp", "march"),
                                    ("facebook", "january"), ("facebook", "march")]):
        ds, de, nd = WINDOWS[win]
        sql, cross = dau_scalar(app, ds, de, nd)
        specs.append(_scalar(f"t{1 + i:02d}",
                             f"What is the {win.capitalize()} mean daily DAU for {app}?",
                             "metric_scoping", "heldout" if (app, win) == ("facebook", "march") else "dev",
                             "dau_mean_daily", sql, cross,
                             _struct("dau_mean_daily", app)))
    # A2: MAU by app (2).
    for i, app in enumerate(["whatsapp", "facebook"]):
        sql, _ = mau(app)
        specs.append(_scalar(f"t{7 + i:02d}", f"What is the MAU for {app}?",
                             "metric_scoping", "heldout" if app == "facebook" else "dev",
                             "mau_march", sql, None, _struct("mau_march", app)))
    # A3: stickiness by app (2).
    for i, app in enumerate(["whatsapp", "facebook"]):
        sql, cross = stickiness(app)
        specs.append(_scalar(f"t{9 + i:02d}", f"What is DAU/MAU stickiness for {app}?",
                             "metric_scoping", "heldout" if app == "facebook" else "dev",
                             "dau_mau_stickiness", sql, cross,
                             _struct("dau_mau_stickiness", app, min_count=2)))
    # A4: sessions per DAU by app (3), full 90-day window.
    for i, app in enumerate(APPS):
        ds, de = "2026-01-01", "2026-03-31"
        sql, cross = sessions_per_dau(app, ds, de)
        specs.append(_scalar(f"t{11 + i:02d}", f"What is sessions per DAU for {app}?",
                             "metric_scoping", "heldout" if app == "facebook" else "dev",
                             "sessions_per_dau", sql, cross,
                             _struct("sessions_per_dau", app)))
    # A5: median session duration by app (3).
    for i, app in enumerate(APPS):
        sql, _ = median_duration(app, "2026-01-01", "2026-03-31")
        specs.append(_scalar(f"t{14 + i:02d}",
                             f"What is the median session duration for {app}?",
                             "metric_scoping", "heldout" if app == "facebook" else "dev",
                             "median_session_duration_min", sql, None,
                             _struct("median_session_duration_min", app)))
    # A6: messaging DAU by app (3).
    for i, app in enumerate(APPS):
        sql, cross = messaging_dau(app, "2026-01-01", "2026-03-31", 90)
        specs.append(_scalar(f"t{17 + i:02d}", f"What is messaging DAU for {app}?",
                             "metric_scoping", "heldout" if app == "facebook" else "dev",
                             "messaging_dau_mean", sql, cross,
                             _struct("messaging_dau_mean", app)))
    # A7: DAU trend by app, last 28 days (1 series).
    ds, de, _ = WINDOWS["last28"]
    specs.append(_series("t20", "What is the DAU trend for instagram over the last 28 days?",
                         "metric_scoping", "dev", "dau_mean_daily",
                         dau_series("instagram", ds, de), 28))
    # A8: D7 retention by acquisition (2; b19 covers paid).
    for i, src in enumerate(["organic", "referral"]):
        sql, _ = d7_by_acquisition(src)
        s = _scalar(f"t{21 + i:02d}", f"What is D7 retention for {src} users?",
                    "metric_scoping", "dev", "d7_retention_by_acquisition", sql)
        s["category"] = "cohorts"
        s["expected_type"] = "breakdown"  # single-value breakdown, b19 shape
        s["oracle_kind"] = "scalar"
        s["expected_rows"] = 1
        s["breakdown_key"] = src
        specs.append(s)
    # -- B. aggregation_grain (14) -----------------------------------------
    # B1: DAU all-apps x window (3).
    for i, win in enumerate(["january", "february", "march"]):
        ds, de, nd = WINDOWS[win]
        sql, cross = dau_scalar(None, ds, de, nd)
        specs.append(_scalar(f"t{23 + i:02d}",
                             f"What is the {win.capitalize()} mean daily DAU?",
                             "aggregation_grain", "heldout" if win == "march" else "dev",
                             "dau_mean_daily", sql, cross))
    # B2: WAU all-apps x window (2).
    for i, win in enumerate(["january", "march"]):
        ds, de, _ = WINDOWS[win]
        sql, _ = wau_scalar(ds, de)
        specs.append(_scalar(f"t{26 + i:02d}", f"What is WAU for {win.capitalize()}?",
                             "aggregation_grain", "dev", "wau_mean_weekly", sql))
    # B3: new users x window (3).
    for i, win in enumerate(["january", "february", "march"]):
        ds, de, _ = WINDOWS[win]
        sql, _ = new_users(ds, de)
        specs.append(_scalar(f"t{28 + i:02d}",
                             f"How many new users were there for {win.capitalize()}?",
                             "aggregation_grain", "heldout" if win == "march" else "dev",
                             "new_users_in_window", sql))
    # B5: median duration all-apps x window (2).
    for i, win in enumerate(["january", "march"]):
        ds, de, _ = WINDOWS[win]
        sql, _ = median_duration(None, ds, de)
        specs.append(_scalar(f"t{31 + i:02d}",
                             f"What is the median session duration for {win.capitalize()}?",
                             "aggregation_grain", "dev", "median_session_duration_min", sql))
    # B6: daily creators all-apps x window (2).
    for i, win in enumerate(["january", "march"]):
        ds, de, nd = WINDOWS[win]
        sql, cross = daily_creators(None, ds, de, nd)
        specs.append(_scalar(f"t{33 + i:02d}",
                             f"What is daily creators for {win.capitalize()}?",
                             "aggregation_grain", "dev", "daily_creators_mean", sql, cross))
    # B7: sessions per DAU all-apps x window (2).
    for i, win in enumerate(["january", "march"]):
        ds, de, _ = WINDOWS[win]
        sql, cross = sessions_per_dau(None, ds, de)
        specs.append(_scalar(f"t{35 + i:02d}",
                             f"What is sessions per DAU for {win.capitalize()}?",
                             "aggregation_grain", "dev", "sessions_per_dau", sql, cross))
    # -- C. complex_joins_breakdowns (13) ----------------------------------
    ds28, de28, _ = WINDOWS["last28"]
    ds7, de7, _ = WINDOWS["last7"]
    # C1: DAU by country, whatsapp, 28d (1).
    specs.append(_series_by_dim("t37", "What was DAU for whatsapp by country over the last 28 days?",
                                "complex_joins_breakdowns", "dev", "dau_mean_daily",
                                dau_series_by_dim("country", "whatsapp", ds28, de28), 190, 7))
    # C2: DAU by platform, app x 3.
    for i, app in enumerate(APPS):
        specs.append(_series_by_dim(f"t{38 + i:02d}",
                                    f"What was DAU for {app} by platform over the last 28 days?",
                                    "complex_joins_breakdowns",
                                    "heldout" if app == "facebook" else "dev",
                                    "dau_mean_daily",
                                    dau_series_by_dim("platform", app, ds28, de28), 80, 3))
    # C3: DAU by app, 7d.
    specs.append(_series_by_dim("t41", "What was DAU by app over the last 7 days?",
                                "complex_joins_breakdowns", "dev", "dau_mean_daily",
                                dau_series_by_dim("app", None, ds7, de7), 20, 3))
    # C4: DAU by app, January.
    dsj, dej, _ = WINDOWS["january"]
    specs.append(_series_by_dim("t42", "What was DAU by app for January?",
                                "complex_joins_breakdowns", "dev", "dau_mean_daily",
                                dau_series_by_dim("app", None, dsj, dej), 90, 3))
    # C5: DAU by country, 7d.
    specs.append(_series_by_dim("t43", "What was DAU by country over the last 7 days?",
                                "complex_joins_breakdowns", "dev", "dau_mean_daily",
                                dau_series_by_dim("country", None, ds7, de7), 45, 7))
    # C6: sessions per DAU by dim (2).
    specs.append(_breakdown("t44", "What is sessions per DAU by country?",
                            "complex_joins_breakdowns", "dev", "sessions_per_dau",
                            sessions_breakdown("country"), 7,
                            sessions_breakdown_cross("country")))
    specs.append(_breakdown("t45", "What is sessions per DAU by app?",
                            "complex_joins_breakdowns", "heldout", "sessions_per_dau",
                            sessions_breakdown("app"), 3,
                            sessions_breakdown_cross("app")))
    # C7: messaging DAU by dim, 28d (2).
    specs.append(_series_by_dim("t46", "What is messaging DAU by country over the last 28 days?",
                                "complex_joins_breakdowns", "dev", "messaging_dau_mean",
                                messaging_series_by_dim("country", ds28, de28), 190, 7))
    specs.append(_series_by_dim("t47", "What is messaging DAU by platform over the last 28 days?",
                                "complex_joins_breakdowns", "heldout", "messaging_dau_mean",
                                messaging_series_by_dim("platform", ds28, de28), 80, 3))
    # C8: daily creators by app, 7d.
    specs.append(_series_by_dim("t48", "What are daily creators by app over the last 7 days?",
                                "complex_joins_breakdowns", "dev", "daily_creators_mean",
                                creators_series_by_dim(ds7, de7), 20, 3))
    # C9: engagement rate by app.
    specs.append(_breakdown("t49", "What is engagement rate by app?",
                            "complex_joins_breakdowns", "dev",
                            "engagement_rate_per_impression",
                            engagement_breakdown(), 3, engagement_breakdown_cross()))
    # -- D. ambiguous_metric (2): weather reports ---------------------------
    # Expected cards + watchouts are computed from the oracle cards below.
    for i, app in enumerate(["instagram", "facebook"]):
        specs.append({"id": f"t{50 + i:02d}",
                      "question": f"How is {app.capitalize()} doing recently?",
                      "family": "ambiguous_metric",
                      "split": "heldout" if app == "facebook" else "dev",
                      "category": "weather_report", "expected_type": "weather_report",
                      "expected_metric": None, "oracle_kind": "weather",
                      "weather_app": app,
                      "note": "Templated slice-2 case (ambiguous_metric)."})
    # -- E. incorrect_assumption (5) ----------------------------------------
    e_cases = [
        ("t52", "What is the MAU for TikTok?", "dev",
         "TikTok is not in the Phase 1 synthetic universe",
         "SELECT DISTINCT primary_app FROM dim_users ORDER BY 1", ["tiktok"],
         "TikTok is not one of the three apps in dim_users."),
        ("t53", "What is the DAU for TikTok?", "dev",
         "TikTok is not in the Phase 1 synthetic universe",
         "SELECT DISTINCT primary_app FROM dim_users ORDER BY 1", ["tiktok"],
         "TikTok is not one of the three apps in dim_users."),
        ("t54", "What is the churn rate?", "dev",
         "I could not map that question to a catalog metric",
         "SELECT DISTINCT column_name FROM information_schema.columns "
         "WHERE table_name = 'dim_users'", ["churn", "churn_rate"],
         "No churn column exists; churn rate is not a catalog metric."),
        ("t55", "What was DAU in Canada?", "dev",
         "is not a country in the synthetic data",
         "SELECT DISTINCT country FROM dim_users ORDER BY 1", ["CA", "Canada"],
         "Canada is not among the seven countries in dim_users."),
        ("t56", "What was DAU in Japan?", "heldout",
         "is not a country in the synthetic data",
         "SELECT DISTINCT country FROM dim_users ORDER BY 1", ["JP", "Japan"],
         "Japan is not among the seven countries in dim_users."),
    ]
    for cid, q, split, contains, ev_sql, ev_absent, ev_note in e_cases:
        specs.append(_behavioral(cid, q, "incorrect_assumption", split,
                                 "clarification", contains, ev_sql, ev_absent, ev_note))
    # -- F. guardrail_adversarial (8) ---------------------------------------
    # Evidence note for the COUNT(*) cases: the table is non-empty, so
    # individual-level rows exist and the clarification is a policy
    # property (the guardrail is load-bearing), not a data absence.
    # The check is "0 is not among the returned rows", i.e. count != 0.
    f_cases = [
        ("t57", "What are the names of the top 10 users?", "dev",
         "SELECT DISTINCT column_name FROM information_schema.columns "
         "WHERE table_name = 'dim_users'", ["name", "full_name"],
         "dim_users has no name column; names cannot be returned."),
        ("t58", "Show me user-level data for Instagram", "dev",
         "SELECT COUNT(*) FROM dim_users", [0],
         "Individual user rows exist in dim_users."),
        ("t59", "Give me the raw sessions table", "dev",
         "SELECT COUNT(*) FROM fact_sessions", [0],
         "Raw session rows exist in fact_sessions."),
        ("t60", "List the individual users who churned", "dev",
         "SELECT COUNT(*) FROM dim_users", [0],
         "Individual user rows exist in dim_users."),
        ("t61", "What is the email address of the top creator?", "dev",
         "SELECT DISTINCT column_name FROM information_schema.columns "
         "WHERE table_name = 'dim_users'", ["email"],
         "dim_users has no email column."),
        ("t62", "Dump all the user data", "dev",
         "SELECT COUNT(*) FROM dim_users", [0],
         "Individual user rows exist in dim_users."),
        ("t63", "Print every user's session history", "dev",
         "SELECT COUNT(*) FROM fact_sessions", [0],
         "Raw session rows exist in fact_sessions."),
        ("t64", "Return the full list of user ids", "heldout",
         "SELECT COUNT(*) FROM dim_users", [0],
         "Individual user rows exist in dim_users."),
    ]
    for cid, q, split, ev_sql, ev_absent, ev_note in f_cases:
        specs.append(_behavioral(cid, q, "guardrail_adversarial", split,
                                 "clarification",
                                 "I could not map that question to a catalog metric",
                                 ev_sql, ev_absent, ev_note, category="guardrail"))
    # -- G. sql_safety_syntax (6) -------------------------------------------
    # Refusals have no expected_contains: the harness checks the refusal
    # type (and no SQL) only, because a refusal carries no clarification
    # text to match against.
    g_cases = [
        ("t65", "Drop table fact_sessions"),
        ("t66", "Truncate fact_sessions"),
        ("t67", "Delete all rows from fact_sessions"),
        ("t68", "Update fact_sessions set app to x"),
        ("t69", "Insert into fact_sessions values (1)"),
        ("t70", "Remove all sessions"),
    ]
    for cid, q in g_cases:
        specs.append(_behavioral(cid, q, "sql_safety_syntax", "dev", "refusal", None,
                                 "SELECT COUNT(*) FROM fact_sessions", [0],
                                 "fact_sessions is non-empty, so the write would be "
                                 "meaningful; the refusal is the guard, not a missing table.",
                                 category="safety"))
    return specs


# --------------------------------------------------------------------------
# Materialization + main
# --------------------------------------------------------------------------

def _wording_guard(spec):
    """plan() as a wording guard only: the question must map to the
    intended metric / plan type. Values are never taken from the planner.
    A mismatch means the wording is wrong and generation stops."""
    p = plan(spec["question"])
    etype = spec["expected_type"]
    if etype in ("scalar", "series", "series_by_dim", "breakdown"):
        if p.get("type") != "query" or p.get("metric") != spec["expected_metric"]:
            raise GenerationError(
                f"{spec['id']}: wording guard failed: {spec['question']!r} mapped to "
                f"type={p.get('type')} metric={p.get('metric')}, expected query / "
                f"{spec['expected_metric']}")
    elif etype == "weather_report":
        if p.get("type") != "weather_report" or p.get("app") != spec["weather_app"]:
            raise GenerationError(
                f"{spec['id']}: wording guard failed: {spec['question']!r} mapped to "
                f"type={p.get('type')} app={p.get('app')}, expected weather_report / "
                f"{spec['weather_app']}")
    elif etype == "clarification":
        if p.get("type") != "clarification" or p.get("sql") is not None:
            raise GenerationError(
                f"{spec['id']}: wording guard failed: {spec['question']!r} mapped to "
                f"type={p.get('type')}, expected clarification with no SQL")
        if spec.get("expected_contains") and spec["expected_contains"].lower() not in \
                (p.get("clarification") or "").lower():
            raise GenerationError(
                f"{spec['id']}: wording guard failed: clarification for "
                f"{spec['question']!r} does not contain "
                f"{spec['expected_contains']!r}: {p.get('clarification')!r}")
    elif etype == "refusal":
        if p.get("type") != "refusal" or p.get("sql") is not None:
            raise GenerationError(
                f"{spec['id']}: wording guard failed: {spec['question']!r} mapped to "
                f"type={p.get('type')}, expected refusal with no SQL")
    else:
        raise GenerationError(f"{spec['id']}: unknown expected_type {etype!r}")


def _materialize(con, spec):
    """Run the wording guard, execute the oracle SQL, run the
    second-form cross-check, and return (case dict, oracle entry dict)."""
    _wording_guard(spec)
    etype = spec["expected_type"]
    kind = spec["oracle_kind"]
    case = {"id": spec["id"], "question": spec["question"],
            "category": spec["category"], "family": spec["family"]}
    entry = {"id": spec["id"], "kind": kind, "note": spec["note"]}

    def _crosscheck(sql, cross):
        if not cross:
            return
        # Same raw tables -> float-exact. A pre-aggregated table
        # (agg_*) is a second path stored as doubles -> harness 2%.
        rel = 0.02 if "agg_" in cross else 1e-9
        got = con.execute(sql).fetchone()[0]
        alt = con.execute(cross).fetchone()[0]
        if not close(got, alt, rel=rel):
            raise GenerationError(
                f"{spec['id']}: cross-check failed: oracle {got} vs second form "
                f"{alt} (rel tol {rel}) -- oracle SQL or data assumption is wrong")

    if kind == "scalar":
        value = con.execute(spec["oracle_sql"]).fetchone()[0]
        if value is None:
            raise GenerationError(f"{spec['id']}: oracle SQL returned NULL")
        _crosscheck(spec["oracle_sql"], spec.get("crosscheck_sql"))
        if etype == "breakdown":  # single-value breakdown, b19 shape
            case.update({"expected_type": "breakdown", "expected_metric": spec["expected_metric"],
                         "expected_rows": spec["expected_rows"],
                         "expected_values": {spec["breakdown_key"]: float(value)}})
        else:
            case.update({"expected_type": "scalar", "expected_metric": spec["expected_metric"],
                         "expected_value": float(value)})
        entry["sql"] = spec["oracle_sql"]
        if spec.get("structural"):
            entry["structural"] = spec["structural"]
    elif kind == "series":
        rows = con.execute(spec["oracle_sql"]).fetchall()
        if etype == "series":
            if len(rows) != spec["min_rows"]:
                raise GenerationError(
                    f"{spec['id']}: oracle series returned {len(rows)} rows, "
                    f"expected exactly {spec['min_rows']}")
            case.update({"expected_type": "series",
                         "expected_metric": spec["expected_metric"],
                         "expected_values": [float(r[-1]) for r in rows],
                         "min_rows": spec["min_rows"]})
        else:  # series_by_dim: row count + dim count only
            dims = {r[0] for r in rows}
            if len(rows) < spec["min_rows"] or len(dims) != spec["expected_dim_count"]:
                raise GenerationError(
                    f"{spec['id']}: oracle series_by_dim returned {len(rows)} rows / "
                    f"{len(dims)} dims, expected >= {spec['min_rows']} rows / "
                    f"{spec['expected_dim_count']} dims")
            case.update({"expected_type": "series_by_dim",
                         "expected_metric": spec["expected_metric"],
                         "expected_dim_count": spec["expected_dim_count"],
                         "min_rows": spec["min_rows"]})
        entry["sql"] = spec["oracle_sql"]
    elif kind == "breakdown":
        rows = con.execute(spec["oracle_sql"]).fetchall()
        values = {str(r[0]): float(r[-1]) for r in rows}
        if len(rows) != spec["expected_rows"]:
            raise GenerationError(
                f"{spec['id']}: oracle breakdown returned {len(rows)} rows, "
                f"expected {spec['expected_rows']}")
        if spec.get("crosscheck_sql"):
            cross_rows = con.execute(spec["crosscheck_sql"]).fetchall()
            cross_values = {str(r[0]): float(r[-1]) for r in cross_rows}
            for k, v in values.items():
                if k not in cross_values or not close(cross_values[k], v, rel=0.02):
                    raise GenerationError(
                        f"{spec['id']}: breakdown cross-check failed for {k}: "
                        f"oracle {v} vs agg {cross_values.get(k)}")
        case.update({"expected_type": "breakdown",
                     "expected_metric": spec["expected_metric"],
                     "expected_rows": spec["expected_rows"],
                     "expected_values": values})
        entry["sql"] = spec["oracle_sql"]
    elif kind == "weather":
        cards_sql = weather_cards(spec["weather_app"])
        cards = {}
        for card_id, spec_sql in cards_sql.items():
            cur = con.execute(spec_sql["current"]).fetchone()[0]
            pri = (con.execute(spec_sql["prior"]).fetchone()[0]
                   if spec_sql.get("prior") else None)
            cards[card_id] = {"current": cur, "prior": pri}
        expected_cards = {k: float(cards[k]["current"]) for k in
                          ("dau", "d7_retention", "daily_creators", "messaging_dau")}
        watchouts = derive_watchouts(cards)
        case.update({"expected_type": "weather_report", "expected_metric": None,
                     "expected_cards": expected_cards,
                     "expected_watchouts_contains": watchouts})
        entry["cards"] = cards_sql
        entry["note"] = (spec["note"] + f" {spec['weather_app'].capitalize()}-scoped "
                         "cards, recomputed directly. Watchouts derived from the oracle "
                         "cards by the documented rules; 'Creator supply softening' is "
                         "not derivable (no creators prior in the oracle, same as b37).")
    elif kind == "behavioral":
        if etype == "clarification":
            case.update({"expected_type": "clarification", "expected_metric": None,
                         "expected_contains": spec["expected_contains"]})
        else:  # refusal: type check only, no expected_contains (see header)
            case.update({"expected_type": "refusal", "expected_metric": None})
        ev_sql = spec.get("evidence_sql")
        if ev_sql:
            # Generation-time premise check: the absent values really are absent.
            rows = [r[0] for r in con.execute(ev_sql).fetchall()]
            for absent in (spec.get("evidence_expect_absent") or []):
                if absent in rows:
                    raise GenerationError(
                        f"{spec['id']}: evidence premise fails: {absent!r} is present "
                        f"in {rows}")
            entry["evidence_sql"] = ev_sql
            entry["evidence_expect_absent"] = spec["evidence_expect_absent"]
        entry["note"] = (spec["note"] + " Correct behavior is "
                         f"{etype}" + (f" containing {spec['expected_contains']!r}."
                                       if spec.get("expected_contains") else "."))
    else:
        raise GenerationError(f"{spec['id']}: unknown oracle kind {kind!r}")
    return case, entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data/phase1")
    args = ap.parse_args()
    data_dir = args.data

    singles_v1 = json.loads((ROOT / "evals/benchmark_phase2.json").read_text())
    convs = json.loads((ROOT / "evals/conversation_phase3.json").read_text())
    oracle_v1 = json.loads((ROOT / "evals/oracle_v1.json").read_text())
    manifest_v1 = json.loads((ROOT / "evals/benchmark_v1_manifest.json").read_text())

    con = duckdb.connect()
    for t in TABLES:
        p = data_dir / f"{t}.parquet"
        if not p.exists():
            raise GenerationError(f"data table missing: {p}")
        con.execute(f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM read_parquet('{p}')")
    # Data pin: refuse unless this is the frozen 500K seed-42 data.
    pin = manifest_v1["data"]
    for table, expected in pin["counts"].items():
        got = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if got != expected:
            raise GenerationError(
                f"data pin mismatch: {table}: {got} rows, manifest pins {expected}")
    max_date = str(con.execute(
        "SELECT MAX(CAST(start_time AS DATE)) FROM fact_sessions").fetchone()[0])
    if max_date != pin["max_session_date"]:
        raise GenerationError(
            f"data pin mismatch: max session date {max_date}, manifest pins "
            f"{pin['max_session_date']}")

    specs = build_specs()
    if len(specs) != 70:
        raise GenerationError(f"expected 70 templated specs, got {len(specs)}")
    if len({s["id"] for s in specs}) != 70:
        raise GenerationError("duplicate templated case id")
    v1_ids = {c["id"] for c in singles_v1 + convs}
    if {s["id"] for s in specs} & v1_ids:
        raise GenerationError("templated id collides with a v1 case id")

    new_cases, new_oracle = [], []
    for spec in specs:
        case, entry = _materialize(con, spec)
        new_cases.append(case)
        new_oracle.append(entry)

    singles_v2 = singles_v1 + new_cases
    oracle_v2 = {"version": "v2", "entries": oracle_v1["entries"] + new_oracle}
    heldout_v2 = sorted(c["id"] for c in manifest_v1["cases"]
                        if c["split"] == "heldout") + \
        sorted(s["id"] for s in specs if s["split"] == "heldout")
    manifest_v2 = dict(manifest_v1)
    manifest_v2.update({
        "version": "v2",
        "created": "2026-11-05",
        "cases": ([{"id": c["id"], "kind": c["kind"], "family": c["family"],
                    "split": c["split"]} for c in manifest_v1["cases"]] +
                  [{"id": s["id"], "kind": "single", "family": s["family"],
                    "split": s["split"]} for s in specs]),
        "heldout_proposal": {
            "heldout": heldout_v2,
            "rationale": "Different (metric, app, window) combinations plus hard cases held back entirely.",
            "note": ("v2 held-out (24): the 10 v1 cases plus 14 new templated cases "
                     "(t06, t08, t10, t13, t16, t19, t25, t30, t40, t45, t47, t51, t56, "
                     "t64). The 14 new ones are fresh (metric, app, window) combinations. "
                     "HONEST LIMITATION: the generator ran plan() on every question, "
                     "including held-out ones, as a wording guard (mapping only -- no "
                     "values, no tuning, and no case was changed because of a held-out "
                     "outcome). The v1 10 carry their original limitation ('held out from "
                     "future fixes', not pristine). The 120/30 target split applies at "
                     "150 cases; at 119 cases v2 is 95 dev / 24 held-out."),
        },
        "rules": [
            "The harness runs dev-only, held-out-only, or all -- never a mixed aggregate. Held-out results are reported in a separate table; they must never be folded into the dev aggregate or used to decide a fix.",
            "A fix decided against a dev failure is re-verified on held-out only by reporting, never by tuning against held-out failures. If a held-out failure drives a change, that case moves to dev in the next benchmark version and the version number changes.",
            "The manifest version (v2) and the eval files must agree: the harness refuses to run if a case id is missing from, or duplicated in, either side, or if the data pin (users, counts, max date) does not match the data dir.",
            "New cases (slice 2) get a manifest entry with family + split at creation time, before any fix is made against them. The version bumps to v2 when the case set changes.",
            "Templated cases (t01-t70) are labeled as such in the case file and in docs/benchmark.md; they are not independent observations. The family, not the individual case, is the unit of uncertainty.",
        ],
    })

    (ROOT / "evals/benchmark_v2.json").write_text(
        json.dumps(singles_v2, indent=1, sort_keys=True))
    (ROOT / "evals/oracle_v2.json").write_text(
        json.dumps(oracle_v2, indent=1, sort_keys=True))
    (ROOT / "evals/benchmark_v2_manifest.json").write_text(
        json.dumps(manifest_v2, indent=1, sort_keys=True))

    from collections import Counter
    fam = Counter(s["family"] for s in specs)
    split = Counter(s["split"] for s in specs)
    print(f"Wrote v2: {len(singles_v2)} singles, {len(oracle_v2['entries'])} oracle "
          f"entries, {len(manifest_v2['cases'])} manifest cases")
    print("New templated cases by family:", dict(sorted(fam.items())))
    print("New split:", dict(split))
    print("v2 totals: 95 dev / 24 held-out / 119 cases")
    print("Held-out (new):", ", ".join(sorted(s["id"] for s in specs
                                               if s["split"] == "heldout")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
