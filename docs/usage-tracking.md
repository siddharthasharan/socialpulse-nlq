# Usage tracking — SocialPulse demo (Render)

Live usage tracking for the demo at socialpulse-nlq.onrender.com:
how many people use it, and which countries they come from. Nothing
more. This document is the whole privacy statement: what is collected,
what is not, and where to read it.

## What is collected

Two server-side events, each one JSON line printed to stdout, so they
land in Render Logs:

- `page_load` — every time the dashboard (`/`) or the shell (`/shell`)
  page is served.
- `session_start` — every time a new chat session starts (`POST
  /api/chat` with a session id the server has not seen).

Each line carries exactly four fields:

```json
{"ts": "2026-11-05T21:00:00Z", "event": "session_start", "country": "DE", "visitor_hash": "9f2c41ab77d0e3f1"}
```

- `ts` — UTC timestamp of the event.
- `event` — `page_load` or `session_start`.
- `country` — two-letter country code (e.g. `DE`, `IN`, `US`), or
  `unknown` when the country could not be determined. Country level
  only: no city, no coordinates, no region.
- `visitor_hash` — a salted daily hash of the visitor IP (see below).

A "session" is a `session_start` event (a new chat session). "Unique
visitors" for a day is the count of distinct visitor hashes seen that
day. A visitor who only loads a page and never chats counts as a
visitor, but not as a session.

## What is NOT collected

- **No raw IP address, ever** — not full, not partial, not a /24
  subnet, not in logs, not in any endpoint, not in any file. The only
  place an IP exists is in memory for the fraction of a second it
  takes to hash it and look up its country.
- No User-Agent, no message text, no full URLs or query strings.
- No per-visitor rows, no cross-day tracking: the hash changes every
  day, so the same person on two days looks like two different
  hashes. There is deliberately no way to follow one visitor across
  days.
- No city, coordinates, or any location finer than country.

Earlier the logging guarantee was "no IP at all". It now reads: **no
raw IP stored; country + daily salted hash only.** The hash is the one
deliberate exception, and it is one-way.

## How the visitor hash works

`sha256(salt | UTC date | IP)`, truncated to 16 hex characters.

- The salt is generated once per server process (`secrets.token_hex`)
  and is never logged, never committed, never returned by any
  endpoint. Restarting the server (which the free tier does whenever
  the instance sleeps) generates a new salt, so hashes do not survive
  a restart either.
- The date is part of the input, so the same IP hashes differently
  every day.
- A hash cannot be reversed to an IP: it is a one-way digest, and
  the salt that would let anyone recompute it is gone with the
  process.

## How the country is looked up

The visitor IP (first entry of `X-Forwarded-For`, else the socket
peer) is resolved to a country with the ip-api.com free endpoint (no
key, no new dependency). The limits, stated plainly:

- **At most one lookup per /24 subnet per day.** Results are cached
  in memory by subnet, so a returning subnet costs no lookup.
- **1-second timeout, fail-open.** If the lookup fails or times out,
  the country is `unknown` and the request proceeds. The lookup is
  synchronous on a cache miss, so the worst case is a delay of at
  most 1 second on the first request from a new subnet while the
  resolver is down; cache hits cost nothing, and the demo's first
  data scan is already far slower than that.
- **Private/reserved IPs are never sent to the resolver** (localhost,
  LAN addresses, etc. are `unknown` by construction). IPv6 addresses
  are also `unknown`: the cache is keyed by IPv4 /24.
- Resolving an IP necessarily sends that IP to ip-api.com as the
  lookup key. We do not log or store it ourselves; ip-api.com's own
  handling of lookup requests is theirs — see ip-api.com.
- `X-Forwarded-For` can be spoofed, but it only feeds an aggregate
  usage counter, never an access decision.

## Where to read it

**Render Logs (the log trail).** Dashboard → service `socialpulse-nlq`
→ Logs, search for `"event"`. Each line is one event. Nothing here is
retroactive: logging starts when this code deploys.

**Live counters (since the last restart).** `GET /api/usage`:

```json
{"sessions": 12, "page_loads": 31, "unique_visitors_today": 9,
 "date": "2026-11-05",
 "sessions_by_country": {"DE": 5, "IN": 4, "US": 2, "unknown": 1},
 "since": "server start",
 "note": "Counters are in-memory only and reset when the instance restarts or sleeps (Render free tier). ..."}
```

Counts only — no IPs, no hashes, no per-visitor rows. Unauthenticated
and read-only, by design: there is nothing sensitive in it to protect.

**Daily table from saved logs.** `scripts/usage_summary.py` reads log
lines (a file, or Render Logs piped on stdin) and prints one row per
UTC date:

```bash
python scripts/usage_summary.py render-logs.txt
# date        sessions  visitors  page_loads  sessions by country
# 2026-11-05  sessions=  12  visitors=   9  page_loads=  31  DE=5, IN=4, US=2, unknown=1
```

## Free-tier caveats (read this before trusting a number)

- **Counters reset on restart.** The instance sleeps after ~15
  minutes idle and restarts on deploy. `/api/usage` is "since this
  process started", not "all time". For anything longer-lived, use
  the Render Logs trail and the summary script — the logs outlive
  the process.
- **A session is a chat session, not a page view.** Page-only
  visitors appear in `page_loads` and in the visitor count, but not
  in `sessions`.
- **First sight from a new subnet can log `unknown`.** Only if the
  resolver is down at that moment (1-second timeout, fail-open);
  later events from the same subnet carry the resolved country.
- **Log lines can arrive slightly out of request order.** The event
  is logged after the response is written, so under concurrency a
  later request's line can land first. The `ts` field is the
  authority, and the summary script groups by it — the counts are
  unaffected.
- **"Unique visitors" is per day, by hash.** Two people behind one
  NAT (same public IP) count as one visitor; one person on two
  networks counts as two. It is a rough count, not an identity.
