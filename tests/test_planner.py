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


def test_dau_by_day_returns_series_not_average():
    # 2026-11-06 (daily-series turn): "by day" / "by individual day"
    # / "each day" ask for the date-by-date series. Before this fix
    # the probe phrasing fell through to the scalar average, because
    # none of the series triggers ("daily", "per day", "trend") matched.
    p = plan("DAU for instagram by day in March")
    assert p["type"] == "query" and p["grain"] == "user x day"
    assert "GROUP BY date" in p["sql"] and "AVG(dau)" not in p["sql"]
    p2 = plan("show me the DAU for month of Mar 2026 by individual day in a line chart")
    assert p2["grain"] == "user x day" and "GROUP BY date" in p2["sql"]
    assert p2["filters"]["date_start"] == "2026-03-01" and p2["filters"]["date_end"] == "2026-03-31"
    assert "country" not in p2["filters"]  # "in a line chart" is not India
    # Explicit mean/average still returns the scalar average.
    p3 = plan("What is the mean daily DAU for instagram in March?")
    assert p3["grain"] == "user x day (averaged)" and "AVG(dau)" in p3["sql"]
    p4 = plan("What is the average daily DAU for whatsapp?")
    assert p4["grain"] == "user x day (averaged)"


def test_in_a_line_chart_is_not_india():
    # 2026-11-06: "in a/an ..." is presentation phrasing, never the
    # country code IN. The probe's "in a line chart" used to set
    # country IN and silently return India-only numbers.
    p = plan("DAU for instagram by day in a line chart")
    assert "country" not in p["filters"]
    # A genuine India question still scopes.
    p2 = plan("mean daily DAU in India")
    assert p2["filters"]["country"] == "IN"
    p3 = plan("DAU for instagram in India by day")
    assert p3["filters"]["country"] == "IN" and p3["grain"] == "user x day"


def test_month_abbreviations_map_to_month_windows():
    # 2026-11-06: "Mar"/"Feb"/"Jan" are months, not words to ignore.
    # The probe's "month of Mar 2026" used to fall back to the full
    # 90-day window. Abbreviations also must not read as country IN
    # ("in Feb" / "in Jan" are months, like "in February").
    p = plan("DAU for instagram in Feb by day")
    assert p["filters"] == {"date_start": "2026-02-01", "date_end": "2026-02-28",
                            "app": "instagram"}
    p2 = plan("daily DAU for whatsapp in Jan")
    assert p2["filters"] == {"date_start": "2026-01-01", "date_end": "2026-01-31",
                             "app": "whatsapp"}
    p3 = plan("What is the mean daily DAU for instagram in Jan?")
    assert p3["grain"] == "user x day (averaged)"
    assert p3["filters"]["date_start"] == "2026-01-01"


def test_in_the_window_and_in_minutes_are_prepositions():
    # 2026-11-08 ('in'-preposition pass): "in the window" (b05) and
    # "in minutes" (b07) are prepositions, not the country code IN.
    # The old test pinned the buggy behavior (country IN); this one
    # pins the fix, which also subsumes the slice-2b guard: bare "in"
    # is a country only when it is literally the code ("in IN"), so
    # "in Brazil" / "in the UK" now scope to the named country instead
    # of silently reading as India. b05 must answer under ALL THREE
    # variants -- before the fix the variants wrongly clarified.
    p = plan("How many new users were there in the window?")
    assert "country" not in p["filters"]
    for variant in ("baseline", "catalog-assisted", "validated"):
        pv = plan("How many new users were there in the window?", variant=variant)
        assert pv["type"] == "query" and pv["sql"] is not None
        assert "country" not in pv["sql"]
    p2 = plan("What is the median session duration in minutes?")
    assert "country" not in p2["filters"]
    assert "country" not in (p2["sql"] or "")
    # Genuine India phrasings still scope.
    p3 = plan("mean daily DAU in India")
    assert p3["filters"]["country"] == "IN"
    p4 = plan("What is DAU in IN?")
    assert p4["filters"]["country"] == "IN"
    p5 = plan("DAU in India in January")
    assert p5["filters"]["country"] == "IN"
    assert p5["filters"]["date_start"] == "2026-01-01"
    # Slice-2b behavior intact: months are windows, not India.
    p6 = plan("mean daily DAU for Instagram in January")
    assert "country" not in p6["filters"]
    # The wider class: a named non-India country after "in" scopes
    # to that country, not India.
    assert plan("What is DAU in Brazil?")["filters"]["country"] == "BR"
    assert plan("What is DAU in the UK?")["filters"]["country"] == "GB"


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


