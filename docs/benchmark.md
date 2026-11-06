# Benchmark v1 — method, split, oracle

Phase 1, slice 1. The frozen benchmark is version **v1**: 39 single-shot
cases (`evals/benchmark_phase2.json`) + 10 multi-turn conversations
(`evals/conversation_phase3.json`) = **49 cases**, data pinned to 500K
users, seed 42, window 2026-01-01..2026-03-31. The freeze lives in
`evals/benchmark_v1_manifest.json`; the harness
(`src/socialpulse_nlq/eval_harness.py`, CLI `scripts/run_benchmark_v1.py`)
refuses to run if the manifest and the eval files disagree, or if the data
pin does not match the data dir.

**v2 is now the current version** (slice 2, part A) — see
[Benchmark v2](#benchmark-v2--slice-2-part-a) below. v1 stays frozen and
runnable for comparison (`scripts/run_benchmark_v1.py`); nothing in the
v1 files changed except the two approved fixes (c8 description, b08
canonical SQL), which are corrections to stale/wrong values, not a
re-baseline.

## The five rubric items (scored separately, never collapsed)

1. **SQL syntax** — the planned SQL parses (DuckDB EXPLAIN, no execution).
2. **SQL safety** — the read-only guard accepts it, reason recorded.
3. **Execution** — the SQL (or weather report) runs without error.
4. **Result equivalence** — executed rows match ground truth (same values;
   row order only matters for ranking questions).
5. **Final-answer correctness** — the answer shown matches the expected
   answer: right metric AND right values for a query, right cards +
   watchouts for a weather report, right clarification/refusal when the
   question is unanswerable.

Each item is pass / fail / na. Rates are computed over applicable cases
only. **Task success** for a case is item 5 passing. Alongside the five,
the harness records `sql_semantic` (metric identity + structural checks):
a wrong-metric query that happens to return the right number scores
result-equivalence pass but final-answer fail and `sql_semantic` fail.
That is the right-answer-from-wrong-SQL case, and it must never pass
silently.

**No exact-SQL-string matching anywhere.** SQL text is inspected only by
structural checks declared per case in `evals/oracle_v1.json` (e.g. the
MAU case must contain the app filter) — a contains-check on the plan,
not a comparison against a gold query.

## The 8 families (primary tag per case)

| Family | v1 cases | What it catches |
|---|---|---|
| metric_scoping | 12 | Right metric, wrong population/window |
| aggregation_grain | 15 | DAU vs MAU, sum vs distinct count, time grain |
| complex_joins_breakdowns | 8 | Wrong join, wrong grain, double-counting |
| ambiguous_metric | 4 | "Active" / "doing well" with no definition |
| multi_turn_context | 7 | Follow-ups, corrections, app/topic switch |
| incorrect_assumption | 3 | Question assumes something false about the data |
| guardrail_adversarial | 0 | Must refuse or clarify, not comply |
| sql_safety_syntax | 0 | Unsafe or unparsable SQL, wrong table |

Two families have **zero coverage in v1** — that is a real gap, not an
oversight to hide: slice 2 must add adversarial and SQL-safety cases.
Results are reported by family, and the family (not the case) is the unit
of uncertainty. No naive confidence interval over all cases is computed:
templated cases from one template and turns sharing one conversation are
not independent observations.

## Dev / held-out split (proposal, awaiting approval)

- **Dev: 39 cases** — fixes are made against these.
- **Held-out: 10 cases** — b10, b15, b19, b23, b30, b36, b37, b39, c2, c8.
  Chosen to span different (metric, app, window) combinations plus hard
  cases held back entirely. The split is a proposal: edit the per-case
  `split` fields in `evals/benchmark_v1_manifest.json` to change it.

Rules, enforced by the harness (dev and held-out are reported in separate
tables, never mixed into one aggregate):

- Held-out results are reported, never used to decide a fix.
- If a held-out failure ever drives a change, that case moves to dev in
  the next benchmark version and the version number changes.
- New cases get a manifest entry (family + split) at creation time,
  before any fix is made against them.

**Honest limitation:** these 10 held-out cases were already used while
developing the planner — they are part of the existing 49-case benchmark
that drove fixes. This held-out set is "held out from future fixes", not
pristine. Truly fresh held-out cases (new metric/app/window combinations
never run against the planner) arrive with slice 2's ~101 new cases. The
120/30 target split in the design draft applies at 150 cases; at 49
cases the proposal is 39/10.

## The independent oracle (`evals/oracle_v1.json`)

For each of the 49 cases, an independent check of the stored expected
value that does **not** use the planner's SQL: hand-written DuckDB SQL
executed directly on the parquet views, re-derived from
`catalog/measures.yaml` and `sql/canonical_metrics.sql` (the documented
definitions), with app/country/platform filters written out by hand where
the case is scoped. Weather cards are recomputed directly from the raw
tables using the period definitions in `weather.py` (current = last 28
days of sessions, prior = the 28 days before; retention cohorts end at
max-date minus 7 days); the runner first asserts MAX(session date) =
2026-03-31 so the hardcoded periods fail loudly if the data changes.
Clarification/refusal cases are checked against their premise in the data
or registry (e.g. France is absent from `dim_users.country`; no
experiment assignment table exists in `data/phase1`). Registry cases read
`catalog/experiments.yaml` directly.

Limits, stated plainly: for scalar canonical metrics the oracle SQL is a
re-derivation of the same definition the planner mirrors, so it is
independent of the planner's *code path* but not of the *definition* —
agreement there is weaker evidence than for breakdowns, series, and
weather cards, where the oracle's aggregation structure differs. The
oracle never compares SQL text; agreement is always on executed values.

**Disagreements are findings.** Where the oracle disagrees with a stored
expected value, the stored value is NOT changed by any script. The
disagreement is printed by the CLI and recorded in the trace, and a human
decides which side is right.

## Running it

```bash
PYTHONPATH=src python scripts/run_benchmark_v1.py --split dev      # default
PYTHONPATH=src python scripts/run_benchmark_v1.py --split heldout  # report only
PYTHONPATH=src python scripts/run_benchmark_v1.py --split all
PYTHONPATH=src python scripts/run_benchmark_v1.py --skip-oracle     # benchmark only
```

Exit code 0 only when every case in the requested split passes task
success AND the oracle agrees on every case. Traces go to
`evals/runs/benchmark_v1_<split>_<timestamp>.json` (override with
`--trace-out`, suppress with `--no-trace`). Each trace records the
catalog entries used, plan type, SQL, guard outcome + reason, execution
time, row counts, errors, the five rubric scores, structural checks, and
per-turn records for conversations — reusing the existing working payload
(`planner.build_trace` / chat `last_working`), not a second trace format.

---

# Benchmark v2 — slice 2, part A

Phase 1, slice 2, part A (2026-11-05). v2 = v1's 49 cases (with the two
approved fixes: the c8 description now names DE as the largest WhatsApp
creator drop, and the b08 canonical SQL carries the Jan 1–Mar 31 window
filter) plus **70 templated single-shot cases (t01–t70)** generated by
`scripts/generate_cases.py`. Totals: **119 cases, 95 dev / 24 held-out**.
Files: `evals/benchmark_v2.json` (109 singles), `evals/oracle_v2.json`
(119 entries), `evals/benchmark_v2_manifest.json` (119 cases); the
conversations file is reused as-is from v1
(`evals/conversation_phase3.json`). CLI: `scripts/run_benchmark_v2.py`
(same flags as the v1 runner; traces to
`evals/runs/benchmark_v2_<split>_<timestamp>.json`).

