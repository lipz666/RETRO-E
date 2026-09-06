# RETRO-E 实验交接文档

> **2026-09-06：本文已成为历史记录。** 当前状态、下一步、命令与文件地图请看
> [`docs/`](docs/README.md)：
> [STATUS.md](docs/STATUS.md) 是状态的唯一叙述来源，
> [RUNBOOK.md](docs/RUNBOOK.md) 是命令，
> [ARTIFACTS.md](docs/ARTIFACTS.md) 是文件与哈希地图，
> `uv run python scripts/status.py` 出实时数字。
>
> 本文以下内容按时间顺序保留，用于追溯 round 1 与 v2 早期的决策过程，**不再更新**。

更新时间：2026-09-03（Asia/Shanghai）

## 1. 当前状态（截至 2026-09-03，已被 docs/STATUS.md 取代）

实验已按用户要求暂停，所有 generation、judge 和 context synthesis 进程均已停止。所有成功结果已经以 append-only JSONL 形式落盘，没有需要恢复的内存状态。

当前进度：

- 项目架构：完成。
- 计划 1，API/model pilot：完成。
- 计划 2，200/200/100 数据构建与泄漏审计：完成。
- 计划 3，E0 构建与内容审计：完成。
- 计划 4，30-target H1 screen：完成，包含 180 generations、270/270 judge votes 和正式 screen metrics。
- 计划 5，E* context optimization：尚未开始执行；仅有不可变优化契约和数据划分。
- Final untouched Test Set：从未调用，必须继续保持 untouched。

不要删除或覆盖已有 JSONL。所有 CLI 都支持读取现有键后跳过已完成项。

## 2. 用户指定与冻结模型

- API Base URL：`https://vps.lpzproxy.xyz`
- Chat Completions：`/v1/chat/completions`
- 用户指定 generator：`gemini-3.7-flash-high`
- 网关实际返回的 response model：`gemini-3.7-flash`
- Blind judge：`gpt-5.6-sol`，使用不同模型族减少 generator 自评偏差
- generation：temperature 0.3，top_p 1.0，1 route/call，3 samples/target
- judge：temperature 0.0，3 votes/pair

API key 没有写入仓库。本地继续运行前应设置：

```bash
export RETRO_E_API_KEY='<从用户处安全取得，不要写入版本控制>'
export RETRO_E_GENERATOR_MODEL='gemini-3.7-flash-high'
export RETRO_E_JUDGE_MODEL='gpt-5.6-sol'
```

曾尝试 `claude-opus-5`，但网关返回上游 OAuth expired 401；其结果没有进入任何正式 context 或 generation。E0 batch cache 会按 model ID 过滤，旧模型产生的单个废弃 batch 不会被读取。

## 3. 环境与验证

```bash
cd /Users/lpz/Desktop/AtomFlow/RETRO-E
uv sync --python 3.13 --extra dev
uv run pytest
uv run ruff check src scripts
```

当前验证结果：13 tests passed，Ruff passed。Python 3.13 和 RDKit 已安装在 `.venv`。

主要文档与配置：

- `README.md`
- `reports/EXPERIMENT_PLAN.md`
- `config/experiment.toml`
- `prompts/system_v1.txt`
- `prompts/judge_v1.txt`
- `contexts/experience_E0.md`

## 4. 数据来源和数据集状态

采用官方 PaRoutes 2.0 n1：

- Zenodo：https://zenodo.org/records/7341155
- DOI：`10.5281/zenodo.7341155`
- 原始 route 文件：`data/raw/paroutes_v2/ref_routes_n1.json`
- 原始 route MD5：`8f11ca81d6d184539fa4d2c5bde215af`
- 本地 SHA-256：`b818f2115ce10da51f84bf13f19dc2206863b1b074ef348017de258e9e1bff02`

PaRoutes 是从 USPTO 专利反应网络机械抽取的 benchmark routes，不是独立实验验证路线。所有记录都标为 `compiled_patent_route_dataset`，`needs_human_review=true`，并明确记录缺少 reaction class、isolated yield 和完整 conditions。

构建脚本：`scripts/build_paroutes_dataset.py`

