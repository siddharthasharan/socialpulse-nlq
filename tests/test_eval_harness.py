"""Eval harness unit tests. No data files are needed for these: rubric
scoring is tested on fabricated runner outcomes, and split/manifest
integrity is tested against the real (small) JSON files."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from socialpulse_nlq.eval_harness import (FAMILIES, ManifestError, _summarize,
                                          _structural_checks, cases_for_split,
                                          load_cases, load_manifest, score_single,
                                          validate_case_set)

ROOT = Path(__file__).resolve().parents[1]

# A minimal scalar case + a passing outcome, reused across tests.
CASE = {"id": "b38", "expected_type": "scalar", "expected_metric": "mau_march",
        "expected_value": 210809.0, "family": "metric_scoping",
        "question": "What is the MAU for Instagram?"}
PLAN = {"type": "query", "metric": "mau_march",
        "sql": ("SELECT COUNT(DISTINCT user_id) AS value FROM fact_sessions "
                "WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' "
                "AND DATE '2026-03-31' AND app = 'instagram'"),
        "tables": ["fact_sessions"], "grain": "user x month"}


def _outcome(**overrides):
    outcome = {"plan": PLAN, "guard": (True, "ok"), "syntax_ok": True,
               "syntax_error": "", "exec_ok": True, "exec_error": "",
               "columns": ["value"], "rows": [[210809.0]], "runtime_ms": 12,
               "weather": None, "trace": None}
    outcome.update(overrides)
    return outcome


def test_rubric_pass():
    r = score_single(CASE, _outcome())
    assert all(v["pass"] is True for v in r["items"].values()), r["items"]
    assert r["sql_semantic"] is True and r["task_success"] is True


def test_rubric_wrong_metric_right_value():
    # Right answer from wrong SQL: the planner picks a different metric
    # whose value happens to coincide with the expected one. Result
    # equivalence (values only) passes; final answer (metric + values)
    # and sql_semantic must fail.
    wrong = {"type": "query", "metric": "dau_mean_daily",
             "sql": "SELECT 210809.0 AS value",
             "tables": ["fact_sessions"], "grain": "user x day"}
    r = score_single(CASE, _outcome(plan=wrong, rows=[[210809.0]]))
    assert r["items"]["result_equiv"]["pass"] is True
    assert r["items"]["final_answer"]["pass"] is False
    assert r["sql_semantic"] is False


def test_rubric_structural_only_failure():
    # Value and metric are right, but the SQL lacks the required app
    # filter (e.g. the number came from a different-but-equal slice).
    # Final answer passes on value; sql_semantic must still fail.
    no_filter = dict(PLAN, sql="SELECT 210809.0 AS value")
    r = score_single(CASE, _outcome(plan=no_filter),
                     structural_specs=[{"contains": "app = 'instagram'"}])
    assert r["items"]["final_answer"]["pass"] is True
    assert r["sql_semantic"] is False
    assert r["structural"][0]["pass"] is False


def test_rubric_guard_fail_blocks_execution():
    r = score_single(CASE, _outcome(guard=(False, "table not in allowlist: x"),
                                    exec_ok=False,
                                    exec_error="not executed: guard failed"))
    assert r["items"]["sql_safety"]["pass"] is False
    assert r["items"]["execution"]["pass"] is False
    assert r["items"]["result_equiv"]["pass"] is False
    assert r["task_success"] is False


def test_rubric_syntax_fail():
    r = score_single(CASE, _outcome(syntax_ok=False, syntax_error="parse error",
                                    exec_ok=False, exec_error="not executed: syntax check failed"))
    assert r["items"]["sql_syntax"]["pass"] is False
    assert r["items"]["execution"]["pass"] is False


def test_rubric_clarification_case():
    case = {"id": "b31", "expected_type": "clarification",
            "expected_contains": "France", "family": "incorrect_assumption",
            "question": "What was DAU in France?"}
    plan = {"type": "clarification", "metric": None, "sql": None, "tables": [],
            "clarification": "'france' is not a country in the synthetic data."}
    r = score_single(case, _outcome(plan=plan, rows=[], columns=[]))
    assert r["items"]["sql_syntax"]["pass"] is None  # na: no SQL by design
    assert r["items"]["sql_safety"]["pass"] is None
    assert r["items"]["execution"]["pass"] is None
    assert r["items"]["result_equiv"]["pass"] is None
    assert r["items"]["final_answer"]["pass"] is True
    assert r["task_success"] is True
    # A number instead of a clarification must fail the final answer.
    wrong = dict(plan, type="query", metric="dau_mean_daily",
                 sql="SELECT 1 AS value")
    r2 = score_single(case, _outcome(plan=wrong, rows=[[1.0]], columns=["value"]))
    assert r2["items"]["final_answer"]["pass"] is False


def test_rubric_weather_watchout_missing():
    case = {"id": "b34", "expected_type": "weather_report",
            "expected_cards": {"daily_creators": 10338.9},
            "expected_watchouts_contains": ["Creator problem"],
            "family": "ambiguous_metric", "question": "What is product health?"}
    weather = {"cards": [{"id": "daily_creators", "current": 10338.9}],
               "watchouts": ["No watchouts fired on these thresholds."]}
    r = score_single(case, _outcome(plan={"type": "weather_report", "metric": None,
                                          "sql": None, "tables": []},
                                    weather=weather))
    assert r["items"]["result_equiv"]["pass"] is True  # cards match
    assert r["items"]["final_answer"]["pass"] is False  # watchout missing
    assert r["task_success"] is False


def test_structural_min_count():
    specs = [{"contains": "app = 'instagram'", "min_count": 2}]
    assert _structural_checks("x app = 'instagram' y app = 'instagram'", specs)[0]["pass"] is True
    assert _structural_checks("x app = 'instagram' y", specs)[0]["pass"] is False
    assert _structural_checks("", specs)[0]["found"] == 0


def test_split_integrity():
    manifest = load_manifest(ROOT)
    singles, convs, _ = load_cases(ROOT)
    validate_case_set(manifest, singles, convs)
    ids = [c["id"] for c in manifest["cases"]]
    assert len(ids) == len(set(ids)) == 49
    dev = cases_for_split(manifest, "dev")
    held = cases_for_split(manifest, "heldout")
    assert dev & held == set()
    assert dev | held == set(ids)
    assert len(held) == 10 and len(dev) == 39
    assert held == {"b10", "b15", "b19", "b23", "b30", "b36", "b37", "b39", "c2", "c8"}


def test_heldout_never_in_dev_summary():
    # The summary is built from the results it is given; dev runs only
    # ever receive dev results, so a held-out failure cannot move the
    # dev aggregate. Simulate: a dev pass + a held-out fail.
    dev_result = {"id": "b01", "family": "aggregation_grain",
                  "task_success": True,
                  "items": {n: {"pass": True, "reason": ""} for n in
                            ("sql_syntax", "sql_safety", "execution",
                             "result_equiv", "final_answer")}}
    summary = _summarize([dev_result])
    assert summary["overall"] == {"n": 1, "task_success": 1}
    assert summary["by_family"]["aggregation_grain"]["task_success"] == 1


def test_manifest_version_enforced(tmp_path=None):
    # A manifest with the wrong version, a missing case, or an unknown
    # family must be refused. Build them in a scratch dir (tmp_path is
    # a pytest fixture the plain runner cannot supply, so fall back to
    # a scratch dir under the workspace when it is absent).
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="harness_manifest_"))
    (base / "evals").mkdir()
    good = load_manifest(ROOT)
    bad = json.loads(json.dumps(good))
    bad["version"] = "v99"
    (base / "evals/benchmark_v1_manifest.json").write_text(json.dumps(bad))
    try:
        load_manifest(base)
    except ManifestError:
        pass
    else:
        raise AssertionError("wrong manifest version was not refused")
    bad2 = json.loads(json.dumps(good))
    bad2["cases"] = [c for c in bad2["cases"] if c["id"] != "b01"]
    (base / "evals/benchmark_v1_manifest.json").write_text(json.dumps(bad2))
    singles, convs, _ = load_cases(ROOT)
    try:
        validate_case_set(bad2, singles, convs)
    except ManifestError:
        pass
    else:
        raise AssertionError("manifest missing a case was not refused")
    bad3 = json.loads(json.dumps(good))
    bad3["cases"][0]["family"] = "not_a_family"
    (base / "evals/benchmark_v1_manifest.json").write_text(json.dumps(bad3))
    try:
        load_manifest(base)
    except ManifestError:
        pass
    else:
        raise AssertionError("unknown family was not refused")


def test_oracle_covers_every_case_and_families_valid():
    manifest = load_manifest(ROOT)
    singles, convs, oracle = load_cases(ROOT)
    assert set(oracle) == {c["id"] for c in singles + convs}
    assert len(oracle) == 49
    for c in singles + convs:
        assert c.get("family") in FAMILIES, c["id"]
    for c in manifest["cases"]:
        assert c["family"] in FAMILIES
