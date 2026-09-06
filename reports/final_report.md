# RETRO-E Final Report: Trainable Synthetic-Experience Context for LLM Retrosynthesis

**Date:** 2026-09-04 | **Status:** Primary confirmatory test, auxiliary control, and a post-hoc
cross-judge robustness check are all complete. **This report was revised after the cross-judge check
changed the H1 verdict** — see Section 5.4.

---

## 1. Executive Summary

Under a fully frozen foundation LLM, system prompt, sampling parameters, and target set, adding a
short synthetic-experience context (`E0`) to the prompt improved blind pairwise retrosynthetic route
quality over the same LLM with no context — but only when judged by the same model that generated the
routes (self-judging). Re-judging the identical routes with a genuinely different model
(`gpt-5.6-luna` in place of the self-judging `gemini-3.7-flash-high`) **did not replicate this
effect**: net win fell from +11.7% (significant) to +2.7% (confidence interval crosses zero, not
significant). **H1 is downgraded from "Yes" to "Inconclusive."** Training that context further with a
GEPA-style reflective-mutation optimizer produced `E*`, which improved over `E0` again on the same
untouched test set under *both* judges — self-judged +14.0% and independently judged +13.0%, both
statistically significant. **H2 is supported, and is the more robust of the two findings.**

| Comparison | Judge | Net win rate | Target-cluster 95% CI | Holm-adjusted p |
|---|---|---:|---:|---:|
| E0 vs baseline (**H1**) | gemini (self) | +11.7% | [1.3%, 21.7%] | 0.043 |
| E0 vs baseline (**H1**) | **gpt-5.6-luna (independent)** | **+2.7%** | **[−8.3%, 13.3%]** | **0.680** |
| E\* vs E0 (**H2**) | gemini (self) | +14.0% | [3.3%, 24.3%] | 0.024 |
| E\* vs E0 (**H2**) | gpt-5.6-luna (independent) | +13.0% | [3.0%, 22.7%] | 0.046 |
| E\* vs baseline (consistency) | gemini (self) | +23.3% | [12.7%, 33.7%] | <0.0001 |
| E\* vs baseline (consistency) | gpt-5.6-luna (independent) | +16.0% | [5.3%, 26.7%] | 0.016 |
| E\* vs control (auxiliary, self-judged only) | gemini (self) | +14.3% | [3.0%, 25.7%] | 0.011 |

The auxiliary length-matched control (E\* vs. a non-strategic chemistry-facts text of matched token
length) rules out "longer / more chemistry-flavored prompt" as the explanation for E\*'s advantage —
a text of matched length and comparable chemistry register, but with no retrosynthetic strategy
content, does not match E\*'s performance. This check was only run under the self-judging model; given
the cross-judge finding below, it should be read with the same caveat as the self-judged H1 result
until independently re-judged.

The most important finding of this project is not a single number: it is that **self-judging
materially inflated one result (H1) and left another intact (H2)**, exactly the failure mode the
original protocol's own risk list warned about (Section 7). Treat H1 as an open question, not a
confirmed effect, until it is tested under an independent judge at full confirmatory scale.

---

## 2. Research Question and Hypotheses

Two questions, tested under one constraint — the foundation model is never fine-tuned or otherwise
modified. Only the text handed to it as "experience" changes.

- **H1** — Does adding a fixed, unoptimized synthetic-experience context (`E0`) improve retrosynthesis
  route quality over the same frozen LLM with no context? `LLM(x, E0) > LLM(x)`
- **H2** — Can that context be trained/optimized (with the foundation model still frozen) to a form
  `E*` that improves further over `E0`? `LLM(x, E*) > LLM(x, E0)`

The guiding principle throughout: *never train the LLM; only train the experience context.*

---

## 3. Experimental Design

### 3.1 Variables held frozen throughout

Foundation model + version, system prompt, target splits, output schema, sampling parameters
(temperature/top_p/max_tokens/n_routes), number of independent samples per target, judge model,
judge prompt, and evaluation rubric. The only thing that varies across conditions is the experience
context text. Test targets were never used to build `E0`, optimize `E*`, or select the control text.

### 3.2 Four conditions

