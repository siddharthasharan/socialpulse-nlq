"""Regression test for the three small chat-UI changes (2026-10-29).

1. Hide dashboard: exiting full screen used to bring the older
   dashboard page back. The new "Hide dashboard" button removes it
   from view (CSS only, never deleted), so Docking back does not
   restore it until "Show dashboard" is clicked.
2. No grey placeholder question in the chat input bars.
3. Rounded corners on buttons, inputs, the docked window, and charts
   — but not on full-screen / chat-only windows, where rounded
   corners would reveal the page behind them.
"""
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "app/index.html"


def test_hide_dashboard_toggle_present():
    html = HTML.read_text()
    for marker in ('id="dockedhidebtn"', "toggleChatOnly",
                   "body.chat-only", "Hide dashboard", "Show dashboard",
                   "editQuestionsFromDocked"):
        assert marker in html, f"missing hide-dashboard piece: {marker}"
    # Hidden, not deleted: the dashboard sections are still in the HTML.
    for marker in ("Product health — weather report",
                   "Phase 3 chat — multi-turn assistant",
                   "Questions editor — chat flow + evals"):
        assert marker in html, f"dashboard section deleted, not hidden: {marker}"


def test_chat_only_is_default_on_load():
    """Chat-only default (2026-10-29): the user said the dashboard
    background is not needed, so the page must load chat-only, not
    with the dashboard visible behind a docked chat. The dashboard
    is still in the HTML (hidden by CSS), and the header button
    starts as "Show dashboard" because it is already hidden.
    """
    html = HTML.read_text()
    assert '<body class="chat-only">' in html
    assert '>Show dashboard</button>' in html


def test_no_grey_placeholder_question_in_chat_inputs():
    html = HTML.read_text()
    assert 'placeholder="What\'s happening with Instagram?"' not in html
    assert 'id="dockedq"' in html and 'id="chatq"' in html


def test_rounded_corners():
    html = HTML.read_text()
    assert "border-radius:8px" in html  # buttons and text inputs
    assert "border-radius:16px 16px 0 0" in html  # bottom-docked window
    assert "border-radius:12px" in html  # charts
    fullscreen_block = html.split("#docked.fullscreen{")[1].split("}")[0]
    assert "border-radius:0" in fullscreen_block
