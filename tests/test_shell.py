"""Unified shell (slice 1) static checks.

These check the shell page and route wiring without starting servers
or touching the 500K data: the shell must exist, name both engines and
their separation honestly, use the Copilot palette, persist only to
localStorage, and carry the single-dataset extension point. The live
verification (both backends, both modes) is a manual/local-server step
documented in docs/shell.md, not a unit test.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = (ROOT / "app/shell.html").read_text()
APP = (ROOT / "scripts/app.py").read_text()
COPILOT = (Path(__file__).resolve().parents[2] / "experiment-copilot"
           / "copilot_platform" / "server.py").read_text()


def test_shell_page_exists_and_is_single_file():
    assert (ROOT / "app/shell.html").exists()
    assert "<script" in SHELL
    assert 'src="http' not in SHELL and "cdn" not in SHELL.lower()


def test_shell_has_sidebar_chatlist_mode_indicator_composer():
    for token in ("chatlist", "newAnalytics", "newExperiments", "modeBadge",
                  "modeEngine", "transcript", "msgInput", "sendBtn",
                  "artifact", "attachBtn"):
        assert token in SHELL, token


def test_shell_palette_and_localstorage_only():
    for color in ("#6d28d9", "#ede9fe", "#1f2937", "#6b7280", "#e5e7eb", "#f9fafb"):
        assert color in SHELL, color
    assert "portfolio-shell-conversations-v1" in SHELL
    assert "localStorage" in SHELL
    assert "indexedDB" not in SHELL


def test_shell_honesty_no_shared_data_or_live_llm_claim():
    assert "do <b>not</b> share data" in SHELL or "do not share data" in SHELL
    assert "No live LLM" in SHELL
    assert "never mixes" in SHELL or "never computes a lift" in SHELL
    for bad in ("shared session", "unified dataset", "live LLM calculates"):
        assert bad not in SHELL, bad


def test_shell_mode_switch_starts_new_conversation():
    # The switch control creates a new conversation; it must not rewrite
    # an existing conversation's mode in place.
    assert "New chat in the other mode" in SHELL
    assert "newConversation(c && c.mode" in SHELL


def test_shell_dataset_extension_point_is_single_dataset():
    assert 'SHELL_DATASETS = ["social"]' in SHELL
    assert "Marketplace" in SHELL or "docs/shell.md" in SHELL


def test_app_routes_shell_without_touching_existing():
    assert '"/shell"' in APP
    assert "/api/shell-config" in APP
    assert '"/", "/index.html"' in APP  # existing dashboard route intact
    assert '"/api/chat"' in APP


def test_copilot_cors_is_narrow_and_preflight_exists():
    assert "socialpulse-nlq.onrender.com" in COPILOT
    assert "localhost:" in COPILOT
    assert "def do_OPTIONS" in COPILOT
    assert "Access-Control-Allow-Origin" in COPILOT
    assert "Access-Control-Allow-Methods" in COPILOT
    # No wildcard origin: the allowlist must stay narrow.
    assert 'Access-Control-Allow-Origin", "*"' not in COPILOT
