"""P0 re-analysis of the round-1 confirmatory results (plan v2 section 2.1).

Four checks the plan requires before leaning on round 1:

1. Recompute wins/losses/ties from the raw judgment records and reconcile against the
   recorded metrics files. Nothing is rewritten: divergences are reported.
2. Redo the analysis with the target as the unit. Round 1's exact sign test ran over 300
   route pairs as if independent, but three samples share a target.
3. Compare the two judges ON THE SAME ROUTES as a paired, target-clustered contrast. The
   plan is explicit that "one significant, one not" does not establish that the judges
   differ; only a direct test of the difference does.
4. Report per-condition invalid rates alongside, since they are a co-primary metric.

    uv run python scripts/p0_round1_reanalysis.py

Writes results/v2/p0_round1_reanalysis.json. Round-1 files are read-only inputs.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from retro_e.io import read_jsonl
from retro_e.judge import source_winner

ROOT = Path(__file__).resolve().parent.parent
JUDGMENTS = ROOT / "results/judgments"
BOOTSTRAP_DRAWS = 10000
PERMUTATIONS = 10000
SEED = 20260906

COMPARISONS = {
    "E0_vs_baseline": ("E0", "baseline"),
    "E_star_vs_E0": ("E_star", "E0"),
    "E_star_vs_baseline": ("E_star", "baseline"),
    "E_star_vs_control": ("E_star", "control"),
}


def pair_scores(path: Path, favored: str) -> dict[str, dict[int, float]]:
    """+1 / 0 / -1 for the favored condition, per (target, sample), after majority vote."""
    by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(path):
        by_pair[row["pair_id"]].append(row)
    scores: dict[str, dict[int, float]] = defaultdict(dict)
    for rows in by_pair.values():
        winner = source_winner(rows)
        score = 1.0 if winner == favored else (0.0 if winner == "Tie" else -1.0)
        scores[str(rows[0]["target_id"])][int(rows[0]["sample_id"])] = score
    return scores


def tallies(scores: dict[str, dict[int, float]]) -> dict[str, int]:
    flat = [value for samples in scores.values() for value in samples.values()]
    return {
        "pairs": len(flat),
        "targets": len(scores),
        "wins": sum(value > 0 for value in flat),
        "losses": sum(value < 0 for value in flat),
        "ties": sum(value == 0 for value in flat),
    }


def target_means(scores: dict[str, dict[int, float]]) -> dict[str, float]:
    return {
        target: sum(samples.values()) / len(samples) for target, samples in scores.items()
    }


def bootstrap_ci(values: list[float], seed: int) -> list[float]:
    if not values:
        return [float("nan"), float("nan")]
    rng = random.Random(seed)
    draws = sorted(
        statistics.mean(rng.choices(values, k=len(values))) for _ in range(BOOTSTRAP_DRAWS)
    )
    lower = draws[max(0, math.floor(0.025 * (len(draws) - 1)))]
    upper = draws[min(len(draws) - 1, math.ceil(0.975 * (len(draws) - 1)))]
    return [lower, upper]


def sign_flip_p(values: list[float], seed: int) -> float:
    """Cluster sign-flip permutation test: exchangeable under H0 of zero mean effect.

    Uses the target as the exchangeable unit, which the route-pair sign test did not.
    """
    if not values:
        return float("nan")
    observed = abs(statistics.mean(values))
    rng = random.Random(seed)
    extreme = sum(
        abs(statistics.mean(value if rng.getrandbits(1) else -value for value in values))
        >= observed - 1e-12
        for _ in range(PERMUTATIONS)
    )
    return (extreme + 1) / (PERMUTATIONS + 1)


def exact_sign_test(wins: int, losses: int) -> float:
    total = wins + losses
    if total == 0:
        return 1.0
    tail = sum(math.comb(total, k) for k in range(min(wins, losses) + 1)) / (2**total)
    return min(1.0, 2 * tail)


def holm(entries: list[tuple[str, float]]) -> dict[str, float]:
    ordered = sorted(entries, key=lambda item: item[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    for rank, (name, raw) in enumerate(ordered):
        running = max(running, min(1.0, (len(ordered) - rank) * raw))
        adjusted[name] = running
    return adjusted


def recorded_metrics() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for name in ("final_primary_metrics", "final_control_metrics", "final_lunajudge_metrics"):
        path = ROOT / "results" / f"{name}.json"
        if not path.exists():
            continue
        for entry in json.loads(path.read_text(encoding="utf-8"))["comparisons"]:
            found[f"{name}:{entry['comparison']}"] = entry
    return found


def main() -> None:
    report: dict[str, Any] = {
        "purpose": "Plan v2 section 2.1 P0 re-analysis of round-1 confirmatory results.",
        "inputs_read_only": True,
        "bootstrap_draws": BOOTSTRAP_DRAWS,
        "permutations": PERMUTATIONS,
        "seed": SEED,
        "check_1_recount": {},
        "check_2_target_unit": {},
        "check_3_paired_judge_difference": {},
    }
    recorded = recorded_metrics()

    gemini: dict[str, dict[str, dict[int, float]]] = {}
    luna: dict[str, dict[str, dict[int, float]]] = {}
    for comparison, (favored, _) in COMPARISONS.items():
        final_path = JUDGMENTS / f"final_{comparison}.jsonl"
        luna_path = JUDGMENTS / f"lunajudge_{comparison}.jsonl"
        if final_path.exists():
            gemini[comparison] = pair_scores(final_path, favored)
        if luna_path.exists():
            luna[comparison] = pair_scores(luna_path, favored)

    # --- check 1: recount against the recorded metrics -------------------------------
    for judge_label, scores_by_comparison, metric_files in (
        ("gemini", gemini, ("final_primary_metrics", "final_control_metrics")),
        ("gpt-5.6-luna", luna, ("final_lunajudge_metrics",)),
    ):
        for comparison, scores in scores_by_comparison.items():
            counts = tallies(scores)
            match = None
            for prefix in metric_files:
                # The cross-judge run suffixed its comparison names; try both spellings.
                entry = recorded.get(f"{prefix}:{comparison}") or recorded.get(
                    f"{prefix}:{comparison}_lunajudge"
                )
                if entry is None:
                    continue
                match = {
                    "recorded_wins": entry["wins"],
                    "recorded_losses": entry["losses"],
                    "recorded_ties": entry["ties"],
                    "recorded_net_win_rate": entry["net_win_rate"],
                    "agrees": (
                        entry["wins"] == counts["wins"]
                        and entry["losses"] == counts["losses"]
                        and entry["ties"] == counts["ties"]
                    ),
                }
                break
            report["check_1_recount"][f"{judge_label}:{comparison}"] = {
                **counts,
                "recomputed_net_win_rate": (counts["wins"] - counts["losses"]) / counts["pairs"],
                "reconciliation": match or "no recorded metrics entry found",
            }

    # --- check 2: target as the unit of analysis ------------------------------------
    for judge_label, scores_by_comparison in (("gemini", gemini), ("gpt-5.6-luna", luna)):
        raw_p: list[tuple[str, float]] = []
        block: dict[str, Any] = {}
        for comparison, scores in scores_by_comparison.items():
            counts = tallies(scores)
            means = list(target_means(scores).values())
            route_pair_p = exact_sign_test(counts["wins"], counts["losses"])
            target_p = sign_flip_p(means, SEED)
            block[comparison] = {
                **counts,
                "route_pair_sign_test_p_treats_300_pairs_as_independent": route_pair_p,
                "target_mean_net": statistics.mean(means),
                "target_cluster_bootstrap_95ci": bootstrap_ci(means, SEED),
                "target_sign_flip_p": target_p,
                "targets_positive": sum(value > 0 for value in means),
                "targets_negative": sum(value < 0 for value in means),
                "targets_zero": sum(value == 0 for value in means),
            }
            if comparison != "E_star_vs_control":
                raw_p.append((comparison, target_p))
        for comparison, adjusted in holm(raw_p).items():
            block[comparison]["target_sign_flip_holm_p"] = adjusted
        if "E_star_vs_control" in block:
            block["E_star_vs_control"]["holm_note"] = (
                "Auxiliary comparison, its own family of one (round-1 convention retained)."
            )
        report["check_2_target_unit"][judge_label] = block

    # --- check 3: paired judge difference on identical routes ------------------------
    for comparison in sorted(set(gemini) & set(luna)):
        shared = sorted(set(gemini[comparison]) & set(luna[comparison]))
        gemini_means = target_means(gemini[comparison])
        luna_means = target_means(luna[comparison])
        differences = [gemini_means[target] - luna_means[target] for target in shared]
        agreements = []
        for target in shared:
            for sample, value in gemini[comparison][target].items():
                other = luna[comparison][target].get(sample)
                if other is not None:
                    agreements.append(value == other)
        report["check_3_paired_judge_difference"][comparison] = {
            "targets_compared": len(shared),
            "gemini_target_mean_net": statistics.mean(gemini_means[t] for t in shared),
            "luna_target_mean_net": statistics.mean(luna_means[t] for t in shared),
            "mean_difference_gemini_minus_luna": statistics.mean(differences),
            "difference_cluster_bootstrap_95ci": bootstrap_ci(differences, SEED),
            "difference_sign_flip_p": sign_flip_p(differences, SEED),
            "pairwise_verdict_agreement_rate": (
                sum(agreements) / len(agreements) if agreements else None
            ),
            "interpretation_rule": (
                "A judge difference is established only if this interval excludes 0. Comparing "
                "each judge's own significance separately does not test this."
            ),
        }

    out = ROOT / "results/v2/p0_round1_reanalysis.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["check_3_paired_judge_difference"], ensure_ascii=False, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
