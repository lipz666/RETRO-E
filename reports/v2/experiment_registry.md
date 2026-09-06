# RETRO-E v2_pilot 实验登记表

> 计划 v2 §15 要求的 `experiment_registry.md`：登记本轮**所有**尝试、变更、未完成项和负结果，
> 不只保留成功的部分。按时间追加，不覆盖历史条目。
>
> **这是历史记录，不是状态看板。** 当前状态看 [`docs/STATUS.md`](../../docs/STATUS.md)，
> 实时数字跑 `uv run python scripts/status.py`。

**当前状态：** 工作包 A（资产与接口）部分完成。尚未开始任何 v2 优化运行或路线生成。

---

## 2026-09-06 — 工作包 A：启动校准、v2 数据划分、多角色与结构化评判

### A6 启动校准（计划 §3.1，已完成）

在真实 Stage-5 路线对和真实 judge prompt 上做了 107 次实测调用，**没有生成任何新路线**。
完整记录：`results/v2/calibration/startup_calibration.json`，原始逐次数据
`results/v2/calibration/judge_cost_probe_raw.json`，可重跑脚本 `scripts/v2_calibrate.py --probe cost|order|judge_v3`。

| 候选 judge | 隐藏推理 tok | 总 tok/次 | 延迟 | 解析 | 顺序翻转一致性 | A-rate |
|---|---:|---:|---:|---:|---:|---:|
| gemini-3.8-flash-high | 2869 | 5370 | 16.2s | 5/5 | **90%** | 55% |
| gemini-3.7-flash-high | **638** | 3139 | 7.2s | 5/5 | 80% | 60% |
| gemini-3.6-flash-high | 2067 | 4569 | 7.4s | 5/5 | — | — |
| claude-haiku-4-5 | **0** | 3268 | **2.1s** | 5/5 | **40%** | **75%** |
| gpt-5.5 | 1059 | 3938 | 22.3s | 5/5 | — | — |
| gpt-5.6-luna | 1213 | 4091 | 24.7s | 5/5 | — | — |
| gpt-5.4-mini | 4160 | 7038 | 61.2s | 5/5 | — | — |

三个改变计划假设的结论：

1. **「3.7 和 3.8 同价」在这个任务上不成立。** 单 token 同价，但 3.8 做 judge 每次烧 2869 个隐藏
   推理 token，是 3.7 的 4.5 倍，总 token 甚至高于 gpt-5.6-luna。因此 3.8 分配给 **generation**，
   3.7 分配给 **judge**。
2. **不存在便宜的异家族 judge。** claude-haiku-4-5 零推理 token、2.1 秒、100% 解析，看上去正是
   round 1 报告建议里要找的模型——但 10 对路线正反各判一次只有 **40% 前后一致**、A-rate 75%，
   它主要在读位置而不是读化学，**已否决**。网关上其余非 Gemini 候选全是推理模型。
3. **异家族预算买得起。** 计划 §3.2 的 600 次外部调用 × ~4.1k tok ≈ **250 万 token**，是 round 1
   逼停 `gpt-5.6-sol` 那笔（约 2600 万）的 1/10。**用户已决定保留该预算**，避免重蹈 round 1 自评
   导致 H1 崩塌的覆辙。

**锁定的角色分配**（`config/v2_pilot.toml` 的 `[roles]`，环境变量在 `.env`）：

| 角色 | 模型 |
|---|---|
| G 路线生成 | `gemini-3.8-flash-high` |
| M 反思与变异 | `gemini-3.8-flash-high` |
| JG 训练/开发评估 | `gemini-3.7-flash-high` |
| GX / JX 异家族 | `gpt-5.6-luna`（600 次上限，角色重叠已披露） |

**遗留未解项：** 3.8 在**生成端**的推理开销尚未实测。它在 judge 任务上是 3.7 的 4.5 倍，
所以 E1 的生成预算有可能显著高于 round 1 的每次约 8k token。E1 开跑前应先量。

### A1 v2 数据划分（计划 §4.1，已完成）

`scripts/build_v2_splits.py` → `data/v2/`，审计报告 `data/v2/splits.json`。

做法：用 round 1 的种子 `20260902` **逐位重放**它的 500 个 scaffold 选择，剔除，再用新种子
`20260906` 从剩下的池子里抽 —— 因此新 target 与 round 1 的 scaffold 不相交是构造保证的，
不是事后过滤出来的。

| 划分 | 数量 | 作用 |
|---|---:|---|
| `dev_new` | 80 | v2 方法筛查、模块干预（E4）；允许反复查看 |
| `holdout_new` | 60 | 本轮最终冻结比较，只揭盲一次 |
| `external_audit_panel` | 40 | `dev_new` 的子集，留给非 Gemini 复核与专家抽样 |
| `ood_probe` | **0，未构建** | 见下 |
| 储备 | 265 | 未抽取的唯一 scaffold |

