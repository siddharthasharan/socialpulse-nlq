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

## Limitations

- Descriptive, not causal. Nothing here says *why* creators fell;
  that needs an experiment or a cohort analysis, not a dashboard.
- Synthetic data only. The creator decline is a property of this
  generator's output, not a claim about any real product.
- The report needs generated data. With no `data/phase1/`, the UI
  shows an error telling you to generate Phase 1 first.
