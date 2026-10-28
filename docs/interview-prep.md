# SocialPulse NLQ — Interview Preparation Guide

> **Disclaimer, read this first and say it first in an interview if asked.**
> SocialPulse NLQ is an **independent synthetic project**. It is **not Meta's
> tool**, it is not affiliated with Meta Platforms, and it does not copy
> internal code, data, dashboards, or metric definitions from any employer.
> It is inspired only by the *idea* of a natural-language analytics tool:
> natural language is the interface, semantic correctness is the product.
> Every number in this guide comes from the project's own synthetic data and
> its own validation report (`docs/validation-phase1.md`). Nothing here is a
> claim about real users of any real product.

## Who this guide is for, and how to use it

You are a data scientist who is strong on statistics and experimentation and
shaky on dev tooling. You used AI assistance to build this project. That is
fine to say out loud — what an interviewer needs to hear is that you
understand what the system does, why each decision was made, what you
verified yourself, and where the system stops being trustworthy.

For each stage this guide gives you three things, in that order:

1. **What is actually being done** — the mechanism, in concrete terms.
2. **Why it works that way** — the reasoning an interviewer is testing.
3. **What you specifically need to know to make it work** — the part you
   cannot bluff, because it is where the system breaks if you get it wrong.

Each stage ends with interview Q&A: a short strong answer, then the trap to
avoid. The traps matter as much as the answers. Most of them are ways of
accidentally claiming more than this project can support.

A note on scope: Phase 1 (data foundation, catalog, validation) is built and
validated. Phase 2 (deterministic planner, SQL guard, local web UI,
32-question benchmark) is built and scored 32/32 against the Phase 1
seed-42 data (see `docs/phase2.md`). Quote that score only for that data
and that benchmark set, not as a general claim about any question.

---

## The one-paragraph version (memorise this)

SocialPulse NLQ answers product-analytics questions in plain English over a
synthetic social platform. Phase 1 generates 50,000 synthetic users, 12
tables, and 90 days of activity (2026-01-01 to 2026-03-31) from one seed, so
the data is reproducible. A semantic catalog defines 20 canonical metrics
with an explicit grain, denominator, and hand-written SQL, so "DAU" always
means the same thing. A validation script proves the daily aggregates
reconcile exactly to the underlying events. Phase 2 adds a deterministic
planner: it maps a question to a catalog metric, builds SQL from an
allowlisted template, checks it with a read-only guard, runs it on DuckDB,
and returns the result with a receipt showing metric, grain, filters,
tables, and runtime. If a question is ambiguous or invalid, it asks for
clarification instead of inventing an answer. That last sentence is the
whole product thesis: semantic correctness, not fluent text.

---

## Stage 1 — Phase 1 data foundation: seeded generation

### What is actually being done

`scripts/generate_phase1.py` generates the warehouse in a fixed dependency
order, with one random-number generator seeded with 42:

1. **Users** (`dim_users`, 50,000 rows). Each synthetic user gets an opaque
   id (`U000001`, …), a signup date, a country, a primary app
   (instagram / whatsapp / facebook), a platform (android / ios / web), an
   acquisition source (organic / referral / paid), a latent engagement
   score, a social degree, and an `is_creator` flag.
2. **Devices** (`dim_devices`, 64,884 rows). One or two devices per user.
3. **Social graph** (`dim_social_edges`, 473,943 rows). Directed follow
   edges, source → target, deduplicated, never self-edges.
4. **Content** (`dim_content`, 172,752 rows). Creators publish items with a
   creator, app, surface (Feed / Reels / Stories), type (photo / video /
   text), creation time, and video duration where relevant.
5. **User-days and sessions.** For each of the 90 days, each user who has
   signed up by that day gets an activity probability, and active users
   get 1–4 sessions (`fact_sessions`, 1,355,587 rows) with a lognormal
   duration.
6. **Impressions** (`fact_content_impressions`, 3,204,996 rows). Sampled
   views, only of content created up to that day, weighted by creator tier.
7. **Engagements and video plays.** Engagements
   (`fact_content_engagements`, 511,652 rows) are a 16% sample of
   impressions, split like / comment / share / save. Video plays
   (`fact_video_plays`, 937,205 rows) are a 60% sample of video
   impressions, with a watch time and a completion percentage.
8. **Messages** (`fact_messages`, 462,920 rows). Messages go only along
   social edges, with a same-day reply probability that creates two-way
   conversations.
9. **Creation events** (`fact_content_creation`, 172,752 rows). One row per
   published item, mirroring `dim_content`.
10. **Aggregates.** `agg_user_daily` (800,762 rows: user × date) and
    `agg_surface_daily` (15,197 rows: date × app × surface × country ×
    platform) are computed from the event tables by grouping, never typed
    in by hand.

The generator runs in about a minute and writes Parquet files, plus a
`manifest.json` with the row counts. The Parquet files are gitignored:
the repo holds code, not 111 MB of generated data. Anyone can regenerate
the same data by running the generator with seed 42.

### Why that order matters

Referential integrity is a generation-order problem before it is a
validation problem. You cannot generate an impression of a post that does
not exist yet, a message to a user who does not exist, or an engagement on
an impression that was never sampled. So the order is: entities first
(users), then relationships (edges), then the things entities produce
(content), then the activity that references them (sessions, impressions),
then the activity that references *that* activity (engagements, plays,
messages), and only last the aggregates that summarise all of it.

Two details inside that order are worth being able to explain:

- **Impressions use a content cutoff.** For each day, only content created
  up to that day is eligible to be viewed. Without the cutoff, a post could
  be "viewed" before it was published, and no validation check on row
  counts would catch it. The generator uses `searchsorted` on the sorted
  creation timestamps to enforce this.
- **Aggregates are derived, not authored.** `agg_user_daily` is a group-by
  of sessions, impressions, engagements, and messages. That is what makes
  reconciliation meaningful later: if the aggregate and the events disagree,
  one of them is wrong, and the check will find it. If aggregates were
  generated independently, agreement would prove nothing.

### Why numpy, pandas, DuckDB, and Parquet

- **NumPy** is used for the random draws (Beta, Poisson, lognormal,
  choice) because drawing 50,000 user attributes or millions of event
  attributes one at a time in pure Python would be slow, and because a
  single seeded generator (`np.random.default_rng(42)`) makes the whole
  run reproducible.
- **pandas** is used for tabular assembly, joins, and group-bys — the
  parts of generation that look like data work rather than random draws.
- **Parquet** is the storage format because it is columnar and compressed:
  analytic queries usually touch a few columns of millions of rows, and
  Parquet reads only those columns. CSV would be larger and slower, and
  would lose types (dates become strings).
- **DuckDB** is the query engine because it reads Parquet directly, runs
  real SQL (including `MEDIAN`, window functions, and `DATE_TRUNC`), and
  needs no server, no warehouse, and no cost. That is the whole reason the
  project can run locally in a Codespace for free. It is not a claim that
  DuckDB replaces a production warehouse; it is a deliberate choice for a
  portfolio project that must be runnable by anyone who clones the repo.

### What a seed does and does not guarantee

A seed guarantees **reproducibility of this generator, on this code, with
these library versions**: run it again with seed 42 and you get the same
rows, the same row counts, and the same metric values. That is what makes
the validation report a checkable claim rather than a screenshot.

A seed does **not** guarantee:

- That the data is realistic. Realism comes from the distributions and
  relationships you chose, not from the seed. A seed only freezes one draw
  from those distributions.
- That results generalise. One seed is one synthetic world. If a metric
  looks fragile, the honest answer is to rerun with other seeds and see
  whether it survives, not to quote seed 42 as if it were a population.
- That the data is unbiased or representative. The country shares, app
  mix, and acquisition mix are illustrative choices written into the
  generator, not estimates from any real population.

If an interviewer asks "is seed 42 special?", the answer is no: it is an
arbitrary fixed number, chosen so that results are stable across runs.
Any other seed would give slightly different numbers and the same
structure. That is a strength to state plainly, not something to be
embarrassed about.

### What you need to know to make this work

- The command sequence, in order: create a venv, install
  `requirements.txt`, run `generate_phase1.py`, then run
  `validate_phase1.py`. Validation is not optional decoration; it is how
  you know the generation step actually worked.
- The window is fixed: 2026-01-01 to 2026-03-31, 90 days. Cohorts, months,
  and retention windows are all defined against those dates.
- The data is **not committed** to the repo. If someone clones the repo and
  asks "where is the data?", the answer is "generate it" — that is by
  design, and `.gitignore` enforces it.
- The generator is vectorised in the hot loops (user-days, sessions,
  impressions) and loop-based where the logic is inherently sequential
  (the day-by-day habit loop, messages along edges). If it feels slow,
  the day loop and the per-creator content loop are the places to look,
  not the random draws.

### Interview Q&A — Stage 1

**Q: Why generate synthetic data instead of using a real dataset?**
A: Because the point is to test whether the tool answers questions
correctly, and that requires known ground truth. With synthetic data I
know exactly which relationships were planted, which tables each metric
comes from, and what the right answer should be. With a real dataset I
would be guessing at the answer key.
Trap: do not say synthetic data is "just as good as real data." It is
good for testing the tool; it says nothing about real users.

**Q: Why does generation order matter?**
A: Because every later table references an earlier one. Users before
edges, edges before messages, content before impressions, impressions
before engagements. Generate in the wrong order and you either invent
dangling references or silently drop rows to hide them.
Trap: do not say "the validator catches it." The validator catches some
of it (see Stage 6). Order is what prevents most of it in the first
place.

**Q: What does the seed actually buy you?**
A: Reproducibility: same code, same seed, same data, same metric values,
so anyone can rerun my validation and get my numbers. It does not buy
realism or generalisability, and I would rerun with other seeds before
claiming a result is robust.
Trap: do not imply seed 42 was tuned or chosen because it gives good
numbers. It is arbitrary, and saying otherwise destroys the credibility
of the whole validation story.

**Q: Why Parquet and DuckDB instead of a real warehouse?**
A: Because the constraint was free, local, and runnable from a clone.
Parquet is columnar, so analytic queries read only the columns they
need; DuckDB queries Parquet directly with real SQL and no server. The
tradeoff is scale and concurrency: this setup is right for a portfolio
demo and wrong for a production warehouse serving many users.
Trap: do not claim DuckDB "scales like" a production warehouse. Name the
tradeoff before the interviewer does.