审计结果：967 个合格候选中 round 1 用掉 500 个 scaffold，剩 **405 个唯一新 scaffold**（需要 140）。
对 round 1 全部划分：canonical SMILES 泄漏 **0**、Murcko scaffold 重叠 **0**、v2 内部 scaffold
重复 **0**。项目自带的 `retro-e audit-targets` 在 440 个 target 上复核通过。

**近邻相似度审计**（计划 §4.1 新增要求，Morgan r=2/2048 位 Tanimoto，每个新 target 对全部 500 个
round-1 target 取最大值）：中位数 0.251，p90 0.326，最大 **0.640**，>0.7 的 **0 个**，>0.5 的 4 个。
按计划要求**如实报告分布，没有事后删除最近的那几对来美化结果**。

**`ood_probe` 未构建，理由是负结果而不是省事**：剩余池与 round 1 抽取的池分布几乎完全一致
（重原子中位数 30 vs 31，路线深度中位数 5 vs 5，范围也基本重合）。从这里抽 30 个只是更多同分布
数据被贴上「分布外」标签。真正的 OOD probe 需要另一个来源（PaRoutes n5，或非专利路线），
并且要自己声明 OOD 的定义。

`external_audit_panel` 的抽取按预声明分层：重原子三分位 × 参考路线深度中位数，种子 `20260907`，
**在任何一次 v2 API 调用之前完成**，与任何方法的胜负无关。

### A5a 多角色模型配置（已完成）

round 1 只有两个环境变量、两个角色，表达不了 G≠JG，也表达不了独立的 mutator / 外部 judge。
新增 `config.RolesConfig` 与 `ExperimentConfig.model_for_role()` / `with_roles()`：角色在配置里声明，
运行时解析成具体模型 ID，并写进 manifest 的 `resolved_models`，所以**一次 G=3.8/JG=3.7 的运行
在协议哈希上永远不会与自评运行混淆**。

向后兼容：`config/experiment.toml` 不含 `[roles]`，`public_dict()` 会略去所有未设置的新字段，
经验证 round-1 配置载荷与 `final-cb9d6f…` manifest 中记录的**逐字节一致**。

### A5b judge_v3 结构化输出（已完成）

`prompts/judge_v3.txt` + `judge.parse_judgment()`。judge 先独立判定两条路线各自是否存在致命化学
问题，再做整体比较；输出计划 §5.1 规定的 JSON（`decision` / `insufficient_information` /
`main_reason` / `fatal_A` / `fatal_B` / `error_tags` / `decisive_step`）。

12 对真实路线实测：**12/12 解析成功，12/12 结构化**，未知 tag 0 个，11/12 给出了决定性步骤，
2/12 触发致命标记。成本 3526 tok/次，比 judge_v2 的 3139 只高 12%。

两条按计划 §15 要求保持可区分的边界：
- **解析失败 ≠ 化学无效。** JSON 坏掉但能捞出 DECISION 的回复，`decision` 保留、`structured=false`、
  `parse_error` 记录原因，绝不当成化学发现。
- **decision-only 的回复不等于「judge 认为没有致命问题」。** 退化回复的 `fatal_A/fatal_B` 是
  `None` 而不是 `false`；预声明有效性规则（`predeclared_validity_rule`）产生的判定也是 `None`，
  它是操作层裁定，不是化学意见，不进 §5.3 的行为指标。

### 附带处理：round 1 协议哈希的冻结（非计划内，必须做）

发现 `build_manifest` 把整个 `src/retro_e` 树哈希进协议哈希，因此 **v2 的任何代码改动都会永久
破坏 round 1 记录哈希的可复现性** —— 那正是 `reports/final_report_zh.md` §8 明确承诺的东西。

处理：`scripts/freeze_round1_protocol.py --mode freeze` 在动任何代码前，把每个 manifest 各自的
文件逐字节快照到 `results/manifests/round1_frozen/<manifest>/`。之后随时
`--mode verify` 复核。

结果（如实记录，含已经无法挽回的部分）：

| Stage | 文件恢复 | 状态 |
|---|---:|---|
| `final-cb9d6f…`（Stage 5 主实验 + control） | 22/22 | **完整冻结** |
| `final-b501f1…`（跨 judge 检验） | 22/22 | **完整冻结** |
| `optimization-ec50b6…`（产出 E\*） | 22/23 | `optimize.py` 已丢失 |
| `optimization-f8fb55…` | 20/23 | 3 个文件已丢失 |
| `h1_screen-6040c0…` | 14/20 | 6 个文件已丢失 |
| `pilot-29a029…` | 13/20 | 7 个文件已丢失 |

两个**确证性**阶段完整保住了。早期非确证阶段在本次冻结之前就已经漂移——round 1 期间代码在这些
阶段跑完之后继续演进，这是既存事实，不是本轮造成的。它们的逐文件哈希仍在 `results/manifests/`
里，可用于校验单个文件，但聚合协议哈希已无法从任何本地副本重新导出。

`schemas/target.schema.json` 保持 round-1 原样（`split` 枚举仍是三个值）。v2 的
`dev_new` / `holdout_new` 枚举放在独立文件，避免污染 round-1 资产。

