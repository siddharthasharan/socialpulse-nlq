"""Benchmark harness for the SocialPulse evaluation study (v1 and v2).

Runs a frozen benchmark version (evals/benchmark_v1_manifest.json or
evals/benchmark_v2_manifest.json) against the deterministic planner/chat
and scores every case on the FIVE rubric items from docs/phase1-design —
separately, never collapsed into one number:

  1. sql_syntax      — the planned SQL parses (DuckDB EXPLAIN, no execution).
  2. sql_safety      — the read-only guard accepts the SQL, reason recorded.
  3. execution       — the SQL (or weather report) runs without error.
  4. result_equiv    — executed rows match ground truth (same values; row
                       order only matters where the question asks for a
                       ranking).
  5. final_answer    — the answer shown matches the expected answer: the
                       right metric AND the right values for a query, the
                       right cards + watchouts for a weather report, the
                       right clarification/refusal for an unanswerable one.

NO exact-SQL-string matching anywhere. The only place SQL text is ever
inspected is a structural check declared per case in evals/oracle_v1.json
(e.g. the MAU case must contain the app filter) — a contains-check on the
plan, not a string comparison against a gold query. The distinction
between items 4 and 5 is deliberate: a wrong-metric query that happens to
return the right number scores result_equiv pass but final_answer fail,
and sql_semantic (recorded alongside the five items: metric identity +
structural checks) fails. That is the right-answer-from-wrong-SQL case,
and it must never pass silently.

Each rubric item is pass / fail / na ("not applicable": a clarification
correctly has no SQL to check). Item rates are reported over applicable
cases only. Task success for a case is final_answer pass — the correct
answer when answerable, the correct clarification/refusal when not.

Results are reported BY the 8 failure families from the design draft, and
the family (not the individual case) is the unit of uncertainty: no naive
confidence interval over all cases is ever computed here. Dev and held-out
are reported in separate tables and never mixed into one aggregate.

Per-case traces are recorded as JSON: catalog entries used (metric +
definition), plan type, SQL, guard outcome + reason, syntax check,
execution time, row counts, errors, the five rubric scores, structural
checks, and (for conversations) every turn. The trace reuses the existing
working payload (planner.build_trace / chat last_working), it does not
invent a second trace format.

The harness refuses to run (exit non-zero, no results) when the frozen
state disagrees: manifest version != the requested version, a case id
missing from or duplicated in either side, a family tag outside the 8,
or the data pin (users / table counts / max session date) not matching
the data dir.
"""
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # src/socialpulse_nlq/ -> repo root
sys.path.insert(0, str(ROOT / "src"))

from socialpulse_nlq.planner import (ALLOWED_TABLES, build_trace, guard_sql,
                                     metric_definition, plan, run_plan)
from socialpulse_nlq.weather import compute_weather

FAMILIES = ["metric_scoping", "ambiguous_metric", "multi_turn_context",
            "complex_joins_breakdowns", "incorrect_assumption",
            "guardrail_adversarial", "sql_safety_syntax", "aggregation_grain"]
MANIFEST_VERSION = "v1"
# One frozen file set per benchmark version. v2 reuses the v1 conversations
# file (it already carries the corrected c8 description); only the singles,
# oracle, and manifest are versioned. Default stays v1 so existing callers
# (and the v1 runner) keep running the frozen baseline unchanged.
VERSIONS = {
    "v1": {"manifest": "benchmark_v1_manifest.json",
           "singles": "benchmark_phase2.json",
           "convs": "conversation_phase3.json",
           "oracle": "oracle_v1.json"},
    "v2": {"manifest": "benchmark_v2_manifest.json",
           "singles": "benchmark_v2.json",
           "convs": "conversation_phase3.json",
           "oracle": "oracle_v2.json"},
    # v3 (slice 2b): v2's 119 cases plus the 31 hand-designed hard cases
    # (h01-h31). New conversations file: conversation_phase3.json is
    # shared by v1/v2 and frozen, so v3 has its own copy plus h28-h31.
    "v3": {"manifest": "benchmark_v3_manifest.json",
           "singles": "benchmark_v3.json",
           "convs": "conversation_v3.json",
           "oracle": "oracle_v3.json"},
    # v4 (daily-series turn, 2026-11-06): v3's 150 cases plus the two
    # user probes p01 (MAU trend, single) and p02 (DAU by day,
    # conversation). Both in dev; held-out stays 30.
    "v4": {"manifest": "benchmark_v4_manifest.json",
           "singles": "benchmark_v4.json",
           "convs": "conversation_v4.json",
           "oracle": "oracle_v4.json"},
    # v5 (MAU-to-breakdown wiring, 2026-11-09): v4's 153 cases plus
    # the user probe p04 (MAU -> "do you have this data by surface",
    # conversation). In dev; held-out stays 30.
    "v5": {"manifest": "benchmark_v5_manifest.json",
           "singles": "benchmark_v5.json",
           "convs": "conversation_v5.json",
           "oracle": "oracle_v5.json"},
}