| Condition | Prompt content |
|---|---|
| `baseline` | System prompt + target only |
| `E0` | System prompt + unoptimized synthetic experience + target |
| `E*` | System prompt + GEPA-optimized synthetic experience + target |
| `control` | System prompt + length-matched, non-strategic chemistry-facts text + target |

### 3.3 Data

Source: **PaRoutes 2.0 n1 reference routes** (Zenodo, DOI `10.5281/zenodo.7341155`) — routes
mechanically extracted from a USPTO patent reaction network. **These are benchmark routes, not
independently validated laboratory or literature syntheses**; every record is flagged
`needs_human_review=true` and lacks reaction class, isolated yield, and full conditions. This is a
data-quality limitation carried through the whole project (see Section 7).

Selection: targets with 18–65 heavy atoms, routes with ≥5 steps and ≥2 leaves, fixed seed
`20260902`, filtered from 10,000 raw routes (967 eligible after size/complexity screening) down to
500 unique Murcko scaffolds. Three scaffold-disjoint splits:

| Split | n | Role |
|---|---:|---|
| Experience Source Routes | 200 | Only used to synthesize `E0` |
| Train (Context Training Set) | 200 | Split into 160 optimizer + 40 selection targets for Stage 4 |
| Test | 100 | Untouched until Stage 5; scaffold-disjoint from the other two splits |

Leakage audit (`data/processed/dataset_quality_report.json`, `retro-e audit-targets`): 0 canonical
SMILES leaks and 0 Murcko scaffold overlaps across splits; 0 duplicate target IDs; 300/300
train+test targets parse cleanly under RDKit.

### 3.4 `E0` construction

`E0` was synthesized *only* from the 200 Experience Source Routes (never from target performance):
10 batches of 20 routes summarized independently, aggregated and compressed to a target budget,
audited by a separate model for target memorization / overgeneralization, and revised — all before
any generation or judging occurred. Final `E0`: 16 numbered entries, no SMILES, no source-route
identifiers (confirmed by exact-string leak check), frozen at
sha256 `183f9018fdae2555720e49d1f65842519e033a089dfb6eab03428396d8ba88e4`.

### 3.5 `E*` optimization (Stage 4)

A GEPA-style black-box text optimizer, implemented as a three-tier successive-halving harness rather
than a literal `dspy.GEPA` autoloop, because the protocol requires that *"GEPA may choose candidate
edits, but it may not choose the evaluation targets, sample counts, or stopping rule"* — GEPA's own
internal budget/Pareto scheduler cannot be made to respect an externally predeclared budget schedule.
Concretely: each mutation step critiques the current candidate's losing/tying cases (judge-family
model) and rewrites the text (generator-family model) — the reflective-mutation idea from GEPA,
externally scheduled.

| Tier | Candidates evaluated | Target batch | Samples × votes | Best reward |
|---|---:|---|---|---:|
| 1 (screen) | 24 (incl. E0 seed) | 40 (fixed subset of the 160 optimizer targets) | 1 × 1 | 0.713 (E0 seed: 0.525) |
| 2 (advance) | top 6 from tier 1 | 120 (disjoint remainder of the 160) | 1 × 3 | 0.637 |
| 3 (final) | top 2 from tier 2 | 40 selection targets (untouched until this tier) | 3 × 3 | **0.621 (winner)** |

Reward = mean(win=1, tie=0.5, loss=0) of the candidate's route vs. a cached baseline route on the
same target, judged blind with randomized order. Notably, tier 1's rank-1 candidate (0.713 on 40
targets, 1 vote) dropped to a mid-pack 0.537 once re-evaluated on the larger, more heavily voted tier
2 batch — exactly the small-batch overfitting the successive-halving design exists to catch. The
actual winner, `5fd7566a40e30762`, was tier 1's *second*-best candidate.

Hard constraints enforced throughout: `E*` token count ≤ 110% of `E0` under a locked `tiktoken`
`o200k_base` encoding (winner: 755 vs. 736 tokens, ratio 1.026); every candidate scanned for
verbatim leakage of any train/test target SMILES or source identifier (zero leaks — this check
actively discarded at least one over-budget/leaking candidate during the run); the optimizer never
saw `test_targets.jsonl`.

