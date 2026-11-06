"""Phase 3 chat tests. Data-dependent tests run only when Phase 1
parquet is present (as in CI, where the generator runs first); the
state-machine tests use a stubbed weather report so they run anywhere.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from socialpulse_nlq.chat import ChatSession


def _stub_session():
    """A session whose weather call is replaced by the seed-42 Instagram
    shape, so routing/diagnosis/recommendation can be tested without data."""
    s = ChatSession(Path("data/phase1"))

    def fake_weather(data_dir, app=None):
        def card(cid, cur, prior, kind, unit, direction, good_bad, change_pct=None, change_pp=None):
            return {"id": cid, "label": cid, "current": cur, "prior": prior, "kind": kind,
                    "unit": unit, "direction": direction, "change_pct": change_pct,
                    "change_pp": change_pp, "good_bad": good_bad, "lower_is_better": False}
        return {
            "app": app, "current_period": ["2026-03-04", "2026-03-31"],
            "prior_period": ["2026-02-04", "2026-03-03"],
            "headline": "Instagram growth is up.",
            "cards": [
                card("dau", 4543.71, 4335.0, "count", "users/day", "up", "good", 4.8),
                card("wau", 14742.5, 13600.0, "count", "users/week", "up", "good", 8.4),
                card("sessions_per_dau", 1.69, 1.69, "count", "sessions", "flat", "neutral", 0.0),
                card("d7_retention", 0.2323, 0.206, "rate", "share", "up", "good", None, 2.6),
                card("daily_creators", 455.79, 674.0, "count", "creators/day", "down", "bad", -32.5),
                card("creator_penetration", 0.1006, 0.155, "rate", "share", "down", "bad", None, -5.4),
                card("messaging_dau", 1976.6, 1900.0, "count", "senders/day", "up", "good", 4.0),
                card("engagement_rate", 0.159, 0.160, "rate", "share", "flat", "neutral", None, -0.1),
                card("reels_completion", 0.351, 0.350, "rate", "share", "flat", "neutral", None, 0.1),
                card("reels_watch_time", 0.73, 0.73, "count", "minutes", "flat", "neutral", 0.0),
                card("orphan_post_rate", 0.2444, None, "rate", "share", "flat", "neutral"),
                card("top1_share", 0.095, None, "rate", "share", "flat", "neutral"),
            ],
            "watchouts": ["Creator problem: 24.4% of posts get zero engagement."],
            "notes": [],
        }

    import socialpulse_nlq.chat as chat_mod
    chat_mod.compute_weather = fake_weather
    # Drill-downs that hit DuckDB are stubbed out for the no-data tests.
    s._monthly_creations = lambda: [{"month": "2026-01", "posts": 37268, "creators": 3435},
                                    {"month": "2026-02", "posts": 24171, "creators": 3425},
                                    {"month": "2026-03", "posts": 16372, "creators": 3025}]
    s._daily_dau_series = lambda: [{"date": "2026-03-04", "dau": 4500}]
    return s


def test_opening_weather_sets_app_and_options():
    s = _stub_session()
    r = s.respond("What's happening with Instagram?")
    assert r["type"] == "weather" and r["app"] == "instagram"
    assert r["options"] and r["options"][0]["topic"] == "creators"


def test_diagnosis_before_weather_clarifies():
    s = _stub_session()
    r = s.respond("what's the diagnosis?")
    assert r["type"] == "clarification"


def test_recommendation_is_hypothesis_not_forecast():
    s = _stub_session()
    s.respond("What's happening with Instagram?")
    s.respond("creators")
    r = s.respond("what experiment would you recommend?")
    assert r["type"] == "recommendation"
    assert r["recommendation"]["label"] == "HYPOTHESIS — not a forecast"
    assert r["recommendation"]["topic"] == "creators"
    assert "will increase" not in r["text"].lower()
    assert r["recommendation"]["primary_metric"]


def test_brief_has_pm_and_eng_points():
    s = _stub_session()
    s.respond("What's happening with Instagram?")
    r = s.respond("give me the PM/Eng brief")
    assert r["type"] == "brief"
    assert len(r["brief"]["pm_points"]) >= 3
    assert len(r["brief"]["eng_points"]) >= 3
    assert r["brief"]["caveats"]


def test_drilldown_options_advance_instead_of_looping():
    """Bug 2026-10-28: drill-down options used to re-offer the same
    drill-downs, so clicking a) Creators opened the same panel again.
    After a drill-down, the first button must now advance the flow."""
    s = _stub_session()
    s.respond("What's happening with Instagram?")
    d = s.respond("creators")
    assert d["type"] == "drilldown"
    assert d["options"], "drill-down must offer next-step buttons"
    # The buttons themselves must not re-offer a creators drill-down.
    labels = " ".join(o["label"] for o in d["options"])
    assert "Diagnosis" in labels and "Experiment recommendation" in labels and "PM/Eng brief" in labels
    # Clicking the first button (via its send text) must diagnose, not
    # re-open the creators drill-down.
    diag = s.respond(d["options"][0]["send"])
    assert diag["type"] == "diagnosis"
    # And the letter shortcut must do the same, not loop.
    s2 = _stub_session()
    s2.respond("What's happening with Instagram?")
    s2.respond("creators")
    diag2 = s2.respond("a")
    assert diag2["type"] == "diagnosis"


def test_current_state_phrasing_maps_to_weather():
    """Turn-by-turn Step 1, the reported screenshot: 'What is the
    current state of Instagram' must be Instagram weather, not a
    clarification, even though it uses none of the old weather words."""
    s = _stub_session()
    r = s.respond("What is the current state of Instagram")
    assert r["type"] == "weather" and r["app"] == "instagram"


def test_what_about_inherits_weather_intent_with_new_app():
    s = _stub_session()
    s.respond("What is the current state of Instagram")
    r = s.respond("What about WhatsApp?")
    assert r["type"] == "weather" and r["app"] == "whatsapp"


def test_same_for_app_reruns_same_drilldown():
    s = _stub_session()
    s.respond("What's happening with Instagram?")
    s.respond("creators")
    r = s.respond("same for whatsapp")
    assert r["type"] == "drilldown" and r["app"] == "whatsapp"
    assert r["drilldown"]["topic"] == "creators"


def test_past_experiment_evaluation_clarifies():
    s = _stub_session()
    s.respond("What's happening with Instagram?")
    r = s.respond("did the new creator experiment improve retention?")
    assert r["type"] == "clarification"
    assert "assignment" in r["text"]


def test_instagram_full_flow_on_seed42_data():
    data = Path(__file__).resolve().parents[1] / "data/phase1"
    if not (data / "dim_users.parquet").exists():
        return
    s = ChatSession(data)
    w = s.respond("What's happening with Instagram?")
    assert w["type"] == "weather" and abs(w["weather"]["cards"][0]["current"] - 4543.71) < 10
    d = s.respond("creators")
    assert d["type"] == "drilldown"
    assert abs(d["drilldown"]["evidence"]["daily_creators"]["current"] - 455.79) < 1.0
    diag = s.respond("what's the diagnosis?")
    assert diag["diagnosis"]["primary_topic"] == "creators"
    rec = s.respond("what experiment would you recommend?")
    assert rec["recommendation"]["label"] == "HYPOTHESIS — not a forecast"
    brief = s.respond("give me the PM/Eng brief")
    assert brief["type"] == "brief" and brief["brief"]["pm_points"] and brief["brief"]["eng_points"]
    print("instagram headline:", w["weather"]["headline"])


def _real_session():
    data = Path(__file__).resolve().parents[1] / "data/phase1"
    if not (data / "dim_users.parquet").exists():
        return None
    return ChatSession(data)


def test_correction_topic_switches_drilldown_and_prompts_next_step():
    s = _real_session()
    if s is None:
        return
    s.respond("What's happening with Instagram?")
    s.respond("creators")
    r = s.respond("you are not looking at retention, look at retention instead")
    assert r["type"] == "drilldown" and r["drilldown"]["topic"] == "retention"
    assert r["correction"] == {"parameter": "topic", "value": "retention", "applied": True}
    assert any("diagnosis" in o["send"] for o in r["options"])


def test_correction_app_switch_without_how_is():
    s = _real_session()
    if s is None:
        return
    s.respond("What's happening with Instagram?")
    r = s.respond("you are not looking at whatsapp, use whatsapp instead")
    assert r["type"] == "weather" and r["app"] == "whatsapp"
    assert r["correction"] == {"parameter": "app", "value": "whatsapp", "applied": True}


def test_correction_grain_daily_shows_daily_series_for_growth():
    s = _real_session()
    if s is None:
        return
    s.respond("What's happening with Instagram?")
    s.respond("growth")
    r = s.respond("that's wrong, show daily not average")
    assert r["type"] == "drilldown"
    assert r["correction"] == {"parameter": "grain", "value": "daily", "applied": True}
    assert "Daily values (corrected grain" in r["text"]
    assert r["text"].count("2026-03-") >= 28


def test_correction_filter_platform_is_applied_and_scoped_honestly():
    s = _real_session()
    if s is None:
        return
    s.respond("What's happening with Instagram?")
    s.respond("growth")
    r = s.respond("you are not looking at android, use android instead")
    assert r["correction"] == {"parameter": "platform", "value": "android", "applied": True}
    assert "platform=android" in r["text"]
    assert "weather cards are app-scoped only" in r["text"]


def test_correction_without_parameter_prompts_for_parameter():
    s = _real_session()
    if s is None:
        return
    s.respond("What's happening with Instagram?")
    r = s.respond("that's wrong, you are not looking at the right thing")
    assert r["type"] == "clarification"
    assert "which parameter" in r["text"].lower()
    # Diagnosis questions must NOT be hijacked as corrections.
    s2 = _real_session()
    s2.respond("What's happening with Instagram?")
    s2.respond("creators")
    r2 = s2.respond("what's the diagnosis?")
    assert r2["type"] == "diagnosis"


def test_breakdown_by_country_and_surface_on_seed42_data():
    """Turn-by-turn Step 1, country/surface first decomposition
    (chosen 2026-10-29, ahead of creator cohorts): breakdowns are
    real queries over the current period, sorted, with shares, and
    a why question returns the same breakdown type, never a cause."""
    s = _real_session()
    if s is None:
        return
    s.respond("What's happening with Instagram?")
    b = s.respond("creators by country")
    assert b["type"] == "breakdown" and b["breakdown"]["topic"] == "creators"
    assert b["breakdown"]["dimension"] == "country"
    rows = b["breakdown"]["rows"]
    assert rows and all(r["label"] and r["count"] > 0 for r in rows)
    assert [r["count"] for r in rows] == sorted((r["count"] for r in rows), reverse=True)
    assert abs(sum(r["share"] for r in rows) - 1.0) < 0.01
    assert "not a cause" in b["text"]
    bs = s.respond("creators by surface")
    assert bs["type"] == "breakdown" and bs["breakdown"]["dimension"] == "surface"
    assert {r["label"] for r in bs["breakdown"]["rows"]} <= {"reels", "feed", "stories"}
    why = s.respond("what's driving the creator drop?")
    assert why["type"] == "breakdown" and why["breakdown"]["topic"] == "creators"
    assert "does not prove why" in why["text"]


def test_ranking_most_drop_last_2_weeks():
    """The 2026-10-29 22:55 PDT screenshot failure: after a WhatsApp
    creators breakdown, 'which country had the most drop in the last
    2 weeks' must rank by change. Seed-42 answer on 500K data is DE
    (906 vs 930 creators, -24, -2.6%; posts 2,740 vs 2,813),
    total creators 16,178 vs 16,091 — a small short-window move, not
    the Jan-Mar decline, and the reply must say so. (On 50K data the
    winner was ID; the winner is scale-dependent, the rank-by-change
    behaviour is what this test pins.)"""
    s = _real_session()
    if s is None:
        return
    s.respond("What's happening with WhatsApp?")
    s.respond("creators by country")
    r = s.respond("Which country had the most drop in the last 2 weeks")
    assert r["type"] == "ranking" and r["ranking"]["topic"] == "creators"
    assert r["ranking"]["dimension"] == "country" and r["ranking"]["days"] == 14
    rows = {row["label"]: row for row in r["ranking"]["rows"]}
    assert rows["DE"]["current"] == 906 and rows["DE"]["prior"] == 930 and rows["DE"]["change"] == -24
    assert "largest drop is DE" in r["text"]
    assert "short-window" in r["text"]
    # 'Most growth' is a direction, not the growth topic: in a
    # creators conversation it must still rank creators.
    g = s.respond("Which country had the most growth in the last 2 weeks")
    assert g["type"] == "ranking" and g["ranking"]["topic"] == "creators"
    assert "largest growth is BR" in g["text"]


def test_segment_followup_filters_drilldown():
    s = _real_session()
    if s is None:
        return
    s.respond("What's happening with Instagram?")
    r = s.respond("creators in India")
    assert r["type"] == "drilldown" and r["drilldown"]["topic"] == "creators"
    assert r["correction"] == {"parameter": "country", "value": "IN", "applied": True}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("all chat tests pass")


def test_mau_turn_standalone_no_weather():
    # 2026-11-04: MAU is a standalone metric turn — it answers as the
    # first turn in a new chat, with no weather report required, and
    # the 500K seed-42 gold is Instagram March MAU 210,809.
    s = _real_session()
    if s is None:
        return
    r = s.respond("What's the MAU for Instagram?")
    assert r["type"] == "mau" and r["app"] == "instagram"
    assert "210,809" in r["text"]
    w = r["working"]
    assert "app = 'instagram'" in w["sql"] and w["tables"] == ["fact_sessions"]
    assert w["metric_definition"]["definition"].startswith("Distinct users")
    viz = w["visualization"]
    assert viz["available"] is True and viz["chart"] is None  # single scalar: table only
    assert [st["stage"] for st in viz["diagram"]["stages"]] == [
        "sources", "filters", "aggregation", "output"]  # no invented joins/grouping


def test_mau_typo_probe():
    # Literal 2026-11-04 shell probe: the typo must not matter, since
    # mapping is on the "mau" keyword, not perfect spelling.
    s = _real_session()
    if s is None:
        return
    r = s.respond("What's teh MAU for Instagram?")
    assert r["type"] == "mau" and r["app"] == "instagram"
    assert "210,809" in r["text"]


def test_mau_no_app_clarifies_which_app():
    # No app mentioned and no app in context: clarify, never silently
    # answer all-apps MAU for a question that likely expects one app.
    s = _real_session()
    if s is None:
        return
    r = s.respond("What's the MAU?")
    assert r["type"] == "clarification"
    assert "instagram" in r["text"] and "whatsapp" in r["text"]


def test_mau_followup_what_about_swaps_app():
    s = _real_session()
    if s is None:
        return
    s.respond("What's the MAU for Instagram?")
    r = s.respond("what about whatsapp?")
    assert r["type"] == "mau" and r["app"] == "whatsapp"
    assert "140,378" in r["text"]


def test_mau_option_letter_advances():
    # Option letters work after a MAU turn (stage == "mau" satisfies
    # the letter gate): "a" is the weather option for the same app.
    s = _real_session()
    if s is None:
        return
    s.respond("What's the MAU for Instagram?")
    r = s.respond("a")
    assert r["type"] == "weather" and r["app"] == "instagram"


def test_mau_trend_probe_answers_scalar():
    # Literal 2026-11-06 shell probe (p01): "MAU trend" maps to the
    # mau_march scalar -- MAU is a monthly scalar, there is no 2-week
    # MAU trend, and the honest answer is the March number.
    s = _real_session()
    if s is None:
        return
    r = s.respond("Give me Instagram MAU trend for the last 2 weeks")
    assert r["type"] == "mau" and r["app"] == "instagram"
    assert "210,809" in r["text"]


def test_daily_series_turn_standalone_no_weather():
    # 2026-11-06 (p02): the daily-series turn answers as a later turn
    # in a chat with no weather report -- here the MAU turn first sets
    # the app, since the probe itself names no app. 500K seed-42 golds:
    # March Instagram daily DAU, 31 days, mean 45,385.32.
    s = _real_session()
    if s is None:
        return
    s.respond("Give me Instagram MAU trend for the last 2 weeks")
    r = s.respond("show me the DAU for month of Mar 2026 by individual day in a line chart")
    assert r["type"] == "daily_series" and r["app"] == "instagram"
    assert "March 2026" in r["text"] and "by day" in r["text"]
    ds = r["daily_series"]
    assert ds["start"] == "2026-03-01" and ds["end"] == "2026-03-31" and ds["days"] == 31
    assert abs(ds["mean"] - 45385.32) < 1.0 and ds["low"] == 42781 and ds["high"] == 50601
    w = r["working"]
    assert "GROUP BY date" in w["sql"] and "country" not in w["sql"]
    # The session app must reach the SQL: the probe names no app, so
    # the turn appends it -- without that, this would be the all-apps
    # series (~89K/day) presented as Instagram.
    assert "instagram" in w["sql"] and w["tables"] == ["fact_sessions", "dim_users"]
    viz = w["visualization"]
    assert viz["available"] is True and viz["chart"]["kind"] == "line"
    assert len(viz["chart"]["points"]) == 31
    assert viz["rows"][0] == ["2026-03-01", 50601] and viz["rows"][-1] == ["2026-03-31", 42820]
    assert viz["diagram"] is not None


def test_daily_series_no_app_clarifies_which_app():
    # The probe names no app, so as a first turn it correctly clarifies
    # which app -- it must never silently answer one app's series.
    s = _real_session()
    if s is None:
        return
    r = s.respond("show me the DAU for month of Mar 2026 by individual day in a line chart")
    assert r["type"] == "clarification"
    assert "instagram" in r["text"] and "whatsapp" in r["text"]


def test_daily_series_followup_what_about_swaps_app_keeps_window():
    # App-switch follow-up after a daily-series turn keeps the series
    # intent AND the window (March), like the MAU follow-up does.
    s = _real_session()
    if s is None:
        return
    s.respond("Give me Instagram MAU trend for the last 2 weeks")
    s.respond("show me the DAU for month of Mar 2026 by individual day in a line chart")
    r = s.respond("what about whatsapp?")
    assert r["type"] == "daily_series" and r["app"] == "whatsapp"
    ds = r["daily_series"]
    assert ds["start"] == "2026-03-01" and ds["end"] == "2026-03-31" and ds["days"] == 31
    assert abs(ds["mean"] - 30232.68) < 1.0
    assert r["working"]["visualization"]["chart"]["kind"] == "line"


def test_daily_series_mean_still_clarifies():
    # "mean daily DAU" is the scalar average, not a series -- the chat
    # has no scalar-DAU turn, so it clarifies, exactly as before.
    s = _real_session()
    if s is None:
        return
    r = s.respond("What is the mean daily DAU for instagram?")
    assert r["type"] == "clarification"


def test_daily_series_option_letter_advances():
    # Option letters work after a daily-series turn (stage ==
    # "daily_series" satisfies the letter gate): "a" is the weather
    # option for the same app.
    s = _real_session()
    if s is None:
        return
    s.respond("Give me Instagram MAU trend for the last 2 weeks")
    s.respond("show me the DAU for month of Mar 2026 by individual day in a line chart")
    r = s.respond("a")
    assert r["type"] == "weather" and r["app"] == "instagram"
