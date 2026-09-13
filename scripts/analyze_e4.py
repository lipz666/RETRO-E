"""Target-clustered statistics for the E4 module intervention.

Two things the raw run output cannot settle.

First, whether the net win rates are distinguishable from noise. E1 established that this
system's measurement noise is large: the same E0 text re-evaluated on 20 targets spanned
0.175. Point estimates on 40 targets need a target-clustered interval before they are read.

Second, whether the predeclared tag predictions hold. A module's predicted-tag rate when it
lost is meaningless on its own -- some tags are simply common. The right contrast is within
the same comparison: the rate when the ablated condition lost minus the rate when the full
context lost, which holds the target set and the judge fixed. Specificity is then whether
that contrast is larger for the module's own predicted tags than for the others.

    uv run python scripts/analyze_e4.py
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
JUDGE_DIR = ROOT / "results/v2/ablation/judgments"
BOOTSTRAP = 10000
SEED = 20260913


def cluster_ci(by_target: dict[str, list[float]], seed: int) -> tuple[float, list[float]]:
    keys = sorted(by_target)
    means = [statistics.mean(by_target[k]) for k in keys]
    point = statistics.mean(means) if means else float("nan")
    if len(means) < 2:
        return point, [float("nan"), float("nan")]
    rng = random.Random(seed)
    draws = sorted(statistics.mean(rng.choices(means, k=len(means))) for _ in range(BOOTSTRAP))
    lo = draws[max(0, math.floor(0.025 * (len(draws) - 1)))]
    hi = draws[min(len(draws) - 1, math.ceil(0.975 * (len(draws) - 1)))]
    return point, [lo, hi]


def load(path: Path, full: str, drop: str) -> dict[str, Any]:
    by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(path):
        by_pair[row["pair_id"]].append(row)

    win_by_target: dict[str, list[float]] = defaultdict(list)
    # tag presence per losing side, keyed by target so the bootstrap can cluster on it
    tag_by_target: dict[str, dict[str, list[float]]] = {
        full: defaultdict(list), drop: defaultdict(list)
    }
    losses_by_target: dict[str, dict[str, int]] = {full: defaultdict(int), drop: defaultdict(int)}
    tags_seen: set[str] = set()

    for rows in by_pair.values():
        target = str(rows[0]["target_id"])
        winner = source_winner(rows)
        win_by_target[target].append(
            1.0 if winner == full else (0.0 if winner == "Tie" else -1.0)
        )
        for row in rows:
            verdict = row.get("structured_verdict") or {}
            if not verdict.get("structured") or row["decision"] == "Tie":
                continue
            loser_side = "B" if row["decision"] == "A" else "A"
            loser = row.get(f"route_{loser_side}_source")
            if loser not in tag_by_target:
                continue
            losses_by_target[loser][target] += 1
            for tag in verdict.get("error_tags") or []:
                tags_seen.add(tag)
                tag_by_target[loser][tag].append(1.0)

    net, ci = cluster_ci(win_by_target, SEED)
    # Per-tag rate = tag occurrences / times that condition lost, pooled.
    rates: dict[str, dict[str, float]] = {}
    for side in (full, drop):
        total_losses = sum(losses_by_target[side].values())
        rates[side] = {
            tag: (len(tag_by_target[side].get(tag, [])) / total_losses if total_losses else 0.0)
            for tag in sorted(tags_seen)
        }
        rates[side]["_losses"] = total_losses
    return {
        "pairs": len(by_pair),
        "targets": len(win_by_target),
        "net_full_over_drop": net,
        "net_cluster_95ci": ci,
        "tag_rates": rates,
    }


def main() -> None:
    spec = json.loads(
        (ROOT / "contexts/v2/e4_ablation/variants.json").read_text(encoding="utf-8")
    )
    predicted = {
        f"drop_{name}": set(info["predicted_error_tags"])
        for name, info in spec["variants"].items()
    }

    out: dict[str, Any] = {"bootstrap": BOOTSTRAP, "seed": SEED, "comparisons": {}}
    print(f"{'module dropped':34}{'net(full>drop)':>15}{'cluster 95% CI':>22}{'n':>6}")
    for path in sorted(JUDGE_DIR.glob("full_vs_*.jsonl")):
        drop = path.stem.replace("full_vs_", "")
        block = load(path, "full", drop)
        out["comparisons"][drop] = block
        ci = block["net_cluster_95ci"]
        print(
            f"{drop:34}{block['net_full_over_drop']:>+15.3f}"
            f"{f'[{ci[0]:+.3f}, {ci[1]:+.3f}]':>22}{block['targets']:>6}"
        )

    print("\n预测 tag 的特异性检验（同一对比内：drop 败诉率 − full 败诉率）")
    for drop, block in out["comparisons"].items():
        rates = block["tag_rates"]
        pred = predicted.get(drop, set())

        # A predicted tag that never appeared has a contrast of 0.0, not missing data:
        # "the predicted error did not occur" is itself the finding, and dropping it would
        # silently exclude the clearest possible refutation of the prediction.
        observed = {tag for tag in rates[drop] if tag != "_losses"}
        contrasts = {
            tag: rates[drop].get(tag, 0.0) - rates["full"].get(tag, 0.0)
            for tag in sorted(observed | pred)
        }
        pred_vals = [v for t, v in contrasts.items() if t in pred]
        other_vals = [v for t, v in contrasts.items() if t not in pred]
        pred_mean = statistics.mean(pred_vals) if pred_vals else 0.0
        other_mean = statistics.mean(other_vals) if other_vals else 0.0
        print(f"  {drop}")
        print(
            f"    预测 tag 平均对比 = {pred_mean:+.3f}  "
            f"其他 tag 平均对比 = {other_mean:+.3f}  "
            f"特异性差 = {pred_mean - other_mean:+.3f}"
        )
        top = sorted(contrasts.items(), key=lambda kv: -kv[1])[:3]
        print(
            "    对比最大的 tag: "
            + ", ".join(f"{t}{'*' if t in pred else ''}={v:+.3f}" for t, v in top)
        )
        out["comparisons"][drop]["tag_contrast"] = contrasts
        out["comparisons"][drop]["predicted_tags"] = sorted(pred)

    (ROOT / "results/v2/ablation/ablation_stats.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("\n* = 该模块预先声明的 tag。特异性差 ≈ 0 表示删除模块并未特异性地引发它所针对的错误。")
    print("写入 results/v2/ablation/ablation_stats.json")


if __name__ == "__main__":
    main()