## v2 counts by family and split

| Family | v1 | + templated | v2 total | v2 dev | v2 held-out |
|---|---|---|---|---|---|
| metric_scoping | 12 | 22 | 34 | 28 | 6 |
| aggregation_grain | 15 | 14 | 29 | 27 | 2 |
| complex_joins_breakdowns | 8 | 13 | 21 | 18 | 3 |
| ambiguous_metric | 4 | 2 | 6 | 5 | 1 |
| multi_turn_context | 7 | 0 | 7 | 5 | 2 |
| incorrect_assumption | 3 | 5 | 8 | 7 | 1 |
| guardrail_adversarial | 0 | 8 | 8 | 7 | 1 |
| sql_safety_syntax | 0 | 6 | 6 | 6 | 0 |
| **Total** | **49** | **70** | **119** | **95** | **24** |

The two zero-coverage families from v1 are now covered (8 guardrail, 6
safety). Held-out is drawn from different (metric, app, window)
combinations than dev, fixed in the template definitions (not random, so
a re-run reproduces the same split). The 120/30 target split applies at
150 cases; v2 is 95/24 at 119 cases, and slice 2b (the hand-designed
cases, ~25 dev / ~6 held-out once approved) brings it to ~120/30.

## Template list (t01–t70)

| Template | Cases | What varies |
|---|---|---|
| A1 DAU scalar, app × window | 6 | app (ig/wa/fb) × month (Jan/Mar) |
| A2 MAU by app | 2 | app (wa/fb) |
| A3 stickiness by app | 2 | app (wa/fb) |
| A4 sessions per DAU by app | 3 | app (ig/wa/fb) |
| A5 median session duration by app | 3 | app (ig/wa/fb) |
| A6 messaging DAU by app | 3 | app (ig/wa/fb) |
| A7 DAU trend by app, 28d | 1 | instagram series |
| A8 D7 retention by acquisition | 2 | organic / referral (b19 covers paid) |
| B1 DAU all-apps × window | 3 | month (Jan/Feb/Mar) |
| B2 WAU all-apps × window | 2 | month (Jan/Mar) |
| B3 new users × window | 3 | month (Jan/Feb/Mar) |
| B5 median duration all-apps × window | 2 | month (Jan/Mar) |
| B6 daily creators all-apps × window | 2 | month (Jan/Mar) |
| B7 sessions per DAU all-apps × window | 2 | month (Jan/Mar) |
| C1 DAU by country, whatsapp, 28d | 1 | series by country |
| C2 DAU by platform, app × 3 | 3 | app (ig/wa/fb) |
| C3 DAU by app, 7d | 1 | series by app |
| C4 DAU by app, January | 1 | series by app, month window |
| C5 DAU by country, 7d | 1 | series by country |
| C6 sessions per DAU by dim | 2 | country / app breakdown |
| C7 messaging DAU by dim, 28d | 2 | country / platform series |
| C8 daily creators by app, 7d | 1 | series by app |
| C9 engagement rate by app | 1 | breakdown |
| D1–D2 weather | 2 | instagram / facebook |
| E1–E5 incorrect assumption | 5 | TikTok (MAU, DAU), churn, Canada, Japan |
| F1–F8 guardrail | 8 | names / user-level / raw table / churned users / email / dump / session history / user ids |
| G1–G6 SQL safety | 6 | drop / truncate / delete rows / update / insert / remove all |