---

## Stage 2 — Grain, denominators, and joins

### What is actually being done

Every metric in `catalog/measures.yaml` states three things before it
states any SQL: its **grain** (one row of the underlying table represents
what?), its **denominator** (what are we dividing by, and in what units?),
and its **source table**. The schema doc enforces four grain rules:

- DAU counts users, never sessions or impressions.
- Creator metrics count creators, with the denominator stated every time.
- Impression metrics count sampled impressions, and say "sampled" wherever
  that applies.
- A user, a session, an impression, and a content item are never mixed in
  one denominator without labelling.

The 12 tables have four different grains, and confusing them is the single
most common way to produce a plausible, wrong answer:

| Grain | Example table | One row means |
|---|---|---|
| User | `dim_users` | one synthetic person |
| Session | `fact_sessions` | one visit |
| Impression | `fact_content_impressions` | one sampled view of one item |
| Content item | `dim_content` | one published post |
| User × day | `agg_user_daily` | one user active on one day |
| Conversation | `fact_messages` grouped by `conversation_id` | one pair's thread |

### Join fanout, concretely

Join fanout is what happens when a one-to-many join multiplies rows and
you then count the multiplied rows as if they were the original grain.

Example from this project: join `dim_users` (one row per user) to
`fact_sessions` (many rows per user) and then `COUNT(*)` — you are now
counting sessions, not users. Join `dim_content` to
`fact_content_impressions` and count rows — you are counting impressions,
not posts. Join a user to their devices (1–2 rows per user) and then to
their sessions, and the same session appears twice.

The defences, in order of importance:

1. **Aggregate before you join.** Count sessions per user first, then join
   the per-user summary to the user table. The canonical DAU query does
   this: it groups sessions by day and counts distinct users, rather than
   joining first and counting rows.
2. **Count distinct keys, not rows.** `COUNT(DISTINCT user_id)` survives a
   fanout that `COUNT(*)` does not. Every user-counting query in
   `sql/canonical_metrics.sql` uses distinct counts for exactly this
   reason.
3. **Check the row count after the join.** If a join produces more rows
   than the fact table you started from, you have a fanout. That check is
   cheap and catches most mistakes before they become a metric.

### Denominator bias, concretely

Denominator bias is choosing a denominator that makes a rate look better
or worse than it is, usually by dropping the hard cases from the bottom
of the fraction.

Examples that matter in this project:

- **Creator penetration** is daily creators divided by DAU. If you divided
  by registered users instead, the number would collapse, because most
  registered users never publish. If you divided by "users who opened the
  creation screen," it would inflate, because you dropped everyone who
  never tried. The denominator is a modelling decision, and the catalog
  states it so the choice is visible.
- **Orphan post rate** divides by *eligible published items*. If you
  dropped posts published in the last day (which have had no time to get
  engagement), the rate would fall — but that is censoring, not
  improvement. The Phase 1 definition uses all published items in the
  window, and says so.
- **Engagement rate** divides by *sampled* impressions. Calling it
  "engagement rate" without the word "sampled" would imply it was computed
  over every impression ever served. It was not. The catalog keeps the
  word in the definition for that reason.

The general rule to state in an interview: **a rate is meaningless
without its denominator, its eligibility rule, and its time window.** If
any of those three is unstated, the number is not yet a metric, it is
just a fraction someone computed.

### Sampled impressions and aggregates: when to route to each

Impressions in Phase 1 are sampled, not exhaustive: about 3.2 million
sampled impressions stand in for the much larger number of views a real
platform would serve. That sampling is deliberate and documented, and it
drives the routing rule for Phase 2:

- **Answer from aggregates when the question is about a standard metric
  over a standard window.** "What was DAU trend over the last 90 days?"
  should hit `agg_user_daily`, not scan 1.36 million sessions and
  re-derive it. It is faster, cheaper, and — because the aggregates are
  validated against the events — just as correct for that question.
- **Answer from raw events when the question needs a grain the aggregates
  do not have.** "What percentage of first-time creators received a
  comment within 24 hours?" needs content creation, impression, and
  engagement facts joined together; no daily aggregate contains
  time-to-first-comment. Scanning events is the price of that question,
  and the planner should say so in its receipt.
- **Never answer a rare-event or long-tail question from a small sample
  without saying so.** If a filter leaves only a few hundred cases,
  the estimate is unstable, and the honest output is the estimate plus
  its sample size and a warning, not a confident decimal.

This routing rule — cheapest sufficient table, not fastest engine — is
the practical version of the original Aqua query-planning idea: choose
the source that defines the metric correctly first, then worry about
latency.

### What you need to know to make this work

- Be able to point at any metric and say its grain, denominator, and
  source table without looking it up. If you cannot, you do not yet own
  that metric, and an interviewer will find out by asking for a segment
  you have not precomputed.
- Know the four grains cold: user, session, impression, content item.
  Most interview traps in this area are just asking you to count the
  wrong grain and then defending the number.
- Know that `agg_surface_daily` is date × app × surface × country ×
  platform. Summing its DAU across surfaces double-counts users who used
  two surfaces. Summing its impressions does not double-count anything,
  because an impression belongs to exactly one surface. That asymmetry
  is worth stating unprompted: it shows you understand why "sum the
  aggregate" is sometimes right and sometimes wrong.

### Interview Q&A — Stage 2

**Q: A query runs and returns a number. How do you know it is right?**
A: I check three things before I believe it: the grain (what does one
row represent?), the denominator (who is excluded from the bottom of
the fraction, and why?), and the join (did a one-to-many join multiply
rows before I counted them?). A query can be syntactically perfect and
analytically wrong on all three.
Trap: do not answer with "I checked the SQL." SQL correctness and
analytic correctness are different things, and interviewers ask this
question precisely to see whether you know that.

**Q: Why is DAU not just the number of sessions?**
A: Because one user can have many sessions in a day. DAU counts distinct
users with at least one session that day — in this project, mean daily
DAU is 8,897, while sessions per DAU is 1.69, so counting sessions would
overstate DAU by about 69%. The fix is `COUNT(DISTINCT user_id)` after
grouping by day.
Trap: do not quote sessions per DAU as if it were a user count, and do
not let a "DAU" number pass without asking whether it was deduplicated.

**Q: What is join fanout, in one example?**
A: Joining a one-row-per-user table to a many-rows-per-user session table
and then counting rows counts sessions, not users. The defence is to
aggregate to the grain you want before joining, or to count distinct
keys.
Trap: do not say fanout is a performance problem. It is a correctness
problem: it changes the answer, silently.

**Q: When would you answer from aggregates, and when from raw events?**
A: Aggregates for standard metrics over standard windows — DAU trend,
sessions per DAU — because they are validated against the events and far
cheaper to scan. Raw events when the question needs a grain or a
time-to-event the aggregates do not contain, like time to first comment.
The planner's receipt should say which route was taken, so the choice is
auditable.
Trap: do not say "always use raw events for accuracy." Raw events are
not more accurate for a metric the aggregates already encode correctly;
they are just slower. Accuracy comes from the definition, not the table.

---

## Stage 3 — Semantic catalog and DAX-style measures

### What is actually being done

`catalog/measures.yaml` holds the 20 canonical metrics. Each entry has an
`id`, a `name`, a `definition` in plain English, a `grain`, a
`denominator`, a `source` table, and a `dax_like` expression. Example:

```yaml
- id: dau_mean_daily
  name: DAU (mean daily)
  definition: Mean distinct users with at least one session per day.
  grain: user x day
  denominator: distinct users
  source: fact_sessions
  dax_like: "DAU := DISTINCTCOUNT(fact_sessions[user_id])"
```

The hand-written SQL for the same metric lives in
`sql/canonical_metrics.sql`, and the validation script runs that SQL
against the generated data. So there are three representations of each
metric — English definition, DAX-style expression, and SQL — and they
must agree. If they disagree, the metric is not defined yet, no matter
how nice any one representation looks.

### What a measure definition is

A measure definition is a contract, and it has more parts than a formula:

1. **The population.** Who is eligible? (e.g. users with at least one
   session, creators who published, posts that were published in the
   window.)
2. **The grain.** What does one counted unit represent?
3. **The denominator and its eligibility rule.** Who is in the bottom of
   the fraction, and who was excluded, and why?
4. **The time window and the time rule.** Is retention measured on an
   exact day, or on-or-after a day? Is a month a calendar month or a
   rolling 28 days?
5. **The filter semantics.** What does "Instagram DAU" mean — users whose
   primary app is instagram, or users with at least one instagram
   session? Those are different populations, and the catalog must pick
   one.
6. **The lifecycle / authority.** Is this measure reviewed and current, or
   a draft, or superseded? A measure without a lifecycle is how two teams
   end up quoting different "DAUs" with equal confidence.

A formula alone — `DAU / MAU`, say — satisfies none of this. That is why
the catalog stores definitions, not just expressions.

### Why a PM note is not a metric contract

This is a line from the original Aqua design, and it is worth
understanding rather than repeating. In a real analytics system, context
comes from sources with different authority: reviewed metric definitions,
schema documentation, join patterns, worked examples, and informal
working notes. A product manager's note might say "DAU is up," but it does
not define DAU, state its denominator, or survive a schema change. A
reviewed metric contract does all three.

In this project the equivalent distinction is: `catalog/measures.yaml`
plus `sql/canonical_metrics.sql` plus a passing validation run is the
contract. A sentence in a README, a slide, or a chat message is not. If
someone proposes a new metric in a note, the work is to turn it into a
catalog entry with a grain, a denominator, and SQL, and then to validate
it — not to add it to a dashboard because the note sounded convincing.

The same principle applies to retrieval in Phase 2: the planner ranks
catalog context, but relevance is not authority. A keyword match to a
note does not override a metric definition. The planner is built to
prefer the catalog entry, and to say so in its receipt.

### How a DAX-style definition compiles to SQL

DAX is the formula language of Power BI semantic models. This project does
not run a DAX engine — there is no Fabric or Power BI here — but it
borrows the *shape* of DAX definitions because that shape is precise about
aggregation:

