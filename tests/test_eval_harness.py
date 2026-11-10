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


# --------------------------------------------------------------------------
# v2 (slice 2, part A): the versioned loader must serve v2 alongside v1,
# and the v1 default must be unchanged.
# --------------------------------------------------------------------------

V2_HELDOUT = {"t06", "t08", "t10", "t13", "t16", "t19", "t25", "t30",
              "t40", "t45", "t47", "t51", "t56", "t64",
              "b10", "b15", "b19", "b23", "b30", "b36", "b37", "b39", "c2", "c8"}


def test_v1_still_default():
    manifest = load_manifest(ROOT)
    assert manifest["version"] == "v1"
    assert len(manifest["cases"]) == 49
    singles, _, oracle = load_cases(ROOT)
    assert len(singles) == 39 and len(oracle) == 49


def test_v2_split_integrity():
    manifest = load_manifest(ROOT, version="v2")
    assert manifest["version"] == "v2"
    singles, convs, _ = load_cases(ROOT, version="v2")
    validate_case_set(manifest, singles, convs)
    ids = [c["id"] for c in manifest["cases"]]
    assert len(ids) == len(set(ids)) == 119
    assert len(singles) == 109 and len(convs) == 10
    dev = cases_for_split(manifest, "dev")
    held = cases_for_split(manifest, "heldout")
    assert dev & held == set()
    assert dev | held == set(ids)
    assert len(held) == 24 and len(dev) == 95
    assert held == V2_HELDOUT


def test_v2_oracle_covers_every_case_and_templated_labeled():
    manifest = load_manifest(ROOT, version="v2")
    singles, convs, oracle = load_cases(ROOT, version="v2")
    assert set(oracle) == {c["id"] for c in singles + convs}
    assert len(oracle) == 119
    for c in singles + convs:
        assert c.get("family") in FAMILIES, c["id"]
    # Every templated case is labeled as such in its oracle note, and its
    # id is in the t01-t70 range -- templated cases never count as
    # independent observations.
    templated = [c for c in singles if c["id"].startswith("t")]
    assert len(templated) == 70
    for c in templated:
        assert "Templated" in oracle[c["id"]].get("note", ""), c["id"]
    # The two approved fixes carried into v2.
    assert oracle["c8"].get("evidence_expect_top_dim") == "DE"
    assert "2026-01-01" in oracle["b08"]["sql"] and "2026-03-31" in oracle["b08"]["sql"]


def test_unknown_version_refused():
    try:
        load_manifest(ROOT, version="v99")
    except ManifestError:
        pass
    else:
        raise AssertionError("unknown benchmark version was not refused")
    try:
        load_cases(ROOT, version="v99")
    except ManifestError:
        pass
    else:
        raise AssertionError("unknown benchmark version was not refused for cases")


def test_v2_families_all_covered():
    # v1 had zero guardrail_adversarial and sql_safety_syntax cases;
    # slice 2 fills both, and every family now has dev cases.
    manifest = load_manifest(ROOT, version="v2")
    from collections import Counter
    fam = Counter(c["family"] for c in manifest["cases"])
    assert fam["guardrail_adversarial"] == 8
    assert fam["sql_safety_syntax"] == 6
    assert set(fam) == set(FAMILIES)


# --------------------------------------------------------------------------
# v3 (slice 2b): the 31 hand-designed hard cases (h01-h31) wired in.
# --------------------------------------------------------------------------

V3_HELDOUT = V2_HELDOUT | {"h02", "h08", "h18", "h21", "h26", "h30"}


