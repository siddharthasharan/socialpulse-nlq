# Unified shell (slice 1) — `GET /shell`

One Muse-style chat shell over two separate, deterministic demos. It is a
front end only; it computes nothing itself.

## Modes (never merged)

| Conversation mode | Engine | Backend | Data |
|---|---|---|---|
| Analytics | SocialPulse planner + chat (`/api/chat` on this server) | This server | Synthetic SocialPulse analytics, 500K users, seed 42 |
| Experiments | Experiment Copilot orchestrator + engine | Copilot service (`/api/chat`, `/api/run`, `/api/run-status`) | Separate synthetic experiment data, deterministic decision rules |

A conversation is Analytics **or** Experiments and stays in that mode.
Switching mode starts a **new** conversation. The mode indicator at the
top of the chat always names the engine that answered. The two engines
do not share data, sessions, or history, and neither uses a live LLM.

## Routes and files

- SocialPulse: `app/shell.html` (single file, inline CSS/JS, no CDN, same
  stdlib-only convention as the rest of the UI), served by
  `scripts/app.py` at `GET /shell` (and `/shell.html`). Existing `/`
  dashboard + docked chat routes are untouched.
- `GET /api/shell-config` returns `{"copilot_base": ...}` from the
  `COPILOT_BASE_URL` env var, or `""`. When empty, the shell uses
  `http://localhost:8001` on localhost and
  `https://experiment-copilot-platform.onrender.com` otherwise; a
  `?copilot=<origin>` query parameter overrides both (local testing).
- Copilot CORS patch (separate repo/service): `copilot_platform/server.py`
  — `_cors_origin()` allowlist (SocialPulse Render origin + localhost /
  127.0.0.1 any port), `Access-Control-Allow-Origin` on JSON responses,
  and `do_OPTIONS` preflight. No Copilot engine, scenario, verdict, or
  session logic changed.

## Artifacts

- Analytics turns that run a query show their working in the right panel:
  exact SQL, tables, metric definition, and the deterministic trace
  labelled "No live LLM, no API key, no external call." Weather,
  diagnosis, recommendation, and brief turns honestly show no single SQL.
- SQL visualization (slice 1, see `docs/visualization.md`): a
  single-SQL turn's artifact also shows a chart + table of its result
  rows (capped at 50, stated when capped) and a deterministic diagram
  of the SQL itself (tables → joins → filters → grouping/aggregation
  → output). Shapes the parser does not model (WITH, UNION, scalar
  subqueries, multi-subquery joins) get an honest note, never a
  guessed diagram; routed turns diagram only the aggregate SQL that
  actually ran.
- Experiment runs show their scorecard in the right panel: verdict, KPIs,
  metrics table, PM view toggle, watch-outs.

## Persistence

Conversation list and transcripts live in browser `localStorage` only
(key `portfolio-shell-conversations-v1`). No login, no server
persistence, no personal data. Server-side chat sessions remain
in-memory per backend (restarting a backend resets its session context;
the shell transcript survives, but follow-ups may start a fresh server
context under the same stored session id).

## Analytics dataset extension point (NOT built in slice 1)

`SHELL_DATASETS` in `app/shell.html` has exactly one entry, `"social"`.
A later slice adds a dataset by, in order:

1. Adding its id to `SHELL_DATASETS`.
2. Teaching the SocialPulse chat backend a per-dataset data directory /
   session (e.g. a dataset parameter on `/api/chat`), with its own
   validation and benchmarks — a dataset that only changes the shell
   label without changing the queried data must never ship.
3. Adding a dataset picker in the shell that sends the chosen dataset
   with each Analytics call and shows it in the mode indicator.

Queued domain idea: synthetic SocialPulse analytics data for
Marketplace, Pharma, and AI Labs domains. Those datasets are analytics
data for the SocialPulse gallery/chat; their rows must **not** be the
Copilot experiment rows, and they do not make the two demos share data.
