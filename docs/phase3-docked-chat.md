# Bottom-docked chat window (2026-10-28)

Everything on the dashboard remains — weather, single-shot Ask,
Phase 3 chat section, Questions editor, experiments, catalog. The
change is only at the bottom: a fixed chat window, like a
Muse/ChatGPT input bar, that stays visible while you scroll.

## What it is

- `#docked` in `app/index.html`: a fixed window at the bottom of the
  page, header bar + scrollable message area + input + Send, pinned
  at the bottom. Clicking the header collapses it to just the bar;
  clicking again expands it. The page gets bottom padding so the
  window never hides dashboard content underneath it.
- Messages render as bubbles: yours on the right, the assistant's on
  the left, with its type (`weather`, `drilldown`, …) shown. Option
  chips under an assistant message are the same server options as in
  the chat section — clicking one **selects** the next step
  (drill-down, diagnosis, recommendation, brief).
- **Update** works two ways, both already built: in chat, a correction
  ("you are not looking at retention", "show daily, not average")
  re-runs the step with the corrected parameter; or the header's
  **Edit questions** button scrolls to the Questions editor section,
  where flow and eval questions are edited and saved. **New chat**
  in the header starts a fresh session.

## Continuity

The docked window and the in-page chat section share one
`chatSessionId` and one client-side transcript (`chatHistory`), so
a conversation started in one box continues in the other: the server
keeps app/topic/corrections in state, and the transcript is mirrored
into both logs. No new server route was needed — both boxes POST to
the existing `/api/chat`.

## Tests

- `tests/test_docked_chat.py`: every pre-existing dashboard section
  is still present, and every docked piece (window, log, input,
  send/toggle/new-chat functions, shared transcript, docked chips,
  Edit questions) is present.
- Server behaviour is unchanged: chat tests, weather, planner,
  conversation benchmark, and single-shot benchmark are unaffected
  by this frontend-only change (re-run after sync to confirm).