选择条件：

- target 18–65 heavy atoms；
- route 至少 5 steps；
- 至少 2 leaves；
- Experience Source、Train、Test 三组 scaffold-disjoint；
- 固定 seed `20260902`。

已生成：

| 文件 | 数量 | 用途 |
|---|---:|---|
| `data/processed/experience_source_routes.jsonl` | 200 | 仅用于构建 E0 |
| `data/processed/train_targets.jsonl` | 200 | Context training 总集 |
| `data/processed/optimizer_targets.jsonl` | 160 | E* optimizer |
| `data/processed/selection_targets.jsonl` | 40 | E* candidate selection |
| `data/processed/pilot_targets.jsonl` | 5 | API/prompt pilot，属于 optimizer 子集 |
| `data/processed/h1_screen_targets.jsonl` | 30 | H1 resource gate，属于 optimizer 子集 |
| `data/processed/test_targets.jsonl` | 100 | untouched final test |

审计结果见 `data/processed/dataset_quality_report.json`：

- canonical-SMILES 跨 train/test 泄漏：0；
- Murcko scaffold overlap：0；
- duplicate target ID：0；
- provenance missing：0；
- 300 个 train/test targets 均通过 RDKit。

## 5. E0 状态

E0 文件：`contexts/experience_E0.md`

- 最终 SHA-256：`183f9018fdae2555720e49d1f65842519e033a089dfb6eab03428396d8ba88e4`
- 16 个经验条目；
- approximate tokens：1109；
- 精确 source target/route ID 泄漏：0；
- 前 8 条为 route-global strategy，后 8 条为 conditional tactics。

生成过程：

1. `scripts/synthesize_e0.py` 将 200 条路线分成 10 批，每批 20 条；
2. `gemini-3.7-flash-high` 生成 batch summaries；
3. `gpt-5.6-sol` 做内容审计；
4. Gemini 聚合并压缩；
5. `scripts/review_e0.py` 在不查看任何 target performance 的条件下再次审计 route-global coverage；
6. 旧版本和完整审计均保存在 `contexts/candidates/`。

E0 已冻结。不要再根据 pilot/H1 结果修改它，否则 H1 screen 会变成训练数据反馈。

## 6. Pilot 完成结果

Pilot manifest：

```text
29a029921b3afdc44e22ac320bca9d9c47e62b1d5c6d8ed63befdb0f7c046e84
```

产物：

- `results/generations/pilot_baseline.jsonl`：15/15 valid
- `results/generations/pilot_E0.jsonl`：15/15 valid
- `results/judgments/pilot_E0_vs_baseline.jsonl`：45/45 votes，15/15 pairs 完整
- `results/pilot_metrics.json`
- `results/pilot_results.csv`

Pilot majority result：

- E0 wins：10
- baseline wins：5
- ties：0
- net win rate：+33.3%
- exact sign-test p：0.3018
- target-cluster CI 很宽，不能作确认性结论

Pilot 只作为继续 H1 screen 的正向资源 gate，未用于修改 E0。

## 7. H1 screen 完成结果

H1-screen manifest/protocol hash：

```text
6040c06bd57e28470b2d351786d089f4334a2de81953698a57bbac15c4fc2b62
```

Generation 已完成：

- `results/generations/h1_screen_baseline.jsonl`
  - 90 unique records
  - 83 valid / 7 invalid
  - validity 92.2%
- `results/generations/h1_screen_E0.jsonl`
  - 90 unique records
  - 84 valid / 6 invalid
  - validity 93.3%

Judge：

- 文件：`results/judgments/h1_screen_E0_vs_baseline.jsonl`
- 270 unique votes / expected 270
- 90/90 pairs 均具有 3 votes

正式 H1-screen 多数结果：

- E0 wins：51
- baseline wins：35
- ties：4
- net win rate：+17.78%
- conditional E0 win rate excluding ties：59.30%
- conditional Wilson 95% CI：48.74%–69.07%
- exact sign-test p：0.1052（screen，单比较）
- target-cluster bootstrap net-win 95% CI：−1.11%–36.67%

指标文件：

