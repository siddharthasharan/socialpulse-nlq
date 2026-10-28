"""Phase 3 multi-turn chatbot for SocialPulse NLQ. Deterministic, no LLM.

This is deliberately NOT a free-form chatbot. It is a state machine over
the data the project already computes:

    weather (scoped) -> drill-down (a/b/c) -> diagnosis
    -> experiment hypothesis -> PM/Eng brief

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


class ChatSession:
    """One multi-turn conversation. Create one per UI session id."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.app = None
        self.stage = "start"
        self.weather = None
        self.topic = None
        self.options = []
        self.history = []

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
        rows = con.execute(
            "SELECT CAST(DATE_TRUNC('month', CAST(f.published_at AS DATE)) AS VARCHAR) AS month, "
            "COUNT(*) AS posts, COUNT(DISTINCT f.creator_id) AS creators "
            "FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
            f"WHERE 1=1{app_filter} GROUP BY month ORDER BY month").fetchall()
        # DATE_TRUNC returns a timestamp; monthly labels are YYYY-MM only.
        return [{"month": str(r[0])[:7], "posts": r[1], "creators": r[2]} for r in rows]

    def _daily_dau_series(self):
        """Daily DAU over the weather current period, scoped to the app."""
        if not self.weather:
            return []
        start, end = self.weather["current_period"]
        con = self._connect()
        app_filter = f" AND u.primary_app = '{self.app}'" if self.app else ""
        rows = con.execute(
            "SELECT CAST(s.start_time AS DATE) AS d, COUNT(DISTINCT s.user_id) AS dau "
            "FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id "
            f"WHERE CAST(s.start_time AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'{app_filter} "
            "GROUP BY d ORDER BY d").fetchall()
        return [{"date": str(r[0]), "dau": r[1]} for r in rows]

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
                out.append({"key": chr(ord("a") + i), "label": TOPIC_LABELS[item],
                            "topic": item, "send": item})
            else:
                out.append({"key": chr(ord("a") + i), "label": item["label"],
                            "topic": item.get("topic"), "send": item.get("send", item.get("topic"))})
        self.options = out
        return self.options

    def _weather_turn(self, app):
        self.app = app
        self.weather = compute_weather(self.data_dir, app=app)
        self.stage = "weather"
        self.topic = None
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
        problems = []
        if cards["orphan_post_rate"]["current"] > 0.20 or cards["daily_creators"]["direction"] == "down" or cards["creator_penetration"]["direction"] == "down":
            problems.append("creators")
        if cards["d7_retention"]["direction"] == "down":
            problems.append("retention")
        if cards["dau"]["direction"] == "down":
            problems.append("growth")
        if cards["engagement_rate"]["direction"] == "down":
            problems.append("engagement")
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
        rec = RECOMMENDATIONS[topic]
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
        if cards["orphan_post_rate"]["current"] > 0.20 or cards["daily_creators"]["direction"] == "down":
            return "creators"
        if cards["d7_retention"]["direction"] == "down":
            return "retention"
        if cards["dau"]["direction"] == "down":
            return "growth"
        return "engagement"

    def _brief_turn(self):
        if not self.weather:
            return self._clarify("Ask for a weather report first, so the brief has numbers to quote.")
        topic = self.topic or self._diagnosis_topic()
        rec = RECOMMENDATIONS[topic]
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

    def _clarify(self, text):
        self.stage = "clarification"
        return self._reply("clarification", text)

    def _reply(self, type_, text, **extra):
        out = {"type": type_, "stage": self.stage, "app": self.app,
               "text": text, "options": self.options}
        out.update(extra)
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
        if q in ("a", "b", "c", "d", "1", "2", "3", "4") and self.options and self.weather:
            idx = ord(q) - ord("a") if q.isalpha() else int(q) - 1
            if 0 <= idx < len(self.options):
                send = self.options[idx].get("send") or self.options[idx].get("topic")
                if send and send.lower() != q:
                    return self.respond(send)
        # App switch / opening question, anywhere in the conversation.
        for app in APPS:
            if _word(q, app) and ("how is" in q or "happening" in q or "doing" in q or "weather" in q or "health" in q):
                return self._weather_turn(app)
        if "how is" in q and "product" in q and "doing" in q or "weather report" in q or "what's happening" in q or "what is happening" in q:
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
            for topic in TOPIC_LABELS:
                if _word(q, topic) or topic in q:
                    return self._drilldown_turn(topic)
            if "look at" in q or "show me" in q or "drill" in q:
                return self._clarify("Pick a drill-down from the last message (a/b/c/d), or name a topic: "
                                     "creators, retention, growth, messaging, or engagement.")
        return self._clarify("I could not map that to a chat turn. Try: \"What's happening with Instagram?\", "
                             "then pick a drill-down, ask for a diagnosis, an experiment recommendation, or the PM/Eng brief.")
