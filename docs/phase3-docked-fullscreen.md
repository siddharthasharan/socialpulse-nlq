# Full-screen docked chat (2026-10-29)

## What changed, and why

The docked chat was built to keep the whole dashboard visible, with the
chat pinned at the bottom. That is what the user asked for on 2026-10-28
— and it is what made the Render page look busy: the dashboard stayed
visible behind/above the chat.

Now the docked window has a **⛶ Full screen** button in its header
(next to New chat and Edit questions). Clicking it expands the docked
window to fill the viewport, with an opaque background and the page
behind it unscrollable, so the chat is the only interface visible — a
clean, ChatGPT/Muse-style view. Nothing on the dashboard is removed or
moved; it is simply covered until you exit.

Exit full screen with:
- the **↩ Dock** button (the same button, relabelled), or
- **Escape**, or
- collapsing the window (▾) — collapsing exits full screen first, so
  you never end up with a full-height header bar.

Exiting restores the bottom-docked window exactly as it was, with the
transcript intact. The chat session is unchanged throughout: full
screen, docked, and the in-page chat section all share one session id
and one transcript.

## Data and charts: docked chat only

When a chat drill-down returns data, it is now shown inside the docked
chat, not in the dashboard behind it:

- **Growth drill-down** → a line chart of the daily DAU series (28
  days, from the server's `drilldown.evidence.daily_dau_series`) drawn
  inside that chat message, with a note: "Chart — daily DAU from this
  chat's growth drill-down, shown here only."
- **Creators drill-down** → a bar chart of posts per month (Jan/Feb/Mar
  2026, from `drilldown.evidence.monthly_creations`), same "shown here
  only" note.
- **Other drill-downs** (retention, messaging, engagement,
  concentration) are scalars, so they get text only — no chart is
  invented for them.

This needed no server change: the server already returned the
`drilldown` evidence in its reply; the client previously rendered only
the reply text and ignored it. The client now stores
`drilldown: d.drilldown` on each assistant message (in both the docked
and in-page send paths, so a conversation started in-page still gets
charts when viewed docked) and `renderDockedLog()` draws the chart
canvas for that message.

Deliberately **not** moved: the dashboard **Ask** section still renders
its own canvas chart into `#out` when you use Ask (not chat). In
full-screen mode that section is behind the opaque chat window, so it
is not visible — which is exactly "chat only" while you are in
full-screen chat. When you exit full screen, Ask charts are where they
always were. The in-page chat log also stays text-only; the chart
canvas exists in the docked log only.

## Files changed

- `app/index.html` only: full-screen CSS (`#docked.fullscreen`,
  `body.docked-full`, `.dockchart`), the header button
  (`id="dockedfullbtn"`), `toggleDockedFullscreen()`,
  `drawDockedLine()` / `drawDockedBars()`, chart rendering in
  `renderDockedLog()`, and `drilldown` stored on assistant messages.
- `tests/test_docked_fullscreen.py`: markers for the toggle, the
  full-screen CSS, the docked-only chart rendering, and that the
  dashboard + docked window are still present. The original
  `tests/test_docked_chat.py` is untouched.
- No server change, no catalog change, no new dependencies.

## How to verify

1. Sync this delta at the repo root, commit, push. Render redeploys
   automatically on push (Blueprint sync) — wait for Live, as before.
2. Open the Render URL, scroll to the bottom, click **⛶ Full screen**.
   The dashboard should disappear behind an opaque chat window, with
   the input pinned at the bottom.
3. In full screen, send `What's happening with Instagram?`, then the
   **Growth** drill-down. A daily DAU line chart should appear inside
   that chat message, not in the dashboard behind it.
4. Then the **Creators** drill-down. A posts-per-month bar chart
   should appear inside that chat message.
5. Press **Escape** (or **↩ Dock**). The window should return to the
   bottom dock, transcript and charts intact. Collapse (▾) and expand
   again — same result.
6. A retention/messaging drill-down should show text only, no chart.

## Known limits

- Charts are drawn with the same simple canvas code as the Ask chart
  (line + dots, bars + values), not a charting library — no CDN was
  added, by design.
- Chat history is still in-memory per server process (Render free
  sleeps/restarts reset it), same as before. Full screen does not
  persist a transcript across a server restart.