class ManifestError(Exception):
    """Frozen state disagrees with the eval files or the data. Refuse to run."""


def close(actual, expected):
    """Same tolerance as scripts/run_benchmark.py: 2% relative or 0.01
    absolute, whichever is larger."""
    return abs(actual - expected) <= max(0.02 * abs(expected), 0.01)


def _item(passed, reason=""):
    return {"pass": bool(passed), "reason": reason}


def _na(reason):
    return {"pass": None, "reason": reason}


# --------------------------------------------------------------------------
# Loading + frozen-state validation
# --------------------------------------------------------------------------

def load_manifest(root=ROOT, version=MANIFEST_VERSION):
    if version not in VERSIONS:
        raise ManifestError(f"unknown benchmark version {version!r}; known: "
                            f"{sorted(VERSIONS)}")
    path = Path(root) / "evals" / VERSIONS[version]["manifest"]
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ManifestError(f"cannot read manifest {path}: {e}")
    if manifest.get("version") != version:
        raise ManifestError(
            f"manifest version {manifest.get('version')!r} != {version!r}; "
            "the harness only runs the frozen benchmark for that version")
    entries = manifest.get("cases", [])
    ids = [c["id"] for c in entries]
    if len(ids) != len(set(ids)):
        raise ManifestError("manifest lists a case id more than once")
    for c in entries:
        if c.get("family") not in FAMILIES:
            raise ManifestError(f"case {c['id']}: family {c.get('family')!r} is not one of the 8")
        if c.get("split") not in ("dev", "heldout"):
            raise ManifestError(f"case {c['id']}: split {c.get('split')!r} is not dev/heldout")
    if not any(c["split"] == "heldout" for c in entries):
        raise ManifestError("manifest has no held-out cases; refusing to run")
    return manifest


def load_cases(root=ROOT, version=MANIFEST_VERSION):
    """Return (single-shot cases, conversations, oracle entries by id)."""
    if version not in VERSIONS:
        raise ManifestError(f"unknown benchmark version {version!r}; known: "
                            f"{sorted(VERSIONS)}")
    files = VERSIONS[version]
    singles = json.loads((Path(root) / "evals" / files["singles"]).read_text())
    convs = json.loads((Path(root) / "evals" / files["convs"]).read_text())
    oracle = json.loads((Path(root) / "evals" / files["oracle"]).read_text())
    if oracle.get("version") != version:
        raise ManifestError(
            f"oracle version {oracle.get('version')!r} != {version!r}")
    return singles, convs, {e["id"]: e for e in oracle["entries"]}


def validate_case_set(manifest, singles, convs):
    """Manifest and eval files must list exactly the same case ids."""
    manifest_ids = {c["id"] for c in manifest["cases"]}
    file_ids = {c["id"] for c in singles + convs}
    if manifest_ids != file_ids:
        missing = sorted(file_ids - manifest_ids)
        extra = sorted(manifest_ids - file_ids)
        raise ManifestError(
            f"manifest and eval files disagree: missing from manifest {missing}, "
            f"in manifest but not in eval files {extra}")
    for c in singles + convs:
        tag = manifest_ids and next(
            m["family"] for m in manifest["cases"] if m["id"] == c["id"])
        if c.get("family") != tag:
            raise ManifestError(
                f"case {c['id']}: family tag in eval file ({c.get('family')!r}) "
                f"!= manifest ({tag!r})")


