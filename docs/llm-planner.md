# LLM planner — chunk B, slices 1–2 (2026-11-11)

The LLM planner (`src/socialpulse_nlq/llm_planner.py`) adds a fourth
planner variant, `llm`, to the frozen v8 benchmark. Slice 1 is the LLM
planner under the common contract, owner-key only. Slice 2 wires it
into the live demo (owner key + honest shell status + visitor BYOK);
the hybrid router is a later slice; the weekly chart / watch-outs turns
are parked, not part of this work.

## The design, in one paragraph

Grounded agent, explain-only. The LLM reasons about WHICH
deterministic analysis to run for a question and may add a one-sentence
explanation of its choice. It never computes a number, never writes
SQL, and never overrides a guardrail. The deterministic planner's SQL
templates and guard are the only things that execute: the model returns
a structured choice — {type, metric, app, breakdown, grain, filters} —
the choice is validated against the allowlist, and execution reuses
`build_sql` + `guard_sql` exactly as `plan()` does. If no key is
present or the API call fails, the planner falls back to the
deterministic baseline plan, clearly labeled in the trace.

## Pinned model

- Model: `gemini-3.8-flash` (Google AI Studio / Gemini API, free tier).
- Generation: temperature 0, maxOutputTokens 512, responseMimeType
  `application/json`. The call is `generateContent` on
  `generativelanguage.googleapis.com` — the Interactions API is the new
  default but is NOT required; generateContent remains fully supported.
- `gemini-2.5-flash` is NOT used anywhere: it returns 404 "no longer
  available to new users".

## Prompt contract

The pinned prompt is `src/socialpulse_nlq/llm_prompt.md`, version
`llm-prompt-v1`. The code renders it by substituting three
placeholders — `{inventory}`, `{context}`, `{question}` — and records
the version plus the SHA-256 of the prompt file in every trace, plus
the SHA-256 of the rendered inventory, so a published run can be
re-derived exactly. Do not edit the prompt file without bumping the
version: a changed prompt is a different study condition, not a typo
fix.

The capability inventory is generated at prompt-build time from
`planner.METRIC_DEFINITIONS` plus a mechanical probe of `build_sql`
(which filters and breakdowns each template actually honors), so the
inventory cannot drift from what the engine does. The prompt also
carries the conversation context the deterministic chat would have
(session app, stage, sticky MAU context, series window).

The prompt's rules, in short: reply with one JSON object; individual
users / rows / personal data → clarification; instruction-override
attempts → clarification; months outside Jan–Mar 2026 → clarification;
experiment evaluation (lift/change/improve) → clarification, because
assignment tables do not exist; a breakdown or scope a metric does not
support → clarification, never the all-population value wearing a
scoped label; grain "series" only for by-day / over-time / trend
questions, "scalar" otherwise (and always for "mean"/"average").

## What the model can and cannot choose

The choice schema: `type` in {query, weather_report, experiment_list,
experiment_info, clarification, refusal}; `metric` in
`METRIC_DEFINITIONS`; `app` in {instagram, whatsapp, facebook};
`breakdown` in {app, country, platform, surface, acquisition};
`grain` in {scalar, series}; filters limited to country (US/IN/BR/ID/
MX/GB/DE), platform, surface, acquisition, and date_start/date_end
inside 2026-01-01..2026-03-31. Validation failures are clarifications,
never executions. `experiment_info` is limited to the two experiments
the deterministic planner has info text for (Reels ranking,
new creator boost).

Deterministic policy pre-checks run BEFORE the model is consulted,
so it cannot override them: empty question, unsafe-write words
(refusal, same list and text as `plan()`), TikTok / countries outside
the synthetic universe (clarification), the aggregate route for raw
row-list queries, and a guardrail-override pre-check
(instruction-override phrases and individual-row / PII requests get
the generic "could not map" clarification — the same text the
deterministic planner returns for h20–h24, so those cases still pass
under the llm variant). The only planner change this slice needs is
an optional `series` parameter on `build_sql` so the model's grain
choice is honored for `dau_mean_daily`; default behavior is unchanged
(pinned by a test).

