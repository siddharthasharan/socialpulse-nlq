"""Tests for the both-in-one questions store (flow + evals)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from socialpulse_nlq.questions import (
    default_flow, load_flow, save_questions, validate_benchmark,
    validate_conversation, validate_flow)


def test_default_flow_matches_shipped_file(tmp_path):
    # The shipped chat/questions.json must be the defaults, so the
    # editor's first load is the chat's actual behaviour, not a copy.
    root = Path(__file__).resolve().parents[1]
    shipped = json.loads((root / "chat" / "questions.json").read_text())
    assert shipped == default_flow()


def test_load_flow_missing_file_is_defaults(tmp_path):
    assert load_flow(tmp_path) == default_flow()


def test_load_flow_partial_file_merges_over_defaults(tmp_path):
    (tmp_path / "chat").mkdir()
    (tmp_path / "chat" / "questions.json").write_text(json.dumps({
        "topics": {"creators": {"label": "Edited creators label", "cards": ["daily_creators"]}},
        "recommendations": {},
    }))
    flow = load_flow(tmp_path)
    assert flow["topics"]["creators"]["label"] == "Edited creators label"
    # Missing recommendations in a partial file must not delete topics.
    assert flow["topics"]["growth"]["label"] == default_flow()["topics"]["growth"]["label"]
    assert flow["recommendations"]["creators"]["hypothesis"]


def test_save_flow_rejects_bad_guardrails_and_writes_nothing(tmp_path):
    # Fresh root (pytest gives each test its own tmp_path; the manual
    # runner above reuses one, so use a subdir to stay independent).
    root = tmp_path / "reject"
    bad = default_flow()
    bad["recommendations"]["creators"]["guardrails"] = "not-a-list"
    ok, _ = validate_flow(bad)
    assert not ok
    try:
        save_questions(root, "flow", bad)
        raise AssertionError("bad flow should not save")
    except ValueError:
        pass
    assert not (root / "chat" / "questions.json").exists()


def test_save_flow_round_trips(tmp_path):
    flow = default_flow()
    flow["topics"]["creators"]["label"] = "Creators (edited)"
    path = save_questions(tmp_path, "flow", flow)
    assert path.exists()
    assert load_flow(tmp_path)["topics"]["creators"]["label"] == "Creators (edited)"


def test_validate_benchmark_rejects_duplicate_ids():
    cases = [{"id": "b1", "question": "Q?", "expected_type": "scalar"},
             {"id": "b1", "question": "Q2?", "expected_type": "scalar"}]
    assert not validate_benchmark(cases)[0]


def test_validate_conversation_rejects_empty_turn():
    convs = [{"id": "c1", "turns": [{"user": "", "expect_type": "weather"}]}]
    assert not validate_conversation(convs)[0]
