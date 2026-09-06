# RETRO-E

**Start here: [`docs/README.md`](docs/README.md)** — index, current status, runbook, and the
artifact/hash map. `uv run python scripts/status.py` prints live state read from the files
themselves.

RETRO-E is a controlled pre-experiment for one question: can a frozen LLM's retrosynthetic ability be improved by adding and optimizing a compact synthetic-experience context?

The repository deliberately separates five things that must not drift together:

1. frozen route-generation prompt;
2. versioned experience context (`none`, `E0`, `E*`, length control);
3. target splits with provenance and leakage checks;
4. raw generations and blind pairwise judgments;
5. statistical aggregation and immutable protocol manifests.

## Layout

```text
config/                 locked experiment parameters
prompts/                versioned generation/judge/context-synthesis prompts
contexts/               E0, E*, control, and optimizer candidates
data/raw/               source files (local, not committed)
data/interim/           extracted/normalized routes
data/processed/         frozen target splits
schemas/                target data contract
src/retro_e/            API, validation, generation, judge, metrics, optimizer boundary
results/generations/    append-only raw generation records
results/judgments/      append-only randomized blind votes
results/manifests/      hashes of every experiment-defining asset
reports/                protocol and final report
tests/                  offline unit tests
results/manifests/round1_frozen/   byte-exact round-1 protocol inputs (see Round 2 below)
```

## Setup

Use Python 3.11-3.13; the machine's default Python 3.14 is intentionally excluded because the chemistry stack is not available there.

```bash
uv venv --python 3.13
uv sync --extra dev
cp .env.example .env
set -a; source .env; set +a
uv run retro-e doctor
uv run retro-e list-models
```

Put the supplied API key only in `.env` or the process environment. Do not add it to TOML, prompts, manifests, outputs, shell scripts, or commits. After `list-models`, lock exact `RETRO_E_GENERATOR_MODEL` and `RETRO_E_JUDGE_MODEL` IDs; prefer a different judge model family when the gateway offers one.

## Safe execution order

```bash
uv run retro-e audit-targets data/processed/train_targets.jsonl data/processed/test_targets.jsonl
uv run retro-e manifest --stage final

# Copy the printed protocol hash into each command.
uv run retro-e generate --targets data/processed/test_targets.jsonl --condition baseline --protocol-hash HASH
uv run retro-e generate --targets data/processed/test_targets.jsonl --condition E0 --protocol-hash HASH

uv run retro-e judge \
  --left results/generations/E0.jsonl \
  --right results/generations/baseline.jsonl \
  --left-source E0 --right-source baseline \
  --comparison E0_vs_baseline \
  --output results/judgments/E0_vs_baseline.jsonl \
  --protocol-hash HASH

uv run retro-e metrics \
  --comparison E0=results/judgments/E0_vs_baseline.jsonl \
  --comparison E_star=results/judgments/E_star_vs_E0.jsonl \
  --comparison E_star=results/judgments/E_star_vs_baseline.jsonl
```

Every generation and judgment is append-only and resumable. Invalid model output is retained with errors instead of being silently discarded or repaired.

The detailed staged plan and decision rules are in [reports/EXPERIMENT_PLAN.md](reports/EXPERIMENT_PLAN.md).

## Round 2 (`v2_pilot`)

Round 1 is finished and frozen. Its results, contexts, splits, and `config/experiment.toml` are
read-only history; round 2 assets live in a separate namespace.

```text
config/v2_pilot.toml    v2 config: model roles, judge_v3, 1 vote/pair, 800-token contexts
data/v2/                dev_new (80), holdout_new (60), external_audit_panel (40), splits.json
results/v2/             v2 generations, judgments, calibration records
reports/v2/             experiment registry, decision report, casebook
```

Two things must hold before any v2 code change lands:

```bash
# 1. Round-1 protocol hashes are re-derivable only from this snapshot, because
#    build_manifest hashes the whole src/retro_e tree. Freeze once, verify forever.
uv run python scripts/freeze_round1_protocol.py --mode verify

# 2. New splits stay scaffold-disjoint from every round-1 split.
uv run retro-e audit-targets \
  data/processed/train_targets.jsonl data/processed/test_targets.jsonl \
  data/v2/dev_new_targets.jsonl data/v2/holdout_new_targets.jsonl
```

Model roles are declared in `config/v2_pilot.toml` under `[roles]` and resolved from the
environment at run time, so the generator and the judge can be different models and the manifest
records which model played which part. The v2 assignment (generator `gemini-3.8-flash-high`,
judge `gemini-3.7-flash-high`) comes from a measured startup calibration, not from list price:
see `results/v2/calibration/startup_calibration.json` and
[reports/v2/experiment_registry.md](reports/v2/experiment_registry.md).