- `results/h1_screen_metrics.json`
- `results/h1_screen_results.csv`

复算命令：

```bash
uv run retro-e metrics \
  --comparison E0=results/judgments/h1_screen_E0_vs_baseline.jsonl \
  --output-json results/h1_screen_metrics.json \
  --output-csv results/h1_screen_results.csv
```

## 8. 已知 API/工程问题

1. `claude-opus-5` 当前不可用，上游 OAuth expired；不要重试作为正式模型。
2. Gemini 偶发 `400 User location is not supported`，已在 `src/retro_e/api.py` 中设为 retryable gateway-pool error。
3. 网关偶发 Cloudflare 524，已加入 retryable status。
4. generation/judge 支持 `--workers`。建议 Gemini generation 先用 6–8 workers，judge 用 8–16；太高并发会增加网关池错误。
5. worker 最终失败不会中止整个批次；成功结果先保存，返回值中的 `failed` 表示应再次运行同一命令补缺失键。
6. invalid generation 不丢弃。valid 对 invalid 获胜，invalid 对 invalid 为 Tie，记录为 `predeclared_validity_rule`。

重要 manifest 注记：H1-screen manifest 创建后，代码只增加了 retryable 400/524 和“单个 worker 失败时保存其他成功结果”的运行可靠性处理；system prompt、judge prompt、模型、target、sampling 和评分规则没有改变。因此已有记录仍使用原 H1 protocol hash。不要回写或伪造新 hash。后续 optimization/final 必须重新创建 manifest 以包含当前代码。

## 9. 计划 5：E* optimization 尚需完成

当前 `src/retro_e/optimize.py` 只定义了冻结/可变契约和 context 长度约束，没有实际 GEPA adapter。

必须保持：

- mutable：只有 experience context text；
- frozen：Gemini generator、system prompt、targets、sampling、judge model、judge prompt；
- reward：win 1 / tie 0.5 / loss 0；
- E* 不得查看 `data/processed/test_targets.jsonl`；
- E* model-native/locked tokenizer token 数不得超过 E0 的 110%；
- 不得复制 target-route pair、加入 tools/RAG/workflow 或修改 system prompt。

建议续做顺序：

1. H1-screen 已完成并冻结；不要据此修改 E0。结果方向为正，但 screen 的 cluster CI 仍跨 0，因此不是确认性 H1 结论。
2. 安装 optimizer 依赖：`uv sync --extra dev --extra optimization`。
3. 为 optimization 创建独立 manifest：`uv run retro-e manifest --stage optimization`。
4. 在 `optimizer_targets.jsonl` 上缓存固定 baseline routes；不要为每个 candidate 重生成 baseline。
5. 实现 GEPA/DSPy adapter，并保存每个 candidate 的：text、parent ID、mutation rationale、hash、token count、target batch、reward、generation record IDs、judge record IDs、API usage。
6. 固定预算建议：
   - 最多 24 candidates × 固定 40 optimizer targets × 1 route sample × 1 vote；
   - 最多 6 candidates × 120 optimizer targets × 1 sample × 3 votes；
   - 最终 2 candidates × 全部 40 selection targets × 3 samples × 3 votes；
   - 只按 selection reward 提升一个 E*。
7. 将最终 E* 写入 `contexts/experience_E_star.md`，保存来源 ledger 和 hash，运行 `retro-e context-budget`。
8. 生成 matched-length chemistry control，之后才建立 final manifest。
9. Final 100 test targets 在 E* 完全冻结前不得运行。

第一阶段 screening 只需 1 sample，而当前主配置固定为 3 samples。不要临时手改同一 config 后混用 manifest；应新增版本化的 optimization config，或给 CLI 增加明确记录在 manifest 中的 sample override。

## 10. 不要做的事情

- 不要修改或清洗已有 raw responses。
- 不要删除 invalid generations。
- 不要把 PaRoutes 称为实验验证文献路线。
- 不要根据 Test Set 修改 E0/E*。
- 不要运行 `data/processed/test_targets.jsonl`，直到 E*、control 和 final manifest 全部冻结。
- 不要把用户 API key 写入 `.env` 后提交、写进本文档或输出文件。
- 不要把 3 个 sample 或 3 个 judge votes 当作 target-level 独立样本。

