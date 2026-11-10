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
   (Superseded 2026-11-08/09 by the 'in'-preposition pass: the
   `_in_is_preposition` guard is deleted; bare "in" is the country
   code IN only when it is literally the code, "in IN" — see the v4
   section note and `docs/ablations.md`.)
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

# Benchmark v4 — daily-series turn + three user probes (2026-11-06)

v4 = v3's 150 cases plus the **three user probes (p01–p03)** from
Siddhartha's live-shell screenshots, wired in by
`scripts/wire_daily_series_cases.py`. Totals: **153 cases, 123 dev /
30 held-out** (all three new cases in dev; held-out unchanged). Files:
`evals/benchmark_v4.json` (138 singles), `evals/conversation_v4.json`
(15 conversations), `evals/oracle_v4.json` (153 entries),
`evals/benchmark_v4_manifest.json` (153 cases). CLI:
`scripts/run_benchmark_v4.py` (same flags as the v3 runner; traces to
`evals/runs/benchmark_v4_<split>_<timestamp>.json`). The harness gains
a `v4` entry in `VERSIONS`; v1–v3 stay frozen and runnable.

## The three probes (live shell, 2026-11-06)

- **(a) "Give me Instagram MAU trend for the last 2 weeks"** — live
  answered with the March MAU scalar 210,809 + the March-only
  explanation. The planner maps "trend" to `mau_march`; MAU is a
  monthly scalar, there is no 2-week MAU trend. Current behavior is
  honest and stays as-is; p01 pins it.
- **(b) "show me the DAU for month of Mar 2026 by individual day in a
  line chart"** — live answered with the generic "could not map"
  clarification. Two gaps, both fixed here: the planner mapped "by
  individual day" to the scalar average, and the chat had no
  daily-series turn at all.
- **(c) "What's the DAU trend for Instagram?"** — live answered with
  the generic "could not map" clarification, because the live app was
  still on the pre-daily-series code (commit 3100fc6). The turn was
  already built by then: the question names the app, so as a first
  turn it must answer the daily-series turn directly — the full
  90-day window, Jan 1–Mar 31 2026, line chart. p03 pins the "trend"
  routing (series, not the scalar average) and the full-window
  series.

## The planner mapping fix (`src/socialpulse_nlq/planner.py`)

Three small, precise changes — all in the mapping layer, none in the
templates or the data:

1. **`_series_or_scalar`: "by day" / "by individual day" / "each day"
   are series triggers**, checked after the "mean"/"average" scalar
   check. Before/after, probed with `plan()`:

   | Question | Before | After |
   |---|---|---|
   | DAU for month of Mar 2026 by individual day | scalar average | **date-by-date series, March window** |
   | DAU for instagram by day in March | scalar average | **date-by-date series, March window** |
   | daily DAU for Instagram in March | series | series (unchanged) |
   | mean daily DAU for Instagram in March | scalar average | scalar average (unchanged) |
   | DAU trend for instagram over the last 28 days | series | series (unchanged) |

   No existing case regresses: every `dau_mean_daily` case in v3 uses
   "mean daily DAU" phrasing (scalar) or "trend"/"over the last N
   days" (series); none uses "by day"/"by individual day"/"each day".

2. **"in a/an ..." is not India** (at v4 time, the `_in_is_preposition`
   guard; superseded 2026-11-08/09 by the 'in'-preposition pass — bare
   "in" is the country code IN only when it is literally the code,
   "in IN"). The probe's
   "in a line chart" set country IN, so the series would have been
   India-only as well as averaged. "in a/an" can never be a country
   reading (no country name is an article). "in India" still scopes;
   "in January/March" is still a month.
3. **`_date_range`: month abbreviations Jan/Feb/Mar.** The probe's
   "month of Mar 2026" fell back to the full 90-day window, because
   only full month names matched. Abbreviations are word-boundary
   matched (`\bmar\b`), so "march" and "mark" are unaffected, and the
   guard in (2) was extended to "in Jan/Feb/Mar" so "in Feb" does not
   set country IN. No v3 question contains a bare "jan"/"feb"/"mar"
   (verified by grep), so nothing regresses.

**Fixed by the 'in'-preposition pass (2026-11-08/09), not here:**
"in the window" (b05) and "in minutes" (b07) used to read as
country IN — the same bug class, but fixing them changed the
slice-3 variants' b05 outcome under a frozen comparison, so they
waited for the planned pass, which re-ran the ablations. The pass
changed the rule itself: bare "in" is the country code IN only
when it is literally the code ("in IN"); every other "in" is a
preposition, and a named country after "in" ("in Brazil", "in the
UK") now scopes to that country instead of India. The pinning test
was renamed to `test_in_the_window_and_in_minutes_are_prepositions`
and now pins the fixed behavior (no country filter for b05/b07-style
phrasings; b05 answers 250,222 under all three variants; b07's SQL
carries no country filter). Out-of-window month abbreviations
("in Apr") are likewise unhandled. And `_date_range` still only
knows "last 7/28/90 days" — a generic "last 2 weeks" window falls
back to the full 90 days, and the turn states the window it used
rather than pretending otherwise.

## The daily-series chat turn (`src/socialpulse_nlq/chat.py`)

A standalone turn, like the MAU turn: type **`daily_series`**, no
weather report required, works as a first turn once the app is known.

- **Routing** (`respond`, after MAU, before breakdown): DAU ("dau" or
  "daily active") + a series phrase ("by day", "individual day",
  "each day", "daily", "per day", "trend", "over time"), excluding
  "mean"/"average" (the scalar), "mau"/"wau" (no daily series), and
  breakdowns ("by country/surface/platform/app/acquisition").
- **Verification before running:** the plan must be `dau_mean_daily`
  with grain exactly "user x day" and an app filter equal to the
  turn's app; otherwise it clarifies instead of presenting a scalar
  (or another metric — creators/messaging have no series branch in
  the planner) as a series.
- **App inheritance:** the turn plans the user's question, and if the
  app is not named in it (the probe names no app), it appends
  "for \<app\>" from the session — otherwise the plan would run the
  all-apps series while the reply claims one app. Caught by probing
  before the turn shipped; pinned by a test asserting "instagram" in
  the SQL.
- **Window:** stored on the session (`self.series_window`, from the
  plan's filters), so an app-switch follow-up ("what about WhatsApp?",
  "same for WhatsApp") re-runs the same series for the new app with
  the window carried over (`_series_question`, `_window_phrase`).
  Option buttons mirror the MAU turn (weather + the other two apps'
  daily DAU, with the window in the send text). Letter options work
  after the turn (the letter gate now includes `"daily_series"`).
- **Reply:** "Daily active users for \<app\> in \<window label\>, by
  day (N days). Mean …, low … (date), high … (date). The full
  day-by-day series — line chart, table, and the exact SQL — is in the
  working panel. This is the daily series, not the average: ask for
  'mean daily DAU' if you want the single average instead." The
  working payload carries the executed SQL, tables, metric definition,
  and rows, so the shell renders the line chart + table + SQL diagram
  from the same receipts as the single-shot ask UI — no frontend
  change was needed (verified by inspecting a local `/api/chat`
  response: chart kind `line`, 31 points, diagram stages sources →
  joins → filters → grouping → aggregation → output).
- **No-app behavior:** clarifies "Which app's daily DAU?", like the
  MAU turn — never silently answers one app's series.

## The three cases

- **p01 (single, metric_scoping, dev):** "Give me Instagram MAU
  trend for the last 2 weeks" — expected `mau_march` scalar, 210,809.
  Oracle reuses b38's SQL verbatim (same hand-written app-scoped MAU
  query) + its structural check (`app = 'instagram'`).
