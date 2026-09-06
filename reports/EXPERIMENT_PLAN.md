# RETRO-E pre-experiment plan

## Research claims

- H1: under a frozen model and protocol, `E0` improves routes over `baseline`.
- H2: under the same frozen model and protocol, training the text context produces `E*` that improves routes over `E0`.
- Auxiliary control: `E*` outperforms chemistry-related text of matched length.

The unit of generalization is the target molecule. The foundation model, exact model ID, prompts, sampling, target splits, number of samples, judge, and evaluation prompt are frozen. Only the experience text changes.

## Stage 0 — protocol lock and API pilot

1. Query available model IDs, select one exact generator/judge model ID, and record it.
2. Run an API-format pilot on 5 targets, equally for baseline and E0. This pilot may debug parsing, timeouts, output limits, and cost only; it must not be used to rewrite experience based on route quality.
3. Freeze the prompt, configuration, tokenizer, target schema, retry policy, and manifest hash.
4. Keep `n_routes=1`: each API completion is one independent route sample. Multiple routes in a single completion are correlated and make pair formation ambiguous.

Gate: at least 90% parseable responses in both pilot conditions, no condition-specific retry/repair, and estimated budget accepted.

## Stage 1 — data construction and leakage control

- Experience Source Set: 200 high-quality multistep routes with source title/DOI or dataset ID, entry/page, evidence, extraction confidence, SMILES validation, and review flags.
- Context Training Set: 200 targets, independent of source and test sets.
- Test Set: 100 untouched targets, selected by scaffold-group split where feasible.

Audit exact canonical-SMILES overlap, stereochemistry-stripped/InChIKey overlap where available, Bemis–Murcko scaffold overlap, source-route family overlap, and near-duplicate analog series. Test targets remain inaccessible to context synthesis and optimization code.

## Stage 2 — construct E0

Synthesize general experience only from the source routes using `prompts/experience_synthesis_v1.txt`. A chemist reviews it for target-specific memorization, false factual claims, and non-strategic filler. Lock a model-native tokenizer and compress to the predeclared budget (target about 1000 tokens). Hash E0 before H1 generation.

## Stage 3 — H1 screen before expensive optimization

Use a predeclared 30-target validation/pilot subset that is not the final test set. Generate 3 independent baseline and E0 samples per target. Pair equal sample IDs, randomize display order independently for each vote, and take 3 judge votes per pair.

Proceed to context training only if the direction is positive and operational quality is acceptable. This is a resource gate, not the final H1 test; it cannot be reported as confirmatory evidence.

## Stage 4 — train E0 into E*

Use GEPA/DSPy as a black-box text optimizer on the 200 training targets. The mutable object is only the experience text. Reward is candidate route versus the cached baseline route: win 1, tie 0.5, loss 0. Cache baseline generations and all judge outcomes. Use target-level batches and a fixed evaluation budget.

Split the 200 training targets once into 160 optimizer targets and 40 context-selection targets, grouped by scaffold. A concrete first budget is: screen at most 24 candidate contexts on fixed 40-target batches with one route sample and one judge vote; advance at most 6 candidates to 120 optimizer targets with one sample and three votes; evaluate the final two candidates once on all 40 selection targets with three route samples and three votes. Promote exactly one E*, without looking at test data. GEPA may choose candidate edits, but it may not choose the evaluation targets, sample counts, or stopping rule.

Recommended safeguards:

- separate optimizer-development and training-validation subsets inside the 200 targets;
- compare candidates on common targets and common baseline samples;
- cap E* at 110% of E0 model-native tokens;
- store candidate text, parent, mutation rationale, reward, target batch, hashes, and total API usage;
- forbid target SMILES, route copies, test access, system-prompt edits, tools, retrieval, and parameter changes;
- promote E* by training-validation reward, then freeze it once.

## Stage 5 — one-shot confirmatory test

On the untouched 100 targets, generate 3 samples for each of baseline, E0, E*, and the length control. The three primary comparisons are E0 vs baseline, E* vs E0, and E* vs baseline. E* vs control is auxiliary. Pair by target and sample ID; never select the best of three generations.

Run the primary A/B/C experiment first: 900 route generations and 2700 judge calls. If the primary run is operationally sound, add the auxiliary control: 300 generations and 900 judge calls. These counts exclude retries and optimizer training. Record token usage from the pilot before accepting the full budget.

Each valid route pair receives 3 blind votes with deterministic independent order randomization. Votes are first mapped back to source condition and then majority-aggregated. The frozen operational-failure rule is: valid output beats invalid output; two invalid outputs tie. This rule is emitted as three deterministic `predeclared_validity_rule` votes so every planned target/sample pair remains in the estimand. Raw outputs, invalid outputs, order mappings, usage, latency, request IDs, and validation findings are retained.

## Statistics and decision rules

For each comparison report wins, losses, ties, net win rate, conditional win rate excluding ties with Wilson 95% CI, and a two-sided exact sign test. Because three samples from the same target are correlated, the main uncertainty interval is a target-cluster bootstrap, not a route-pair IID interval.

Predeclare conclusions before test execution:

- `Yes`: favored condition has positive net win rate, target-cluster 95% CI excludes 0, and Holm-adjusted primary-comparison p-value is below 0.05.
- `No`: the upper confidence bound is at or below a predeclared smallest meaningful net-win improvement (recommended first value: +5 percentage points), so a useful benefit is excluded or the effect is reversed.
- `Inconclusive`: all other cases, including underpowered results.

H1 is supported only by E0 vs baseline. H2 is supported only by E* vs E0. E* vs baseline is consistency evidence, not a substitute for H2. Report invalid-output rate by condition as a co-primary operational metric; invalid generations are never dropped.

## Important design risks

1. A single LLM judge can share biases with the generator. Use a different judge model family if available; otherwise disclose this and perform a small, blinded chemist agreement audit.
2. Temperature 0 does not guarantee deterministic judging. Preserve three independent calls and request metadata.
3. E0 construction and E* optimization can leak molecule families without exact target matches. Scaffold and source-family audits are mandatory.
4. A route may be valid JSON and valid SMILES but chemically poor. RDKit validation is an operational gate, never the quality endpoint.
5. Placeholder contexts and example targets in the repository must never enter a run. The manifest and preflight checks should reject them before the API pilot.
