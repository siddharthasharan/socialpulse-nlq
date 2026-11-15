"""Multi-turn conversation benchmark for Phase 3 chat.

Unlike scripts/run_benchmark.py (single question -> single expected
answer), this runner checks a *sequence*: each turn's stage/type, app,
and topic, plus value checks on the weather/drill-down cards that the
turn produced. A conversation passes only if every turn passes.

Usage: python scripts/run_conversation_benchmark.py [--data data/phase1]
Exit code 0 only if every conversation passes.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from socialpulse_nlq.chat import ChatSession


def _cards(weather):
    return {c["id"]: c for c in weather["cards"]} if weather else {}


def check_turn(conv, turn, reply, failures):
    exp = turn
    if exp.get("expect_type") and reply["type"] != exp["expect_type"]:
        failures.append(f"type: expected {exp['expect_type']}, got {reply['type']}")
    if exp.get("expect_app") and reply.get("app") != exp["expect_app"]:
        failures.append(f"app: expected {exp['expect_app']}, got {reply.get('app')}")
    topic = None
    if reply.get("ranking"):
        topic = reply["ranking"]["topic"]
    elif reply.get("breakdown"):
        topic = reply["breakdown"]["topic"]
    elif reply.get("drilldown"):
        topic = reply["drilldown"]["topic"]
    elif reply.get("diagnosis"):
        topic = reply["diagnosis"]["primary_topic"]
    elif reply.get("recommendation"):
        topic = reply["recommendation"]["topic"]
    elif reply.get("brief"):
        topic = reply["brief"]["topic"]
    elif reply.get("watchouts"):
        topic = reply["watchouts"]["primary_topic"]
    if exp.get("expect_topic") and topic != exp["expect_topic"]:
        failures.append(f"topic: expected {exp['expect_topic']}, got {topic}")


def check_values(conv, replies, failures):
    checks = conv.get("checks", {})
    weather = next((r.get("weather") for r in replies if r.get("weather")), None)
    if weather and "weather_dau" in checks:
        got = _cards(weather)["dau"]["current"]
        if abs(got - checks["weather_dau"]) > checks.get("weather_dau_tolerance", 10.0):
            failures.append(f"weather DAU: expected ~{checks['weather_dau']}, got {got}")
    if weather and "weather_d7" in checks:
        got = _cards(weather)["d7_retention"]["current"]
        if abs(got - checks["weather_d7"]) > checks.get("weather_d7_tolerance", 0.01):
            failures.append(f"weather D7: expected ~{checks['weather_d7']}, got {got}")
    drill = next((r.get("drilldown") for r in replies if r.get("drilldown")), None)
    if drill and "drilldown_daily_creators" in checks:
        got = drill["evidence"]["daily_creators"]["current"]
        if abs(got - checks["drilldown_daily_creators"]) > 1.0:
            failures.append(f"drill-down daily creators: expected ~{checks['drilldown_daily_creators']}, got {got}")
    if drill and "drilldown_orphan" in checks:
        got = drill["evidence"]["orphan_post_rate"]["current"]
        if abs(got - checks["drilldown_orphan"]) > 0.01:
            failures.append(f"drill-down orphan: expected ~{checks['drilldown_orphan']}, got {got}")
    rec = next((r.get("recommendation") for r in replies if r.get("recommendation")), None)
    if rec and "recommendation_label" in checks and rec["label"] != checks["recommendation_label"]:
        failures.append(f"recommendation label: expected {checks['recommendation_label']}, got {rec['label']}")
    if rec and "recommendation_must_not_contain" in checks:
        rec_text = next(r["text"] for r in replies if r.get("recommendation"))
        if checks["recommendation_must_not_contain"].lower() in rec_text.lower():
            failures.append(f"recommendation must not contain '{checks['recommendation_must_not_contain']}'")
    diag = next((r.get("diagnosis") for r in replies if r.get("diagnosis")), None)
    if diag and "diagnosis_text_must_not_contain" in checks:
        diag_text = next(r["text"] for r in replies if r.get("diagnosis"))
        if checks["diagnosis_text_must_not_contain"].lower() in diag_text.lower():
            failures.append(f"diagnosis must not contain '{checks['diagnosis_text_must_not_contain']}'")
    brief = next((r.get("brief") for r in replies if r.get("brief")), None)
    if brief:
        if len(brief["pm_points"]) < checks.get("brief_pm_points_min", 0):
            failures.append("brief: too few PM points")
        if len(brief["eng_points"]) < checks.get("brief_eng_points_min", 0):
            failures.append("brief: too few Eng points")
    # Daily series (2026-11-06, p02): the reply's working payload
    # carries the visualization built from the executed series SQL,
    # so the chart kind and the day-by-day values are checked against
    # the same receipts the shell shows. The values come from the
    # case's top-level expected_values (also what the oracle's series
    # branch checks); the chart kind is pinned in checks so the
    # conversation's result_equiv item is scored, not "na". Exact
    # equality is right here: both sides are counts from the same
    # deterministic data, not re-derivations with rounding.
    series_reply = next((r for r in replies if r.get("daily_series")), None)
    if series_reply:
        viz = (series_reply.get("working") or {}).get("visualization") or {}
        if "daily_series_chart_kind" in checks:
            chart = viz.get("chart") or {}
            if chart.get("kind") != checks["daily_series_chart_kind"]:
                failures.append(f"daily series chart: expected "
                                f"{checks['daily_series_chart_kind']}, got {chart.get('kind')}")
        if conv.get("expected_values") is not None:
            rows = viz.get("rows") or []
            got = [float(r[-1]) for r in rows if len(r) > 1]
            want = [float(v) for v in conv["expected_values"]]
            if len(got) != len(want):
                failures.append(f"daily series rows: got {len(got)}, expected {len(want)}")
            elif any(g != w for g, w in zip(got, want)):
                failures.append("daily series values differ from expected")
    # Weekly series (2026-11-12, p14): the reply's working payload
    # carries the visualization built from the executed weekly SQL,
    # so the chart kind and the week-by-week values are checked
    # against the same receipts the shell shows. The LAST
    # weekly_series reply is the one checked: turn 1 is the Instagram
    # series (the question that opens the conversation) and turn 2 is
    # the WhatsApp series (the app-switch the case pins), so the first
    # match would check the wrong app. Exact equality is right here:
    # both sides are counts from the same deterministic data, not
    # re-derivations with rounding.
    weekly_replies = [r for r in replies if r.get("weekly_series")]
    if weekly_replies:
        viz = (weekly_replies[-1].get("working") or {}).get("visualization") or {}
        if "weekly_series_chart_kind" in checks:
            chart = viz.get("chart") or {}
            if chart.get("kind") != checks["weekly_series_chart_kind"]:
                failures.append(f"weekly series chart: expected "
                                f"{checks['weekly_series_chart_kind']}, got {chart.get('kind')}")
        if "weekly_series_values" in checks:
            rows = viz.get("rows") or []
            got = [float(r[-1]) for r in rows if len(r) > 1]
            want = [float(v) for v in checks["weekly_series_values"]]
            if len(got) != len(want):
                failures.append(f"weekly series rows: got {len(got)}, expected {len(want)}")
            elif any(g != w for g, w in zip(got, want)):
                failures.append("weekly series values differ from expected")
    # Last 4 weeks (2026-11-13, v11, p17/p18): mirror the weekly
    # series branch -- the LAST weekly_last4 reply is the one
    # checked (turn 2 in p17; turn 3 in p18 is the WhatsApp last-4).
    # Values exact; the excluded partial week is pinned by the
    # payload's excluded_partial_weeks list.
    last4_replies = [r for r in replies if r.get("weekly_last4")]
    if last4_replies:
        viz = (last4_replies[-1].get("working") or {}).get("visualization") or {}
        if "weekly_last4_chart_kind" in checks:
            chart = viz.get("chart") or {}
            if chart.get("kind") != checks["weekly_last4_chart_kind"]:
                failures.append(f"last-4-weeks chart: expected "
                                f"{checks['weekly_last4_chart_kind']}, got {chart.get('kind')}")
        if "weekly_last4_values" in checks:
            rows = viz.get("rows") or []
            got = [float(r[-1]) for r in rows if len(r) > 1]
            want = [float(v) for v in checks["weekly_last4_values"]]
            if len(got) != len(want):
                failures.append(f"last-4-weeks rows: got {len(got)}, expected {len(want)}")
            elif any(g != w for g, w in zip(got, want)):
                failures.append("last-4-weeks values differ from expected")
        if "weekly_last4_excluded" in checks:
            got_ex = (last4_replies[-1].get("weekly_last4") or {}).get("excluded_partial_weeks")
            if got_ex != checks["weekly_last4_excluded"]:
                failures.append(f"last-4-weeks excluded: expected {checks['weekly_last4_excluded']}, "
                                f"got {got_ex}")
    # Weekly compare (2026-11-13, v11, p20): the reply carries a
    # weekly_compare payload (both series + stats) and the working
    # visualization carries the combined 14x3 rows. The values are
    # checked per app against the stored series (exact counts); the
    # stats (peak weeks exact, percents/means/ratio within 0.05)
    # against the stored figures; the chart must be a line chart
    # with two series. A missing weekly_compare reply is itself a
    # failure.
    if "weekly_compare_values" in checks or "weekly_compare_stats" in checks \
            or "weekly_compare_chart_kind" in checks:
        wc = next((r.get("weekly_compare") for r in replies if r.get("weekly_compare")), None)
        if wc is None:
            failures.append("weekly compare: no weekly_compare reply")
        else:
            if "weekly_compare_values" in checks:
                want = checks["weekly_compare_values"]
                for app_key, vals_key in (("app_a", "values_a"), ("app_b", "values_b")):
                    app_name = wc.get(app_key)
                    got = wc.get(vals_key) or []
                    w = want.get(app_name)
                    if w is None:
                        failures.append(f"weekly compare: no expected series for {app_name}")
                    elif len(got) != len(w) or any(g != e for g, e in zip(got, w)):
                        failures.append(f"weekly compare {app_name} values differ from expected")
            if "weekly_compare_stats" in checks:
                want = checks["weekly_compare_stats"]
                for key in ("peak_a", "peak_b", "peak_a_week", "peak_b_week"):
                    if wc.get(key) != want[key]:
                        failures.append(f"weekly compare {key}: expected {want[key]}, got {wc.get(key)}")
                for key in ("growth_a_pct", "growth_b_pct", "change_a_pct",
                            "change_b_pct", "mean4_a", "mean4_b", "ratio"):
                    if abs(float(wc.get(key) or 0) - want[key]) > 0.05:
                        failures.append(f"weekly compare {key}: expected ~{want[key]}, got {wc.get(key)}")
            if "weekly_compare_chart_kind" in checks:
                wc_reply = next((r for r in replies if r.get("weekly_compare")), None)
                viz = (wc_reply.get("working") or {}).get("visualization") or {}
                chart = viz.get("chart") or {}
                if chart.get("kind") != checks["weekly_compare_chart_kind"]:
                    failures.append(f"weekly compare chart: expected "
                                    f"{checks['weekly_compare_chart_kind']}, got {chart.get('kind')}")
                if len(chart.get("series") or []) != 2:
                    failures.append(f"weekly compare chart: expected 2 series, "
                                    f"got {len(chart.get('series') or [])}")
    # Creators series (2026-11-13, v11, p21/p22/p23): mirror the
    # weekly series branch -- the LAST creators_series reply is the
    # one checked (turn 2 in p21/p23; turn 2 in p22 is the WhatsApp
    # series). Values exact; chart kind line.
    creators_replies = [r for r in replies if r.get("creators_series")]
    if creators_replies:
        viz = (creators_replies[-1].get("working") or {}).get("visualization") or {}
        if "creators_series_chart_kind" in checks:
            chart = viz.get("chart") or {}
            if chart.get("kind") != checks["creators_series_chart_kind"]:
                failures.append(f"creators series chart: expected "
                                f"{checks['creators_series_chart_kind']}, got {chart.get('kind')}")
        if "creators_series_values" in checks:
            rows = viz.get("rows") or []
            got = [float(r[-1]) for r in rows if len(r) > 1]
            want = [float(v) for v in checks["creators_series_values"]]
            if len(got) != len(want):
                failures.append(f"creators series rows: got {len(got)}, expected {len(want)}")
            elif any(g != w for g, w in zip(got, want)):
                failures.append("creators series values differ from expected")
    # Daily creators scalar (2026-11-13, v11, p23 turn 1): the reply
    # carries a daily_creators payload (app, value). Mean within
    # 0.01 of the stored value. A missing payload is itself a
    # failure.
    if "daily_creators_value" in checks:
        dc = next((r.get("daily_creators") for r in replies if r.get("daily_creators")), None)
        if dc is None:
            failures.append("daily creators: no daily_creators reply")
        elif abs(float(dc.get("value") or 0) - checks["daily_creators_value"]) > 0.01:
            failures.append(f"daily creators value: expected ~{checks['daily_creators_value']}, "
                            f"got {dc.get('value')}")
    # Watch-outs after a weekly series (2026-11-12, p15/p16): the
    # reply carries a watchouts payload (app, primary_topic, weekly
    # creators + WAU values, break numbers, plateau values). Counts
    # exact; means within 1.0; the plateau list exact. A missing
    # watchouts reply is itself a failure: without it the values
    # check would silently pass on nothing. The text checks pin the
    # two things the reply must say (creator supply, plateau) and
    # the one thing it must not (forecast language -- "will ").
    if "watchouts_values" in checks:
        wo = next((r.get("watchouts") for r in replies if r.get("watchouts")), None)
        if wo is None:
            failures.append("watch-outs: no watchouts reply")
        else:
            want = checks["watchouts_values"]
            if wo.get("primary_topic") != want["primary_topic"]:
                failures.append(f"watch-outs primary topic: expected {want['primary_topic']}, "
                                f"got {wo.get('primary_topic')}")
            for key in ("creators_before", "creators_before_week",
                        "creators_after", "creators_after_week",
                        "wau_peak", "wau_peak_week"):
                if wo.get(key) != want[key]:
                    failures.append(f"watch-outs {key}: expected {want[key]}, got {wo.get(key)}")
            for key in ("daily_creators_before_mean", "daily_creators_after_mean"):
                if abs(float(wo.get(key) or 0) - want[key]) > 1.0:
                    failures.append(f"watch-outs {key}: expected ~{want[key]}, got {wo.get(key)}")
            if wo.get("wau_plateau_last3") != want["wau_plateau_last3"]:
                failures.append(f"watch-outs plateau: expected {want['wau_plateau_last3']}, "
                                f"got {wo.get('wau_plateau_last3')}")
    if "watchouts_text_must_contain" in checks:
        wo_text = next((r["text"] for r in replies if r.get("watchouts")), "")
        for phrase in checks["watchouts_text_must_contain"]:
            if phrase.lower() not in wo_text.lower():
                failures.append(f"watch-outs text missing {phrase!r}")
    if "watchouts_text_must_not_contain" in checks:
        wo_text = next((r["text"] for r in replies if r.get("watchouts")), "")
        if checks["watchouts_text_must_not_contain"].lower() in wo_text.lower():
            failures.append(f"watch-outs text must not contain "
                            f"{checks['watchouts_text_must_not_contain']!r}")
    # MAU by country (2026-11-10, p07): the "by country" follow-up
    # after a MAU turn cuts the MAU number just shown (distinct users
    # per country), not the creators breakdown. Rows carry the country
    # in "label" and the MAU count in "count". A missing
    # mau_by_country reply is itself a failure: without it the values
    # check would silently pass on nothing.
    if "mau_by_country_values" in checks:
        mbc = next((r.get("mau_by_country") for r in replies if r.get("mau_by_country")), None)
        if mbc is None:
            failures.append("mau by country: no mau_by_country reply")
        else:
            got = {row["label"]: row["count"] for row in mbc.get("rows") or []}
            want = checks["mau_by_country_values"]
            if set(got) != set(want):
                failures.append(f"mau by country segments: got {sorted(got)}, "
                                f"expected {sorted(want)}")
            else:
                for label, val in want.items():
                    if got[label] != val:
                        failures.append(f"mau by country {label}: expected {val}, "
                                        f"got {got[label]}")
    # Creator share of MAU (2026-11-10, p10): the "% of creators of
    # MAU" follow-up after a MAU turn answers the ratio plus both
    # numbers, not the MAU scalar. The reply carries a creator_share
    # payload (app, creators, mau, pct as a percent). Counts exact;
    # the percent within 0.01 of the stored value. A missing
    # creator_share reply is itself a failure: without it the values
    # check would silently pass on nothing.
    if "creator_share_values" in checks:
        cs = next((r.get("creator_share") for r in replies if r.get("creator_share")), None)
        if cs is None:
            failures.append("creator share: no creator_share reply")
        else:
            want = checks["creator_share_values"]
            if cs.get("creators") != want["creators"]:
                failures.append(f"creator share creators: expected {want['creators']}, "
                                f"got {cs.get('creators')}")
            if cs.get("mau") != want["mau"]:
                failures.append(f"creator share mau: expected {want['mau']}, "
                                f"got {cs.get('mau')}")
            if abs(float(cs.get("pct", 0)) - want["pct"]) > 0.01:
                failures.append(f"creator share pct: expected {want['pct']}, "
                                f"got {cs.get('pct')}")
    # MAU and creators by country (2026-11-11, p12): the "Monthly
    # active users and creators by country" follow-up after a
    # MAU-by-country turn answers both populations per country, not
    # the MAU column alone. The reply carries a
    # mau_creators_by_country payload (app, rows with label/mau/
    # creators/share as a fraction, totals). Counts exact; the share
    # within 0.001 (0.1pp) of the stored 4-decimal fraction. A
    # missing reply is itself a failure: without it the values check
    # would silently pass on nothing.
    if "mau_creators_by_country_values" in checks:
        mc = next((r.get("mau_creators_by_country") for r in replies
                   if r.get("mau_creators_by_country")), None)
        if mc is None:
            failures.append("mau and creators by country: no mau_creators_by_country reply")
        else:
            got = {row["label"]: row for row in mc.get("rows") or []}
            want = checks["mau_creators_by_country_values"]
            if set(got) != set(want):
                failures.append(f"mau and creators by country segments: got {sorted(got)}, "
                                f"expected {sorted(want)}")
            else:
                for label, vals in want.items():
                    if got[label]["mau"] != vals["mau"]:
                        failures.append(f"mau and creators by country {label} mau: expected "
                                        f"{vals['mau']}, got {got[label]['mau']}")
                    if got[label]["creators"] != vals["creators"]:
                        failures.append(f"mau and creators by country {label} creators: expected "
                                        f"{vals['creators']}, got {got[label]['creators']}")
                    if abs(float(got[label]["share"]) - vals["share"]) > 0.001:
                        failures.append(f"mau and creators by country {label} share: expected "
                                        f"{vals['share']}, got {got[label]['share']}")
    # Chained breakdown (2026-11-09, p05): the second breakdown in
    # the same session (creators by country after creators by
    # surface, both off the sticky MAU context) is checked against
    # the case's stored breakdown_country_values. Country rows carry
    # creators in "count" and posts in the "extra" field ("9,286
    # posts") -- the mirror image of the surface rows, so the parse
    # is swapped. Exact equality, as above. A missing
    # country-dimension reply is itself a failure: without it the
    # values check would silently pass on nothing.
    if "breakdown_country_values" in checks:
        br_country = next((r.get("breakdown") for r in replies
                           if r.get("breakdown")
                           and r["breakdown"].get("dimension") == "country"), None)
        if br_country is None:
            failures.append("breakdown country: no country-dimension breakdown reply")
        else:
            got = {}
            for row in br_country.get("rows") or []:
                posts = None
                extra = row.get("extra") or ""
                if "posts" in extra:
                    posts = int(extra.split()[0].replace(",", ""))
                got[row["label"]] = {"creators": row["count"], "posts": posts}
            want = checks["breakdown_country_values"]
            if set(got) != set(want):
                failures.append(f"breakdown country segments: got {sorted(got)}, "
                                f"expected {sorted(want)}")
            else:
                for label, vals in want.items():
                    if got[label]["creators"] != vals["creators"]:
                        failures.append(f"breakdown country {label} creators: expected "
                                        f"{vals['creators']}, got {got[label]['creators']}")
                    if got[label]["posts"] != vals["posts"]:
                        failures.append(f"breakdown country {label} posts: expected "
                                        f"{vals['posts']}, got {got[label]['posts']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data/phase1")
    args = ap.parse_args()
    convs = json.loads((ROOT / "evals/conversation_phase3.json").read_text())
    all_ok = True
    for conv in convs:
        session = ChatSession(args.data)
        replies = []
        failures = []
        for turn in conv["turns"]:
            reply = session.respond(turn["user"])
            replies.append(reply)
            check_turn(conv, turn, reply, failures)
        check_values(conv, replies, failures)
        status = "PASS" if not failures else "FAIL"
        if failures:
            all_ok = False
        print(f"{status} {conv['id']}: {conv['description']}")
        for f in failures:
            print(f"  - {f}")
    print(f"\n{len(convs)} conversations, {'all pass' if all_ok else 'FAILURES'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