- **p02 (conversation, aggregation_grain, dev):** turn 1 is p01's
  question (MAU turn, sets the session app to Instagram); turn 2 is
  probe (b) — expected type `daily_series`, app instagram. The 2-turn
  shape is the session as asked: the probe names no app, so as a first
  turn it would correctly clarify which app; the case tests the turn
  plus app inheritance. The case carries the 31 March values
  (`expected_values`, `min_rows: 31`) and `checks:
  {"daily_series_chart_kind": "line"}`. Oracle kind `series` with the
  aggregate SQL (`agg_user_daily`, Instagram, March); the values were
  verified **two ways** — the aggregate form and the raw form
  (`fact_sessions JOIN dim_users` on `primary_app`) agree exactly
  (Mar 1: 50,601; Mar 31: 42,820; mean 45,385.32). The oracle entry
  also carries a structural `not_contains: "country = '"` — the "in a
  line chart" trap — applied to both turns' SQL by the harness.
- **p03 (single, aggregation_grain, dev):** "What's the DAU trend
  for Instagram?" — expected `dau_mean_daily` series, Instagram, the
  full 90-day window (Jan 1–Mar 31 2026), 90 daily values
  (`expected_values`, `min_rows: 90`). Family aggregation_grain, like
  p02: what is at stake is the series grain (90 daily values), not
  the metric — `dau_mean_daily` is unambiguous here; the case pins
  the "trend" routing (series, not the scalar average) and the
  full-window series. Oracle kind `series` with the aggregate SQL
  (`agg_user_daily`, Instagram, Jan 1–Mar 31); the values were
  verified **two ways** — aggregate and raw forms agree exactly
  (Jan 1: 21,299; Mar 31: 42,820; mean 40,207; low 21,299 on
  2026-01-01, high 50,601 on 2026-03-01 — the mean/low/high also
  cross-checked against the independent turn probe of the same
  question). The oracle entry carries structural checks
  (`app = 'instagram'` must be present, `country = '` must not be).
  The case carries `checks: {"daily_series_chart_kind": "line"}` for
  the record, but the harness scores singles through `plan()` only —
  `check_values` (chart kind + working-payload rows) runs for
  conversations, so the chart kind is **not** harness-evaluated for
  p03; it is verified by the fresh-session turn probe below, and the
  chart-kind mechanism itself is pinned by p02. If the chart kind
  ever needs harness enforcement for p03, the case would have to
  become a 1-turn conversation.

## Harness extension

- **`check_values` daily-series branch**
  (`scripts/run_conversation_benchmark.py`, shared by the harness via
  `_load_conv_runner`): for a reply with a `daily_series` payload, it
  reads the working payload's visualization rows and compares all
  values exactly against the case's top-level `expected_values`, and
  pins the chart kind from `checks["daily_series_chart_kind"]`. Exact
  equality is right here — both sides are counts from the same
  deterministic data, not re-derivations with rounding. Old cases are
  unaffected (no `daily_series` reply, no chart check → no-op).

## v4 counts by family and split

| Family | v3 | + probes | v4 total | v4 dev | v4 held-out |
|---|---|---|---|---|---|
| metric_scoping | 46 | 1 (p01) | 47 | 35 | 12 |
| aggregation_grain | 29 | 2 (p02, p03) | 31 | 28 | 3 |
| complex_joins_breakdowns | 21 | 0 | 21 | 16 | 5 |
| ambiguous_metric | 10 | 0 | 10 | 8 | 2 |
| multi_turn_context | 11 | 0 | 11 | 8 | 3 |
| incorrect_assumption | 11 | 0 | 11 | 9 | 2 |
| guardrail_adversarial | 13 | 0 | 13 | 11 | 2 |
| sql_safety_syntax | 9 | 0 | 9 | 8 | 1 |
| **Total** | **150** | **3** | **153** | **123** | **30** |

Held-out is unchanged (30 cases, same ids as v3). **Honest
limitation:** all three probes were run during the build (that is how the
turn was verified), so v4 held-out means "held out from fixes", not
pristine — the same caveat as v1–v3.

## v4 results

**Dev run (2026-11-06): 115/123 task success** — the 112 v3 dev passes
plus all three new probe cases. Per family:

