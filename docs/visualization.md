# SQL visualization (slice 1) — chart + diagram of an executed query

No LLM, no new data, no new query execution. Everything below is
derived deterministically from a SQL statement that already ran and
from the rows it already returned.

## What you get, for a single-SQL turn

In the shell's artifact panel, under "Show the working":

1. **Result chart + table (same data, two views).** The backend
   attaches the executed query's result rows to the working payload
   (capped at the first 50 rows, stated when capped). The chart is a
   shape heuristic, not a metric claim: it charts the first two
   columns when the second is numeric for every row — a line chart
   when the first column looks like a date/month, a bar chart when
   it is categorical. Further columns (breakdown share, monthly
   creators) stay in the table only. Anything else — a single scalar
   value, or a (category, date, value) result whose second column is
   a date (one bar per row would repeat categories) — gets the table
   only, with a note saying why no chart was drawn.
2. **Diagram of the SQL itself.** `src/socialpulse_nlq/sqlviz.py`
   parses the SQL text into stages — source tables → joins → filters
   → grouping → aggregation → output — carrying the actual table
   names, join conditions, filter conditions, and aggregation
   functions. One level of subquery wrapping
   (`SELECT AGG(x) FROM (flat query)`) is modelled: the inner
   query's stages are shown, then the outer aggregation.

## What honestly gets nothing

- **Weather, diagnosis, recommendation, and brief turns** run many
  queries or none, so they have no single SQL and therefore no chart
  and no diagram. The panel says so instead of inventing one. This is
  the same rule the working panel already used.
- **Ranking turns** run two queries (current window + prior window),
  and **why-evidence for creators** runs two breakdown queries, so
  they also get no single-SQL visualization. Their text already lists
  the rows.
- **Diagram shapes the parser does not model** get an honest note,
  never a guessed diagram: WITH/CTE queries, UNION queries, scalar
  subqueries in the SELECT list, and FROM clauses joining two
  subqueries (e.g. creator penetration's USING join). Those turns
  still get their result table, and a chart when the result shape
  allows one — chart availability is independent of diagram
  availability.
- **Aggregate-routed turns** diagram and chart only the aggregate SQL
  that actually ran. The raw-implied SQL stays labelled NOT executed
  and is never diagrammed as if it ran.

## Coverage

- **Chat working turns** (`/api/chat`, shell artifact panel): fully
  covered. Drill-downs (creators monthly, growth daily DAU) and
  breakdowns (by country, by surface) carry result rows + diagram.
- **Single-shot / gallery queries** (`/api/ask`): the working payload
  carries the same visualization object, so the dashboard working
  panel can render it with the same shell code. The shell itself has
  no gallery UI in slice 1, so gallery cards are not yet reachable
  from the shell — a follow-up would add a gallery entry point to
  the shell that calls `/api/ask` and renders its working payload.
  Nothing about the gallery's SQL or data changes in this slice.
- **No playground.** A visitor cannot type SQL here and there is no
  endpoint that executes arbitrary SQL. The planner's allowlisted
  SELECT/WITH guard logic is untouched.

## Files

- `src/socialpulse_nlq/sqlviz.py` — parser + visualization builder.
- `src/socialpulse_nlq/chat.py` — drill-down/breakdown helpers attach
  result rows; `_reply` attaches the visualization to `working`.
- `scripts/app.py` — `/api/ask` working payload gains `visualization`
  from the executed plan's rows.
- `app/shell.html` — artifact panel renders chart (inline SVG, no
  CDN), table, and diagram.
- `tests/test_sqlviz.py` — parser, chart, cap, and payload tests.