- `DISTINCTCOUNT(fact_sessions[user_id])` compiles to
  `COUNT(DISTINCT user_id)` in SQL, grouped by day for DAU.
- `DIVIDE(a, b)` compiles to `a / NULLIF(b, 0)` in SQL: it names the
  denominator and forces you to decide what happens when it is zero,
  instead of letting a division-by-zero error (or a silent null) make
  the decision for you.
- `MEDIAN(fact_sessions[duration_minutes])` compiles to DuckDB's
  `MEDIAN(duration_minutes)`.
- A measure like stickiness, `DAU / MAU`, compiles to two subqueries
  (mean daily DAU for March, distinct users for March) combined by
  division — it is not a single column you can select, because its two
  parts have different grains (day vs month). That grain mismatch is
  exactly the kind of thing a DAX-style definition surfaces and a naive
  SQL query hides.

So when you say "DAX-style" in an interview, mean this: a measure
definition written as an aggregation over a named table and column, with
an explicit denominator, which the planner translates into SQL by a
fixed rule — not free-form SQL written by a model, and not a DAX engine
running somewhere. If the interviewer presses, say plainly: the runtime
is DuckDB SQL; the DAX text is the human-readable contract that the SQL
is compiled from.

### What you need to know to make this work

- The catalog is the single source of truth for metric names. If the
  planner, the UI, and the benchmark disagree on what "DAU" means, the
  catalog is where you fix it, not in three places.
- Adding a metric means adding all three representations (definition,
  DAX-style expression, SQL) and a validation check. Adding only the SQL
  creates a metric nobody else can audit.
- Filter semantics must be decided per metric, not per question. If
  "by app" means primary app for one metric and session app for another,
  say so in both definitions, or you will ship a breakdown that mixes
  two populations in one chart.

### Interview Q&A — Stage 3

**Q: What is a semantic layer, and why not just let people write SQL?**
A: Because SQL lets two people write two different queries that both
call themselves "DAU" and both run successfully. A semantic layer fixes
the definition — population, grain, denominator, window — in one place,
and every question, chart, and benchmark answer goes through it. The
product is agreement on meaning, not query-writing speed.
Trap: do not describe the semantic layer as "a place where SQL lives."
It is a place where definitions live; SQL is compiled from them.

**Q: What is the difference between a metric definition and a formula?**
A: A formula is `DAU / MAU`. A definition also states who counts, over
what window, with what denominator, and what happens at the edges
(zero denominators, censored cohorts, sampled data). Without those, the
formula can be computed ten ways and every way is defensible.
Trap: do not recite a formula and stop. Interviewers will ask for the
denominator next, and "I didn't specify one" is the wrong answer.

**Q: You say "DAX-style" but there is no Power BI here. Is that honest?**
A: Yes, if I say exactly what I mean: the measures are written in a
DAX-like aggregation syntax as a human-readable contract, and they are
compiled to DuckDB SQL by fixed rules. There is no DAX engine running,
no Fabric, no Power BI model. The DAX text is the contract; SQL is the
execution.
Trap: do not let "DAX" imply a Power BI deployment exists, and do not
let "SQL" imply the definition lives in the query. Both mislead in
opposite directions.

**Q: Why is a PM note not a metric contract?**
A: Because a note does not state a population, grain, denominator, or
lifecycle, and it does not survive a schema change or a personnel
change. A contract does all of that, and it is validated against data.
Retrieval relevance is not authority: a note that matches keywords does
not override a reviewed definition.
Trap: do not dismiss product input. The point is not that PM notes are
useless; it is that they are inputs to a definition process, not
definitions themselves.

---

## Stage 4 — Retention and cohorts

### What is actually being done

Retention in this project is cohort retention: take users who signed up
on a given date, then ask what share of them did something later. The
canonical metric is **D7 exact retention**:

> For cohorts signing up 2026-01-01 to 2026-02-21, share active on exactly
> signup_date + 7 days.

Validated value: **22.1%** overall — organic 22.2%, paid 20.0%, referral
22.8%. (Those three numbers are from `docs/validation-phase1.md`; quote
them with the cohort window, or not at all.)

The SQL implements this with an `EXISTS` check per cohort user: is there
a session for that user on exactly the date seven days after signup?
Averaging that 0/1 indicator over the cohort gives the rate. The
by-acquisition version does the same thing, grouped by
`acquisition_source`.

### Exact-day vs rolling retention

These are different metrics with different meanings, and mixing them up
is a classic interview trap:

- **Exact-day (classic) retention, D7:** active on *exactly* day 7 after
  signup. This is strict: a user active on day 6 and day 8 but not day 7
  counts as not retained on D7. It measures a point-in-time habit.
- **Rolling retention, D7:** active on day 7 *or any day after it* (within
  the observation window). This is lenient: the same user counts as
  retained. It measures "came back at some point after day 7."
- **Unbounded retention:** active at any point in a broader future
  window. Even more lenient.

Rolling retention is always at least as high as exact-day retention for
the same cohort, because its condition is weaker. If someone reports a
retention number without saying which kind it is, you cannot compare it
to anything. This project uses exact-day retention for its canonical
metric, and says so in the definition. That strictness is a choice, not
an oversight.

There is also **behaviour retention vs user retention**, which the
project's design notes call out: a user can keep opening the app while
completely stopping posting, commenting, or messaging. Aggregate DAU
would miss that change entirely. That is why the catalog has creator
retention, commenter retention, and messaging retention as separate
concepts, even though Phase 1's canonical set only fully implements user
D7 retention plus daily creator counts. Do not claim behaviour retention
is measured in Phase 1 when only user retention is.

### Eligible cohorts and right-censoring: why D7 stops at 2026-02-21

Right-censoring means the observation window ends before the outcome
could be observed. The window here ends 2026-03-31. A user who signs up
on 2026-03-28 has no day 7 inside the window — their day 7 would be
2026-04-04, which does not exist in the data. Including them in the
denominator would count them as "not retained" when in fact they are
"not yet observable." That biases retention downward, and the bias is
worse for recent cohorts, which is exactly the cohort a product team
most wants to read.

The fix is an eligibility rule: only cohorts whose day 7 falls inside
the window are included. Day 7 of 2026-02-21 is 2026-02-28, safely inside
the window; day 7 of 2026-02-22 would be 2026-03-01, which is also inside
the window, so why stop at 2026-02-21 rather than 2026-03-24 (whose day 7
is 2026-03-31)? Because the generator's own signup distribution only
places in-window signups up to late February (the "during" signups are
drawn over 59 days from 2026-01-01, i.e. to about 2026-02-28), and the
canonical query conservatively restricts to cohorts fully observable
with margin. The point to make in an interview is the principle, not
the exact cutoff date: **define eligibility by observability, and state
the cutoff.** A retention number without a cohort eligibility rule is
not comparable across time, because the most recent cohorts are always
the most censored.

The same logic applies to D30, D60, and D90: each needs a progressively
earlier signup cutoff, which is why long-horizon retention needs a long
window and cannot be read off a 90-day dataset for recent cohorts at all.

### Retention by acquisition, and the planted confounding

Validated D7 retention by acquisition source: organic 22.2%, paid 20.0%,
referral 22.8%. It is tempting to conclude "paid acquisition is worse."
That conclusion is **not supported by this data, by design.**

The generator deliberately makes acquisition source correlate with
latent engagement: paid users start 0.04 lower on the latent engagement
score (a Beta(2,5) draw, mean about 0.29) before any activity is
generated. Latent engagement then drives activity probability. So paid
users retain less in part because they started with lower latent
engagement, not necessarily because the paid channel causes worse
retention. The difference is observational, not causal, and the
synthetic-assumptions doc says so explicitly: "retention by acquisition
source is confounded in the synthetic data on purpose."

This is a gift in an interview, not a flaw, if you handle it right. The
strong answer is: "I see a 2.2-point gap between paid and organic, but
I cannot attribute it to the channel, because in this synthetic data
acquisition is confounded with latent engagement by construction. To
make a causal claim I would need random assignment, or a design that
adjusts for the confounder — and I would say so before recommending any
spend change." The trap answer is to recommend cutting paid spend on
the basis of the raw gap.

### What you need to know to make this work

- The cohort definition: signup date is the cohort date. There is no
  "activation date" cohort in Phase 1's canonical metric, even though
  activation is a real concept. If you want activation cohorts, you must
  define the activation milestone first, then build the cohort from it.
- The eligibility rule and its reason (right-censoring). Be ready to
  recompute the cutoff for D30 on the spot: a D30 cohort must have
  signed up at least 30 days before the window ends.
- The confounding story for acquisition. Know the mechanism (paid → −0.04
  latent), not just the slogan ("confounded").
- The difference between user retention and behaviour retention, and
  which one Phase 1 actually measures.

### Interview Q&A — Stage 4

**Q: What is D7 retention in your project, exactly?**
A: For users signing up between 2026-01-01 and 2026-02-21, the share with
a session on exactly signup date plus seven days. Validated at 22.1%.
The cohort window matters because it is the eligibility rule: only
cohorts whose day 7 is observable inside the data window are counted.
Trap: do not quote 22.1% without the cohort window, and do not call it
"retention" in general — it is D7 *exact-day* retention, not rolling
retention.

**Q: Why not include March signups in D7 retention?**
A: Right-censoring. Their day 7 falls outside or at the edge of the
observation window, so counting them as not retained would confuse
"not yet observable" with "churned." The honest fix is to exclude
ineligible cohorts and state the cutoff, even though it costs sample
size.
Trap: do not say "we have enough data anyway." Sample size is not the
issue; bias is. Including censored cohorts biases recent cohorts down
systematically.

**Q: Paid retention is lower than organic. Should we cut paid spend?**
A: Not on this evidence. In this synthetic data, paid users were given
lower latent engagement by construction, so the gap is confounded. I
would need random assignment or a credible adjustment for the
confounder before making a causal or budget claim. The raw gap is a
starting point for investigation, not a decision.
Trap: do not turn an observational gap into a causal recommendation.
That is the single most common seniority signal in this whole area, and
getting it wrong is worse than saying "I don't know yet."

**Q: Exact-day vs rolling retention — which is better?**
A: Neither is better in general; they answer different questions.
Exact-day measures point-in-time habit and is stricter. Rolling
measures "came back at some point after" and is always at least as high.
The mistake is comparing one team's exact-day number to another team's
rolling number as if they were the same metric.
Trap: do not pick one and defend it as universally correct. State the
definition you used and why it fits the question.