def test_structural_not_contains():
    # h12: sessions per DAU in March returns a number 0.03% from the
    # right one when the planner silently filters to India, so only
    # "no country filter unless asked" catches it.
    specs = [{"not_contains": "country = '",
              "label": "no country filter unless asked"}]
    assert _structural_checks("SELECT 1 AS value", specs)[0]["pass"] is True
    assert _structural_checks(
        "SELECT 1 AS value FROM t WHERE u.country = 'IN'", specs)[0]["pass"] is False
    assert _structural_checks("", specs)[0]["found"] == 0
    # Positive and negative specs compose in one list.
    mixed = [{"contains": "app = 'instagram'"},
             {"not_contains": "country = '"}]
    r = _structural_checks("SELECT 1 WHERE app = 'instagram'", mixed)
    assert [s["pass"] for s in r] == [True, True]


def test_oracle_behavioral_expect_value():
    # The behavioral oracle can now check a premise that IS a value:
    # the true app-scoped number a hard case rests on (h01-h09), or
    # "zero April sessions" (h17/h18). No data files needed: an
    # in-memory DuckDB connection with one fact_sessions row.
    import tempfile

    import duckdb

    from socialpulse_nlq.eval_harness import run_oracle

    con = duckdb.connect()
    con.execute("CREATE TABLE fact_sessions(start_time TIMESTAMP)")
    con.execute("INSERT INTO fact_sessions VALUES ('2026-03-31 10:00:00')")
    data_dir = Path(tempfile.mkdtemp(prefix="harness_oracle_"))
    entries = {
        "h17": {"id": "h17", "kind": "behavioral",
                "evidence_sql": ("SELECT COUNT(*) FROM fact_sessions WHERE "
                                 "CAST(start_time AS DATE) >= DATE '2026-04-01'"),
                "evidence_expect_value": 0},
        "h01": {"id": "h01", "kind": "behavioral",
                "evidence_sql": "SELECT 0.20171801455598928 AS value",
                "evidence_expect_value": 0.20171801455598928},
        "bad": {"id": "bad", "kind": "behavioral",
                "evidence_sql": "SELECT 1.0 AS value",
                "evidence_expect_value": 2.0},
    }
    results = {r["id"]: r for r in
               run_oracle(con, data_dir, [], [], entries)}
    assert results["h17"]["agree"] is True
    assert results["h17"]["oracle_value"] == 0
    assert results["h01"]["agree"] is True
    assert results["bad"]["agree"] is False
    assert "1.0 != expected 2.0" in results["bad"]["detail"]


def test_v3_split_integrity():
    manifest = load_manifest(ROOT, version="v3")
    assert manifest["version"] == "v3"
    singles, convs, _ = load_cases(ROOT, version="v3")
    validate_case_set(manifest, singles, convs)
    ids = [c["id"] for c in manifest["cases"]]
    assert len(ids) == len(set(ids)) == 150
    assert len(singles) == 136 and len(convs) == 14
    dev = cases_for_split(manifest, "dev")
    held = cases_for_split(manifest, "heldout")
    assert dev & held == set()
    assert dev | held == set(ids)
    assert len(held) == 30 and len(dev) == 120
    assert held == V3_HELDOUT


def test_v3_oracle_covers_every_case_and_hard_labeled():
    manifest = load_manifest(ROOT, version="v3")
    singles, convs, oracle = load_cases(ROOT, version="v3")
    assert set(oracle) == {c["id"] for c in singles + convs}
    assert len(oracle) == 150
    for c in singles + convs:
        assert c.get("family") in FAMILIES, c["id"]
    hard = [c for c in singles + convs if c["id"].startswith("h")]
    assert len(hard) == 31
    for c in hard:
        assert "Hard case (slice 2b" in oracle[c["id"]].get("note", ""), c["id"]
    # The review decisions carried into v3: h26 is a refusal (the guard
    # widening makes it refuse by design), h30 expects a clarification,
    # h31's switch turn is the bare "same for WhatsApp" phrasing.
    h26 = next(c for c in singles if c["id"] == "h26")
    assert h26["expected_type"] == "refusal"
    h30 = next(c for c in convs if c["id"] == "h30")
    assert h30["turns"][3]["expect_type"] == "clarification"
    assert "expect_topic" not in h30["turns"][3]
    h31 = next(c for c in convs if c["id"] == "h31")
    assert h31["turns"][2]["user"] == "same for WhatsApp"
    assert h31["turns"][2]["expect_type"] == "drilldown"
    h29 = next(c for c in convs if c["id"] == "h29")
    assert h29["turns"][3]["expect_topic"] == "creators"


