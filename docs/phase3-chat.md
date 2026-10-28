# Phase 3 — multi-turn chat assistant

Phase 2 answered one question at a time. Phase 3 keeps state, so a
conversation means something: the app, the last weather report, and the
last topic carry from turn to turn. No LLM is involved; the dialogue is
a deterministic state machine in `src/socialpulse_nlq/chat.py`.

## The flow (the demo)

1. **Opening — weather, scoped.** "What's happening with Instagram?"
   returns the app-scoped weather report (Instagram: DAU 4,544, +4.8%;
   D7 23.2%, +2.6 points; daily creators 456, down) plus watchouts,
   then offers drill-downs generated from those watchouts.
2. **Drill-down — a/b/c.** Picking "creators" (letter or topic name)
   shows daily creators vs prior, penetration, orphan rate (whole
   window, level only), top-1% share, and creations by month
   (Instagram: Jan 37,268 posts → Feb 24,171 → Mar 16,372 — supply
   falling over the window).
3. **Diagnosis.** "What's the diagnosis?" picks the first problematic
   topic in a documented priority order — creators (orphan > 20% or
   supply down), then retention, then growth, then engagement — and
   states what is good alongside what is bad. For Instagram, growth
   and retention are up, so the diagnosis is creator-specific, not a
   general collapse. For Whatsapp, retention is also down, so the
   diagnosis says creators are first, not only.
4. **Experiment recommendation — HYPOTHESIS, not a forecast.**
   "What experiment would you recommend?" returns a hypothesis, an
   intervention, a population, a primary metric, and guardrails — and
   explicitly no lift. There are no experiment assignment tables in
   Phase 3, so a lift claim would be a causal claim without evidence.
   That is the same refusal Phase 2 gives for "did experiment X
   improve Y?", and the chat keeps it: that question clarifies, even
   mid-conversation.
5. **PM/Eng brief.** "Give me the PM/Eng brief" turns the same numbers
   into talking points: PM gets the problem, the hypothesis, and the
   success criteria to agree before launch; Eng gets what to build and
   instrument, and the no-peeking rule; both get the caveats out loud
   (synthetic Jan–Mar 2026 only, thresholds are project choices,
   orphan/top-1% are levels not trends).

## What is new vs Phase 2

- `src/socialpulse_nlq/chat.py` — `ChatSession` state machine, drill-down
  queries (monthly creations, daily DAU series), diagnosis priority,
  hypothesis templates, PM/Eng brief builder.
- `scripts/chat.py` — terminal chat loop (`python scripts/chat.py`).
- `scripts/app.py` — `POST /api/chat` with a per-session `ChatSession`
  (in-memory; restarting `app.py` resets chat history, by design for a
  local demo). The single-shot `/api/ask` UI is unchanged.
- `app/index.html` — Phase 3 chat panel, with option buttons that send
  the topic (or, for app choices, the full "What's happening with…?"
  question).
- `evals/conversation_phase3.json` + `scripts/run_conversation_benchmark.py`
  — scripted conversations, not single questions. Each turn is checked
  for stage/type, app, and topic, and value checks run on the weather
  and drill-down cards. Conversations: c1 Instagram full flow, c2
  Whatsapp retention (diagnosis must not claim creator-only), c3 past
  experiment evaluation must clarify, c4 letter choice picks the first
  drill-down.
- `tests/test_chat.py` — routing, diagnosis-before-weather clarification,
  hypothesis label, brief contents, past-experiment refusal, plus a
  full Instagram flow on seed-42 data.

## What is NOT claimed

- The creator hypothesis is motivated by a pattern (high orphan rate +
  falling supply), not by a planted causal effect. The generator
  encodes reply → two-way conversation in messages; it does not encode
  "first-post reply causes creator retention", and this module does not
  invent that.
- "Historical" means the 90-day synthetic window and last-28 vs
  prior-28. There are no years of history in this dataset.
- A recommendation is a test to run with pre-registered primary and
  guardrails. If a guardrail breaches, do not ship on the primary alone.

## Run it

```
python scripts/chat.py
# or, in the web UI (restart app.py first):
python scripts/app.py
# then use the "Phase 3 chat" panel, starting with:
What's happening with Instagram?
```

## What comes next (not in Phase 3)

Experiment assignment tables — the other Phase 3 fork — would upgrade
the recommendation step from "hypothesis to test" to "a similar past
experiment showed…", with actual assignments behind the claim. Scale
expansion (500K users, 24 tables, 12 months) is a separate fork and is
not needed for the chat flow.