---

## Stage 5 — Creator ecosystem

### What is actually being done

Phase 1's creator metrics, all validated:

- **Daily creators (mean): 1,631** — distinct creators publishing per
  day, averaged over the window.
- **Creator penetration: 19.9%** — daily creators divided by DAU,
  averaged over days. About one in five daily active users publishes on
  a given day, in this synthetic world.
- **Posts per creator (90 days): 22.7** — total published items divided
  by distinct creators in the window.
- **Orphan post rate: 24.6%** — share of published items with zero
  qualifying engagements in the window.
- **Top-1% creator impression share: 9.5%** — sampled impressions on
  content from the top 1% of creators by impressions, divided by all
  sampled impressions.

The generator encodes the creator story in three mechanisms: creator
tiers (top 5%, mid 25%, long-tail 70%) with impression weights 10 / 3 / 1,
so exposure is concentrated by construction; long-tail creators publish
less and fade in the second half of the window (70% of their posts fall
in the first 45 days, and their activity probability is multiplied by
0.6 after day 45); and creator probability itself rises with latent
engagement and social degree.

### Why concentration and orphan rate matter more than total views

Total views can rise while the ecosystem gets sicker. That is the core
creator-marketplace argument, and it is the argument an interviewer for a
social product role most wants to hear you make:

- **Consumption needs supply.** If fewer creators publish, or if new
  creators stop publishing, future consumption has nothing to consume,
  no matter how good ranking is today. Daily creators and creator
  penetration are leading indicators; views are a lagging one.
- **Orphan posts are a feedback failure.** A post with zero engagement is
  not just an unsuccessful post; for a new creator, it is often the last
  post. A 24.6% orphan rate means roughly one in four posts gets no
  response at all in this synthetic data. The diagnostic question that
  follows — what share of *new* creators get a meaningful response within
  24 hours of their first post, and how does that relate to whether they
  publish again? — is more actionable than the overall rate, and it is
  the kind of follow-up a strong candidate proposes unprompted.
- **Concentration is a distribution question, not a volume question.**
  A 9.5% top-1% share in this synthetic data is actually quite mild
  (in a purely uniform world it would be about 1%, so 9.5% already shows
  the tier weighting at work). The point is not whether 9.5% is "good"
  or "bad" in the abstract; it is whether concentration is rising, and
  whether rising concentration coincides with falling long-tail
  participation. If the top 1% takes a growing share while daily creators
  fall, the platform is becoming more dependent on fewer suppliers —
  a fragility, even if total views are up.
- **Views per creator and median views per post matter more than mean
  views.** Means are dominated by hits. A median, and a P10/P90 spread,
  tell you what the typical creator experiences. This project reports
  top-1% share as its concentration summary; in an interview, say you
  would also want the median and the distribution, because a mean alone
  hides exactly the creators at risk.

### What "healthy" means here, and what it does not

A healthy creator ecosystem in this framing is not "more posts." It is:
enough creators publishing, a reasonable share of them getting a
response, new creators surviving past their first post, and exposure not
so concentrated that the long tail gives up. All four are measurable in
Phase 1's tables (daily creators, orphan rate, posts per creator,
top-1% share), even though new-creator survival specifically is a
Phase 3-style cohort question that needs a first-post cohort definition
first.

What Phase 1 does **not** show: creator satisfaction, creator earnings,
or whether any individual creator is "happy." Those need survey or
payout data that Phase 1 does not generate. Do not infer them from
engagement counts.

### What you need to know to make this work

- The denominator for every creator metric. Creator penetration divides
  by DAU, not by registered users. Posts per creator divides by distinct
  creators, not by days. Orphan rate divides by published items.
- The tier mechanism: weights 10 / 3 / 1 are a modelling choice that
  *creates* concentration, so the 9.5% share is a consequence of the
  generator, not a discovery about a real platform. Say that.
- The fade mechanism for long-tail creators, and why it exists: to make
  creator persistence a measurable phenomenon in the data rather than a
  flat rate.

### Interview Q&A — Stage 5

**Q: DAU is flat, watch time is up, but daily creators are down. Is that good?**
A: Not necessarily. Watch time up with creators down means consumption
is being sustained by a shrinking supply base, possibly by concentrating
exposure on fewer creators. I would decompose the creator decline (new
vs retained vs resurrected creators), check orphan rate and top-1%
share, and check whether new creators are getting early feedback —
before calling it a win.
Trap: do not celebrate a consumption metric while ignoring a supply
metric. That is the exact tradeoff this project is built to surface.

**Q: What does a 24.6% orphan post rate tell you?**
A: That roughly one in four published items in this synthetic window
received zero engagements. As a headline it is a feedback-health signal;
as a diagnosis it is incomplete, because I also need to know whether
orphans are concentrated among new creators, and whether orphaned new
creators publish again. The rate motivates the cohort follow-up; it does
not complete it.
Trap: do not propose "boost all orphan posts" as the fix. Distribution
and feedback quality are the levers; indiscriminate boosting just moves
the orphan problem to a different set of posts.

**Q: Is a 9.5% top-1% impression share high?**
A: It is concentrated relative to a uniform baseline (where 1% of
creators would get about 1% of impressions), but the absolute judgement
depends on trend and on what happens to the long tail. In this synthetic
data the concentration is planted by tier weights, so the honest
statement is: "the generator creates concentration, and the metric
detects it." The interview-relevant skill is tracking concentration
*over time* alongside creator counts, not judging one number in isolation.
Trap: do not call 9.5% good or bad without a baseline, a trend, and a
long-tail counterpart metric.

**Q: Why count unique creators, not just posts?**
A: Because 1,000 posts from 10 creators and 1,000 posts from 1,000
creators are different ecosystems with different futures, even though
the post count is identical. Unique creators, creator penetration, and
posts per creator together separate volume from breadth. Counting posts
alone hides supply concentration.
Trap: do not let a "content supply is up" claim pass without asking who
supplied it.

---

## Stage 6 — Validation and reconciliation

### What is actually being done

`scripts/validate_phase1.py` does three distinct jobs, and they prove
different things:

1. **Runs the 20 canonical metric queries** from
   `sql/canonical_metrics.sql` against the generated Parquet, via
   DuckDB views, and records the values in a report.
2. **Runs 11 reconciliation checks** (listed in full below) that compare
   aggregates to events and check foreign keys.
3. **Runs sanity checks** on the metrics: rates must be in [0, 1], counts
   must be positive, nothing may be null. If any reconciliation check
   fails or any metric is insane, the script exits non-zero. Validation
   is a gate, not a report that can be ignored.

### The 11 checks, and what each one actually proves

From `docs/validation-phase1.md`, all passing:

1. `agg_user_daily` sessions sum = `fact_sessions` count (1,355,587).
   Proves the user-daily aggregate did not drop or duplicate sessions.
2. `agg_user_daily` impressions sum = `fact_content_impressions` count
   (3,204,996). Proves the same for impressions at user-day grain.
3. `agg_surface_daily` impressions sum = impressions count. Proves the
   surface aggregate also covers every impression, not just a subset.
4. `agg_surface_daily` engagements sum = engagements count (511,652).
   Proves the surface aggregate covers every engagement.
5. `dim_content` count = `fact_content_creation` count (172,752). Proves
   every published item has exactly one creation event, and vice versa.
6. Sessions user foreign-key violations = 0. Proves every session
   references a real user (anti-join finds no orphans).
7. Impressions viewer foreign-key violations = 0. Same, for viewers.
8. Impressions content foreign-key violations = 0. Same, for content.
9. Messages sender foreign-key violations = 0. Same, for senders.
10. Video plays on non-video content = 0. Proves the play table only
    contains plays of video content — a type-consistency check, not just
    a key check.
11. Maximum daily DAU difference between `agg_user_daily` and sessions
    = 0. Proves the daily aggregate's user count matches the event-level
    distinct-user count on *every* day, not just in total. A total could
    match while individual days were wrong in opposite directions; this
    check rules that out.

### What validation does NOT prove

This is as important as the passes, and interviewers respect candidates
who volunteer it:

- **It does not prove the data is realistic.** Reconciliation proves
  internal consistency: the aggregates faithfully summarise the events
  that were generated. It says nothing about whether those events
  resemble any real platform. Realism is a judgement about distributions
  and relationships, not something a row-count check can certify.
- **It does not prove the metric definitions are right.** The queries
  are validated against the data, but the definitions themselves
  (grain, denominator, window) are human decisions. A perfectly
  reconciled metric can still be the wrong metric for the question.
- **It does not prove causality.** Nothing in validation turns an
  observational gap (paid vs organic retention) into a causal effect.
  Validation proves arithmetic, not attribution.
- **It does not cover every table or every join.** There is no check, for
  example, on recipient foreign keys in messages, or on device
  foreign keys, or on every possible breakdown. Passing 11 checks is
  evidence of care, not a proof of total correctness. Saying "11/11 pass"
  is accurate; saying "the data is proven correct" is not.

### The two real bugs validation caught — tell these as stories

These are your best interview stories in the whole project, because they
are true, specific, and they show debugging rather than just building.
Tell them in this shape: symptom → investigation → root cause → fix →
re-verification.

**Bug 1: the surface aggregate silently dropped 139 engagements.**

- *Symptom:* the first validation run failed one check:
  `agg_surface_daily` engagements summed to 511,513, but
  `fact_content_engagements` had 511,652. A difference of 139.
- *Investigation:* the surface aggregate was built by grouping
  impressions by date × app × surface × country × platform, grouping
  engagements the same way, and left-joining engagements onto
  impressions. That join only keeps engagement groups that already exist
  as impression groups.
- *Root cause:* an engagement's timestamp is its impression time plus
  0–60 minutes. An engagement near midnight can therefore fall on the
  next calendar day, into a date × surface × country × platform
  combination that has no impressions that day. The left join dropped
  exactly those groups — 139 engagements.
- *Fix:* change the merge to an outer join, filling missing DAU,
  impressions, engagements, and dwell with 0. The comment in the
  generator now explains why, so the next person does not "simplify" it
  back to a left join.
