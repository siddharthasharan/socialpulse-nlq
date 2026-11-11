"""LLM planner for SocialPulse NLQ (chunk B, slice 1).

Grounded-agent design, agreed and binding: the LLM reasons about
WHICH deterministic analysis to run and explains the engine's numbers
only. It NEVER computes a number, NEVER writes SQL, and NEVER
overrides a guardrail. The deterministic planner's SQL templates and
guard are the only things that execute.

How a question flows through plan_with_llm():
  1. Deterministic policy pre-checks, identical in effect to plan():
     empty question, unsafe-write words (refusal), TikTok / countries
     outside the synthetic universe (clarification), the aggregate
     route for raw row-list queries, and a guardrail-override pre-check
     (instruction-override phrases and individual-row / PII requests
     get the generic clarification — the model is never consulted on
     those, so a jailbreak cannot reach execution).
  2. The LLM is asked for a structured choice — {type, metric, app,
     breakdown, grain, filters, explanation} — against the capability
     inventory generated from planner.METRIC_DEFINITIONS plus a
     mechanical probe of build_sql (which filters and breakdowns each
     template actually honors). The prompt is the pinned contract in
     llm_prompt.md (version llm-prompt-v1); its version and SHA-256
     are recorded in every trace.
  3. The choice is VALIDATED against the allowlist (unknown metric /
     app / breakdown / filter, dates outside 2026-01-01..2026-03-31):
     a failed check is a clarification, never an execution.
  4. Execution reuses the deterministic path — the mau_march ->
     mau_by_country switch, build_sql, guard_sql — exactly as plan()
     does. The only planner change this slice needs is an optional
     `series` parameter on build_sql so the model's grain choice
     (scalar vs series) is honored for dau_mean_daily; default
     behavior is unchanged.

Failure is always a deterministic fallback, clearly labeled: no key,
an API error, a timeout, or a malformed model reply all fall back to
the baseline plan() result, with the reason recorded in the trace's
llm fields. Nothing in this module ever logs, stores, or returns the
API key: it is read from the GEMINI_API_KEY environment variable
(or passed in by a caller that owns it) and sent only as the
x-goog-api-key header, never in a URL.

Costs: tokens are the REAL values from the API response
usageMetadata — never estimates. Cost is computed at the published
paid rates for gemini-3.8-flash ($0.75 input / $3.75 output per 1M
tokens through Dec 31 2026) and labeled as a paid-rate cost; on the
free tier the actual billed cost is $0, recorded separately as
billed_usd. See docs/llm-planner.md for the full cost method.
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from .planner import (
    APPS,
    DATA_END,
    DATA_START,
    INVALID_COUNTRIES,
    METRIC_DEFINITIONS,
    _aggregate_route,
    _experiment_plan,
    _extract_filters,
    _word,
    build_sql,
    guard_sql,
    plan,
)

MODEL_ID = "gemini-3.8-flash"
PROMPT_VERSION = "llm-prompt-v1"
PROMPT_FILE = Path(__file__).resolve().parent / "llm_prompt.md"
API_KEY_ENV = "GEMINI_API_KEY"
# Published paid rates for gemini-3.8-flash, per 1M tokens, through
# Dec 31 2026 (Google AI Studio / Gemini API pricing page). Used ONLY
# to compute the paid-rate cost figure; the free tier bills $0.
PAID_INPUT_PER_1M = 0.75
PAID_OUTPUT_PER_1M = 3.75
MAX_OUTPUT_TOKENS = 512

_GENERIC_CLARIFICATION = (
    "I could not map that question to a catalog metric. Try a specific metric, for example: "
    "DAU, WAU, MAU, D7 retention, daily creators, orphan post rate, engagement rate, "
    "Reels watch time, or messaging DAU, optionally with 'by app', 'by country', or 'over the last 28 days'.")
_REFUSAL_TEXT = "I only run read-only SELECT queries. I will not run write or control statements."

# Unsafe words, copied from planner.plan() (the refusal list there).
# Kept in sync by test: test_llm_planner pins plan() and
# plan_with_llm() to the same refusal on the same questions.
_UNSAFE_WORDS = ("delete", "drop table", "drop database", "drop the database",
                 "update ", "insert into", "remove all", "truncate")
# Guardrail-override pre-check: phrases that ask the model to ignore
# instructions, role-play a privileged user, or hand over individual
# rows / personal data. These never reach the model: the question is
# answered with the same generic clarification the deterministic
# planner returns for them (h20–h24 expect exactly that text), so a
# jailbreak cannot change the answer by changing the model.
_OVERRIDE_PHRASES = (
    "ignore the guardrail", "ignore previous instructions",
    "ignore all instructions", "ignore your instructions",
    "disregard previous instructions", "disregard all instructions",
    "pretend you are", "pretend to be", "you are a database admin",
    "dump the users table", "print the full user table",
    "show me all individual users", "list every user",
    "every user's email", "raw data for user",
)
_ALLOWED_TYPES = ("query", "weather_report", "experiment_list",
                  "experiment_info", "clarification", "refusal")
_ALLOWED_BREAKDOWNS = ("app", "country", "platform", "surface", "acquisition")
_COUNTRY_CODES = ("US", "IN", "BR", "ID", "MX", "GB", "DE")
# experiment_info answers the model can choose: the two experiments
# _experiment_plan has an info dict for, keyed by registry primary
# metric. The third registry experiment (two_way_conversation_rate)
# has no info dict in the deterministic planner, so the model cannot
# choose it — the validator clarifies instead.
_EXPERIMENT_INFO_QUESTIONS = {
    "reels_watch_time_per_viewer_day": "reels ranking primary metric",
    "new_creator_d7_retention": "new creator primary metric",
}
_FILTER_VALUES = {"app": "instagram", "country": "US", "platform": "android",
                  "surface": "reels", "acquisition": "organic"}
_FILTER_NEEDLES = {
    "app": ("primary_app = 'instagram'", "app = 'instagram'",
            "c.app = 'instagram'", "i.app = 'instagram'"),
    "country": ("country = 'US'", "u.country = 'US'"),
    "platform": ("platform = 'android'", "u.platform = 'android'"),
    "surface": ("surface = 'reels'", "c.surface = 'reels'",
                "i.surface = 'reels'"),
    "acquisition": ("acquisition_source = 'organic'",),
}
_BREAKDOWN_TOKENS = {"app": ("primary_app",), "country": ("country",),
                     "platform": ("platform",), "surface": ("surface",),
                     "acquisition": ("acquisition_source",)}
_PLATFORMS = ("android", "ios", "web")
_SURFACES = ("reels", "feed", "stories")
_ACQUISITIONS = ("organic", "referral", "paid")


class LLMError(Exception):
    """A call to the provider failed (HTTP error, timeout, bad body)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.message = message
        self.status = status