**已实测的一条限制：聚合协议哈希是路径相关的。** `build_manifest` 把 `config.public_dict()`
（其中含仓库根的绝对路径）哈希在内。把快照复制到另一个目录后重建，即使文件逐字节相同，得到的
聚合哈希也不同（实测 `2de5ee…` vs 记录的 `cb9d6f…`）。因此重新导出聚合哈希必须把快照**就地
还原到同一绝对路径**；逐文件 sha256 校验则与路径无关，`--mode verify` 做的就是后者。

### 与计划的偏离（全部登记）

1. **配置文件用 TOML 不用 YAML。** 计划 §15 写的是 `configs/v2_pilot.yaml`；本项目的加载器读 TOML，
   因此实际是 `config/v2_pilot.toml`，字段名未改。
2. **`ood_probe` 未构建。** 理由见 A1；这是一个有依据的负结果，不是跳过。
3. **顺序偏倚是用 judge_v2 测的**，而 v2 将使用 judge_v3。需要在预声明的 10% 顺序翻转复核里
   用 judge_v3 重测。

### 尚未开始

- **E1 核心矩阵**（3 方法 × 3 seed）：`search_only` 与 `instruction_opt` 两个分支尚未实现；
  现有 `optimize.py` 只有 reflective 一条路径，且调度是 round 1 的 24→6→2，需改成 16→4→2。
- E2 学习曲线、E5 各分支：按计划须等 E1 的 §6.4 门槛。
- E3 迁移、E4 模块消融：不依赖 E1（都用冻结的 legacy E\*），`dev_new` 就绪后即可开跑。
- 专家审计、专用模型探针：未开始。

---

## 2026-09-06（续）— E1 三方法分支与 16→4→2 调度实现完成

### 生成端开销实测（补上之前的遗留未解项）

10 个 dev_new target × baseline/E0 两条件，`gemini-3.8-flash-high`：

| 条件 | 隐藏推理 tok | 可见输出 | 总 tok/次 | 延迟 | 有效率 |
|---|---:|---:|---:|---:|---:|
| baseline | 15,875 | 1,284 | 18,599 | 69.6s | 10/10 |
| E0 | 16,669 | 1,403 | 20,244 | 60.4s | 10/10 |
| *round-1 `gemini-3.7` 参考* | *4,135* | — | *6,879–8,784* | — | — |

3.8 在生成端每次烧约 1.6 万隐藏推理 token，**是 3.7 的 4 倍，总 token 2.5 倍**。用户已明确 Gemini
开销不构成约束，因此保留 3.8 作为 generator。但**延迟**是实际约束：E1 的约 6,800 次生成按 8 并发
估算约 15 小时纯生成时间，加评判后更多。生成有效率 20/20，无格式问题。

### E1 三个方法分支（计划 §6.1）

`src/retro_e/optimize.py` 新增 `propose_candidate()` 分发器与 `MutationProposal`，三个算子共用
同一评估 harness、同一 target 子集、同一父代选择规则（当前最优 reward）和同一层级截断——
**唯一的差别是改写器被允许看到什么**：

| ID | 分支 | 改写器看到 | 备注 |
|---|---|---|---|
| R | `reflective` | 真实失败/打平案例的策略级摘要 | round 1 方法，`propose_mutation` 原样复用 |
| S | `search_only` | **什么都看不到**——无 target、无路线、无失败描述、无 reward | 只给一条内容无关的结构化改写指令（8 条固定列表轮换）+ 温度 0.9 |
| I | `instruction_opt` | 与 R 完全相同的反馈 | 但输出被限制为通用规划/检查/执行指令，禁止任何领域词汇 |

S 仍然通过层级筛选间接使用 reward——计划 §6.1 明确允许，并要求不得宣称它"完全不学习"；它检验的是
**反思反馈是否超越无向候选搜索**。父代选择规则对三者一致，所以 R 是"有信息的爬山"、S 是"盲变异的
爬山"，对比干净。

### 三次实测暴露并修掉的问题（新 prompt 必须先在真实模型上验证——judge_v2 的教训）

1. **两个分支都超长度上限。** 首次实测 838 和 1,083 locked token，上限 800。把"目标≈上限"改成
   纯上限后又**严重欠长**（469 token，父代 E0 是 736）——系统性变短会混淆方法间比较。最终改为
   同时给目标（0.92×上限）和硬上限，实测落在 689 token。runner 仍独立强制硬上限。
2. **化学词表用子串匹配产生假阳性。** `check` 里含 `heck`（named reaction），被误判为越界。
   改为词边界正则。
3. **强制标题行 `SYNTHETIC EXPERIENCE` 让每个候选都必然命中 `synthetic`。** 标题是格式要求不是
   改写器的选择，审计已排除标题行——否则越界率恒为 100%，指标失去意义。