Winning `E*` (`contexts/experience_E_star.md`, sha256
`5fd7566a40e3076275e8845c2b65e92914f72562a5c37bf4b4fbad4a1bbaf79a`) reads as 16 entries of the same
register and structure as `E0` — strategic disconnection heuristics, convergence/linearity tradeoffs,
protecting-group and chemoselectivity judgment calls — not a list of memorized reactions.

### 3.6 Blind pairwise judging

For every target/sample pair, two routes are randomly and independently assigned to "Route A" /
"Route B" per vote (deterministic seeded randomization, so the mapping is reproducible but the judge
never sees which condition produced which route). Three independent judge votes per pair; majority
vote after mapping back to source condition. Invalid model output is never silently dropped: a valid
route beats an invalid one, two invalid routes tie (`predeclared_validity_rule`), keeping every
planned target/sample pair inside the analysis.

### 3.7 Protocol deviations made mid-project (fully disclosed)

Two changes were made after Stage 3 (H1-screen) had already completed, both driven by cost, not by
a change in scientific design intent — documented in full with measured evidence in `HANDOFF.md`
sections 10a/10b:

1. **Judge model: `gpt-5.6-sol` → `gemini-3.7-flash-high`.** `gpt-5.6-sol` is a reasoning model that
   spends ~1,000–3,700 hidden reasoning tokens per single-token judge call (measured on real
   H1-screen data and cross-checked against its `luna`/`terra` siblings — none were meaningfully
   cheaper). The remaining protocol needed ~6,500–7,500 judge calls, i.e. ~24–28M tokens on that
   model family alone. The user made an informed decision to switch the judge to
   `gemini-3.7-flash-high` — the same model as the generator — accepting **self-judging** as a
   deliberate cost/rigor tradeoff. Pilot and H1-screen (already complete, `gpt-5.6-sol`-judged) were
   **not** re-judged: their only role was a resource gate, not confirmatory evidence, so this does not
   contaminate the Stage 4/5 results, which are self-consistent under one judge model throughout.
2. **Judge prompt v1 → v2, `max_tokens` 16 → 150.** Switching the judge to `gemini-3.7-flash-high`
   under the old prompt/token-budget produced a **50% parse-failure rate** on a 16-pair real-data
   test (the model would start explaining before answering, then get truncated mid-sentence). Fixed
   with `prompts/judge_v2.txt` (explicit `DECISION: <A|B|Tie>` anchor as the required first line) and
   a lenient anchor-based fallback in `parse_decision`. Re-tested on the same 16 pairs: 16/16 parsed,
   averaging only 4 visible completion tokens per call.

Both changes were made, tested against real API calls, and frozen **before** any Stage 4/5 data was
collected — no results were collected under the broken configuration.

---

## 4. Statistical Methods

Per comparison: wins/losses/ties, net win rate `(wins − losses) / pairs`, conditional win rate
excluding ties with a Wilson 95% CI, and a two-sided exact sign test. Because three samples from the
same target are correlated observations, the **primary uncertainty interval is a target-level cluster
bootstrap** (10,000 resamples of targets, not of individual pairs), not an i.i.d. pairwise interval.
Across the three-comparison confirmatory family, p-values are **Holm-adjusted** for multiplicity.
Decision rule (pre-registered, `reports/EXPERIMENT_PLAN.md`):

- **Yes** — positive net win rate, cluster-bootstrap CI excludes 0, Holm-adjusted p < 0.05.
- **No** — CI upper bound at or below +5 percentage points (a useful benefit is excluded).
- **Inconclusive** — anything else, including underpowered results.

---

## 5. Results

### 5.1 Stage 0–3 — pilot and H1-screen (non-confirmatory, resource gates only)

| Stage | n targets | Pairs | Net win rate | Sign-test p | Cluster 95% CI |
|---|---:|---:|---:|---:|---:|
| Pilot (`E0` vs baseline) | 5 | 15 | +33.3% | 0.302 | wide, uninformative |
| H1-screen (`E0` vs baseline) | 30 | 90 | +17.8% | 0.105 | [−1.1%, 36.7%] |

Both used `gpt-5.6-sol` as judge (pre-dating the protocol change). Direction was positive and the
gate criterion ("proceed to expensive optimization only if the screen direction is positive") was
met, but per protocol these numbers were never treated as confirmatory and did not inform `E0` or
any later prompt edits.