- *Re-verification:* regenerated with seed 42 and reran validation:
  surface engagements now equal 511,652 exactly, and all 11 checks pass.
- *Interview lesson:* the bug was not in the data, it was in an
  assumption — that every engagement group must have a matching
  impression group. Aggregations across time boundaries break that
  assumption, and the only reason it was caught is that the check
  compared totals exactly instead of eyeballing them as "close enough."

**Bug 2: a plausible distribution made completion look broken.**

- *Symptom:* the first completion rate, defined as share of video plays
  with `completion_pct >= 0.95`, came out at about 0.7%. That is not a
  plausible completion rate for a video product, and it would have made
  any demo look broken.
- *Investigation:* completion percentages were drawn from a
  Beta(2,2) distribution, which is symmetric around 0.5. The probability
  of exceeding 0.95 under Beta(2,2) is genuinely about 0.7% — the code
  was doing exactly what it was told, and the distribution was the wrong
  choice for the metric's threshold.
- *Root cause:* a distribution and a threshold must be chosen together.
  Beta(2,2) is a reasonable generic shape, but it puts almost no mass
  near 1.0, while a 0.95 completion threshold demands mass near 1.0.
- *Fix:* model completion as a mixture — 35% of plays are full watches
  (`completion_pct = 1.0`), and the rest follow a right-skewed Beta(2,3).
  Validated completion rate is now 35.0%, which is plausible for a
  sampled video metric with a strict 0.95 threshold.
- *Interview lesson:* when a metric looks absurd, check the distribution
  and the threshold before checking the code. And state the fix as a
  modelling choice, not as "tuning the data to look good": the mixture
  is documented in the generator and in this guide, so it is auditable.

### What you need to know to make this work

- How to run validation, what exit code 1 means, and where the report
  goes (`data/phase1/validation_report.json` and `.md`; the `.md` is
  copied to `docs/validation-phase1.md` for the repo).
- That validation is run *after every regeneration*. A report from an
  old run does not validate new data.
- The difference between a reconciliation check (exact equality between
  two independently computed totals), a foreign-key check (anti-join
  must be empty), and a sanity check (rates in range, counts positive).
  Interviewers often ask "how do you test a pipeline?" — this
  three-layer answer is a strong one.

### Interview Q&A — Stage 6

**Q: How do you know your aggregates are correct?**
A: I do not trust them; I reconcile them. The validation script sums
`agg_user_daily` sessions and impressions and compares them exactly to
the event tables, does the same for the surface aggregate, and checks
daily DAU day by day, not just in total. All 11 checks pass on seed 42.
That proves the aggregates faithfully summarise the events I generated.
Trap: do not say this proves the data is realistic or the metrics are
right. It proves internal consistency. Realism and definition quality
are separate judgements.

**Q: Tell me about a bug you found.**
A: Use Bug 1 above, in full: 139 engagements, midnight crossing, left
join assumption, outer join fix, re-verification to exact equality.
Trap: do not tell a bug story without the re-verification step. A fix
you did not rerun is a hypothesis, not a fix.

**Q: Your completion rate changed from 0.7% to 35%. Did you just tune it?**
A: No — I changed a modelling assumption and documented it. Beta(2,2)
puts almost no mass above 0.95, so a 0.95 threshold was mismatched to
the distribution. The mixture (35% full watches, rest Beta(2,3)) is a
stated, auditable choice for what video completion looks like. If an
interviewer disagrees with the mixture, that is a legitimate modelling
debate, and the code makes the assumption visible enough to have it.
Trap: do not hide a distribution change, and do not defend Beta(2,2)
as "standard" when the threshold makes it wrong.

**Q: What does validation not cover?**
A: Realism, definition correctness, causality, and every possible join
— for example, recipient foreign keys and device keys are not all
checked. Passing checks is evidence of care, not a certificate of total
correctness. I say that explicitly so the 11/11 number is not
over-read.
Trap: do not oversell a green test suite. Interviewers trust candidates
who state the limits of their own evidence.

---

## Stage 7 — Phase 2 planner and SQL guard

> Status note (updated after the Phase 2 build): Phase 2 is built and
> scored. The deterministic planner, SQL guard, local web UI, and
> 32-question benchmark were run against the Phase 1 seed-42 data and
> scored 32/32 (see `docs/phase2.md` and `scripts/run_benchmark.py`).
> Quote that score only for that data and that benchmark set.

### What is actually being done

The Phase 2 flow is:

```
Natural-language question
  -> planner: map the question to a catalog metric + filters + breakdown
  -> SQL builder: fill an allowlisted template for that metric
  -> SQL guard: check the built SQL before it runs
  -> DuckDB: execute, with a timer
  -> result + receipt: metric, grain, filters, tables, runtime, chart
```