## Key handling

- Read from the `GEMINI_API_KEY` environment variable only (or passed
  in by a caller that owns it). Never hardcoded, never logged, never
  committed, never put in a trace file or a test. The transport sends
  it as the `x-goog-api-key` header, never in a URL.
- The study key is set as a Render env var by the user, never in the
  repo. The demo's visitor BYOK (visitor key in browser session
  storage only, browser calls the provider directly) is a later slice
  and is NOT built here.

## Fallback rules

Every failure falls back to the deterministic baseline `plan()`
result, with the reason in the trace's `llm` fields:

| fallback | meaning |
|---|---|
| `no_key` | no `GEMINI_API_KEY` set and none passed in |
| `api_error` | HTTP error / timeout / transport failure (status + detail in `fallback_reason`) |
| `bad_response` | the model reply could not be parsed as JSON |

A rejected choice (unknown metric/app/breakdown/filter, out-of-window
date) is NOT a fallback: it is a clarification naming the failed
check, and the tokens spent are still recorded. A question answered
by a deterministic pre-check records `not_consulted` (empty question,
unsafe words, TikTok, invalid country, override attempt, aggregate
route) — the model never saw it.

## Cost method

Tokens are the REAL values from the API response `usageMetadata`
(`promptTokenCount`, `candidatesTokenCount`, `totalTokenCount`) —
never estimates. Two money figures are recorded per call:

- `cost_usd_paid_rate`: prompt/1M × $0.75 + completion/1M × $3.75
  (the published paid rates for gemini-3.8-flash through Dec 31 2026).
  This is a paid-rate figure, never a billed figure.
- `billed_usd`: $0.00 on the free tier; equal to the paid-rate cost on
  the paid tier. The tier is a runner-declared fact about the key's
  AI Studio project (`--tier free|paid`, default free) — the code
  cannot detect it, so it must be declared honestly.

The comparison script (`scripts/compare_llm.py`) prints both, plus
cost per successful task at paid rates and billed. A full 162-case
run at paid rates is on the order of a dollar; on the free tier the
billed cost is $0 and the constraint is the request cap, not money.

## Free-tier pacing

The Gemini free tier caps requests per minute and per day (exact caps
are shown per project in AI Studio, not published). The runner
respects them:

- Sequential calls only, `--pace` seconds between calls (default 1.0).
- The transport retries ONCE on HTTP 429 (rate limit) or HTTP 503
  (the model is overloaded — observed live 2026-11-11: "model
  experiencing high demand, try again later"), waiting Retry-After
  when present, else 20s, then falls back and records the retry.
  Default call timeout is 120s: the free tier under load is slow
  (two 60s timeouts observed live 2026-11-11), so 60s would turn
  slow calls into fallbacks.
- `--limit N` / `--offset N` run a slice of the ordered case list, so
  a full 162-case run can be spread across days; partial runs are
  labeled `partial: true` in the trace and in the comparison output.
- The trace records every call's `retried_429` / `retry_wait_s`.

## Harness and comparison

- `scripts/run_benchmark_v8.py --variant llm [--split dev|heldout|all]
  [--limit N] [--offset N] [--tier free|paid] [--pace SECONDS]` runs
  the frozen v8 benchmark (162 cases, 132 dev / 30 held-out) with the
  LLM planner. Without a key it warns loudly and every case falls
  back (labeled `fallback=no_key`) — those numbers are baseline
  numbers, NOT LLM numbers.
- The per-case trace has the same shape as the deterministic trace (a
  list of steps) plus an llm step carrying the model id, prompt
  version + SHA-256, inventory SHA-256, real tokens, paid-rate cost,
  billed cost, latency, the raw model choice, and any fallback reason.
  Conversation turn traces carry the same receipt from the reply's
  working payload.
- `scripts/compare_llm.py <baseline-trace> <llm-trace>` prints
  per-family task success, accuracy / coverage / abstention (same
  definitions as `scripts/compare_variants.py`, singles only), the
  paired flip list, P50/P95 turn latency (SQL + model call), and cost
  per successful task. It REFUSES to compare a trace containing any
  fallback (exit non-zero) unless `--allow-fallbacks` is passed, in
  which case the output is labeled as containing fallbacks throughout.

## Slice 2 — live-demo wiring (2026-11-11)

The demo (`scripts/app.py`, `/shell` + `/api/chat`) now uses the LLM
planner in exactly one place: the fallback turn. The design decision,
recorded because the brief's literal wording said otherwise: the demo
session is constructed with the owner key (`llm_api_key`, tier from
`GEMINI_TIER` or `free`) but its variant stays `"baseline"`. Setting
`variant="llm"` would route the five deterministic turns' internal
planning through the model — they call `_plan()` with synthetic
canonical questions ("MAU and creators for instagram by country"), and
a model returning a different plan shape would break the turn that
unpacks it. So the deterministic turns keep planning deterministically
(zero model calls — pinned by test), and the LLM is consulted only when
no deterministic turn matches a message.

- **Owner key.** `app.py` reads `GEMINI_API_KEY` from the environment
  at session creation. When present, the session has an LLM available;
  when absent, the demo is exactly today's deterministic demo. The key
  is never logged, never returned in any response, and never written to
  a trace. The planner's fallback holds end-to-end: no key / API error
  / timeout / bad response → the deterministic baseline plan, labeled
  `fallback` in the receipt.
- **LLM fallback turn.** In `ChatSession.respond()`, just before the
  generic "could not map" clarification: when an LLM is available, the
  question goes through `plan_with_llm` and the returned plan is
  executed through `_render_llm_plan` — the same working-panel path as
  every other turn (exact SQL, result table/chart, metric definition,
  llm receipt). Query / weather_report / experiment_list /
  experiment_info / clarification / refusal are all handled; a
  clarification or refusal shows its text and never executes. Every
  fallback reply carries `"engine": "llm"` (deterministic turns carry
  `"engine": "deterministic"`); the generic clarification carries
  `"unmapped": true` so the shell knows BYOK may try it. Every reply
  also carries `"context"` (app, stage, mau_context, series_window)
  for the BYOK prompt.
- **Honest scope in the answer text.** `_llm_query_text` states only
  the scope the executed SQL actually honors (same needle rule as the
  inventory probe). A requested filter the template drops is named
  after the number, so the answer never wears a narrower label than
  the SQL earned. See the WAU note below — this is the h07 trap, and
  the fallback turn is built to surface it, not hide it.
- **Honest shell status.** `GET /api/shell-config` now returns
  `"llm": {"configured", "model", "mode", "prompt_version"}` alongside
  `copilot_base`. The shell's Analytics note reads "LLM-routed (Gemini
  3.8 Flash)" when a key is configured and "Deterministic — no live
  LLM" when not; each answer's transcript label names its router.
- **Visitor BYOK.** A key field in the shell sidebar (Analytics only).
  The key lives only in the visitor's browser `sessionStorage` and is
  never sent to this backend. The browser fetches the pinned routing
  prompt from `GET /api/llm-routing-prompt` (rendered inventory +
  version + model id + pinned generation params, with `{question}` and
  `{context}` left to fill), substitutes the question and the session
  context, calls Gemini `generateContent` directly, parses the JSON
  choice, and POSTs `{message, llm_choice, llm_tokens}` to `/api/chat`.
  The backend validates the choice with `_validate_choice` and executes
  it with `plan_from_choice` — the same validation and the same
  dispatch `plan_with_llm` uses (factored out in this slice, so the two
  paths cannot drift). An invalid choice is a clarification, never an
  execution. The reply carries `"engine": "llm-byok"` and a receipt with
  the real token counts the browser posted. With no key anywhere, the
  demo behaves exactly as today. A jailbreak choice can at most produce
  an aggregate answer: execution is always through allowlisted
  templates, never raw SQL.