## 10a. 2026-09-03 追加：judge 模型变更（gpt-5.6-sol -> gemini-3.7-flash-high）

**决策**：Stage 4（optimization）和 Stage 5（final confirmatory test）起，judge 模型从 `gpt-5.6-sol` 改为
`gemini-3.7-flash-high`，与 generator 完全相同 = **自评（self-judging）**。这是用户在权衡成本后做出的
明确决定，接受由此引入的自评偏差（EXPERIMENT_PLAN.md 风险 1 所述）。

**触发原因**：成本，不是技术故障。诊断过程：

1. 单次调用与 8 路并发调用 `gpt-5.6-sol` 均正常返回，排除网关/模型不可用。
2. 用 H1-screen 已有的 234 条真实 judge 记录统计：`gpt-5.6-sol` 平均每次调用消耗
   prompt≈2617 + completion≈1085（其中约 1000+ 是隐藏 reasoning tokens，不受 `max_tokens=16`
   约束）= 总计≈3702 tokens/次，且方差很大。
3. 用同一个真实 judge prompt 交叉测试 `gpt-5.6-luna` / `gpt-5.6-sol` / `gpt-5.6-terra` 三个变体，
   均有 1000+ token 的隐藏 reasoning 开销，没有发现明显更便宜的档位。
4. Stage 4 + Stage 5（含可选 control）预计还需要约 6500-7500 次 judge 调用，若继续用 GPT-5.6 系列，
   总计约 2400-2800 万 tokens 落在 judge 角色上。
5. 换成 `gemini-3.7-flash-high` 后用同一 judge prompt 测试 3 次：稳定返回可解析的单 token 决策，
   总消耗约 800-930 tokens/次，且方差远小于 GPT-5.6 系列。

**对已完成数据的处理**：Pilot（45 votes）和 H1-screen（270 votes）**不重新评判**，原始
`results/judgments/pilot_E0_vs_baseline.jsonl` 与 `results/judgments/h1_screen_E0_vs_baseline.jsonl`
保持不变（judge=`gpt-5.6-sol`）。理由：H1-screen 的作用仅是"是否值得投入昂贵优化"的资源门槛
（EXPERIMENT_PLAN.md Stage 3 明确写明"this is a resource gate, not the final H1 test; it cannot be
reported as confirmatory evidence"），门槛已经通过并已被采纳（继续 Stage 4）。真正的 H1/H2 结论只来自
Stage 5 在 untouched test set 上的确证实验，而 Stage 4/5 会在**同一个新协议哈希下**从头到尾统一使用
`gemini-3.7-flash-high` 同时作为 generator 和 judge，内部完全自洽。

**必须写入 final_report.md 的限制声明**：

- H1-screen（screen-only, 未用于确证）使用 `gpt-5.6-sol` 判定；
- Stage 4 optimization 与 Stage 5 final confirmatory test 使用 `gemini-3.7-flash-high` 自评（generator
  与 judge 为同一模型），因此无法排除 judge 与 generator 共享偏差这个风险（EXPERIMENT_PLAN.md 风险 1）；
- 这是在测得 GPT-5.6 系列 judge 成本（约 2400-2800 万 tokens）后，经用户明确决定的成本权衡，而非协议
  设计缺陷。

**代码影响**：`config/experiment.toml` 未改动（judge 模型始终是环境变量驱动，不写入版本控制文件）；
只改了本地 `.env` 的 `RETRO_E_JUDGE_MODEL`。`build_manifest` 会把 resolved judge model 哈希进
`protocol_sha256`，所以任何在此变更后新建的 manifest（`optimization`、`final`）会自动获得与旧
`pilot`/`h1_screen` manifest 不同的哈希——这是预期行为，不需要额外处理。

## 10b. 2026-09-03 追加：Stage 4 完成，E* 已冻结