Concretely, the planner does **intent mapping**, not text generation.
It looks for the metric in the question ("DAU", "D7 retention", "orphan
posts", "top creators"), for filters ("by app", "in March", "for
instagram", "by acquisition source"), and for a breakdown dimension, and
it matches those against the catalog. It then selects a pre-written SQL
template for that metric — the same family of queries as
`sql/canonical_metrics.sql` — and fills in only allowlisted values
(dates from the window, apps from the data, countries from the data).
User text never becomes SQL text. That sentence is the security and
correctness story in one line.

The CLI entry point is `scripts/ask.py "What was DAU by app over the
last 28 days?"` and the web UI is `scripts/app.py`, serving
`http://localhost:8000`. Both go through the same planner and guard, so
a question that passes in the CLI cannot bypass the guard in the UI.

### Deterministic intent mapping vs LLM text-to-SQL

An LLM text-to-SQL system takes a question and a schema and generates
SQL freely. Its strengths are flexibility (it can attempt almost any
question) and its weaknesses are exactly what this project is trying to
avoid: it can invent a table, a column, a metric definition, or a
denominator that sounds right; it can produce SQL that runs and is
analytically wrong; and its output changes between runs, which makes
evaluation hard to reproduce.

A deterministic planner inverts those tradeoffs:

- **What it gains:** reproducibility (same question, same plan, same
  SQL, every time); auditability (the plan, the template, and the guard
  check are all inspectable); and safety (only allowlisted metrics,
  tables, and filter values can appear in SQL at all).
- **What it loses:** coverage. A question about a metric that is not in
  the catalog, or a filter value that is not in the data, cannot be
  answered. That is not a bug to be patched with a fallback that guesses;
  it is the design. The correct output for an uncovered question is
  clarification, not a confident guess.
- **When you would add an LLM:** as a layer *in front of* the planner —
  to paraphrase a question into a catalog metric, or to explain a result
  — never as a replacement for the template and guard. The LLM can
  propose; the planner and guard dispose. If an interviewer asks "why not
  just use an LLM?", that sentence is your answer, and it shows you
  understand where models help (language flexibility) and where they
  hurt (silent definition drift).

### The SQL guard: read-only, allowlisted, checked before execution

The guard is a set of rules applied to the *built* SQL before DuckDB
ever sees it. The rules to be able to state:

1. **Read-only.** The statement must be a `SELECT` or a `WITH ... SELECT`.
   `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, and any
   multi-statement input are rejected. This is enforced in code, not by
   asking the model nicely to be read-only — a prompt is not a control,
   and the original Aqua design makes the same point about bounded
   capabilities.
2. **Allowlisted tables only.** Only the 12 Phase 1 tables (and, later,
   the tables the catalog names) may appear. An unknown table name is a
   rejection, not a runtime error to be debugged live.
3. **Allowlisted filter values only.** Apps, countries, platforms,
   surfaces, and acquisition sources must come from the values actually
   present in the data. A filter for a country that is not in the
   synthetic data is a clarification, not a query that returns zero rows
   and looks like a finding.
4. **Single statement, no comments-as-code tricks.** Semicolons that
   would split the input into multiple statements are rejected.
5. **Checked before execution, every time, in both CLI and UI.** A guard
   that only runs in one entry point is not a guard.

A useful interview line: "The guard does not make the SQL correct; it
makes the SQL *safe*. Correctness comes from the catalog definition and
the template. Safety comes from the guard. Conflating those two is how
unsafe systems get shipped with a 'validated' label."

### Ambiguous and invalid questions: clarification, not hallucination

The planner has three honest outputs, and only one of them is a number:

1. **An answer with a receipt** — metric matched, filters valid, SQL
   passed the guard, result returned with grain, tables, and runtime.
2. **A clarification request** — the question is ambiguous ("How is the
   product doing?"), the metric is not in the catalog, or a filter value
   is not in the data. The response names what is missing and offers
   the valid options, e.g. "I can answer DAU, WAU, MAU, D7 retention,
   creator penetration, or orphan post rate. Which one did you mean?"
3. **A refusal** — the request is not a read-only analytics question
   (e.g. "delete all users"). No SQL is built, no SQL is run.

The evaluation significance of this is large: in a benchmark, a
clarification on an ambiguous question is a *pass*, not a failure,
because the alternative — a confident wrong answer — is the failure
mode that matters in a real analytics tool. A system that answers
everything is not more useful than a system that knows what it does not
know; it is more dangerous, because its errors look like answers.

### Receipts, and why they matter for trust

Every answer carries a receipt: which metric, which grain, which
filters, which tables, which route (aggregate vs raw events), and how
long the query took. A receipt does not make a result causal, complete,
or transferable — the original Aqua design is explicit that a result
receipt supports a claim about *what ran*, not a causal story — but it
does make the result *checkable*. A reviewer can rerun the same SQL,
see the same grain and denominator, and disagree with the definition
from an informed position, rather than guessing what the tool did.

In an interview, connect receipts to trust directly: "Users do not trust
an analytics tool because its answers sound fluent. They trust it
because they can see what it computed, reproduce it, and find its
limits. The receipt is the mechanism for that."

### Async jobs and honest terminal states (architecture context)

The original Aqua design uses asynchronous execution for expensive
queries: submit a bounded query, receive a job ID, poll the same job,
observe a terminal result, and retain or expire it explicitly. A client
timeout is not proof the backend query stopped, so resubmitting blindly
can duplicate expensive work.

The local Phase 2 does not need full async orchestration for a
single-user demo, but the *state honesty* behind it still applies and is
worth knowing: running, empty, failed, partial, expired, and unknown are
different states, not synonyms for "no result." An empty result (a valid
query with zero rows) is not a failure. A failed query is not an empty
result. An expired result is not a current result. A polling cache is
not durable orchestration. If you add async execution later, keep those
distinctions; if you do not, say that the demo runs queries
synchronously and that async is a stated future tradeoff, not a hidden
limitation.

### What you need to know to make this work

- The planner's vocabulary: which metric phrases it recognises, which
  filters are allowlisted, and which breakdowns each metric supports.
  If a demo question falls outside that vocabulary, the honest demo move
  is to show the clarification, not to rephrase until it accidentally
  matches.
- The guard rules, in order, and the fact that they run before execution.
- The three outputs (answer + receipt, clarification, refusal) and when
  each is correct.
- That user text never becomes SQL text. If you can explain why that
  one rule prevents an entire class of injection and hallucination
  failures, you understand the planner better than most people who
  demo text-to-SQL tools.

### Interview Q&A — Stage 7

**Q: Why a deterministic planner instead of an LLM generating SQL?**
A: Because the failure I care about is a query that runs and is
analytically wrong — wrong grain, wrong denominator, invented
definition. A deterministic planner can only produce SQL from catalog
templates with allowlisted values, so that failure class is structurally
reduced, and the same question always produces the same SQL, which
makes evaluation reproducible. An LLM can sit in front of the planner to
paraphrase, but it must not replace the template and guard.
Trap: do not say LLMs are "bad at SQL." They are often fluent at SQL.
The problem is definition drift and non-reproducibility, not syntax.

**Q: How do you prevent SQL injection if users type questions?**
A: User text is never interpolated into SQL. The planner maps text to a
metric id and to allowlisted filter values, then fills a pre-written
template. The guard then checks the built SQL is a single read-only
SELECT over allowlisted tables before DuckDB runs it. There is no path
from keystrokes to query text.
Trap: do not say "we use parameterised queries" and stop, unless you can
also say what is parameterised (values) and what is not (metric and
table choice, which come from the allowlist). Both halves matter.

**Q: What does your tool do when it does not understand a question?**
A: It asks for clarification and names the valid options, or it refuses
if the request is not a read-only analytics question. It does not guess
a metric, invent a filter, or return zero rows as if that were a
finding. In the benchmark, a correct clarification on an ambiguous
question counts as a pass, because a confident wrong answer is the
failure that matters.
Trap: do not apologise for clarification as if it were a weakness.
Selective abstention is a feature of a trustworthy analytics tool, and
interviewers for this kind of role know it.

**Q: What does a receipt prove, and what does it not prove?**
A: It proves what ran: metric, grain, filters, tables, route, runtime.
It does not prove the data is complete, the definition is the right one
for the business question, or that any observed difference is causal.
Provenance, analytic validity, and causal validity are three different
claims, and the receipt only covers the first.
Trap: do not let a receipt stand in for a causal argument. That
overclaim is exactly what the trust layer of the original design warns
against.

---

## Stage 8 — Benchmarks and evaluation

### What is actually being done (by design)

Phase 2's benchmark is a set of 30 questions with expected answers,
run by `scripts/run_benchmark.py` against the Phase 1 data. The intended
distribution, following the project's Phase 2 plan, is roughly:

- Simple metrics (single metric, no joins beyond the definition).
- Joins and segments (a metric broken down by app, country, platform,
  surface, or acquisition source).
- Cohorts and funnels (D7 retention, creator counts, orphan analysis).
- Experiments (questions scoped to the experiment registry in
  `catalog/experiments.yaml`; full assignment-based scoping needs the
  Phase 3 assignment tables, so Phase 2 experiment questions are about
  the registry and its primary metrics, not about treatment effects
  from assignments that do not exist yet).
- Complex diagnostics (questions that need several tables, e.g. orphan
  posts plus concentration plus creator counts together).
- Ambiguous or invalid questions (where the correct output is
  clarification or refusal, not a number).

### What the benchmark measures, and why those four dimensions

1. **Result correctness.** Does the returned number match the gold
   answer computed from the canonical SQL? This is the obvious one, and
   it is not sufficient on its own.
2. **Metric-definition adherence.** Did the planner use the catalog
   metric, grain, and denominator — or did it compute a plausible
   lookalike? A benchmark that only checks the final number cannot
   distinguish a correct metric from a coincidentally close one. The
   planner's receipt is what makes this checkable: the metric id in the
   receipt must match the expected metric id.
3. **SQL safety.** Did every generated query pass the guard? Did any
   question produce a non-read-only statement, an unallowlisted table,
   or an unallowlisted filter value? Safety is scored as a hard gate:
   a single unsafe query is a failure of the system, not a partial-credit
   item. This mirrors the original design's point that hard failures
   cannot be averaged away.
4. **Latency.** How long did each query take, and did aggregate-routed
   questions actually run faster than raw-event questions? Latency is
   reported, not just averaged: a system that is fast on simple metrics
   and slow on diagnostics has a different profile from one that is
   uniformly mediocre, and the benchmark should show that.

A useful fifth dimension, if you discuss evaluation more deeply, is
**clarification quality**: on ambiguous questions, did the system ask
for the missing piece and offer valid options, rather than guessing or
giving a generic error? That is scored by the expected-output type
(clarification vs answer vs refusal), not by string-matching the
clarification text.

### How to talk about accuracy honestly

This is where many portfolio projects lose credibility, so be precise:

- **Report accuracy per category, not just overall.** "28/30 overall"
  hides whether the two misses were simple metrics (bad) or ambiguous
  questions (different kind of miss). Category accuracy is the honest
  unit.
- **State the denominator.** 30 questions, over Phase 1 data, seed 42,
  with the planner version at the time of the run. A score without a
  question count, a data version, and a system version is not a score.
- **Do not compare against a baseline you did not run.** The project's
  longer-term plan mentions comparing a basic text-to-SQL system, a
  schema-aware system, and the semantic planner. Until those baselines
  are actually run on the same 30 questions, do not claim an improvement
  over them. "Designed to be compared" is not "outperforms."
- **Do not tune the benchmark to the planner.** If a question is
  consistently missed, the honest responses are: fix the planner, fix
  the question if it was ambiguous, or report the miss. Quietly
  dropping hard questions inflates the score and destroys the
  benchmark's value as evidence.
- **Do not quote a score that does not exist yet.** At the time of
  writing this guide, Phase 2 is in progress and no benchmark score has
  been run. If asked in an interview before it exists, say: "The
  benchmark is designed and the release bar is X; the score is not yet
  measured." That sentence is stronger than any invented number,
  because it is true and it shows you know what a release bar is for.

### What you need to know to make this work

- The benchmark file location and runner command, and the fact that the
  benchmark requires generated Phase 1 data (generate first, then run).
- The expected-output type for each question: answer, clarification, or
  refusal. Knowing this in advance is what lets you demo a "failure"
  case confidently instead of being embarrassed by it.
- That safety is a hard gate, not a weighted score. One unsafe query
  fails the run in spirit, even if the average looks fine.

### Interview Q&A — Stage 8

**Q: How do you evaluate a natural-language analytics tool?**
A: On a fixed benchmark with gold answers, scored on four dimensions:
result correctness, metric-definition adherence (did it use the catalog
metric, not a lookalike?), SQL safety as a hard gate, and latency. I
also score clarification quality on ambiguous questions, because a
correct clarification is a pass and a confident wrong answer is the
failure that matters.
Trap: do not answer with only "accuracy." Accuracy without definition
adherence and safety is how a fluent, wrong system scores well.

**Q: What is your benchmark score?**
A: [Only answer this with a real, run number, per category, with the
data version and planner version. Before the benchmark is run, say:]
The benchmark is designed — 30 questions across simple metrics,
segments, cohorts, experiments, diagnostics, and ambiguous cases — and
the release bar is defined, but the score has not been measured yet, so
I am not quoting one. Quoting an unrun score would be exactly the kind
of unverified claim this project is built to avoid.
Trap: do not invent, round up, or "estimate" a score. Interviewers can
tell, and it undermines every verified number you have already earned.

**Q: Why is SQL safety a hard gate rather than a weighted score?**
A: Because one unsafe query — a write, a drop, an unallowlisted table —
is a system failure, not a partial-credit miss. Averaging it away with
29 good answers hides the one failure that matters most. Hard failures
cannot be averaged away; that principle comes from the original
evaluation design and I have kept it.
Trap: do not trade safety for accuracy in your answer. A system that is
accurate but unsafe is not "mostly good"; it is not shippable.

**Q: How do you avoid overfitting to your own benchmark?**
A: By fixing the questions before tuning the planner, reporting per
category, and treating a consistently missed question as a planner bug,
a question bug, or a reported miss — never as a question to quietly
drop. And by not claiming a comparison to baselines I have not run.
Trap: do not say "my benchmark is representative" without saying of
what, and how it was fixed before tuning began.

---

## Stage 9 — Architecture and tradeoffs

### The original architecture, in one paragraph

The high-level Aqua architecture shared for this project has six layers
(and you should be able to sketch them from memory):

1. **Remote systems, not stored inside Aqua.** A Fabric / OneLake
   warehouse (SQL endpoint), Power BI semantic models (queried by DAX),
   Scuba / Interana behavioural events (BQL or saved-query replay), an
   ECS deployment system (configuration, feature flags, rollout), ExP
   experiment results (workspace → feature → experiment → step →
   analysis → scorecard), and approved external feedback/research inputs.
   Aqua does not store source data; it queries it through adapters, and
   exact upstream ingestion and lineage are audited elsewhere, not
   inside Aqua.
2. **Local Aqua process: a Node / TypeScript MCP server over stdio.**
   The host agent calls typed tools; adapters return rows, CSV, or
   metadata. Adapters cover SQL (T-SQL, SELECT / read-only CTE guard),
   DAX (Power BI REST `executeQueries`), Scuba (HTTP API, BQL or replay),
   and source-edition Fabric / OneLake helpers (notebooks, job status)
   that are *not* exposed in the managed allowlist.
3. **On-disk knowledge / catalog.** Schemas, Power BI metric definitions,
   examples, glossary, and relationships, compiled into a catalog at
   runtime, with progressive context: mandatory source rules survive
   retrieval, and deferred references expand within the same inquiry
   without resetting it.
4. **Execution boundary: routing, policy, identity.** The agent resolves
   intent and picks tools; typed schemas, SQL guards, lifecycle
   controls, and backend permissions bound execution. Identity is
   user-scoped (Azure CLI / Entra tokens for the user, a separate token
   for Scuba); a capability that is not shipped or registered must not
   be recoverable through an indirect route.
5. **Transient runtime state.** In-process maps for inquiries, query
   runs, result receipts, claims, and checks. Job polling caches, TTLs,
   and byte/count caps bound memory. A receipt id is not a durable audit
   database, and process loss is a separate concern from upstream data
   completeness.
6. **User / model boundary and local files.** The host is a Copilot CLI
   agent plus an approved model service; the LLM is outside Aqua, and
   only prompt, context, and selected results cross the boundary under
   host policy. Persistent local files are user-owned configuration,
   notes, and explicitly saved outputs — never credentials, never
   production data by default.

Alongside Aqua sits **Raven**, a local skill (not its own MCP server):
feedback intake and review, then ExP linkage, then review + measurement
→ structured model → static HTML renderer. Raven's scoring is covered
below.

Offline, there is an evaluation and build sidecar: repository tests,
source-backed evaluation cases, replay / live runs under approved scope,
and a curated, hashed release (compiled allowlist, curated knowledge,
release manifest, no credentials or raw customer corpus in the package).

### The local SocialPulse architecture, and why it differs

SocialPulse NLQ keeps the *shape* of that architecture and replaces
every remote dependency with a local equivalent:

| Original layer | SocialPulse local equivalent | What changes |
|---|---|---|
| Fabric / OneLake warehouse | DuckDB over Parquet | No cloud, no cost, no scale, no concurrency |
| Power BI semantic models (DAX) | `catalog/measures.yaml` + DAX-style text compiled to SQL | No DAX engine; the contract is kept, the runtime is SQL |
| Scuba / Interana events (BQL) | `fact_content_impressions` + event tables, queried by SQL | No BQL, no saved-query replay service |
| ECS deployment | `catalog/experiments.yaml` (registry) | Metadata only; no rollout control |
| ExP results | Manual scorecard logic in Phase 2/3; assignment tables in Phase 3 | No remote experiment service |
| Node / TypeScript MCP server (stdio) | Python package + CLI + local web UI | No MCP server in Phase 2; typed functions instead of typed MCP tools |
| Copilot CLI agent + model service | Deterministic planner + web UI (the user asks directly) | No Copilot dependency, no model key, no cloud LLM |
| Azure / Entra identity | Local read-only guard + allowlist | No user-scoped tokens, because there is no remote system to authenticate to |

### Why local was chosen, and what is lost and gained

**Why local:** the constraints were a public GitHub repo, free tooling,
and a demo anyone can run from a clone in a Codespace. A Fabric
workspace, a Power BI service, and a Scuba instance cannot be cloned.
A Python + DuckDB + Parquet stack can. The decision was also about
interview defensibility: a system you can run, break, and fix yourself
is a system you can defend; a system that depends on services you do
not control is a system you can only describe.

**What is gained:** reproducibility (seeded data, deterministic
planner), zero cost, no credentials in the repo, no vendor lock-in for
the demo, and a complete vertical slice — data, catalog, planner,
guard, UI, benchmark — that one person can actually maintain.

**What is lost, and say it unprompted:**

- **Scale.** Millions of rows in DuckDB locally are not billions of
  rows in a warehouse. Query latency, partitioning, and concurrency
  behaviour at production scale are not demonstrated here.
- **Authorisation.** There is no per-user identity, no Entra token, no
  row-level permission. The guard is a code-level control, not an
  identity system. Do not claim otherwise.
- **Freshness and lineage.** Real warehouses have ingestion, refresh,
  and lineage. This project's data is generated once per run and is
  frozen at seed 42. "Current data" means "the last generated run," not
  a live feed.
- **The MCP surface.** The original system exposes typed MCP tools to a
  host agent. Phase 2's planner is a library + CLI + UI, not an MCP
  server. That is a stated scope decision; an MCP wrapper is a natural
  Phase 3 addition, and naming it as such shows you know the difference
  between "same idea" and "same interface."
- **Browser / Playwright observation.** In the original design,
  Playwright is a host tool for opening ECS / ExP pages and inspecting
  rendered output — it is not an Aqua database connector. SocialPulse
  does not need it, and dropping it loses nothing for the core
  analytics loop. If asked, say that explicitly: browser observation
  proves what is visible on a page; it does not prove what the database
  contains.

### Raven-style decision scoring, without inventing causal lift

Raven, in the original design, composes feedback, Aqua measurements,
reviewed historical context, and a reproducible local dashboard. Its
model is:

- **Demand, out of 30.** What users are asking for, from authorised
  feedback, after atomic review (individual asks, reviewed, not bulk
  sentiment harvested without admission).
- **Opportunity, out of 30.** Reach, Benefit, Frequency, and Leverage —
  four sub-scores that together describe how much good a change could
  do, and for whom, how often, and with what leverage.
- **Overall, out of 60.** Demand + Opportunity. No auto-change scores,
  no lift forecast, no hidden model.

Three boundaries make this defensible, and you should state all three:

1. **Measure versus model.** Aqua measurements are measured; Opportunity
   sub-scores are modelled judgements, labelled as such. Mixing them
   into one "score" without labelling which parts are measured and
   which are modelled is how a dashboard starts lying politely.
2. **Historical lift is not a forecast.** A linked ExP result describes
   what happened for a linked step and population. It does not predict
   what a different feature will do for a different population. Raven
   requires review for candidate similarity before any historical result
   is even shown alongside a proposal.
3. **Output is a snapshot, not a service.** Raven renders a static HTML
   file from a saved JSON model. It is not a live BI service, it does
   not refresh, and it does not auto-publish. That limitation is a
   feature for trust: what you see is exactly what was reviewed and
   saved.

In SocialPulse, the Phase 2 experiment list (`catalog/experiments.yaml`)
is the seed of this: experiments have a primary metric and guardrails,
but no treatment effect is computed from assignments that do not exist
yet. Do not let a demo question ("did Experiment B work?") pull you
into inventing an effect. The correct Phase 2 answer is to show the
experiment's definition, primary metric, and guardrails, and to state
that assignment-based analysis arrives with Phase 3 tables.

### What you need to know to make this work

- Sketch the six original layers from memory, then the local mapping
  table above, without notes. That sketch is the architecture answer.
- The three losses (scale, authorisation, freshness/lineage) and the
  three Raven boundaries (measure vs model, lift vs forecast, snapshot
  vs service). Interviewers test tradeoffs more than diagrams.
- That "local" is a constraint-driven choice, not a claim that local is
  better in general. For a production system serving many users with
  fresh data and per-user permissions, the remote architecture is the
  right one — and saying so shows judgement rather than defensiveness.

### Interview Q&A — Stage 9

**Q: Walk me through your architecture.**
A: Sketch the six original layers, then the local mapping: DuckDB over
Parquet replaces Fabric, the YAML catalog plus DAX-style text replaces
Power BI models, event tables replace Scuba, the YAML registry replaces
ECS/ExP metadata, and a deterministic Python planner plus a local UI
replaces the MCP server and the Copilot host. Then state what is lost:
scale, per-user authorisation, and live freshness.
Trap: do not present the local stack as if it were the original stack.
Name every replacement and its cost, or the architecture answer reads
as confusion rather than adaptation.

**Q: Why not just use Fabric / Power BI / Scuba like the original?**
A: Because this project's constraints are a public repo, zero cost, and
a demo anyone can clone and run. Those services cannot be cloned, and
they would make the demo unrunnable and unauditable for a reviewer.
The local stack is the right choice *for this project and its
constraints*, not a general claim that local beats cloud.
Trap: do not argue that DuckDB is "better than" a warehouse. Argue
fitness for constraints, and concede the scale and authorisation losses.

**Q: What is Raven, and how is it different from another dashboard?**
A: Raven is a decision structure, not a chart pack. It takes reviewed
feedback (Demand, out of 30) and a modelled opportunity (Opportunity,
out of 30: Reach, Benefit, Frequency, Leverage) and renders a static
snapshot from a saved model. It does not invent a lift forecast, it
does not auto-change scores, and it labels measured versus modelled
inputs separately. A dashboard shows numbers; Raven forces the question
"what did we measure, what did we model, and what was reviewed?"
Trap: do not describe Raven as an AI that decides. It structures a human
decision; the human, the review, and the snapshot are the point.

**Q: Your system has no authentication. Is that a problem?**
A: For this demo, no — there is no remote system and no real user data,
so there is nothing to authenticate to, and the guard is a code-level
read-only control. For a production system, yes, it would be a critical
gap: user-scoped identity, per-user permissions, and token handling
would be required, and the original design's Entra / token boundary is
exactly that layer. I can describe it; I have not built it here, because
this project's scope does not need it.
Trap: do not claim the SQL guard "is" authentication. A guard constrains
what code can do; authentication constrains who can ask. They are
different layers.

---

## Stage 10 — Interview Q&A drill (cross-cutting)

Stages 1–9 each already end with four Q&As (a short strong answer plus a
trap). This stage is the cross-cutting drill: questions that span stages,
plus a practice plan. Use these for mock interviews, out loud, without
notes, until the numbers and the boundaries come without hesitation.

**Q: Give me the 60-second tour of the project.**
A: "SocialPulse NLQ is an independent synthetic natural-language
analytics tool. Phase 1 generates 50,000 users, 12 tables, 90 days from
seed 42, defines 20 metrics in a catalog with grain and denominator, and
validates that aggregates reconcile exactly to events — 11 checks pass.
Phase 2 adds a deterministic planner that maps questions to catalog
metrics, builds SQL from allowlisted templates, guards it read-only,
runs it on DuckDB, and returns a receipt. If a question is ambiguous, it
clarifies instead of guessing. The thesis is semantic correctness, not
fluent text."
Trap: do not open with tools ("I used DuckDB and pandas"). Open with
the problem and the thesis, then name tools as choices.

**Q: What is the hardest technical decision in the project?**
A: "Choosing determinism over flexibility in the planner. An LLM could
answer more questions, but it could also invent definitions, and its
output would not be reproducible. I traded coverage for auditability and
reproducibility, and I made the tradeoff explicit: uncovered questions
get clarification, not guesses."
Trap: do not frame determinism as a limitation you apologise for. Frame
it as a designed tradeoff with a named cost (coverage) and a named
benefit (auditability).

**Q: How would you scale this to production?**
A: "Three changes, in order: a real warehouse and partitioned storage for
scale and concurrency; per-user identity and row-level permissions,
because a code guard is not authentication; and ingestion with
freshness and lineage, because generated frozen data is not a live feed.
I would keep the catalog, the guard, and the receipt — those layers
scale; only the storage and identity layers change."
Trap: do not say "just use a bigger DuckDB" or "just add an LLM." Name
the layers that actually change.

**Q: A stakeholder says DAU fell 2% yesterday. What do you do?**
A: "I do not react to the headline. I decompose it: new vs retained vs
resurrected vs churned users; by app, country, platform, and version;
against the same weekday last week and a rolling baseline; and I check
instrumentation first — logging, eligibility, app version — before
attributing it to behaviour. A 2% move is not an insight; the
decomposition is."
Trap: do not propose a product fix for a move you have not decomposed,
and do not skip the instrumentation check. Tracking failures look
exactly like behaviour changes.

**Q: You used AI to build this. What did you actually do?**
A: See "What AI did vs what I decided and verified" below. The short
version: AI drafted code and text; I chose the architecture, the metric
definitions, the validation bar, and the tradeoffs, and I verified the
results by running them and by debugging the two real failures
validation caught. If you cannot say that sentence truthfully about a
part of the project, do not demo that part until you can.
Trap: do not hide AI use, and do not overclaim it either. "AI built it"
and "I built it alone" are both wrong here. The honest middle is
verifiable: decisions, definitions, and debugging are yours.

**Q: Is this Meta's Aqua?**
A: "No. It is an independent synthetic project inspired by the idea of a
natural-language analytics tool. It does not copy internal code, data,
or definitions, and it does not claim to be Meta's tool or to describe
Meta's actual dashboards. The name SocialPulse NLQ is deliberate: it
keeps that boundary visible."
Trap: do not hedge this answer, do not say "basically Aqua," and do not
let an interviewer reframe it as an internal tool. Clarity here protects
both you and the project.

### How to practise

1. **Blind sketch.** Draw the generation order, the grain table, and the
   local architecture mapping from memory, on paper, in under five
   minutes each. If you cannot, reread only that stage, then redraw.
2. **Number drill.** Recite, without notes: 50,000 users; 12 tables;
   90 days; seed 42; DAU mean 8,897; March MAU 46,818; stickiness 0.215;
   D7 retention 22.1%; daily creators 1,631; creator penetration 19.9%;
   orphan rate 24.6%; top-1% share 9.5%; engagement rate 16.0%;
   two-way conversation rate 66.8%. Then recite the two bug stories
   (139 engagements; Beta(2,2) → 0.7%).
3. **Trap drill.** For each stage, have someone ask the trap version of
   a question ("so paid is worse, right?", "so the data is proven
   correct?", "so DuckDB scales like a warehouse?") and practise the
   correction without getting defensive. The correction *is* the
   seniority signal.
4. **Live demo drill.** Run the generator, run validation, and ask three
   questions through the planner (one simple, one segmented, one
   ambiguous) in front of someone, narrating grain, denominator, route,
   and receipt as you go. If the ambiguous question embarrasses you,
   you have not yet internalised that clarification is a pass.

---

## One-page cheat sheet

Copy this section alone for last-minute revision. Numbers are from
`docs/validation-phase1.md`, seed 42, window 2026-01-01 to 2026-03-31.

**Scale:** 50,000 users · 12 tables · 90 days · seed 42 · ~7.4M rows
total across tables (see validation report for the per-table counts).

**Tables:** dim_users 50,000 · dim_devices 64,884 · dim_content 172,752 ·
dim_social_edges 473,943 · fact_sessions 1,355,587 ·
fact_content_impressions 3,204,996 (sampled) ·
fact_content_engagements 511,652 · fact_video_plays 937,205 ·
fact_messages 462,920 · fact_content_creation 172,752 ·
agg_user_daily 800,762 · agg_surface_daily 15,197.

**Headline metrics:** DAU mean 8,897 · WAU mean 26,652 · March MAU
46,818 · DAU/MAU 0.215 · new users in window 25,071 · D7 exact retention
22.1% (organic 22.2%, paid 20.0%, referral 22.8%) · sessions per DAU 1.69
· median session 12.0 min · daily creators 1,631 · creator penetration
19.9% · posts per creator 22.7 · orphan post rate 24.6% · top-1% creator
share 9.5% · video completion (≥0.95) 35.0% · watch time per viewer-day
0.90 min · messaging DAU 3,821 · two-way conversation rate 66.8% ·
commenter penetration 9.1% · engagement rate per sampled impression
16.0%.

**Validation:** 20 metrics sane · 11 reconciliation checks pass ·
foreign-key violations 0 · daily DAU difference 0 · exit code 0.

**Generation order:** users → devices/edges → content → user-days →
sessions → impressions (content cutoff) → engagements / video plays →
messages (along edges, with replies) → aggregates (group-bys, never
hand-typed).

**Grain rule:** user ≠ session ≠ impression ≠ content item. Count
distinct keys, aggregate before joining, state the denominator.

**Catalog rule:** metric = population + grain + denominator + window +
filter semantics + lifecycle. A formula alone is not a definition. A PM
note is an input, not a contract.

**Retention rule:** D7 exact = active on exactly signup + 7 days.
Eligibility = day 7 observable in window (cohorts to 2026-02-21).
Rolling ≥ exact, always. Paid vs organic gap is confounded by design
(paid latent −0.04); do not make a causal or budget claim from it.

**Creator rule:** volume ≠ health. Watch daily creators, creator
penetration, orphan rate, and top-1% share together. Total views can
rise while supply shrinks.

**Planner rule:** question → catalog metric + allowlisted filters →
template SQL → read-only guard → DuckDB → result + receipt. User text
never becomes SQL text. Ambiguous/invalid → clarification, not a guess.
Unsafe → refusal, no SQL built.

**Receipt rule:** a receipt proves what ran (metric, grain, filters,
tables, route, runtime). It does not prove completeness, definition
correctness, or causality.

**Architecture rule:** local Python + DuckDB + Parquet replaces Fabric /
Power BI / Scuba / ECS / ExP for a free, clonable demo. Gained:
reproducibility, zero cost, no credentials. Lost: production scale,
per-user authorisation, live freshness/lineage. MCP server and Copilot
host are out of scope for Phase 2 by design.

**Raven rule:** Demand/30 + Opportunity/30 (Reach, Benefit, Frequency,
Leverage) = Overall/60. Measured vs modelled labelled separately.
Historical lift is not a forecast. Output is a static snapshot, not a
live service, and scores are never auto-changed.

**Bugs to tell:** (1) Surface left join dropped 139 midnight-crossing
engagements → outer join → exact reconciliation. (2) Beta(2,2) +
0.95 threshold → 0.7% completion → 35% full-watch mixture + Beta(2,3)
→ 35.0%. Both rerun and re-verified on seed 42.

**Lines never to cross:** do not claim this is Meta's tool; do not claim
a billion rows; do not claim real-user findings; do not turn synthetic
correlation into causation; do not quote a benchmark score that has not
been run; do not mix denominators without labelling them.

---

## What AI did vs what I decided and verified (say this honestly)

Use this framing verbatim if it is true for you, and adjust any line
that is not true rather than reciting it:

> "I used AI assistance to draft code, documentation, and this guide.
> What I decided myself was the architecture (local Python + DuckDB
> instead of cloud services, because the project had to be free and
> clonable), the metric definitions (grain, denominator, and window for
> each of the 20 canonical metrics), the validation bar (exact
> reconciliation, foreign-key checks, and sanity ranges, with a
> non-zero exit on failure), and the planner tradeoff (deterministic
> templates and a read-only guard instead of free-form LLM SQL, with
> clarification instead of guessing). What I verified myself was the
> output: I ran the generator and the validator, I saw the 11 checks
> pass, and I debugged the two failures validation actually caught —
> the 139 dropped engagements and the mismatched completion
> distribution — and reran until they passed. The parts I have not
> verified, I do not claim: Phase 2's benchmark has not been scored yet,
> and nothing in this synthetic data describes real users."

Why this framing works in an interview:

- It does not hide AI use, which interviewers will assume anyway and
  will trust you more for stating.
- It separates drafting (AI) from deciding (you) and verifying (you
  running code and fixing failures). Decisions and verification are the
  parts that demonstrate judgement, and they are the parts a candidate
  who merely pasted output cannot reproduce under questioning.
- It ends with an explicit non-claim. A candidate who volunteers what
  they have *not* verified is far more credible on everything else they
  say they *have* verified.

If an interviewer pushes — "but did you write the generator?" — the
honest, strong answer is: "AI drafted it from my design: the generation
order, the distributions, the planted relationships, and the validation
checks were my specification. I read it, I ran it, I fixed what
validation caught, and I can walk you through any function in it now."
Then do exactly that: walk through one function, e.g. the user-day
habit loop or the impression cutoff, without notes. That walkthrough is
worth more than any claim about authorship.

---

## Sources inside this repo (for follow-up reading)

- `README.md` — project scope, Phase 1 / Phase 2 summary, quickstart.
- `docs/schema.md` — 24-table design, Phase 1 tables marked, grain rules.
- `docs/synthetic-assumptions.md` — planted relationships and what is
  deliberately not encoded.
- `docs/architecture-local.md` — local architecture diagram in text form.
- `docs/validation-phase1.md` — the only source for the numbers quoted
  in this guide (seed 42, 90 days, 20 metrics, 11 checks).
- `catalog/measures.yaml` — the 20 metric definitions with grain,
  denominator, source, and DAX-style expression.
- `catalog/experiments.yaml` — the experiment registry for Phase 2.
- `scripts/generate_phase1.py` — the generator, in dependency order.
- `scripts/validate_phase1.py` — the validator and its three layers of
  checks.
- `sql/canonical_metrics.sql` — the hand-written metric SQL.

If a number in this guide ever disagrees with
`docs/validation-phase1.md`, trust the validation report and fix this
guide — the report is generated from a run, and this guide is not.
