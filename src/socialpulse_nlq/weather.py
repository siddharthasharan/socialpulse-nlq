"""Product-health "weather report" for SocialPulse NLQ.

Why this exists: the planner answers one catalog metric at a time, so
"How is the product doing?" used to clarify instead of answering. This
module answers that question directly, as a small set of period-over-
period comparisons plus level-only creator metrics, all computed from
the same Phase 1 parquet tables. No single score: a score would hide
which part is moving, and inventing one would be the exact dishonesty
the planner's guard exists to prevent.

Periods (derived from the data, not hardcoded to March 2026):
- current: the last 28 days of sessions, prior: the 28 days before that.
- retention: D7 needs a full 7-day follow-up, so cohorts run to
  (max session date - 7 days). Recent cohorts are the last 28 complete
  cohorts, prior cohorts are the 28 before that.
- WAU: mean of the last 4 weekly WAUs vs the 4 before them.

Direction thresholds (chosen, documented, synthetic — not business
truth): counts move on a 2% relative change; rates move on a 1
percentage-point change. Anything smaller is "flat".

Known biases, stated in the output notes, not hidden:
- Orphan post rate is whole-window only, never a trend: posts published
  late in the window had less time to attract engagement, so a
  current-period orphan rate would overstate the problem.
- MAU is deliberately omitted: a 31-day MAU compounds daily activity
  probability, so March MAU looks inflated next to a 28-day window.
  DAU and WAU already cover growth.
"""
from pathlib import Path

CURRENT_DAYS = 28


def classify_change(current, prior, kind):
    """Return (direction, change_pct, change_pp).

    kind "count" uses a 2% relative threshold; kind "rate" uses a
    1 percentage-point threshold. Both numbers are returned so the
    caller can display whichever fits the metric.
    """
    if prior in (None, 0) or current is None:
        return "flat", None, None
    change_pp = (current - prior) * 100 if kind == "rate" else None
    change_pct = (current - prior) / prior * 100 if prior else None
    if kind == "rate":
        if change_pp >= 1.0:
            return "up", change_pct, change_pp
        if change_pp <= -1.0:
            return "down", change_pct, change_pp
        return "flat", change_pct, change_pp
    if change_pct >= 2.0:
        return "up", change_pct, change_pp
    if change_pct <= -2.0:
        return "down", change_pct, change_pp
    return "flat", change_pct, change_pp


def _card(card_id, label, current, prior, kind, unit, lower_is_better=False):
    direction, change_pct, change_pp = classify_change(current, prior, kind)
    if direction == "flat":
        good_bad = "neutral"
    elif lower_is_better:
        good_bad = "bad" if direction == "up" else "good"
    else:
        good_bad = "good" if direction == "up" else "bad"
    return {
        "id": card_id, "label": label, "current": current, "prior": prior,
        "kind": kind, "unit": unit, "direction": direction,
        "change_pct": change_pct, "change_pp": change_pp,
        "good_bad": good_bad, "lower_is_better": lower_is_better,
    }