def open_connection(data_dir):
    """One DuckDB connection with a view per allowlisted table."""
    import duckdb

    con = duckdb.connect()
    for t in sorted(ALLOWED_TABLES):
        p = Path(data_dir) / f"{t}.parquet"
        if p.exists():
            con.execute(f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM read_parquet('{p}')")
    return con


def validate_data_pin(con, data_dir, manifest):
    """The data dir must match the manifest pin, or refuse to run."""
    pin = manifest["data"]
    problems = []
    if not (Path(data_dir) / "dim_users.parquet").exists():
        raise ManifestError(f"data not found at {data_dir}")
    counts = {}
    for table, expected in pin["counts"].items():
        try:
            got = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception as e:  # noqa: BLE001 - any failure is a pin mismatch
            problems.append(f"{table}: cannot count ({e})")
            continue
        counts[table] = got
        if got != expected:
            problems.append(f"{table}: {got} rows, manifest pins {expected}")
    max_date = con.execute(
        "SELECT MAX(CAST(start_time AS DATE)) FROM fact_sessions").fetchone()[0]
    if str(max_date) != pin["max_session_date"]:
        problems.append(
            f"max session date {max_date}, manifest pins {pin['max_session_date']}")
    if problems:
        raise ManifestError("data pin mismatch: " + "; ".join(problems))
    return counts


# --------------------------------------------------------------------------
# Rubric scoring (pure functions over a runner-produced outcome — unit-testable)
# --------------------------------------------------------------------------

def _structural_checks(sql, specs):
    """Contains-checks on the planned SQL, never a string comparison
    against a gold query. Each spec: {"contains": s} (optionally with
    "min_count") or {"not_contains": s} -- the SQL must NOT contain s.
    The negative form is for wrong-scope traps where the value alone
    cannot catch the error: h12 (sessions per DAU in March) returns a
    number 0.03% from the right one when the planner silently filters
    to India, so only "no country filter unless asked" catches it."""
    out = []
    for spec in specs or []:
        if "not_contains" in spec:
            needle = spec["not_contains"]
            n = (sql or "").count(needle)
            out.append({"label": spec.get("label", f"SQL must not contain {needle!r}"),
                        "pass": n == 0, "found": n, "want": 0})
            continue
        needle = spec["contains"]
        n = (sql or "").count(needle)
        want = spec.get("min_count", 1)
        out.append({"label": spec.get("label", f"SQL contains {needle!r}"),
                    "pass": n >= want,
                    "found": n, "want": want})
    return out


def _result_equiv_single(case, rows, cols):
    """Compare executed rows to the stored expectation. Returns
    (pass, reason). Row order is ignored except for ranking questions
    (no single-shot ranking case exists in v1)."""
    etype = case["expected_type"]
    if etype == "scalar":
        if len(rows) != 1 or len(cols) != 1:
            return False, f"expected 1x1, got {len(rows)} rows x {len(cols)} cols"
        if "expected_value" in case:
            if not close(float(rows[0][0]), case["expected_value"]):
                return False, f"value {rows[0][0]} != {case['expected_value']}"
        return True, ""
    if etype == "series":
        if len(rows) < case.get("min_rows", 0):
            return False, f"only {len(rows)} rows, expected >= {case.get('min_rows')}"
        if "expected_values" in case:
            got = [float(r[-1]) for r in rows]
            if len(got) != len(case["expected_values"]) or any(
                    not close(g, e) for g, e in zip(got, case["expected_values"])):
                return False, "series values differ from expected"
        return True, ""
    if etype == "series_by_dim":
        dims = {r[0] for r in rows}
        if len(rows) < case.get("min_rows", 0) or len(dims) != case.get("expected_dim_count"):
            return False, f"rows={len(rows)} dims={len(dims)}"
        return True, ""
    if etype == "breakdown":
        if len(rows) != case.get("expected_rows"):
            return False, f"rows={len(rows)} != {case.get('expected_rows')}"
        if "expected_values" in case:
            got = {str(r[0]): float(r[-1]) for r in rows}
            for k, v in case["expected_values"].items():
                if k not in got or not close(got[k], v):
                    return False, f"{k}: {got.get(k)} != {v}"
        return True, ""
    return False, f"no result comparison defined for type {etype!r}"


def _weather_cards_match(case, weather):
    by_id = {card["id"]: card for card in weather["cards"]}
    for card_id, expected in case.get("expected_cards", {}).items():
        if card_id not in by_id:
            return False, f"weather card {card_id} missing"
        if not close(float(by_id[card_id]["current"]), expected):
            return False, f"weather {card_id}: {by_id[card_id]['current']} != {expected}"
    return True, ""


def _watchouts_match(case, weather):
    joined = " ".join(weather["watchouts"]).lower()
    for phrase in case.get("expected_watchouts_contains", []):
        if phrase.lower() not in joined:
            return False, f"watchout missing {phrase!r}"
    return True, ""


def score_single(case, outcome, structural_specs=None):
    """Score one single-shot case from a runner outcome.

    outcome keys: plan, syntax_ok, syntax_error, guard (ok, reason),
    exec_ok, exec_error, columns, rows, runtime_ms, weather (or None).
    Returns a result dict with the five rubric items, sql_semantic,
    structural checks, and task_success.
    """
    p = outcome["plan"]
    etype = case["expected_type"]
    structural = _structural_checks(p.get("sql"), structural_specs)
    struct_ok = all(s["pass"] for s in structural)
    catalog = None
    if p.get("metric") and metric_definition(p["metric"]):
        definition, grain, _den = metric_definition(p["metric"])
        catalog = {"metric": p["metric"], "definition": definition, "grain": grain}

    items = {}
    if etype in ("scalar", "series", "series_by_dim", "breakdown"):
        items["sql_syntax"] = (_item(True, "") if outcome["syntax_ok"]
                               else _item(False, outcome.get("syntax_error", "")))
        items["sql_safety"] = (_item(True, outcome["guard"][1]) if outcome["guard"][0]
                               else _item(False, outcome["guard"][1]))
        items["execution"] = (_item(True, f"{outcome['runtime_ms']}ms, {len(outcome['rows'])} rows")
                              if outcome["exec_ok"]
                              else _item(False, outcome.get("exec_error", "")))
        if outcome["exec_ok"]:
            ok, reason = _result_equiv_single(case, outcome["rows"], outcome["columns"])
            items["result_equiv"] = _item(ok, reason)
        else:
            items["result_equiv"] = _item(False, "no result: execution failed")
        metric_ok = p.get("metric") == case.get("expected_metric")
        items["final_answer"] = _item(
            metric_ok and items["result_equiv"]["pass"],
            "" if metric_ok else f"metric {p.get('metric')} != {case.get('expected_metric')}")
        sql_semantic = metric_ok and struct_ok
    elif etype == "weather_report":
        items["sql_syntax"] = _na("no SQL: weather report")
        items["sql_safety"] = _na("no SQL: weather report")
        items["execution"] = (_item(True, "weather report computed")
                              if outcome["exec_ok"]
                              else _item(False, outcome.get("exec_error", "")))
        if outcome["exec_ok"]:
            ok, reason = _weather_cards_match(case, outcome["weather"])
            items["result_equiv"] = _item(ok, reason)
            ok2, reason2 = _watchouts_match(case, outcome["weather"])
            items["final_answer"] = _item(
                ok and ok2, reason if not ok else reason2)
        else:
            items["result_equiv"] = _item(False, "no result: weather failed")
            items["final_answer"] = _item(False, "no result: weather failed")
        sql_semantic = p.get("type") == "weather_report"
    elif etype in ("clarification", "refusal"):
        items["sql_syntax"] = _na("no SQL by design")
        items["sql_safety"] = _na("nothing executed")
        items["execution"] = _na("nothing to execute")
        items["result_equiv"] = _na("no result: clarification/refusal")
        type_ok = p.get("type") == etype and p.get("sql") is None
        contains_ok = True
        if "expected_contains" in case:
            contains_ok = case["expected_contains"].lower() in (p.get("clarification") or "").lower()
        items["final_answer"] = _item(
            type_ok and contains_ok,
            "" if type_ok and contains_ok else
            f"type {p.get('type')} / clarification did not contain {case.get('expected_contains')!r}")
        sql_semantic = type_ok
    elif etype == "experiment_list":
        items["sql_syntax"] = _na("no SQL: registry listing")
        items["sql_safety"] = _na("nothing executed")
        items["execution"] = _na("nothing to execute")
        items["result_equiv"] = _na("no result: registry listing")
        items["final_answer"] = _item(p.get("type") == "experiment_list",
                                      f"type {p.get('type')}")
        sql_semantic = p.get("type") == "experiment_list"
    elif etype == "experiment_info":
        items["sql_syntax"] = _na("no SQL: registry lookup")
        items["sql_safety"] = _na("nothing executed")
        items["execution"] = _na("nothing to execute")
        items["result_equiv"] = _na("no result: registry lookup")
        ok = p.get("type") == "experiment_info" and p.get("metric") == case.get("expected_metric")
        items["final_answer"] = _item(ok, f"type {p.get('type')} metric {p.get('metric')}")
        sql_semantic = ok
    else:
        raise ValueError(f"unknown expected_type {etype!r} for case {case['id']}")

    return {"id": case["id"], "kind": "single", "family": case.get("family"),
            "question": case["question"], "plan_type": p.get("type"),
            "metric": p.get("metric"), "catalog": catalog,
            "sql": p.get("sql"), "guard": {"ok": outcome["guard"][0],
                                           "reason": outcome["guard"][1]},
            "runtime_ms": outcome["runtime_ms"],
            "row_count": len(outcome["rows"]),
            "columns": outcome["columns"],
            "trace": outcome.get("trace"),
            "items": items, "sql_semantic": bool(sql_semantic),
            "structural": structural,
            "task_success": bool(items["final_answer"]["pass"])}


# --------------------------------------------------------------------------
# Conversation scoring
# --------------------------------------------------------------------------

def _load_conv_runner():
    """Import check_turn / check_values from the existing conversation
    runner instead of duplicating them. That runner is the reference
    implementation; the harness drives it through the same checks."""
    spec = importlib.util.spec_from_file_location(
        "run_conversation_benchmark",
        ROOT / "scripts/run_conversation_benchmark.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check_turn, module.check_values


def _turn_topic(reply):
    for key in ("ranking", "breakdown", "drilldown"):
        if reply.get(key):
            return reply[key]["topic"]
    if reply.get("diagnosis"):
        return reply["diagnosis"]["primary_topic"]
    if reply.get("recommendation"):
        return reply["recommendation"]["topic"]
    if reply.get("brief"):
        return reply["brief"]["topic"]
    return None


def score_conversation(conv, data_dir, structural_specs=None, variant="baseline"):
    """Drive a ChatSession through one conversation and score it.
    Returns a result dict with per-turn traces and the five items."""
    from socialpulse_nlq.chat import ChatSession

    check_turn, check_values = _load_conv_runner()
    session = ChatSession(data_dir, variant=variant)
    replies, turn_traces, failures = [], [], []
    con = None
    syntax_ok, safety_ok, exec_ok = True, True, True
    syntax_reason, safety_reason, exec_reason = "", "", ""
    try:
        con = open_connection(data_dir)
        for turn in conv["turns"]:
            t0 = time.time()
            try:
                reply = session.respond(turn["user"])
            except Exception as e:  # noqa: BLE001 - a crashing turn is a failed turn
                reply = {"type": "error", "text": str(e)}
                exec_ok, exec_reason = False, f"turn raised: {e}"
            ms = int((time.time() - t0) * 1000)
            replies.append(reply)
            working = reply.get("working") or {}
            sql = working.get("sql")
            turn_failures = []
            check_turn(conv, turn, reply, turn_failures)
            if sql:
                try:
                    con.execute("EXPLAIN " + sql)
                except Exception as e:  # noqa: BLE001 - syntax failure is data
                    syntax_ok, syntax_reason = False, f"turn SQL did not parse: {e}"
                gok, greason = guard_sql(sql)
                if not gok:
                    safety_ok, safety_reason = False, greason
            if turn_failures:
                failures.extend(turn_failures)
            turn_traces.append({
                "user": turn["user"], "type": reply.get("type"),
                "app": reply.get("app"), "topic": _turn_topic(reply),
                "text": (reply.get("text") or "")[:300],
                "sql": sql, "tables": working.get("tables"),
                "row_count": working.get("row_count"),
                "runtime_ms": ms, "turn_failures": turn_failures})
    finally:
        if con is not None:
            con.close()
    value_failures = []
    check_values(conv, replies, value_failures)
    failures.extend(value_failures)

    structural = []
    for trace in turn_traces:
        structural.extend(_structural_checks(trace.get("sql"), structural_specs))
    struct_ok = all(s["pass"] for s in structural)
    has_sql = any(t.get("sql") for t in turn_traces)
    items = {
        "sql_syntax": (_item(syntax_ok, syntax_reason) if has_sql
                       else _na("no SQL in any turn")),
        "sql_safety": (_item(safety_ok, safety_reason) if has_sql
                       else _na("no SQL in any turn")),
        "execution": _item(exec_ok, exec_reason),
        "result_equiv": (_item(not value_failures, "; ".join(value_failures))
                         if conv.get("checks") else _na("no numeric checks in this conversation")),
        "final_answer": _item(not failures, "; ".join(failures)),
    }
    return {"id": conv["id"], "kind": "conversation", "family": conv.get("family"),
            "description": conv.get("description"), "items": items,
            "sql_semantic": (not failures) and struct_ok,
            "structural": structural, "turns": turn_traces,
            "task_success": bool(items["final_answer"]["pass"])}


# --------------------------------------------------------------------------
# Oracle
# --------------------------------------------------------------------------

def _derive_watchouts(cards):
    """Watchout phrases from oracle card values, using the documented
    rules in weather.py (counts move on 2% relative, rates on 1 point;
    orphan > 20%, top-1% > 8%). Re-implemented here, not imported, so
    the oracle does not share code with the thing it checks."""
    def direction(kind, cur, pri):
        if cur is None or pri in (None, 0):
            return "flat"
        if kind == "rate":
            pp = (cur - pri) * 100
            return "up" if pp >= 1.0 else ("down" if pp <= -1.0 else "flat")
        pct = (cur - pri) / pri * 100
        return "up" if pct >= 2.0 else ("down" if pct <= -2.0 else "flat")

    out = []
    dau, d7 = cards.get("dau"), cards.get("d7_retention")
    if dau and direction("count", dau.get("current"), dau.get("prior")) == "down":
        out.append("Growth falling")
    if d7 and direction("rate", d7.get("current"), d7.get("prior")) == "down":
        out.append("Retention falling")
    orph = cards.get("orphan_post_rate", {})
    if orph.get("current") and orph["current"] > 0.20:
        out.append("Creator problem")
    creators, pen = cards.get("daily_creators"), cards.get("creator_penetration")
    if ((creators and direction("count", creators.get("current"), creators.get("prior")) == "down")
            or (pen and direction("rate", pen.get("current"), pen.get("prior")) == "down")):
        out.append("Creator supply softening")
    eng = cards.get("engagement_rate")
    if eng and direction("rate", eng.get("current"), eng.get("prior")) == "down":
        out.append("Engagement softening")
    top1 = cards.get("top1_share", {})
    if top1.get("current") and top1["current"] > 0.08:
        out.append("Concentration watch")
    return out


def run_oracle(con, data_dir, singles, convs, oracle_by_id):
    """Execute every oracle entry directly on DuckDB and compare against
    the stored expectation. Never touches the planner. Returns a list of
    per-case oracle results; disagreements are findings, and stored
    values are never rewritten here."""
    single_by_id = {c["id"]: c for c in singles}
    conv_by_id = {c["id"]: c for c in convs}
    results = []
    max_date = con.execute(
        "SELECT MAX(CAST(start_time AS DATE)) FROM fact_sessions").fetchone()[0]
    if str(max_date) != "2026-03-31":
        raise ManifestError(
            f"oracle periods assume max session date 2026-03-31, data has {max_date}")
    for case_id, entry in sorted(oracle_by_id.items()):
        case = single_by_id.get(case_id) or conv_by_id.get(case_id)
        kind = entry["kind"]
        try:
            if kind == "scalar":
                value = con.execute(entry["sql"]).fetchone()[0]
                if "expected_value" in case:
                    stored = case["expected_value"]
                elif case.get("expected_values") and len(case["expected_values"]) == 1:
                    # Breakdown case with a single stored value (b19).
                    stored = next(iter(case["expected_values"].values()))
                elif case.get("expected_type") is None:
                    # Conversation case with no stored numeric value:
                    # c9/c10, MAU ground truth, same as b38.
                    stored = 210809.0
                else:
                    stored = None
                if stored is None:
                    results.append({"id": case_id, "kind": kind, "agree": False,
                                    "oracle_value": value, "stored_value": None,
                                    "detail": "no stored expectation to compare"})
                    continue
                agree = close(float(value), float(stored))
                rel = abs(float(value) - float(stored)) / max(1.0, abs(float(stored)))
                # Within tolerance but not exact: still agreement, but the
                # gap stays visible (b08: canonical SQL lacks the window
                # filter, so it is 1.07% off the stored value).
                inexact = agree and rel > 0.001
                results.append({"id": case_id, "kind": kind, "agree": agree,
                                "inexact": inexact,
                                "oracle_value": value, "stored_value": stored,
                                "detail": (f"oracle {value} differs from stored {stored} "
                                           f"by {rel * 100:.2f}% (within tolerance)"
                                           if inexact else
                                           ("" if agree else
                                            f"oracle {value} != stored {stored}"))})
            elif kind in ("series", "breakdown"):
                cur = con.execute(entry["sql"])
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
                ok, reason = _result_equiv_single(case, rows, cols)
                # _result_equiv_single compares against the planner-shaped
                # expectation; for breakdowns with no stored values it only
                # checks row counts, which is exactly the stored assertion.
                results.append({"id": case_id, "kind": kind, "agree": ok,
                                "oracle_value": f"{len(rows)} rows",
                                "stored_value": f"expected_rows={case.get('expected_rows')} "
                                                f"min_rows={case.get('min_rows')} "
                                                f"dims={case.get('expected_dim_count')}",
                                "detail": reason})
            elif kind == "weather":
                cards = {}
                for card_id, spec in entry["cards"].items():
                    cur = con.execute(spec["current"]).fetchone()[0]
                    pri = (con.execute(spec["prior"]).fetchone()[0]
                           if spec.get("prior") else None)
                    cards[card_id] = {"current": cur, "prior": pri}
                stored = case.get("expected_cards", {})
                problems, inexact_cards = [], []
                for card_id, expected in stored.items():
                    if card_id not in cards:
                        problems.append(f"card {card_id} missing from oracle")
                    elif not close(float(cards[card_id]["current"]), expected):
                        problems.append(
                            f"{card_id}: oracle {cards[card_id]['current']} != stored {expected}")
                    elif abs(float(cards[card_id]["current"]) - float(expected)) > \
                            0.001 * max(1.0, abs(float(expected))):
                        inexact_cards.append(card_id)
                derived = _derive_watchouts(cards)
                for phrase in case.get("expected_watchouts_contains", []):
                    if not any(phrase.lower() in w.lower() for w in derived):
                        problems.append(f"watchout {phrase!r} not derivable from oracle cards")
                results.append({"id": case_id, "kind": kind,
                                "agree": not problems,
                                "inexact": bool(inexact_cards),
                                "oracle_value": {k: v["current"] for k, v in cards.items()},
                                "stored_value": stored,
                                "detail": "; ".join(problems) or
                                          (f"cards agree within tolerance but not exact: "
                                           f"{inexact_cards}" if inexact_cards else "")})
            elif kind == "conversation_values":
                problems, values = [], {}
                for check_name, spec in entry["checks"].items():
                    value = con.execute(spec["sql"]).fetchone()[0]
                    values[check_name] = value
                    stored = case["checks"][check_name]
                    tol = spec.get("tolerance", case["checks"].get(
                        check_name + "_tolerance", 10.0))
                    if abs(float(value) - float(stored)) > tol:
                        problems.append(
                            f"{check_name}: oracle {value} != stored {stored} (tol {tol})")
                results.append({"id": case_id, "kind": kind,
                                "agree": not problems, "oracle_value": values,
                                "stored_value": {k: case["checks"][k]
                                                 for k in entry["checks"]},
                                "detail": "; ".join(problems)})
            elif kind == "behavioral":
                problems = []
                ev = entry.get("evidence_sql")
                ev_value = None
                if ev:
                    rows = [r[0] for r in con.execute(ev).fetchall()]
                    for absent in entry.get("evidence_expect_absent", []):
                        if absent in rows:
                            problems.append(f"{absent} unexpectedly present in {rows}")
                    if "evidence_expect_value" in entry:
                        # The premise is a value, not an absence: e.g. the
                        # true app-scoped number a hard case rests on
                        # (h01-h09), or "zero April sessions" (h17/h18).
                        # Compared with the harness 2% tolerance for
                        # numbers, exact equality otherwise. The value
                        # is the premise the case rests on, not an
                        # expected answer -- these cases expect a
                        # clarification, so no answer is stored.
                        ev_value = rows[0] if rows else None
                        expected = entry["evidence_expect_value"]
                        try:
                            # DuckDB may return Decimal for literal
                            # decimals; coerce so the comparison is
                            # numeric, not type-sensitive.
                            ok = close(float(ev_value), float(expected))
                        except (TypeError, ValueError):
                            ok = ev_value == expected
                        if not ok:
                            problems.append(
                                f"evidence value {ev_value} != expected {expected}")
                evf = (entry.get("evidence") or {}).get("data_dir_has_no_file_matching")
                if evf:
                    import fnmatch

                    matches = [f for f in Path(data_dir).iterdir()
                               if fnmatch.fnmatch(f.name, evf)]
                    if matches:
                        problems.append(f"files matching {evf}: {[m.name for m in matches]}")
                if "evidence_sql" in entry and "evidence_expect_top_dim" in entry:
                    rows = con.execute(entry["evidence_sql"]).fetchall()
                    if not rows or str(rows[0][0]) != entry["evidence_expect_top_dim"]:
                        problems.append(
                            f"top dim {rows[0][0] if rows else None} != "
                            f"{entry['evidence_expect_top_dim']}")
                results.append({"id": case_id, "kind": kind,
                                "agree": not problems,
                                "oracle_value": (ev_value if "evidence_expect_value" in entry
                                                 else "premise holds"),
                                "stored_value": (entry.get("evidence_expect_value")
                                                 if "evidence_expect_value" in entry
                                                 else "behavioral spec"),
                                "detail": "; ".join(problems)})
            elif kind == "registry":
                text = (Path(ROOT) / "catalog/experiments.yaml").read_text()
                ids = re.findall(r"-\s*id:\s*(\S+)", text)
                spec = entry["registry"]
                if spec["expect"] == "list":
                    agree = len(ids) == 3
                    results.append({"id": case_id, "kind": kind, "agree": agree,
                                    "oracle_value": ids, "stored_value": "3 experiments",
                                    "detail": "" if agree else f"registry lists {ids}"})
                else:
                    block = re.search(
                        rf"-\s*id:\s*{re.escape(spec['experiment'])}\b(.*?)(?=-\s*id:|\Z)",
                        text, re.DOTALL)
                    got = None
                    if block:
                        m = re.search(rf"{re.escape(spec['field'])}:\s*(\S+)", block.group(1))
                        got = m.group(1) if m else None
                    agree = got == spec["value"]
                    results.append({"id": case_id, "kind": kind, "agree": agree,
                                    "oracle_value": got, "stored_value": spec["value"],
                                    "detail": "" if agree else f"registry says {got}"})
            else:
                results.append({"id": case_id, "kind": kind, "agree": False,
                                "oracle_value": None, "stored_value": None,
                                "detail": f"unknown oracle kind {kind!r}"})
        except Exception as e:  # noqa: BLE001 - an oracle that errors is a finding
            results.append({"id": case_id, "kind": kind, "agree": False,
                            "oracle_value": None, "stored_value": None,
                            "detail": f"oracle execution failed: {e}"})
    return results


# --------------------------------------------------------------------------
# Full run + summary
# --------------------------------------------------------------------------

def _run_single(case, con, data_dir, structural_specs, variant="baseline"):
    p = plan(case["question"], variant=variant)
    outcome = {"plan": p, "guard": (True, "no SQL"), "syntax_ok": True,
               "syntax_error": "", "exec_ok": True, "exec_error": "",
               "columns": [], "rows": [], "runtime_ms": 0, "weather": None,
               "trace": None}
    if case["expected_type"] in ("scalar", "series", "series_by_dim", "breakdown"):
        if p.get("sql"):
            try:
                con.execute("EXPLAIN " + p["sql"])
            except Exception as e:  # noqa: BLE001 - syntax failure is data
                outcome["syntax_ok"] = False
                outcome["syntax_error"] = str(e)
            outcome["guard"] = guard_sql(p["sql"])
            if outcome["guard"][0] and outcome["syntax_ok"]:
                try:
                    cols, rows, ms = run_plan(p, data_dir)
                    outcome.update({"columns": cols, "rows": rows, "runtime_ms": ms})
                    outcome["trace"] = build_trace(p, len(rows), ms,
                                                   rows[0] if rows else None)
                except Exception as e:  # noqa: BLE001 - execution failure is data
                    outcome["exec_ok"] = False
                    outcome["exec_error"] = str(e)
            else:
                outcome["exec_ok"] = False
                outcome["exec_error"] = "not executed: " + (
                    "guard failed" if not outcome["guard"][0] else "syntax check failed")
        else:
            outcome["syntax_ok"] = False
            outcome["syntax_error"] = "planner produced no SQL for a query case"
            outcome["guard"] = (False, "no SQL to guard")
            outcome["exec_ok"] = False
            outcome["exec_error"] = "no SQL produced"
    elif case["expected_type"] == "weather_report":
        try:
            t0 = time.time()
            outcome["weather"] = compute_weather(data_dir, app=p.get("app"))
            outcome["runtime_ms"] = int((time.time() - t0) * 1000)
        except Exception as e:  # noqa: BLE001 - execution failure is data
            outcome["exec_ok"] = False
            outcome["exec_error"] = str(e)
    return score_single(case, outcome, structural_specs)


def _summarize(results):
    """Per-family + overall summary. No naive CI: the family is the unit,
    and v1 families are too small for one anyway — counts and rates only."""
    by_family = {}
    for r in results:
        fam = by_family.setdefault(r["family"], {"n": 0, "task_success": 0,
                                                 "items": {}})
        fam["n"] += 1
        fam["task_success"] += int(r["task_success"])
        for name, item in r["items"].items():
            slot = fam["items"].setdefault(name, {"pass": 0, "applicable": 0})
            if item["pass"] is not None:
                slot["applicable"] += 1
                slot["pass"] += int(item["pass"])
    overall = {"n": len(results),
               "task_success": sum(int(r["task_success"]) for r in results)}
    return {"overall": overall, "by_family": by_family}


def cases_for_split(manifest, split):
    """Case ids in one split. 'all' is every case; dev and held-out are
    disjoint by construction (validated in load_manifest)."""
    return {c["id"] for c in manifest["cases"]
            if split == "all" or c["split"] == split}


def run_benchmark(root=ROOT, data_dir=None, split="dev", with_oracle=True,
                  version=MANIFEST_VERSION, variant="baseline"):
    """Run the frozen benchmark for one split. Returns the full report dict.
    variant selects a slice-3 planner variant (see docs/ablations.md);
    default "baseline" is the current planner exactly as-is."""
    data_dir = Path(data_dir or (Path(root) / "data/phase1"))
    manifest = load_manifest(root, version=version)
    singles, convs, oracle_by_id = load_cases(root, version=version)
    validate_case_set(manifest, singles, convs)
    con = open_connection(data_dir)
    try:
        counts = validate_data_pin(con, data_dir, manifest)
    finally:
        con.close()
    oracle_results = None
    if with_oracle:
        con = open_connection(data_dir)
        try:
            oracle_results = run_oracle(con, data_dir, singles, convs, oracle_by_id)
        finally:
            con.close()
    wanted = cases_for_split(manifest, split)
    structural = {}
    for cid, entry in oracle_by_id.items():
        if entry.get("structural"):
            structural[cid] = entry["structural"]
    results = []
    con = open_connection(data_dir)
    try:
        for case in singles:
            if case["id"] in wanted:
                results.append(_run_single(case, con, data_dir,
                                           structural.get(case["id"]), variant))
    finally:
        con.close()
    for conv in convs:
        if conv["id"] in wanted:
            results.append(score_conversation(conv, data_dir,
                                              structural.get(conv["id"]), variant))
    report = {"benchmark": manifest["benchmark"], "version": manifest["version"],
              "variant": variant,
              "split": split, "data": manifest["data"],
              "oracle": oracle_results,
              "results": results,
              "summary": _summarize(results)}
    return report
