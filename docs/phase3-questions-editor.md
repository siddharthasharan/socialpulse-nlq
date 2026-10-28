# Phase 3 questions editor + in-chat corrections

Both in one interface, added 2026-10-28 after the Phase 3 chat fix-2.

## The editor (web UI, "Questions editor" section)

Three tabs, one panel, three files — and the tabs are separate on purpose:

| Tab | File | What it changes |
|---|---|---|
| Flow | `chat/questions.json` | What the chat asks/recommends: topic labels, diagnosis priority, recommendation hypothesis/intervention/primary metric/guardrails/population. |
| Single-shot evals | `evals/benchmark_phase2.json` | What single-shot questions are graded on (b01–b37 today). |
| Conversation evals | `evals/conversation_phase3.json` | What multi-turn scripts are graded on (c1–c4 today). |

Editing the flow changes what the bot *does*; editing an eval changes
what you *expect* it to do. Keeping them in separate tabs stops you
grading the bot on a question you just taught it.

Saves go through `POST /api/questions` in `scripts/app.py`, which calls
`src/socialpulse_nlq/questions.py:save_questions`. That function
validates first (non-empty labels, unique eval ids, allowed
`expected_type` values, gold values numeric) and writes atomically
(temp file + replace), so a rejected save writes nothing. The editor
writes only those three files — never code, never SQL. A question
template is text, not a query; the planner's allowlist and guard still
apply to any metric query.

Edited flow takes effect for a **new** chat session, not the one already
open: `ChatSession` loads `chat/questions.json` (merged over the
defaults in `questions.py`, so a partial file cannot delete a topic)
when it is created. Use the editor's "Start new chat with updated
questions" button to test an edit immediately. Existing sessions keep
the flow they started with.

Adding a single-shot eval: fill in the question, expected type, and
expected metric, save, then run `scripts/run_benchmark.py` and copy
the actual value into the gold field if it differs. The benchmark
runner, not the editor, computes golds — the editor only records them.

## In-chat corrections (multi-turn, conversational)

If a chat step is wrong, correct it in chat instead of restarting:

- "you are not looking at retention, look at retention instead" →
  switches the drill-down to retention, re-runs it, and offers
  diagnosis / recommendation / brief as the next steps.
- "you are not looking at whatsapp, use whatsapp instead" → switches
  the weather report to Whatsapp, without needing "how is…?".
- "that's wrong, show daily not average" → re-runs the current
  drill-down at daily grain: for growth, creators, and messaging the
  reply lists the actual daily values over the current period; for
  retention, engagement, and concentration Phase 3 has no daily
  series, and the reply says so instead of inventing one.
- "you are not looking at android, use android instead" → stores
  `platform=android` and re-runs the drill-down series filtered by
  it. Filters are country (full name only, e.g. india — the code
  "in" is also an English word), platform, surface, and acquisition,
  all from the planner's allowlists.

Honest scope, stated in the reply: **weather cards are app-scoped
only; only the drill-down series is filtered, not the cards.** A
correction with no recognisable parameter does not guess — it asks
which parameter you mean (app, topic, grain, filter) and prompts the
next step. "What's the diagnosis?" is not hijacked as a correction.

## Tests

- `tests/test_questions.py`: shipped file equals defaults, partial
  file merges over defaults, bad save rejected and writes nothing,
  eval validators reject duplicate ids / empty turns.
- `tests/test_chat.py` (correction tests, real seed-42 data): topic
  switch, app switch without "how is", daily grain for growth (28
  daily values), platform filter with the app-scoped-cards note,
  no-parameter clarification, and diagnosis not hijacked.
- Existing suites unchanged: chat 7/7 + corrections, weather 7/7,
  planner 10/10, conversation benchmark 4/4, single-shot benchmark
  37/37 (re-run after sync; the editor does not change any eval
  golds by itself).
