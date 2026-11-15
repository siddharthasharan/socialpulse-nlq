"""SQL visualization: deterministic parser, chart heuristic, payload.

No LLM, no new queries: parser tests run on SQL strings, chart tests
on rows, and payload tests on the existing seed-42 parquet via the
chat/planner paths that already execute those queries.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from socialpulse_nlq.sqlviz import parse_sql, build_visualization, PREVIEW_LIMIT
from socialpulse_nlq.planner import plan, run_plan, build_sql
from socialpulse_nlq.chat import ChatSession

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/phase1"

CHAT_MONTHLY = (
    "SELECT CAST(DATE_TRUNC('month', CAST(f.published_at AS DATE)) AS VARCHAR) AS month, "
    "COUNT(*) AS posts, COUNT(DISTINCT f.creator_id) AS creators "
    "FROM fact_content_creation f JOIN dim_content c ON c.content_id = f.content_id "
    "WHERE 1=1 AND c.app = 'instagram' GROUP BY month ORDER BY month")
CHAT_DAILY_DAU = (
    "SELECT CAST(s.start_time AS DATE) AS d, COUNT(DISTINCT s.user_id) AS dau "
    "FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id "
    "WHERE CAST(s.start_time AS DATE) BETWEEN DATE '2026-03-04' AND DATE '2026-03-31' "
    "AND u.primary_app = 'instagram' GROUP BY d ORDER BY d")


def test_parser_chat_monthly_tables_joins_filters_aggregation():
    d, reason = parse_sql(CHAT_MONTHLY)
    assert d is not None, reason
    stages = {s["stage"]: s["items"] for s in d["stages"]}
    assert "fact_content_creation AS f" in stages["sources"]
    assert any("dim_content ON c.content_id = f.content_id" in j for j in stages["joins"])
    assert stages["filters"] == ["c.app = 'instagram'"]
    assert "COUNT(*) AS posts" in stages["aggregation"]
    assert "COUNT(DISTINCT f.creator_id) AS creators" in stages["aggregation"]


def test_parser_between_and_not_split():
    d, reason = parse_sql(CHAT_DAILY_DAU)
    assert d is not None, reason
    stages = {s["stage"]: s["items"] for s in d["stages"]}
    assert stages["filters"] == [
        "CAST(s.start_time AS DATE) BETWEEN DATE '2026-03-04' AND DATE '2026-03-31'",
        "u.primary_app = 'instagram'"]


def test_parser_subquery_wrapped_scalar_is_modelled():
    p = plan("What is daily creators for instagram?")
    d, reason = parse_sql(p["sql"])
    assert d is not None, reason
    assert d["subquery"] is True
    assert any("AVG(creators) AS value" in a for s in d["stages"]
               if s["stage"] == "aggregation" for a in s["items"])


def test_parser_unmodelled_shapes_are_honest_not_guessed():
    cases = {}
    cases["with"], _, _ = build_sql("top1_creator_impression_share",
                                    {"date_start": "2026-01-01", "date_end": "2026-03-31"}, None, "")
    cases["union"] = plan("What is engagement rate by surface?")["sql"]
    cases["using_join"], _, _ = build_sql("creator_penetration",
                                          {"date_start": "2026-01-01", "date_end": "2026-03-31"}, None, "")
    cases["scalar_subquery"], _, _ = build_sql("orphan_post_rate",
                                               {"date_start": "2026-01-01", "date_end": "2026-03-31"}, None, "")
    for name, sql in cases.items():
        d, reason = parse_sql(sql)
        assert d is None and reason, (name, sql)
        assert "does not model" in reason or "subquery" in reason, (name, reason)


def test_chart_bar_for_category_line_for_dates_table_only_for_scalar():
    bar = build_visualization("SELECT x, y FROM t", ["country", "dau"],
                              [["US", 100], ["IN", 80]])
    assert bar["chart"]["kind"] == "bar" and bar["diagram"] is not None
    line = build_visualization("SELECT x, y FROM t", ["date", "dau"],
                               [["2026-03-01", 100], ["2026-03-02", 110]])
    assert line["chart"]["kind"] == "line"
    scalar = build_visualization("SELECT x FROM t", ["value"], [[42.0]])
    assert scalar["chart"] is None and "single column" in scalar["chart_note"]
    three = build_visualization("SELECT a, b, c FROM t", ["country", "date", "dau"],
                                [["US", "2026-03-01", 100]])
    assert three["chart"] is None and three["chart_note"] is not None


def test_preview_cap_is_stated():
    rows = [[f"2026-03-{i:02d}", i] for i in range(1, 80)]
    v = build_visualization("SELECT d, dau FROM t", ["date", "dau"], rows)
    assert v["capped"] is True and len(v["rows"]) == PREVIEW_LIMIT and v["row_count"] == 79


def test_routed_sql_diagrams_only_the_executed_aggregate():
    p = plan("List all sessions")
    assert p.get("routed") is True
    cols, rows, _ = run_plan(p, DATA)
    v = build_visualization(p["sql"], cols, rows)
    assert v["diagram"] is not None
    assert "agg_user_daily" in v["diagram"]["tables"]
    # The raw SQL is never passed to the builder: a caller that did
    # would be diagramming a query that did not run.
    assert "fact_sessions" not in v["diagram"]["tables"]


def test_chat_drilldown_working_carries_visualization():
    session = ChatSession(DATA)
    session.respond("What's happening with Instagram?")
    reply = session.respond("creators")
    viz = reply["working"]["visualization"]
    assert viz["available"] is True
    assert viz["columns"] == ["month", "posts", "creators"]
    assert viz["chart"] is not None
    assert viz["diagram"] is not None
    months = [r[0] for r in viz["rows"]]
    assert months == ["2026-01", "2026-02", "2026-03"]


def test_chat_breakdown_working_carries_bar_chart_and_diagram():
    session = ChatSession(DATA)
    session.respond("What's happening with Instagram?")
    reply = session.respond("creators by country")
    viz = reply["working"]["visualization"]
    assert viz["chart"]["kind"] == "bar"
    assert viz["diagram"] is not None
    assert len(viz["rows"]) == viz["row_count"] == len(reply["breakdown"]["rows"])


def test_chat_weather_has_no_visualization():
    session = ChatSession(DATA)
    reply = session.respond("What's happening with Instagram?")
    assert reply["type"] == "weather" and "working" not in reply


def test_gallery_plan_rows_chart_or_table_honestly():
    for card_q in ("What was DAU for instagram by country over the last 28 days?",
                   "What is engagement rate by surface?"):
        p = plan(card_q)
        cols, rows, _ = run_plan(p, DATA)
        v = build_visualization(p["sql"], cols, rows)
        assert v["available"] is True and v["rows"]
        # Engagement by surface is a UNION query: table + bar chart,
        # but honestly no diagram.
        if "engagement" in card_q:
            assert v["chart"]["kind"] == "bar"
            assert v["diagram"] is None and v["diagram_unavailable_reason"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("all sqlviz tests pass")


def test_chart_multi_series_line_for_dates_with_two_numeric_columns():
    # 2026-11-13 (v11, p19/p20): a result whose first column looks
    # like a date and whose second AND third columns are numeric
    # draws a multi-series line chart -- one line per numeric column,
    # in column order. The payload carries "series" alongside the
    # single-series "points" (the first series, kept so the existing
    # single-series renderers keep drawing the first series
    # unchanged) and "y_label" (the first series' label).
    v = build_visualization("SELECT w, instagram, whatsapp FROM t",
                            ["w", "instagram", "whatsapp"],
                            [["2026-03-02", 147939, 98372],
                             ["2026-03-09", 147048, 98125],
                             ["2026-03-16", 147057, 98063]])
    chart = v["chart"]
    assert chart["kind"] == "line"
    assert chart["x_label"] == "w" and chart["y_label"] == "instagram"
    assert [s["label"] for s in chart["series"]] == ["instagram", "whatsapp"]
    assert chart["series"][0]["points"] == chart["points"]
    assert chart["series"][0]["points"] == [
        {"x": "2026-03-02", "y": 147939},
        {"x": "2026-03-09", "y": 147048},
        {"x": "2026-03-16", "y": 147057}]
    assert chart["series"][1]["points"] == [
        {"x": "2026-03-02", "y": 98372},
        {"x": "2026-03-09", "y": 98125},
        {"x": "2026-03-16", "y": 98063}]
    assert v["rows"][0] == ["2026-03-02", 147939, 98372]


def test_chart_single_series_unchanged_no_series_key():
    # Single-series behavior is byte-identical: a two-column date
    # result carries no "series" key at all, so nothing downstream
    # can mistake it for a multi-series chart.
    v = build_visualization("SELECT w, wau FROM t", ["w", "wau"],
                            [["2026-03-02", 147939], ["2026-03-09", 147048]])
    chart = v["chart"]
    assert chart["kind"] == "line"
    assert "series" not in chart
    assert chart["points"] == [{"x": "2026-03-02", "y": 147939},
                               {"x": "2026-03-09", "y": 147048}]
    assert chart["y_label"] == "wau"


def test_chart_multi_series_falls_back_when_third_column_not_numeric():
    # If any later column is not numeric for every row, the payload
    # falls back to the single-series chart of the first two columns
    # -- the third column stays in the table only, never faked into
    # a line.
    v = build_visualization("SELECT w, wau, note FROM t",
                            ["w", "wau", "note"],
                            [["2026-03-02", 147939, "peak"],
                             ["2026-03-09", 147048, "flat"]])
    chart = v["chart"]
    assert chart["kind"] == "line"
    assert "series" not in chart
    assert chart["points"] == [{"x": "2026-03-02", "y": 147939},
                               {"x": "2026-03-09", "y": 147048}]


def test_chart_bar_with_three_columns_unchanged():
    # Multi-series applies to line charts only: a bar result keeps
    # the old behavior (first two columns charted, further columns in
    # the table only).
    v = build_visualization("SELECT c, dau, extra FROM t",
                            ["country", "dau", "extra"],
                            [["US", 100, 5], ["IN", 80, 4]])
    chart = v["chart"]
    assert chart["kind"] == "bar"
    assert "series" not in chart
    assert chart["points"] == [{"x": "US", "y": 100}, {"x": "IN", "y": 80}]