### 5.2 Stage 4 — optimization trajectory

See Section 3.5 above. Headline: every one of the 24 tier-1 mutations matched or beat the E0 seed's
own reward on that batch; the winning `E*` beat its cached baseline 72–43 (5 ties) on the 40
never-before-used selection targets.

### 5.3 Stage 5 — primary confirmatory test (the main result)

100 untouched test targets, 3 independent samples per condition (baseline/E0/E\*/control), 3
independent judge votes per pair. 1,200 route generations, 3,600 judge calls, 0 API failures.

| Condition | Generations | Invalid rate |
|---|---:|---:|
| baseline | 300 | 5.0% |
| E0 | 300 | 4.7% |
| E\* | 300 | 4.7% |
| control | 300 | 4.0% |

| Comparison | Wins | Losses | Ties | Net win | Win rate excl. ties (Wilson 95% CI) | Cluster-bootstrap 95% CI | Sign-test p | **Holm-adjusted p** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **E0 vs baseline (H1)** | 159 | 124 | 17 | **+11.7%** | 56.2% [50.4%, 61.8%] | **[1.3%, 21.7%]** | 0.0431 | **0.0431** |
| **E\* vs E0 (H2)** | 154 | 112 | 34 | **+14.0%** | 57.9% [51.9%, 63.7%] | **[3.3%, 24.3%]** | 0.0118 | **0.0236** |
| E\* vs baseline | 172 | 102 | 26 | +23.3% | 62.8% [56.9%, 68.3%] | [12.7%, 33.7%] | 0.0000279 | 0.0000838 |
| E\* vs control *(auxiliary)* | 158 | 115 | 27 | +14.3% | 57.9% [51.9%, 63.6%] | [3.0%, 25.7%] | 0.0109 | 0.0109 *(n=1 family)* |

The Holm correction for the three primary rows is computed within that three-comparison family only,
exactly as predeclared; `E* vs control` is auxiliary and reported as its own single-comparison family
(so its "Holm-adjusted" p equals its raw sign-test p). Mixing it into the primary family would
silently change the primary rows' adjusted p-values — this was caught and corrected during analysis
(`results/final_primary_metrics.json` for the primary three, `results/final_control_metrics.json`
for the auxiliary one).

**E\* also beats the length-matched, non-strategic control**, at a magnitude (+14.3%) close to its win
over E0 (+14.0%). Since `control` and `E*` are matched in token length and both read as
chemistry-adjacent text, this result rules out "longer prompt" or "more chemistry-flavored tokens" as
the explanation for E\*'s advantage — the actionable strategic content in E\* is doing the work, not
its length or register. (This check has only been run under the self-judging model — see 5.4.)

### 5.4 Cross-judge robustness check (post-hoc)

Limitation 1 in the original design (self-judging: Stage 4/5 use `gemini-3.7-flash-high` as both
generator and judge) was a disclosed risk, not yet a measured one. To test it directly, the three
primary comparisons were **re-judged on the identical, already-generated Stage 5 routes** using
`gpt-5.6-luna` — a genuinely different model family from the frozen generator — in place of the
self-judging `gemini-3.7-flash-high` judge. No new routes were generated; only the judge changed.
2,700 additional judge calls, 0 API failures, same underlying routes and validity rates as the
self-judged run.

| Comparison | Wins–Losses–Ties | Net win | Cluster 95% CI | Holm p (own 3-comparison family) |
|---|---:|---:|---:|---:|
| E0 vs baseline (H1) | 148–140–12 | +2.7% | [−8.3%, 13.3%] | 0.680 |
| E\* vs E0 (H2) | 159–120–21 | +13.0% | [3.0%, 22.7%] | 0.046 |
| E\* vs baseline | 166–118–16 | +16.0% | [5.3%, 26.7%] | 0.016 |

**H2 replicates**: net win, confidence interval, and significance all hold under the independent
judge, with the margin narrowing but not disappearing (Holm p moves from 0.024 to 0.046 — still below
0.05). **H1 does not replicate**: the independent judge sees a much weaker effect whose interval
crosses zero (Holm p = 0.680). Gemini's self-assessment appears to specifically inflate `E0`'s
advantage over `baseline` in a way that does not survive a change of judge; the advantage of `E*` over
`E0` — the actual training effect — does not depend on which model is judging.

