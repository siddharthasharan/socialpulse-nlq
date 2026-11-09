# Slice 3 — controlled planner experiments (Week 3 / ChatGPT W3)

Phase 1, slice 3 (2026-11-05). Three deterministic planner variants
tested against the **frozen v3 benchmark** (150 cases, 120 dev / 30
held-out, oracle 150/150 agree, data pin 500K / seed 42 /
2026-01-01..2026-03-31). The variants are **tested against** the
benchmark, never tuned to it: no variant changes because a held-out
case failed, no benchmark/oracle/manifest file is edited in this slice.

## The three variants (feature flags, not three planners)

All three are the same planner with a `variant` flag on `plan()`
(`plan(question, variant="baseline")`; default stays the current
planner, so every existing caller is the baseline). The flag threads
through the harness (`run_benchmark(..., variant=...)`,
`--variant` on `scripts/run_benchmark_v3.py`) and through
`ChatSession(variant=...)` for the conversation cases (the only
`plan()` call inside chat is the MAU turn). The trace records the
variant. Baseline re-runs at the end must reproduce the slice-2b
numbers exactly (dev 112/120, held-out 27/30) — that is the check
that the flags did not change default behavior.

### 1. Baseline — the current planner exactly as-is

`plan()` with no variant behavior. Known result (slice 2b): dev
112/120, held-out 27/30. The 11 failures are the expected findings:
h01–h09 (planner answers all-apps values where a clarification is
expected) and h17–h18 (planner answers in-window DAU/WAU for April
questions). Baseline is the control; the other two variants are
measured against it.

### 2. Catalog-assisted — the planner consults the semantic catalog before planning

**What "consults" means, precisely.** After metric detection and
filter extraction, before `build_sql`, the variant reads
`catalog/measures.yaml` at runtime (regex parse — no PyYAML in the
venv, same style as the existing `catalog_metric_ids`) and applies
three checks, in order:

1. **Metric exists in the catalog.** The detected query metric must
   be one of the 20 ids in `measures.yaml`; otherwise clarify
   ("no catalog definition"). This is a guard against future drift,
   not a live check on v3 — every metric the baseline detects today
   is in the catalog.