Stage 4（E0 -> E* optimization）已跑完并冻结，使用 [scripts/optimize_e_star.py](scripts/optimize_e_star.py) 实现的
reflective-mutation 三层 successive-halving（细节见 `optimize_e_star.py` 模块 docstring 与 [src/retro_e/optimize.py](src/retro_e/optimize.py)）。
Protocol hash：`ec50b6273304e3e28f53051c0edead6b99128325d84356a9cabae162bafb6ac8`（对应新的
generator=judge=`gemini-3.7-flash-high`、`judge_v2` prompt、`max_tokens=150` 的配置）。

**结果**（完整 ledger 见 `results/optimization/ledger.jsonl`，24+6+2 = 32 行）：

| tier | 候选数 | target 批次 | samples/votes | 最优 reward |
|---|---:|---|---|---:|
| tier1（screen） | 24 | 40（optimizer_targets 的固定子集） | 1/1 | 0.713（seed E0 本身 0.525） |
| tier2（advance） | 6 | 120（optimizer_targets 剩余子集，与 tier1 不重叠） | 1/3 | 0.637 |
| tier3（final） | 2 | 40（selection_targets，此前从未用过） | 3/3 | **0.621（胜者）** |

胜出候选 `5fd7566a40e30762`（tier3 reward 0.621，72 胜/43 负/5 平 vs 缓存 baseline）已写入
`contexts/experience_E_star.md`，sha256=`5fd7566a40e3076275e8845c2b65e92914f72562a5c37bf4b4fbad4a1bbaf79a`，
locked tiktoken(`o200k_base`) token 数 755（E0 为 736，比例 1.026，在 110% 上限内）。晋级记录见
`results/optimization/promotion.json`。tier3 亚军 `fa24bdf5c27b4193`（reward 0.608）未被选中，其完整 ledger/生成/
判定记录仍保留在 `results/optimization/tier2/`、`tier3/` 下，供复查。

注意：`locked_token_count`（真实 tiktoken `o200k_base`）给出的 E0 token 数是 736，与 `prompts.approximate_token_count`
字符估算法给出的 1109 不同——这是预期的，优化阶段的预算判定一律以前者为准（见 `optimize.py` 顶部说明）；
`retro-e context-budget` CLI 命令仍使用字符估算法，只作为粗略参考，不是 Stage 4 实际使用的判定依据。

Stage 4 全程未访问 `data/processed/test_targets.jsonl`。

**Length-matched control**：`contexts/experience_control.md` 已从占位符替换为真实内容——16 条纯教科书有机化学
事实/定义（杂化、极性、pKa 趋势、SN1/SN2/E1/E2、碳正离子稳定性、芳香性、IR/NMR、色谱），locked token 数 746
（对 E* 755 的比例 0.988），不含任何逆合成策略指导，也不含任何 target SMILES/ID 泄漏（已跑
`leak_scan` 确认）。

## 10c. 2026-09-04 追加：跨 judge 复现检验，H1 结论下修为 Inconclusive

在 final_report.md 发给专家看之后，应要求对 Stage 5 的三个 primary 对比用 `gpt-5.6-luna`（与冻结
generator `gemini-3.7-flash-high` 完全不同的模型族）重新评判，路线完全复用（不重新生成），只换 judge。
Protocol hash `b501f1272363f04bde2e4c9744dc500f70c46df2e73880508a6d1a3a03d5eba7`（final 阶段 manifest，
judge 环境变量临时改为 `gpt-5.6-luna`，未写回 `.env`）。做法：把 `results/generations/final_{baseline,
E0,E_star}.jsonl` 复制为 `results/generations/lunajudge_input_*.jsonl`，仅把 `protocol_sha256` 字段
重标为新 hash（内容完全不变，用于满足 `assert_protocol` 对"同一文件只能有一个 hash"的检查），原始
`final_*.jsonl` 未改动。2700 次新 judge 调用，0 失败。

**结果**（`results/final_lunajudge_metrics.json`，与 `results/final_primary_metrics.json` 对照）：

