"""Weather report tests. Pure-logic tests need no data; the integration
test runs only when Phase 1 parquet is present (as in CI, where the
generator runs first)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from socialpulse_nlq.planner import plan
from socialpulse_nlq.weather import _card, classify_change, compute_weather


def test_classify_count_threshold():
    assert classify_change(105, 100, "count")[0] == "up"
    assert classify_change(101, 100, "count")[0] == "flat"
    assert classify_change(97, 100, "count")[0] == "down"


def test_classify_rate_threshold_in_points():
    assert classify_change(0.22, 0.20, "rate")[0] == "up"
    assert classify_change(0.205, 0.20, "rate")[0] == "flat"


def test_orphan_up_is_bad():
    c = _card("orphan_post_rate", "Orphan", 0.25, None, "rate", "share", lower_is_better=True)
    assert c["direction"] == "flat"  # level only, no prior: no direction claim
    c2 = _card("x", "X", 0.30, 0.20, "rate", "share", lower_is_better=True)
    assert c2["direction"] == "up" and c2["good_bad"] == "bad"


def test_product_doing_routes_to_weather_not_clarification():
    for q in ["How is the product doing?", "How is product doing?", "Show me the weather report"]:
        p = plan(q)
        assert p["type"] == "weather_report", (q, p)
        assert p["sql"] is None


def test_weather_on_seed42_data():
    data = Path(__file__).resolve().parents[1] / "data/phase1"
    if not (data / "dim_users.parquet").exists():
        return  # data-dependent; skipped without data
    w = compute_weather(data)
    ids = {c["id"] for c in w["cards"]}
    assert {"dau", "wau", "d7_retention", "daily_creators", "orphan_post_rate"} <= ids
    dau = [c for c in w["cards"] if c["id"] == "dau"][0]
    assert dau["current"] > 0
    assert "Creator" in " ".join(w["watchouts"]) or "creators" in w["headline"].lower()
    print("weather headline:", w["headline"])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("all weather tests pass")
