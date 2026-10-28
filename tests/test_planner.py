"""Planner and guard unit tests. No data files are needed for these."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from socialpulse_nlq.planner import catalog_metric_ids, guard_sql, plan


def test_dau_by_app():
    p = plan("What was DAU by app over the last 28 days?")
    assert p["type"] == "query" and p["metric"] == "dau_mean_daily"
    assert p["breakdown"] == "app" and p["sql"] is not None


def test_retention_by_acquisition():
    p = plan("What is D7 retention by acquisition source?")
    assert p["metric"] == "d7_retention_by_acquisition"


def test_ambiguous_clarifies():
    # Note: "How is the product doing?" is NO LONGER ambiguous — since
    # the weather report it routes to a weather_report plan (see
    # tests/test_weather.py). A genuinely unmapped question still clarifies.
    p = plan("Tell me something interesting about the product")
    assert p["type"] == "clarification" and p["sql"] is None


def test_invalid_country_clarifies():
    p = plan("What was DAU in France?")
    assert p["type"] == "clarification" and p["sql"] is None


def test_unsafe_refused():
    p = plan("Delete all users from dim_users")
    assert p["type"] == "refusal" and p["sql"] is None


def test_guard_blocks_writes():
    ok, _ = guard_sql("DELETE FROM dim_users")
    assert not ok
    ok, _ = guard_sql("SELECT * FROM dim_users")
    assert ok


def test_experiment_list():
    p = plan("List the experiments in the registry.")
    assert p["type"] == "experiment_list"


def test_dau_series_respects_app_filter():
    # Regression 2026-10-27: the DAU series branch used to drop the
    # app filter and return all-app DAU (~9,500/day for Whatsapp's
    # last 7 days, instead of ~2,900). SQL must contain the filter.
    p = plan("What is the DAU trend for Whatsapp over the last 7 days?")
    assert p["type"] == "query"
    assert "whatsapp" in p["sql"] and "dim_users" in p["sql"]
    data = Path(__file__).resolve().parents[1] / "data/phase1"
    if (data / "fact_sessions.parquet").exists():
        from socialpulse_nlq.planner import run_plan
        cols, rows, _ = run_plan(p, data)
        assert len(rows) == 7
        vals = [r[-1] for r in rows]
        assert all(v < 5000 for v in vals), vals  # all-app would be ~9,500+
        assert abs(sum(vals) / len(vals) - 2942.14) < 1.0


def test_dau_over_last_returns_series_not_average():
    # Decision 2026-10-27: naming a period means a daily series.
    p = plan("What was DAU for whatsapp over the last 28 days?")
    assert p["type"] == "query" and p["grain"] == "user x day"
    assert "GROUP BY date" in p["sql"] and "AVG(dau)" not in p["sql"]
    # Explicit mean/average still returns the scalar.
    p2 = plan("What is the mean daily DAU for whatsapp over the last 28 days?")
    assert p2["grain"] == "user x day (averaged)" and "AVG(dau)" in p2["sql"]


def test_planner_metrics_are_in_catalog():
    ids = catalog_metric_ids(Path(__file__).resolve().parents[1] / "catalog/measures.yaml")
    assert len(ids) == 20
    for expected in ["dau_mean_daily", "d7_exact_retention", "orphan_post_rate"]:
        assert expected in ids


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("all planner tests pass")