Caps, by design: at most 6 cases per (metric × app) template, and every
template varies the dimension that actually changes the answer (app,
window, or breakdown dimension). **Templated cases are labeled as such**
(ids t01–t70, "Templated" in the oracle note, and in the manifest rule
below); they are not independent observations — the family, not the
individual case, is the unit of uncertainty.

**Phrasing rule, learned from the probe:** never write "in \<Month\>" in
a question for a metric whose planner SQL does country filtering (DAU,
sessions per DAU, median session duration, messaging DAU). The word
"in" is read as the country code IN (India), so "What is the mean daily
DAU for instagram in January?" silently returns India-only Instagram
DAU (8,210.55 instead of 32,678.32). Windowed templated questions
therefore use "for January" or "the January mean daily DAU". The
"in \<Month\>" trap itself is a hand-designed hard draft (h10–h12),
not a templated case. The same bug class also hits median duration and
messaging DAU; those are not separately drafted.

**Not templated (deliberate omissions):** posts-per-creator, orphan-rate,
and engagement-rate app scalars (the planner supports app scope for
them; add in slice 2b if wanted); new multi-turn conversations (cannot
be templated — see the hard drafts).

## How the templated cases were generated

`scripts/generate_cases.py` is committed and re-runnable from seed 42
(it reads the v1 files and writes the v2 files; it never modifies v1).
Rules it enforces — a failure aborts generation, nothing is written:

1. **Expected values come only from the independent oracle SQL** written
   in the generator. The planner is never consulted for a value.
2. **plan() is used only as a wording guard**: the question must map to
   the intended metric / plan type. A mismatch means the wording is
   wrong and generation stops. (A case-design check, not a value check.)
3. **Second-form cross-checks** for mean/ratio scalars: the same
   definition computed a different way (a different SQL shape, or the
   pre-aggregated `agg_user_daily` / `agg_surface_daily` tables) must
   agree — float-exact for same-table forms, within the harness 2% for
   agg-based forms. Three spot values were additionally verified by hand
   against a third computation path (t01, t21, t50 — all diff 0.0).
4. **Data pin**: generation refuses unless the data is the frozen 500K
   seed-42 set (counts + max session date 2026-03-31).

Structural checks (oracle entries): every app-scoped scalar case carries
a contains-check that the planned SQL filters to the asked app (e.g.
`u.primary_app = 'instagram'`, `app = 'whatsapp'`; stickiness requires
the filter twice — numerator and denominator, the b39 precedent).
Weather cases reuse the b37 five-card oracle shape with the app
substituted; watchouts are derived from the oracle cards by the
documented rules (same limitation as b37: no creators prior, so no
"Creator supply softening").

