# Chart and trend fixes (2026-10-27)

Two bugs found from one user report: "DAU for Whatsapp over the last
7 days gave a number but no chart."

## Bug 1: empty chart for single-value results (UI)

`app/index.html`'s `drawChart` built a path with only a `moveTo` for a
one-point series, then called `stroke()`. A path with no line segment
draws nothing, so every scalar result (mean DAU, orphan rate, and so
on) showed an empty canvas with just the "series" label.

Fix: single points are drawn as a dot with their value labelled, and
every point in a multi-point series gets a dot too. The UI also adds
a note for one-row results: this is an averaged value, not a trend.

## Bug 2: DAU trend dropped the app filter (planner, more serious)

Asking "What is the DAU trend for Whatsapp over the last 7 days?"
returned 7 daily values around 9,500–10,900 — all-app DAU, not
Whatsapp. The DAU series branch in `build_sql` queried
`fact_sessions` alone, ignoring the app/country/platform filters
that the averaged branch applied via the `dim_users` join.

Fix: the series branch now uses the same join and `where` clause as
the averaged branch. Cross-check after the fix: the 7 Whatsapp
daily values (2,808, 2,850, 2,830, 3,131, 3,277, 2,876, 2,823)
average to 2,942.14, exactly the scalar answer. If those two ever
disagree again, one of them is wrong.

## Follow-up: "over the last N days" now means a daily series (2026-10-27)

After the chart fix, the user pointed out the deeper expectation:
"What was DAU for whatsapp over the last 28 days?" should show 28
daily values, not one averaged dot. The planner used to treat a
period phrase as only a date filter, returning the scalar
`dau_mean_daily` unless the question also said "trend", "daily",
or had a breakdown.

Changed `_series_or_scalar` in the planner: an explicit period
("over the last/past", "last/past 7/28/90 days", "last week",
"last month") now returns a daily series for DAU. Explicit
"mean"/"average" still returns the scalar — that check runs first.
Cross-check: the 28 Whatsapp daily values average to 2,971.89,
exactly the old scalar answer. Benchmark b36 scores the new
behaviour with all 28 values. Benchmark is now 36/36.

## Tests

- `tests/test_planner.py::test_dau_series_respects_app_filter`:
  SQL must contain the Whatsapp filter and the `dim_users` join;
  on data, all 7 values must be under 5,000 (all-app would be
  ~9,500+) and their mean must match the scalar within 1.0.
- Benchmark b35 scores the same trend question end to end,
  including the exact 7 daily values. Benchmark is now 35/35.