| 对比 | gemini（自评） | gpt-5.6-luna（独立） |
|---|---|---|
| E0 vs baseline (H1) | 净胜 +11.7%，CI [1.3%,21.7%]，Holm p=0.043 | **净胜 +2.7%，CI [-8.3%,13.3%]，Holm p=0.680（不显著）** |
| E\* vs E0 (H2) | 净胜 +14.0%，CI [3.3%,24.3%]，Holm p=0.024 | 净胜 +13.0%，CI [3.0%,22.7%]，Holm p=0.046（仍显著） |
| E\* vs baseline | 净胜 +23.3%，CI [12.7%,33.7%]，Holm p=0.0001 | 净胜 +16.0%，CI [5.3%,26.7%]，Holm p=0.016（仍显著） |

**H2 在独立 judge 下复现，H1 没有复现**（CI 直接跨 0）。这把 EXPERIMENT_PLAN.md 风险 1（judge 与
generator 共享偏差）从"披露的理论风险"变成了"实测到的、方向明确的效应"：自评具体虚高了"加经验有没有用"
这件事（H1），但没有虚高"经验能不能被训练"这件事（H2）——是一个不对称的、具体的发现，不是笼统的"自评
不可信"。

**处理**：`reports/final_report.md`（新增 5.4 节、改写 Executive Summary/决策verdict/Limitation 1/4/5/6/
Recommended Next Steps）和已发布的 HTML 报告页都已同步改写，H1 结论明确下修为 **Inconclusive**，H2 保持
**Yes** 并标注为"跨 judge 验证过的、更可靠的发现"。

**成本备注**：`gpt-5.6-luna` 的真实开销（同一真实 judge_v2 prompt 测试）平均约 4008 tokens/次，比当初
放弃的 `gpt-5.6-sol`（3702 tokens/次）还略高——即换掉的判定模型家族里没有"便宜的那一档"。因此这次复现
检验只覆盖了 3 个 primary 对比（2700 次），没有覆盖 auxiliary control（会再加 900 次）；control 目前
仍只有 gemini 自评结果，同样需要标注这个局限。

## 10d. 2026-09-06 追加：v2_pilot 启动（工作包 A 部分完成）

按 `RETRO-E_experiment_plan_v2.md` 开始第二轮预实验。**round 1 的所有结果、结论和文件都不动**；
v2 的资产一律走 `data/v2/` `config/v2_pilot.toml` `results/v2/` `reports/v2/` 独立命名空间。

完整登记见 **`reports/v2/experiment_registry.md`**。摘要：

- **A6 启动校准（107 次真实调用，未生成任何新路线）**：`gemini-3.8-flash-high` 做 judge 每次烧
  2869 个隐藏推理 token，是 `gemini-3.7-flash-high`（638）的 4.5 倍——「两个 3.x 同价」在这个任务上
  不成立。因此 **G=3.8 生成 / JG=3.7 评判**。`claude-haiku-4-5` 零推理 token、2.1 秒，看似理想的
  便宜异家族 judge，但顺序翻转一致性只有 40%、A-rate 75%，**已否决**。异家族预算（600 次 ×
  ~4.1k tok ≈ 250 万 token，仅为 round 1 那笔 2600 万的 1/10）**用户已决定保留**。
  数据：`results/v2/calibration/`。
- **A1 v2 数据划分**：重放 round 1 的种子剔除其 500 个 scaffold 后，剩 405 个唯一新 scaffold。
  建成 `dev_new` 80 / `holdout_new` 60 / `external_audit_panel` 40（储备 265）。对 round 1 全部划分
  SMILES 与 scaffold 泄漏均为 0；近邻 Tanimoto 中位 0.251、最大 0.640、>0.7 的 0 个。
  **`ood_probe` 未构建**：剩余池与 round 1 同分布，从中抽样只是更多同分布数据。
- **A5a 多角色配置**：新增 `[roles]` 与 `model_for_role()` / `with_roles()`，角色解析结果写进
  manifest，G=JG 的自评运行在协议哈希上不可能与 G≠JG 混淆。round-1 配置载荷经验证逐字节未变。
- **A5b judge_v3**：结构化 JSON 输出（致命标记 / error_tags / decisive_step），12 对真实路线
  12/12 解析成功且全部结构化，成本仅比 judge_v2 高 12%。解析失败与化学无效严格分开记录。

