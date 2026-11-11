"""Phase 3 multi-turn chatbot for SocialPulse NLQ. Deterministic, no LLM.

This is deliberately NOT a free-form chatbot. It is a state machine over
the data the project already computes:

    weather (scoped) -> drill-down (a/b/c) -> diagnosis
    -> experiment hypothesis -> PM/Eng brief

Plus standalone metric turns that need no weather context: MAU
(app-scoped, March 2026), which answers as the first turn in a
new chat and does not set the weather state.

Why a state machine, not an LLM chat: every turn must stay grounded in
the same DuckDB tables, the same weather thresholds, and the same
receipts as Phase 2. An LLM would be free to invent a lift, a cause, or
a number on turn 3 that turn 1 never showed. Here, a turn can only
surface numbers the weather report or a named drill-down query produced.

State kept on the session (not in the user's text):
- app: instagram / whatsapp / facebook, set by the opening question and
  kept until the user names a different app.
- weather: the last scoped weather report, so later turns do not
  re-derive or silently change the numbers.
- topic: the last drill-down topic, so "diagnosis" and "recommendation"
  answer about that topic, not about whatever the user asked first.
- stage: start / weather / drilldown / diagnosis / recommendation /
  brief / clarification. The stage is what the conversation evals
  check, not just the final text.

Honesty rules, carried from Phase 2 and Raven-style scoring:
- A recommendation is labelled HYPOTHESIS, never a forecast. There are
  no experiment assignment tables in Phase 3 (that was the other fork),
  so the bot cannot say an experiment *will* improve a metric, or by
  how much. It can say what to test, on whom, with what primary metric
  and guardrails, and what would change its mind.
- Historical evidence is a pattern, not a cause. The creator hypothesis
  is motivated by a high orphan rate plus falling creator supply; it is
  NOT claimed as a planted causal effect in the generator. The generator
  encodes reply -> two-way conversation in messages, not "first-post
  reply causes creator retention", and this module does not invent that.
- The data window is Jan-Mar 2026 only. "Historical" here means that
  90-day window and last-28 vs prior-28, not years of history.
- Orphan rate and top-1% share are whole-window levels, never trends,
  for the exposure reason documented in weather.py.
"""
import re
from pathlib import Path

from .questions import load_flow
from .sqlviz import build_visualization
from .weather import compute_weather

APPS = ["instagram", "whatsapp", "facebook"]

TOPIC_LABELS = {
    "creators": "Creators — supply, orphan rate, concentration",
    "retention": "Retention — D7 and growth",
    "growth": "Growth — DAU and WAU",
    "messaging": "Messaging — messaging DAU",
    "engagement": "Engagement — engagement rate and Reels",
    "concentration": "Concentration — orphan rate and top-1% share",
}

# Turn-by-turn Step 1 (2026-10-29): intent phrasings, not a keyword
# list per turn. A weather intent is an app slot plus any of these
# phrases — "current state", "status", "overview" mean the same as
# "what's happening", which is why "What is the current state of
# Instagram" used to clarify. Follow-up prefixes ("what about",
# "and", "same for") mean "same intent, swap one slot" (see
# respond()). These live here, not in questions.json yet; the
# editor gains the intent table in Step 4 of the design.
WEATHER_PHRASES = (
    "how is", "happening", "doing", "weather", "health",
    "current state", "state of", "status", "overview", "summary",
    "report", "update on", "how's", "how is it going",
    "what's up with", "what is up with", "how are things",
)
FOLLOWUP_PREFIXES = (
    "what about", "how about", "and", "also", "same for", "switch to",
)

# Intervention hypotheses. Each is a test to run, not a result.
# Primary metric and guardrails are the pre-registered success criteria:
# if the primary moves and a guardrail breaches, the experiment does
# not ship, however good the primary looks.
RECOMMENDATIONS = {
    "creators": {
        "hypothesis": ("New and small creators' first posts are not reaching "
                       "likely engagers, so too many posts get zero engagement "
                       "and creator supply softens."),
        "intervention": ("Surface first posts from new/small creators to likely "
                         "engagers, and prompt a first reply on those posts."),
        "primary_metric": "daily_creators and creator_penetration",
        "guardrails": ["orphan_post_rate", "d7_retention", "engagement_rate", "top1_share"],
        "population": "new and small creators on the scoped app",
    },
    "retention": {
        "hypothesis": ("New cohorts are not forming a first-week habit, so D7 "
                       "retention falls even while DAU is up."),
        "intervention": ("Improve first-week onboarding and re-engagement for "
                         "new cohorts, with a clear first-week return prompt."),
        "primary_metric": "d7_retention",
        "guardrails": ["dau", "messaging_dau", "engagement_rate"],
        "population": "new signups on the scoped app",
    },
    "growth": {
        "hypothesis": ("Activation after signup is weak, so DAU growth is not "
                       "keeping up with signups."),
        "intervention": ("Improve signup-to-first-session activation for new users."),
        "primary_metric": "dau",
        "guardrails": ["d7_retention", "sessions_per_dau"],
        "population": "new signups on the scoped app",
    },
    "messaging": {
        "hypothesis": ("Messaging is not becoming a habit for new users, so "
                       "messaging DAU lags overall DAU."),
        "intervention": ("Prompt a first message / reply in the first week, "
                         "targeted at new users with no sent message yet."),
        "primary_metric": "messaging_dau",
        "guardrails": ["d7_retention", "dau"],
        "population": "new users on the scoped app with no sent message yet",
    },
    "engagement": {
        "hypothesis": ("Feed/Reels relevance is soft for low-engagement segments, "
                       "so engagement per impression falls without DAU falling."),
        "intervention": ("Improve ranking relevance for low-engagement segments."),
        "primary_metric": "engagement_rate",
        "guardrails": ["creator_penetration", "d7_retention", "reels_completion"],
        "population": "low-engagement users on the scoped app",
    },
    "concentration": {
        "hypothesis": ("Attention is concentrated on a few creators, so new and "
                       "small creators' posts get zero engagement."),
        "intervention": ("Surface first posts from new/small creators to likely "
                         "engagers, as for the creators topic."),
        "primary_metric": "orphan_post_rate (lower is better)",
        "guardrails": ["daily_creators", "d7_retention", "top1_share"],
        "population": "new and small creators on the scoped app",
    },
}


def _word(q, w):
    return re.search(rf"\b{re.escape(w)}\b", q) is not None


def _find_segment(q):
    """A segment slot in a follow-up turn, or None.

    Only fires on a segment cue ("only", "just", "in", "for", "on"),
    so "India is growing" is not silently read as a filter, and so a
    first-turn weather question ("what's happening with Instagram in
    India") is not hijacked — the weather branch runs first there
    because no weather report exists yet. Full country names only,
    as in the correction turn (the code "in" is also an English word).
    """
    cue = (q.startswith("only ") or q.startswith("just ") or " only " in q
           or " just " in q or " in " in q or " for " in q or " on " in q)
    if not cue:
        return None
    from .planner import ACQUISITIONS, COUNTRIES, PLATFORMS, SURFACES
    for name, code in COUNTRIES.items():
        if len(name) > 2 and _word(q, name):
            return ("country", code)
    for plat in PLATFORMS:
        if _word(q, plat):
            return ("platform", plat)
    for surf in SURFACES:
        if _word(q, surf):
            return ("surface", surf)
    for acq in ACQUISITIONS:
        if _word(q, acq):
            return ("acquisition", acq)
    return None


def _window_label(start, end):
    """Human label for a plan window (start/end ISO dates).

    Full months get their name ("March 2026"); the two short
    trailing windows get their span; anything else is spelled out
    as a date range. Only used to SAY the window in a reply — the
    window itself always comes from the planner's filters.
    """
    import datetime
    months = {("2026-01-01", "2026-01-31"): "January 2026",
              ("2026-02-01", "2026-02-28"): "February 2026",
              ("2026-03-01", "2026-03-31"): "March 2026",
              ("2026-01-01", "2026-03-31"): "Jan 1 – Mar 31, 2026"}
    if (start, end) in months:
        return months[(start, end)]
    s = datetime.date.fromisoformat(start)
    e = datetime.date.fromisoformat(end)
    if s.year == e.year and s.month == e.month:
        return f"{s.strftime('%b')} {s.day}–{e.day}, {s.year}"
    return f"{s.strftime('%b %d')} – {e.strftime('%b %d')}, {e.year}"


def _window_phrase(start, end):
    """Question phrasing that reproduces a plan window.

    The planner only produces these windows (_date_range), so the
    mapping is exhaustive: a full month, a trailing 7/28-day window,
    or the full 90 days (no phrase needed). Used to re-ask the same
    daily series for another app with the window carried over.
    """
    phrases = {("2026-01-01", "2026-01-31"): " in January 2026",
               ("2026-02-01", "2026-02-28"): " in February 2026",
               ("2026-03-01", "2026-03-31"): " in March 2026",
               ("2026-03-04", "2026-03-31"): " over the last 28 days",
               ("2026-03-25", "2026-03-31"): " over the last 7 days",
               ("2026-01-01", "2026-03-31"): ""}
    return phrases.get((start, end), "")


def _fmt(v, unit):
    if v is None:
        return "n/a"
    if unit == "share":
        return f"{v * 100:.1f}%"
    if unit == "minutes":
        return f"{v:.1f} min"
    return f"{v:,.0f}"


def _cards(weather):
    return {c["id"]: c for c in weather["cards"]}


def _find_repo_root(data_dir):
    """Find the repo root from a data dir, for chat/questions.json."""
    for parent in [Path(data_dir)] + list(Path(data_dir).parents):
        if (parent / "chat" / "questions.json").exists() or (parent / "catalog" / "measures.yaml").exists():
            return parent
    return Path(__file__).resolve().parents[2]


