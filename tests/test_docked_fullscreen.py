"""Regression test for the full-screen docked chat (2026-10-29).

The user asked that the docked chat expand to full screen, and that
data and charts from a chat drill-down be shown in the docked chat
only — not in the dashboard behind it. So this test asserts:
  1. The full-screen toggle exists and covers the viewport, hiding
     the dashboard behind an opaque window, with an exit path.
  2. The docked log renders drill-down charts (growth daily DAU line,
     creators monthly posts bars) from the server's drilldown evidence,
     and that rendering lives in renderDockedLog only.
  3. The pre-existing docked window and dashboard sections are intact.
"""
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "app/index.html"


def test_fullscreen_toggle_present():
    html = HTML.read_text()
    for marker in ('id="dockedfullbtn"', "toggleDockedFullscreen",
                   "#docked.fullscreen", "body.docked-full",
                   "⛶ Full screen", "↩ Dock"):
        assert marker in html, f"missing full-screen piece: {marker}"
    # Collapsing must exit full screen, otherwise a collapsed
    # full-screen window would leave a full-height header bar.
    assert "collapsed" in html and "fullscreen" in html


def test_docked_charts_render_in_docked_log_only():
    html = HTML.read_text()
    for marker in ("dockchart", "daily_dau_series", "monthly_creations",
                   "drawDockedLine", "drawDockedBars",
                   "drilldown: d.drilldown",
                   "shown here only"):
        assert marker in html, f"missing docked-chart piece: {marker}"
    # The chart code must live in renderDockedLog, not in the Ask
    # section's drawChart (which renders into the dashboard #out).
    docked_section = html.split("function renderDockedLog()")[1].split(
        "document.getElementById('dockedlog').addEventListener")[0]
    assert "dockchart" in docked_section


def test_pinned_input_in_fullscreen():
    """Pinned input (2026-10-29, user choice): in full screen the input
    stays at the bottom and only the log scrolls above it.

    Regression for the bug seen on Render 2026-10-29 13:51 PDT: after
    several turns the input bar was off-screen, because #docked was
    height:100vh but not display:flex, so #dockedbody's flex:1 did
    nothing, the log grew, and the input was pushed below the viewport
    while body scroll was locked. The fix is a flex column on the
    full-screen window: header and input rows shrink-proof, log flexes
    with min-height:0 and its own scroll.
    """
    html = HTML.read_text()
    block = html.split("#docked.fullscreen{")[1].split("}")[0]
    assert "display:flex" in block, "fullscreen window must be a flex column"
    assert "flex-direction:column" in block
    assert "#docked.fullscreen header{flex-shrink:0}" in html
    assert "#docked.fullscreen #dockedlog{max-height:none;flex:1;min-height:0;overflow:auto}" in html
    assert "#docked.fullscreen #dockedbody > p{flex-shrink:0}" in html


def test_dashboard_and_docked_still_present():
    html = HTML.read_text()
    for marker in ('id="docked"', 'id="dockedlog"', 'id="dockedq"',
                   "sendDockedMsg", "toggleDocked",
                   "Product health — weather report",
                   "Phase 3 chat — multi-turn assistant"):
        assert marker in html, f"missing piece: {marker}"
