# Phase 0 — Access + reliability

First slice of the evaluation-platform plan: make the demo open reliably
from a shared link before scaling the benchmark. Three changes, all in
the SocialPulse repo; no Copilot, data, or generator change.

## 1. Query-string routing fix (`scripts/app.py`)

`do_GET` used to match `self.path` exactly against `("/", "/index.html",
"/shell", ...)`, so any query string returned `{"error": "not found"}`:
`/?v=abc`, `/shell?utm_source=chatgpt.com`, `/api/health?x=1` all failed.
Now the handler strips the query string (`urlparse(self.path).path`)
before matching, so every page and `/api/*` route works with any query
string. The original path is kept for `/api/weather`, which still reads
its own `?app=` query exactly as before. Unknown paths still return
`{"error": "not found"}` with 404. POST routes are unchanged (shared
links are GETs).

## 2. Health endpoint: `GET /api/health`

```json
{"status": "ok", "service": "socialpulse-nlq", "users": 500000,
 "seed": 42, "window": "2026-01-01..2026-03-31"}
```

Static payload, no DuckDB, no data-dir access — a test proves it by
pointing the handler at a nonexistent data dir and still getting 200.
It is a **liveness** check ("is the server up?"), NOT a data-integrity
check ("is the data right?"). Data integrity is what the validation
suite (`sql/canonical_metrics.sql`, `tests/`) is for; health saying
`ok` while the parquet was corrupt would be a lie by omission if it
claimed more, so it claims nothing about the data.

## 3. Cold-start check (measured 2026-11-05, local VM, 500K seed-42)

Fresh `python scripts/app.py` process, timed from launch:

| Check | Time |
|---|---|
| First `/api/health` 200 | **0.14 s** after start |
| First `/shell?utm_source=test` 200 | **0.14 s** after start |
| First `/api/chat` weather turn (Instagram) | **20.9 s** |
| Warm MAU turn ("What is the MAU for Instagram?" → 210,809) | 0.69 s |
| Warm `/api/health` round-trip | 0.001 s |

Two honest consequences:

- **Process start is fast; the first data query is not.** The server
  is up in ~0.14 s, but the first weather turn scans 500K rows of
  parquet and takes ~21 s locally. A recruiter opening the demo sees
  the shell immediately, then waits ~21 s for the first answer. That
  is a 500K-scan cost, not a routing bug, and Phase 0 does not change
  it.
- **Render's free-tier wake was NOT tested here and cannot be from
  this workspace.** Known facts: the free instance sleeps after ~15
  min idle and takes ~50 s to wake. So on Render a recruiter's first
  load is ~50 s of wake *plus* the ~21 s first-scan cost, even with
  routing fixed. The health endpoint helps distinguish "asleep" from
  "down" (health answers in ~1 ms once the process is up), but it does
  not warm the instance. A keep-warm ping or a paid instance would
  change that; both are separate decisions (the latter is spend), and
  neither was built in this slice.

## Tests

`tests/test_phase0.py` (6 tests, all pass): query-string variants
return 200 for `/`, `/index.html`, `/shell`, `/shell.html`;
`/api/*` routes work with a query string; health returns the exact
payload in < 2 s and with no data dir; `/api/weather?app=instagram`
still works; unknown paths still 404.

## Deliberately left out

Phase 1 benchmark work, observability traces, planner experiments,
LLM layer, BYOK, Copilot changes, data/generator changes, dashboard
restructure, keep-warm cron, paid tier.
