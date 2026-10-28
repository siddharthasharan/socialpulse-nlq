"""Regression test for the bottom-docked chat window (2026-10-28).

The user asked that everything on the dashboard remain, and that the
bottom show a chat window like a Muse/ChatGPT input bar, with the
conversation continuing and allowing select (option chips) and update
(corrections / Edit questions) as needed. So this test asserts both
halves: every pre-existing section is still present, and the docked
window (with its shared-session wiring) is present too.
"""
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "app/index.html"


def test_everything_remains():
    html = HTML.read_text()
    for marker in ("Product health — weather report", "Ask a question",
                   "Phase 3 chat — multi-turn assistant",
                   "Questions editor — chat flow + evals",
                   "Experiments (registry only in Phase 2)",
                   "Catalog metrics"):
        assert marker in html, f"missing dashboard section: {marker}"


def test_docked_chat_window_present():
    html = HTML.read_text()
    for marker in ('id="docked"', 'id="dockedlog"', 'id="dockedq"',
                   "sendDockedMsg", "toggleDocked", "newDockedChat",
                   "chatHistory", "data-docked-send",
                   "Edit questions", "Same session as the chat section above"):
        assert marker in html, f"missing docked chat piece: {marker}"
    # The docked window must not replace the in-page chat: both senders
    # exist, sharing one session id.
    assert "sendChatMsg" in html and "chatSessionId" in html