class ChatSession:
    """One multi-turn conversation. Create one per UI session id."""

    def __init__(self, data_dir, variant="baseline"):
        self.data_dir = Path(data_dir)
        self.variant = variant
        self.repo_root = _find_repo_root(data_dir)
        # Flow questions come from chat/questions.json when it exists
        # (the Questions editor writes it); otherwise the hardcoded
        # defaults in questions.py, which match chat.py's old constants.
        # A new session loads the file again, so editing questions in
        # the editor takes effect for the next chat, not just on restart.
        self.flow = load_flow(self.repo_root)
        self.topic_labels = {t: spec["label"] for t, spec in self.flow["topics"].items()}
        self.recommendations = self.flow["recommendations"]
        self.diagnosis_priority = self.flow.get("diagnosis_priority") or ["creators", "retention", "growth", "engagement"]
        self.app = None
        self.stage = "start"
        self.weather = None
        self.topic = None
        self.options = []
        self.history = []
        # Correction state (see _correction_turn): the last grain the
        # user asked for, filters they corrected to, and the log of
        # corrections so the next step can say what changed.
        self.grain_preference = None
        self.filters = {}
        self.corrections = []
        # Daily-series state (see _daily_series_turn): the window the
        # last series ran over, so an app-switch follow-up ("what
        # about WhatsApp?") re-runs the same series for the new app
        # instead of opening a weather report.
        self.series_window = None
        # Sticky MAU context (see _breakdown_period): the app + March
        # 2026 period the last MAU turn established, so a breakdown
        # works off it even after the first breakdown moves the stage
        # off "mau" -- chained breakdowns in one session keep
        # resolving to the same app and period. Cleared by a weather
        # or daily-series turn (those establish their own context);
        # never cleared by a breakdown turn (that is the point); a
        # new MAU turn re-sets it.
        self.mau_context = None
        # Last single SQL executed by a drill-down/breakdown helper, for
        # the "Show the working" panel. Weather, diagnosis, recommendation
        # and brief turns run many queries or none, so they set nothing
        # here and honestly get no single-SQL panel, rather than an
        # invented one.
        self.last_working = None

    # -- DuckDB helpers (same tables as weather.py, read-only views) ----
    def _connect(self):
        import duckdb

        con = duckdb.connect()
        for t in ["fact_sessions", "fact_content_impressions",
                  "fact_content_engagements", "fact_video_plays",
                  "fact_messages", "fact_content_creation",
                  "dim_users", "dim_content"]:
            p = self.data_dir / f"{t}.parquet"
            if p.exists():
                con.execute(f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM read_parquet('{p}')")
        return con

    def _monthly_creations(self):
        """Creations per month, scoped to the session app. History for the
        creator drill-down: Jan/Feb/Mar 2026 in this dataset."""
        con = self._connect()
        app_filter = f" AND c.app = '{self.app}'" if self.app else ""
        _sql = (
            "SELECT CAST(DATE_TRUNC('month', CAST(f.published_at AS DATE)) AS VARCHAR) AS month, "
            "COUNT(*) AS posts, COUNT(DISTINCT f.creator_id) AS creators "
            "FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
            f"WHERE 1=1{app_filter} GROUP BY month ORDER BY month")
        rows = con.execute(_sql).fetchall()
        # DATE_TRUNC returns a timestamp; monthly labels are YYYY-MM only.
        monthly = [{"month": str(r[0])[:7], "posts": r[1], "creators": r[2]} for r in rows]
        self.last_working = {"sql": _sql, "tables": ["fact_content_creation", "dim_content"],
                             "row_count": len(rows),
                             "result_columns": ["month", "posts", "creators"],
                             "result_rows": [[m["month"], m["posts"], m["creators"]] for m in monthly]}
        return monthly

    def _user_filter_sql(self, alias, filters):
        """Allowlisted user filters only (country/platform/acquisition).
        Values come from the planner's allowlists, never from raw text,
        so a correction cannot inject a new filter or a new table."""
        conds = []
        if filters.get("country"):
            conds.append(f"{alias}.country = '{filters['country']}'")
        if filters.get("platform"):
            conds.append(f"{alias}.platform = '{filters['platform']}'")
        if filters.get("acquisition"):
            conds.append(f"{alias}.acquisition_source = '{filters['acquisition']}'")
        return conds

    def _daily_dau_series(self, filters=None):
        """Daily DAU over the weather current period, scoped to the app
        and to any corrected user filters (country/platform/acquisition)."""
        if not self.weather:
            return []
        start, end = self.weather["current_period"]
        con = self._connect()
        app_filter = f" AND u.primary_app = '{self.app}'" if self.app else ""
        extra = ""
        for c in self._user_filter_sql("u", filters or {}):
            extra += f" AND {c}"
        _sql = (
            "SELECT CAST(s.start_time AS DATE) AS d, COUNT(DISTINCT s.user_id) AS dau "
            "FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id "
            f"WHERE CAST(s.start_time AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter}{extra} "
            "GROUP BY d ORDER BY d")
        rows = con.execute(_sql).fetchall()
        series = [{"date": str(r[0]), "dau": r[1]} for r in rows]
        self.last_working = {"sql": _sql, "tables": ["fact_sessions", "dim_users"],
                             "row_count": len(rows),
                             "result_columns": ["date", "dau"],
                             "result_rows": [[s["date"], s["dau"]] for s in series]}
        return series

    def _daily_series_for_topic(self, topic, filters=None):
        """Daily series for a drill-down topic, or None if Phase 3 has
        no daily series for that topic. Used by grain corrections:
        'daily, not average' needs actual daily values, not a summary."""
        if not self.weather or topic not in ("growth", "creators", "messaging"):
            return None
        start, end = self.weather["current_period"]
        con = self._connect()
        filters = filters or {}
        if topic == "growth":
            return self._daily_dau_series(filters)
        if topic == "creators":
            app_filter = f" AND c.app = '{self.app}'" if self.app else ""
            surf = f" AND c.surface = '{filters['surface']}'" if filters.get("surface") else ""
            extra = ""
            for c in self._user_filter_sql("u", filters):
                extra += f" AND {c}"
            rows = con.execute(
                "SELECT CAST(f.published_at AS DATE) AS d, COUNT(DISTINCT f.creator_id) AS creators "
                "FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
                "JOIN dim_users u ON u.user_id = f.creator_id "
                f"WHERE CAST(f.published_at AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter}{surf}{extra} "
                "GROUP BY d ORDER BY d").fetchall()
            return [{"date": str(r[0]), "value": r[1]} for r in rows]
        # messaging
        app_filter = f" AND u.primary_app = '{self.app}'" if self.app else ""
        extra = ""
        for c in self._user_filter_sql("u", filters):
            extra += f" AND {c}"
        rows = con.execute(
            "SELECT CAST(m.sent_at AS DATE) AS d, COUNT(DISTINCT m.sender_id) AS senders "
            "FROM fact_messages m JOIN dim_users u ON u.user_id = m.sender_id "
            f"WHERE CAST(m.sent_at AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter}{extra} "
            "GROUP BY d ORDER BY d").fetchall()
        return [{"date": str(r[0]), "value": r[1]} for r in rows]

    def _grain_and_filter_note(self, topic):
        """Text appended to a drill-down when a correction changed its
        grain or filters. States what changed and what did NOT change:
        weather cards stay app-scoped only; only the series is filtered."""
        notes = []
        if self.grain_preference == "daily":
            series = self._daily_series_for_topic(topic, self.filters)
            if series:
                key = "dau" if topic == "growth" else "value"
                vals = ", ".join(f"{s['date']}: {s[key]:,}" for s in series)
                notes.append(f"Daily values (corrected grain, current period): {vals}.")
            else:
                notes.append("Daily grain requested, but Phase 3 has no daily series for this topic — showing the averaged cards instead.")
        if self.filters:
            filt = ", ".join(f"{k}={v}" for k, v in self.filters.items())
            notes.append(f"Filtered by: {filt}. Note: weather cards are app-scoped only; this series is filtered, the cards are not.")
        return ("\n" + "\n".join(notes)) if notes else ""

    # -- Turn routing -----------------------------------------------------
    def _set_options(self, items):
        """Set the option buttons for the next turn.

        Each item is either a drill-down topic string (label from
        TOPIC_LABELS, send = the topic name) or a dict with label/send/
        topic. The send text is the exact message the button sends, so a
        button can advance the conversation (diagnosis, recommendation,
        brief) instead of re-offering the same drill-down.

        Bug fixed 2026-10-28: the drill-down turn used to re-offer the
        same drill-down topics, so clicking a) Creators after a creators
        drill-down just opened the same panel again and again. Options
        after a drill-down are now the NEXT steps, not the same topics.
        """
        out = []
        for i, item in enumerate(items):
            if isinstance(item, str):
                out.append({"key": chr(ord("a") + i), "label": self.topic_labels.get(item, TOPIC_LABELS.get(item, item)),
                            "topic": item, "send": item})
            else:
                out.append({"key": chr(ord("a") + i), "label": item["label"],
                            "topic": item.get("topic"), "send": item.get("send", item.get("topic"))})
        self.options = out
        return self.options

    def _mau_turn(self, app):
        """Standalone MAU turn (2026-11-04): a single metric, so it
        needs no weather report first — unlike drill-down, diagnosis,
        recommendation, and brief, which all read the weather cards.
        The number comes from the planner's app-scoped mau_march
        template, so the SQL, tables, and metric definition in the
        working payload are the same receipts the single-shot ask UI
        shows. The working payload carries one scalar row, so the
        visualization builder honestly renders table-only (no chart
        for a single number) and a diagram of the actual query:
        sources -> filters -> aggregation -> output, with no joins
        or grouping stages invented for a query that has none.
        """
        from .planner import metric_definition, plan, run_plan
        self.app = app
        self.stage = "mau"
        self.topic = None
        # Sticky context for breakdowns: March 2026, the MAU period.
        # Survives the breakdown turn, so chained breakdowns keep
        # resolving to this app and period until a weather or
        # daily-series turn clears it.
        self.mau_context = {"app": app, "start": "2026-03-01", "end": "2026-03-31"}
        p = plan(f"MAU for {app}", variant=self.variant)
        if p["type"] != "query" or not p.get("sql"):
            return self._clarify("I could not build the MAU query for "
                                 f"{app}. Try \"What's the MAU for Instagram?\"")
        cols, rows, _ms = run_plan(p, self.data_dir)
        value = rows[0][0] if rows else 0
        definition = metric_definition("mau_march")
        self.last_working = {
            "sql": p["sql"], "tables": p["tables"], "row_count": len(rows),
            "result_columns": cols, "result_rows": rows,
            "metric_definition": ({"definition": definition[0], "grain": definition[1],
                                   "denominator": definition[2]} if definition else None),
        }
        others = [a for a in APPS if a != app]
        self._set_options([
            {"label": f"Weather — what's happening with {app}?",
             "send": f"What's happening with {app}?"},
            {"label": f"MAU for {others[0]}",
             "send": f"What's the MAU for {others[0]}?"},
            {"label": f"MAU for {others[1]}",
             "send": f"What's the MAU for {others[1]}?"},
        ])
        text = (f"Monthly active users (MAU) for {app} in March 2026: "
                f"{value:,} users.\n\n"
                "That is distinct users with at least one session on "
                f"{app} in March 2026, app-scoped, not all-apps. "
                "March is the only month MAU is defined for in this "
                "data (the Jan-Mar 2026 window), and the exact SQL is "
                "in the working panel.")
        return self._reply("mau", text, mau={"app": app, "value": value})

    def _mau_by_country_turn(self, app):
        """MAU split by country (2026-11-10): distinct users per
        country for one app, March 2026. The Nov 5 hold ("MAU is only
        defined per app") is reversed: the cut is well-defined --
        each user has exactly one country in dim_users, so the rows
        add up to the MAU total. That is the whole difference from
        MAU by surface, which stays a clarification: one user can
        appear on several surfaces, so per-surface counts would
        double-count. The SQL comes from the planner's mau_by_country
        template (same session-app definition as mau_march), so the
        working panel shows the exact query that produced the
        numbers -- run-and-show, the user judges right/wrong live.
        Like the MAU turn it needs no weather report first, and it
        re-sets the sticky MAU context (same app, March 2026).
        The stage stays "mau", so a further bare "by country" repeats
        this cut and a "what about X?" follow-up still swaps the MAU
        app.
        """
        from .planner import metric_definition, plan, run_plan
        self.app = app
        self.stage = "mau"
        self.topic = None
        self.mau_context = {"app": app, "start": "2026-03-01", "end": "2026-03-31"}
        p = plan(f"MAU for {app} by country", variant=self.variant)
        if p["type"] != "query" or not p.get("sql"):
            return self._clarify("I could not build the MAU-by-country query for "
                                 f"{app}. Try \"What's the MAU for Instagram by country?\"")
        cols, rows, _ms = run_plan(p, self.data_dir)
        total = sum(r[1] for r in rows)
        definition = metric_definition("mau_by_country")
        share_rows = []
        for country, n in rows:
            share_rows.append({"label": country, "count": n, "extra": None,
                               "share": n / total if total else 0.0})
        self.last_working = {
            "sql": p["sql"], "tables": p["tables"], "row_count": len(rows),
            "result_columns": ["label", "count", "share"],
            "result_rows": [[r["label"], r["count"], r["share"]] for r in share_rows],
            "metric_definition": ({"definition": definition[0], "grain": definition[1],
                                   "denominator": definition[2]} if definition else None),
        }
        others = [a for a in APPS if a != app]
        self._set_options([
            {"label": f"MAU by country for {others[0]}",
             "send": f"What's the MAU for {others[0]} by country?"},
            {"label": f"MAU by country for {others[1]}",
             "send": f"What's the MAU for {others[1]} by country?"},
            {"label": f"MAU for {app}",
             "send": f"What's the MAU for {app}?"},
        ])
        lines = [f"- {r['label']}: {r['count']:,} ({r['share'] * 100:.1f}% of the total)"
                 for r in share_rows]
        text = (f"Monthly active users (MAU) for {app} in March 2026, by country:\n" +
                "\n".join(lines) +
                f"\n\nThese are distinct users with at least one session on {app} in March 2026, "
                "split by the user's country. The rows add up to the MAU total of "
                f"{total:,} — each user has exactly one country, so nothing is double-counted. "
                "The exact SQL is in the working panel.")
        return self._reply("mau_by_country", text,
                           mau_by_country={"app": app, "rows": share_rows, "total": total})

    def _creator_share_turn(self, app):
        """Creator share of MAU (2026-11-10): distinct creators with
        >=1 content item published in March 2026 for the app, over
        distinct users with >=1 session in March 2026 for the app
        (the MAU). The Nov 10 live failure: "% of creators of MAU"
        answered the MAU scalar (210,809) -- the question names two
        populations and the turn must answer both numbers plus the
        ratio, never the scalar alone. The creators definition is the
        same one the creators-by-country breakdown uses (p05 sums to
        the same 31,297 for Instagram), so the share is a monthly
        share -- NOT the weather report's daily creator penetration
        (daily creators / DAU, ~10.4% for Instagram); the reply says
        which one it is. Like the MAU turn it needs no weather report
        first, and it re-sets the sticky MAU context (same app, March
        2026) and keeps stage "mau", so a later "by country" still
        gives MAU by country and "creators by country" still gives
        the breakdown. The working panel shows the exact CTE query
        that ran plus a two-row table (creators vs all users, with
        the share); the chart is a bar of the two counts, and the SQL
        diagram is honestly unavailable (WITH/CTE shape, per the
        sqlviz rules -- never a guessed diagram). The payload's pct
        is a percent (14.85), matching the SQL's share_pct column.
        """
        from .planner import metric_definition, plan, run_plan
        self.app = app
        self.stage = "mau"
        self.topic = None
        self.mau_context = {"app": app, "start": "2026-03-01", "end": "2026-03-31"}
        p = plan(f"creator share of MAU for {app}", variant=self.variant)
        if p["type"] != "query" or p.get("metric") != "creator_share" or not p.get("sql"):
            return self._clarify("I could not build the creator-share query for "
                                 f"{app}. Try \"What is the % of creators of MAU in {app}?\"")
        cols, rows, _ms = run_plan(p, self.data_dir)
        if not rows:
            return self._clarify(f"No creator-share result came back for {app}.")
        creators, mau, share_pct = rows[0]
        pct = float(share_pct)
        definition = metric_definition("creator_share")
        self.last_working = {
            "sql": p["sql"], "tables": p["tables"], "row_count": len(rows),
            "result_columns": ["population", "count", "share_of_mau"],
            "result_rows": [["creators", creators, pct / 100.0],
                            ["all users (MAU)", mau, 1.0]],
            "metric_definition": ({"definition": definition[0], "grain": definition[1],
                                   "denominator": definition[2]} if definition else None),
        }
        others = [a for a in APPS if a != app]
        self._set_options([
            {"label": f"MAU by country for {app}",
             "send": f"What's the MAU for {app} by country?"},
            {"label": f"MAU for {others[0]}",
             "send": f"What's the MAU for {others[0]}?"},
            {"label": f"Creator share for {others[0]}",
             "send": f"What is the % of creators of MAU in {others[0]}?"},
        ])
        text = (f"Creators are {pct:.2f}% of MAU for {app} in March 2026: "
                f"{creators:,} creators out of {mau:,} monthly active users.\n\n"
                "That is distinct creators with at least one content item published on "
                f"{app} in March 2026, over distinct users with at least one session on "
                f"{app} in March 2026. This is a monthly share -- not the daily creator "
                "penetration on the weather report (daily creators divided by DAU). "
                "The exact SQL is in the working panel.")
        return self._reply("creator_share", text,
                           creator_share={"app": app, "creators": creators,
                                          "mau": mau, "pct": pct})

    def _mau_creators_by_country_turn(self, app):
        """MAU and creators by country (2026-11-11): one breakdown
        with both populations side by side per country -- distinct
        users with >=1 session in March 2026 (the MAU) and distinct
        creators with >=1 content item published in March 2026, plus
        the share of MAU who are creators. The Nov 11 live failure:
        "I need the Monthly active users and creators by country"
        answered with MAU by country only, dropping the creators half
        -- the question names two populations and the turn must answer
        both, never one alone. The creators column is the EXACT
        creators-by-country definition the p05 oracle uses (so it sums
        to the creator_share creators count) and the MAU column is the
        mau_by_country definition (so it sums to the MAU total); each
        user has exactly one country, so both columns add up to their
        totals. This is a monthly cut -- NOT the weather report's
        daily creator penetration (daily creators / DAU). Like the
        MAU turn it needs no weather report first, and it re-sets the
        sticky MAU context (same app, March 2026) and keeps stage
        "mau", so a later bare "by country" still gives MAU by
        country and "creators by country" still gives the breakdown.
        The working panel shows the exact combined CTE query that ran
        plus the full table (country, MAU, creators, share); the chart
        is a bar of MAU by country (first two columns, per the sqlviz
        rule -- further columns stay in the table only), and the SQL
        diagram is honestly unavailable (WITH/CTE shape, per the
        sqlviz rules -- never a guessed diagram).
        """
        from .planner import metric_definition, plan, run_plan
        self.app = app
        self.stage = "mau"
        self.topic = None
        self.mau_context = {"app": app, "start": "2026-03-01", "end": "2026-03-31"}
        p = plan(f"MAU and creators for {app} by country", variant=self.variant)
        if p["type"] != "query" or p.get("metric") != "mau_creators_by_country" or not p.get("sql"):
            return self._clarify("I could not build the MAU-and-creators-by-country query for "
                                 f"{app}. Try \"What are the monthly active users and creators "
                                 f"by country for {app}?\"")
        cols, rows, _ms = run_plan(p, self.data_dir)
        if not rows:
            return self._clarify(f"No MAU-and-creators-by-country result came back for {app}.")
        total_mau = sum(r[1] for r in rows)
        total_creators = sum(r[2] for r in rows)
        definition = metric_definition("mau_creators_by_country")
        combined_rows = []
        for country, mau, creators, share_pct in rows:
            combined_rows.append({"label": country, "mau": mau, "creators": creators,
                                  "share": share_pct / 100.0})
        self.last_working = {
            "sql": p["sql"], "tables": p["tables"], "row_count": len(rows),
            "result_columns": ["label", "mau", "creators", "share"],
            "result_rows": [[r["label"], r["mau"], r["creators"], r["share"]]
                            for r in combined_rows],
            "metric_definition": ({"definition": definition[0], "grain": definition[1],
                                   "denominator": definition[2]} if definition else None),
        }
        others = [a for a in APPS if a != app]
        self._set_options([
            {"label": f"MAU by country for {app}",
             "send": f"What's the MAU for {app} by country?"},
            {"label": f"Creator share for {app}",
             "send": f"What is the % of creators of MAU in {app}?"},
            {"label": f"MAU and creators by country for {others[0]}",
             "send": f"What are the monthly active users and creators by country for {others[0]}?"},
        ])
        lines = [f"- {r['label']}: {r['mau']:,} users, {r['creators']:,} creators "
                 f"({r['share'] * 100:.2f}% of MAU)"
                 for r in combined_rows]
        total_share = (total_creators * 100.0 / total_mau) if total_mau else 0.0
        text = (f"Monthly active users (MAU) and creators for {app} in March 2026, by country:\n" +
                "\n".join(lines) +
                f"\n\nTotal: {total_mau:,} users, {total_creators:,} creators "
                f"({total_share:.2f}% of MAU).\n\n"
                "MAU is distinct users with at least one session on "
                f"{app} in March 2026; creators are distinct users with at least one content item "
                f"published on {app} in March 2026. Each user has exactly one country, so both "
                "columns add up to their totals. This is a monthly cut -- not the daily creator "
                "penetration on the weather report (daily creators divided by DAU). "
                "The exact SQL is in the working panel.")
        return self._reply("mau_creators_by_country", text,
                           mau_creators_by_country={"app": app, "rows": combined_rows,
                                                    "total_mau": total_mau,
                                                    "total_creators": total_creators})

    def _series_question(self, app):
        """Question that re-asks the stored daily series for an app.

        Same window as the last series (from the planner's filters),
        so an app-switch follow-up carries the window over instead
        of silently resetting to the full 90 days.
        """
        phrase = _window_phrase(*self.series_window) if self.series_window else ""
        return f"daily DAU for {app}{phrase}"

    def _daily_series_turn(self, app, question=None):
        """Standalone daily-series turn (2026-11-06): DAU by day.

        A single metric series, so like the MAU turn it needs no
        weather report first. The planner's dau_mean_daily series
        template supplies the date-by-date query, and the working
        payload carries it with the metric definition, so the shell
        renders the line chart + table + SQL diagram from the same
        receipts the single-shot ask UI shows. The turn verifies the
        plan is really the series (grain "user x day") before
        running it: a scalar plan ("mean daily DAU") must never be
        presented as a series, and a question about another metric
        (creators, messaging) has no daily series in the planner and
        clarifies instead of inventing one.
        """
        from .planner import metric_definition, plan, run_plan
        self.app = app
        self.stage = "daily_series"
        self.topic = None
        # A daily series establishes its own context and is not a
        # breakdown context, so it clears the sticky MAU context:
        # after this turn a breakdown asks for weather first again.
        self.mau_context = None
        q = question or f"daily DAU for {app}"
        if not _word(q.lower(), app):
            # The session app is inherited, not named in the question
            # (the probe names no app): say it, or the plan would run
            # the all-apps series while the reply claims one app. The
            # window and any other slot still come from the question.
            q = f"{q} for {app}"
        p = plan(q, variant=self.variant)
        if (p["type"] != "query" or p.get("metric") != "dau_mean_daily"
                or p.get("grain") != "user x day" or not p.get("sql")
                or p.get("filters", {}).get("app") != app):
            return self._clarify("I could not build a daily DAU series for "
                                 f"{app} from that question. Try \"Daily DAU for {app} in March 2026\".")
        cols, rows, _ms = run_plan(p, self.data_dir)
        if not rows:
            return self._clarify(f"No daily DAU series came back for {app} — the window may be empty.")
        start = p["filters"]["date_start"]
        end = p["filters"]["date_end"]
        self.series_window = (start, end)
        label = _window_label(start, end)
        vals = [r[1] for r in rows]
        dates = [str(r[0]) for r in rows]
        mean = sum(vals) / len(vals)
        low_i = vals.index(min(vals))
        high_i = vals.index(max(vals))
        definition = metric_definition("dau_mean_daily")
        self.last_working = {
            "sql": p["sql"], "tables": p["tables"], "row_count": len(rows),
            "result_columns": cols, "result_rows": rows,
            "metric_definition": ({"definition": definition[0], "grain": definition[1],
                                   "denominator": definition[2]} if definition else None),
        }
        others = [a for a in APPS if a != app]
        phrase = _window_phrase(*self.series_window)
        self._set_options([
            {"label": f"Weather — what's happening with {app}?",
             "send": f"What's happening with {app}?"},
            {"label": f"Daily DAU for {others[0]}",
             "send": f"Daily DAU for {others[0]}{phrase}"},
            {"label": f"Daily DAU for {others[1]}",
             "send": f"Daily DAU for {others[1]}{phrase}"},
        ])
        text = (f"Daily active users for {app} in {label}, by day ({len(rows)} days).\n\n"
                f"Mean {mean:,.0f} per day, low {min(vals):,} ({dates[low_i]}), "
                f"high {max(vals):,} ({dates[high_i]}). The full day-by-day series — "
                "line chart, table, and the exact SQL — is in the working panel.\n\n"
                "This is the daily series, not the average: ask for \"mean daily DAU\" "
                "if you want the single average instead.")
        return self._reply("daily_series", text,
                           daily_series={"app": app, "start": start, "end": end,
                                         "days": len(rows), "mean": mean,
                                         "low": min(vals), "low_date": dates[low_i],
                                         "high": max(vals), "high_date": dates[high_i]})

    def _weather_turn(self, app):
        self.app = app
        self.weather = compute_weather(self.data_dir, app=app)
        self.stage = "weather"
        self.topic = None
        # Weather establishes its own context (and wins over MAU
        # context in _breakdown_period), so the sticky flag is
        # cleared: later breakdowns run off the weather period.
        self.mau_context = None
        # Drill-downs follow the watchouts: creators first when a creator
        # watchout fired, retention next when retention fell, then growth.
        cards = _cards(self.weather)
        topics = []
        if cards["daily_creators"]["direction"] == "down" or cards["orphan_post_rate"]["current"] > 0.20:
            topics.append("creators")
        if cards["d7_retention"]["direction"] == "down":
            topics.append("retention")
        if "creators" not in topics:
            topics.append("creators")
        for t in ("growth", "retention", "messaging", "engagement"):
            if t not in topics:
                topics.append(t)
        self._set_options(topics[:4])
        opt_text = "\n".join(f"  {o['key']}) {o['label']}" for o in self.options)
        text = (f"{self.weather['headline']}\n\n"
                f"Watchouts:\n" + "\n".join(f"- {w}" for w in self.weather["watchouts"]) +
                f"\n\nWould you like to look at:\n{opt_text}\n"
                "Reply with a letter (a/b/c/d), a topic name, or ask for a diagnosis.")
        return self._reply("weather", text, weather=self.weather)

    def _drilldown_turn(self, topic):
        self.topic = topic
        self.stage = "drilldown"
        cards = _cards(self.weather)
        if topic == "creators":
            c, p, o, t = cards["daily_creators"], cards["creator_penetration"], cards["orphan_post_rate"], cards["top1_share"]
            monthly = self._monthly_creations()
            monthly_text = ", ".join(f"{m['month']}: {m['posts']:,} posts / {m['creators']:,} creators" for m in monthly)
            evidence = {"daily_creators": c, "creator_penetration": p, "orphan_post_rate": o,
                        "top1_share": t, "monthly_creations": monthly}
            text = (f"Creators for {self.app}: daily creators {_fmt(c['current'], 'users/day')} "
                    f"({c['direction']}, {c['change_pct']:+.1f}% vs prior 28 days), "
                    f"penetration {_fmt(p['current'], 'share')} ({p['direction']}).\n"
                    f"Orphan post rate (whole window, level only): {_fmt(o['current'], 'share')} of posts get zero engagement.\n"
                    f"Top-1% creator impression share (whole window): {_fmt(t['current'], 'share')}.\n"
                    f"Creations by month: {monthly_text}.\n"
                    "That monthly pattern is history, not a cause: it shows supply falling over the window, "
                    "it does not prove why.")
        elif topic == "retention":
            r, d, w = cards["d7_retention"], cards["dau"], cards["wau"]
            evidence = {"d7_retention": r, "dau": d, "wau": w}
            text = (f"Retention for {self.app}: D7 {_fmt(r['current'], 'share')} "
                    f"({r['direction']}, {r['change_pp']:+.1f} points vs prior cohorts), "
                    f"DAU {_fmt(d['current'], 'users/day')} ({d['direction']}), "
                    f"WAU {_fmt(w['current'], 'users/week')} ({w['direction']}).")
        elif topic == "growth":
            d, w = cards["dau"], cards["wau"]
            series = self._daily_dau_series()
            vals = [s["dau"] for s in series]
            evidence = {"dau": d, "wau": w, "daily_dau_series": series}
            text = (f"Growth for {self.app}: DAU {_fmt(d['current'], 'users/day')} "
                    f"({d['direction']}, {d['change_pct']:+.1f}% vs prior 28 days), "
                    f"WAU {_fmt(w['current'], 'users/week')} ({w['direction']}).\n"
                    f"Daily DAU over the current period ({len(series)} days): "
                    f"mean {sum(vals)/len(vals):,.0f}, low {min(vals):,}, high {max(vals):,}." if vals else
                    f"Growth for {self.app}: DAU {_fmt(d['current'], 'users/day')}.")
        elif topic == "messaging":
            m = cards["messaging_dau"]
            evidence = {"messaging_dau": m}
            text = (f"Messaging for {self.app}: messaging DAU {_fmt(m['current'], 'senders/day')} "
                    f"({m['direction']}, {m['change_pct']:+.1f}% vs prior 28 days).")
        elif topic == "engagement":
            e, rc, rw = cards["engagement_rate"], cards["reels_completion"], cards["reels_watch_time"]
            evidence = {"engagement_rate": e, "reels_completion": rc, "reels_watch_time": rw}
            text = (f"Engagement for {self.app}: engagement rate {_fmt(e['current'], 'share')} ({e['direction']}), "
                    f"Reels completion {_fmt(rc['current'], 'share')} ({rc['direction']}), "
                    f"Reels watch {_fmt(rw['current'], 'minutes')} per viewer day ({rw['direction']}).")
        else:  # concentration
            o, t = cards["orphan_post_rate"], cards["top1_share"]
            evidence = {"orphan_post_rate": o, "top1_share": t}
            text = (f"Concentration for {self.app}: orphan post rate {_fmt(o['current'], 'share')} "
                    f"(whole window, level only), top-1% creator share {_fmt(t['current'], 'share')} "
                    "(whole window). Attention is concentrated and many posts get nothing — "
                    "those two levels rhyme, but neither proves the other causes it.")
        # Grain / filter corrections change what the drill-down shows:
        # daily values instead of an averaged summary, and a filtered
        # series instead of the app-wide one. The note states both.
        text += self._grain_and_filter_note(topic)
        # Next steps, not the same drill-downs again: offering "creators"
        # here would loop back to this same panel when clicked.
        self._set_options([
            {"label": "Diagnosis — what's problematic?", "send": "what's the diagnosis?", "topic": topic},
            {"label": "Experiment recommendation — hypothesis to test", "send": "what experiment would you recommend?", "topic": topic},
            {"label": "PM/Eng brief — talking points for a meeting", "send": "give me the PM/Eng brief", "topic": topic},
        ])
        text += ("\n\nNext: use the buttons below for a diagnosis, an experiment recommendation, or the PM/Eng brief — "
                 "or type another topic name (retention, growth, messaging) to switch drill-downs.")
        return self._reply("drilldown", text, drilldown={"topic": topic, "evidence": evidence})

    def _diagnosis_turn(self):
        if not self.weather:
            return self._clarify("Ask for a weather report first — for example, \"What's happening with Instagram?\" — "
                                 "so there is a report to diagnose.")
        cards = _cards(self.weather)
        problem_set = set()
        if cards["orphan_post_rate"]["current"] > 0.20 or cards["daily_creators"]["direction"] == "down" or cards["creator_penetration"]["direction"] == "down":
            problem_set.add("creators")
        if cards["d7_retention"]["direction"] == "down":
            problem_set.add("retention")
        if cards["dau"]["direction"] == "down":
            problem_set.add("growth")
        if cards["engagement_rate"]["direction"] == "down":
            problem_set.add("engagement")
        # Priority comes from the questions file, so editing the
        # diagnosis order in the editor changes this, not code.
        problems = [t for t in self.diagnosis_priority if t in problem_set]
        problems += [t for t in sorted(problem_set) if t not in problems]
        primary = problems[0] if problems else None
        self.topic = primary or self.topic
        self.stage = "diagnosis"
        if not primary:
            text = (f"No metric fired as problematic on the weather thresholds for {self.app}: "
                    "growth, retention, creators and engagement are all flat or up. "
                    "That is a statement about this synthetic window, not a guarantee.")
            return self._reply("diagnosis", text, diagnosis={"primary_topic": None, "problems": []})
        good = [k for k, c in cards.items() if c["good_bad"] == "good"]
        bad = [k for k, c in cards.items() if c["good_bad"] == "bad"]
        c, o = cards["daily_creators"], cards["orphan_post_rate"]
        if primary == "creators":
            # Instagram is the clean "creator-specific" case (growth and
            # retention both up); Whatsapp is not (retention also down),
            # so the sentence must not claim creator-only when other
            # problems fired too.
            if cards["dau"]["direction"] == "up" and cards["d7_retention"]["direction"] == "up":
                context = "Growth and retention are both up, so this is not a general collapse — it is a creator-specific problem hiding inside healthy growth."
            else:
                context = (f"Growth is {cards['dau']['direction']} and retention is "
                           f"{cards['d7_retention']['direction']}, so creators are the first problem, not the only one.")
            detail = (f"The problematic metric is creator supply for {self.app}. "
                      f"Daily creators are {_fmt(c['current'], 'creators/day')} ({c['direction']}), "
                      f"and {_fmt(o['current'], 'share')} of posts get zero engagement (whole window). "
                      f"{context}")
        elif primary == "retention":
            r = cards["d7_retention"]
            detail = (f"The problematic metric is retention for {self.app}: D7 {_fmt(r['current'], 'share')} "
                      f"({r['direction']}, {r['change_pp']:+.1f} points vs prior cohorts).")
        elif primary == "growth":
            d = cards["dau"]
            detail = (f"The problematic metric is growth for {self.app}: DAU {_fmt(d['current'], 'users/day')} "
                      f"({d['direction']}, {d['change_pct']:+.1f}% vs prior 28 days).")
        else:
            e = cards["engagement_rate"]
            detail = (f"The problematic metric is engagement for {self.app}: engagement rate {_fmt(e['current'], 'share')} "
                      f"({e['direction']}).")
        self._set_options([
            {"label": "Experiment recommendation — hypothesis to test", "send": "what experiment would you recommend?", "topic": primary},
            {"label": "PM/Eng brief — talking points for a meeting", "send": "give me the PM/Eng brief", "topic": primary},
        ])
        text = (detail + f"\n\nGood cards: {', '.join(good) if good else 'none'}. "
                f"Bad cards: {', '.join(bad) if bad else 'none'}. "
                "Next: use the buttons below for an experiment recommendation or the PM/Eng brief.")
        return self._reply("diagnosis", text, diagnosis={"primary_topic": primary, "problems": problems})

    def _recommendation_turn(self):
        if not self.weather:
            return self._clarify("Ask for a weather report first, so the recommendation has a metric to target.")
        topic = self.topic or self._diagnosis_topic()
        rec = self.recommendations.get(topic) or RECOMMENDATIONS[topic]
        self.topic = topic
        self.stage = "recommendation"
        recommendation = {
            "label": "HYPOTHESIS — not a forecast",
            "topic": topic,
            "hypothesis": rec["hypothesis"],
            "intervention": rec["intervention"],
            "primary_metric": rec["primary_metric"],
            "guardrails": rec["guardrails"],
            "population": rec["population"],
            "what_would_change_mind": ("If the primary metric does not move, or a guardrail breaches, "
                                       "the hypothesis is wrong for this population — do not ship on the primary alone."),
        }
        text = (f"Recommendation — HYPOTHESIS, not a forecast (no experiment assignment data yet, "
                f"so no lift is promised).\n\n"
                f"Hypothesis: {rec['hypothesis']}\n"
                f"Experiment to run: {rec['intervention']}\n"
                f"Population: {rec['population']}.\n"
                f"Primary metric: {rec['primary_metric']}. Guardrails: {', '.join(rec['guardrails'])}.\n"
                f"What would change my mind: {recommendation['what_would_change_mind']}\n"
                "Next: use the button below for the PM/Eng brief to take this into a meeting.")
        self._set_options([
            {"label": "PM/Eng brief — talking points for a meeting", "send": "give me the PM/Eng brief", "topic": topic},
        ])
        return self._reply("recommendation", text, recommendation=recommendation)

    def _diagnosis_topic(self):
        cards = _cards(self.weather)
        problem_set = set()
        if cards["orphan_post_rate"]["current"] > 0.20 or cards["daily_creators"]["direction"] == "down":
            problem_set.add("creators")
        if cards["d7_retention"]["direction"] == "down":
            problem_set.add("retention")
        if cards["dau"]["direction"] == "down":
            problem_set.add("growth")
        if cards["engagement_rate"]["direction"] == "down":
            problem_set.add("engagement")
        for t in self.diagnosis_priority:
            if t in problem_set:
                return t
        return "engagement"

    def _brief_turn(self):
        if not self.weather:
            return self._clarify("Ask for a weather report first, so the brief has numbers to quote.")
        topic = self.topic or self._diagnosis_topic()
        rec = self.recommendations.get(topic) or RECOMMENDATIONS[topic]
        cards = _cards(self.weather)
        app = self.app or "the product"
        pm_points = [
            f"{app.capitalize()} is growing (DAU {_fmt(cards['dau']['current'], 'users/day')}, "
            f"{cards['dau']['direction']}) but creator supply is the problem: daily creators "
            f"{_fmt(cards['daily_creators']['current'], 'creators/day')} ({cards['daily_creators']['direction']}) "
            f"and {_fmt(cards['orphan_post_rate']['current'], 'share')} of posts get zero engagement.",
            f"Recommendation (hypothesis, not a forecast): {rec['intervention']}",
            f"Success criteria to agree before launch: primary metric {rec['primary_metric']}; "
            f"guardrails {', '.join(rec['guardrails'])}. If a guardrail breaches, do not ship on the primary alone.",
            "Ask: approve the population and the primary/guardrail set, and the run length needed for a pre-registered sample.",
        ]
        eng_points = [
            f"Build/instrument: {rec['intervention']}",
            "Instrument creator cohorts (new vs established), first-post engagement, and the guardrail metrics daily during the run.",
            "Do not peek and stop early on the primary metric: pre-register sample and duration, then read primary + guardrails together.",
            "Data to surface in the run dashboard: daily creators, penetration, orphan rate (whole-window level only), D7 for the exposed cohorts.",
        ]
        caveats = [
            "Synthetic data, Jan-Mar 2026 only. 'Historical' here is that 90-day window and last-28 vs prior-28.",
            "No experiment assignment tables yet, so this is a hypothesis to test, not evidence from a past experiment and not a lift forecast.",
            "Thresholds (counts 2%, rates 1 point, orphan > 20%, top-1% > 8%) are project choices, not business truth.",
            "Orphan rate and top-1% share are whole-window levels, not trends.",
        ]
        brief = {"pm_points": pm_points, "eng_points": eng_points, "caveats": caveats, "topic": topic}
        self.stage = "brief"
        self._set_options([
            {"label": f"What's happening with {a}?", "send": f"What's happening with {a}?", "topic": a}
            for a in APPS
        ])
        text = ("PM/Eng brief — what to say in the meeting:\n\nFor the PM:\n" +
                "\n".join(f"- {p}" for p in pm_points) +
                "\n\nFor Eng:\n" + "\n".join(f"- {p}" for p in eng_points) +
                "\n\nCaveats to say out loud:\n" + "\n".join(f"- {p}" for p in caveats))
        return self._reply("brief", text, brief=brief)

    def _breakdown_rows(self, topic, dimension):
        """One decomposition query, current weather period only.

        Returns a list of {label, count, extra, share} sorted by count
        descending, or None if this topic/dimension pair has no query
        in Step 1. Country uses the user's country (dim_users);
        surface uses the content's surface (dim_content), so surface
        only exists for content topics (creators, engagement).
        """
        period = self._breakdown_period()
        if period is None:
            return None
        start, end, _label = period
        con = self._connect()
        app_filter_u = f" AND u.primary_app = '{self.app}'" if self.app else ""
        app_filter_c = f" AND c.app = '{self.app}'" if self.app else ""
        rows = None
        _sql, _tables = None, []
        if dimension == "country":
            if topic == "growth":
                _sql = (
                    "SELECT u.country, COUNT(DISTINCT s.user_id) AS n "
                    "FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id "
                    f"WHERE CAST(s.start_time AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter_u} "
                    "GROUP BY u.country ORDER BY n DESC")
                _tables = ["fact_sessions", "dim_users"]
                rows = con.execute(_sql).fetchall()
                rows = [{"label": r[0], "count": r[1], "extra": None} for r in rows]
            elif topic == "creators":
                _sql = (
                    "SELECT u.country, COUNT(DISTINCT f.creator_id) AS creators, COUNT(*) AS posts "
                    "FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
                    "JOIN dim_users u ON u.user_id = f.creator_id "
                    f"WHERE CAST(f.published_at AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter_c} "
                    "GROUP BY u.country ORDER BY creators DESC")
                _tables = ["fact_content_creation", "dim_content", "dim_users"]
                rows = con.execute(_sql).fetchall()
                rows = [{"label": r[0], "count": r[1], "extra": f"{r[2]:,} posts"} for r in rows]
            elif topic == "messaging":
                _sql = (
                    "SELECT u.country, COUNT(DISTINCT m.sender_id) AS n "
                    "FROM fact_messages m JOIN dim_users u ON u.user_id = m.sender_id "
                    f"WHERE CAST(m.sent_at AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter_u} "
                    "GROUP BY u.country ORDER BY n DESC")
                _tables = ["fact_messages", "dim_users"]
                rows = con.execute(_sql).fetchall()
                rows = [{"label": r[0], "count": r[1], "extra": None} for r in rows]
        elif dimension == "surface" and topic in ("creators", "engagement"):
            _sql = (
                "SELECT c.surface, COUNT(*) AS posts, COUNT(DISTINCT f.creator_id) AS creators "
                "FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
                f"WHERE CAST(f.published_at AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter_c} "
                "GROUP BY c.surface ORDER BY posts DESC")
            _tables = ["fact_content_creation", "dim_content"]
            rows = con.execute(_sql).fetchall()
            rows = [{"label": r[0], "count": r[1], "extra": f"{r[2]:,} creators"} for r in rows]
        if not rows:
            return None
        total = sum(r["count"] for r in rows) or 1
        for r in rows:
            r["share"] = r["count"] / total
        if _sql:
            cols = ["label", "count", "extra", "share"] if any(r.get("extra") for r in rows) else ["label", "count", "share"]
            self.last_working = {"sql": _sql, "tables": _tables, "row_count": len(rows),
                                 "result_columns": cols,
                                 "result_rows": [[r["label"], r["count"]] +
                                                 ([r.get("extra")] if "extra" in cols else []) +
                                                 [r["share"]] for r in rows]}
        return rows

    def _breakdown_period(self):
        """(start, end, label) for a breakdown: the weather current
        period, or — after a MAU turn — March 2026, the MAU period.
        Returns None when neither context exists, so the breakdown
        turn still clarifies instead of inventing a window. MAU only:
        the daily-series turn is not breakdown context, and the
        diagnosis / recommendation / brief gates still require weather.
        The MAU context is sticky (self.mau_context): it survives the
        breakdown turn, so a second breakdown in the same session
        still resolves to March 2026 for the same app. A weather or
        daily-series turn clears it; a new MAU turn re-sets it.
        """
        if self.weather:
            start, end = self.weather["current_period"]
            return start, end, f"current period {start} to {end}"
        if self.mau_context:
            return (self.mau_context["start"], self.mau_context["end"],
                    "March 2026 (the MAU period, Mar 1 to Mar 31)")
        return None

    def _breakdown_turn(self, topic, dimension):
        """Decomposition turn: where the numbers sit, never why.

        Country/surface was chosen (2026-10-29) as the first
        decomposition, ahead of creator cohorts, because it needs no
        generator change — the cuts already exist in the tables.
        """
        period = self._breakdown_period()
        if period is None:
            return self._clarify("Ask for a weather report first, then ask for a breakdown — "
                                 "for example, \"creators by country\".")
        rows = self._breakdown_rows(topic, dimension)
        if rows is None:
            return self._clarify(f"I cannot break {topic} down by {dimension} from the Phase 1 tables. "
                                 "Try creators by country, creators by surface, growth by country, "
                                 "or messaging by country.")
        self.topic = topic
        self.stage = "breakdown"
        start, end, period_label = period
        lines = []
        for r in rows:
            extra = f" ({r['extra']})" if r.get("extra") else ""
            lines.append(f"- {r['label']}: {r['count']:,}{extra} ({r['share'] * 100:.1f}% of the total)")
        self._set_options([
            {"label": "Diagnosis — what's problematic?", "send": "what's the diagnosis?", "topic": topic},
            {"label": "Experiment recommendation — hypothesis to test", "send": "what experiment would you recommend?", "topic": topic},
            {"label": "PM/Eng brief — talking points for a meeting", "send": "give me the PM/Eng brief", "topic": topic},
        ])
        text = (f"Breakdown for {self.app}: {topic} by {dimension}, {period_label}:\n" +
                "\n".join(lines) +
                "\n\nThis is a decomposition, not a cause: it shows where the numbers sit, "
                "not why they moved. Next: use the buttons below for a diagnosis, "
                "an experiment recommendation, or the PM/Eng brief.")
        return self._reply("breakdown", text,
                           breakdown={"topic": topic, "dimension": dimension, "rows": rows})

    def _ranking_rows(self, topic, dimension, days):
        """Per-segment metric in a recent window vs the prior window
        of the same length, both ending at the weather current end.

        Returns (recent_label, prior_label, rows) where rows are
        {label, current, prior, change, change_pct, extra} sorted by
        change ascending (largest drop first — callers re-sort for
        growth). Missing in a window counts as 0, so a segment that
        appears only in one window still ranks, instead of vanishing.
        """
        if not self.weather:
            return None
        import datetime
        if days == 28:
            # Default: the weather's own current vs prior periods, so
            # the ranking matches the weather numbers exactly.
            recent = self.weather["current_period"]
            prior = self.weather["prior_period"]
        else:
            end = datetime.date.fromisoformat(self.weather["current_period"][1])
            recent_start = end - datetime.timedelta(days=days - 1)
            prior_end = recent_start - datetime.timedelta(days=1)
            prior_start = prior_end - datetime.timedelta(days=days - 1)
            recent = [recent_start.isoformat(), end.isoformat()]
            prior = [prior_start.isoformat(), prior_end.isoformat()]
        con = self._connect()
        app_filter_u = f" AND u.primary_app = '{self.app}'" if self.app else ""
        app_filter_c = f" AND c.app = '{self.app}'" if self.app else ""

        def _query(start, end):
            if dimension == "country":
                if topic == "growth":
                    rows = con.execute(
                        "SELECT u.country, COUNT(DISTINCT s.user_id) AS n, NULL AS e "
                        "FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id "
                        f"WHERE CAST(s.start_time AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter_u} "
                        "GROUP BY u.country").fetchall()
                elif topic == "creators":
                    rows = con.execute(
                        "SELECT u.country, COUNT(DISTINCT f.creator_id) AS n, COUNT(*) AS e "
                        "FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
                        "JOIN dim_users u ON u.user_id = f.creator_id "
                        f"WHERE CAST(f.published_at AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter_c} "
                        "GROUP BY u.country").fetchall()
                elif topic == "messaging":
                    rows = con.execute(
                        "SELECT u.country, COUNT(DISTINCT m.sender_id) AS n, NULL AS e "
                        "FROM fact_messages m JOIN dim_users u ON u.user_id = m.sender_id "
                        f"WHERE CAST(m.sent_at AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter_u} "
                        "GROUP BY u.country").fetchall()
                else:
                    return {}
            elif dimension == "surface" and topic in ("creators", "engagement"):
                rows = con.execute(
                    "SELECT c.surface, COUNT(*) AS n, COUNT(DISTINCT f.creator_id) AS e "
                    "FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
                    f"WHERE CAST(f.published_at AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter_c} "
                    "GROUP BY c.surface").fetchall()
            else:
                return {}
            return {r[0]: (r[1], r[2]) for r in rows}

        cur = _query(recent[0], recent[1])
        pri = _query(prior[0], prior[1])
        rows = []
        # The extra column's meaning depends on the query: for
        # creators by country it is posts, for creators by surface
        # it is creators (the primary count there is posts). Labelling
        # it "posts" in both cases was a bug caught in testing.
        extra_label = "creators" if dimension == "surface" else ("posts" if topic == "creators" else None)
        for label in sorted(set(cur) | set(pri)):
            c, ce = cur.get(label, (0, None))
            p, pe = pri.get(label, (0, None))
            change = c - p
            rows.append({
                "label": label, "current": c, "prior": p, "change": change,
                "change_pct": (change / p * 100.0) if p else None,
                "extra": (f"{extra_label} {ce:,} (was {pe:,})"
                          if extra_label and ce is not None and pe is not None else None),
            })
        rows.sort(key=lambda r: r["change"])
        return (f"{recent[0]} to {recent[1]}", f"{prior[0]} to {prior[1]}", rows)

    def _ranking_turn(self, topic, dimension, days, direction):
        """'Which country had the most drop in the last 2 weeks?'

        The turn the screenshot showed failing (2026-10-29 22:55 PDT):
        a breakdown lists levels, it cannot answer a change question.
        This turn ranks segments by change over two equal windows and
        names the top segment in the first sentence, with the full
        list behind it so the ranking can be checked — and so a
        reader can see whether 'most drop' is absolute, percent, or
        both (in the WhatsApp creators case it is both: ID, -12 and
        -7.0%). A short window is labelled as such: a 14-day move is
        not the Jan-Mar story, and small counts are noisy.
        """
        if not self.weather:
            return self._clarify("Ask for a weather report first, then ask which segment dropped or grew most — "
                                 "for example, \"which country had the most drop in the last 2 weeks?\"")
        result = self._ranking_rows(topic, dimension, days)
        if result is None or not result[2]:
            return self._clarify(f"I cannot rank {topic} by {dimension} from the Phase 1 tables. "
                                 "Try creators by country, creators by surface, growth by country, "
                                 "or messaging by country.")
        recent_label, prior_label, rows = result
        if direction == "growth":
            rows = sorted(rows, key=lambda r: -r["change"])
        top = rows[0]
        self.topic = topic
        self.stage = "ranking"
        total_cur = sum(r["current"] for r in rows)
        total_pri = sum(r["prior"] for r in rows)
        total_change = total_cur - total_pri
        if direction == "drop" and top["change"] >= 0:
            headline = (f"No {dimension} dropped in this window — the smallest increase is "
                        f"{top['label']} ({top['current']:,} vs {top['prior']:,}, +{top['change']:,}).")
        elif direction == "drop":
            pct = f", {top['change_pct']:+.1f}%" if top["change_pct"] is not None else ""
            headline = (f"The {dimension} with the largest drop is {top['label']}: "
                        f"{top['current']:,} vs {top['prior']:,}, {top['change']:+,}{pct}.")
        else:
            pct = f", {top['change_pct']:+.1f}%" if top["change_pct"] is not None else ""
            headline = (f"The {dimension} with the largest growth is {top['label']}: "
                        f"{top['current']:,} vs {top['prior']:,}, {top['change']:+,}{pct}.")
        lines = []
        for r in rows:
            pct = f", {r['change_pct']:+.1f}%" if r["change_pct"] is not None else ""
            extra = f" ({r['extra']})" if r.get("extra") else ""
            lines.append(f"- {r['label']}: {r['current']:,} (was {r['prior']:,}, {r['change']:+,}{pct}){extra}")
        self._set_options([
            {"label": "Diagnosis — what's problematic?", "send": "what's the diagnosis?", "topic": topic},
            {"label": "Experiment recommendation — hypothesis to test", "send": "what experiment would you recommend?", "topic": topic},
            {"label": "PM/Eng brief — talking points for a meeting", "send": "give me the PM/Eng brief", "topic": topic},
        ])
        if dimension == "surface":
            metric = "posts"
        else:
            metric = {"creators": "creators", "growth": "active users", "messaging": "senders"}.get(topic, topic)
        text = (f"Ranking for {self.app}: {topic} ({metric}) by {dimension}, "
                f"last {days} days ({recent_label}) vs prior {days} days ({prior_label}), "
                f"sorted by largest {'drop' if direction == 'drop' else 'growth'}:\n\n{headline}\n\n" +
                "\n".join(lines) +
                f"\n\nTotal: {total_cur:,} (was {total_pri:,}, {total_change:+,}). "
                "This is a short-window change, not the 28-day weather trend or the Jan-Mar story — "
                "small segment counts in a short window are noisy, so read small moves as noise, "
                "not as a collapse. Next: use the buttons below for a diagnosis, "
                "an experiment recommendation, or the PM/Eng brief.")
        return self._reply("ranking", text,
                           ranking={"topic": topic, "dimension": dimension, "days": days,
                                    "direction": direction, "rows": rows})

    def _why_turn(self, topic):
        """Why-evidence turn, answered only as a decomposition.

        Creators gets both country and surface (the first decomposition
        the user chose); growth/messaging get country. Anything else
        clarifies with the breakdown options instead of inventing a cause.
        """
        if not self.weather:
            return self._clarify("Ask for a weather report first, then ask what is driving a topic — "
                                 "for example, \"what's driving the creator drop?\"")
        if topic == "creators":
            country = self._breakdown_rows("creators", "country") or []
            surface = self._breakdown_rows("creators", "surface") or []
            if not country and not surface:
                return self._clarify("I cannot break creators down from the Phase 1 tables right now.")
            self.topic = "creators"
            self.stage = "breakdown"
            start, end = self.weather["current_period"]

            def _lines(rows):
                return "\n".join(
                    f"- {r['label']}: {r['count']:,}" +
                    (f" ({r['extra']})" if r.get("extra") else "") +
                    f" ({r['share'] * 100:.1f}%)" for r in rows)
            self._set_options([
                {"label": "Diagnosis — what's problematic?", "send": "what's the diagnosis?", "topic": "creators"},
                {"label": "Experiment recommendation — hypothesis to test", "send": "what experiment would you recommend?", "topic": "creators"},
            ])
            text = (f"What's behind the creator numbers for {self.app} — as a decomposition, not a cause "
                    f"(current period {start} to {end}):\n\nBy country:\n{_lines(country)}\n\nBy surface:\n{_lines(surface)}\n\n"
                    "That shows where creator supply sits; it does not prove why it fell. "
                    "Next: use the buttons below for a diagnosis or an experiment recommendation.")
            # Two queries behind this turn (country + surface), so no
            # single SQL panel: showing only the second would mislead.
            self.last_working = None
            return self._reply("breakdown", text,
                               breakdown={"topic": "creators", "dimension": "country+surface",
                                          "rows": country, "surface_rows": surface})
        if topic in ("growth", "messaging"):
            return self._breakdown_turn(topic, "country")
        self._set_options([
            {"label": "Creators by country", "send": "creators by country", "topic": "creators"},
            {"label": "Creators by surface", "send": "creators by surface", "topic": "creators"},
        ])
        return self._clarify(f"I can break creators down by country or surface, but not {topic} by itself. "
                             "Pick one of the breakdowns below, or ask about creators.")

    def _correction_turn(self, q):
        """Handle 'you are not looking at X / wrong grain / incorrect
        parameter' corrections, conversationally, in the current turn.

        Returns a reply dict if q is a correction, else None (so normal
        routing continues). A correction always ends by prompting the
        next step — it never leaves the user at a dead end.

        What a correction can change in Phase 3 (honest scope):
        - app: switches the weather report to that app.
        - topic: switches the drill-down to that topic.
        - grain: daily (full daily series) vs average (summary).
        - filter: country (full name only, e.g. india, not the code
          "in", which is also an English word), platform, surface,
          acquisition — stored on the session and applied to the
          drill-down SERIES only. Weather cards stay app-scoped; the
          reply says so, instead of pretending the cards were filtered.
        What it cannot change: an unrecognised parameter (france,
        tiktok) clarifies with the allowlist, and a grain/filter for a
        topic with no daily series says so instead of inventing one.
        """
        # "What's wrong?" is a diagnosis request, not a correction,
        # unless it also says "instead" / "not looking at" / "should".
        is_diagnosis_q = q.startswith("what's wrong") or q.startswith("what is wrong")
        cue_phrases = ["not looking at", "wrong grain", "incorrect grain", "wrong parameter",
                       "incorrect parameter", "that's wrong", "that is wrong", "that's off",
                       "should look at", "should be looking at", "look at", "use"]
        has_cue = any(p in q for p in cue_phrases) and ("instead" in q or "not looking" in q
                                                       or "wrong" in q or "incorrect" in q
                                                       or "should" in q or "that's" in q or "that is" in q)
        if is_diagnosis_q and not ("instead" in q or "not looking" in q):
            return None
        if not has_cue:
            return None
        from .planner import ACQUISITIONS, COUNTRIES, PLATFORMS, SURFACES, INVALID_COUNTRIES
        # Unrecognised parameters clarify with the allowlist, like the planner.
        for bad in INVALID_COUNTRIES:
            if _word(q, bad):
                return self._clarify(f"'{bad}' is not a country in the synthetic data. "
                                     "The countries are US, IN, BR, ID, MX, GB, and DE (use the full name, e.g. india). "
                                     "Which parameter did you mean?")
        if "tiktok" in q:
            return self._clarify("TikTok is not in the Phase 1 synthetic universe. The apps are instagram, whatsapp, and facebook. "
                                 "Which parameter did you mean?")
        # Extract, in order: app, topic, grain, filter. At most one
        # primary change per correction turn, so the reply stays clear.
        for app in APPS:
            if _word(q, app):
                old = self.app
                reply = self._weather_turn(app)
                change = f"Corrected: switching app from {old or 'none'} to {app}."
                self.corrections.append({"parameter": "app", "value": app, "applied": True})
                reply["text"] = f"Got it — {change}\n\n{reply['text']}"
                reply["correction"] = {"parameter": "app", "value": app, "applied": True}
                return reply
        for topic in self.topic_labels:
            if _word(q, topic) or topic in q:
                reply = self._drilldown_turn(topic) if self.weather else None
                if reply is None:
                    return self._clarify("Ask for a weather report first, then correct the topic.")
                self.corrections.append({"parameter": "topic", "value": topic, "applied": True})
                reply["text"] = f"Got it — corrected: looking at {topic} instead.\n\n{reply['text']}"
                reply["correction"] = {"parameter": "topic", "value": topic, "applied": True}
                return reply
        grain = None
        if "daily" in q or "per day" in q or "each day" in q:
            grain = "daily"
        elif "average" in q or "mean" in q or "averaged" in q:
            grain = "average"
        if grain:
            self.grain_preference = grain
            topic = self.topic or "growth"
            if not self.weather:
                return self._clarify("Ask for a weather report first, then tell me the grain you want.")
            reply = self._drilldown_turn(topic)
            self.corrections.append({"parameter": "grain", "value": grain, "applied": True})
            reply["text"] = f"Got it — corrected: showing {grain} grain for {topic} instead of the summary.\n\n{reply['text']}"
            reply["correction"] = {"parameter": "grain", "value": grain, "applied": True}
            return reply
        # Filters: full country names only (see scope note above).
        for name, code in COUNTRIES.items():
            if len(name) > 2 and _word(q, name):
                self.filters["country"] = code
                return self._filter_correction("country", code)
        for plat in PLATFORMS:
            if _word(q, plat):
                self.filters["platform"] = plat
                return self._filter_correction("platform", plat)
        for surf in SURFACES:
            if _word(q, surf):
                self.filters["surface"] = surf
                return self._filter_correction("surface", surf)
        for acq in ACQUISITIONS:
            if _word(q, acq):
                self.filters["acquisition"] = acq
                return self._filter_correction("acquisition", acq)
        # Cue, but no recognisable parameter: prompt conversationally
        # for the next step instead of guessing.
        self._set_options(["creators", "retention", "growth", "messaging"])
        return self._clarify("Got it — something is off, but I could not tell which parameter you mean. "
                             "Tell me the app, topic, grain (daily vs average), or filter "
                             "(country, platform, surface, acquisition) — for example, "
                             "\"you are not looking at retention\" or \"show daily, not average\". "
                             "Then I will re-run that step and prompt you for the next one.")

    def _filter_correction(self, parameter, value):
        if not self.weather:
            return self._clarify("Ask for a weather report first, then tell me the filter to apply.")
        topic = self.topic or "growth"
        reply = self._drilldown_turn(topic)
        self.corrections.append({"parameter": parameter, "value": value, "applied": True})
        reply["text"] = (f"Got it — corrected: applying {parameter}={value} to the {topic} drill-down series. "
                         "Note: weather cards are app-scoped only, so only the series is filtered, not the cards.\n\n"
                         f"{reply['text']}")
        reply["correction"] = {"parameter": parameter, "value": value, "applied": True}
        return reply

    def _clarify(self, text):
        self.stage = "clarification"
        return self._reply("clarification", text)

    def _reply(self, type_, text, **extra):
        out = {"type": type_, "stage": self.stage, "app": self.app,
               "text": text, "options": self.options}
        out.update(extra)
        if getattr(self, "last_working", None):
            working = dict(self.last_working)
            # Attach the visualization for this single executed SQL:
            # chart + table from its result rows, diagram from its
            # text. Turns with no single SQL set nothing here, so
            # they honestly get no visualization, as before.
            if working.get("sql") and working.get("result_rows") is not None:
                working["visualization"] = build_visualization(
                    working["sql"], working.get("result_columns", []),
                    working.get("result_rows", []))
            # The visualization payload is the public contract; the
            # raw result fields that built it stay internal.
            working.pop("result_columns", None)
            working.pop("result_rows", None)
            out["working"] = working
            self.last_working = None
        self.history.append({"user": None, "assistant": out})
        return out

    # -- Public entry point -------------------------------------------------
    def respond(self, message):
        q = (message or "").strip().lower()
        if not q:
            return self._clarify("Ask something — for example, \"What's happening with Instagram?\"")
        # Letter / number choice for the offered options. The option's
        # send text is what is actually sent, so a letter after a
        # drill-down advances to diagnosis/recommendation/brief instead
        # of re-opening the same drill-down.
        if q in ("a", "b", "c", "d", "1", "2", "3", "4") and self.options and (self.weather or self.stage in ("mau", "daily_series")):
            idx = ord(q) - ord("a") if q.isalpha() else int(q) - 1
            if 0 <= idx < len(self.options):
                send = self.options[idx].get("send") or self.options[idx].get("topic")
                if send and send.lower() != q:
                    return self.respond(send)
        # Correction first: "you are not looking at X" must change the
        # current step, not be treated as a new weather question.
        corrected = self._correction_turn(q)
        if corrected is not None:
            return corrected
        # -- Turn-by-turn Step 1: intent + slots + context -----------------
        # Slots found in this turn, before any intent is chosen.
        app_found = next((a for a in APPS if _word(q, a)), None)
        topic_found = next((t for t in self.topic_labels
                            if _word(q, t) or t in q), None)
        # Follow-up: "what about X?", "and X", "same for X" means the
        # same intent with one slot swapped, inherited from context.
        stripped = q
        for prefix in FOLLOWUP_PREFIXES:
            if q.startswith(prefix + " "):
                stripped = q[len(prefix) + 1:].strip()
                break
        stripped_clean = stripped.rstrip("?!.").strip()
        if stripped_clean in APPS:
            if self.stage == "mau":
                # MAU follow-up ("what about WhatsApp?" after a MAU
                # turn, or a bare app name) keeps the MAU intent and
                # swaps the app, instead of opening a weather report.
                return self._mau_turn(stripped_clean)
            if self.stage == "daily_series":
                # Daily-series follow-up keeps the series intent and
                # the window, and swaps the app.
                return self._daily_series_turn(stripped_clean, self._series_question(stripped_clean))
            if q.startswith("same for") and self.topic and self.weather:
                # Same drill-down, different app: weather the new app
                # first (so its numbers are the receipt), then re-run
                # the same drill-down on it.
                old_topic = self.topic
                self._weather_turn(stripped_clean)
                reply = self._drilldown_turn(old_topic)
                reply["text"] = (f"Same drill-down, switched app to {stripped_clean}.\n\n"
                                 f"{reply['text']}")
                return reply
            return self._weather_turn(stripped_clean)
        if stripped_clean in self.topic_labels:
            if self.weather:
                return self._drilldown_turn(stripped_clean)
            return self._clarify("Ask for a weather report first — for example, "
                                 "\"What's happening with Instagram?\" — then pick a topic.")
        # Creator share of MAU (2026-11-10): "% of creators of MAU"
        # and "number of creators and all users" are one question --
        # the monthly creator share -- not the MAU scalar and not a
        # breakdown. Placed before the MAU block so a question naming
        # creators + MAU/users never falls through to the MAU scalar
        # (the Nov 10 live failure: "% of creators of MAU" answered
        # 210,809). Must not hijack: "creators by country/surface"
        # (breakdown), "what's driving the creator drop" (diagnosis),
        # plain MAU, "MAU by country". The bare follow-up form after
        # a MAU turn ("what is the % of creators of MAU", no app
        # named) inherits the app from the sticky MAU context or the
        # session app.
        if "creator" in q and (
                (("mau" in q or "monthly active" in q or "all users" in q)
                 and ("share" in q or "%" in q or "percent" in q or "of mau" in q
                      or "and all users" in q or "creators of" in q))
                or ("share" in q and "users" in q)) \
                and " by country" not in q and " by surface" not in q \
                and "drop" not in q and "driving" not in q:
            app = (app_found or (self.mau_context["app"] if self.mau_context else None)
                   or self.app)
            if app:
                return self._creator_share_turn(app)
            self._set_options([
                {"label": a, "send": f"What is the % of creators of MAU in {a}?", "topic": None} for a in APPS
            ])
            return self._reply("clarification",
                               "Which app's creator share of MAU? Reply instagram, whatsapp, or "
                               "facebook — for example, \"What is the % of creators of MAU in Instagram?\"")
        # MAU and creators by country (2026-11-11): "I need the
        # Monthly active users and creators by country" is one
        # question -- a breakdown with both populations side by side
        # -- not MAU by country alone (the Nov 11 live failure: the
        # creators half was dropped). Placed after the creator_share
        # block (which excludes " by country", so it never fires
        # here) and before the MAU block (which would otherwise take
        # "monthly active" + " by country" to mau_by_country). The
        # %-by-country form ("What is the % of creators of MAU ...
        # by country?") also lands here in chat -- the combined turn
        # answers the per-country shares -- while the single-shot
        # planner keeps the creator_share refusal for it (pinned v7
        # test; see the planner branch comment). App from the
        # question, else the sticky MAU context, else the session
        # app, else clarify which app.
        if "creator" in q and ("mau" in q or "monthly active" in q) and " by country" in q:
            app = (app_found or (self.mau_context["app"] if self.mau_context else None)
                   or self.app)
            if app:
                return self._mau_creators_by_country_turn(app)
            self._set_options([
                {"label": a, "send": f"What are the monthly active users and creators by country for {a}?", "topic": None} for a in APPS
            ])
            return self._reply("clarification",
                               "Which app's MAU and creators by country? Reply instagram, whatsapp, or "
                               "facebook — for example, \"What are the monthly active users and creators "
                               "by country for Instagram?\"")
        # MAU intent (2026-11-04): a standalone metric turn. The
        # keyword is "mau", so the probe's typo ("teh") does not
        # matter, and no weather report is required first. Placed
        # before breakdown/ranking/why/weather so a MAU question is
        # never swallowed by them. Stickiness questions ("dau/mau")
        # are not MAU questions and fall through to clarification,
        # as before. MAU by country is its own turn (2026-11-10);
        # MAU by surface still clarifies.
        if ("mau" in q or "monthly active" in q) and "stickiness" not in q \
                and "dau/mau" not in q and "dau / mau" not in q:
            # MAU by surface stays a clarification (2026-11-10): one
            # user can appear on several surfaces, so per-surface
            # distinct-user counts would add up to more than the MAU
            # total. MAU by country (hold reversed 2026-11-10) is a
            # real turn: each user has exactly one country, so the
            # rows add up to the MAU total.
            if " by surface" in q:
                return self._clarify("MAU can't be broken down by surface — one user can appear on "
                                     "several surfaces, so per-surface distinct-user counts would add "
                                     "up to more than the MAU total. Ask \"What's the MAU for "
                                     "Instagram?\" or \"MAU for Instagram by country\".")
            if " by country" in q:
                app = (app_found or (self.mau_context["app"] if self.mau_context else None)
                       or self.app)
                if app:
                    return self._mau_by_country_turn(app)
                self._set_options([
                    {"label": a, "send": f"What's the MAU for {a} by country?", "topic": None} for a in APPS
                ])
                return self._reply("clarification",
                                   "Which app's MAU by country? Reply instagram, whatsapp, or "
                                   "facebook — for example, \"What's the MAU for Instagram by country?\"")
            app = app_found or self.app
            if app:
                return self._mau_turn(app)
            self._set_options([
                {"label": a, "send": f"What's the MAU for {a}?", "topic": None} for a in APPS
            ])
            return self._reply("clarification",
                               "Which app's MAU? Reply instagram, whatsapp, or facebook — "
                               "for example, \"What's the MAU for Instagram?\"")
        # Daily-series intent (2026-11-06): "DAU by day" asks for the
        # date-by-date series, not the averaged scalar. Placed after
        # MAU (a "MAU trend" question is a MAU scalar, not a series)
        # and before breakdown (a series is not a decomposition).
        # "mean"/"average" stays the scalar; MAU/WAU have no daily
        # series in the planner; "by country/surface/platform/app/
        # acquisition" is a breakdown, not a series.
        if (("dau" in q or "daily active" in q)
                and ("by day" in q or "individual day" in q or "each day" in q
                     or "daily" in q or "per day" in q or "trend" in q or "over time" in q)
                and "mean" not in q and "average" not in q
                and "mau" not in q and "wau" not in q
                and " by country" not in q and " by surface" not in q
                and " by platform" not in q and " by app" not in q
                and " by acquisition" not in q):
            app = app_found or self.app
            if app:
                return self._daily_series_turn(app, message)
            self._set_options([
                {"label": a, "send": f"Daily DAU for {a}?", "topic": None} for a in APPS
            ])
            return self._reply("clarification",
                               "Which app's daily DAU? Reply instagram, whatsapp, or facebook — "
                               "for example, \"Daily DAU for Instagram in March 2026\".")
        # Breakdown: "creators by country", "creators by surface".
        # This is the country/surface decomposition the user chose
        # first (2026-10-29), ahead of creator cohorts. The trigger
        # has no leading-space requirement so a bare "by surface"
        # (no topic named) also lands here and defaults to creators.
        # After a MAU turn, a bare "by country" (no topic named)
        # cuts the MAU number just shown: distinct users per country,
        # using the sticky MAU context. A named topic ("creators by
        # country") still takes the breakdown turn, as does "by
        # country" after a breakdown (a dimension switch on the same
        # topic) -- the stage gate is what distinguishes them. "by
        # surface" after MAU stays the creators breakdown: MAU by
        # surface is undefined (double-counting), so there is no MAU
        # cut for it to mean.
        if "by country" in q and not topic_found and self.stage == "mau":
            app = (self.mau_context["app"] if self.mau_context else None) or self.app
            if app:
                return self._mau_by_country_turn(app)
            self._set_options([
                {"label": a, "send": f"What's the MAU for {a} by country?", "topic": None} for a in APPS
            ])
            return self._reply("clarification",
                               "Which app's MAU by country? Reply instagram, whatsapp, or "
                               "facebook — for example, \"What's the MAU for Instagram by country?\"")
        if "by country" in q or "by surface" in q:
            dimension = "country" if "by country" in q else "surface"
            topic = topic_found or self.topic or "creators"
            return self._breakdown_turn(topic, dimension)
        # Ranking by change: "which country had the most drop in the
        # last 2 weeks?" — the turn that failed in the 2026-10-29
        # 22:55 PDT screenshot. A breakdown lists levels; it cannot
        # answer a change question, so this is its own intent.
        # Period: an explicit "last N days/weeks" wins; otherwise the
        # weather's current vs prior 28 days. Direction: drop words
        # rank most-negative first, growth words most-positive first.
        # Topic/dimension inherit from context when not named.
        if ("most drop" in q or "most decline" in q or "dropped most" in q
                or "biggest drop" in q or "largest drop" in q or "most fell" in q
                or "most growth" in q or "grew most" in q or "biggest increase" in q
                or "largest increase" in q or "increased most" in q):
            direction = "growth" if any(w in q for w in
                                         ("most growth", "grew most", "biggest increase",
                                          "largest increase", "increased most")) else "drop"
            dimension = "surface" if "surface" in q else "country"
            # "Most growth" is a direction, not the growth topic: strip
            # direction phrases before re-detecting the topic, or
            # "which country had the most growth" would rank active
            # users when the conversation was about creators.
            q_nodir = q
            for phrase in ("most growth", "grew most", "biggest increase",
                           "largest increase", "increased most", "most drop",
                           "most decline", "dropped most", "biggest drop",
                           "largest drop", "most fell"):
                q_nodir = q_nodir.replace(phrase, " ")
            topic_nodir = next((t for t in self.topic_labels
                                if _word(q_nodir, t) or t in q_nodir), None)
            topic = topic_nodir or self.topic or "creators"
            days = 28
            m = re.search(r"last (\d+)\s*(week|weeks|day|days)", q)
            if m:
                days = int(m.group(1)) * 7 if "week" in m.group(2) else int(m.group(1))
            elif "last week" in q:
                days = 7
            elif "last month" in q:
                days = 28
            if days < 1 or days > 90:
                return self._clarify("I can rank changes over windows inside the Jan-Mar 2026 data "
                                     "(up to 90 days). Try \"last 2 weeks\" or \"last month\".")
            return self._ranking_turn(topic, dimension, days, direction)
        # Why-evidence: "what's driving the creator drop?", "why is
        # retention down?" — answered as a decomposition, never a cause.
        if ("driving" in q or "why is" in q or "why are" in q or "causing" in q
                or "what's behind" in q or "what is behind" in q):
            topic = topic_found or self.topic or "creators"
            return self._why_turn(topic)
        # Segment follow-up, once a weather report exists: "only
        # India", "creators in India", "for android". A surface filter
        # only makes sense for a content drill-down; anything else
        # clarifies instead of pretending the series was filtered.
        segment = _find_segment(q)
        if segment and self.weather:
            dim, value = segment
            if dim == "surface" and (topic_found or self.topic) not in ("creators", "engagement"):
                return self._clarify("Surface only filters the creators drill-down — "
                                     "ask \"creators by surface\", or switch to creators first.")
            if topic_found:
                self.filters[dim] = value
                reply = self._drilldown_turn(topic_found)
                reply["correction"] = {"parameter": dim, "value": value, "applied": True}
                reply["text"] = (f"Filtered: {dim}={value} for the {topic_found} drill-down. "
                                 "Note: weather cards are app-scoped only, so only the series "
                                 "is filtered, not the cards.\n\n" + reply["text"])
                return reply
            return self._filter_correction(dim, value)
        # Weather intent: an app slot plus any weather phrase. This is
        # the fix for "What is the current state of Instagram".
        if app_found and any(p in q for p in WEATHER_PHRASES):
            return self._weather_turn(app_found)
        if any(p in q for p in WEATHER_PHRASES):
            if self.app:
                return self._weather_turn(self.app)
            self.stage = "clarification"
            # Options here are apps, with a full question as send text so
            # clicking one opens that app's weather report.
            self._set_options([
                {"label": a, "send": f"What's happening with {a}?", "topic": a} for a in APPS
            ])
            return self._reply("clarification",
                               "Which app? Reply instagram, whatsapp, or facebook — "
                               "for example, \"What's happening with Instagram?\"")
        # Bare app name: "instagram" alone, after a conversation has
        # started or as an opening question, is a weather request.
        if q.rstrip("?!.").strip() in APPS:
            return self._weather_turn(q.rstrip("?!.").strip())
        # Evaluating a past experiment is the one question this bot must
        # refuse to answer without assignment data.
        if ("experiment" in q and ("did" in q or "lift" in q or "improve" in q or "effect" in q or "work" in q)
                and "recommend" not in q):
            return self._clarify("I cannot evaluate a past experiment yet: there are no experiment assignment tables "
                                 "in Phase 3, so a lift claim would be a causal claim without evidence. "
                                 "Ask for an experiment recommendation (a hypothesis to test) instead, "
                                 "or for the experiment registry in the single-shot ask UI.")
        if "brief" in q or "talk to" in q or ("pm" in q and "eng" in q):
            return self._brief_turn()
        if "recommend" in q or "experiment" in q:
            return self._recommendation_turn()
        if "diagnos" in q or "problematic" in q or "what's wrong" in q or "what is wrong" in q or "what is the problem" in q:
            return self._diagnosis_turn()
        # Drill-down by topic name, once a weather report exists.
        if self.weather:
            for topic in self.topic_labels:
                if _word(q, topic) or topic in q:
                    return self._drilldown_turn(topic)
            if "look at" in q or "show me" in q or "drill" in q:
                return self._clarify("Pick a drill-down from the last message (a/b/c/d), or name a topic: "
                                     "creators, retention, growth, messaging, or engagement.")
        return self._clarify("I could not map that to a chat turn. Try: \"What is the current state of Instagram?\", "
                             "\"What about WhatsApp?\", \"creators by country\", \"what's driving the creator drop?\", or "
                             "\"What's the MAU for Instagram?\" — "
                             "then pick a drill-down, ask for a diagnosis, an experiment recommendation, or the PM/Eng brief.")