Cost note: `gpt-5.6-luna` was found to have essentially the same reasoning-token overhead as
`gpt-5.6-sol` (~4,000 tokens per judge call on real Stage-5-style prompts, measured before this run),
the exact cost profile Section 3.7 moved the frozen judge away from. This check was therefore scoped
to the three primary comparisons only, skipping the auxiliary control, to answer the most important
open question (does H1/H2 hold under an independent judge) without repeating that cost commitment at
full scale.

**Revised verdict for H1**: per the pre-registered decision rule ("Inconclusive: all other cases,
including underpowered results"), a result that reverses from significant to null under a materially
different, equally-capable judge on identical data is not a confident "Yes." H1 is downgraded to
**Inconclusive** pending a full independent-judge confirmatory run. H2 stands, and is now the
better-supported of the two hypotheses in this project.

---

## 6. Decision-Rule Verdicts

> **Question 1 — Does a fixed synthetic-experience context improve retrosynthesis route quality
> under a completely frozen foundation LLM?**
> **Answer: Inconclusive.** Under the self-judging model: net win +11.7%, cluster 95% CI
> [1.3%, 21.7%], Holm p = 0.043 — a "Yes" on its own. Under an independent judge (`gpt-5.6-luna`) on
> the identical routes: net win +2.7%, cluster 95% CI [−8.3%, 13.3%] (crosses zero), Holm p = 0.680 —
> not significant. A result that reverses under a materially different judge is not a confident "Yes"
> per the pre-registered decision rule. This is the headline caution of the whole project.

> **Question 2 — Can that experience context be trained/optimized to improve further, with the
> foundation LLM still frozen?**
> **Answer: Yes — and this is the more robust finding.** Self-judged: E\* vs E0 net win +14.0%,
> cluster 95% CI [3.3%, 24.3%], Holm p = 0.024. Independently judged on the identical routes: net win
> +13.0%, cluster 95% CI [3.0%, 22.7%], Holm p = 0.046. Same direction, similar magnitude,
> significant under both judges.

Read together: the evidence that *training* the experience context helps (H2) is solid and survives
a change of judge. The evidence that *adding* an untrained experience context helps at all (H1) does
not survive that same check, and should be treated as an open question rather than an established
result until re-tested with an independent judge at full confirmatory scale (not just the three
primary comparisons checked here).

---

## 7. Limitations

1. **Self-judging — confirmed to matter, not just a theoretical risk.** Stage 4 and Stage 5 use
   `gemini-3.7-flash-high` as *both* generator and judge, overridden from a different-family judge
   mid-project for cost reasons (Section 3.7). The Section 5.4 cross-judge check turned this from a
   disclosed risk into a measured one: re-judging the identical Stage 5 routes with `gpt-5.6-luna`
   left H2 intact (+14.0% self-judged → +13.0% independent, both significant) but **collapsed H1**
   (+11.7% self-judged → +2.7% independent, CI crosses zero). Self-judging inflated the *presence* of
   an experience-context effect (H1) without inflating the *training* effect (H2) — a specific,
   asymmetric finding, not a uniform bias in one direction. `E*` was also optimized against reward
   from the same judge that evaluates it in Stage 5, a channel through which it could in principle
   exploit judge-specific quirks; that H2 survived an independent judge argues against this being a
   large effect here, but does not rule out a smaller one.
2. **Data provenance.** PaRoutes routes are patent-mined, not independently validated syntheses.
   RDKit-valid, schema-compliant output is an operational floor, not a chemical-quality endorsement —
   a route can be syntactically perfect and still be a poor real synthesis. All judging is LLM
   judgment of unverified hypotheses, not literature-procedure comparison.
3. **Judge determinism.** Temperature 0 does not guarantee identical repeated judgments from a
   generative model; three independent votes with majority aggregation partially mitigates this but
   does not eliminate it.
4. **No independent (e.g. expert-chemist) judge-agreement audit** has been performed to validate that
   either LLM judge's preferences track real synthetic merit. The Section 5.4 check shows the two LLM
   judges disagree with each other on H1 specifically, which makes this audit more urgent, not less —
   an LLM-vs-LLM disagreement does not by itself say which one (if either) is closer to real synthetic
   judgment.
5. **Auxiliary control run under the self-judging model only.** `E*` vs. a length-matched,
   non-strategic chemistry-facts text: net win +14.3%, cluster 95% CI [3.0%, 25.7%], p = 0.011 —
   closes the "longer / more chemistry-flavored prompt" explanation for E\*'s advantage, but given
   Section 5.4, this specific result has not itself been cross-judge-checked and should carry the same
   caveat as the self-judged H1 result until it is.
6. **Effect size.** H2 is statistically significant but modest under both judges checked so far — not
   evidence of a dramatic capability jump. H1's status is now Inconclusive rather than a modest
   positive effect (Section 5.4).

---

## 8. Reproducibility

Every experiment-defining asset (config, prompts, schemas, code, contexts, target files) is hashed
into a versioned protocol manifest before any API call under that manifest is made
(`results/manifests/*.json`); every generation and judgment record carries its `protocol_sha256` and
is rejected from mixing with a different one. All raw model output, including invalid generations, is
retained. Key hashes:

| Asset | SHA-256 |
|---|---|
| `E0` | `183f9018fdae2555720e49d1f65842519e033a089dfb6eab03428396d8ba88e4` |
| `E*` | `5fd7566a40e3076275e8845c2b65e92914f72562a5c37bf4b4fbad4a1bbaf79a` |
| Stage 4 (optimization) protocol | `ec50b6273304e3e28f53051c0edead6b99128325d84356a9cabae162bafb6ac8` |
| Stage 5 (final) protocol | `cb9d6faac3bbceca4a520d6f40f469f5579f48f9f6371e37f2129179cb06a1a7` |
| Stage 5 cross-judge (`gpt-5.6-luna`) protocol | `b501f1272363f04bde2e4c9744dc500f70c46df2e73880508a6d1a3a03d5eba7` |

Full raw data: `results/generations/final_*.jsonl`, `results/judgments/final_*.jsonl`,
`results/optimization/ledger.jsonl` (all 32 Stage-4 candidates with text, parent, reward, and
generation/judgment file pointers), `results/final_primary_metrics.json` (the three primary
comparisons, Holm-corrected as one family), `results/final_control_metrics.json` (the auxiliary
comparison, its own single-comparison family), and `results/final_lunajudge_metrics.json` (the
cross-judge check, Section 5.4). The cross-judge check's input files
(`results/generations/lunajudge_input_*.jsonl`) are byte-identical copies of the corresponding
`final_*.jsonl` generations with only the `protocol_sha256` field relabeled to the cross-judge
protocol hash, so they pass the same integrity guard for a run that intentionally reuses fixed routes
under a new judge; the original `final_*.jsonl` files remain the untouched canonical record.

---

## 9. Recommended Next Steps

1. **Re-test H1 at full confirmatory scale under an independent judge**, not just the post-hoc
   3-comparison check in Section 5.4. The current independent-judge result for H1 (n=100 targets, 900
   pairs, 3 votes) is already reasonably powered and points to "no effect," but a dedicated
   pre-registered run — ideally with a judge cheaper than the `gpt-5.6` reasoning family — would settle
   it more cleanly than a post-hoc check appended to an existing report.
2. **Re-run the auxiliary control comparison under an independent judge too** (Limitation 5) — it has
   only been self-judged so far, and Section 5.4 shows self-judging is not uniformly reliable.
3. A small blinded chemist-agreement audit against both LLM judges would help adjudicate which one (if
   either) is closer to real synthetic merit, now that they disagree on H1 specifically.
4. For any follow-up study, budget explicitly for an independent-judge run from the start rather than
   discovering the cost mid-run, and pick a genuinely cheap different-family judge if possible — this
   project's two candidate alternatives (`gpt-5.6-sol`, `gpt-5.6-luna`) both turned out to share the
   same expensive reasoning-token profile.
5. Given H2's effect size is real but modest, further work on *how* E\* helps (which of its 16 entries
   drive the win, via ablation) would be more informative than simply scaling up sample counts.