class UrllibGeminiTransport:
    """Stdlib-urllib transport for the Gemini API (generateContent).

    The key is sent as the x-goog-api-key header, never in a URL and
    never logged. Retries ONCE on a transient failure — HTTP 429 (rate
    limit) or HTTP 503 (the model is overloaded: "experiencing high
    demand, try again later", observed live 2026-11-11) — waiting
    Retry-After when present, else retry_delay_s, then raises
    LLMError; the adapter falls back to the deterministic planner.
    `sleep` is injectable so tests can pass a no-op instead of waiting
    out a real backoff. Default timeout 120s: the free tier under load
    is slow (two 60s timeouts observed live 2026-11-11), so 60s would
    turn slow calls into fallbacks.
    """

    def __init__(self, model=MODEL_ID, timeout=120, retry_delay_s=20, sleep=None):
        self.model = model
        self.timeout = timeout
        self.retry_delay_s = retry_delay_s
        self._sleep = sleep or time.sleep

    @property
    def url(self):
        return ("https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent")

    def generate(self, prompt, api_key):
        payload = json.dumps({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0,
                                 "maxOutputTokens": MAX_OUTPUT_TOKENS,
                                 "responseMimeType": "application/json"},
        }).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "x-goog-api-key": api_key}
        retried = False
        retry_status = None
        wait_s = 0.0
        for attempt in (1, 2):
            req = urllib.request.Request(self.url, data=payload,
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                return {"response": body, "retried": retried,
                        "retry_status": retry_status,
                        "retried_429": retry_status == 429,
                        "retry_wait_s": wait_s}
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:500]
                except Exception:  # noqa: BLE001 - error body is best-effort
                    detail = ""
                if e.code in (429, 503) and attempt == 1:
                    wait_s = self._retry_wait(e)
                    self._sleep(wait_s)
                    retried = True
                    retry_status = e.code
                    continue
                raise LLMError(f"Gemini API HTTP {e.code}: {detail}",
                               status=e.code)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise LLMError(f"Gemini API request failed: {e}")
            except (ValueError, UnicodeDecodeError) as e:
                raise LLMError(f"Gemini API returned an unreadable body: {e}")
        raise LLMError("Gemini API request failed after retry")

    def _retry_wait(self, http_error):
        raw = None
        try:
            raw = http_error.headers.get("Retry-After")
        except Exception:  # noqa: BLE001 - header read is best-effort
            raw = None
        try:
            return max(0.0, float(raw)) if raw else self.retry_delay_s
        except (TypeError, ValueError):
            return self.retry_delay_s


