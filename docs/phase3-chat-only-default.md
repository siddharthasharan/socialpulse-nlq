# Chat-only by default (2026-10-29)

## What changed, and why

The previous delta added a **Hide dashboard** button, but the page
still loaded with the dashboard visible behind the docked chat, and
the user had to click to hide it. On 2026-10-29 15:29 PDT he pointed
at that background in a screenshot and said: "this background is not
needed."

So the background is no longer the default view. The page now loads
chat-only:

- `<body class="chat-only">` on load, so all dashboard sections
  (weather button, Ask, in-page chat, questions editor, experiments,
  catalog) are hidden by the existing `body.chat-only` CSS, and the
  docked chat fills the viewport with its input pinned at the bottom.
- The header button now starts as **Show dashboard**, because the
  dashboard is already hidden. Clicking it removes `chat-only` and
  restores the dashboard exactly as it was, transcript intact.
- Clicking **Hide dashboard** again hides it again. Full screen
  (⛶ / ↩ Dock) still works on top of chat-only, and Docking back
  while chat-only is on does not bring the dashboard back.
- **Edit questions** still exits chat-only first, then scrolls to
  the editor — otherwise it would scroll to a hidden section.

## What was NOT done

The dashboard sections were **not deleted** from `app/index.html`.
They are hidden with CSS only, and Show dashboard brings them back
in one click. Deleting them outright would also delete the Ask
planner/chart, the questions editor, and the in-page chat from the
UI, with no way back except a code change. If a permanent deletion
is wanted instead of a hidden-by-default dashboard, say so and that
removal can be made — this delta keeps the escape hatch.

## Files changed

- `app/index.html` only: `<body class="chat-only">`, header button
  initial text/title, and the chat-only CSS comment updated to say
  the page loads chat-only.
- `tests/test_docked_chat_only.py`: new
  `test_chat_only_is_default_on_load` (body class + initial
  "Show dashboard" button). All earlier tests are untouched.
- No server change, no new dependencies.

## How to verify

1. Sync, commit, push, wait for Render Live, open in Incognito
   (plain URL, no query).
2. On load you should see only the chat — no SocialPulse NLQ header,
   no weather/Ask sections behind it. The header button should read
   **Show dashboard**.
3. Send a question, run a drill-down — input stays pinned at the
   bottom, charts stay in the chat.
4. Click **Show dashboard** — the dashboard returns, chat becomes
   bottom-docked again, transcript intact.
5. Click **Edit questions** from chat-only — dashboard returns and
   the page scrolls to the editor.
