# 容量臂复用 baseline 缓存的说明

容量臂（`config/v2_capacity.toml`，协议哈希 `355720e9…`）复用了挂在 `c7a40018…` 下的
baseline 缓存，未重新生成。

**理由**：两个配置之间唯一的差异是 `context.target_tokens`（800 → 1600）。该参数只用于候选
经验文本的长度上限，而 baseline 条件**不注入任何 context**（`compose_generation_prompt` 在
`condition="baseline"` 时不附加经验文本），因此 baseline 生成可证明不受该参数影响。

**为什么这与此前的决定不矛盾**：E1 阶段重新生成 baseline，是因为**生成模型**从
`gemini-3.7-flash-high` 换成了 `gemini-3.8-flash-high` —— 那确实改变 baseline 的内容。
此处模型未变，只有一个不参与 baseline 生成的参数变了。

**记录而非静默**：`run_e1_matrix.py` 的 `load_baseline` 不校验协议哈希，所以这一复用不会被
代码拦下。本文件存在的目的就是让它成为一条显式披露，而不是一个无人知晓的不一致。