| Family | n | task | sql_syntax | sql_safety | execution | result_equiv | final_answer |
|---|---|---|---|---|---|---|---|
| metric_scoping | 35 | 28/35 | 27/27 | 27/27 | 27/27 | 25/25 | 28/35 |
| aggregation_grain | 28 | 28/28 | 28/28 | 28/28 | 28/28 | 28/28 | 28/28 |
| complex_joins_breakdowns | 16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| ambiguous_metric | 8 | 8/8 | — | — | 4/4 | 4/4 | 8/8 |
| multi_turn_context | 8 | 8/8 | 7/7 | 7/7 | 8/8 | 2/2 | 8/8 |
| incorrect_assumption | 9 | 8/9 | — | — | 1/1 | — | 8/9 |
| guardrail_adversarial | 11 | 11/11 | — | — | — | — | 11/11 |
| sql_safety_syntax | 8 | 8/8 | — | — | — | — | 8/8 |

**Oracle agreement: v4 153/153, no inexact entries** (dev run trace;
p01 exact 210,809; p02 31-row series agrees; p03 90-row series agrees).

**Held-out run (2026-11-06, `--skip-oracle` — the oracle was already
verified by the dev run): 27/30 task success**, the same 3 failures as
v3 (h02, h08, h18 — reported only, drove no fix). Per family:
metric_scoping 10/12, aggregation_grain 3/3, complex_joins_breakdowns
5/5, ambiguous_metric 2/2, multi_turn_context 3/3,
incorrect_assumption 1/2, guardrail_adversarial 2/2,
sql_safety_syntax 1/1.