def test_mau_by_country_grouped_sql():
    # 2026-11-10: "MAU for Instagram by country" is a grouped query,
    # not the mau_march scalar. The rows add up to the MAU total
    # (each user has exactly one country), so the app filter is on
    # fact_sessions.app -- the same session-app definition as
    # mau_march -- and there is no country filter in the SQL.
    p = plan("What is the MAU for Instagram by country?")
    assert p["type"] == "query" and p["metric"] == "mau_by_country"
    assert "GROUP BY u.country" in p["sql"]
    assert "COUNT(DISTINCT s.user_id)" in p["sql"]
    assert "s.app = 'instagram'" in p["sql"]
    assert "2026-03-01" in p["sql"] and "2026-03-31" in p["sql"]
    assert "country = '" not in p["sql"]
    # The plain MAU question is still the scalar, and the other apps
    # scope the same way.
    assert plan("What is the MAU for Instagram?")["metric"] == "mau_march"
    p_wa = plan("MAU by country for WhatsApp")
    assert p_wa["metric"] == "mau_by_country" and "s.app = 'whatsapp'" in p_wa["sql"]


def test_mau_by_country_no_app_all_apps():
    # Without an app the grouped query is the all-apps MAU split by
    # country -- the same no-app convention as the mau_march scalar.
    p = plan("MAU by country")
    assert p["metric"] == "mau_by_country"
    assert "app = " not in p["sql"]


def test_mau_by_surface_clarifies_in_planner():
    # MAU by surface is not a query: one user can appear on several
    # surfaces, so per-surface distinct-user counts would add up to
    # more than the MAU total. The planner refuses instead of
    # silently returning the scalar for a breakdown question; the
    # chat layer clarifies with the same reason.
    p = plan("MAU by surface for Instagram")
    assert p["type"] == "clarification" and p["metric"] == "mau_march"
    assert "surface" in p["clarification"] and "country" in p["clarification"]


def test_creator_share_sql():
    # 2026-11-10 (p08/p09): "% of creators of MAU" and "number of
    # creators and all users" are one question -- the monthly creator
    # share, not the MAU scalar. One combined CTE query: monthly
    # distinct creators (fact_content_creation joined to dim_content
    # for the app) over monthly distinct users (fact_sessions, the
    # same session-app definition as mau_march). 500K seed-42 golds,
    # verified three ways in the oracle: creators 31,297, MAU
    # 210,809, share 14.85%.
    p = plan("What is the % of creators of MAU in Instagram")
    assert p["type"] == "query" and p["metric"] == "creator_share"
    assert p["sql"].startswith("WITH creators AS (")
    assert "COUNT(DISTINCT f.creator_id)" in p["sql"]
    assert "COUNT(DISTINCT user_id)" in p["sql"]
    assert "c.app = 'instagram'" in p["sql"] and "app = 'instagram'" in p["sql"]
    assert "2026-03-01" in p["sql"] and "2026-03-31" in p["sql"]
    assert "share_pct" in p["sql"] and "country = '" not in p["sql"]
    assert p["tables"] == ["fact_content_creation", "dim_content", "fact_sessions"]
    # The rephrased probe maps the same way, and so does the
    # bare "what share of users are creators" form.
    assert plan("what is the number of creators and all users in Instagram for March 2026")["metric"] == "creator_share"
    assert plan("what share of users are creators in Instagram")["metric"] == "creator_share"
    # The other apps scope the same way.
    p_wa = plan("What is the % of creators of MAU in WhatsApp")
    assert p_wa["metric"] == "creator_share" and "c.app = 'whatsapp'" in p_wa["sql"]
    # No app: the all-apps share, the same no-app convention as mau_march.
    p_all = plan("What is the % of creators of MAU")
    assert p_all["metric"] == "creator_share" and "app = '" not in p_all["sql"]


def test_creator_share_does_not_hijack():
    # The creator_share branch must not swallow the neighbours: the
    # plain MAU question stays the scalar, MAU by country stays the
    # grouped query, "creators by country" is a breakdown question
    # (no share asked for), and the weather's daily penetration is a
    # different metric entirely.
    assert plan("What's the MAU for Instagram?")["metric"] == "mau_march"
    assert plan("What is the MAU for Instagram by country?")["metric"] == "mau_by_country"
    assert plan("creators by country for Instagram")["metric"] != "creator_share"
    assert plan("what's driving the creator drop for Instagram")["metric"] != "creator_share"
    assert plan("What is the creator penetration for Instagram?")["metric"] == "creator_penetration"
    assert plan("how many creators are there per day on Instagram")["metric"] == "daily_creators_mean"
    # A breakdown of the share itself is refused, not silently
    # answered as the plain share.
    p = plan("What is the % of creators of MAU in Instagram by country?")
    assert p["type"] == "clarification" and p["metric"] == "creator_share"


