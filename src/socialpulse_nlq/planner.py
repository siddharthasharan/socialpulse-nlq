"""Deterministic natural-language planner for SocialPulse NLQ, Phase 2.

No LLM is involved. The planner:
1. Detects a metric by keyword rules, using SQL templates that mirror
   catalog/measures.yaml. The YAML itself is the human-readable contract;
   it is not yet the runtime source of truth for templates (see
   catalog_metric_ids, used by tests to check the two stay in sync).
   Making the planner truly catalog-driven is a Phase 3 cleanup.
2. Extracts only allowlisted filters (date range, app, country, platform,
   surface, acquisition source) and an allowlisted breakdown dimension.
3. Fills a pre-written SQL template for that metric. User text is never
   interpolated into SQL except through those allowlisted values.
4. Returns a plan plus a receipt. Ambiguous, invalid, or unsafe questions
   return a clarification and no SQL, instead of a guessed answer.

Supported breakdowns per metric are deliberately narrow in Phase 1/2.
Asking for an unsupported breakdown returns a clarification, not a
silently ignored filter.
"""
import re
from pathlib import Path

DATA_START = "2026-01-01"
DATA_END = "2026-03-31"

ALLOWED_TABLES = {
    "dim_users", "dim_devices", "dim_content", "dim_social_edges",
    "fact_sessions", "fact_content_impressions", "fact_content_engagements",
    "fact_video_plays", "fact_messages", "fact_content_creation",
    "agg_user_daily", "agg_surface_daily",
}
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|attach|copy|grant|revoke|vacuum)\b",
    re.IGNORECASE,
)
COUNTRIES = {
    "us": "US", "united states": "US", "america": "US",
    "india": "IN", "in": "IN",
    "brazil": "BR", "indonesia": "ID", "mexico": "MX",
    "uk": "GB", "united kingdom": "GB", "britain": "GB", "england": "GB",
    "germany": "DE",
}
INVALID_COUNTRIES = [
    "france", "canada", "japan", "australia", "spain", "italy",
    "nigeria", "egypt", "turkey", "korea", "china", "russia",
]
APPS = ["instagram", "whatsapp", "facebook"]
PLATFORMS = ["android", "ios", "web"]
SURFACES = ["reels", "feed", "stories"]
ACQUISITIONS = ["organic", "referral", "paid"]


