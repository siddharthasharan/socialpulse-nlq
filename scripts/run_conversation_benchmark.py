"""Multi-turn conversation benchmark for Phase 3 chat.

Unlike scripts/run_benchmark.py (single question -> single expected
answer), this runner checks a *sequence*: each turn's stage/type, app,
and topic, plus value checks on the weather/drill-down cards that the
turn produced. A conversation passes only if every turn passes.

Usage: python scripts/run_conversation_benchmark.py [--data data/phase1]
Exit code 0 only if every conversation passes.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from socialpulse_nlq.chat import ChatSession


def _cards(weather):
    return {c["id"]: c for c in weather["cards"]} if weather else {}


def check_turn(conv, turn, reply, failures):
    exp = turn
    if exp.get("expect_type") and reply["type"] != exp["expect_type"]:
        failures.append(f"type: expected {exp['expect_type']}, got {reply['type']}")
    if exp.get("expect_app") and reply.get("app") != exp["expect_app"]:
        failures.append(f"app: expected {exp['expect_app']}, got {reply.get('app')}")
    topic = None
    if reply.get("breakdown"):
        topic = reply["breakdown"]["topic"]
    elif reply.get("drilldown"):
        topic = reply["drilldown"]["topic"]
    elif reply.get("diagnosis"):
        topic = reply["diagnosis"]["primary_topic"]
    elif reply.get("recommendation"):
        topic = reply["recommendation"]["topic"]
    elif reply.get("brief"):
        topic = reply["brief"]["topic"]
    if exp.get("expect_topic") and topic != exp["expect_topic"]:
        failures.append(f"topic: expected {exp['expect_topic']}, got {topic}")


def check_values(conv, replies, failures):
    checks = conv.get("checks", {})
    weather = next((r.get("weather") for r in replies if r.get("weather")), None)
    if weather and "weather_dau" in checks:
        got = _cards(weather)["dau"]["current"]
        if abs(got - checks["weather_dau"]) > checks.get("weather_dau_tolerance", 10.0):
            failures.append(f"weather DAU: expected ~{checks['weather_dau']}, got {got}")
    if weather and "weather_d7" in checks:
        got = _cards(weather)["d7_retention"]["current"]
        if abs(got - checks["weather_d7"]) > checks.get("weather_d7_tolerance", 0.01):
            failures.append(f"weather D7: expected ~{checks['weather_d7']}, got {got}")
    drill = next((r.get("drilldown") for r in replies if r.get("drilldown")), None)
    if drill and "drilldown_daily_creators" in checks:
        got = drill["evidence"]["daily_creators"]["current"]
        if abs(got - checks["drilldown_daily_creators"]) > 1.0:
            failures.append(f"drill-down daily creators: expected ~{checks['drilldown_daily_creators']}, got {got}")
    if drill and "drilldown_orphan" in checks:
        got = drill["evidence"]["orphan_post_rate"]["current"]
        if abs(got - checks["drilldown_orphan"]) > 0.01:
            failures.append(f"drill-down orphan: expected ~{checks['drilldown_orphan']}, got {got}")
    rec = next((r.get("recommendation") for r in replies if r.get("recommendation")), None)
    if rec and "recommendation_label" in checks and rec["label"] != checks["recommendation_label"]:
        failures.append(f"recommendation label: expected {checks['recommendation_label']}, got {rec['label']}")
    if rec and "recommendation_must_not_contain" in checks:
        rec_text = next(r["text"] for r in replies if r.get("recommendation"))
        if checks["recommendation_must_not_contain"].lower() in rec_text.lower():
            failures.append(f"recommendation must not contain '{checks['recommendation_must_not_contain']}'")
    diag = next((r.get("diagnosis") for r in replies if r.get("diagnosis")), None)
    if diag and "diagnosis_text_must_not_contain" in checks:
        diag_text = next(r["text"] for r in replies if r.get("diagnosis"))
        if checks["diagnosis_text_must_not_contain"].lower() in diag_text.lower():
            failures.append(f"diagnosis must not contain '{checks['diagnosis_text_must_not_contain']}'")
    brief = next((r.get("brief") for r in replies if r.get("brief")), None)
    if brief:
        if len(brief["pm_points"]) < checks.get("brief_pm_points_min", 0):
            failures.append("brief: too few PM points")
        if len(brief["eng_points"]) < checks.get("brief_eng_points_min", 0):
            failures.append("brief: too few Eng points")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data/phase1")
    args = ap.parse_args()
    convs = json.loads((ROOT / "evals/conversation_phase3.json").read_text())
    all_ok = True
    for conv in convs:
        session = ChatSession(args.data)
        replies = []
        failures = []
        for turn in conv["turns"]:
            reply = session.respond(turn["user"])
            replies.append(reply)
            check_turn(conv, turn, reply, failures)
        check_values(conv, replies, failures)
        status = "PASS" if not failures else "FAIL"
        if failures:
            all_ok = False
        print(f"{status} {conv['id']}: {conv['description']}")
        for f in failures:
            print(f"  - {f}")
    print(f"\n{len(convs)} conversations, {'all pass' if all_ok else 'FAILURES'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