审计现在分两个列表报告，**不合并计数**：`CHEMISTRY_SPECIFIC_TERMS`（无歧义领域词汇，决定
`boundary_respected`）与 `AMBIGUOUS_STRATEGY_TERMS`（`convergent`、`intermediate`、`reduction`
等在通用规划语境下也成立的词，单独报告）。修完之后 instruction_opt 实测
`boundary_respected=True`，只剩歧义词——这是诚实的结果，不是被修饰过的。

按计划 §6.1 要求，这套审计是**词法代理**，无法证明语义合规；越界率非 0 时报告"控制不完美"，
不强行修补，也不因此宣称领域特异性结论。

### 16→4→2 调度（计划 §6.2）

`scripts/run_e1_matrix.py`，一次调用跑一个 (method, seed)：

| 层 | 候选 | Target | 样本 × 票 |
|---|---:|---|---|
| screen | 16（含 E0 种子） | 20 个 optimizer target（按 seed 抽，与方法无关） | 1 × 1 |
| advance | screen 前 4 | 另外 40 个 optimizer target（与 screen 不相交） | 1 × 1 |
| selection | advance 前 2 | 40 个 selection target | 1 × 1 |

每次运行 **560 次生成 + 560 次评判**（有单元测试锁死这个算术），9 次运行共约 5,040 次。
Target 子集只依赖 seed 不依赖 method，所以同一 seed 下 R/S/I 在**共同 target、共同 baseline
样本**上比较。赢家只按 selection reward 冻结，`dev_new` 不参与挑选。

长度上限改为**三方法共享的 800 token 绝对值**，而不是 round 1 的"E0 的 110%"——避免某个方法靠
被允许写更多字取胜。每个候选仍逐字扫描训练/selection target 的 SMILES 与来源标识符；超限或泄漏的
候选被拒绝并记账（`rejected` 字段），不静默丢弃。压缩兜底对三方法逻辑相同，但**触发次数本身是
方法差异**，因此 ledger 单独记录 `compression_passes`。

Ledger 每行记录：method、optimizer_seed、tier、candidate_id、parent_id、mutation_rationale、
`saw_task_feedback`、`method_audit`、text、locked_tokens、target_batch、reward、胜负平、
rejected、compression_passes、api_usage、generator/judge model、protocol hash。

### 已验证

43 个单元测试通过，`ruff check src scripts tests` 全清。v2 manifest 可正常生成（
`retro-e --config config/v2_pilot.toml manifest --stage optimization` 通过 preflight，
输出到 `results/v2/manifests/`）；生成的临时 hash 已删除，**正式 hash 必须在代码冻结后重新铸造**。
runner 在缺 baseline 缓存时报出明确的可执行错误而不是继续跑。

### 正式开跑前仍缺的东西

1. **v2 baseline 缓存必须用 `gemini-3.8` 重新生成。** round 1 的
   `results/generations/{optimizer,selection}_pool_baseline.jsonl` 是 `gemini-3.7` 产物，
   不能复用。需要 160 + 40 = 200 次生成（约 27 分钟 @ 8 并发）。
2. **冻结代码后铸造正式 protocol hash**，9 次运行全部使用同一个。
3. **`retro-e generate --condition` 目前硬编码为 round-1 的四个条件**，无法用任意 winner context
   生成。这**不阻塞 E1 的九次优化运行**（runner 内部走 `compose_generation_prompt_from_text`），
   但阻塞计划 §6.3 的 dev 评估（11 个条件 × 80 dev target × 2 路线）。需要加 `--context-file`
   选项或单独写 dev 评估脚本。
4. **§6.3 dev 评估脚本尚未编写**（1,760 次生成、1,920 对判断）。
5. E1 runner 尚未端到端跑过任何一层真实数据。建议先用 1 个 method × 1 seed 跑通 screen 层的
   前几个候选再全量铺开。

---

## 2026-09-06（续 2）— 代码冻结、baseline 生成，以及一次被抓住的严重错误

### 事故：整批 baseline 用错了模型（已作废重跑）

第一次生成的 400 条 v2 baseline（optimizer 320 + selection 80）**全部由 `gemini-3.7-flash`
产出，而不是锁定的 `gemini-3.8-flash-high`**。

原因：`retro-e generate` 的 CLI 从未调用 `with_roles()`。`load_config()` 之后
`config.generator_model` 仍然回退读取 round-1 的 `RETRO_E_GENERATOR_MODEL` 环境变量
（`gemini-3.7-flash-high`）。A5a 加的角色机制只在我自己写的脚本里生效，CLI 完全绕过了它。

**发现方式**：不是靠 smoke run，而是生成完成后核对数据——`response_model` 全是
`gemini-3.7-flash`，平均隐藏推理 token 4,338（3.8 应该是约 1.6–1.9 万）。当时的 manifest 其实
已经把矛盾如实记下来了：

```
"generator": "gemini-3.7-flash-high",      <- 环境变量回退，实际使用的
"role:generator": "gemini-3.8-flash-high"  <- 声明的
```

manifest 暴露了矛盾，但**没有任何东西强制两者一致**。