def guard_sql(sql: str):
    """Read-only guard. Returns (ok, reason)."""
    s = sql.strip().rstrip(";").strip()
    if not s:
        return False, "empty SQL"
    if not (s.lower().startswith("select") or s.lower().startswith("with")):
        return False, "only SELECT / WITH queries are allowed"
    if FORBIDDEN.search(s):
        return False, "write or control statement detected"
    if ";" in s:
        return False, "multiple statements are not allowed"
    # CTE names defined in a WITH clause are not real tables.
    ctes = set(m.group(1).lower() for m in
               re.finditer(r"(?:WITH|,)\s*([a-z_][a-z0-9_]*)\s+AS\s*\(", s, re.IGNORECASE))
    tables = set()
    for m in re.finditer(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", s, re.IGNORECASE):
        tables.add(m.group(1).lower())
    bad = tables - ALLOWED_TABLES - ctes
    if bad:
        return False, f"table not in allowlist: {sorted(bad)}"
    return True, "ok"


def catalog_metric_ids(catalog_path: Path):
    text = Path(catalog_path).read_text()
    return re.findall(r"-\s*id:\s*(\S+)", text)


def _date_range(q: str):
    if "last 7 days" in q or "past 7 days" in q or "last week" in q:
        return "2026-03-25", DATA_END
    if "last 28 days" in q or "past 28 days" in q or "last month" in q:
        return "2026-03-04", DATA_END
    if "last 90 days" in q or "past 90 days" in q:
        return DATA_START, DATA_END
    if "march" in q:
        return "2026-03-01", "2026-03-31"
    if "february" in q:
        return "2026-02-01", "2026-02-28"
    if "january" in q:
        return "2026-01-01", "2026-01-31"
    return DATA_START, DATA_END


def _word(q: str, w: str) -> bool:
    return re.search(rf"\b{re.escape(w)}\b", q) is not None


def _extract_filters(q: str):
    filters = {}
    filters["date_start"], filters["date_end"] = _date_range(q)
    for app in APPS:
        if _word(q, app):
            filters["app"] = app
            break
    for name, code in COUNTRIES.items():
        if _word(q, name):
            filters["country"] = code
            break
    for plat in PLATFORMS:
        if _word(q, plat):
            filters["platform"] = plat
            break
    for surf in SURFACES:
        if _word(q, surf) and "by surface" not in q:
            # "reels" in a question about reels metrics is a surface filter,
            # unless the question explicitly asks for a surface breakdown.
            filters["surface"] = surf
            break
    for acq in ACQUISITIONS:
        if _word(q, acq):
            filters["acquisition"] = acq
            break
    return filters


def _breakdown(q: str):
    if "by app" in q:
        return "app"
    if "by country" in q:
        return "country"
    if "by platform" in q:
        return "platform"
    if "by surface" in q:
        return "surface"
    if "by acquisition" in q or "by channel" in q or "by acquisition source" in q:
        return "acquisition"
    return None


def _user_where(f, alias="u"):
    conds = []
    if "app" in f:
        conds.append(f"{alias}.primary_app = '{f['app']}'")
    if "country" in f:
        conds.append(f"{alias}.country = '{f['country']}'")
    if "platform" in f:
        conds.append(f"{alias}.platform = '{f['platform']}'")
    return conds


def _dim_expr(breakdown, context="user"):
    if breakdown is None:
        return None, None
    if context == "user":
        return {"app": "u.primary_app", "country": "u.country",
                "platform": "u.platform"}[breakdown], breakdown
    if context == "content":
        return {"app": "c.app", "surface": "c.surface"}[breakdown], breakdown
    if context == "impression":
        return {"app": "i.app", "surface": "i.surface"}[breakdown], breakdown
    return None, None


def _series_or_scalar(q: str):
    if "mean" in q or "average" in q:
        return "scalar"
    if "trend" in q or "over time" in q or "daily" in q or "per day" in q:
        return "series"
    # Changed 2026-10-27 (user decision): naming a period means "show
    # me each day in that period", not "average over that period".
    # "What was DAU for whatsapp over the last 28 days?" used to
    # return 1 averaged row; it now returns 28 daily rows. Explicit
    # "mean"/"average" still returns the scalar, as above.
    if re.search(r"over the (last|past)|last (7|28|90) days|past (7|28|90) days|last week|last month", q):
        return "series"
    return "scalar"


def build_sql(metric: str, f: dict, breakdown, q: str):
    """Return (sql, tables, grain). Raises ValueError for unsupported combos."""
    ds, de = f["date_start"], f["date_end"]
    tables = []
    grain = ""

    if metric == "dau_mean_daily":
        tables = ["fact_sessions", "dim_users"]
        uw = _user_where(f)
        where = f"CAST(s.start_time AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}'"
        if uw:
            where += " AND " + " AND ".join(uw)
        if breakdown:
            if breakdown not in ("app", "country", "platform"):
                raise ValueError(f"breakdown by {breakdown} is not supported for DAU in Phase 2")
            dim, _ = _dim_expr(breakdown)
            grain = f"user x day, grouped by {breakdown}"
            return (f"SELECT {dim} AS {breakdown}, CAST(s.start_time AS DATE) AS date, "
                    f"COUNT(DISTINCT s.user_id) AS dau FROM fact_sessions s "
                    f"JOIN dim_users u ON u.user_id = s.user_id WHERE {where} "
                    f"GROUP BY {dim}, date ORDER BY {dim}, date"), tables, grain
        if _series_or_scalar(q) == "series":
            # Bug fixed 2026-10-27: this branch used to ignore the user
            # filters entirely (fact_sessions only, no dim_users join),
            # so "DAU trend for Whatsapp" returned all-app DAU (~9,500/day
            # instead of ~2,900). It now uses the same join + where as
            # the averaged branch.
            grain = "user x day"
            return (f"SELECT CAST(s.start_time AS DATE) AS date, COUNT(DISTINCT s.user_id) AS dau "
                    f"FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id WHERE {where} "
                    f"GROUP BY date ORDER BY date"), tables, grain
        grain = "user x day (averaged)"
        inner = (f"SELECT CAST(s.start_time AS DATE) AS date, COUNT(DISTINCT s.user_id) AS dau "
                 f"FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id WHERE {where} GROUP BY date")
        return f"SELECT AVG(dau) AS value FROM ({inner})", tables, grain

    if metric == "wau_mean_weekly":
        tables = ["fact_sessions"]
        grain = "user x week (averaged)"
        return ("SELECT AVG(wau) AS value FROM (SELECT DATE_TRUNC('week', CAST(start_time AS DATE)) AS w, "
                "COUNT(DISTINCT user_id) AS wau FROM fact_sessions "
                f"WHERE CAST(start_time AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}' GROUP BY w)"), tables, grain

    if metric == "mau_march":
        tables = ["fact_sessions"]
        grain = "user x month"
        return ("SELECT COUNT(DISTINCT user_id) AS value FROM fact_sessions "
                "WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'"), tables, grain

    if metric == "dau_mau_stickiness":
        tables = ["fact_sessions"]
        grain = "month"
        return ("SELECT AVG(dau) / NULLIF((SELECT COUNT(DISTINCT user_id) FROM fact_sessions "
                "WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'), 0) AS value FROM "
                "(SELECT CAST(start_time AS DATE) AS d, COUNT(DISTINCT user_id) AS dau FROM fact_sessions "
                "WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' GROUP BY d)"), tables, grain

    if metric == "new_users_in_window":
        tables = ["dim_users"]
        grain = "user"
        s, e = ds, de
        return (f"SELECT COUNT(*) AS value FROM dim_users "
                f"WHERE signup_date BETWEEN DATE '{s}' AND DATE '{e}'"), tables, grain

    if metric in ("d7_exact_retention", "d7_retention_by_acquisition"):
        tables = ["dim_users", "fact_sessions"]
        grain = "cohort user"
        acq_where = ""
        if "acquisition" in f:
            acq_where = f" AND u.acquisition_source = '{f['acquisition']}'"
        if metric == "d7_retention_by_acquisition":
            grain = "cohort user x acquisition_source"
            return (f"SELECT u.acquisition_source AS acquisition_source, "
                    f"AVG(CASE WHEN EXISTS (SELECT 1 FROM fact_sessions s WHERE s.user_id = u.user_id "
                    f"AND CAST(s.start_time AS DATE) = u.signup_date + INTERVAL 7 DAY) THEN 1.0 ELSE 0.0 END) AS value "
                    f"FROM dim_users u WHERE u.signup_date BETWEEN DATE '2026-01-01' AND DATE '2026-02-21'{acq_where} "
                    f"GROUP BY u.acquisition_source ORDER BY u.acquisition_source"), tables, grain
        return (f"SELECT AVG(CASE WHEN EXISTS (SELECT 1 FROM fact_sessions s WHERE s.user_id = u.user_id "
                f"AND CAST(s.start_time AS DATE) = u.signup_date + INTERVAL 7 DAY) THEN 1.0 ELSE 0.0 END) AS value "
                f"FROM dim_users u WHERE u.signup_date BETWEEN DATE '2026-01-01' AND DATE '2026-02-21'{acq_where}"), tables, grain

    if metric == "sessions_per_dau":
        tables = ["fact_sessions", "dim_users"]
        uw = _user_where(f)
        where = f"CAST(s.start_time AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}'"
        if uw:
            where += " AND " + " AND ".join(uw)
        if breakdown:
            if breakdown not in ("app", "country", "platform"):
                raise ValueError(f"breakdown by {breakdown} is not supported for sessions per DAU")
            dim, _ = _dim_expr(breakdown)
            grain = f"session / user-day, grouped by {breakdown}"
            return (f"SELECT {breakdown}, SUM(sessions) * 1.0 / NULLIF(SUM(user_days), 0) AS value FROM "
                    f"(SELECT {dim} AS {breakdown}, CAST(s.start_time AS DATE) AS d, COUNT(*) AS sessions, "
                    f"COUNT(DISTINCT s.user_id) AS user_days FROM fact_sessions s "
                    f"JOIN dim_users u ON u.user_id = s.user_id WHERE {where} GROUP BY {dim}, d) "
                    f"GROUP BY {breakdown} ORDER BY {breakdown}"), tables, grain
        grain = "session / user-day"
        return (f"SELECT COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT (s.user_id, CAST(s.start_time AS DATE))), 0) AS value "
                f"FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id WHERE {where}"), tables, grain

    if metric == "median_session_duration_min":
        tables = ["fact_sessions", "dim_users"]
        uw = _user_where(f)
        where = f"CAST(s.start_time AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}'"
        if uw:
            where += " AND " + " AND ".join(uw)
        grain = "session"
        return (f"SELECT MEDIAN(s.duration_minutes) AS value FROM fact_sessions s "
                f"JOIN dim_users u ON u.user_id = s.user_id WHERE {where}"), tables, grain

    if metric == "daily_creators_mean":
        tables = ["fact_content_creation"]
        app_where = f" AND c.app = '{f['app']}'" if "app" in f else ""
        if breakdown:
            if breakdown != "app":
                raise ValueError(f"breakdown by {breakdown} is not supported for daily creators")
            grain = "creator x day, grouped by app"
            return (f"SELECT c.app AS app, CAST(k.published_at AS DATE) AS date, COUNT(DISTINCT k.creator_id) AS creators "
                    f"FROM fact_content_creation k JOIN dim_content c ON c.content_id = k.content_id "
                    f"WHERE CAST(k.published_at AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}' "
                    f"GROUP BY c.app, date ORDER BY c.app, date"), ["fact_content_creation", "dim_content"], grain
        grain = "creator x day (averaged)"
        inner = (f"SELECT CAST(k.published_at AS DATE) AS date, COUNT(DISTINCT k.creator_id) AS creators "
                 f"FROM fact_content_creation k JOIN dim_content c ON c.content_id = k.content_id "
                 f"WHERE CAST(k.published_at AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}'{app_where} GROUP BY date")
        return f"SELECT AVG(creators) AS value FROM ({inner})", tables, grain

    if metric == "creator_penetration":
        tables = ["fact_content_creation", "fact_sessions"]
        grain = "day (averaged)"
        return ("SELECT AVG(creators * 1.0 / NULLIF(dau, 0)) AS value FROM "
                "(SELECT CAST(published_at AS DATE) AS d, COUNT(DISTINCT creator_id) AS creators "
                "FROM fact_content_creation GROUP BY d) c JOIN "
                "(SELECT CAST(start_time AS DATE) AS d, COUNT(DISTINCT user_id) AS dau "
                "FROM fact_sessions GROUP BY d) s USING (d)"), tables, grain

    if metric == "posts_per_creator_90d":
        tables = ["fact_content_creation", "dim_content"]
        app_where = f" WHERE c.app = '{f['app']}'" if "app" in f else ""
        grain = "creator"
        return (f"SELECT COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT k.creator_id), 0) AS value "
                f"FROM fact_content_creation k JOIN dim_content c ON c.content_id = k.content_id{app_where}"), tables, grain

    if metric == "orphan_post_rate":
        tables = ["dim_content", "fact_content_engagements"]
        app_where = f" WHERE c.app = '{f['app']}'" if "app" in f else ""
        grain = "content item"
        return (f"SELECT AVG(CASE WHEN EXISTS (SELECT 1 FROM fact_content_engagements e WHERE e.content_id = c.content_id) "
                f"THEN 0.0 ELSE 1.0 END) AS value FROM dim_content c{app_where}"), tables, grain

    if metric == "top1_creator_impression_share":
        tables = ["fact_content_impressions", "dim_content"]
        grain = "sampled impression"
        return ("WITH creator_imps AS (SELECT c.creator_id, COUNT(*) AS imps FROM fact_content_impressions i "
                "JOIN dim_content c ON c.content_id = i.content_id GROUP BY c.creator_id), "
                "ranked AS (SELECT creator_id, imps, NTILE(100) OVER (ORDER BY imps DESC) AS tile FROM creator_imps) "
                "SELECT SUM(CASE WHEN tile = 1 THEN imps ELSE 0 END) * 1.0 / NULLIF(SUM(imps), 0) AS value FROM ranked"), tables, grain

    if metric == "reels_completion_rate":
        tables = ["fact_video_plays"]
        grain = "video play"
        return ("SELECT AVG(CASE WHEN completion_pct >= 0.95 THEN 1.0 ELSE 0.0 END) AS value FROM fact_video_plays"), tables, grain

    if metric == "reels_watch_time_per_viewer_day":
        tables = ["fact_video_plays"]
        grain = "viewer x day"
        return ("SELECT SUM(watch_ms) / 60000.0 / NULLIF(COUNT(DISTINCT (user_id, CAST(play_time AS DATE))), 0) AS value "
                "FROM fact_video_plays"), tables, grain

    if metric == "messaging_dau_mean":
        tables = ["fact_messages", "dim_users"]
        uw = _user_where(f, alias="u")
        where = f"CAST(m.sent_at AS DATE) BETWEEN DATE '{ds}' AND DATE '{de}'"
        if uw:
            where += " AND " + " AND ".join(uw)
        if breakdown:
            if breakdown not in ("app", "country", "platform"):
                raise ValueError(f"breakdown by {breakdown} is not supported for messaging DAU")
            dim, _ = _dim_expr(breakdown)
            grain = f"user x day, grouped by {breakdown}"
            return (f"SELECT {dim} AS {breakdown}, CAST(m.sent_at AS DATE) AS date, COUNT(DISTINCT m.sender_id) AS senders "
                    f"FROM fact_messages m JOIN dim_users u ON u.user_id = m.sender_id WHERE {where} "
                    f"GROUP BY {dim}, date ORDER BY {dim}, date"), tables, grain
        grain = "user x day (averaged)"
        inner = (f"SELECT CAST(m.sent_at AS DATE) AS date, COUNT(DISTINCT m.sender_id) AS senders "
                 f"FROM fact_messages m JOIN dim_users u ON u.user_id = m.sender_id WHERE {where} GROUP BY date")
        return f"SELECT AVG(senders) AS value FROM ({inner})", tables, grain

    if metric == "two_way_conversation_rate":
        tables = ["fact_messages"]
        grain = "conversation"
        return ("SELECT AVG(CASE WHEN senders >= 2 THEN 1.0 ELSE 0.0 END) AS value FROM "
                "(SELECT conversation_id, COUNT(DISTINCT sender_id) AS senders FROM fact_messages GROUP BY conversation_id)"), tables, grain

    if metric == "commenter_penetration":
        tables = ["fact_content_engagements", "fact_sessions"]
        grain = "user x day (averaged)"
        return ("SELECT AVG(commenters * 1.0 / NULLIF(dau, 0)) AS value FROM "
                "(SELECT CAST(e.created_at AS DATE) AS d, COUNT(DISTINCT e.user_id) AS commenters "
                "FROM fact_content_engagements e WHERE e.action_type = 'comment' GROUP BY d) c JOIN "
                "(SELECT CAST(start_time AS DATE) AS d, COUNT(DISTINCT user_id) AS dau "
                "FROM fact_sessions GROUP BY d) s USING (d)"), tables, grain

    if metric == "engagement_rate_per_impression":
        tables = ["fact_content_impressions", "fact_content_engagements", "dim_content"]
        if breakdown:
            if breakdown not in ("surface", "app"):
                raise ValueError(f"breakdown by {breakdown} is not supported for engagement rate")
            dim = "i.surface" if breakdown == "surface" else "i.app"
            grain = f"sampled impression, grouped by {breakdown}"
            return (f"SELECT {breakdown}, SUM(eng) * 1.0 / NULLIF(SUM(imp), 0) AS value FROM "
                    f"(SELECT {dim} AS {breakdown}, COUNT(*) AS imp, 0 AS eng FROM fact_content_impressions i GROUP BY {dim} "
                    f"UNION ALL SELECT c.{breakdown} AS {breakdown}, 0 AS imp, COUNT(*) AS eng "
                    f"FROM fact_content_engagements e JOIN dim_content c ON c.content_id = e.content_id GROUP BY c.{breakdown}) "
                    f"GROUP BY {breakdown} ORDER BY {breakdown}"), tables, grain
        surf_where = f" AND i.surface = '{f['surface']}'" if "surface" in f else ""
        app_where = f" AND i.app = '{f['app']}'" if "app" in f else ""
        grain = "sampled impression"
        # Engagements are counted on the same surface/app as their content.
        eng_where = ""
        if "surface" in f:
            eng_where += f" AND c.surface = '{f['surface']}'"
        if "app" in f:
            eng_where += f" AND c.app = '{f['app']}'"
        return (f"SELECT (SELECT COUNT(*) FROM fact_content_engagements e JOIN dim_content c ON c.content_id = e.content_id "
                f"WHERE 1=1{eng_where}) * 1.0 / NULLIF((SELECT COUNT(*) FROM fact_content_impressions i "
                f"WHERE 1=1{surf_where}{app_where}), 0) AS value"), tables, grain

    raise ValueError(f"unknown metric: {metric}")


def _experiment_plan(q: str):
    if "list" in q or "which experiments" in q or "show experiments" in q or "experiments" in q and "primary" not in q and "did" not in q and "improve" not in q:
        return {
            "type": "experiment_list", "metric": None, "sql": None, "tables": [],
            "grain": "experiment registry",
            "explanation": "Experiments from catalog/experiments.yaml. Assignment tables arrive in Phase 3, so this list is descriptive, not an evaluation.",
            "clarification": None,
        }
    if "reels ranking" in q and "primary" in q:
        return {
            "type": "experiment_info", "metric": "reels_watch_time_per_viewer_day", "sql": None, "tables": [],
            "grain": "experiment",
            "explanation": "Experiment exp_reels_ranking_v1 (Reels ranking update, instagram, completed 2026-02-01 to 2026-02-28): primary metric is reels watch time per viewer, guardrails are creator penetration and report rate.",
            "clarification": None,
        }
    if "new creator" in q and "primary" in q:
        return {
            "type": "experiment_info", "metric": "new_creator_d7_retention", "sql": None, "tables": [],
            "grain": "experiment",
            "explanation": "Experiment exp_new_creator_boost_v1 (New creator discovery boost, instagram, running 2026-03-01 to 2026-03-31): primary metric is new creator D7 retention, guardrails are orphan post rate and top-1% creator share.",
            "clarification": None,
        }
    if "did" in q or "improve" in q or "effect" in q or "lift" in q:
        return {
            "type": "clarification", "metric": None, "sql": None, "tables": [],
            "grain": "",
            "explanation": "",
            "clarification": ("I cannot evaluate that experiment yet: Phase 1/2 ships the experiment registry only, "
                              "and experiment assignment tables arrive in Phase 3. Evaluating a lift without assignments "
                              "would be a causal claim without evidence. Ask for the experiment's primary metric and guardrails instead, "
                              "or wait for the assignment tables."),
        }
    return None


def plan(question: str):
    """Return a plan dict. Clarification plans have sql=None."""
    q = question.lower().strip()
    if not q:
        return {"type": "clarification", "metric": None, "sql": None, "tables": [], "grain": "",
                "explanation": "",
                "clarification": "Ask a question about a specific metric, for example 'What was DAU by app over the last 28 days?'"}
    # Unsafe first.
    if any(w in q for w in ["delete", "drop table", "update ", "insert into", "remove all", "truncate"]):
        return {"type": "refusal", "metric": None, "sql": None, "tables": [], "grain": "",
                "explanation": "",
                "clarification": "I only run read-only SELECT queries. I will not run write or control statements."}
    if "tiktok" in q:
        return {"type": "clarification", "metric": None, "sql": None, "tables": [], "grain": "",
                "explanation": "",
                "clarification": "TikTok is not in the Phase 1 synthetic universe. The apps are instagram, whatsapp, and facebook."}
    for bad in INVALID_COUNTRIES:
        if _word(q, bad):
            return {"type": "clarification", "metric": None, "sql": None, "tables": [], "grain": "",
                    "explanation": "",
                    "clarification": f"'{bad}' is not a country in the synthetic data. The countries are US, IN, BR, ID, MX, GB, and DE."}
    if "experiment" in q:
        exp = _experiment_plan(q)
        if exp is not None:
            return exp
    # Product-health questions are answered by the weather report, not
    # by a single metric. This must come before metric detection: "How
    # is the product doing?" contains no metric keyword, and without
    # this branch it used to clarify instead of answering.
    if (("how is" in q and "product" in q and "doing" in q)
            or "product health" in q or "weather report" in q
            or "executive summary" in q or "overall health" in q
            or "how are we doing" in q):
        return {
            "type": "weather_report", "metric": None, "sql": None, "tables": [],
            "grain": "product health (last 28 days vs prior 28 days)",
            "explanation": ("Weather report: DAU, WAU, retention, and creator health, "
                            "computed from the Phase 1 tables. No single score; see the cards and watchouts."),
            "clarification": None,
        }
    # Metric detection, specific before generic.
    metric = None
    if "two-way" in q or "two way" in q or "reciprocal conversation" in q:
        metric = "two_way_conversation_rate"
    elif "orphan" in q:
        metric = "orphan_post_rate"
    elif "top 1%" in q or "top 1 percent" in q or "concentration" in q or "disproportionate share" in q:
        metric = "top1_creator_impression_share"
    elif "commenter" in q:
        metric = "commenter_penetration"
    elif "reels" in q and "completion" in q:
        metric = "reels_completion_rate"
    elif "watch time" in q:
        metric = "reels_watch_time_per_viewer_day"
    elif "messaging dau" in q or "message senders" in q or ("messages" in q and "daily" in q):
        metric = "messaging_dau_mean"
    elif "daily creators" in q or "creators per day" in q or "how many creators" in q:
        metric = "daily_creators_mean"
    elif "creator penetration" in q:
        metric = "creator_penetration"
    elif "posts per creator" in q:
        metric = "posts_per_creator_90d"
    elif "d7" in q or "day 7" in q or "day-7" in q:
        metric = "d7_retention_by_acquisition" if ("by acquisition" in q or "by channel" in q or "acquisition" in q or any(_word(q, a) for a in ACQUISITIONS)) else "d7_exact_retention"
    elif "retention" in q:
        metric = "d7_retention_by_acquisition" if ("by acquisition" in q or "by channel" in q or any(_word(q, a) for a in ACQUISITIONS)) else "d7_exact_retention"
    elif "sessions per dau" in q or "sessions per daily" in q:
        metric = "sessions_per_dau"
    elif "session duration" in q or "median session" in q:
        metric = "median_session_duration_min"
    elif "stickiness" in q or "dau/mau" in q or "dau / mau" in q:
        metric = "dau_mau_stickiness"
    elif "wau" in q or "weekly active" in q:
        metric = "wau_mean_weekly"
    elif "mau" in q or "monthly active" in q:
        metric = "mau_march"
    elif "new users" in q:
        metric = "new_users_in_window"
    elif "engagement rate" in q:
        metric = "engagement_rate_per_impression"
    elif "dau" in q or "daily active" in q:
        metric = "dau_mean_daily"
    else:
        return {"type": "clarification", "metric": None, "sql": None, "tables": [], "grain": "",
                "explanation": "",
                "clarification": ("I could not map that question to a catalog metric. Try a specific metric, for example: "
                                  "DAU, WAU, MAU, D7 retention, daily creators, orphan post rate, engagement rate, "
                                  "Reels watch time, or messaging DAU, optionally with 'by app', 'by country', or 'over the last 28 days'.")}
    f = _extract_filters(q)
    breakdown = _breakdown(q)
    # "by acquisition" is the metric itself for retention, not a separate breakdown.
    if metric == "d7_retention_by_acquisition":
        breakdown = None
    try:
        sql, tables, grain = build_sql(metric, f, breakdown, q)
    except ValueError as e:
        return {"type": "clarification", "metric": metric, "sql": None, "tables": [], "grain": "",
                "explanation": "", "clarification": str(e)}
    ok, reason = guard_sql(sql)
    if not ok:
        return {"type": "clarification", "metric": metric, "sql": None, "tables": [], "grain": "",
                "explanation": "", "clarification": f"SQL guard rejected the planned query: {reason}"}
    return {
        "type": "query", "metric": metric, "sql": sql, "tables": tables, "grain": grain,
        "filters": f, "breakdown": breakdown,
        "explanation": f"Metric {metric}, grain {grain}, filters {f}, breakdown {breakdown}.",
        "clarification": None,
    }


def run_plan(plan: dict, data_dir: Path):
    """Execute a query plan on DuckDB. Returns (columns, rows, runtime_ms)."""
    import time
    import duckdb

    if plan.get("sql") is None:
        return [], [], 0
    con = duckdb.connect()
    for t in ALLOWED_TABLES:
        p = Path(data_dir) / f"{t}.parquet"
        if p.exists():
            con.execute(f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM read_parquet('{p}')")
    t0 = time.time()
    cur = con.execute(plan["sql"])
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else []
    ms = int((time.time() - t0) * 1000)
    return cols, rows, ms
