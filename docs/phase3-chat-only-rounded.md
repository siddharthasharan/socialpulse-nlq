# Chat-only, no placeholder, rounded corners (2026-10-29)

Three small changes, all frontend only (`app/index.html` CSS + two
small JS functions), after the pinned-input fix was verified on Render
in Incognito on 2026-10-29 15:01 PDT (full-screen Instagram creators
chart 37,268 / 24,171 / 16,372, input pinned at the bottom).

## 1. Hide dashboard — so docking back does not bring the older page back

Interpretation, stated plainly so it can be corrected: "when I remove
the dock I still see the older page" was read as exiting full screen
(↩ Dock) uncovering the dashboard again. The fix is a **Hide
dashboard** button in the docked header (next to Full screen), not a
deletion of the dashboard.

- Click **Hide dashboard**: `body.chat-only` hides every dashboard
  section with CSS (`body.chat-only > *:not(#docked) { display:none }`)
  and the docked chat fills the viewport, pinned input and all.
  Exiting full screen while chat-only is on does **not** bring the
  dashboard back — the chat stays the only interface.
- Click **Show dashboard** (same button, relabelled): the dashboard
  returns exactly as it was, transcript intact.
- **Edit questions** exits chat-only first, then scrolls to the
  editor — otherwise it would scroll to a hidden section.
- The dashboard is never deleted from the page, only hidden, so no
  data, editor, or Ask functionality is lost. If a permanent
  chat-only default (no dashboard at all, even on load) is wanted
  instead, that is a separate decision — this toggle keeps both.

## 2. No grey placeholder question in the chat input bars

The grey text "What's happening with Instagram?" was the `placeholder`
attribute on both chat inputs. Both are removed:

- `#dockedq` (the bottom / full-screen input bar) — blank now.
- `#chatq` (the in-page chat input) — blank now, for consistency.

Not changed: the Ask section's input still has its sample question as
an actual value ("What was DAU by app over the last 28 days?"), not a
grey placeholder — that one is a working example, not decoration.

## 3. Rounded corners

- Buttons and text inputs: `border-radius:8px`.
- Bottom-docked window: `border-radius:16px 16px 0 0` (top corners only).
- Charts (Ask canvas and in-chat `.dockchart`): `border-radius:12px`.
- Message bubbles were already `12px` and are unchanged.
- Full-screen and chat-only windows stay square (`border-radius:0`):
  rounded corners there would reveal the page behind them at the
  viewport edges.

## Files changed

- `app/index.html` only.
- `tests/test_docked_chat_only.py`: hide-dashboard toggle + dashboard
  still present in HTML, no placeholder string, rounded-corner markers
  including square full-screen corners.
- No server change, no new dependencies.

## How to verify

1. Sync, commit, push, wait for Render Live, open in Incognito
   (plain URL, no query — see the query-string note in the
   full-screen docs).
2. Click **Hide dashboard** — dashboard disappears, chat fills the
   screen. Click **↩ Dock** if you were in full screen — dashboard
   must *not* come back. Click **Show dashboard** — it returns.
3. In chat-only, click **Edit questions** — dashboard returns and the
   page scrolls to the editor.
4. Chat inputs are blank (no grey question). Buttons, inputs, docked
   window top, and charts look rounded; full-screen corners are square.
5. Run a long conversation — input stays pinned at the bottom.

## What is NOT in this delta

Synthetic data generation and turn-by-turn logic: the user said that
comes *after* these three changes. Nothing for it was built here, and
no generator, schema, or benchmark was changed.