**处理**：
1. `src/retro_e/cli.py` 现在在任何命令执行前，只要 config 声明了 `[roles]` 就绑定角色。
2. 加了回归测试 `test_cli_binds_roles_so_it_cannot_fall_back_to_round1_env`。
3. 400 条错误 baseline、对应 manifest、以及被中断的 smoke run 目录**全部删除**，不保留、不复用。
4. 协议哈希重新铸造（`7aa453d0…` 作废 → `fd81a5da…`），`resolved_models` 现在
   `generator = gemini-3.8-flash-high`，与角色声明一致。
5. 重跑前先用 5 个 target 的小批量验证 `response_model`，确认是 `gemini-3.8-flash`、
   隐藏推理 18,805 token，才铺全量。

**教训**：新增一层配置抽象（角色）时，必须检查**所有**入口是否都走了这一层。旧的入口不会报错，
它只会安静地用旧路径。

### 并发 40 实测

| 指标 | 实测 |
|---|---|
| 有效并发 | 38–44（设定 40，达成） |
| 单次生成延迟 | 均值 17.8s，p90 26.7s，max 40.1s |
| 吞吐 | 124–152 次/分钟 |
| API 失败 | 0 |
| 生成有效率 | 94.4%（round-1 baseline 为 95.0%，一致） |

**这推翻了之前的时间估算。** 之前用 6 并发的探针测得单次 60–70 秒，据此估 E1 约 15 小时。
40 并发下单次延迟反而降到 17.8 秒，**E1 的约 6,800 次生成按此吞吐约需 50 分钟**，不是 15 小时。
低并发下的单次延迟不能外推到高并发场景。

### 代码冻结与协议哈希

冻结后的 v2 optimization 协议哈希：**`fd81a5daf04e36a4afa9091d805a798a043d9a764054b1f57493cec42b0b7809`**
（`results/v2/manifests/`）。九次 E1 运行必须全部使用这一个。

顺带修掉的一个 manifest 缺陷：`_manifest_files` 原本把 `config/experiment.toml` 写死，导致 v2
的 manifest 去哈希 round-1 的配置文件——改 `config/v2_pilot.toml`（角色、每对票数、采样参数）
**不会改变协议哈希**，正是 manifest 机制要防的静默漂移。现在哈希的是实际使用的配置文件，
已确认 `config/v2_pilot.toml` 出现在哈希文件清单中。

**已知未覆盖：协议哈希只包含 `src/retro_e/*.py`，不含 `scripts/`。** 因此 `run_e1_matrix.py`
本身不在协议哈希内（round 1 的 `optimize_e_star.py` 同样如此）。调度参数由 ledger 每行记录，
但脚本改动不会反映在哈希上——这是一个已披露的 provenance 缺口。

### runner 的 smoke 隔离

`--run-tag` 与 `--max-screen-candidates`。smoke run 必须用 `--run-tag`：否则它写进正式运行的
同一目录，而 advance/selection 层的行会被断点续跑当作已完成——某个进了 smoke 前 4 名但进不了
真实前 4 名的候选，会留下一行陈旧记录被 selection 层参与排序。`promotion.json` 会记录
`full_schedule: false` 并在 note 里注明「不是有效的 E1 结果」。

`freeze_round1_protocol.py` 也修了一个同类问题：工作树漂移之后重跑 freeze 会把已有的好快照
记账降级（22/22 → 18/22）。现在优先信任已有快照副本，只在快照缺文件时才从工作树取，可重复执行。

---

## 2026-09-06（续 3）— smoke run 抓到的第二个严重 bug

### 事故：optimizer 的判定路径解析不了 judge_v3，19/20 判定静默失败

第一次 smoke run（reflective / seed 11）的 E0 种子候选产出：

```
screen  183f9018fdae2555  reward=1.000  pairs=1  w/l/t=1/0/0
```

20 条生成全部成功，**只有 1 条判定落盘**，而 ledger 记下了一个基于单对配对的 `reward=1.000`。

**根因**：`src/retro_e/optimize.py` 的 `judge_candidate_vs_baseline()` 一直 import 的是
`parse_decision`。A5b 加 judge_v3 时只改了 `src/retro_e/judge.py` 的主评判路径
（`_judge_one`），optimizer 有一条**独立的**评判实现，被漏掉了。judge_v3 输出 JSON，而
`_DECISION_ANCHOR` 正则要求 `decision` 后紧跟 `:`，JSON 里是 `"decision":`，中间隔着引号，
匹配不上 → 抛 `JudgmentError` → 被 `evaluate_candidate` 的 `except Exception` 吞掉并只加到
一个从未被读取的 `judgment_failures` 计数上。

这是与前一次「CLI 绕过角色机制」**完全同类**的错误：新增一层能力时只改了主路径，第二条并行
路径安静地继续用旧行为。

**三层修复**：

1. optimizer 改用 `parse_judgment`，判定记录新增 `judge_prompt_version` 与
   `structured_verdict`——此前 optimizer 产出的判定完全没有计划 §5.3 的行为字段。