def compute_weather(data_dir):
    """Compute the weather report from Phase 1 parquet. Returns a dict."""
    import duckdb

    con = duckdb.connect()
    for t in ["fact_sessions", "fact_content_impressions", "fact_content_engagements",
              "fact_video_plays", "fact_messages", "fact_content_creation",
              "dim_users", "dim_content"]:
        p = Path(data_dir) / f"{t}.parquet"
        if p.exists():
            con.execute(f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM read_parquet('{p}')")
    end = con.execute("SELECT MAX(CAST(start_time AS DATE)) FROM fact_sessions").fetchone()[0]
    cur_start, cur_end = end - __import__("datetime").timedelta(days=27), end
    pri_start, pri_end = end - __import__("datetime").timedelta(days=55), end - __import__("datetime").timedelta(days=28)
    cutoff = end - __import__("datetime").timedelta(days=7)
    ret_cur_start, ret_cur_end = cutoff - __import__("datetime").timedelta(days=27), cutoff
    ret_pri_start, ret_pri_end = cutoff - __import__("datetime").timedelta(days=55), cutoff - __import__("datetime").timedelta(days=28)

    def one(sql, *args):
        return con.execute(sql, list(args)).fetchone()[0]

    def dau_mean(a, b):
        return one(
            "SELECT AVG(dau) FROM (SELECT CAST(start_time AS DATE) AS d, COUNT(DISTINCT user_id) AS dau "
            "FROM fact_sessions WHERE CAST(start_time AS DATE) BETWEEN ? AND ? GROUP BY d)", a, b)

    def wau_mean(n_weeks, offset_weeks):
        # Weekly WAU series, most recent first, COMPLETE weeks only.
        # The first and last calendar weeks in the window are partial
        # (4 days in late Dec, 2 days on Mar 30-31), and including them
        # understates WAU by ~10% — that was a real bug in the first
        # version of this report, caught by checking DAU vs WAU direction.
        rows = con.execute(
            "SELECT DATE_TRUNC('week', CAST(start_time AS DATE)) AS w, COUNT(DISTINCT user_id) AS wau, "
            "COUNT(DISTINCT CAST(start_time AS DATE)) AS days "
            "FROM fact_sessions GROUP BY w HAVING days = 7 ORDER BY w DESC").fetchall()
        block = [r[1] for r in rows[offset_weeks:offset_weeks + n_weeks]]
        return sum(block) / len(block) if block else None

    def sessions_per_dau(a, b):
        return one(
            "SELECT COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT (user_id, CAST(start_time AS DATE))), 0) "
            "FROM fact_sessions WHERE CAST(start_time AS DATE) BETWEEN ? AND ?", a, b)

    def daily_creators(a, b):
        return one(
            "SELECT AVG(c) FROM (SELECT CAST(published_at AS DATE) AS d, COUNT(DISTINCT creator_id) AS c "
            "FROM fact_content_creation WHERE CAST(published_at AS DATE) BETWEEN ? AND ? GROUP BY d)", a, b)

    def creator_penetration(a, b):
        return one(
            "SELECT AVG(c * 1.0 / NULLIF(dau, 0)) FROM "
            "(SELECT CAST(published_at AS DATE) AS d, COUNT(DISTINCT creator_id) AS c FROM fact_content_creation "
            "WHERE CAST(published_at AS DATE) BETWEEN ? AND ? GROUP BY d) c JOIN "
            "(SELECT CAST(start_time AS DATE) AS d, COUNT(DISTINCT user_id) AS dau FROM fact_sessions "
            "WHERE CAST(start_time AS DATE) BETWEEN ? AND ? GROUP BY d) s USING (d)", a, b, a, b)

    def messaging_dau(a, b):
        return one(
            "SELECT AVG(s) FROM (SELECT CAST(sent_at AS DATE) AS d, COUNT(DISTINCT sender_id) AS s "
            "FROM fact_messages WHERE CAST(sent_at AS DATE) BETWEEN ? AND ? GROUP BY d)", a, b)

    def engagement_rate(a, b):
        imps = one("SELECT COUNT(*) FROM fact_content_impressions WHERE CAST(viewed_at AS DATE) BETWEEN ? AND ?", a, b)
        engs = one("SELECT COUNT(*) FROM fact_content_engagements WHERE CAST(created_at AS DATE) BETWEEN ? AND ?", a, b)
        return (engs / imps) if imps else None

    def reels_completion(a, b):
        return one(
            "SELECT AVG(CASE WHEN completion_pct >= 0.95 THEN 1.0 ELSE 0.0 END) FROM fact_video_plays "
            "WHERE CAST(play_time AS DATE) BETWEEN ? AND ?", a, b)

    def reels_watch(a, b):
        return one(
            "SELECT SUM(watch_ms) / 60000.0 / NULLIF(COUNT(DISTINCT (user_id, CAST(play_time AS DATE))), 0) "
            "FROM fact_video_plays WHERE CAST(play_time AS DATE) BETWEEN ? AND ?", a, b)

    def d7(a, b):
        return one(
            "SELECT AVG(CASE WHEN retained THEN 1.0 ELSE 0.0 END) FROM ("
            "SELECT u.user_id, EXISTS (SELECT 1 FROM fact_sessions s WHERE s.user_id = u.user_id "
            "AND CAST(s.start_time AS DATE) = u.signup_date + INTERVAL 7 DAY) AS retained "
            "FROM dim_users u WHERE u.signup_date BETWEEN ? AND ?)", a, b)

    orphan = one(
        "SELECT AVG(CASE WHEN engaged THEN 0.0 ELSE 1.0 END) FROM ("
        "SELECT c.content_id, EXISTS (SELECT 1 FROM fact_content_engagements e WHERE e.content_id = c.content_id) AS engaged "
        "FROM dim_content c)")
    top1 = one(
        "WITH creator_imps AS (SELECT c.creator_id, COUNT(*) AS imps FROM fact_content_impressions i "
        "JOIN dim_content c ON c.content_id = i.content_id GROUP BY c.creator_id), "
        "ranked AS (SELECT creator_id, imps, NTILE(100) OVER (ORDER BY imps DESC) AS tile FROM creator_imps) "
        "SELECT SUM(CASE WHEN tile = 1 THEN imps ELSE 0 END) * 1.0 / NULLIF(SUM(imps), 0) FROM ranked")

    cards = [
        _card("dau", "Mean daily DAU", dau_mean(cur_start, cur_end), dau_mean(pri_start, pri_end), "count", "users/day"),
        _card("wau", "Mean weekly WAU", wau_mean(4, 0), wau_mean(4, 4), "count", "users/week"),
        _card("sessions_per_dau", "Sessions per DAU", sessions_per_dau(cur_start, cur_end), sessions_per_dau(pri_start, pri_end), "count", "sessions"),
        _card("d7_retention", "D7 retention (complete cohorts)", d7(ret_cur_start, ret_cur_end), d7(ret_pri_start, ret_pri_end), "rate", "share"),
        _card("daily_creators", "Daily creators", daily_creators(cur_start, cur_end), daily_creators(pri_start, pri_end), "count", "creators/day"),
        _card("creator_penetration", "Creator penetration", creator_penetration(cur_start, cur_end), creator_penetration(pri_start, pri_end), "rate", "share"),
        _card("messaging_dau", "Messaging DAU", messaging_dau(cur_start, cur_end), messaging_dau(pri_start, pri_end), "count", "senders/day"),
        _card("engagement_rate", "Engagement rate per impression", engagement_rate(cur_start, cur_end), engagement_rate(pri_start, pri_end), "rate", "share"),
        _card("reels_completion", "Reels completion rate", reels_completion(cur_start, cur_end), reels_completion(pri_start, pri_end), "rate", "share"),
        _card("reels_watch_time", "Reels watch time per viewer day", reels_watch(cur_start, cur_end), reels_watch(pri_start, pri_end), "count", "minutes"),
        _card("orphan_post_rate", "Orphan post rate (whole window, level only)", orphan, None, "rate", "share", lower_is_better=True),
        _card("top1_share", "Top-1% creator impression share (whole window, level only)", top1, None, "rate", "share", lower_is_better=True),
    ]
    by_id = {c["id"]: c for c in cards}

    def fmt(v, unit):
        if v is None:
            return "n/a"
        if unit == "share":
            return f"{v * 100:.1f}%"
        if unit == "minutes":
            return f"{v:.1f} min"
        return f"{v:,.0f}"

    def arrow(c):
        return {"up": "↑", "down": "↓", "flat": "→"}[c["direction"]]

    dau, ret, creators, pen = by_id["dau"], by_id["d7_retention"], by_id["daily_creators"], by_id["creator_penetration"]
    headline = (
        f"Growth is {dau['direction']}: mean daily DAU {fmt(dau['current'], 'users/day')} in {cur_start} to {cur_end}, "
        f"{dau['change_pct']:+.1f}% vs the prior 28 days. "
        f"Retention is {ret['direction']}: D7 {fmt(ret['current'], 'share')} for cohorts {ret_cur_start} to {ret_cur_end}, "
        f"{ret['change_pp']:+.1f} points vs prior cohorts. "
        f"Creator supply is {creators['direction']}: {fmt(creators['current'], 'creators/day')} daily creators, "
        f"penetration {fmt(pen['current'], 'share')}."
    )
    watchouts = []
    if dau["direction"] == "down":
        watchouts.append(f"Growth falling: DAU down {abs(dau['change_pct']):.1f}% vs prior 28 days.")
    if ret["direction"] == "down":
        watchouts.append(f"Retention falling: D7 down {abs(ret['change_pp']):.1f} points vs prior cohorts.")
    if by_id["orphan_post_rate"]["current"] and by_id["orphan_post_rate"]["current"] > 0.20:
        watchouts.append(f"Creator problem: {fmt(by_id['orphan_post_rate']['current'], 'share')} of posts get zero engagement (whole window). "
                         "That is the single biggest creator-health signal in this report.")
    if creators["direction"] == "down" or pen["direction"] == "down":
        watchouts.append("Creator supply softening: daily creators and/or creator penetration are down vs the prior period.")
    if by_id["engagement_rate"]["direction"] == "down":
        watchouts.append("Engagement softening: engagement rate per impression is down vs the prior period.")
    if by_id["top1_share"]["current"] and by_id["top1_share"]["current"] > 0.08:
        watchouts.append(f"Concentration watch: the top 1% of creators capture {fmt(by_id['top1_share']['current'], 'share')} of impressions. "
                         "Not a problem by itself, but it rhymes with a high orphan rate: attention is concentrated.")
    if not watchouts:
        watchouts = ["No watchouts fired on these thresholds. That is a statement about this synthetic data, not a guarantee."]
    return {
        "current_period": [str(cur_start), str(cur_end)],
        "prior_period": [str(pri_start), str(pri_end)],
        "retention_cohorts_current": [str(ret_cur_start), str(ret_cur_end)],
        "retention_cohorts_prior": [str(ret_pri_start), str(ret_pri_end)],
        "headline": headline, "cards": cards, "watchouts": watchouts,
        "notes": [
            "Counts move on a 2% relative change; rates move on 1 percentage point. Smaller moves are shown as flat.",
            "Orphan rate and top-1% share are whole-window levels, not trends: late-window posts had less time to attract engagement.",
            "D7 uses only complete cohorts (signup + 7 days within the data window).",
            "MAU is omitted on purpose: a 31-day MAU compounds daily activity and is not comparable to a 28-day window.",
            "Thresholds (orphan > 20%, top-1% > 8%) are chosen for this synthetic project, not business truth.",
        ],
    }
