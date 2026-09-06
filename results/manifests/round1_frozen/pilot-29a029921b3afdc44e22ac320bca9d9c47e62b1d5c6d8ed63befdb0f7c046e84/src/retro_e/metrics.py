from __future__ import annotations

import csv
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from .io import read_jsonl, write_json
from .judge import source_winner


def exact_sign_test_p_value(wins: int, losses: int) -> float:
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / (2**n)
    return min(1.0, 2 * tail)


def wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> list[float]:
    if trials == 0:
        return [0.0, 1.0]
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _cluster_bootstrap_net(
    outcomes: list[dict[str, str]], seed: int, draws: int = 10000
) -> list[float]:
    by_target: dict[str, list[str]] = defaultdict(list)
    for row in outcomes:
        by_target[row["target_id"]].append(row["outcome"])
    targets = sorted(by_target)
    if not targets:
        return [0.0, 0.0]
    rng = random.Random(seed)
    values = []
    for _ in range(draws):
        sampled = [rng.choice(targets) for _ in targets]
        flat = [outcome for target in sampled for outcome in by_target[target]]
        values.append((flat.count("win") - flat.count("loss")) / len(flat))
    return [_percentile(values, 0.025), _percentile(values, 0.975)]


def summarize_judgments(
    judgment_path: Path,
    favored_source: str,
    seed: int = 20260904,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_rows = list(read_jsonl(judgment_path))
    for row in all_rows:
        grouped[str(row["pair_id"])].append(row)
    outcomes = []
    for pair_id, rows in grouped.items():
        winner = source_winner(rows)
        outcome = "tie" if winner == "Tie" else ("win" if winner == favored_source else "loss")
        outcomes.append(
            {
                "pair_id": pair_id,
                "target_id": str(rows[0]["target_id"]),
                "outcome": outcome,
            }
        )
    wins = sum(row["outcome"] == "win" for row in outcomes)
    losses = sum(row["outcome"] == "loss" for row in outcomes)
    ties = sum(row["outcome"] == "tie" for row in outcomes)
    total = len(outcomes)
    non_ties = wins + losses
    generation_validity: dict[str, dict[str, bool]] = defaultdict(dict)
    for row in all_rows:
        for label in ("A", "B"):
            source = str(row[f"route_{label}_source"])
            record_id = str(row[f"route_{label}_record_id"])
            generation_validity[source][record_id] = bool(row.get(f"route_{label}_valid"))
    validity_summary = {
        source: {
            "generations": len(records),
            "valid": sum(records.values()),
            "invalid": len(records) - sum(records.values()),
            "invalid_rate": (len(records) - sum(records.values())) / len(records)
            if records
            else 0.0,
        }
        for source, records in sorted(generation_validity.items())
    }

    return {
        "comparison": next(iter(grouped.values()))[0]["comparison"] if grouped else "unknown",
        "favored_source": favored_source,
        "pairs": total,
        "targets": len({row["target_id"] for row in outcomes}),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_rate": wins / total if total else 0.0,
        "loss_rate": losses / total if total else 0.0,
        "tie_rate": ties / total if total else 0.0,
        "net_win_rate": (wins - losses) / total if total else 0.0,
        "conditional_win_rate_excluding_ties": wins / non_ties if non_ties else 0.0,
        "conditional_win_rate_95ci_wilson": wilson_interval(wins, non_ties),
        "two_sided_exact_sign_test_p": exact_sign_test_p_value(wins, losses),
        "target_cluster_bootstrap_net_win_95ci": _cluster_bootstrap_net(outcomes, seed),
        "generation_validity_by_source": validity_summary,
    }


def add_holm_adjustment(metrics: list[dict[str, Any]]) -> None:
    """Add Holm-adjusted p-values in place for the supplied confirmatory family."""
    ordered = sorted(
        enumerate(metrics), key=lambda item: float(item[1]["two_sided_exact_sign_test_p"])
    )
    running = 0.0
    total = len(ordered)
    for rank, (original_index, metric) in enumerate(ordered):
        adjusted = min(1.0, (total - rank) * float(metric["two_sided_exact_sign_test_p"]))
        running = max(running, adjusted)
        metrics[original_index]["holm_adjusted_p"] = running


def write_metric_outputs(
    output_json: Path, output_csv: Path, metrics: list[dict[str, Any]]
) -> None:
    write_json(output_json, {"comparisons": metrics})
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "comparison",
            "favored_source",
            "pairs",
            "targets",
            "wins",
            "losses",
            "ties",
            "win_rate",
            "loss_rate",
            "tie_rate",
            "net_win_rate",
            "two_sided_exact_sign_test_p",
            "holm_adjusted_p",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(metrics)
