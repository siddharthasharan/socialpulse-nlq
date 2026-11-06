"""Planner and guard unit tests. No data files are needed for these."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from socialpulse_nlq.planner import (
    _load_catalog_metrics, build_sql, catalog_metric_ids, guard_sql, plan)


def _scope_from_templates(metric):
    """Dimensions the build_sql template for `metric` actually honors:
    call it with each filter and check the SQL contains the filter
    condition. This is the independent side of the catalog-scope check
    -- it reads the code, not the YAML."""
    f_base = {"date_start": "2026-01-01", "date_end": "2026-03-31"}
    needles = {
        "app": ("primary_app = 'instagram'", "app = 'instagram'",
                "c.app = 'instagram'", "i.app = 'instagram'"),
        "country": ("country = 'US'", "u.country = 'US'"),
        "platform": ("platform = 'android'", "u.platform = 'android'"),
        "surface": ("surface = 'reels'", "c.surface = 'reels'",
                    "i.surface = 'reels'"),
        "acquisition": ("acquisition_source = 'organic'",),
    }
    values = {"app": "instagram", "country": "US", "platform": "android",
              "surface": "reels", "acquisition": "organic"}
    out = []
    for dim in ("app", "country", "platform", "surface", "acquisition"):
        f = dict(f_base, **{dim: values[dim]})
        sql, _tables, _grain = build_sql(metric, f, None, "test question")
        if any(n in sql for n in needles[dim]):
            out.append(dim)
    return out


def test_catalog_scope_matches_templates():
    """Slice 3: the catalog scope field must be a faithful transcription
    of what the templates do. A mismatch means the catalog drifted from
    the code (or the code changed without updating the catalog)."""
    catalog = _load_catalog_metrics()
    assert len(catalog) == 20
    for metric, entry in sorted(catalog.items()):
        assert entry["scope"] is not None, f"{metric} has no scope line"
        assert sorted(_scope_from_templates(metric)) == sorted(entry["scope"]), \
            f"{metric}: templates honor {_scope_from_templates(metric)}, catalog says {entry['scope']}"


def test_baseline_default_unchanged():
    """plan(q) is plan(q, variant='baseline') for a spread of questions."""
    for q in ("What is the mean daily DAU for instagram?",
              "What is creator penetration for instagram?",
              "What was DAU in April 2026?",
              "List all sessions",
              "How is the product doing?"):
        assert plan(q) == plan(q, variant="baseline"), q


def test_unknown_variant_raises():
    try:
        plan("What is DAU?", variant="nope")
    except ValueError:
        return
    raise AssertionError("unknown variant did not raise")


def test_catalog_assisted_scope_trap_clarifies():
    p = plan("What is creator penetration for instagram?", variant="catalog-assisted")
    assert p["type"] == "clarification" and p["sql"] is None
    assert "creator_penetration" in p["clarification"] and "app" in p["clarification"]
    # No scope requested: the same metric still answers.
    p2 = plan("What is creator penetration?", variant="catalog-assisted")
    assert p2["type"] == "query" and p2["metric"] == "creator_penetration"


def test_catalog_assisted_app_scoped_dau_still_queries():
    p = plan("What is the mean daily DAU for instagram?", variant="catalog-assisted")
    assert p["type"] == "query" and p["metric"] == "dau_mean_daily"
    assert "primary_app = 'instagram'" in p["sql"]


def test_catalog_assisted_reels_metric_not_blocked():
    """The metric-name conflation: 'reels' in a Reels-metric question is
    the metric, not a surface-scope request."""
    for q in ("What is the Reels completion rate?",
              "What is Reels watch time per viewer?"):
        p = plan(q, variant="catalog-assisted")
        assert p["type"] == "query", q


def test_catalog_assisted_window_not_fixed():
    """Documented limitation (docs/ablations.md): the catalog has no
    window dimension, so catalog-assisted does NOT fix April questions."""
    p = plan("What was DAU in April 2026?", variant="catalog-assisted")
    assert p["type"] == "query" and p["metric"] == "dau_mean_daily"


def test_catalog_assisted_experiment_registry_agrees():
    p = plan("What is the primary metric of the Reels ranking experiment?",
             variant="catalog-assisted")
    assert p["type"] == "experiment_info"
    assert p["metric"] == "reels_watch_time_per_viewer_day"


def test_validated_scope_trap_clarifies():
    p = plan("What is creator penetration for instagram?", variant="validated")
    assert p["type"] == "clarification" and p["sql"] is None
    assert "app" in p["clarification"]


def test_validated_april_clarifies():
    p = plan("What was DAU in April 2026?", variant="validated")
    assert p["type"] == "clarification" and p["sql"] is None
    assert "april" in p["clarification"].lower()


def test_validated_normal_query_passes():
    p = plan("What is the mean daily DAU for instagram?", variant="validated")
    assert p["type"] == "query" and p["metric"] == "dau_mean_daily"


def test_validated_routed_and_weather_pass_through():
    """Validation applies to query plans only: routed plans and weather
    reports are not second-guessed."""
    p = plan("List all sessions", variant="validated")
    assert p["type"] == "query" and p.get("routed") is True
    p2 = plan("How is the product doing?", variant="validated")
    assert p2["type"] == "weather_report"


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
        assert all(v < 50000 for v in vals), vals  # all-app would be ~89,000+
        assert abs(sum(vals) / len(vals) - 29735.43) < 1.0


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


def test_mau_app_scoped_sql():
    # 2026-11-04: mau_march must filter to the app asked about, so an
    # Instagram question no longer returns all-apps MAU.
    p = plan("What is the MAU for Instagram?")
    assert p["type"] == "query" and p["metric"] == "mau_march"
    assert "app = 'instagram'" in p["sql"]
    assert "2026-03-01" in p["sql"] and "2026-03-31" in p["sql"]


def test_mau_no_app_unchanged_all_apps():
    # Without an app the query is unchanged: all-apps March MAU.
    p = plan("What is MAU in March?")
    assert p["metric"] == "mau_march"
    assert "app = " not in p["sql"]


def test_stickiness_app_scopes_numerator_and_denominator():
    # Stickiness is a ratio for ONE population: the app filter must
    # appear in both the DAU numerator and the MAU denominator, or
    # the ratio would mix all-app DAU with app MAU.
    p = plan("What is DAU/MAU stickiness for Instagram?")
    assert p["metric"] == "dau_mau_stickiness"
    assert p["sql"].count("app = 'instagram'") == 2


def test_stickiness_no_app_unchanged():
    p = plan("What is DAU/MAU stickiness?")
    assert p["metric"] == "dau_mau_stickiness"
    assert "app = " not in p["sql"]
