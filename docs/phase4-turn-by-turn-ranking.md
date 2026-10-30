# Turn-by-turn: ranking by change (2026-10-29, evening)

Follow-up to Step 1, after the user tested Step 1 live and said
"it's pretty good kinda fails on the last step". The screenshot
(2026-10-29 22:55 PDT, Render, Incognito) shows Step 1 working:
the country breakdown for WhatsApp creators matches Step 1 exactly
(US 590, IN 464, BR 281, ID 203, MX 163, GB 128, DE 117). The last
turn then fails:

  You: "Which country had the most drop in the last 2 weeks"
  Assistant [clarification]: "I could not map that to a chat turn…"

## Why it failed

A breakdown lists levels (creators per country in the current
28 days). A "most drop" question asks about change over time.
No Step 1 intent could answer it, so it clarified. That is a
missing intent, not a phrasing miss.

## What changed

`src/socialpulse_nlq/chat.py` only, plus evals:

- **New intent `ranking`.** Triggered by "most drop / biggest
  drop / dropped most / most growth / grew most / biggest
  increase" (plus a dimension word). Topic and dimension inherit
  from context when not named — in the screenshot flow the topic
  is creators, because the previous turn was a creators breakdown.
- **Period parsing.** "Last N days/weeks" → two equal windows
  ending at the weather current end (2026-03-31). "Last 2 weeks"
  = 2026-03-18 to 2026-03-31 vs 2026-03-04 to 2026-03-17. With no
  period, the weather's current vs prior 28 days are used, so the
  ranking matches the weather numbers. Windows outside Jan-Mar
  2026 clarify instead of extrapolating.
- **Metric per topic.** Creators = distinct creators (posts as
  the extra column); growth = distinct active users; messaging =
  distinct senders; surface = posts (creators as the extra).
  Missing in a window counts as 0, so a segment does not vanish
  from the ranking.
- **Answer shape.** First sentence names the top segment, then
  the full sorted list with current, prior, change, and percent,
  then the total — so the ranking can be checked, and so the
  reader can see whether "most" is absolute, percent, or both.
  If no segment dropped, it says so instead of naming a "drop"
  that is really a small increase.
- **Short-window honesty.** The reply labels the window and says
  it is not the 28-day weather trend or the Jan-Mar story, and
  that small segment counts in a short window are noisy.

## The seed-42 answer to the screenshot question

WhatsApp creators by country, last 14 days vs prior 14 days:

- ID: 159 vs 171, **-12, -7.0%** (posts 462 vs 510, -48) — largest
  drop on both absolute and percent, so the answer is unambiguous.
- BR: 231 vs 236, -5, -2.1%. MX: 129 vs 130, -1, -0.8%.
- GB +1, DE +2, US +14, IN +25. Total creators 1,575 vs 1,551,
  +24. So the largest country drop sits inside a slightly rising
  total — a -12 move in 14 days, not a collapse, and not the
  Jan-Mar creator decline (creations Jan 37,268 → Mar 16,372
  for Instagram; the WhatsApp long trend is the 28-day weather
  comparison, creators -31.7%).

## Bugs caught while testing this turn

- "Most growth" was first read as the growth topic, so "which
  country had the most growth" ranked active users in a creators
  conversation. Direction phrases are now stripped before the
  topic is re-detected for a ranking turn.
- Surface rankings first labelled their primary count (posts)
  as creators, and the extra column (creators) as posts. Both
  labels are now set by topic/dimension, not hardcoded.

## What did NOT change

- No generator change, no new tables, no assignment tables,
  no scale change.
- No server change (`POST /api/chat` unchanged), no Render
  config change, no planner/single-shot change.
- The first clarification in the screenshot ("So people are not
  creating new messages") is left alone: a bare paraphrase with
  no question or breakdown request has no actionable intent, and
  guessing messaging vs creators from it would be worse than
  clarifying. The user rephrased it successfully in the next
  turn, which is the intended recovery path.

## Evals

- Conversation benchmark 7 → 8 cases: c8 is the screenshot flow
  (WhatsApp weather → creators by country → most drop, last
  2 weeks). The runner now extracts the topic from a `ranking`
  reply too.
- `tests/test_chat.py`: ranking test asserting ID -12 / -7.0%,
  total context, short-window note, and that "most growth"
  still ranks creators in a creators conversation.
