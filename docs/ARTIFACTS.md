# 文件与哈希地图

## 协议哈希

哈希覆盖：使用中的 config 文件、system/judge prompt、两个 schema、该 stage 引用的
context 与 target 文件、以及 `src/retro_e/*.py` 全部源码。**`scripts/` 不在其中。**

| 阶段 | 哈希 | generator | judge | 状态 |
|---|---|---|---|---|
| round1 pilot | `29a02992…` | gemini-3.7-flash-high | gpt-5.6-sol | 部分文件已漂移 |
| round1 h1_screen | `6040c06b…` | gemini-3.7-flash-high | gpt-5.6-sol | 部分文件已漂移 |
| round1 optimization | `ec50b627…` | gemini-3.7-flash-high | gemini-3.7-flash-high | 22/23 可复原 |
| round1 final（Stage 5 主实验） | `cb9d6faa…` | gemini-3.7-flash-high | gemini-3.7-flash-high | **22/22 完整冻结** |
| round1 final（跨 judge 检验） | `b501f127…` | gemini-3.7-flash-high | gpt-5.6-luna | **22/22 完整冻结** |
| **v2 optimization（当前）** | **`c7a40018…`** | **gemini-3.8-flash-high** | **gemini-3.7-flash-high** | 使用中 |

实时列表：`uv run python scripts/status.py`。

### 已作废的 v2 哈希

不要使用，对应数据已删除：

| 哈希 | 作废原因 |
|---|---|
| `7aa453d0…` | CLI 未绑定角色，baseline 实际由 gemini-3.7 生成 |
| `fd81a5da…` | optimizer 用 `parse_decision` 解析不了 judge_v3 |
| `3fb5fb19…` | judge_v3 输出约 1% 被 `max_tokens=600` 截断 |

### 聚合哈希是路径相关的

`build_manifest` 把 `config.public_dict()` 哈希在内，其中含仓库根的**绝对路径**。
把快照复制到别处重建，即使文件逐字节相同，聚合哈希也不同（实测 `2de5ee…` vs `cb9d6f…`）。

- **重新导出聚合哈希** → 必须就地还原到同一绝对路径
- **校验单个文件** → 与路径无关，`freeze_round1_protocol.py --mode verify` 做的就是这个

## 冻结与只读

| 路径 | 规则 |
|---|---|
| `results/manifests/round1_frozen/` | round-1 协议输入的逐字节快照。**只增不改。** |
| `config/experiment.toml` | round-1 配置，不得编辑（编辑会破坏 `cb9d6faa…` 的可复现性） |
| `schemas/target.schema.json` | 同上。v2 的 split 枚举**不要**加进这里 |
| `results/generations/`、`results/judgments/` | round-1 原始记录，append-only |
| `contexts/experience_E0.md`、`experience_E_star.md` | round-1 冻结产物 |
| `src/retro_e/*.py` | **E1 运行期间不得修改**——会让续跑用不同代码跑在同一声明哈希下 |
| `scripts/*` | 不在协议哈希内，运行期间可安全修改 |

## v2 数据划分

| 集合 | 数量 | 文件 | 用途 |
|---|---:|---|---|
| `dev_new` | 80 | `data/v2/dev_new_targets.jsonl` | 方法筛查、E4 模块干预；可反复查看 |
| `holdout_new` | 60 | `data/v2/holdout_new_targets.jsonl` | 最终冻结比较，**只揭盲一次** |
| `external_audit_panel` | 40 | `data/v2/external_audit_panel.jsonl` | dev_new 子集，留给非 Gemini 复核与专家抽样 |
| `ood_probe` | 0 | — | **未构建**，理由见 `data/v2/splits.json` |
| 储备 | 265 | — | 未抽取的唯一 scaffold |

审计报告：`data/v2/splits.json`（含近邻 Tanimoto 分布与隔离检查）。

构造方式：重放 round-1 的种子 `20260902` 剔除其 500 个 scaffold，再用 `20260906` 从剩余
405 个唯一新 scaffold 中抽取。**scaffold 不相交是构造保证的，不是事后过滤的。**

## 上下文文本

| 文件 | 说明 |
|---|---|
| `contexts/experience_E0.md` | round-1 起点，16 条，736 locked token |
| `contexts/experience_E_star.md` | round-1 优化产物，755 token |
| `contexts/experience_control.md` | 长度匹配、无策略内容的化学事实文本 |
| `contexts/v2/<method>/<seed>/winner.md` | E1 赢家。**只有 `promotion.json` 里 `full_schedule: true` 的才有效** |
| `contexts/v2/<method>/<seed>/promotion.json` | 冻结记录：赢家 id、reward、token 数、调度参数 |

locked token 一律用 `tiktoken` `o200k_base` 计数，v2 的共享上限是 **800**（三方法相同）。

## 结果目录

| 路径 | 内容 |
|---|---|
| `results/v2/manifests/` | v2 协议 manifest |
| `results/v2/generations/` | baseline 缓存（optimizer 320、selection 80，各含 2 样本） |
| `results/v2/optimization/<method>/<seed>/` | ledger、各层生成与判定 |
| `results/v2/optimization/_logs/` | 批次完整日志（不截断） |
| `results/v2/calibration/` | 启动校准：模型成本、位置偏倚、judge_v3 解析率 |
| `results/v2/p0_round1_reanalysis.json` | 计划 §2.1 的四项复核 |
| `results/v2/dev_eval/` | §6.3 dev 评估（尚未开始） |

## ledger 字段

`results/v2/optimization/<method>/<seed>/ledger.jsonl` 每行一个候选评估：

| 字段 | 含义 |
|---|---|
| `tier` | `screen` / `advance` / `selection` |
| `candidate_id` | 文本内容的 sha256 前 16 位（内容寻址） |
| `parent_id` | 父代候选；E0 种子为 `null` |
| `saw_task_feedback` | 改写器是否看到了失败案例。`search_only` 恒为 `false` |
| `method_audit` | `instruction_opt` 的化学词汇越界审计结果 |
| `reward` / `pairs` / `expected_pairs` | 奖励与配对完成度。**两者不等时不会写入**（会抛错） |
| `generation_failures` / `judgment_failures` | 重试后仍失败的任务数 |
| `compression_passes` | 压缩兜底触发次数。**方法间差异显著，是已知混淆项** |
| `rejected` | 被拒候选（超长、泄漏、重复）及其原因 |
| `locked_tokens` | o200k_base 计数 |
