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
do not share data, sessions, or history. Experiments never uses a live
LLM. Analytics is deterministic-only unless a key is configured (see
"LLM wiring" below); with no key anywhere it uses no live LLM either,
and the shell says so.

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
  Slice 2 adds `"llm": {"configured", "model", "mode", "prompt_version"}`
  — whether a server-side owner key is configured (never the key).
- `GET /api/llm-routing-prompt` (slice 2) returns the pinned routing
  prompt (inventory rendered, `{question}` and `{context}` left to
  fill) plus the model id, prompt version + SHA-256, pinned generation
  params, and the Gemini `generateContent` endpoint — no key involved.
- Copilot CORS patch (separate repo/service): `copilot_platform/server.py`
  — `_cors_origin()` allowlist (SocialPulse Render origin + localhost /
  127.0.0.1 any port), `Access-Control-Allow-Origin` on JSON responses,
  and `do_OPTIONS` preflight. No Copilot engine, scenario, verdict, or
  session logic changed.

## LLM wiring (chunk B, slice 2)

How the demo decides which engine answers an Analytics question:

1. The deterministic chat router runs first, exactly as before. MAU,
   weather, drill-down, diagnosis, recommendation, brief, breakdown,
   daily-series, and MAU-by-country turns are unchanged and make zero
   model calls, key or no key.
2. When no deterministic turn matches AND the server has an owner key
   (`GEMINI_API_KEY` env var, set by the repo owner as a Render env
   var, never in the repo), the LLM fallback turn routes the question
   through `plan_with_llm` (model `gemini-3.8-flash`, prompt
   `llm-prompt-v1`) and executes the returned plan through the
   deterministic engine. The model chooses the analysis, never the
   number. On API error / timeout / bad response the deterministic
   baseline answers instead, labeled in the receipt. With no owner key,
   the question gets today's generic "could not map" clarification.
3. Visitor BYOK: a key field in the sidebar (Analytics only). The
   visitor's key lives only in their browser's `sessionStorage` and is
   never sent to this server. When the deterministic answer is the
   generic unmapped clarification and a visitor key is set, the browser
   fetches the routing prompt, calls Gemini directly, and posts only
   the model's choice (+ real token counts) to `/api/chat`. The backend
   validates the choice against the same allowlist as `plan_with_llm`
   and executes it through the same deterministic dispatch; an invalid
   choice is a clarification, never an execution.

What the shell status means:

- Mode note "Deterministic — no live LLM": no server-side key is
  configured. Every Analytics answer is deterministic.
- Mode note "LLM-routed (Gemini 3.8 Flash)": the owner key is
  configured. Deterministic turns still answer deterministically; only
  otherwise-unmapped questions go to the model.
- Each answer's transcript label names its router: "analytics ·
  deterministic", "analytics · LLM (Gemini 3.8 Flash)", or "analytics ·
  LLM (your key)". The working panel shows the LLM receipt (model,
  prompt version, real tokens, fallback reason) for LLM-routed turns.

Key handling, stated plainly: the owner key is read from the server
environment and never leaves the server process except as the
`x-goog-api-key` header to Gemini; it is never logged, never returned
in any response, and never written to a trace. The visitor key never
reaches the server at all. Neither key is ever in the repo.

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