**重要：round 1 协议哈希已冻结。** `build_manifest` 把整个 `src/retro_e` 树哈希进协议哈希，因此 v2
的任何代码改动都会永久破坏 round 1 记录哈希的可复现性（即 final_report §8 承诺的东西）。已在动代码前
用 `scripts/freeze_round1_protocol.py --mode freeze` 逐字节快照到 `results/manifests/round1_frozen/`。
两个**确证性**阶段（Stage 5 主实验 `cb9d6f…`、跨 judge `b501f1…`）各 22/22 完整保住；
早期非确证阶段（pilot / h1_screen / optimization）在本次冻结之前就已因 round 1 期间代码继续演进而
部分漂移，如实记录在 `round1_frozen/FROZEN_FILES.json`。随时可用 `--mode verify` 复核。

**E1 已实现（2026-09-06 当日追加）：** `optimize.py` 新增 `propose_candidate()` 分发器，支持
`reflective` / `search_only` / `instruction_opt` 三个分支；`scripts/run_e1_matrix.py` 实现 16→4→2
调度（每次运行 560 生成 + 560 评判，9 次运行约 5,040 次）。三方法共享 target 子集、父代选择规则和
800-token 绝对长度上限，唯一差别是改写器能看到什么。实测暴露并修掉了三个问题（长度校准、化学词表
子串假阳性、强制标题行导致越界率恒为 100%）——详见 `reports/v2/experiment_registry.md`。

**生成端开销实测：** `gemini-3.8-flash-high` 每次生成烧约 1.6 万隐藏推理 token（3.7 的 4 倍，
总 token 2.5 倍），延迟 60–70 秒。开销不构成约束，但 E1 约 6,800 次生成按 8 并发约需 15 小时纯生成。

**2026-09-06 晚：smoke run 已端到端跑通，代码冻结。** 最终 v2 optimization 协议哈希
`c7a4001867a5a92b6385c3460de4e1fc3a0a3ea3ecac3fd71825eb24da3b8b7e`，九次 E1 运行全部使用它。
smoke 过程中抓到三个不会崩溃、也不会被测试或 lint 发现的 bug（CLI 绕过角色导致用错模型、
optimizer 用旧解析器读不了 judge_v3、judge 输出约 1% 截断），全部已修并加了回归测试，
详见 `reports/v2/experiment_registry.md`。

**并发实测：** 40 并发下 `gemini-3.8` 单次生成约 52 秒、有效并发约 12（网关对 3.8 限流明显紧于
3.7），400 次 baseline 约 18 分钟。E1 的约 6,800 次生成据此外推约 5 小时。

**九次 E1 运行的启动方式：**

```bash
uv run python scripts/run_e1_matrix.py --method {reflective,search_only,instruction_opt} \
  --seed {11,29,47} --protocol-hash c7a4001867a5a92b6385c3460de4e1fc3a0a3ea3ecac3fd71825eb24da3b8b7e \
  --workers 40
```

**仍缺（不阻塞 E1）：** `retro-e generate --condition` 硬编码为 round-1 四个条件，阻塞计划 §6.3
的 dev 评估（需加 `--context-file` 或单独脚本）；§6.3 dev 评估脚本未写。E3 迁移与 E4 模块消融
不依赖 E1，`dev_new` 已就绪即可开跑。

## 11. 快速完整性检查

```bash
uv run pytest
uv run ruff check src scripts

wc -l \
  results/generations/h1_screen_baseline.jsonl \
  results/generations/h1_screen_E0.jsonl \
  results/judgments/h1_screen_E0_vs_baseline.jsonl

uv run retro-e audit-targets \
  data/processed/train_targets.jsonl \
  data/processed/test_targets.jsonl
```

暂停时的预期行数为：90、90、270。三个文件现已达到该数量。

v2 追加检查（应全部通过，`passed: true` 且 0 泄漏；快照校验只应报告工作树漂移，不应报告快照损坏）：

```bash
uv run retro-e audit-targets \
  data/processed/train_targets.jsonl \
  data/processed/test_targets.jsonl \
  data/v2/dev_new_targets.jsonl \
  data/v2/holdout_new_targets.jsonl

uv run python scripts/freeze_round1_protocol.py --mode verify
```