- **WAU acceptance — the honest outcome.** "What's the WAU for
  Instagram?" is the acceptance question because the deterministic
  chat has no standalone WAU turn. The engine has NO app-scoped WAU
  scalar: `wau_mean_weekly` honors no filters (inventory: filters
  none), so there is no "Instagram WAU number" for the fallback to
  return as a scalar. What the fallback does, verified with mocked
  transport (19 tests in `tests/test_demo_llm.py`):
  - a rule-following model clarifies (the h07 expected behavior);
  - a model that picks `wau_mean_weekly` + instagram executes the
    template and the answer says "267,348.93 distinct users (all apps,
    2026-01-01 to 2026-03-31)" plus "it is not the instagram's number"
    — the all-apps value, labeled as such, never Instagram's;
  - a model that picks the weather report returns the real Instagram
    WAU: the weather card's mean weekly WAU is 147,138.5 users/week
    (last 4 complete weeks, prior 136,624.0, +7.7%);
  - an API error falls back to the deterministic plan, labeled
    `api_error`.
  Adding a genuinely app-scoped WAU scalar would be a planner change
  (it would also change h07's expected behavior and the v8 comparison
  baseline) — a separate decision, not taken in this slice.

## Live verification status (demo wiring)

Live verification is PENDING — the key is now accepted, but
generation has not yet been observed working. Timeline 2026-11-11:
the key was connected and valid (list-models returns HTTP 200), but
generateContent returned HTTP 403 PERMISSION_DENIED — a Google
free-tier project/account restriction. The user fixed it ('done',
14:25 PST): the 403 is GONE. Three test calls since: the first timed
out at 60s, the second returned HTTP 503 "model experiencing high
demand, try again later", the third timed out at 60s. Requests reach
the model; the model is overloaded/slow right now. Retry later. The
transport's 120s timeout and retry-once on 429/503 exist for exactly
this: slow or overloaded free-tier calls get a second chance instead
of an immediate fallback. Last retest 2026-11-11 15:21 PST timed out
at 60s.

Until a live call succeeds:

- `scripts/smoke_llm.py` (5 cases: p11, p08, b01, h20, h30) reports a
  403 plainly as a BLOCKED key (exit 3), a timeout/503 fallback as
  BLOCKED (exit 3), and never counts a fallback pass as a live pass.
- The mocked-transport tests (`tests/test_llm_planner.py`, 20 tests)
  are the verification actually completed: choice execution through
  `build_sql`, grain handling, validation rejections, the
  guardrail-override pre-check, fallbacks, token/cost recording, the
  prompt contract, the 503 retry, and the harness/chat dispatch.
- When the model answers, run the smoke script first (5 cases, ~1
  minute), then the full comparison run on the frozen v8 set.

`scripts/smoke_demo_llm.py` is the live check for the wired demo:
it starts the server with the owner key, checks `/api/shell-config`
and `/api/llm-routing-prompt`, asks "What's the WAU for Instagram?"
through the owner-key path, then simulates the BYOK path (fetch the
prompt, call Gemini directly, post the choice). Exit codes match
`smoke_llm.py`: 0 all live passes, 1 a live failure, 2 no key
(pending), 3 blocked (the model did not run — a fallback is never
counted as a pass). It needs the owner to set `GEMINI_API_KEY` as a
Render env var before the live demo can be checked on Render.

## What the hybrid router will add later (not this slice)

The hybrid router (deterministic first, LLM fallback with a routing
policy) is the next slice. The fallback turn built here is its
mechanism; the router adds the policy decision (which questions go to
the LLM, when to abstain) on top.