**Dev failures (8 total — the same expected findings as v3):** h01,
h03, h04, h05, h06, h07, h09 (app-scope traps) and h17 (April). No
new failures: **p01, p02, and p03 all pass all five rubric items**
(p02's `result_equiv` is scored by the new `check_values` branch —
chart kind `line` + all 31 values exact; p03's `result_equiv` is
scored by the single path — the planner series + all 90 values
exact — while its chart kind is verified by the fresh-session turn
probe below, not the harness; see the p03 note under "The three
cases"). No "right answer, wrong SQL"
warnings in either run.

**Test suite (2026-11-06, re-run 2026-11-09 after the
'in'-preposition pass): 149 pass / 1 known fail / 5 skipped**
(plain runner; the fail is the pre-existing
`test_docked_chat_only::test_rounded_corners` on an untouched file;
the 5 skips are the `test_questions` `tmp_path` fixture tests). 10 new
tests at v4 time: 4 planner (by-day series, "in a line chart" is not India,
month abbreviations, b05/b07 pin — renamed 2026-11-09 to
`test_in_the_window_and_in_minutes_are_prepositions`, now pinning
the fixed behavior) + 6 chat (MAU-trend probe,
daily-series turn + values + chart, no-app clarification, app-switch
follow-up keeps the window, mean still clarifies, option letter
advances).

**Live verification (local server, 2026-11-06):** `/api/chat` for probe
(a) returns the MAU turn (210,809, working payload with diagram, no
chart — a scalar); probe (b) in the same session returns the
daily-series turn (March, 31 days, mean 45,385, line chart with 31
points, diagram sources → joins → filters → grouping → aggregation →
output); "what about whatsapp?" re-runs the series for WhatsApp
(March, 31 days, mean 30,233); probe (b) in a fresh session clarifies
which app. Probe (c) ("What's the DAU trend for Instagram?") in a
fresh session returns the daily-series turn directly (90 days, Jan 1–
Mar 31, mean 40,207, low 21,299 on 2026-01-01, high 50,601 on
2026-03-01, line chart with 90 points) — no app inheritance needed,
the question names Instagram.

## Running v4

```bash
PYTHONPATH=src python scripts/run_benchmark_v4.py --split dev      # default
PYTHONPATH=src python scripts/run_benchmark_v4.py --split heldout  # report only
PYTHONPATH=src python scripts/run_benchmark_v4.py --split all
PYTHONPATH=src python scripts/run_benchmark_v4.py --skip-oracle     # benchmark only
```

# Benchmark v5 — MAU-to-breakdown wiring + sticky MAU context + two user probes (2026-11-09)

v5 = v4's 153 cases plus the **user probes p04 and p05** from
Siddhartha's live-shell screenshot, wired in by
`scripts/wire_mau_breakdown_cases.py`. Totals: **155 cases, 125 dev /
30 held-out** (the new cases in dev; held-out unchanged). Files:
`evals/benchmark_v5.json` (138 singles, unchanged from v4),
`evals/conversation_v5.json` (17 conversations),
`evals/oracle_v5.json` (155 entries),
`evals/benchmark_v5_manifest.json` (155 cases). CLI:
`scripts/run_benchmark_v5.py` (same flags as the v4 runner; traces to
`evals/runs/benchmark_v5_<split>_<timestamp>.json`). The harness gains
a `v5` entry in `VERSIONS`; v1–v4 stay frozen and runnable.

## The probe (live shell, 2026-11-09)

After the MAU turn ("What is the current MAU for Instagram?",
210,809), Siddhartha asked **"do you have this data by surface"**
and the chat answered with the weather-first clarification ("Ask for
a weather report first, then ask for a breakdown"). He expected the
MAU turn's context to carry into the breakdown — the data was right
there. He was right about the mechanism and wrong about one premise:
the surface data was never the blocker. The fact tables have carried
a surface on every row (reels/feed/stories) since Phase 1, with an
`agg_surface_daily` aggregate and a planner that knows surfaces; the
gap was the chat wiring. `_breakdown_turn` gates on `self.weather`,
which only `_weather_turn` sets; `_mau_turn` sets the app, the stage,
and `last_working` (the MAU SQL + result in the working panel) but
never `self.weather`. So the breakdown turn never saw the MAU result.

## The wiring (`src/socialpulse_nlq/chat.py`)

Three small, precise changes — all in the chat turn layer, none in
the planner, the templates, or the data (no generator change):

1. **`_breakdown_period()`: the breakdown's window comes from the
   weather current period, or — after a MAU turn — March 2026.**
   It returns `(start, end, label)` or `None` when neither context
   exists. Weather context is unchanged (label "current period
   {start} to {end}", byte-identical reply text). MAU context is
   March 2026 (Mar 1–31, the MAU period), label "March 2026 (the
   MAU period, Mar 1 to Mar 31)", so the reply states the topic +
   app + period plainly: "Breakdown for instagram: creators by
   surface, March 2026 (the MAU period, Mar 1 to Mar 31):".
2. **`_breakdown_rows` and `_breakdown_turn` use the helper** instead
   of gating on `self.weather` directly. The gate now accepts MAU
   context; with neither context it still clarifies "Ask for a
   weather report first". MAU only: the daily-series turn is **not**
   breakdown context (after a daily-series turn, "creators by
   surface" still clarifies), and the diagnosis / recommendation /
   brief gates still require a weather report (unchanged, verified by
   probe).
3. **The breakdown trigger no longer requires a leading space** (`"by
   country" in q or "by surface" in q`), so a bare "by surface" (no
   topic named) lands in the breakdown turn and defaults to creators
   — the same default the turn already used when the topic slot was
   empty (`topic_found or self.topic or "creators"`). "creators by
   surface", "show me this by surface", and "do you have this data by
   surface" all route the same way.

## The sticky flag (2026-11-09, chained breakdowns)

The wiring above lets *one* breakdown run off MAU context — but the
breakdown turn moves the stage off "mau", so a second breakdown in
the same session ("creators by country" after "by surface") still
asked for a weather report first. That is the next probe Siddhartha
would try, so it was authorized as an extension the same day. The
fix is a sticky flag, `self.mau_context` (app + March 2026 period),
set by the MAU turn and consulted by `_breakdown_period()` instead
of the current stage:

- **Set** by `_mau_turn` (re-set by a new MAU turn, including an
  app-switch follow-up — "what about whatsapp?" from the MAU turn
  re-sets it to WhatsApp).
- **Cleared** by a weather turn (which establishes its own context
  and wins in `_breakdown_period()` anyway) and by a daily-series
  turn (not breakdown context — after it, a breakdown asks for
  weather first again).
- **Not cleared** by a breakdown turn — that is the point. Chained
  breakdowns for the same app and period keep resolving to March
  2026. App-switching breakdowns ("creators by country for
  WhatsApp" after an Instagram MAU) are out of scope and not built:
  the breakdown routing never switches the app, so the question
  answers the session app.

Unchanged, verified by probe: after a breakdown, "what about
whatsapp?" opens a weather report for WhatsApp (the pre-existing
bare-app-name routing when the stage is not "mau" / "daily_series"),
and the weather turn then clears the flag — so that path was already
weather context, not MAU context.

Unchanged, verified by probe: "MAU for Instagram by country" still
clarifies (MAU is per-app only — held behavior, confirmed with the
user 2026-11-06); "Instagram by device" still clarifies (device is
not a breakdown dimension — `dim_devices` exists in the data but no
turn uses it; explicitly out of scope); weather → "creators by
country" still works with the same text and period; MAU → "what about
whatsapp?" still works (140,378); a breakdown with no context at all
still asks for a weather report first. The country dimension also
works off MAU context now (same gate, March 2026) — a consequence of
the shared helper, not a separate change.

## The case (p04)

- **p04 (conversation, multi_turn_context, dev):** turn 1 is the MAU
  question (expected type `mau`, app instagram); turn 2 is the probe
  — expected type `breakdown`, app instagram, topic creators. The
  2-turn shape is the session as asked: the probe names no app and no
  topic, so as a first turn it would correctly clarify which app; the
  case tests the turn plus app/topic inheritance. The case carries
  `checks["breakdown_values"]` (posts + creators per surface) and the
  six scalar values the oracle checks. Oracle kind
  `conversation_values`, exact tolerance (counts): posts via an
  IN-subquery on `dim_content`, creators via a DISTINCT-pairs CTE —
  independent formulations that agree exactly with the direct GROUP
  BY form (reels 75,450 posts / 24,388 creators; feed 58,096 /
  22,096; stories 33,509 / 17,145), and the per-surface posts sum to
  the 167,055 total. No aggregate table carries posts/creators by
  surface (`agg_surface_daily` has dau/impressions/engagements
  only), so the cross-check is two independent raw formulations plus
  the totals check, not agg-vs-raw — stated in the oracle note. The
  oracle entry also carries structural checks (`app = 'instagram'`
  must be present — it holds for both turns' SQL, the MAU SQL and
  the breakdown SQL — and `country = '` must not be).

- **p05 (conversation, multi_turn_context, dev):** the same session
  chained one turn further — turn 3 is "creators by country",
  expected type `breakdown`, app instagram, topic creators. Before
  the sticky flag, turn 3 deflected to weather-first; the flag lets
  it run: creators by country, Instagram, March 2026. The case
  carries `checks["breakdown_values"]` (the p04 surface values for
  turn 2) plus `checks["breakdown_country_values"]` (creators +
  posts per country for turn 3) and the fourteen scalar values the
  oracle checks. Oracle kind `conversation_values`, exact tolerance
  (counts): posts via an IN-subquery on `dim_content` for the app
  filter, creators via a DISTINCT-pairs CTE — independent
  formulations that agree exactly with the direct GROUP BY form
  (US 9,286 creators / 49,531 posts; IN 7,843 / 42,372; BR 4,785 /
  25,720; ID 3,149 / 16,537; MX 2,488 / 13,344; GB 1,897 / 9,884;
  DE 1,849 / 9,667), the per-country posts sum to the 167,055 total
  and the per-country creators sum to the 31,297 distinct Instagram
  March creators. No aggregate table carries posts/creators by
  country, so the cross-check is two independent raw formulations
  plus the totals checks, not agg-vs-raw — stated in the oracle
  note. The oracle entry also carries a structural check (`app =
  'instagram'` must be present in the chained breakdown SQL).

## Harness extension

- **`check_values` breakdown branch**
  (`scripts/run_conversation_benchmark.py`, shared by the harness via
  `_load_conv_runner`): for a reply with a `breakdown` payload, it
  reads the turn's rows (label/count/extra/share) and compares posts
  and creators per segment exactly against the case's
  `checks["breakdown_values"]` (the "extra" field is the creators
  count formatted as "24,388 creators"). Exact equality is right
  here — both sides are counts from the same deterministic data, not
  re-derivations with rounding. Old cases are unaffected (no
  `breakdown` reply with a `breakdown_values` check → no-op).
- **Chained-breakdown branch (p05):** for a reply with a
  `breakdown` payload whose dimension is `"country"`, it compares
  creators and posts per country exactly against the case's
  `checks["breakdown_country_values"]`. Country rows carry creators
  in "count" and posts in the "extra" field ("9,286 posts") — the
  mirror image of the surface rows, so the parse is swapped. A
  missing country-dimension reply is itself a failure (otherwise
  the values check would silently pass on nothing). Old cases are
  unaffected (no `breakdown_country_values` check → no-op).

## v5 counts by family and split

| Family | v4 | + probes | v5 total | v5 dev | v5 held-out |
|---|---|---|---|---|---|
| metric_scoping | 47 | 0 | 47 | 35 | 12 |
| aggregation_grain | 31 | 0 | 31 | 28 | 3 |
| complex_joins_breakdowns | 21 | 0 | 21 | 16 | 5 |
| ambiguous_metric | 10 | 0 | 10 | 8 | 2 |
| multi_turn_context | 11 | 2 (p04, p05) | 13 | 10 | 3 |
| incorrect_assumption | 11 | 0 | 11 | 9 | 2 |
| guardrail_adversarial | 13 | 0 | 13 | 11 | 2 |
| sql_safety_syntax | 9 | 0 | 9 | 8 | 1 |
| **Total** | **153** | **2** | **155** | **125** | **30** |

Held-out is unchanged (30 cases, same ids as v4). **Honest
limitation:** the probes were run during the build (that is how the
wiring was verified), so v5 held-out means "held out from fixes", not
pristine — the same caveat as v1–v4.

## v5 results

**Dev run (2026-11-09): 117/125 task success** — the 115 v4 dev
passes plus p04 and p05. Per family:

| Family | n | task | sql_syntax | sql_safety | execution | result_equiv | final_answer |
|---|---|---|---|---|---|---|---|
| metric_scoping | 35 | 28/35 | 27/27 | 27/27 | 27/27 | 25/25 | 28/35 |
| aggregation_grain | 28 | 28/28 | 28/28 | 28/28 | 28/28 | 28/28 | 28/28 |
| complex_joins_breakdowns | 16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| ambiguous_metric | 8 | 8/8 | — | — | 4/4 | 4/4 | 8/8 |
| multi_turn_context | 10 | 10/10 | 9/9 | 9/9 | 10/10 | 4/4 | 10/10 |
| incorrect_assumption | 9 | 8/9 | — | — | 1/1 | — | 8/9 |
| guardrail_adversarial | 11 | 11/11 | — | — | — | — | 11/11 |
| sql_safety_syntax | 8 | 8/8 | — | — | — | — | 8/8 |

**Oracle agreement: v5 155/155, no inexact entries** (dev run trace;
the held-out run also verified the oracle, 155/155 — unlike v4, it
was not run with `--skip-oracle`).

**Held-out run (2026-11-09): 27/30 task success**, the same 3
failures as v4 (h02, h08, h18 — reported only, drove no fix). Per
family: metric_scoping 10/12, aggregation_grain 3/3,
complex_joins_breakdowns 5/5, ambiguous_metric 2/2,
multi_turn_context 3/3, incorrect_assumption 1/2,
guardrail_adversarial 2/2, sql_safety_syntax 1/1.

**Dev failures (8 total — the same expected findings as v4):** h01,
h03, h04, h05, h06, h07, h09 (app-scope traps) and h17 (April). No
new failures: **p04 passes all five rubric items** (`result_equiv`
is scored by the new `check_values` breakdown branch — posts +
creators per surface exact: reels 75,450/24,388, feed 58,096/22,096,
stories 33,509/17,145), and **p05 passes all five rubric items**
(`result_equiv` is scored by the new chained-breakdown branch —
creators + posts per country exact: US 9,286/49,531, IN 7,843/42,372,
BR 4,785/25,720, ID 3,149/16,537, MX 2,488/13,344, GB 1,897/9,884,
DE 1,849/9,667). No "right answer, wrong SQL" warnings in
either run.

**v4 regression with the new code (same day, same data): dev 115/123,
held-out 27/30, oracle 153/153 agree in both runs** (traces
`evals/runs/benchmark_v4_baseline_dev_stickyflag.json` and
`evals/runs/benchmark_v4_baseline_heldout_stickyflag.json`). The
wiring and the sticky flag changed no v4 outcome: the failures are
exactly the 8 known dev findings and the 3 known held-out findings,
and p01–p03 still pass. The breakdown gate is the only behavior that
changed, and no v4 case exercises a breakdown after a MAU turn.

**Test suite (2026-11-09): 153 pass / 1 known fail / 5 skipped**
(plain runner; the fail is the pre-existing
`test_docked_chat_only::test_rounded_corners` on an untouched file;
the 5 skips are the `test_questions` `tmp_path` fixture tests). 4
MAU-breakdown tests: MAU → "do you have this data by surface"
(probe + gold values + SQL checks), named/bare phrasings + chained
country dimension off MAU context (gold rows + March SQL window) +
no-context clarification, MAU-only context (daily-series and
diagnosis/recommendation/brief after MAU still clarify), and the new
chained repeat/clearing test (repeat "by surface", weather clears to
the weather period, daily-series clears to a clarification, and the
re-set by a new MAU turn — WhatsApp surface golds verified by
independent SQL). One test-fix worth recording: the phrasings test
first chained a second breakdown ("creators by country") after a
first ("by surface") in the same session and failed — the breakdown
turn moves the stage off "mau", so the MAU context is the *current*
stage, not a sticky flag. The test was corrected to probe the country
dimension in its own session straight after the MAU turn (the
authorized design), and it pinned the second-breakdown-clarifies
behavior with a comment. **That pinned behavior is now replaced by
the sticky-flag extension below (2026-11-09):** the phrasings test
asserts the chained country breakdown with the full gold rows, and a
new test covers repeating the same breakdown, both clearing rules
(weather / daily-series), and the re-set by a new MAU turn
(app-switch follow-up).

**Live verification (local server probe, workspace ChatSession on
the 500K data):** MAU → "do you have this data by surface" returns
the breakdown turn — "Breakdown for instagram: creators by surface,
March 2026 (the MAU period, Mar 1 to Mar 31):" with rows reels
75,450 (24,388 creators), feed 58,096 (22,096), stories 33,509
(17,145); the SQL carries `c.app = 'instagram'`, no country filter,
March window. MAU → "creators by surface" and MAU → bare "by
surface" both return the breakdown; MAU → "what about whatsapp?"
still returns the MAU turn (140,378); weather → "creators by
country" still works with the same text and period; "MAU for
Instagram by country" still clarifies; "Instagram by device" still
clarifies; a breakdown with no context at all still asks for a
weather report first.

**Chained breakdowns (sticky flag, 2026-11-09):** MAU → "by surface"
→ "creators by country" returns the breakdown both times
(creators by country, Instagram, March 2026 — US 9,286 creators /
49,531 posts, IN 7,843 / 42,372, BR 4,785 / 25,720, ID 3,149 /
16,537, MX 2,488 / 13,344, GB 1,897 / 9,884, DE 1,849 / 9,667; SQL
carries the March window and `c.app = 'instagram'`). MAU → "by
surface" → "by surface" again returns the breakdown both times.
Clearing: MAU → weather → "creators by surface" runs off the
weather period (current period 2026-03-04 to 2026-03-31, no MAU
label); MAU → daily-series → "creators by surface" asks for a
weather report first. Re-set: MAU (Instagram) → "what about
whatsapp?" → "creators by surface" returns the WhatsApp breakdown
for March 2026 (stories 76,263 posts / 18,778 creators, feed 33,072
/ 13,606), and the chained country breakdown still works after the
re-set. After a breakdown, "what about whatsapp?" opens a weather
report for WhatsApp (pre-existing bare-app-name routing) and the
weather turn clears the flag.

## Running v5

```bash
PYTHONPATH=src python scripts/run_benchmark_v5.py --split dev      # default
PYTHONPATH=src python scripts/run_benchmark_v5.py --split heldout  # report only
PYTHONPATH=src python scripts/run_benchmark_v5.py --split all
PYTHONPATH=src python scripts/run_benchmark_v5.py --skip-oracle     # benchmark only
```

## The probe (live shell, 2026-11-10)

After seeing the v5 breakdowns work live (MAU → "do you have this
data by surface" → creators by surface, then "creators by country"),
Siddhartha asked for **"MAU for Instagram by country"** to work too —
and asked whether an LLM connection could parse the text and
understand it. The MAU turn still clarified ("MAU is only defined per
app, not broken down by country or surface") — a hold from Nov 5,
when the MAU turn was built. He pushed back: if he writes "MAU by
country", shouldn't the tool look at the context and translate it to
data by country? And then: can't the tool just run the query and show
the result, so he can judge right or wrong?

The answer to both is yes — but the missing piece was the capability,
not the parsing. The system already understood the question (that is
why the clarification names country specifically); there was no turn
that could answer it. An LLM layer would have hit the same wall: it
can only route a question to a capability that exists, and the
MAU-by-country turn did not exist. Silently translating "MAU by
country" into the creators-by-country breakdown — which does exist —
would have shown US 9,286 as if it were US MAU, the
right-number-wrong-reason trap the benchmark exists to catch. The
fix is the turn itself, built on the run-and-show principle he
proposed: the turn runs the query, the working panel shows the exact
SQL + result table, and he validates live. The LLM layer (chunk B)
still needs its own go-ahead; it would make the phrasing flexible,
not make the turn exist.

MAU by country is well-defined, and MAU by surface is not — that is
the whole distinction. Distinct users per country, March 2026,
app-scoped: each user has exactly one country in `dim_users`, so the
rows add up to the MAU total. Per-surface distinct users would
double-count (one user can appear on several surfaces) and exceed
the MAU total, so MAU by surface stays a clarification.

## The turn (`src/socialpulse_nlq/planner.py`, `src/socialpulse_nlq/chat.py`)

The planner carries the query, the chat carries the turn — the same
split as the MAU turn:

1. **Planner: a `mau_by_country` template.** `build_sql` gains a
   branch (after `mau_march`): `SELECT u.country, COUNT(DISTINCT
   s.user_id) AS n FROM fact_sessions s JOIN dim_users u ON
   u.user_id = s.user_id WHERE ... March 2026 ... AND s.app =
   '{app}' GROUP BY u.country ORDER BY n DESC`. The app filter is
   on `fact_sessions.app` — the same session-app definition as
   `mau_march`, so the rows add up to the MAU total. (The growth
   breakdown filters on `u.primary_app` instead; on this data the
   two formulations agree exactly for all three apps, verified
   during the build — but the turn uses `s.app` because that is the
   MAU definition.) Without an app the query is the all-apps MAU
   split by country, the same no-app convention as the scalar.
   `plan()` switches `mau_march` → `mau_by_country` when the
   question has a country breakdown (`_breakdown(q) == "country"`),
   before the variant checks. `mau_by_country` is recorded in
   `METRIC_DEFINITIONS` (display only) so the working panel and
   trace can show its definition; it is **not** added to
   `catalog/measures.yaml`, which stays at 20 metrics
   (`test_planner_metrics_are_in_catalog` asserts the count) — the
   same template-only status as the routed metrics.
2. **Planner: MAU by surface refuses.** `build_sql("mau_march",
   ..., breakdown="surface")` now raises `ValueError` ("MAU cannot
   be broken down by surface ... Ask for MAU by country instead"),
   which `plan()` turns into a clarification. The single-shot path
   now agrees with the chat path instead of silently returning the
   scalar for a breakdown question. No benchmark case asks MAU +
   surface (checked across all singles and conversations), so no
   case regresses.
3. **Chat: `_mau_by_country_turn(app)`.** It plans `f"MAU for {app}
   by country"` (same pattern as `_mau_turn`), runs it, and replies
   type `"mau_by_country"` with the per-country rows (label/count/
   share), the total, and a payload `mau_by_country={"app", "rows",
   "total"}` for the harness. The reply text states the definition
   plainly (distinct users with at least one session, per country;
   the rows add up to the MAU total; each user has exactly one
   country, so nothing is double-counted) and the working panel
   carries the exact SQL, a bar chart, the result table, and a
   diagram that shows the join and the grouping (the SQL genuinely
   does both, unlike the MAU scalar). The stage stays `"mau"` and
   the turn re-sets the sticky MAU context (`self.mau_context`,
   same app + March 2026), so a further bare "by country" repeats
   the cut and a "what about X?" follow-up still swaps the MAU app.
   Like the MAU turn it needs no weather report first.
4. **Chat routing: two places.** In the MAU intent block, `" by
   surface"` now gets the surface-only clarification (stating the
   double-counting reason, not the old "only defined per app" line)
   and `" by country"` routes to the turn, with the app from the
   question, the sticky MAU context, or the session app — else it
   clarifies which app. Before the generic breakdown routing, a bare
   `"by country"` with no topic named and `self.stage == "mau"`
   routes to the turn using the sticky context (the follow-up form);
   a named topic ("creators by country") or "by country" after a
   breakdown (stage `"breakdown"`, topic set) still takes the
   breakdown turn — the stage gate is what distinguishes them.
   "by surface" after MAU stays the creators breakdown: MAU by
   surface is undefined, so there is no MAU cut for it to mean.
5. **Variant boundary (documented, not reworked):**
   catalog-assisted and validated abstain on `mau_by_country`
   (the metric is not in the 20-metric catalog, same as the routed
   metrics — the scope check fails closed). Only the baseline
   variant answers it, and the benchmark runs the baseline. Making
   the variants catalog-aware of it is a planner-fix-stage decision,
   not part of this build.

Unchanged, verified by probe: plain "What's the MAU for Instagram?"
still returns the scalar (210,809); "MAU by surface" and "MAU by
surface for Instagram" still clarify (now with the double-counting
reason); fresh-session "MAU by country" (no app) clarifies which app;
"Instagram by device" still clarifies (out of scope); weather /
diagnosis / recommendation / brief gates untouched; the
creators-by-country breakdown is untouched — after a MAU-by-country
turn, "creators by country" still returns the creators breakdown
(US 9,286, total 31,297), and after MAU → "by country" → "by surface"
the surface breakdown still returns the p04 values.

## The cases (p06, p07)

- **p06 (single, metric_scoping, dev):** "What's the MAU for
  Instagram by country?" Expected type `mau_by_country` (the reply's
  own type — the harness's `_reply_type` returns it unchanged, so
  the case pins the turn, not the generic `breakdown` category),
  expected metric `mau_by_country`, 7 rows, US 63,372, IN 52,686,
  BR 31,623, ID 21,081, MX 16,946, DE 12,611, GB 12,490, total
  210,809. Oracle kind `breakdown`, exact tolerance (counts): the
  direct GROUP BY form (the shape the planner runs) agrees exactly
  with the independent DISTINCT-pairs CTE formulation, and the rows
  sum to the mau_march scalar. No aggregate table carries MAU by
  country (`agg_user_daily` has daily DAU only), so the cross-check
  is two independent raw formulations plus the totals check, not
  agg-vs-raw — stated in the oracle note. The oracle entry also
  carries structural checks (`app = 'instagram'` present, `GROUP BY
  u.country` present, `country = '` absent). The metric check is
  what stops a silent substitution: the creators-by-country
  breakdown would give the right shape with the wrong population.
- **p07 (conversation, multi_turn_context, dev):** turn 1 is the MAU
  question (expected type `mau`, app instagram); turn 2 is the bare
  "by country" (expected type `mau_by_country`, app instagram). The
  2-turn shape is the session as asked: the probe names no app in
  turn 2, so the case tests the turn plus the sticky MAU context.
  The case carries `checks["mau_by_country_values"]` (per-country
  MAU counts) and the seven scalar values the oracle checks. Oracle
  kind `conversation_values`, exact tolerance (counts): independent
  per-country scalar queries (DISTINCT-pairs CTE formulation) that
  agree exactly with the direct GROUP BY form, and the rows sum to
  the MAU total. The oracle entry also carries a structural check
  (`app = 'instagram'` present in the follow-up SQL).

## Harness extension

- **`check_values` mau-by-country branch**
  (`scripts/run_conversation_benchmark.py`, shared by the harness
  via `_load_conv_runner`): for a reply with a `mau_by_country`
  payload, it reads the turn's rows (label/count) and compares the
  per-country MAU counts exactly against the case's
  `checks["mau_by_country_values"]`. A missing `mau_by_country`
  reply is itself a failure (otherwise the values check would
  silently pass on nothing). Old cases are unaffected (no
  `mau_by_country_values` check → no-op).

## v6 counts by family and split

| Family | v5 | + probes | v6 total | v6 dev | v6 held-out |
|---|---|---|---|---|---|
| metric_scoping | 47 | 1 (p06) | 48 | 36 | 12 |
| aggregation_grain | 31 | 0 | 31 | 28 | 3 |
| complex_joins_breakdowns | 21 | 0 | 21 | 16 | 5 |
| ambiguous_metric | 10 | 0 | 10 | 8 | 2 |
| multi_turn_context | 13 | 1 (p07) | 14 | 11 | 3 |
| incorrect_assumption | 11 | 0 | 11 | 9 | 2 |
| guardrail_adversarial | 13 | 0 | 13 | 11 | 2 |
| sql_safety_syntax | 9 | 0 | 9 | 8 | 1 |
| **Total** | **155** | **2** | **157** | **127** | **30** |

Held-out is unchanged (30 cases, same ids as v5). **Honest
limitation:** the probes were run during the build (that is how the
turn was verified), so v6 held-out means "held out from fixes", not
pristine — the same caveat as v1–v5.

## v6 results

**Dev run (2026-11-10): 119/127 task success** — the 117 v5 dev
passes plus p06 and p07. Per family:

| Family | n | task | sql_syntax | sql_safety | execution | result_equiv | final_answer |
|---|---|---|---|---|---|---|---|
| metric_scoping | 36 | 29/36 | 28/28 | 28/28 | 28/28 | 26/26 | 29/36 |
| aggregation_grain | 28 | 28/28 | 28/28 | 28/28 | 28/28 | 28/28 | 28/28 |
| complex_joins_breakdowns | 16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| ambiguous_metric | 8 | 8/8 | — | — | 4/4 | 4/4 | 8/8 |
| multi_turn_context | 11 | 11/11 | 10/10 | 10/10 | 11/11 | 5/5 | 11/11 |
| incorrect_assumption | 9 | 8/9 | — | — | 1/1 | — | 8/9 |
| guardrail_adversarial | 11 | 11/11 | — | — | — | — | 11/11 |
| sql_safety_syntax | 8 | 8/8 | — | — | — | — | 8/8 |

**Oracle agreement: v6 157/157, no inexact entries** (dev run trace;
the held-out run also verified the oracle, 157/157).

**Held-out run (2026-11-10): 27/30 task success**, the same 3
failures as v5 (h02, h08, h18 — reported only, drove no fix). Per
family: metric_scoping 10/12, aggregation_grain 3/3,
complex_joins_breakdowns 5/5, ambiguous_metric 2/2,
multi_turn_context 3/3, incorrect_assumption 1/2,
guardrail_adversarial 2/2, sql_safety_syntax 1/1.

**Dev failures (8 total — the same expected findings as v5):** h01,
h03, h04, h05, h06, h07, h09 (app-scope traps) and h17 (April). No
new failures: **p06 passes all five rubric items** (`result_equiv`
is scored by the new `mau_by_country` branch in
`_result_equiv_single` — 7 rows, per-country counts exact: US
63,372, IN 52,686, BR 31,623, ID 21,081, MX 16,946, DE 12,611,
GB 12,490; all three structural checks pass), and **p07 passes all
five rubric items** (`result_equiv` is scored by the new
`check_values` mau-by-country branch — the turn-2 reply's rows
exact). No "right answer, wrong SQL" warnings in either run.

**v5 regression with the new code (same day, same data): dev 117/125,
held-out 27/30, oracle 155/155 agree in both runs** (traces
`evals/runs/benchmark_v5_baseline_dev_maucountry.json` and
`evals/runs/benchmark_v5_baseline_heldout_maucountry.json`). The
MAU-by-country turn, the planner surface guard, and the harness
extension changed no v5 outcome: the failures are exactly the 8
known dev findings and the 3 known held-out findings, and p01–p05
still pass. The only behavior that changed for v5 questions is the
MAU-by-surface clarification text (now states the double-counting
reason) — no v5 case asks MAU + surface, so nothing scores
differently.

**Test suite (2026-11-10): 161 pass / 1 known fail / 5 skipped**
(plain runner; the fail is the pre-existing
`test_docked_chat_only::test_rounded_corners` on an untouched file;
the 5 skips are the `test_questions` `tmp_path` fixture tests). 8
new tests: 3 planner (grouped SQL shape + WhatsApp scoping, no-app
all-apps split, surface clarifies in the planner), 4 chat
(single-shot Instagram with gold rows + bar chart + diagram stages,
WhatsApp + bare-"by country" follow-up + repeat + app-switch +
named-topic still the breakdown, no-app clarifies, surface/device
still clarify), and 1 harness (the `mau_by_country` rubric type
scores a fabricated passing outcome, and the creators-breakdown
substitution fails result equivalence — the silent-substitution
guard, pinned at the harness level). One test-fix worth recording:
the single-shot test first expected the MAU scalar's diagram stages
(sources/filters/aggregation/output) and failed — the MAU-by-country
diagram genuinely has `joins` and `grouping` stages (the SQL joins
`dim_users` and groups by country), so the test was corrected to the
real stages, not the code.

**Live verification (local server probe, workspace ChatSession on
the 500K data):** "What's the MAU for Instagram by country?"
returns the mau_by_country turn — "Monthly active users (MAU) for
instagram in March 2026, by country:" with rows US 63,372 (30.1%),
IN 52,686 (25.0%), BR 31,623 (15.0%), ID 21,081 (10.0%), MX 16,946
(8.0%), DE 12,611 (6.0%), GB 12,490 (5.9%), total 210,809; the SQL
carries `GROUP BY u.country` and `s.app = 'instagram'`, no country
filter, March window; bar chart + table + diagram in the working
panel. "MAU by country for WhatsApp" returns US 42,129, IN 35,121,
BR 20,996, ID 14,114, MX 11,170, GB 8,425, DE 8,423, total 140,378.
MAU → "by country" returns the same Instagram rows (sticky
context); repeating it works; "what about whatsapp?" then "by
country" follows the app switch (140,378); "creators by country"
afterwards still returns the creators breakdown (US 9,286).
Fresh-session "MAU by country" clarifies which app. MAU → "MAU by
surface" and "MAU by surface for Instagram" clarify with the
double-counting reason. "Instagram by device" still clarifies.

## Running v6

```bash
PYTHONPATH=src python scripts/run_benchmark_v6.py --split dev      # default
PYTHONPATH=src python scripts/run_benchmark_v6.py --split heldout  # report only
PYTHONPATH=src python scripts/run_benchmark_v6.py --split all
PYTHONPATH=src python scripts/run_benchmark_v6.py --skip-oracle     # benchmark only
```
