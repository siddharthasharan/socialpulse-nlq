# Benchmark v1 — method, split, oracle

Phase 1, slice 1. The frozen benchmark is version **v1**: 39 single-shot
cases (`evals/benchmark_phase2.json`) + 10 multi-turn conversations
(`evals/conversation_phase3.json`) = **49 cases**, data pinned to 500K
users, seed 42, window 2026-01-01..2026-03-31. The freeze lives in
`evals/benchmark_v1_manifest.json`; the harness
(`src/socialpulse_nlq/eval_harness.py`, CLI `scripts/run_benchmark_v1.py`)
refuses to run if the manifest and the eval files disagree, or if the data
pin does not match the data dir.

**v3 is now the current version** (slice 2b) — see
[Benchmark v3](#benchmark-v3--slice-2b) below. v2 stays frozen and
runnable for comparison (`scripts/run_benchmark_v2.py`); v1 stays frozen
too. Nothing in the v1/v2 files changed in slice 2b except the two
planner fixes, which the v1/v2 re-runs below confirm did not change
their results.

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
| metric_scoping | 12 | 22 | 34 | 24 | 10 |
| aggregation_grain | 15 | 14 | 29 | 26 | 3 |
| complex_joins_breakdowns | 8 | 13 | 21 | 16 | 5 |
| ambiguous_metric | 4 | 2 | 6 | 4 | 2 |
| multi_turn_context | 7 | 0 | 7 | 5 | 2 |
| incorrect_assumption | 3 | 5 | 8 | 7 | 1 |
| guardrail_adversarial | 0 | 8 | 8 | 7 | 1 |
| sql_safety_syntax | 0 | 6 | 6 | 6 | 0 |
| **Total** | **49** | **70** | **119** | **95** | **24** |

(Corrected 2026-11-05 in slice 2b: the dev/held-out columns used to read
28/6, 27/2, 18/3, 5/1, which sum to 103/16 and contradict both the
manifest and the per-family run results below. The manifest is the source
of truth; the table now matches it.)

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

  **Update 2026-11-05 (slice 2b): wired in as v3 — see
  [Benchmark v3](#benchmark-v3--slice-2b) below.** The proposal file is
  kept as the pre-approval record; it still says "unprobed /
  needs_probe_before_wiring" for h29–h31 and still expects a
  clarification for h26 — all three are superseded by the probe
  outcomes and the guard widening recorded in v3. The v3 files are
  authoritative, not the proposal file.
- **SQL-safety held-out is 0** (all 6 safety cases are dev). Held-out
  safety cases arrive with slice 2b. **Update: arrived — h26 is the
  first held-out safety case in v3 (1/1).**
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

---

# Benchmark v3 — slice 2b (Week 2)

Phase 1, slice 2b (2026-11-05). v3 = v2's 119 cases plus the **31
hand-designed hard cases (h01–h31)** wired in by
`scripts/wire_hard_cases.py`. Totals: **150 cases, 120 dev / 30 held-out**
(the 120/30 target from the design draft, met). Files:
`evals/benchmark_v3.json` (136 singles), `evals/conversation_v3.json`
(14 conversations — v1/v2 share `conversation_phase3.json`, which is
frozen, so v3 has its own copy plus h28–h31), `evals/oracle_v3.json`
(150 entries), `evals/benchmark_v3_manifest.json` (150 cases). CLI:
`scripts/run_benchmark_v3.py` (same flags as the v1/v2 runners; traces to
`evals/runs/benchmark_v3_<split>_<timestamp>.json`). The harness
(`src/socialpulse_nlq/eval_harness.py`) gains a `v3` entry in `VERSIONS`;
v1 and v2 stay frozen and runnable. `evals/cases_hard_proposed.json` is
kept as the pre-approval proposal record and is still read by nothing.

## The two authorized planner fixes (both in `src/socialpulse_nlq/planner.py`)

1. **"in → IN" tokenisation bug** (`_extract_filters` + new
   `_in_is_preposition`). The word "in" is both the country code IN and
   an English preposition; "in January/March" was read as country IN.
   Now "in \<Month\>" (all twelve months) and "in the last/past ..."
   do not set a country filter, while "in \<Country\>" still scopes.
   Before/after, probed with `plan()` + execution on the 500K data:

   | Question | Before | After |
   |---|---|---|
   | mean daily DAU for Instagram in January | 8,210.55 (India-only) | **32,678.32**, no country filter |
   | mean daily DAU in March | 25,227.55 (India-only) | **100,943.35**, no country filter |
   | sessions per DAU in March | 1.69334 (India-only) | **1.6939**, no country filter |
   | mean daily DAU in India | 22,295.07 (IN) | 22,295.07 (IN, unchanged) |
   | DAU in India in January | — | IN + January window (18,074.68) |
   | mean daily DAU for Instagram in February | India-only | 42,809.75, no country filter |

   `chat.py` never had this bug: both of its country-extraction sites
   already use full country names only (`len(name) > 2`). After the fix
   h10–h12 pass as regression cases (h11 in dev, h10/h12 in dev — the
   fix was verified against h11's question, so h11 is dev, not held-out).
   The "drop in DAU" question still returns a query, not a refusal.
2. **Unsafe-word guard widened** (`plan()`): the list now also catches
   "drop database" and "drop the database". Before, "Drop the database"
   clarified only because no metric mapped (safe by accident); now it
   refuses by the guard. h25/h27 still refuse; a bare "drop" is
   deliberately NOT listed ("the drop in DAU" is a legitimate question).

## Review decisions applied (review sheet, approved 2026-11-05)

- Group 1 as-is: h01–h05, h07–h09, h10–h12, h13–h16, h17–h19, h20–h24,
  h25, h27, h28, h29.
- **A (guardrail wording): A1** — keep the generic "could not map"
  clarification. h20–h24 pin that text; A2 (a distinct refusal) is
  deferred to the LLM layer.
- **B (h06): approve as-is** — expected behavior is a clarification, no
  definition decision needed. The app-scoped two-way-rate definition
  stays deferred; the oracle entry records the draft's candidate
  definition (≥1 Facebook-primary sender; ≥2 distinct senders counting
  only Facebook senders → 11.15%) as the premise, with the warning that
  "all senders Facebook-primary" would give a different number.
- **C (h30): reworded to clarification** — "actually the last 7 days"
  after a ranking clarifies that the ranking window cannot be changed.
  Turn 4 expects type `clarification` and no topic. No 7-day oracle
  value.
- **D (h31): reworded to bare "same for WhatsApp"** — the drafted
  phrasing clarifies and stays on Instagram; the bare phrasing switches
  the drill-down to WhatsApp (US 6,089 creators, 30.1%). Turn 3 expects
  type `drilldown` (the reply type for a same-analysis-other-app turn),
  topic creators, app whatsapp.
- **h26 deviation from the draft:** expected type is `refusal`, not the
  draft's clarification — the guard widening makes it refuse by design.
- **h29 turn 4:** expect_topic `creators` (probe-backed: the Facebook
  diagnosis is Facebook's own, creators first).
- h01–h09 and h17–h18 carry **no `expected_contains`**: the check is
  "clarifies instead of answering with a number", and the future fix's
  wording is not yet defined. h13–h16 and h20–h24 pin the generic
  clarification text; h19 pins "cannot evaluate that experiment";
  h25–h27 pin the refusal text.

## v3 counts by family and split

| Family | v2 | + hard | v3 total | v3 dev | v3 held-out |
|---|---|---|---|---|---|
| metric_scoping | 34 | 12 | 46 | 34 | 12 |
| aggregation_grain | 29 | 0 | 29 | 26 | 3 |
| complex_joins_breakdowns | 21 | 0 | 21 | 16 | 5 |
| ambiguous_metric | 6 | 4 | 10 | 8 | 2 |
| multi_turn_context | 7 | 4 | 11 | 8 | 3 |
| incorrect_assumption | 8 | 3 | 11 | 9 | 2 |
| guardrail_adversarial | 8 | 5 | 13 | 11 | 2 |
| sql_safety_syntax | 6 | 3 | 9 | 8 | 1 |
| **Total** | **119** | **31** | **150** | **120** | **30** |

Held-out hard cases (6): **h02, h08, h18, h21, h26, h30** — one or two
per family, including the first held-out SQL-safety case (h26) and a
multi-turn held-out case (h30). **Honest limitation:** all 31 hard cases
were probed during drafting (that is how `planner_currently` was
determined), and h11 was probed again as the verification check for the
"in → IN" fix, so v3 held-out means "held out from fixes", not pristine
— the same caveat as v1/v2. Truly fresh held-out cases (designed without
probing) have not arrived yet.

## Harness extensions (needed to wire these cases correctly)

- **Structural `not_contains`** (`_structural_checks`): a spec
  `{"not_contains": s}` passes only if the SQL does not contain `s`.
  h10–h12 carry `not_contains: "country = '"` — h12's wrong-scope value
  is 0.03% from the right one, so only "no country filter unless asked"
  catches it.
- **Oracle `evidence_expect_value`** (behavioral kind): the premise can
  be a value, not just an absence — the true app-scoped number a hard
  case rests on (h01–h09, compared with the 2% tolerance), "zero April
  sessions" (h17/h18), the Facebook daily-creators value the h29
  diagnosis reads (2,592.6, the stored t51 value). The value is the
  premise, not an expected answer: these cases expect a clarification,
  so no answer is stored. (Fixed during wiring: DuckDB returns
  `Decimal` for literal decimals, so the comparison coerces through
  `float()` with an equality fallback.)

## v3 results

**Dev run (2026-11-05): 112/120 task success.** Per family:

| Family | n | task | sql_syntax | sql_safety | execution | result_equiv | final_answer |
|---|---|---|---|---|---|---|---|
| metric_scoping | 34 | 27/34 | 26/26 | 26/26 | 26/26 | 24/24 | 27/34 |
| aggregation_grain | 26 | 26/26 | 26/26 | 26/26 | 26/26 | 26/26 | 26/26 |
| complex_joins_breakdowns | 16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| ambiguous_metric | 8 | 8/8 | — | — | 4/4 | 4/4 | 8/8 |
| multi_turn_context | 8 | 8/8 | 7/7 | 7/7 | 8/8 | 2/2 | 8/8 |
| incorrect_assumption | 9 | 8/9 | — | — | 1/1 | — | 8/9 |
| guardrail_adversarial | 11 | 11/11 | — | — | — | — | 11/11 |
| sql_safety_syntax | 8 | 8/8 | — | — | — | — | 8/8 |

**Oracle agreement: v3 150/150, no inexact entries** (checked in the dev
trace; the oracle runs on every run, all 150 entries).

**Held-out run (2026-11-05, `--skip-oracle` — the oracle was already
verified by the dev run): 27/30 task success.** Per family:
metric_scoping 10/12, aggregation_grain 3/3, complex_joins_breakdowns
5/5, ambiguous_metric 2/2, multi_turn_context 3/3,
incorrect_assumption 1/2, guardrail_adversarial 2/2,
sql_safety_syntax 1/1.

**Failures (11 total — all expected findings, reported, not fixed):**

| Case | Family | Split | What the planner does instead |
|---|---|---|---|
| h01 creator penetration ig | metric_scoping | dev | returns all-apps 0.20167 (scoped 0.20172, 0.02% apart) |
| h02 top-1% share wa | metric_scoping | held-out | returns all-apps 0.09644 (scoped 0.09764, 1.2% apart) |
| h03 commenter penetration ig | metric_scoping | dev | returns all-apps 0.09151 (scoped 0.09395, 2.7% apart) |
| h04 Reels completion fb | metric_scoping | dev | returns all-apps 0.35049 (scoped 0.35095, 0.13% apart) |
| h05 Reels watch time ig | metric_scoping | dev | returns all-apps 0.90523 (scoped 0.73088, 19% apart) |
| h06 two-way conversation fb | metric_scoping | dev | returns all-apps 0.66764 (candidate scoped 0.11155) |
| h07 WAU wa | metric_scoping | dev | returns all-apps 267,349 (scoped 79,951, 3.3x) |
| h08 new users ig | metric_scoping | held-out | returns all-apps 250,222 (scoped 112,209, 2.2x) |
| h09 D7 retention wa | metric_scoping | dev | returns all-apps 0.22216 (scoped 0.22282, 0.3% apart) |
| h17 DAU in April 2026 | incorrect_assumption | dev | returns 90-day DAU as if it were April DAU |
| h18 WAU in April | incorrect_assumption | held-out | returns full-window WAU as if it were April WAU |

Fixing h01–h09 means app-scoping nine metrics (several need new joins:
Reels metrics need `dim_content`; WAU needs a user join; D7 needs a
cohort app filter) plus deciding the h06 definition — a larger planner
change, out of scope for slice 2b and deliberately NOT attempted here.
Fixing h17–h18 means an out-of-window check in the window parser. Both
are slice 3 / later work; the held-out failures (h02, h08, h18) are
reported only and drove no fix, per the split rules.

No "right answer, wrong SQL" warnings in either run. h10–h12 pass with
their structural checks (no country filter found). h26 passes as a
refusal by the widened guard; h30 passes as a clarification; h31 passes
as a WhatsApp drill-down.

**Frozen-baseline re-runs after the two planner fixes (2026-11-05,
`--skip-oracle`, `--no-trace`): v2 dev 95/95, v1 dev 39/39** — the fixes
changed no v1/v2 outcome (no v1/v2 question uses "in \<Month\>" or
"drop the database" phrasing; verified by grep before the runs).

**Test suite (2026-11-05): 127 pass / 1 known fail / 5 skipped** (plain
runner; the fail is the pre-existing `test_docked_chat_only::
test_rounded_corners` on an untouched file; the 5 skips are the
`test_questions` `tmp_path` fixture tests). Harness tests 22/22 (17
carried over + 5 new: `not_contains`, behavioral `evidence_expect_value`,
v3 split integrity, v3 oracle coverage + review-decision pins, v3 family
counts).

## What is still missing (slice 3 and beyond)

- **The 11 failures above** — app-scoping nine metrics + the April
  window check. That is the next planner work, not a benchmark change.
- **Held-out is still "held out from fixes", not pristine** (see above).
  Truly fresh held-out cases need to be designed without probing the
  planner first.
- **h06's app-scoped definition is still undecided** (Decision B
  deferred it). Any future planner fix for two-way conversation rate
  must settle it first.
- Slice 3 (planner variants / ablations) — **done, see
  `docs/ablations.md`**: baseline vs catalog-assisted vs validated on
  the frozen v3 benchmark, paired family-aware comparison. The
  technical report (Week 4) and the evals dashboard are still ahead;
  neither is started here.

## Running v3

```bash
PYTHONPATH=src python scripts/run_benchmark_v3.py --split dev      # default
PYTHONPATH=src python scripts/run_benchmark_v3.py --split heldout  # report only
PYTHONPATH=src python scripts/run_benchmark_v3.py --split all
PYTHONPATH=src python scripts/run_benchmark_v3.py --skip-oracle     # benchmark only
```
