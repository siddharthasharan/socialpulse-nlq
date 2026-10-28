"""Both-in-one questions store for SocialPulse NLQ: chat flow + evals.

This module is the file-backed source for the Questions editor in the
web UI (Flow tab + Evals tabs). It deliberately does NOT invent a new
format for evals: the eval files keep their existing shapes
(evals/benchmark_phase2.json, evals/conversation_phase3.json), and the
chat flow lives in chat/questions.json, a new file with the same
structure as the constants that used to be hardcoded in chat.py.

Why a file, not code: the user asked to look at and update the
questions in an interface like the chat UI, instead of editing code.
A file the chat actually reads (on every new ChatSession, not on
server restart) makes that possible, and a test asserts the file and
the hardcoded defaults stay in sync when the file is absent.

Safety: saves are validated before they are written, and written
atomically (temp file + os.replace) so a bad save cannot leave a
half-written JSON file. The editor is local-only: it writes only the
three allowlisted files below, never code, and never SQL. A question
template is text, not a query — the planner's allowlist and guard
still apply to any metric query the chat or planner runs.
"""
import json
import os
from pathlib import Path

# Defaults, identical to the constants chat.py used before this module.
# If chat/questions.json is missing, these are used. If it exists, it
# is merged over these, so a partial file cannot delete a topic.
DEFAULT_TOPIC_LABELS = {
    "creators": "Creators — supply, orphan rate, concentration",
    "retention": "Retention — D7 and growth",
    "growth": "Growth — DAU and WAU",
    "messaging": "Messaging — messaging DAU",
    "engagement": "Engagement — engagement rate and Reels",
    "concentration": "Concentration — orphan rate and top-1% share",
}
DEFAULT_TOPIC_CARDS = {
    "creators": ["daily_creators", "creator_penetration", "orphan_post_rate", "top1_share"],
    "retention": ["d7_retention", "dau", "wau"],
    "growth": ["dau", "wau"],
    "messaging": ["messaging_dau"],
    "engagement": ["engagement_rate", "reels_completion", "reels_watch_time"],
    "concentration": ["orphan_post_rate", "top1_share"],
}
DEFAULT_RECOMMENDATIONS = {
    "creators": {
        "hypothesis": ("New and small creators' first posts are not reaching "
                       "likely engagers, so too many posts get zero engagement "
                       "and creator supply softens."),
        "intervention": ("Surface first posts from new/small creators to likely "
                         "engagers, and prompt a first reply on those posts."),
        "primary_metric": "daily_creators and creator_penetration",
        "guardrails": ["orphan_post_rate", "d7_retention", "engagement_rate", "top1_share"],
        "population": "new and small creators on the scoped app",
    },
    "retention": {
        "hypothesis": ("New cohorts are not forming a first-week habit, so D7 "
                       "retention falls even while DAU is up."),
        "intervention": ("Improve first-week onboarding and re-engagement for "
                         "new cohorts, with a clear first-week return prompt."),
        "primary_metric": "d7_retention",
        "guardrails": ["dau", "messaging_dau", "engagement_rate"],
        "population": "new signups on the scoped app",
    },
    "growth": {
        "hypothesis": ("Activation after signup is weak, so DAU growth is not "
                       "keeping up with signups."),
        "intervention": ("Improve signup-to-first-session activation for new users."),
        "primary_metric": "dau",
        "guardrails": ["d7_retention", "sessions_per_dau"],
        "population": "new signups on the scoped app",
    },
    "messaging": {
        "hypothesis": ("Messaging is not becoming a habit for new users, so "
                       "messaging DAU lags overall DAU."),
        "intervention": ("Prompt a first message / reply in the first week, "
                         "targeted at new users with no sent message yet."),
        "primary_metric": "messaging_dau",
        "guardrails": ["d7_retention", "dau"],
        "population": "new users on the scoped app with no sent message yet",
    },
    "engagement": {
        "hypothesis": ("Feed/Reels relevance is soft for low-engagement segments, "
                       "so engagement per impression falls without DAU falling."),
        "intervention": ("Improve ranking relevance for low-engagement segments."),
        "primary_metric": "engagement_rate",
        "guardrails": ["creator_penetration", "d7_retention", "reels_completion"],
        "population": "low-engagement users on the scoped app",
    },
    "concentration": {
        "hypothesis": ("Attention is concentrated on a few creators, so new and "
                       "small creators' posts get zero engagement."),
        "intervention": ("Surface first posts from new/small creators to likely "
                         "engagers, as for the creators topic."),
        "primary_metric": "orphan_post_rate (lower is better)",
        "guardrails": ["daily_creators", "d7_retention", "top1_share"],
        "population": "new and small creators on the scoped app",
    },
}
DEFAULT_DIAGNOSIS_PRIORITY = ["creators", "retention", "growth", "engagement"]

