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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("all chat tests pass")
