# Turn-by-turn Step 1 — intent + slots + context (2026-10-29)

Design approved 2026-10-29 with three amendments: (1) some turn
types are still missing — try with the designed list first, add
later; (2) country/surface is the first decomposition, ahead of
creator cohorts; (3) experiment assignment tables stay out, and
scale stays at Phase 1. This step implements the design's Step 1
plus the country/surface breakdown (the amendment), with no
generator change and no new data.

## What changed

`src/socialpulse_nlq/chat.py` only, plus evals:

- **Weather intent, not weather keywords.** `WEATHER_PHRASES`
  now includes "current state", "state of", "status", "overview",
  "summary", "report", "update on", "how's", "what's up with".
  "What is the current state of Instagram" → Instagram weather
  (DAU 4,544, D7 23.2%, creators 456). A weather phrase with no
  app but a session app reuses that app; with no app at all it
  asks which app, offering the three apps as buttons.
- **Follow-ups inherit context.** "What about WhatsApp?" after
  an Instagram weather report → WhatsApp weather (same intent,
  new app slot). "What about creators?" / "and retention" →
  that drill-down on the current app. A bare app name
  ("instagram") alone is a weather request. "Same for WhatsApp"
  after a creators drill-down re-weathers the new app and then
  re-runs the same drill-down on it, so the reply's numbers are
  the new app's, not the old app's carried over.
- **Breakdown turn (new type `breakdown`).** "creators by
  country", "creators by surface", "growth by country",
  "messaging by country" — real DuckDB queries over the weather
  current period (last 28 days), sorted descending, with shares.
  Country comes from `dim_users.country`; surface from
  `dim_content.surface`. Surface only exists for content topics
  (creators, engagement); anything else clarifies with the
  supported pairs instead of inventing one.
- **Why-evidence turn.** "What's driving the creator drop?",
  "why is retention down?" → a breakdown, never a cause. Creators
  gets both country and surface in one reply; growth/messaging
  get country. The text says, in words, that the decomposition
  shows where the numbers sit and does not prove why they moved.
- **Segment follow-up.** "creators in India", "only India",
  "for android" — once a weather report exists — applies that
  filter to the drill-down (reusing the correction machinery and
  its honest note: weather cards stay app-scoped, only the series
  is filtered). A surface filter on a non-content drill-down
  clarifies instead of pretending the series was filtered.
- **Clarification improved.** The fallback now names the new
  phrasings ("current state", "what about WhatsApp?", "creators
  by country", "what's driving the creator drop?") instead of
  only "What's happening with Instagram?".

## What did NOT change

- No generator change, no new tables, no creator cohorts yet
  (deferred — country/surface was chosen first).
- No experiment assignment tables, so past-experiment evaluation
  still clarifies (c3 still passes, and c7 re-checks it after
  the new turns).
- No scale change (Phase 1, 50K users, Jan–Mar 2026).
- No server change (`POST /api/chat` unchanged), no Render
  config change, no planner/single-shot change.
- Compare-apps ("which app is growing fastest?") and period
  follow-ups ("last month") are NOT in this step — they are
  Step 2. Asking them now still clarifies, honestly.

## Known limitation, stated plainly

A filtered drill-down's note says the series is filtered, but
the filtered daily series itself only appears when the grain is
daily ("show daily, not average"). So "creators in India" shows
app-wide cards plus the filter note; say "show daily" next and
the daily series is the filtered one. Fixing that presentation
is Step 2 work, not hidden here.

## Evals

- Conversation benchmark 4 → 7 cases: c5 (current state →
  what about WhatsApp → creators by country → why creators),
  c6 (same drill-down, new app → creators by surface), c7
  (creators in India → past-experiment refusal still holds).
  `scripts/run_conversation_benchmark.py` now extracts the topic
  from a `breakdown` reply as well as drill-down/diagnosis/
  recommendation/brief.
- `tests/test_chat.py`: stub tests for current-state weather,
  what-about app inheritance, same-for-app drill-down; real-data
  tests for country/surface breakdowns (sorted, shares sum to
  1, no cause claim) and the India segment follow-up.

## How to verify

1. Sync, commit, push, wait for Render Live, Incognito.
2. "What is the current state of Instagram" → weather, not
   clarification.
3. "What about WhatsApp?" → WhatsApp weather.
4. "Creators by country" → a sorted country list with shares.
5. "What's driving the creator drop?" → country + surface
   lists, with the "not a cause" sentence.
6. "Creators in India" → creators drill-down, filter note
   country=IN.