BENCHMARK_TYPES = {"scalar", "series", "series_by_dim", "breakdown",
                   "clarification", "refusal", "experiment_list", "experiment_info",
                   "weather_report"}
BENCHMARK_CATEGORIES = {"simple", "segments", "cohorts", "diagnostics",
                        "experiments", "weather_report", "ambiguous_invalid"}
CONVERSATION_TYPES = {"weather", "drilldown", "diagnosis", "recommendation",
                      "brief", "clarification"}


def default_flow():
    """Return the flow dict in the chat/questions.json shape."""
    return {
        "topics": {t: {"label": DEFAULT_TOPIC_LABELS[t], "cards": list(DEFAULT_TOPIC_CARDS[t])}
                   for t in DEFAULT_TOPIC_LABELS},
        "recommendations": {t: dict(v, guardrails=list(v["guardrails"]))
                            for t, v in DEFAULT_RECOMMENDATIONS.items()},
        "diagnosis_priority": list(DEFAULT_DIAGNOSIS_PRIORITY),
    }


def flow_path(repo_root):
    return Path(repo_root) / "chat" / "questions.json"


def load_flow(repo_root):
    """Load chat/questions.json merged over defaults. Missing file -> defaults."""
    flow = default_flow()
    p = flow_path(repo_root)
    if not p.exists():
        return flow
    try:
        data = json.loads(p.read_text())
    except Exception:
        return flow
    if not isinstance(data, dict):
        return flow
    for topic, spec in (data.get("topics") or {}).items():
        if isinstance(spec, dict) and isinstance(spec.get("label"), str):
            flow["topics"].setdefault(topic, {"label": spec["label"], "cards": []})
            flow["topics"][topic]["label"] = spec["label"]
            if isinstance(spec.get("cards"), list):
                flow["topics"][topic]["cards"] = spec["cards"]
    for topic, spec in (data.get("recommendations") or {}).items():
        if isinstance(spec, dict) and all(isinstance(spec.get(k), str) for k in
                                          ("hypothesis", "intervention", "primary_metric", "population")):
            flow["recommendations"][topic] = {
                "hypothesis": spec["hypothesis"], "intervention": spec["intervention"],
                "primary_metric": spec["primary_metric"],
                "guardrails": spec.get("guardrails", []),
                "population": spec["population"],
            }
    if isinstance(data.get("diagnosis_priority"), list) and data["diagnosis_priority"]:
        flow["diagnosis_priority"] = data["diagnosis_priority"]
    return flow


def _is_str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def validate_flow(data):
    if not isinstance(data, dict):
        return False, "flow must be an object"
    topics = data.get("topics")
    if not isinstance(topics, dict) or not topics:
        return False, "flow.topics must be a non-empty object"
    for topic, spec in topics.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("label"), str) or not spec["label"].strip():
            return False, f"topic {topic!r} needs a non-empty label"
        if "cards" in spec and not _is_str_list(spec["cards"]):
            return False, f"topic {topic!r} cards must be a list of strings"
    recs = data.get("recommendations")
    if not isinstance(recs, dict) or not recs:
        return False, "flow.recommendations must be a non-empty object"
    for topic, spec in recs.items():
        if not isinstance(spec, dict):
            return False, f"recommendation {topic!r} must be an object"
        for k in ("hypothesis", "intervention", "primary_metric", "population"):
            if not isinstance(spec.get(k), str) or not spec[k].strip():
                return False, f"recommendation {topic!r} needs a non-empty {k}"
        if not _is_str_list(spec.get("guardrails", [])):
            return False, f"recommendation {topic!r} guardrails must be a list of strings"
    if "diagnosis_priority" in data and not _is_str_list(data["diagnosis_priority"]):
        return False, "diagnosis_priority must be a list of strings"
    return True, "ok"


