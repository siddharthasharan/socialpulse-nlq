# Pinned input in full-screen chat (2026-10-29)

## What was wrong

Full screen worked — header showed ↩ Dock, the dashboard was hidden,
and the Facebook flow (weather → messaging drill-down → diagnosis →
recommendation) ran in it on Render on 2026-10-29 13:51 PDT. But after
several turns the input bar was gone from the screenshot.

Cause: `#docked.fullscreen` set `height:100vh` but was not
`display:flex`, so `#dockedbody`'s `flex:1` did nothing. The log grew
to its full content height, pushed the input below the viewport, and
`body.docked-full { overflow:hidden }` blocked page scrolling to reach
it. The chat became unusable after a long conversation, even though
every message was still being answered server-side.

## What the user chose

Scrolling model, confirmed by option tap 2026-10-29 13:53 PDT:
**keep the input pinned at the bottom, messages scroll above it.**
The other model — input scrolling away with the messages — was not
chosen and was not built.

## Fix (frontend only, `app/index.html` CSS only)

- `#docked.fullscreen` is now `display:flex; flex-direction:column`
  with `height:100vh`, so its children actually flex.
- `#docked.fullscreen header` is `flex-shrink:0` (header never shrinks).
- `#docked.fullscreen #dockedlog` is `flex:1; min-height:0; overflow:auto`
  — the log takes all remaining space and scrolls internally, and
  `renderDockedLog()` already scrolls it to the latest message.
- `#docked.fullscreen #dockedbody > p` is `flex-shrink:0` — the input
  row and the Send row never shrink or get pushed off-screen.
- `#docked.fullscreen.collapsed` is `display:block; height:auto`, so
  collapsing still leaves only the header bar.

The bottom-docked (non-full-screen) layout is untouched: it already
constrained `#dockedbody` to 340px, so its input was never pushed off.

No server change, no JS change, no new dependencies.

## Test

`tests/test_docked_fullscreen.py` gains
`test_pinned_input_in_fullscreen`, which asserts the flex column on
the full-screen window, the shrink-proof header, the flexing
scrollable log, and the shrink-proof input rows. The original docked
and full-screen tests are untouched and still pass.

## How to verify

1. Sync, commit, push, wait for Render Live, open the Render URL in
   Incognito (normal Chrome may still show the cached pre-fix page).
2. Click ⛶ Full screen, run a long conversation (e.g. Facebook
   weather → messaging → diagnosis → recommendation → brief).
3. The input + Send row must stay visible at the bottom throughout,
   with only the message area scrolling above it.
4. Press Escape (or ↩ Dock) — back to the bottom dock, transcript
   intact, input still at the bottom of the docked window.