def test_creator_share_variants_abstain():
    # Variant boundary (same as mau_by_country): creator_share is
    # not in the 20-metric catalog, so catalog-assisted and
    # validated abstain on it; only the baseline answers it. The
    # benchmark runs the baseline; if the metric is ever added to
    # the catalog, this test is the one that has to change.
    q = "What is the % of creators of MAU in Instagram"
    assert plan(q, variant="baseline")["metric"] == "creator_share"
    for variant in ("catalog-assisted", "validated"):
        p = plan(q, variant=variant)
        assert p["type"] == "clarification" and p["sql"] is None, variant
        assert "no catalog definition" in p["clarification"], variant


def test_mau_creators_by_country_sql():
    # 2026-11-11 (p11): "Monthly active users and creators by
    # country" is one breakdown with both populations side by side
    # per country, plus the share -- not MAU by country alone (the
    # Nov 11 live failure: the creators half was dropped). One
    # combined CTE query: monthly distinct users per country
    # (fact_sessions joined to dim_users, the mau_by_country
    # definition) and monthly distinct creators per country
    # (fact_content_creation joined to dim_content for the app and
    # to dim_users on creator_id -- the EXACT p05 definition).
    # 500K seed-42 golds, verified two ways in the oracle (grouped
    # COUNT(DISTINCT) CTE == DISTINCT-pairs CTE on both counts per
    # country; columns tie to p06 and p05 exactly).
    p = plan("What are the monthly active users and creators by country for Instagram?")
    assert p["type"] == "query" and p["metric"] == "mau_creators_by_country"
    assert p["sql"].startswith("WITH mau_by_country AS (")
    assert "COUNT(DISTINCT s.user_id)" in p["sql"]
    assert "COUNT(DISTINCT f.creator_id)" in p["sql"]
    assert "s.app = 'instagram'" in p["sql"] and "c.app = 'instagram'" in p["sql"]
    assert "GROUP BY u.country" in p["sql"] and "ORDER BY mau DESC" in p["sql"]
    assert "2026-03-01" in p["sql"] and "2026-03-31" in p["sql"]
    assert "share_pct" in p["sql"] and "country = '" not in p["sql"]
    assert p["tables"] == ["fact_sessions", "dim_users", "fact_content_creation", "dim_content"]
    # The live probe phrasing maps the same way, and so do the
    # other apps.
    assert plan("I need the Monthly active users and creators by country")["metric"] == "mau_creators_by_country"
    p_wa = plan("What are the monthly active users and creators by country for WhatsApp?")
    assert p_wa["metric"] == "mau_creators_by_country" and "s.app = 'whatsapp'" in p_wa["sql"]
    # No app: the all-apps combined cut, the same no-app convention
    # as mau_march / mau_by_country.
    p_all = plan("What are the monthly active users and creators by country?")
    assert p_all["metric"] == "mau_creators_by_country" and "app = '" not in p_all["sql"]


def test_mau_creators_by_country_does_not_hijack():
    # The new branch must not swallow the neighbours: the plain MAU
    # question stays the scalar, MAU by country stays the grouped
    # query, "creators by country" is a breakdown question (no MAU
    # named), and "% of creators of MAU" stays the scalar share.
    assert plan("What's the MAU for Instagram?")["metric"] == "mau_march"
    assert plan("What is the MAU for Instagram by country?")["metric"] == "mau_by_country"
    assert plan("creators by country for Instagram")["metric"] != "mau_creators_by_country"
    assert plan("What is the % of creators of MAU in Instagram")["metric"] == "creator_share"
    # Share language keeps the pinned v7 behaviour: "% of creators
    # of MAU ... by country" is a breakdown of the scalar share and
    # is refused at the planner level (the chat layer sends it to
    # the combined turn instead -- see the branch comment).
    p = plan("What is the % of creators of MAU in Instagram by country?")
    assert p["type"] == "clarification" and p["metric"] == "creator_share"


def test_mau_creators_by_country_variants_abstain():
    # Variant boundary (same as mau_by_country / creator_share):
    # the metric is not in the 20-metric catalog, so
    # catalog-assisted and validated abstain on it; only the
    # baseline answers it. The benchmark runs the baseline; if the
    # metric is ever added to the catalog, this test is the one that
    # has to change.
    q = "What are the monthly active users and creators by country for Instagram?"
    assert plan(q, variant="baseline")["metric"] == "mau_creators_by_country"
    for variant in ("catalog-assisted", "validated"):
        p = plan(q, variant=variant)
        assert p["type"] == "clarification" and p["sql"] is None, variant
        assert "no catalog definition" in p["clarification"], variant