# --------------------------------------------------------------------------
# Prompt contract
# --------------------------------------------------------------------------

_PROMPT_CACHE = {}


def _load_prompt_template():
    """The pinned template: the part of llm_prompt.md after the first
    '---' separator (the header above it is contract documentation,
    not prompt text). Cached; the SHA-256 is over the whole file, so
    any edit to the contract — header or template — changes the hash
    recorded in the trace."""
    if "template" in _PROMPT_CACHE:
        return _PROMPT_CACHE["template"]
    text = PROMPT_FILE.read_text()
    parts = text.split("\n---\n", 1)
    template = parts[1] if len(parts) == 2 else text
    _PROMPT_CACHE["template"] = template
    _PROMPT_CACHE["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return template


def prompt_sha256():
    _load_prompt_template()
    return _PROMPT_CACHE["sha256"]


def _metric_capabilities():
    """{metric: {"filters": [...], "breakdowns": [...]}} — derived by
    probing build_sql through _execute_choice, the same code path that
    executes a validated choice, so the inventory cannot drift from
    what the engine does. A dimension is listed exactly when the
    template's SQL applies that filter (same needle rule as the
    catalog-scope test in tests/test_planner.py); a breakdown is
    listed exactly when the executed query groups by that dimension.
    """
    base = {"date_start": DATA_START, "date_end": DATA_END}
    caps = {}
    for metric in METRIC_DEFINITIONS:
        filters = []
        for dim, value in _FILTER_VALUES.items():
            f = dict(base, **{dim: value})
            try:
                sql, _tables, _grain = build_sql(metric, f, None, "inventory probe")
            except ValueError:
                continue
            if any(n in sql for n in _FILTER_NEEDLES[dim]):
                filters.append(dim)
        breakdowns = []
        for b in _ALLOWED_BREAKDOWNS:
            p = _execute_choice(metric, dict(base), b, "inventory probe")
            if p.get("type") != "query":
                continue
            sql = p.get("sql") or ""
            if "GROUP BY" not in sql:
                continue
            group_by = sql.split("GROUP BY", 1)[1]
            if any(tok in group_by for tok in _BREAKDOWN_TOKENS[b]):
                breakdowns.append(b)
        caps[metric] = {"filters": filters, "breakdowns": breakdowns}
    return caps


def _build_inventory():
    """The capability inventory text for the prompt, generated from
    METRIC_DEFINITIONS + the mechanical capability probe. One line per
    metric: id, definition, grain, filters honored, breakdowns."""
    lines = []
    for metric, (definition, grain, _den) in METRIC_DEFINITIONS.items():
        cap = _metric_capabilities().get(metric, {"filters": [], "breakdowns": []})
        lines.append(
            f"- {metric} — {definition} Grain: {grain}. "
            f"Filters honored: {', '.join(cap['filters']) or 'none'}. "
            f"Breakdowns: {', '.join(cap['breakdowns']) or 'none'}.")
    return "\n".join(lines)


def _format_context(context):
    if not context:
        return "No conversation context — this is the first question."
    lines = []
    if context.get("app"):
        lines.append(f"- app: {context['app']}")
    if context.get("stage"):
        lines.append(f"- stage: {context['stage']}")
    mc = context.get("mau_context")
    if mc:
        lines.append(f"- MAU context: app {mc.get('app')}, March 2026 "
                      f"({mc.get('start')}..{mc.get('end')})")
    sw = context.get("series_window")
    if sw:
        lines.append(f"- last daily series window: {sw[0]}..{sw[1]}")
    return "\n".join(lines) if lines else "No conversation context — this is the first question."


def build_prompt(question, context=None):
    """Render the pinned prompt for one question. Returns
    (prompt_text, inventory_sha256) — the inventory hash pins exactly
    what the model saw, alongside the prompt-file hash in the trace."""
    template = _load_prompt_template()
    inventory = _build_inventory()
    prompt = (template
              .replace("{inventory}", inventory)
              .replace("{context}", _format_context(context))
              .replace("{question}", question))
    return prompt, hashlib.sha256(inventory.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Choice validation + deterministic execution
# --------------------------------------------------------------------------

def _validate_choice(choice):
    """(ok, reason, cleaned) for a parsed model choice. Every failure
    is a clarification downstream — the model is never trusted to have
    named a real metric, app, breakdown, filter, or in-window date."""
    if not isinstance(choice, dict):
        return False, "model choice is not a JSON object", None
    ctype = choice.get("type")
    if ctype not in _ALLOWED_TYPES:
        return False, f"unknown choice type {ctype!r}", None
    app = choice.get("app")
    if app is not None and app not in APPS:
        return False, f"unknown app {app!r}", None
    breakdown = choice.get("breakdown")
    if breakdown is not None and breakdown not in _ALLOWED_BREAKDOWNS:
        return False, f"unknown breakdown {breakdown!r}", None
    grain = choice.get("grain")
    if grain is not None and grain not in ("scalar", "series"):
        return False, f"unknown grain {grain!r}", None
    filters = choice.get("filters") or {}
    if not isinstance(filters, dict):
        return False, "filters is not an object", None
    for key, value in filters.items():
        if key == "country" and value not in _COUNTRY_CODES:
            return False, f"unknown country code {value!r}", None
        if key == "platform" and value not in _PLATFORMS:
            return False, f"unknown platform {value!r}", None
        if key == "surface" and value not in _SURFACES:
            return False, f"unknown surface {value!r}", None
        if key == "acquisition" and value not in _ACQUISITIONS:
            return False, f"unknown acquisition source {value!r}", None
        if key in ("date_start", "date_end"):
            try:
                d = date.fromisoformat(str(value))
            except ValueError:
                return False, f"bad date {key}={value!r}", None
            if not (date.fromisoformat(DATA_START) <= d <= date.fromisoformat(DATA_END)):
                return False, (f"{key}={value} is outside the data window "
                               f"({DATA_START}..{DATA_END})"), None
        elif key not in ("country", "platform", "surface", "acquisition"):
            return False, f"unknown filter {key!r}", None
    ds, de = filters.get("date_start"), filters.get("date_end")
    if ds and de and date.fromisoformat(str(ds)) > date.fromisoformat(str(de)):
        return False, f"date_start {ds} is after date_end {de}", None
    if ctype == "query" and choice.get("metric") not in METRIC_DEFINITIONS:
        return False, f"unknown metric {choice.get('metric')!r}", None
    if ctype == "experiment_info" and choice.get("metric") not in _EXPERIMENT_INFO_QUESTIONS:
        return False, (f"no experiment info for metric "
                       f"{choice.get('metric')!r}"), None
    cleaned = {"type": ctype, "metric": choice.get("metric"),
               "app": app, "breakdown": breakdown, "grain": grain,
               "filters": dict(filters),
               "explanation": choice.get("explanation")}
    return True, "", cleaned


def _execute_choice(metric, filters, breakdown, question, series=None):
    """The deterministic execution path for a validated choice: the
    same mau_march -> mau_by_country switch and d7 breakdown rule as
    plan(), then build_sql + guard_sql. Returns a plan dict (query, or
    clarification when the template refuses the combination — the same
    text plan() returns for it)."""
    if metric == "mau_march" and breakdown == "country":
        metric = "mau_by_country"
    if metric == "d7_retention_by_acquisition":
        breakdown = None
    f = {"date_start": filters.get("date_start", DATA_START),
         "date_end": filters.get("date_end", DATA_END)}
    for dim in ("app", "country", "platform", "surface", "acquisition"):
        if dim in filters:
            f[dim] = filters[dim]
    try:
        sql, tables, grain = build_sql(metric, f, breakdown, question,
                                       series=series)
    except ValueError as e:
        return {"type": "clarification", "metric": metric, "sql": None,
                "tables": [], "grain": "", "explanation": "",
                "clarification": str(e)}
    ok, reason = guard_sql(sql)
    if not ok:
        return {"type": "clarification", "metric": metric, "sql": None,
                "tables": [], "grain": "", "explanation": "",
                "clarification": f"SQL guard rejected the planned query: {reason}"}
    return {"type": "query", "metric": metric, "sql": sql, "tables": tables,
            "grain": grain, "filters": f, "breakdown": breakdown,
            "explanation": (f"Metric {metric}, grain {grain}, filters {f}, "
                            f"breakdown {breakdown}."),
            "clarification": None}


def _parse_model_json(response):
    """The model's JSON choice out of a generateContent response.
    Tolerates markdown fences around the JSON; anything else raises
    ValueError and the adapter falls back."""
    candidates = (response or {}).get("candidates") or []
    if not candidates:
        raise ValueError("model response has no candidates")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


def cost_usd_paid_rate(prompt_tokens, completion_tokens):
    """Cost at the published paid rates ($0.75 input / $3.75 output
    per 1M tokens). None when the token counts are unknown. This is a
    paid-rate figure, never a billed figure — on the free tier the
    billed cost is $0 (see billed_usd in the trace)."""
    if prompt_tokens is None or completion_tokens is None:
        return None
    return (prompt_tokens / 1_000_000 * PAID_INPUT_PER_1M
            + completion_tokens / 1_000_000 * PAID_OUTPUT_PER_1M)


def _llm_skeleton(tier):
    return {"model": MODEL_ID, "prompt_version": PROMPT_VERSION,
            "prompt_sha256": prompt_sha256(), "tier": tier,
            "prompt_tokens": None, "completion_tokens": None,
            "total_tokens": None, "raw_usage": None,
            "cost_usd_paid_rate": None, "billed_usd": None,
            "latency_ms": None, "fallback": None, "fallback_reason": None,
            "retried": False, "retry_status": None,
            "retried_429": False, "retry_wait_s": 0.0,
            "not_consulted": None, "raw_choice": None,
            "model_explanation": None, "choice_rejected": None,
            "inventory_sha256": None}


def _with_llm(plan_dict, llm):
    plan_dict["llm"] = llm
    return plan_dict


def _clarification_plan(text, metric=None, llm=None):
    p = {"type": "clarification", "metric": metric, "sql": None,
         "tables": [], "grain": "", "explanation": "",
         "clarification": text}
    return _with_llm(p, llm) if llm is not None else p


def _refusal_plan(llm):
    p = {"type": "refusal", "metric": None, "sql": None, "tables": [],
         "grain": "", "explanation": "", "clarification": _REFUSAL_TEXT}
    return _with_llm(p, llm)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def plan_with_llm(question, context=None, transport=None, api_key=None,
                  tier="free", timeout=120):
    """Plan one question with the LLM router under the common contract.

    Returns a plan dict in exactly the shape plan() returns
    (type/metric/sql/tables/grain/filters/breakdown/explanation/
    clarification), plus an "llm" key with the model id, prompt
    version + SHA-256, real token counts from usageMetadata, paid-rate
    cost, latency, the raw model choice, and any fallback reason.

    context is the deterministic chat session's context (app, stage,
    mau_context, series_window) or None for a single-shot question.
    transport is an object with .generate(prompt, api_key) (default:
    UrllibGeminiTransport); tests inject a fake. api_key overrides the
    GEMINI_API_KEY environment variable. tier is "free" or "paid" and
    only decides billed_usd (free bills $0); it is a runner-declared
    fact about the key's AI Studio project, not something the code
    detects.
    """
    q = (question or "").lower().strip()
    llm = _llm_skeleton(tier)
    # -- Deterministic policy pre-checks (same effect as plan()). The
    # model is never consulted on these, so it cannot override them.
    if not q:
        llm["not_consulted"] = "empty_question"
        return _clarification_plan(
            "Ask a question about a specific metric, for example "
            "'What was DAU by app over the last 28 days?'", llm=llm)
    if any(w in q for w in _UNSAFE_WORDS):
        llm["not_consulted"] = "unsafe_words"
        return _refusal_plan(llm)
    if "tiktok" in q:
        llm["not_consulted"] = "tiktok"
        return _clarification_plan(
            "TikTok is not in the Phase 1 synthetic universe. The apps are "
            "instagram, whatsapp, and facebook.", llm=llm)
    for bad in INVALID_COUNTRIES:
        if _word(q, bad):
            llm["not_consulted"] = "invalid_country"
            return _clarification_plan(
                f"'{bad}' is not a country in the synthetic data. The countries "
                "are US, IN, BR, ID, MX, GB, and DE.", llm=llm)
    if any(phrase in q for phrase in _OVERRIDE_PHRASES):
        llm["not_consulted"] = "override_attempt"
        return _clarification_plan(_GENERIC_CLARIFICATION, llm=llm)
    routed = _aggregate_route(q, _extract_filters(q))
    if routed is not None:
        llm["not_consulted"] = "aggregate_route"
        return _with_llm(routed, llm)
    # -- The LLM call. No key, or any failure, falls back to the
    # deterministic baseline plan, with the reason in the trace.
    key = api_key if api_key is not None else os.environ.get(API_KEY_ENV)
    if not key:
        llm["fallback"] = "no_key"
        llm["fallback_reason"] = (
            f"no {API_KEY_ENV} set and no key passed in; "
            "deterministic baseline plan used instead")
        return _with_llm(plan(question), llm)
    prompt, inventory_sha = build_prompt(question, context)
    llm["inventory_sha256"] = inventory_sha
    transport = transport or UrllibGeminiTransport(timeout=timeout)
    t0 = time.time()
    try:
        result = transport.generate(prompt, key)
    except LLMError as e:
        llm["fallback"] = "api_error"
        llm["fallback_reason"] = str(e)
        llm["latency_ms"] = int((time.time() - t0) * 1000)
        return _with_llm(plan(question), llm)
    except Exception as e:  # noqa: BLE001 - any transport failure falls back
        llm["fallback"] = "api_error"
        llm["fallback_reason"] = f"transport failed: {e}"
        llm["latency_ms"] = int((time.time() - t0) * 1000)
        return _with_llm(plan(question), llm)
    llm["latency_ms"] = int((time.time() - t0) * 1000)
    llm["retried"] = bool(result.get("retried"))
    llm["retry_status"] = result.get("retry_status")
    llm["retried_429"] = bool(result.get("retried_429"))
    llm["retry_wait_s"] = float(result.get("retry_wait_s") or 0.0)
    try:
        choice = _parse_model_json(result.get("response") or {})
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        llm["fallback"] = "bad_response"
        llm["fallback_reason"] = f"could not parse the model reply as JSON: {e}"
        return _with_llm(plan(question), llm)
    llm["raw_choice"] = choice
    if isinstance(choice, dict):
        llm["model_explanation"] = choice.get("explanation")
    usage = (result.get("response") or {}).get("usageMetadata") or {}
    llm["raw_usage"] = dict(usage)
    llm["prompt_tokens"] = usage.get("promptTokenCount")
    llm["completion_tokens"] = usage.get("candidatesTokenCount")
    llm["total_tokens"] = usage.get("totalTokenCount")
    llm["cost_usd_paid_rate"] = cost_usd_paid_rate(llm["prompt_tokens"],
                                                   llm["completion_tokens"])
    llm["billed_usd"] = 0.0 if tier == "free" else llm["cost_usd_paid_rate"]
    # -- Validate the choice, then execute it deterministically.
    ok, reason, clean = _validate_choice(choice)
    if not ok:
        llm["choice_rejected"] = reason
        metric = choice.get("metric") if isinstance(choice, dict) else None
        return _clarification_plan(
            f"I am not answering that as asked: {reason}.", metric=metric,
            llm=llm)
    ctype = clean["type"]
    if ctype == "clarification":
        text = clean["explanation"] or _GENERIC_CLARIFICATION
        return _clarification_plan(text, metric=clean["metric"], llm=llm)
    if ctype == "refusal":
        return _refusal_plan(llm)
    if ctype == "weather_report":
        app = clean["app"]
        p = {"type": "weather_report", "metric": None, "sql": None,
             "tables": [],
             "grain": (f"product health for {app} (last 28 days vs prior 28 days)"
                       if app else "product health (last 28 days vs prior 28 days)"),
             "app": app,
             "explanation": (f"Weather report for {app}: DAU, WAU, retention, and creator health, "
                             "computed from the Phase 1 tables scoped to that app. No single score; see the cards and watchouts."
                             if app else
                             "Weather report: DAU, WAU, retention, and creator health, "
                             "computed from the Phase 1 tables. No single score; see the cards and watchouts."),
             "clarification": None}
        return _with_llm(p, llm)
    if ctype == "experiment_list":
        return _with_llm(_experiment_plan("list experiments"), llm)
    if ctype == "experiment_info":
        return _with_llm(
            _experiment_plan(_EXPERIMENT_INFO_QUESTIONS[clean["metric"]]), llm)
    series = {"scalar": False, "series": True}.get(clean["grain"])
    # The app is a top-level choice field; build_sql reads it from the
    # filters dict, so merge it in (a filters.app, if the model also
    # set one, must agree — the validator already checked both are
    # allowlisted, and the top-level app is the question's app).
    filters = dict(clean["filters"])
    if clean["app"]:
        filters["app"] = clean["app"]
    p = _execute_choice(clean["metric"], filters,
                        clean["breakdown"], question, series=series)
    return _with_llm(p, llm)