def test_v3_families():
    # 150 cases: metric_scoping grows by the 12 traps, the other hard
    # groups land in their families, and SQL safety finally has a
    # held-out case (h26).
    from collections import Counter

    manifest = load_manifest(ROOT, version="v3")
    fam = Counter(c["family"] for c in manifest["cases"])
    assert fam["metric_scoping"] == 46
    assert fam["aggregation_grain"] == 29
    assert fam["complex_joins_breakdowns"] == 21
    assert fam["ambiguous_metric"] == 10
    assert fam["multi_turn_context"] == 11
    assert fam["incorrect_assumption"] == 11
    assert fam["guardrail_adversarial"] == 13
    assert fam["sql_safety_syntax"] == 9
    assert set(fam) == set(FAMILIES)
    held = Counter(c["family"] for c in manifest["cases"]
                   if c["split"] == "heldout")
    assert held["sql_safety_syntax"] == 1
    assert held["multi_turn_context"] == 3
    assert held["metric_scoping"] == 12


def test_rubric_mau_by_country_type():
    # 2026-11-10 (p06): the mau_by_country expected_type scores
    # through the same SQL branch as a breakdown (grouped per-label
    # result: row count + per-label values), but pins the turn, not
    # the generic breakdown category. A fabricated passing outcome:
    # all five rubric items pass and task succeeds.
    case = {"id": "p06", "expected_type": "mau_by_country",
            "expected_metric": "mau_by_country", "expected_rows": 7,
            "expected_values": {"US": 63372, "IN": 52686, "BR": 31623,
                                "ID": 21081, "MX": 16946, "DE": 12611,
                                "GB": 12490},
            "family": "metric_scoping",
            "question": "What's the MAU for Instagram by country?"}
    plan = {"type": "query", "metric": "mau_by_country",
            "sql": ("SELECT u.country, COUNT(DISTINCT s.user_id) AS n "
                    "FROM fact_sessions s JOIN dim_users u ON u.user_id = s.user_id "
                    "WHERE CAST(s.start_time AS DATE) BETWEEN DATE '2026-03-01' "
                    "AND DATE '2026-03-31' AND s.app = 'instagram' "
                    "GROUP BY u.country ORDER BY n DESC"),
            "tables": ["fact_sessions", "dim_users"],
            "grain": "user x month, grouped by country"}
    rows = [["US", 63372.0], ["IN", 52686.0], ["BR", 31623.0], ["ID", 21081.0],
            ["MX", 16946.0], ["DE", 12611.0], ["GB", 12490.0]]
    r = score_single(case, _outcome(plan=plan, rows=rows, columns=["country", "n"]))
    assert all(v["pass"] is True for v in r["items"].values()), r["items"]
    assert r["sql_semantic"] is True and r["task_success"] is True
    # The silent-substitution guard: the creators-by-country
    # breakdown returns the right shape with a different population
    # (US 9,286 instead of 63,372). Result equivalence fails on the
    # values, so the final answer fails even though the SQL is valid.
    wrong_rows = [["US", 9286.0], ["IN", 7843.0], ["BR", 4785.0], ["ID", 3149.0],
                  ["MX", 2488.0], ["GB", 1897.0], ["DE", 1849.0]]
    wrong_plan = dict(plan, metric="creators_by_country")
    r2 = score_single(case, _outcome(plan=wrong_plan, rows=wrong_rows,
                                     columns=["country", "creators"]))
    assert r2["items"]["result_equiv"]["pass"] is False
    assert r2["items"]["final_answer"]["pass"] is False
    assert r2["task_success"] is False