2. 新增 `IncompleteEvaluationError`：实际裁定的配对数少于计划数时**拒绝计算 reward 并抛错**。
   缺失的配对不是随机缺失的（解析失败与路线内容相关），基于部分批次算出的 reward 比没有 reward
   更有害。`evaluate_candidate` 是幂等可续跑的，所以正确处置是重跑而不是接受残缺结果。
3. ledger 每行新增 `expected_pairs` / `generation_failures` / `judgment_failures`。此前这
   19 次失败在 ledger 里完全不可见。

回归测试两条，其中一条直接对 `optimize.py` 源码断言不得再出现 `parse_decision`。

**修复后重跑**（协议哈希重铸 `fd81a5da…` → `3fb5fb19…`）：

```
screen  183f9018fdae2555  reward=0.550  pairs=20/20  w/l/t=11/9/0  genfail=0  judgefail=0
```

E0 种子在 20 个 optimizer target 上 reward 0.550，与 round 1 tier1 的 E0 种子 0.525 高度一致。

### 关于「先跑 smoke」这个决定

这两个 bug（用错模型、判定解析失败）都**不会**被单元测试、lint 或类型检查发现，也都不会
让程序崩溃——它们只会安静地产出看起来合理的错误数字。如果直接铺开九次运行，第一个会让全部
5,040 次生成用错模型，第二个会让每个候选的 reward 建立在约 5% 的配对上，而且两者在 ledger
里都不留下明显痕迹。

代价是两次 smoke run 加约 400 次作废的 baseline 生成。

---

## 2026-09-06（续 4）— smoke run 端到端跑通，第三个 bug，最终冻结

### smoke run 完整结果（reflective / seed 11 / 3 个候选槽位）

三层全部跑通：screen → advance → selection → winner 冻结。

| 层 | 候选 | reward | 配对 | 胜/负/平 | tokens | 见反馈 | 压缩 | 父代 |
|---|---|---:|---:|---|---:|---|---:|---|
| screen | `183f9018`（E0 种子） | 0.550 | 20/20 | 11/9/0 | 736 | — | 0 | — |
| screen | `37ff85ec` | 0.450 | 20/20 | 8/10/2 | 750 | ✓ | 1 | E0 |
| screen | `8ac00337` | 0.600 | 20/20 | 11/7/2 | 700 | ✓ | 1 | E0 |
| advance | `183f9018` | 0.662 | 40/40 | — | 736 | — | — | — |
| advance | `37ff85ec` | 0.475 | 40/40 | — | 750 | ✓ | — | E0 |
| selection | `183f9018` | 0.525 | 40/40 | — | 736 | — | — | — |
| selection | `8ac00337` | **0.650** | 40/40 | — | 700 | ✓ | — | E0 |

爬山逻辑正确：候选 2 的 0.450 低于 E0 的 0.550，因此候选 3 仍从 E0 派生而不是从更差的候选。
E0 种子在 screen 上 0.550，与 round 1 tier1 的 0.525 高度一致。所有候选都在 800 token 上限内。

**这是 smoke run，不是有效的 E1 结果**：只跑了 16 个槽位中的 3 个，`promotion.json` 记录
`full_schedule: false`。产物已全部删除。

### judge_v3 在 optimizer 路径上的验证（242 次判定）

- LLM 判定 **全部结构化，零解析错误**
- `predeclared_validity_rule`（无效路线）正确地不携带任何化学字段
- error tag 分布：chemoselectivity_conflict 22、infeasible_key_transformation 20、
  structural_or_stereochemistry_omission 17、unnecessary_protection_loop 6、
  starting_material_availability_assumption 3、graph_connectivity_error 2、missing_step 1
- 致命标记 23/93；决定分布 A 44 / B 47 / Tie 8，无明显位置偏倚
- 可见 completion token：均值 88，p90 103，**max 121**

### 事故三：judge_v3 输出被截断（约 1% 发生率）

advance 层曾因「1 次判定失败」中止。看堆栈才发现**不是瞬时故障**：

