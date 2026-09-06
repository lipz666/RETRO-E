# RETRO-E 文档索引

这个项目有两轮实验、两套协议哈希、两个数据命名空间。这份索引说明**每份文档回答什么问题**，
以免在编年体日志里翻找。

## 按你要做的事查

| 你想知道 | 看这里 |
|---|---|
| 现在是什么状态、下一步做什么 | [STATUS.md](STATUS.md) |
| 实时数字（运行进度、哈希、划分、校验） | `uv run python scripts/status.py` |
| 怎么跑 / 怎么续跑 / 怎么校验 | [RUNBOOK.md](RUNBOOK.md) |
| 某个文件是什么、哪个哈希对应什么 | [ARTIFACTS.md](ARTIFACTS.md) |
| round 1 的科学结论 | [../reports/final_report_zh.md](../reports/final_report_zh.md) |
| round 2 的完整实验记录、事故、负结果 | [../reports/v2/experiment_registry.md](../reports/v2/experiment_registry.md) |
| round 1 的实验设计与决策规则 | [../reports/EXPERIMENT_PLAN.md](../reports/EXPERIMENT_PLAN.md) |
| E0 → E\* 到底改了什么 | [../reports/v2/e0_estar_semantic_diff.md](../reports/v2/e0_estar_semantic_diff.md) |
| round 1 结果的 P0 复核 | `results/v2/p0_round1_reanalysis.json` |

## 文档分工

三层，互不重复：

- **`docs/`（操作层）** —— 当前状态、命令、文件地图。会被覆盖更新，永远只描述「现在」。
- **`reports/`（科学层）** —— 实验计划、登记表、最终报告、分析产物。追加不覆盖，是论文的原料。
- **`results/` `data/` `contexts/`（证据层）** —— 原始记录，append-only，从不手工编辑。

数字只有一个来源：**证据层**。`STATUS.md` 里的叙述可能滞后，`scripts/status.py` 不会 ——
它读文件，不读文档。任何时候两者冲突，以脚本为准。

## 写文档的规矩

1. **状态写进 `STATUS.md`，不要追加到编年体日志。** `HANDOFF.md` 与
   `experiment_registry.md` 是历史记录，不是状态看板。
2. **事故、负结果、协议变更写进 `experiment_registry.md`。** 计划 v2 §15 要求它记录
   「所有实验与变更、未完成/负结果」——包括失败的和被推翻的。
3. **不要把数字硬编码进叙述。** 能从文件算出来的，让 `scripts/status.py` 算。
4. **round 1 的资产是只读的。** 见 [ARTIFACTS.md](ARTIFACTS.md) 的冻结说明。
