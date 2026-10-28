# Product health — weather report

## What it is

The answer to "How is the product doing?" as a report, not a single
metric and not a score. The UI has a "Show weather report" button, and
the same question typed into the ask box routes to it instead of
clarifying. The CLI also supports it: `python scripts/ask.py "How is
the product doing?"`.

## What it contains (seed-42 results, for reference)

- **Growth up:** mean daily DAU 10,055 in 2026-03-04 to 2026-03-31,
  +4.5% vs the prior 28 days. WAU up 7.9% on complete weeks.
- **Retention flat:** D7 21.5% for cohorts 2026-02-25 to 2026-03-24,
  +0.7 points vs prior cohorts.
- **Creators are the problem:** daily creators 1,016, down 32% vs the
  prior period; creator penetration 10.1%, down 5.7 points. In this
  synthetic data, creation volume itself falls across the window
  (82,755 creations in January, 53,497 in February, 36,500 in March),
  so daily creators fall even as DAU rises. That divergence is real
  in the data, not a bug in the report — and it is exactly the kind
  of thing a weather report is for.
- **Orphan rate 24.6% (whole window), top-1% share 9.5%:** attention
  is concentrated while a quarter of posts get nothing.

## Definitions and periods

- Current period: last 28 days of sessions. Prior period: the 28 days
  before that. Periods are derived from the maximum session date, not
  hardcoded to March 2026.
- WAU: mean of the last 4 *complete* calendar weeks vs the 4 before
  them. Complete-week filtering matters: the first version of this
  report included the partial final week (Mar 30-31, WAU 15,567
  against ~32,500 for full weeks) and wrongly showed WAU down 10%
  while DAU was up. That bug was caught by checking direction
  consistency, and is a good interview story about partial periods.
- D7 retention: only complete cohorts (signup + 7 days inside the
  window). Recent cohorts are the last 28 complete cohorts.
- Orphan rate and top-1% share are whole-window levels, never trends:
  posts published late in the window had less time to attract
  engagement, so a current-period orphan rate would overstate it.
- MAU is omitted on purpose: a 31-day MAU compounds daily activity
  probability and is not comparable to a 28-day window.

## Thresholds (chosen, not business truth)

Counts move on a 2% relative change; rates move on 1 percentage
point; smaller moves are "flat". Watchouts fire at orphan rate > 20%
and top-1% share > 8%, plus any down move in DAU, retention,
engagement, or creator supply. These thresholds are documented in
`src/socialpulse_nlq/weather.py` so a reviewer can disagree with
them — which is the point. A weather report with hidden thresholds
is just a score with extra steps.

## App-scoped reports: "How is Whatsapp doing?"

The same report, scoped to one app, is triggered by "How is
[instagram|whatsapp|facebook] doing?" — including "How is Whatsapp
doing recently?", where "recently" means the same last-28-days
window. Every card is scoped: user metrics use
`dim_users.primary_app`, content metrics use `dim_content.app`,
impressions use their app column, and messaging uses the sender's
primary_app. This is clean in this synthetic data because session
app always equals primary_app (the cross-tab is diagonal), so the
scoped DAU definition matches the global one. Top-1% share in
scoped mode is within-app, not global.

Scoping is not cosmetic: on seed-42 data, global D7 is flat
(21.5%, +0.7 points), but Whatsapp D7 falls 2.0 points to 19.1%
while Instagram D7 rises 2.6 points to 23.2%. The global report
hides that split; the scoped report shows it. Whatsapp headline:
growth up (DAU 2,972, +3.6%), retention down, creator supply
down (301 daily creators, orphan rate 24.9%).

Benchmark b37 scores the Whatsapp report end to end.

## Benchmarking the report

Routing alone is not scored — the numbers are. Benchmark cases:

- **b32** — "How is the product doing?" must route to a
  `weather_report` plan with no SQL. Routing only.
- **b33** — "Show me the weather report" must also produce the
  growth/retention numbers: DAU 10,054.57, WAU 32,685.25, D7
  0.21532, messaging DAU 4,340.29.
- **b34** — "What is product health?" must produce the creator
  numbers (daily creators 1,015.57, creator penetration 0.10128,
  orphan rate 0.24588, top-1% share 0.09535) and all three
  watchouts ("Creator problem", "Creator supply softening",
  "Concentration watch").

The runner (`scripts/run_benchmark.py`) compares card values with
the same tolerance as other cases: 2% or 0.01 absolute, whichever
is larger. Weather D7 golds differ from the canonical D7 gold
(0.22053) on purpose: the report uses recent complete cohorts,
not the Jan 1–Feb 21 cohort window. A tampered gold value was
verified to fail b33 (33/34), so this scoring is not decorative —
it actually catches a wrong number.

Run: `python scripts/run_benchmark.py --data data/phase1`
(expect 34/34 on seed-42 data).

## Limitations

- Descriptive, not causal. Nothing here says *why* creators fell;
  that needs an experiment or a cohort analysis, not a dashboard.
- Synthetic data only. The creator decline is a property of this
  generator's output, not a claim about any real product.
- The report needs generated data. With no `data/phase1/`, the UI
  shows an error telling you to generate Phase 1 first.
