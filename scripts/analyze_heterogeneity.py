"""Is the effect of an experience context heterogeneous across targets, or is it noise?

A routed experience system -- several experiences, each invoked only when relevant -- needs
one thing to be true first: that "when does this experience help" carries a stable, detectable
signal. If per-target effects are indistinguishable from i.i.d. noise, there is nothing to
route on and the system cannot beat a single monolithic context.

Round-1 Stage 5 gives three independent samples per target per condition, which is exactly
what is needed. If the effect is molecule-specific, the three samples within a target should
agree more than chance. The permutation null shuffles outcomes across targets, preserving the
overall win/tie/loss rates.

    uv run python scripts/analyze_heterogeneity.py
"""

from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from retro_e.judge import source_winner

ROOT = Path(__file__).resolve().parent.parent
JUDGMENTS = ROOT / "results/judgments"
PERMUTATIONS = 10000
SEED = 20260913

COMPARISONS = {
    "E_star_vs_E0": ("E_star", "final_E_star_vs_E0.jsonl", "lunajudge_E_star_vs_E0.jsonl"),
    "E0_vs_baseline": ("E0", "final_E0_vs_baseline.jsonl", "lunajudge_E0_vs_baseline.jsonl"),
    "E_star_vs_baseline": (
        "E_star",
        "final_E_star_vs_baseline.jsonl",
        "lunajudge_E_star_vs_baseline.jsonl",
    ),
}


def per_sample_scores(path: Path, favored: str) -> dict[str, dict[int, float]]:
    by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            by_pair[row["pair_id"]].append(row)
    out: dict[str, dict[int, float]] = defaultdict(dict)
    for rows in by_pair.values():
        winner = source_winner(rows)
        score = 1.0 if winner == favored else (0.0 if winner == "Tie" else -1.0)
        out[str(rows[0]["target_id"])][int(rows[0]["sample_id"])] = score
    return out


def within_target_spread(scores: dict[str, dict[int, float]]) -> float:
    """Mean absolute deviation of samples inside a target. Lower = more consistent."""
    spreads = []
    for samples in scores.values():
        values = list(samples.values())
        if len(values) < 2:
            continue
        mean = statistics.mean(values)
        spreads.append(statistics.mean(abs(value - mean) for value in values))
    return statistics.mean(spreads) if spreads else float("nan")


def permutation_null(scores: dict[str, dict[int, float]], seed: int) -> tuple[float, float]:
    """Reshuffle outcomes across targets, preserving group sizes and the overall rate mix."""
    flat = [value for samples in scores.values() for value in samples.values()]
    sizes = [len(samples) for samples in scores.values()]
    rng = random.Random(seed)
    draws = []
    for _ in range(PERMUTATIONS):
        shuffled = flat[:]
        rng.shuffle(shuffled)
        index = 0
        spreads = []
        for size in sizes:
            chunk = shuffled[index : index + size]
            index += size
            if size < 2:
                continue
            mean = statistics.mean(chunk)
            spreads.append(statistics.mean(abs(value - mean) for value in chunk))
        draws.append(statistics.mean(spreads))
    return statistics.mean(draws), statistics.pstdev(draws)


def oracle_ceiling(scores: dict[str, dict[int, float]]) -> dict[str, float]:
    """If a perfect router applied the context only where it helps, what is the ceiling?

    This is an oracle using the same data it is evaluated on, so it is an upper bound and
    nothing more: a real router must decide from the target alone, before seeing outcomes.
    """
    means = {target: statistics.mean(s.values()) for target, s in scores.items()}
    applied = statistics.mean(means.values())
    routed = statistics.mean(max(value, 0.0) for value in means.values())
    return {
        "always_apply_net": applied,
        "oracle_routed_net": routed,
        "headroom": routed - applied,
        "targets_helped": sum(value > 0 for value in means.values()),
        "targets_harmed": sum(value < 0 for value in means.values()),
        "targets_neutral": sum(value == 0 for value in means.values()),
    }


def main() -> None:
    report: dict[str, Any] = {"permutations": PERMUTATIONS, "seed": SEED, "comparisons": {}}
    for name, (favored, gemini_file, luna_file) in COMPARISONS.items():
        block: dict[str, Any] = {}
        judges: dict[str, dict[str, dict[int, float]]] = {}
        for judge, filename in (("gemini", gemini_file), ("luna", luna_file)):
            path = JUDGMENTS / filename
            if not path.exists():
                continue
            scores = judges[judge] = per_sample_scores(path, favored)
            observed = within_target_spread(scores)
            null_mean, null_sd = permutation_null(scores, SEED)
            block[judge] = {
                "within_target_spread_observed": observed,
                "within_target_spread_null_mean": null_mean,
                "within_target_spread_null_sd": null_sd,
                "z_vs_null": (observed - null_mean) / null_sd if null_sd else float("nan"),
                **oracle_ceiling(scores),
            }
        if len(judges) == 2:
            shared = sorted(set(judges["gemini"]) & set(judges["luna"]))
            gm = {t: statistics.mean(judges["gemini"][t].values()) for t in shared}
            lm = {t: statistics.mean(judges["luna"][t].values()) for t in shared}
            both_helped = sum(gm[t] > 0 and lm[t] > 0 for t in shared)
            both_harmed = sum(gm[t] < 0 and lm[t] < 0 for t in shared)
            disagree = sum((gm[t] > 0) != (lm[t] > 0) for t in shared)
            try:
                corr = statistics.correlation(
                    [gm[t] for t in shared], [lm[t] for t in shared]
                )
            except statistics.StatisticsError:
                corr = float("nan")
            block["cross_judge_target_agreement"] = {
                "targets": len(shared),
                "pearson_r_of_per_target_effect": corr,
                "both_judges_helped": both_helped,
                "both_judges_harmed": both_harmed,
                "sign_disagreement": disagree,
            }
        report["comparisons"][name] = block

    out = ROOT / "results/v2/heterogeneity_analysis.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    for name, block in report["comparisons"].items():
        print(f"\n=== {name} ===")
        for judge in ("gemini", "luna"):
            if judge not in block:
                continue
            b = block[judge]
            print(
                f"  [{judge}] 组内离散 观测={b['within_target_spread_observed']:.4f} "
                f"零假设={b['within_target_spread_null_mean']:.4f}"
                f"±{b['within_target_spread_null_sd']:.4f}  z={b['z_vs_null']:+.2f}"
            )
            print(
                f"  {'':9} 帮助/损害/中性 = {b['targets_helped']}/{b['targets_harmed']}"
                f"/{b['targets_neutral']}   "
                f"全用={b['always_apply_net']:+.3f} → 理想路由={b['oracle_routed_net']:+.3f} "
                f"(上限增益 {b['headroom']:+.3f})"
            )
        if "cross_judge_target_agreement" in block:
            c = block["cross_judge_target_agreement"]
            print(
                f"  [跨judge] 逐target效应相关 r={c['pearson_r_of_per_target_effect']:+.3f}  "
                f"同判帮助={c['both_judges_helped']} 同判损害={c['both_judges_harmed']} "
                f"符号不一致={c['sign_disagreement']}/{c['targets']}"
            )
    print(f"\n写入 {out}")
    print("\n组内离散显著低于零假设（z 明显为负）= 存在分子级的稳定效应，可路由。")
    print("z 接近 0 = 逐 target 效应与噪声无法区分，路由无信号可用。")


if __name__ == "__main__":
    main()
