# RETRO-E 运行手册

所有命令从仓库根目录执行。先加载环境：

```bash
set -a; source .env; set +a
```

`.env` 里有 API key 与模型角色变量。**key 不进版本控制、不进 manifest、不进日志。**

## 0. 环境自检

```bash
uv sync --python 3.13 --extra dev
uv run pytest -q
uv run ruff check src scripts tests
uv run python scripts/status.py
```

`status.py` 末尾会跑 pytest 与 ruff；加 `--no-checks` 可跳过，加 `--json` 出机器可读格式。

## 1. round 1 的完整性校验

round 1 已冻结。协议哈希只能从快照就地还原时复现（详见 ARTIFACTS.md 的路径相关性说明）。

```bash
uv run python scripts/freeze_round1_protocol.py --mode verify
```

预期：两个确证阶段 `snapshot_intact: true`、`snapshot_corrupted: []`。
`working_tree_drifted_since` 非空是**正常的**——v2 改了 `src/`。

## 2. v2 数据划分（已完成，可重复执行验证确定性）

```bash
uv run python scripts/build_v2_splits.py
uv run retro-e audit-targets \
  data/processed/train_targets.jsonl data/processed/test_targets.jsonl \
  data/v2/dev_new_targets.jsonl data/v2/holdout_new_targets.jsonl
```

预期：`isolation_audit.passed = true`，泄漏计数全 0。

## 3. 铸造协议哈希

**只在代码冻结后做，并且九次 E1 运行必须共用同一个。**

```bash
uv run retro-e --config config/v2_pilot.toml manifest --stage optimization
```

改动这些会改变哈希：`config/v2_pilot.toml`、`prompts/`、`schemas/`、`src/retro_e/*.py`、
被引用的 target 与 context 文件。**`scripts/` 不在哈希内**——所以运行期间改 runner 是安全的，
改 `src/` 不是。

## 4. baseline 缓存

E1 从不自己生成 baseline，只消费缓存，以保证所有方法对同一批 baseline 样本打分。

```bash
HASH=<第 3 步的哈希>
uv run retro-e --config config/v2_pilot.toml generate \
  --targets data/processed/optimizer_targets.jsonl --condition baseline \
  --output results/v2/generations/optimizer_pool_baseline.jsonl \
  --protocol-hash $HASH --workers 40
uv run retro-e --config config/v2_pilot.toml generate \
  --targets data/processed/selection_targets.jsonl --condition baseline \
  --output results/v2/generations/selection_pool_baseline.jsonl \
  --protocol-hash $HASH --workers 40
```

**生成后必须核对模型**——CLI 曾静默回退到 round-1 的模型：

```bash
uv run python scripts/status.py --no-checks | grep -A3 "BASELINE CACHES"
```

`model` 应为 `gemini-3.8-flash`，`protocol` 应与 `$HASH` 前 12 位一致。

## 5. E1 优化运行

单次：

```bash
uv run python scripts/run_e1_matrix.py --method reflective --seed 11 \
  --protocol-hash $HASH --workers 40
```

批次（推荐，含失败即停与完整日志）：

```bash
./scripts/run_e1_batch.sh          # 运行列表在脚本内 RUNS 数组
WORKERS=20 GAP=60 ./scripts/run_e1_batch.sh
```

日志：`results/v2/optimization/_logs/<method>_<seed>.log`（完整，不截断）。

**断点续跑**：直接重跑同一条命令。生成与判定按键跳过，`evaluate_candidate` 幂等。

**smoke 运行**必须加 `--run-tag`，否则会污染正式运行的 ledger：

```bash
uv run python scripts/run_e1_matrix.py --method search_only --seed 11 \
  --run-tag _smoke --max-screen-candidates 3 --protocol-hash $HASH --workers 40
```

### 运行失败时

| 现象 | 含义 | 处理 |
|---|---|---|
| `IncompleteEvaluationError` | 配对数少于计划，拒绝在残缺批次上打分 | 重跑同一命令续跑；runner 已内建 4 次重试 |
| `Screen tier filled N of 16` | screen 提前终止 | 读 `screen_stop_reason.json`，修因，**删除整个运行目录**后重跑 |
| `429 model_cooldown` | 五小时配额窗口用完 | 等待（实测约 153 分钟），不是并发问题 |
| `FileNotFoundError: ..._generations.jsonl` | 该候选**所有**生成都失败 | 通常是上一条的下游症状 |

**为什么必须删目录而不是续跑**：短 screen 已经写入的 advance/selection 行会被续跑当作已完成，
而重新排名后的前 4 名可能不同，陈旧行会参与 selection 排序。

## 6. dev 评估（§6.3）

需要九个赢家全部就位。

```bash
uv run python scripts/run_e1_dev_eval.py --protocol-hash $HASH --workers 40
uv run python scripts/run_e1_dev_eval.py --protocol-hash $HASH --stage metrics
```

分阶段：`--stage generate|judge|metrics`。产物 `results/v2/dev_eval/dev_eval_metrics.json`。

## 7. 校准探针（重测模型成本与偏倚时）

```bash
uv run python scripts/v2_calibrate.py --probe cost       # 隐藏推理 token 与延迟
uv run python scripts/v2_calibrate.py --probe order      # A/B 位置偏倚与翻转一致性
uv run python scripts/v2_calibrate.py --probe judge_v3   # 结构化输出解析率
```

复用已冻结的 round-1 路线重新评判，不生成新路线。

## 8. P0 复核（计划 §2.1）

```bash
uv run python scripts/p0_round1_reanalysis.py
```

只读 round-1 文件，产出 `results/v2/p0_round1_reanalysis.json`。

## 停止一切

```bash
pkill -f run_e1_batch.sh; pkill -f run_e1_matrix.py; pkill -f retro_e.cli
```

所有产物 append-only 且可续跑，中断不会损坏数据。