```
JudgmentError: '```json\n{"decision":"A",...,"main_reason":"Route A constructs the 1,2,4
```

响应在 `main_reason` 中途断掉，没有闭合括号 → `_JSON_OBJECT` 的 `\{.*\}` 匹配不到 →
退回 `parse_decision` → 没有 DECISION 锚点 → 抛错。

与 round 1 judge_v1 那次 50% 解析失败同一类问题（模型输出被 `max_tokens` 截断），只是这次
发生率约 1%，A5b 阶段 12 对的抽检完全没碰上——**只有跑到几百次判定的量级才暴露**。

成功判定最多只用 121 个可见 token，远不到 600 的上限；但隐藏推理最高到 4,081，会吃掉输出预算。

**两处修复**：
1. `parse_judgment` 新增截断打捞：用正则从残缺 JSON 里取出 `"decision"`。
   `structured` 保持 `False`——截断点之后的字段确实缺失，缺失的 `fatal` 标记绝不能被读成
   「judge 认为没有致命问题」。`parse_error` 记为 `truncated_json_decision_salvaged`。
2. judge `max_tokens` 600 → 2500。

### runner 的有界重试

`evaluate_candidate` 的完整性 guard 是对的，但九次运行不能因为掉一个请求就整个死掉。
runner 的 `evaluate()` 现在最多重试 4 次，每次断点续跑只补缺失项；**guard 语义不放松**——
重试用尽仍不完整就大声失败，绝不在残缺批次上打分。

### 最终冻结状态

- 测试 48 个通过，`ruff check src scripts tests` 全清
- **最终 v2 optimization 协议哈希：`c7a4001867a5a92b6385c3460de4e1fc3a0a3ea3ecac3fd71825eb24da3b8b7e`**
- baseline 在此哈希下重新生成（前两版分别挂在 `7aa453d0…` 和 `fd81a5da…`，已全部删除）
- 九次 E1 运行必须全部使用这一个哈希

### 三次事故的共同模式

| # | 事故 | 是否会崩 | 是否被测试/lint 抓到 | 暴露它需要 |
|---|---|---|---|---|
| 1 | CLI 绕过角色，用错模型 | 否 | 否 | 核对生成记录的 `response_model` |
| 2 | optimizer 用 `parse_decision` 解析 judge_v3 | 否 | 否 | 跑一次真实评估并核对配对数 |
| 3 | judge 输出截断 | 是（但仅约 1%） | 否 | 几百次判定的量级 |

前两个都是**加一层新能力时只改了主路径，第二条并行路径安静地继续用旧行为**。三个都不会被
单元测试、lint 或类型检查发现；1 和 2 甚至不会让程序崩溃，只会产出看起来合理的错误数字。

如果直接铺开九次运行：事故 1 会让全部 5,040 次生成用错模型；事故 2 会让每个候选的 reward
建立在约 5% 的配对上；两者在 ledger 里都不留明显痕迹。代价是两次 smoke run 加约 800 次
作废的 baseline 生成。

---

## 2026-09-06（续 5）— P0 补做：round-1 复核（计划 §2.1）

此前直接从工作包 A 的基础设施跳到了 P1 的 E1 执行，**跳过了 §2.1 要求的四项 round-1 复核**。
本节补做。脚本 `scripts/p0_round1_reanalysis.py`，产物 `results/v2/p0_round1_reanalysis.json`
与 `reports/v2/e0_estar_semantic_diff.md`。round-1 文件全部只读，未改动。

### 检查 1：复算胜负平

从原始判定记录重新聚合（多数票 → 映射回来源条件），与 `final_primary_metrics.json`、
`final_control_metrics.json`、`final_lunajudge_metrics.json` 逐一核对：

**7 个对比全部逐字一致。** round-1 记录的计数没有问题。

### 检查 2：以 target 为统计单位

round-1 的精确符号检验在 300 个 route pair 上进行，把同一 target 的 3 个样本当成独立观测。
补做 target 层面的整簇符号翻转检验（target 为可交换单位，10,000 次置换）：

| Judge | 对比 | 逐对符号检验 p | **target 符号翻转 p** | target 正/负/零 |
|---|---|---:|---:|---|
| gemini | E0 vs baseline | 0.0431 | **0.0352** | 53/36/11 |
| gemini | E\* vs E0 | 0.0118 | **0.0127** | 56/34/10 |
| gemini | E\* vs baseline | <0.0001 | **0.0001** | 62/29/9 |
| gemini | E\* vs control | 0.0109 | **0.0177** | 55/36/9 |
| luna | E0 vs baseline | 0.6801 | **0.6667** | 49/46/5 |
| luna | E\* vs E0 | 0.0227 | **0.0144** | 56/36/8 |
| luna | E\* vs baseline | 0.0052 | **0.0046** | 57/41/2 |

**结论不变。** 正确的 target 级检验与原来的逐对检验给出几乎相同的 p 值，round-1 的显著性
判定在这个维度上是稳健的——但这是运气好，不是设计对：原始检验确实用错了单位。

### 检查 3：两个 judge 的配对效应差值 —— **实质性发现**

计划 §2.1 明确警告：*「不能由『一个显著、一个不显著』推出 judge 差异显著」*。
round-1 报告的核心叙事恰恰就是这么推的。直接检验（同一批路线，按 target 配对求差值）：

| 对比 | gemini | luna | **差值** | **差值 cluster 95% CI** | 符号翻转 p | 两 judge 逐对一致率 |
|---|---:|---:|---:|---:|---:|---:|
| E0 vs baseline (H1) | +11.7% | +2.7% | **+9.0%** | **[−1.7%, +20.3%]** | **0.127** | 69.7% |
| E\* vs E0 (H2) | +14.0% | +13.0% | +1.0% | [−9.7%, +12.0%] | 0.900 | 70.7% |
| E\* vs baseline | +23.3% | +16.0% | +7.3% | [−2.7%, +17.7%] | 0.180 | 73.7% |

**round-1 报告的中心论断没有通过直接检验。** 该报告称「自评实质性地虚高了 H1，却没有影响
H2」，并把这称为「这个项目最重要的发现」。但两个 judge 在 H1 上的效应差值是 +9.0 个百分点，
**置信区间跨过 0**（p = 0.127）——无法与「两个 judge 没有差异」区分开。

正确的表述是：H1 在 gemini 下显著、在 luna 下不显著，**但没有证据表明这两个 judge 对 H1 的
评判存在系统性差异**。观察到的落差与抽样噪声相容。H1 结论仍应是「不确定」（原报告的结论
正确），但**理由不是「已测得自评偏差」，而是「两个 judge 下结果不一致且都不够有力」**。

附带发现：两个 judge 在**单条路线对**上的一致率只有 69.7–73.7%，即约 30% 的配对判断相反。
这个量级的噪声本身就足以解释上表中的效应落差。

**处理**：`reports/final_report_zh.md` 需要按此修订第 5.4 节、第 6 节与限制 1 的表述。
这不改变任何数字，只改变从数字推出的因果叙述。

### 检查 4：E0/E\* 语义差异表

产物 `reports/v2/e0_estar_semantic_diff.md`，含逐条映射、删除条目、假设层面变化，
以及 §9.1 要求的四个模块划分（M1 战略断键与收敛 / M2 选择性与保护 / M3 步骤排序与可靠性 /
M4 原料与复杂度分配）与预定义的适用 target 规则——**先于任何消融结果冻结**。

文本层面的主要结论：E\* 相对 E0 有 **6 处语义反转**（保护基、化学选择性、立体中心时机、
收敛偏好、de novo 环合 vs 采购目录核心、碱性氮掩蔽的条件化），**2 条实质新增化学内容**
（SNAr、C–H 官能化），**3 条删除**。其余多为语气与条件限定的变化。

因此「E\* 学到了新化学知识」在文本层面**没有支持**；文本支持的描述是「E\* 修正了 E0 的
过度防御与过度自建倾向」。E4 的消融应针对后一个假设设计。

---

## 2026-09-06（续 6）— 批次失败的正确诊断，以及范围收窄到单 seed

### 更正：153 分钟的 429 不是并发造成的

第一次批次（三次运行连跑、40 并发）触发了两个 Gemini 模型的网关级
`429 model_cooldown`，持续 **153 分钟**。我当时诊断为「40 并发超出网关可持续速率」并据此把
并发降到 12 —— **这个诊断是错的**。用户指出真实原因是**五小时配额窗口被用完**。

差别是实质性的：降并发并不能避免它，只是把同样的配额花得更慢；在配额窗口内反而应当保持
高并发，把可用窗口用充分。批次脚本已改回 40 并发，注释里的错误归因也已更正。

**这次失败里真正属于工程缺陷的部分**（与并发无关，已修）：

1. 批次脚本用 `tail -12` 截断每次运行的输出，导致 `reflective/11` 为什么在第 3 个槽位停止
   的诊断信息**永久丢失**。3 小时无人值守的批次不该截断日志。现已完整写入
   `results/v2/optimization/_logs/`。
2. 批次在一次运行失败后继续往下跑，于是在冷却期间连续启动 6 次注定失败的运行，每次 23 秒
   崩在 `FileNotFoundError`（所有生成任务失败 → 输出文件从未创建）。现在首次失败即中止。
3. `reflective/11` 的 screen 只填了 3 个槽位（发生在冷却之前，是真实的 `break`），却仍然
   跑完 advance/selection 并写出了 `winner.md`。`promotion.json` 里虽有 `full_schedule: false`
   标记，但下游读 `winner.md` 的代码不会看这个标记。**runner 现在直接拒绝**在 screen 未填满时
   进入 advance/selection，并把终止原因持久化到 `screen_stop_reason.json`。批次脚本额外校验
   `screen_candidates_evaluated == 16`。

`reflective/11` 已整个删除重跑：它残留的 advance/selection 行会污染续跑
（与 smoke run 隔离要解决的是同一个问题）。

### 范围收窄：先做完一个 seed

按用户要求，**不再一次铺开三个 seed**。先把 seed 11 的三个方法做完，看方向再决定是否扩展到
seed 29 / 47。

这偏离了计划 §6.2 的「3 seed」设计，是**有意的资源排序而非设计变更**：计划 §6.4 的门槛
（「至少 2/3 seed 为正」）与 Q1（方法可重复性）**仍然需要三个 seed 才能回答**。单 seed 的
结果只能用于判断方向，不能用于回答 Q1，也不能作为进入 E2/E3/E4 的正式门槛依据。
后续若只报告单 seed 结果，必须明确标注这一限制。

seed 11 当前状态：

| 运行 | 状态 |
|---|---|
| `search_only/11` | 完整（screen 16 / advance 4 / selection 2） |
| `instruction_opt/11` | screen 16 + advance 4 完成，待跑 selection |
| `reflective/11` | 待完整重跑 |