2. **Requested scope is supported by the metric.** Each filter
   dimension the question requests must be in the metric's catalog
   `scope` list; otherwise clarify, naming the metric and the
   dimension ("creator penetration is not defined per app in the
   catalog").
3. **Experiment answers match the registry.** For `experiment_info`
   plans, the metric must equal the `primary_metric` of the named
   experiment in `catalog/experiments.yaml`; otherwise clarify.
   (The registry metric `new_creator_d7_retention` is deliberately
   NOT in `measures.yaml` — it is a registry metric, so check 1
   does not apply to experiment plans; check 3 is the one that
   covers them.)

**The `scope` field is new in this slice** (`catalog/measures.yaml`).
It lists the filter dimensions the Phase 2 template for that metric
can actually honor (app / country / platform / surface /
acquisition). Derivation rule, stated before any variant ran: the
scope is a transcription of `build_sql` in
`src/socialpulse_nlq/planner.py` — a dimension is listed exactly
when the template's SQL applies that filter. It is **not** derived
from the benchmark cases, and it is **not** a claim about what the
metric could support in principle (WAU *could* be app-scoped with a
join; the template does not, so the scope says so). A test
(`test_catalog_scope_matches_templates`) verifies the transcription
mechanically: for each metric it calls `build_sql` with each filter
and checks the SQL contains the filter condition. If template and
catalog disagree, the test fails — the catalog cannot quietly drift
from the code.

**"Requested filters" — the metric-name conflation.** The requested
filters are `_extract_filters` output with one correction: a surface
filter is dropped when the surface word is part of the detected
metric's own name (e.g. `reels` in `reels_completion_rate`). Without
this, "What is the Reels completion rate?" would count as requesting
a Reels-surface scope on a metric whose name *is* Reels, and the
variant would clarify on a plain, answerable question. The rule is
mechanical (metric id contains the surface word), stated here before
running, and applies identically to both non-baseline variants.

**What catalog-assisted does NOT do.** The data window is a dataset
property, not a catalog dimension — no `scope` entry covers it, and
the variant performs no window check. It is **predicted not to fix
h17/h18**. That is a designed limitation, not an oversight: the
catalog answers "is this metric defined for this scope?", not "does
this window exist in the data?".

**Hypothesis H1.** Catalog assistance reduces wrong-metric /
wrong-scope answers: the pre-plan scope check turns the nine silent
all-apps answers (h01–h09) into clarifications. Predicted: dev
119/120 (h17 remains), held-out 29/30 (h18 remains) — *if* the
variant pays no coverage cost. Whether it pays one (clarifying on
cases the baseline answers correctly) is an open question the run
answers; the likely candidates are cases where filter extraction
over-reads (an app word in a non-scope context) or where a metric's
template scope is narrower than the question assumes.

### 3. Validated — baseline plus a validation / abstention layer

**What it does, precisely.** After the baseline plan is built (and
passes the existing guard), the planned SQL is checked against the
catalog definitions and data constraints **before answering**. The
check applies only to `type == "query"`, non-routed plans. Routed
plans pass through: aggregate re-routing is itself the safety
mechanism, and it is labeled as such in the trace — re-validating
it would second-guess a deliberate, visible decision. Clarification,
refusal, weather-report, and experiment plans pass through
unchanged; validation is about query plans, and there is no SQL to
validate in the others. The validation rules, in order:

1. **Metric in the catalog** — same check as catalog-assisted rule 1.
2. **Every requested filter is present in the SQL.** For each
   requested filter dimension (same `_extract_filters` output and
   same metric-name surface exemption as catalog-assisted), the
   planned SQL must contain the corresponding condition
   (`primary_app = 'instagram'` / `app = 'instagram'` /
   `c.app = 'instagram'` for app, `country = 'US'` for country,
   `platform = 'android'` for platform, `surface = 'reels'` for
   surface, `acquisition_source = 'organic'` for acquisition).
   A requested filter the SQL silently drops is the exact failure
   mode of h01–h09, so this rule is the post-plan version of the
   same check catalog-assisted does pre-plan. The date range is
   **not** text-checked here (every template emits *some* date
   range, so a contains-check would always pass); the window is
   covered by rule 3 instead.
3. **Requested window inside the data window.** If the question
   names a month outside Jan–Mar 2026 (April–December), clarify:
   the data ends 2026-03-31, so there is no April DAU/WAU to
   report. In-window months (January–March) never trigger it. This
   is the rule that addresses h17/h18.
4. **SQL guard passes** — re-checked, not assumed (in practice it
   always passes for baseline query plans, since `plan()` already
   guards; the re-check is there so validation never depends on an
   earlier step's say-so).

On any failure the variant returns a clarification plan (`type`
`clarification`, `sql` None, metric kept in the plan dict for the
trace) whose text names the failed check. The benchmark cases
h01–h09 and h17–h18 carry no `expected_contains`, so the wording
is free; the check identity is what matters, and it is recorded in
the plan's trace.

**Hypothesis H3.** Validation / abstention improves trustworthiness
at the cost of coverage: the post-plan check abstains where the
baseline answers wrongly (h01–h09 via rule 2, h17–h18 via rule 3)
but may also abstain where the baseline answers rightly. Predicted:
dev 120/120 and held-out 30/30 *if* the coverage cost is zero; the
run measures the cost. Likely cost candidates: questions where
filter extraction over-reads a filter the SQL legitimately does not
need, and any metric whose template ignores a filter the question
names for a legitimate reason. A large cost would mean naive
post-plan filter-presence validation is too blunt a tool — that
would be a finding, not a failure to report.

## Hypotheses out of this slice's reach (labeled, not tested)

- **H2 (LLM better coverage on novel language at the cost of
  semantic errors)** needs an LLM planner. There is no LLM planner
  in this slice; H2 is chunk B.
- **H4 (hybrid routing gives the better quality–cost trade-off)**
  needs the router. There is no router in this slice; H4 is chunk B.

Slice 3 tests H1 and H3 only. It does not pretend otherwise.

## How the comparison is reported

- **Per-family task success** for each variant, dev and held-out
  reported separately, never blended.
- **Accuracy / coverage / abstention, separately** (never collapsed
  into one weighted score), computed on **single-shot cases only**
  (136): conversations have clarification turns by design (h30
  expects one), so a case-level abstention rate over conversations
  would be meaningless. Definitions:
  - *coverage* — fraction of singles where the system attempted an
    answer (plan type query / weather_report / experiment_list /
    experiment_info).
  - *abstention rate* — 1 − coverage.
  - *accuracy* — task success among attempted singles (when it
    answers, how often is it right?).
  - *appropriate abstention* — abstentions on cases that expect
    clarification/refusal, as a share of all abstentions.
  - *wrong abstention (coverage cost)* — abstentions on cases that
    expect an answer.
  - *missed abstention* — cases that expect clarification/refusal
    but were answered anyway (the h01–h09 class under baseline).
- **Paired, family-aware comparison.** For each pair of variants,
  the cases that flip (pass→fail, fail→pass) are listed by id and
  family. No naive confidence interval is computed over all cases
  as if independent: templated cases from one template and turns
  sharing one conversation are not independent observations. The
  family is the unit of uncertainty; counts and flip lists are the
  evidence.
- **Before/after vs baseline** on the frozen set, plus the held-out
  confirmation (held-out run once at the end, never used to decide
  a fix).
- **Failure examples** (10–15, with full traces): cases where the
  variants disagree, plus right-number-wrong-reason cases — the
  h01/h02/h04/h09 class, where the wrong-scope value sits inside
  the 2% tolerance and only the scope (or a clarification) is
  wrong. At least one hypothesis that did NOT hold is reported
  honestly if the results show one.

## Results

Runs (2026-11-05/06, 500K seed-42 data, oracle 150/150 agree with no
inexact entries in **all six runs** — the oracle is variant-independent,
as it should be):

| Trace | Result |
|---|---|
| `evals/runs/benchmark_v3_baseline_dev_slice3.json` | dev 112/120 |
| `evals/runs/benchmark_v3_baseline_heldout_slice3.json` | held-out 27/30 |
| `evals/runs/benchmark_v3_catalog-assisted_dev_slice3.json` | dev 118/120 |
| `evals/runs/benchmark_v3_catalog-assisted_heldout_slice3.json` | held-out 29/30 |
| `evals/runs/benchmark_v3_validated_dev_slice3.json` | dev 119/120 |
| `evals/runs/benchmark_v3_validated_heldout_slice3.json` | held-out 30/30 |

Machine-readable comparison:
`evals/runs/variant_comparison_slice3.json`
(`scripts/compare_variants.py`, run over the six traces above).
The baseline re-run reproduces the slice-2b numbers exactly
(112/120, 27/30) — the flags did not change default behavior.

### Re-run after the 'in'-preposition pass (2026-11-08/09)

The slice-3 numbers above carry a known one-case cost: b05 (§Failure
example 12). The 'in'-preposition pass fixed the filter-extraction
bug behind it (bare "in" is the country code IN only when it is
literally the code, "in IN"; every other "in" is a preposition —
see `src/socialpulse_nlq/planner.py`, `_extract_filters`) and
re-ran baseline + all three variants on the same frozen v3 set,
plus the variants on v4+p03. No benchmark, oracle, or manifest file
was edited; the only code change is the extractor, and the only test
change is the renamed pin
(`test_in_the_window_and_in_minutes_are_prepositions`). New traces
are the `*_inpass.json` files in `evals/runs/`; the comparison JSON
above was regenerated from them. Results:

| Trace | Result |
|---|---|
| `evals/runs/benchmark_v3_baseline_dev_inpass.json` | dev 112/120 |
| `evals/runs/benchmark_v3_baseline_heldout_inpass.json` | held-out 27/30 |
| `evals/runs/benchmark_v3_catalog-assisted_dev_inpass.json` | dev **119/120** |
| `evals/runs/benchmark_v3_catalog-assisted_heldout_inpass.json` | held-out 29/30 |
| `evals/runs/benchmark_v3_validated_dev_inpass.json` | dev **120/120** |
| `evals/runs/benchmark_v3_validated_heldout_inpass.json` | held-out 30/30 |

Oracle still 150/150 agree, 0 inexact, in all three dev runs. The
baseline is unchanged (112/120, 27/30) — the fix changed no
baseline outcome, only the variants' reading of b05. Catalog-assisted
gains exactly one case (b05); validated gains exactly one case
(b05). **No case newly fails under any variant on either split.**
The v4+p03 variant runs (first variant runs on v4 — see "Variant
runs on v4+p03" below) agree:
baseline 115/123 / 27/30, catalog-assisted 122/123 / 29/30,
validated 123/123 / 30/30, oracle 153/153 in all three dev runs.
Everything below in this section — family tables, rates, flips,
verdicts, examples 12–13 — is updated to the re-run numbers; the
slice-3 tables above are kept so the one-case move is visible.

### Task success by family

Dev (120 cases) — re-run numbers (slice-3 numbers in parentheses
where they differ):

| Family | n | Baseline | Catalog-assisted | Validated |
|---|---|---|---|---|
| metric_scoping | 34 | 27/34 | 34/34 (was 33/34) | 34/34 (was 33/34) |
| aggregation_grain | 26 | 26/26 | 26/26 | 26/26 |
| complex_joins_breakdowns | 16 | 16/16 | 16/16 | 16/16 |
| ambiguous_metric | 8 | 8/8 | 8/8 | 8/8 |
| multi_turn_context | 8 | 8/8 | 8/8 | 8/8 |
| incorrect_assumption | 9 | 8/9 | 8/9 | 9/9 |
| guardrail_adversarial | 11 | 11/11 | 11/11 | 11/11 |
| sql_safety_syntax | 8 | 8/8 | 8/8 | 8/8 |
| **Overall** | **120** | **112** | **119 (was 118)** | **120 (was 119)** |

Held-out (30 cases, run once at the end, never used to decide a fix):

| Family | n | Baseline | Catalog-assisted | Validated |
|---|---|---|---|---|
| metric_scoping | 12 | 10/12 | 12/12 | 12/12 |
| aggregation_grain | 3 | 3/3 | 3/3 | 3/3 |
| complex_joins_breakdowns | 5 | 5/5 | 5/5 | 5/5 |
| ambiguous_metric | 2 | 2/2 | 2/2 | 2/2 |
| multi_turn_context | 3 | 3/3 | 3/3 | 3/3 |
| incorrect_assumption | 2 | 1/2 | 1/2 | 2/2 |
| guardrail_adversarial | 2 | 2/2 | 2/2 | 2/2 |
| sql_safety_syntax | 1 | 1/1 | 1/1 | 1/1 |
| **Overall** | **30** | **27** | **29** | **30** |

No variant changed any family other than metric_scoping and
incorrect_assumption. Every other family's cases pass under all three
variants, in both splits.

### Accuracy / coverage / abstention — singles only, reported separately

Singles: dev n=109, held-out n=27. (Conversations excluded, as designed:
h30 expects a clarification turn, so a case-level abstention rate over
conversations would be meaningless.)

Dev (re-run; slice-3 values in parentheses where they differ):

| Variant | Coverage (attempted) | Abstention | Accuracy among attempted | Appropriate abst. | Wrong abst. (cost) | Missed abst. |
|---|---|---|---|---|---|---|
| Baseline | 79/109 (72.5%) | 27.5% | 89.9% (71/79) | 30 | 0 | 8 |
| Catalog-assisted | 72/109 (66.1%) (was 71/109, 65.1%) | 33.9% | 98.6% (71/72) | 37 | 0 (was 1, b05) | 1 (h17) |
| Validated | 71/109 (65.1%) (was 70/109, 64.2%) | 34.9% | 100.0% (71/71) | 38 | 0 (was 1, b05) | 0 |

Held-out (unchanged by the re-run — b05 is a dev case):

| Variant | Coverage (attempted) | Abstention | Accuracy among attempted | Appropriate abst. | Wrong abst. (cost) | Missed abst. |
|---|---|---|---|---|---|---|
| Baseline | 23/27 (85.2%) | 14.8% | 87.0% (20/23) | 4 | 0 | 3 |
| Catalog-assisted | 21/27 (77.8%) | 22.2% | 95.2% (20/21) | 6 | 0 | 1 (h18) |
| Validated | 20/27 (74.1%) | 25.9% | 100.0% (20/20) | 7 | 0 | 0 |

Read plainly: the baseline answers more and is wrong more often
(8 missed abstentions on dev, 3 on held-out). Both variants trade
~7–11 points of coverage for accuracy: validated answers nothing
wrongly on either split (71/71 and 20/20 among attempted — small n,
do not overclaim beyond this benchmark), and after the
'in'-preposition pass its coverage cost is **zero on both splits**
— the one dev case it used to pay, b05, is gone (example 12).
Catalog-assisted pays no coverage cost either; its only remaining
miss is the April question h17, by design. Note the denominators:
validated's 100% is 71/71 dev + 20/20 held-out — strong on this
benchmark at small n. It says "on these 150 cases the validator
never let a wrong answer through"; it does not say "the validator
never lets a wrong answer through."

### Paired flips (which cases move, by family — no naive CIs)

Dev, baseline → catalog-assisted: **7 fixed, 0 broken** (re-run;
slice 3 was 7 fixed, 1 broken — b05 no longer breaks).
Fixed: h01, h03, h04, h05, h06, h07, h09 (all metric_scoping).
Broken: none.

Dev, baseline → validated: **8 fixed, 0 broken** (re-run; slice 3
was 8 fixed, 1 broken — b05 no longer breaks).
Fixed: h01, h03, h04, h05, h06, h07, h09, h17.
Broken: none.

Dev, catalog-assisted → validated: **1 fixed, 0 broken.** Fixed: h17.
(The only difference between the two variants on dev is the April
question — exactly the designed boundary between them.)

Held-out, baseline → catalog-assisted: **2 fixed, 0 broken**
(h02, h08). Baseline → validated: **3 fixed, 0 broken**
(h02, h08, h18). Catalog-assisted → validated: **1 fixed, 0 broken**
(h18). No variant broke any held-out case.

### Hypothesis verdicts

- **H1 (catalog assistance reduces wrong-scope answers): HELD —
  including the exact prediction.** It fixed all nine app-scope
  traps across both splits (7 dev + h02, h08 held-out) and broke
  nothing on held-out. In slice 3 the pre-registered dev prediction
  was 119/120 and the actual was 118/120 — the prediction did not
  anticipate the b05 coverage cost (see example 12). After the
  'in'-preposition pass removed that cost, the re-run is exactly
  119/120: the mechanism was right, and the score now matches the
  prediction. The one-case miss is still recorded here as part of
  the history, because the fix — not a re-reading of the
  prediction — is what closed it.
- **H3 (validation / abstention improves trustworthiness at a
  coverage cost): HELD — including the exact prediction.** It fixed
  all eleven known findings across both splits (8 dev + 3 held-out),
  missed zero abstentions on either split, and was 100% accurate
  among attempted answers on both splits. The pre-registered dev
  prediction was 120/120 *if* the coverage cost were zero; in slice
  3 the cost was one case (b05), so 119/120. After the
  'in'-preposition pass the cost is zero, and the re-run is exactly
  120/120. The cost was real, small, diagnosed in §7.12 — one named
  case with one named cause, not a vague "trade-off" — and it is
  now gone because the cause was fixed in the extractor, not
  because validation got more lenient.
- **H2 and H4: NOT TESTED.** No LLM planner and no router exist in
  this slice, as labeled in the design. Nothing here speaks to them.

### Failure examples (13, with full traces)

A note on where the numbers come from: for cases that expect a
clarification, the harness scores the plan type only — it never
executes the planner's SQL (there is nothing to execute once the
system clarifies). So the baseline *returned values* below do not
come from the harness trace; they come from direct probing
(`plan()` + `run_plan()` on the 500K data, re-run 2026-11-06, and
they match the values recorded in `evals/cases_hard_proposed.json`
and the oracle notes exactly). The SQL shown is the SQL in the
baseline trace; the full SQL, rubric items, and oracle entries are
in the trace files listed above and in `evals/oracle_v3.json`.

**1. h01 — creator penetration for Instagram (metric_scoping, dev).**
The right-number-wrong-reason exemplar. Baseline SQL has no app
filter (`SELECT AVG(creators * 1.0 / NULLIF(dau, 0)) ...` over all
apps) and returns **0.20167**; the true Instagram-scoped value is
**0.20172** — 0.02% apart, inside any tolerance, so no value check
could ever catch it. Only the scope is wrong. Both variants
clarify (catalog: "creator_penetration is not defined per app in
the catalog"; validated: "the planned SQL does not apply the
requested app filter (instagram)").

**2. h02 — top-1% creator share for WhatsApp (metric_scoping,
held-out).** Baseline returns **0.09644** (all-apps); true
WhatsApp-scoped **0.09764**, 1.2% apart — again inside tolerance.
Both variants clarify. Held-out confirmation that the fix is not a
dev-only artifact.

**3. h03 — commenter penetration for Instagram (metric_scoping,
dev).** Baseline **0.09151** vs scoped **0.09395**, 2.7% apart —
just outside tolerance, so a value check would catch this one, but
the scope error is the same. Both variants clarify.

**4. h04 — Reels completion rate for Facebook (metric_scoping,
dev).** Baseline **0.35049** vs scoped **0.35095**, 0.13% apart.
Both variants clarify.

**5. h05 — Reels watch time for Instagram (metric_scoping, dev).**
The visible one. Baseline **0.90523** vs scoped **0.73088** — 19%
apart, the largest gap in the class. Both variants clarify.

**6. h06 — two-way conversation rate for Facebook (metric_scoping,
dev).** Baseline **0.66764** vs the draft's candidate scoped value
**0.11155**. Caveat, carried from slice 2b: the app-scoped
definition is still undecided (Decision B deferred it), so 0.11155
is a candidate premise, not a settled ground truth. Both variants
clarify, which is the correct behavior regardless of which
definition is eventually settled.

**7. h07 — WAU for WhatsApp (metric_scoping, dev).** The absurd
one. Baseline **267,349** (all-apps mean WAU) vs scoped **79,951**
— 3.3×. Both variants clarify.

**8. h08 — new users for Instagram (metric_scoping, held-out).**
Baseline **250,222** (all new users in the window) vs scoped
**112,209** — 2.2×. Both variants clarify.

**9. h09 — D7 retention for WhatsApp (metric_scoping, dev).**
Baseline **0.22216** vs scoped **0.22282**, 0.3% apart — inside
tolerance, same class as h01. Both variants clarify.

**10. h17 — DAU in April 2026 (incorrect_assumption, dev).**
Baseline returns **89,324.19** — the full-window mean DAU —
presented as April DAU. True April sessions: **0** (max session
date is pinned at 2026-03-31). Validated clarifies ("april is
outside the data window"). Catalog-assisted still answers: by
design, the catalog has no window dimension, so it cannot see this
failure. This is the one case where the two variants disagree on
dev, and it is exactly the boundary drawn in the design.

**11. h18 — WAU in April (incorrect_assumption, held-out).**
Same as h17 for WAU: baseline returns **267,348.93** (full-window
mean WAU) as "April WAU"; validated clarifies; catalog-assisted
still answers. Held-out confirmation of the designed boundary.

**12. b05 — "How many new users were there in the window?"
(metric_scoping, dev). The coverage cost — in slice 3, the only
case either variant broke; fixed by the 'in'-preposition pass.**
Slice-3 behavior: baseline answers correctly (250,222) while both
variants clarify, wrongly. Cause, diagnosed by probing
`_extract_filters`: the question's "in the window" was read as
country code IN (the slice-2b "in → IN" fix exempted only "in
\<Month\>" and "in the last/past ..."). So both variants believed a
country filter was requested: catalog-assisted found country not
in `new_users_in_window`'s scope and clarified; validated found no
country condition in the SQL and abstained. The variants inherited
the filter-extraction noise — validation can only check the plan
against the planner's own (here wrong) reading of the question.
It was deliberately NOT fixed in slice 3: fixing the extractor
would change baseline behavior mid-experiment (it is the control).
The 'in'-preposition pass (2026-11-08/09) fixed it at the source:
bare "in" is the country code IN only when it is literally the
code ("in IN"); every other "in" is a preposition. Re-run: b05
passes under all three variants (query, 250,222, no country filter
in the SQL), the baseline is unchanged at 112/120, and the
coverage cost is zero — which is why H1 and H3 now meet their
exact predictions (§Hypothesis verdicts). The one-case miss is
kept in the record: the cost was closed by fixing the cause, not
by re-reading the prediction.

**13. b07 — "What is the median session duration in minutes?"
(aggregation_grain, dev). A latent wrong-scope answer no variant
caught — and the benchmark could not see it; fixed by the
'in'-preposition pass.** Slice-3 behavior: the question's "in
minutes" was read as country IN, so the baseline SQL was
`... WHERE ... AND u.country = 'IN'` — an India-only median
presented as the overall median. It passed under all three
variants (validated: the SQL *did* contain the requested country
filter, so validation passed; catalog-assisted: country *is* in
`median_session_duration_min`'s scope). And the value could not
betray it: India-only median is **12.0**, all-apps median is
**12.0** — identical. Only a structural check ("no country filter
unless asked") would catch it, and b07 carries none. The sweep
found the same spurious-IN reading in b22 ("List the experiments
in the registry" — harmless, no SQL) and in the invalid-country
cases b31/t55/t56 (harmless — the invalid-country branch fires
first). The 'in'-preposition pass fixed the reading at the source
(same rule as b05): re-run, the b07 SQL under all three variants
carries no country filter, value still 12.0 — now the scope is
right as well as the value. The structural check the benchmark
lacked is now pinned by the renamed test
(`test_in_the_window_and_in_minutes_are_prepositions`), which
asserts no country filter for b05- and b07-style phrasings and
that "in Brazil" / "in the UK" scope to BR / GB, not IN. b07
passes in this report, and the report says so plainly rather than
counting it as evidence of correctness — the value alone never
could have shown the fix.

### What this slice does not show

- The held-out sets are still "held out from fixes", not pristine
  (all 31 hard cases were probed during drafting). The 30/30 for
  validated is a confirmation, not a pristine generalization claim.
- Validated's 100% accuracy among attempted answers is 71/71 dev +
  20/20 held-out — a strong result on this benchmark, small n.
  It says "on these 150 cases the validator never let a wrong
  answer through"; it does not say "the validator never lets a
  wrong answer through".
- Neither variant fixes the planner's underlying templates: the
  nine metrics still cannot be app-scoped, April still has no
  window check in the baseline. The variants *detect* the gap and
  abstain; they do not close it. Closing it (app-scoping nine
  metrics, the April check) is planner work for a later slice,
  after which the baseline — and these variants — must be re-run on
  the same frozen benchmark. The "in" preposition fix, listed here
  as future work in slice 3, is done: the 'in'-preposition pass
  (2026-11-08/09) fixed the extractor and re-ran all six v3 runs
  plus the six v4+p03 variant runs above.

## Variant runs on v4+p03 (first variant runs on v4)

The 'in'-preposition pass also ran all three variants on v4+p03
(153 cases, 123 dev / 30 held-out) — the three probe cases (p01–
p03) had never been run under the variants before. Traces:
`evals/runs/benchmark_v4_*_inpass.json` (baseline dev/held-out,
catalog-assisted dev/held-out, validated dev/held-out). Oracle
153/153 agree, 0 inexact, in all three dev runs.

| Variant | Dev (123) | Held-out (30) |
|---|---|---|
| Baseline | 115/123 | 27/30 |
| Catalog-assisted | 122/123 | 29/30 |
| Validated | 123/123 | 30/30 |

Failures: baseline dev the same 8 as v3 (h01, h03, h04, h05, h06,
h07, h09, h17); catalog-assisted dev only h17; validated dev none.
Held-out: baseline h02, h08, h18; catalog-assisted h18; validated
none. Per family, dev: metric_scoping 28/35 → 35/35 → 35/35;
incorrect_assumption 8/9 → 8/9 → 9/9; all other families full
under all variants. Singles-only rates (dev n=111, held-out n=27):
baseline dev coverage 81/111 (73.0%), accuracy 90.1% (73/81);
catalog-assisted 74/111 (66.7%), 98.6% (73/74), wrong abstentions
0, missed 1 (h17); validated 73/111 (65.8%), 100.0% (73/73),
wrong abstentions 0, missed 0. Held-out rates are identical to
the v3 held-out rates (same 30 cases). The v4 runs change no
ablation conclusion: the variants fix the same findings on the
three added cases as on v3 (all three probes pass under every
variant), and the 'in' fix is what moves catalog-assisted and
validated to 122 and 123 on dev.