def validate_benchmark(data):
    if not isinstance(data, list) or not data:
        return False, "benchmark must be a non-empty list"
    ids = set()
    for c in data:
        if not isinstance(c, dict):
            return False, "each benchmark case must be an object"
        if not isinstance(c.get("id"), str) or c["id"] in ids:
            return False, "benchmark ids must be unique non-empty strings"
        ids.add(c["id"])
        if not isinstance(c.get("question"), str) or not c["question"].strip():
            return False, f"case {c['id']} needs a non-empty question"
        if c.get("expected_type") not in BENCHMARK_TYPES:
            return False, f"case {c['id']} expected_type must be one of {sorted(BENCHMARK_TYPES)}"
        if c.get("category") and c["category"] not in BENCHMARK_CATEGORIES:
            return False, f"case {c['id']} category must be one of {sorted(BENCHMARK_CATEGORIES)}"
        if "expected_value" in c and not isinstance(c["expected_value"], (int, float)):
            return False, f"case {c['id']} expected_value must be a number"
        if "expected_values" in c and not (isinstance(c["expected_values"], list)
                                           and all(isinstance(x, (int, float)) for x in c["expected_values"])):
            return False, f"case {c['id']} expected_values must be a list of numbers"
        if "expected_cards" in c and not (isinstance(c["expected_cards"], dict)
                                          and all(isinstance(v, (int, float)) for v in c["expected_cards"].values())):
            return False, f"case {c['id']} expected_cards must map card ids to numbers"
    return True, "ok"


def validate_conversation(data):
    if not isinstance(data, list) or not data:
        return False, "conversations must be a non-empty list"
    ids = set()
    for conv in data:
        if not isinstance(conv, dict):
            return False, "each conversation must be an object"
        if not isinstance(conv.get("id"), str) or conv["id"] in ids:
            return False, "conversation ids must be unique non-empty strings"
        ids.add(conv["id"])
        turns = conv.get("turns")
        if not isinstance(turns, list) or not turns:
            return False, f"conversation {conv['id']} needs at least one turn"
        for t in turns:
            if not isinstance(t, dict) or not isinstance(t.get("user"), str) or not t["user"].strip():
                return False, f"conversation {conv['id']} turns need a non-empty user text"
            if t.get("expect_type") and t["expect_type"] not in CONVERSATION_TYPES:
                return False, f"conversation {conv['id']} expect_type must be one of {sorted(CONVERSATION_TYPES)}"
    return True, "ok"


VALIDATORS = {"flow": validate_flow, "benchmark": validate_benchmark,
              "conversation": validate_conversation}


def _target_path(repo_root, kind):
    if kind == "flow":
        return flow_path(repo_root)
    if kind == "benchmark":
        return Path(repo_root) / "evals" / "benchmark_phase2.json"
    if kind == "conversation":
        return Path(repo_root) / "evals" / "conversation_phase3.json"
    raise ValueError(f"unknown questions kind: {kind}")


def save_questions(repo_root, kind, data):
    """Validate then atomically write one allowlisted questions file."""
    if kind not in VALIDATORS:
        raise ValueError(f"unknown questions kind: {kind}")
    ok, reason = VALIDATORS[kind](data)
    if not ok:
        raise ValueError(reason)
    path = _target_path(repo_root, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)
    return path


def load_all_questions(repo_root):
    """Return {flow, benchmark, conversations} for the editor GET endpoint."""
    root = Path(repo_root)
    flow = load_flow(root)
    benchmark = []
    conversations = []
    bp = root / "evals" / "benchmark_phase2.json"
    if bp.exists():
        try:
            benchmark = json.loads(bp.read_text())
        except Exception:
            benchmark = []
    cp = root / "evals" / "conversation_phase3.json"
    if cp.exists():
        try:
            conversations = json.loads(cp.read_text())
        except Exception:
            conversations = []
    return {"flow": flow, "benchmark": benchmark, "conversations": conversations}