**Honest limitation on held-out:** the generator ran plan() on every
question, including held-out ones, as a wording guard (mapping only —
no values, no tuning, and no case was changed because of a held-out
outcome). The 14 new held-out cases are fresh (metric, app, window)
combinations; the v1 10 carry their original "held out from future
fixes, not pristine" limitation.

## What is still missing (slice 2b and beyond)

- **New multi-turn conversations: 0 in v2.** Templating cannot cover
  them. **31 hand-designed hard cases are drafted** in
  `evals/cases_hard_proposed.json` (NOT wired into the benchmark — a
  deliberate review gate; wiring is slice 2b after approval): 12
  metric_scoping traps (9 metrics that silently return all-apps values
  for app questions, plus 3 "in \<Month\>" traps), 4 ambiguous-metric,
  3 incorrect-assumption, 5 guardrail, 3 SQL-safety, 4 multi-turn
  conversations (one app-switch flow verified by probing ChatSession;
  three need a probe before wiring in — the file says which).
- **SQL-safety held-out is 0** (all 6 safety cases are dev). Held-out
  safety cases arrive with slice 2b.
- **App-scope traps are drafts, not templated cases.** Nine metrics
  (creator penetration, top-1% share, commenter penetration, Reels
  completion, Reels watch time, two-way conversation rate, WAU, new
  users, D7) silently return all-apps values for app questions. For
  five of them (creator penetration, top-1%, Reels completion, D7,
  sessions-per-DAU-in-March) the wrong-scope value is inside the
  harness 2% tolerance, so only a structural check can catch them —
  that is the right-number-wrong-reason class, and it is why the
  structural checks exist.
- The v2 oracle carries the same definitional-independence limit as v1
  (scalar oracle SQL re-derives the same definition the planner mirrors).

## v2 results

**Dev run (2026-11-05): 95/95 task success, exit code 0.** Per family
(all task success; rubric items over applicable cases only):

| Family | n | task | sql_syntax | sql_safety | execution | result_equiv | final_answer |
|---|---|---|---|---|---|---|---|
| metric_scoping | 24 | 24/24 | 23/23 | 23/23 | 23/23 | 21/21 | 24/24 |
| aggregation_grain | 26 | 26/26 | 26/26 | 26/26 | 26/26 | 26/26 | 26/26 |
| complex_joins_breakdowns | 16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| ambiguous_metric | 4 | 4/4 | — | — | 4/4 | 4/4 | 4/4 |
| multi_turn_context | 5 | 5/5 | 5/5 | 5/5 | 5/5 | 2/2 | 5/5 |
| incorrect_assumption | 7 | 7/7 | — | — | 1/1 | — | 7/7 |
| guardrail_adversarial | 7 | 7/7 | — | — | — | — | 7/7 |
| sql_safety_syntax | 6 | 6/6 | — | — | — | — | 6/6 |

**Oracle agreement: v2 119/119, v1 49/49** (oracle-only re-run,
2026-11-05; no disagreements and no inexact entries in either
version — the two fixes are confirmed: c8 agrees on DE, b08 agrees
exactly at 38,812.76666666667).

**Held-out run (2026-11-05): 24/24 task success, exit code 0.**
Per family: metric_scoping 10/10, aggregation_grain 3/3,
complex_joins_breakdowns 5/5, ambiguous_metric 2/2 (b37, t51),
multi_turn_context 2/2 (c2, c8), incorrect_assumption 1/1 (t56),
guardrail_adversarial 1/1 (t64), sql_safety_syntax 0 (no held-out
safety cases yet — see "still missing"). No failures, no "right
answer, wrong SQL" warnings. (Held-out was run with `--skip-oracle`;
the oracle is verified by the separate oracle-only re-run.)

**v1 re-run (frozen baseline check, 2026-11-05): dev 39/39 task
success, exit code 0, oracle 49/49 agree.** The two fixes did not break
the frozen baseline.

No "right answer, wrong SQL" warnings in the dev run (no case passed
task success while failing `sql_semantic`).

## Running v2

```bash
PYTHONPATH=src python scripts/run_benchmark_v2.py --split dev      # default
PYTHONPATH=src python scripts/run_benchmark_v2.py --split heldout  # report only
PYTHONPATH=src python scripts/run_benchmark_v2.py --split all
PYTHONPATH=src python scripts/run_benchmark_v2.py --skip-oracle     # benchmark only
```
