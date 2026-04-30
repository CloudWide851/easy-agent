# 测试详情

## 快照策略

- `0.3.5` 版本发布的是 2026 年 4 月 14 日刷新后的 benchmark、public-eval、Python verification 与 real-network 快照。
- 最新一轮未发布验证发生在 2026 年 4 月 30 日：保留 4 月 14 日的 benchmark 与 public-eval headline 分数快照，同时刷新 Python verification 与 browser/dashboard/failure-to-fix CLI 检查；live provider compatibility matrix 仍保留 4 月 20 日验证快照。
- 仓库公开文档只保留方法说明与分数，不暴露机器本地协作日志。

## Benchmark 快照

| 测试集 | 分数 | 平均耗时（秒） |
| --- | ---: | ---: |
| benchmark.single_agent | 100.0 | 5.0674 |
| benchmark.sub_agent | 100.0 | 59.2087 |
| benchmark.multi_agent_graph | 100.0 | 12.6349 |
| benchmark.team_round_robin | 100.0 | 9.9354 |
| benchmark.team_selector | 100.0 | 13.9754 |
| benchmark.team_swarm | 100.0 | 11.7101 |

## Public Eval 快照

| 测试集 | 分数 | 平均耗时（秒） |
| --- | ---: | ---: |
| public_eval.bfcl_simple | 100.0 | 5.0554 |
| public_eval.bfcl_multiple | 100.0 | 6.3535 |
| public_eval.bfcl_parallel_multiple | 100.0 | 8.7009 |
| public_eval.bfcl_irrelevance | 100.0 | 4.3747 |
| public_eval.bfcl_web_search | 100.0 | 6.9273 |
| public_eval.bfcl_memory | 100.0 | 3.9823 |
| public_eval.bfcl_format_sensitivity | 100.0 | 4.1343 |
| public_eval.tau2_mock | 100.0 | 4.9205 |

当前 headline 分数：

| 类别 | 分数 |
| --- | ---: |
| public_eval.bfcl_overall | 100.0 |
| public_eval.bfcl_case_pass_rate | 100.0 |
| public_eval.bfcl_core | 100.0 |
| public_eval.bfcl_agentic | 100.0 |
| public_eval.tau2_mock | 100.0 |

计分说明：

- `public_eval.bfcl_overall` 使用当前仓库已覆盖 BFCL 子类的 official-style subcategory accuracy，不再直接等同于 raw case pass rate。
- `public_eval.bfcl_case_pass_rate` 保留为诊断指标，用来观察单 case 成功率。
- `public_eval.bfcl_web_search` 以规范化最终答案准确率为主，tool-call 命中率继续保留为诊断信号。
- 这次 repo-pinned `full_v4` BFCL 子集已经全绿，既包括 core multi-tool cases，也包括新增的 search-plus-contents 与 memory-backed cases。
- `official_full_v4` 现在会先把 JSON / JSONL 的 raw official manifest 做归一化，再进入过滤和执行流程，而不直接切换 README headline score 的基线。
- `browsecomp_subset`、`simpleqa_subset` 与 `simple_evals_subset` 现在已经支持作为本地补强 profile，但因为仓库不直接 vendored 这些 benchmark 题目，所以它们还不进入保留的 headline score 快照。
- provider compatibility matrix 现在同时覆盖 OpenAI-compatible 的 chat-completions 与 Responses API payload / parsing 对齐，建立在 strict function-calling 基线之上。
- MCP catalog durability 现在也覆盖 `resource_templates`、prompt detail cache entries 与通知驱动的 stale 标记。

2026 年 4 月 14 日 release refresh 的 web-search diagnostics：

| 指标 | 数值 |
| --- | ---: |
| web_search.content_sources.cache | 0 |
| web_search.content_sources.network | 0 |
| web_search.content_sources.replay | 2 |
| web_search.grounded_retry_count | 0 |
| web_search.grounded_sources_average | 1.4 |

解释说明：

- 这次发布保持了 repo-pinned BFCL web-search 子集全绿，同时把 search/contents 的来源类型从 headline 分数里独立暴露出来。
- 在这台机器上的 release refresh 中，BFCL web-search 刷新是通过 replay-backed evidence 完成的，而不是 live SerpApi 结果；这个事实已经写进诊断字段，而不是被简单的 pass 掩盖掉。

## Provider Compatibility Live Verification

这个矩阵对应的是 2026 年 4 月 20 日最新的 live 验证结果，和 README 中保留的 4 月 14 日 benchmark/public-eval headline 分数快照分开维护。

| Target | 状态 | 说明 |
| --- | --- | --- |
| openai_live | passed | 必跑的 DeepSeek/OpenAI-compatible 基线在 `chat_completions` 上通过，strict-schema、`tool_choice: none`、required-tool 与 forced-tool 检查全部通过。 |
| anthropic_live | skipped | 可选 target；这轮验证中本地没有提供 `ANTHROPIC_API_KEY`。 |
| gemini_live | skipped | 可选 target；这轮验证中本地没有提供 `GEMINI_API_KEY`。 |

兼容性说明：

- 对于非 OpenAI 官方的 OpenAI-compatible provider，`single_tool_call_control` 现在会被标成 `best_effort`，而不是在 provider 暴露字段但运行时未严格执行时把整行错误地判成失败。
- BFCL web-search 查询参数现在支持 `x-easy-agent-normalizer: web_search_query`，可以在评分前归一化包装语，但不会放松精确答案校验。

## Real-Network 快照

最新快照时间：`2026-04-29T08:32:11Z`

| 测试集 | 分数 | 耗时（秒） | 说明 |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 1.1644 | well-known discovery 与 send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 8.5967 | 通过本地 A2A surface 的 loopback federation |
| real_network.disconnect_retry_chaos | 100.0 | 5.1623 | callback retry、push notifications 与 signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 5.1668 | replay-safe callback 与 durable task events |
| real_network.workbench_reuse_process | 100.0 | 2.1623 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 29.8542 | container warm-start 与 snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 46.8568 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 16.7065 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 24.5266 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 5.6834 | replay/resume failure injection |

Warm-start telemetry summary：

| 指标 | 数值 |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 4.6869 |
| telemetry.microvm_warm_start_average_seconds | 6.8618 |
| telemetry.snapshot_drift_ratio_average | 0.4081 |
| telemetry.snapshot_drift_ratio_max | 0.7116 |

每条 real-network record 现在都会带场景证明字段，让分数行可以追溯到可执行的场景契约：

| 场景 | Command | Expected Artifact | Pass Criteria |
| --- | --- | --- | --- |
| resume after failure | `uv run easy-agent integration real-network` | real-network report row for replay/resume failure injection | checkpoint replay 或 resume 完成，并且不重复执行已完成工作 |
| human approval pending then continue | `uv run easy-agent run ... --approval-mode deferred` 加 `uv run easy-agent approvals approve ...` | run summary、approval record 与 trace tree | 敏感操作进入 durable approval，并在批准后继续 |
| MCP server restart | transport refresh 后执行 `uv run easy-agent mcp resources list ...` | MCP catalog snapshot 与 subscription state | catalog entries、prompt details 与 subscription state 能跨 refresh 或 restart 保留 |
| provider tool schema rejection then repair | `uv run easy-agent integration public-eval --profile full_v4` | public-eval provider matrix 与 failure-stage diagnostics | provider schema rejection 进入 strict-schema repair 或被标成明确的 best-effort 证据 |
| federation disconnect and retry | `uv run easy-agent integration real-network` | real-network row for disconnect retry chaos | callback retry、signed delivery、sendSubscribe 与 resubscribe 保持耐用 |
| workbench snapshot restore | `uv run easy-agent integration real-network` | real-network workbench restore rows | process、container 或 microVM session 在配置预算内恢复状态 |

同一份报告也会在 executor 和 federation 行里携带安全断言，包括凭据不落盘、loopback-only 测试 server、signed callback verification、scoped workbench roots 和显式 host-gated dependencies。

## 同类 Agent 项目对比

README 只保留高层摘要，本页保留公开证据映射。

| 项目 | 证据来源 | Sessions / Memory | Replay / Resume | Tool Calling | Isolation | Public Evals |
| --- | --- | --- | --- | --- | --- | --- |
| easy-agent | 仓库本地测试证据 | session_id + session_messages + session_state + harness_state | resume、replay、fork、checkpoints | strict function calling + SerpApi web-search eval + provider schema matrix | process / container / microvm | BFCL + tau2 + real-network telemetry |
| OpenHands | 官方文档映射 | conversation 与 state surface 有文档说明 | 持续任务继续能力有文档说明，但不是 replay-first runtime | coding-agent tool 与 browser actions 有文档说明 | sandbox/runtime isolation 有文档说明 | 官方文档中没有 BFCL 风格内建公开评测矩阵 |
| Skyvern | 官方文档映射 | workflow 与 run history 有文档说明 | workflow rerun / recovery 有文档说明，但不是 checkpoint-first | browser 与 action execution 有文档说明 | hosted browser/runtime boundary 有文档说明 | 官方文档中没有 BFCL 风格公开评测矩阵 |
| AutoGPT Platform | 官方文档映射 | agents、workflows、run state 有文档说明 | workflow reruns 有文档说明，但不是 graph replay runtime | agent blocks 与 integrations 有文档说明 | platform execution boundary 有文档说明 | 官方文档中没有 BFCL 风格内建公开评测矩阵 |

## Python 验证

本轮只使用 Python-based verification。

- 静态检查：`ruff` 与 `mypy`
- 定向回归：setup preflight、wizard、config explanation、config doctor、可搜索 HTML trace export、本地 trace 打开、latest-report 汇总和 HTML export、静态 dashboard export、report trend、experimental OTel trace export、connector diagnostics、Playwright MCP browser readiness、browser doctor/artifact listing、browser task packs、本地 skill catalog、plugin doctor、Python facade embedding、mock provider、onboarding CLI、scenario creation、run explanation/fix packages、provider compatibility、config validation、guardrails、BFCL evaluation、official-source search 与 simple-evals profile support；最新 focused set 为 `55 passed`，保留兼容性集合为 `23 passed`、`36 passed`、`89 passed` 加 `4 passed`
- 全量 unit tests：`230 passed`
- 定向 live provider compatibility 回归：`1 passed`
- 全量 real integration：`7 passed`、`2 warnings`
- 保留的 benchmark 与 public-eval headline 分数仍指向 4 月 14 日发布快照，live provider compatibility 证据保留 4 月 20 日快照，详细 real-network artifact 仍保留 4 月 29 日快照，而 4 月 30 日 live integration 命令重新验证了套件
- 剩余 warning 仍然是 Windows asyncio subprocess cleanup 的已知问题，不属于功能失败
- 新增 focused regressions 覆盖 setup preflight、wizard scenario creation、browser template Playwright MCP config、browser doctor/artifact listing、browser task packs、config explanation、config doctor、可搜索 HTML trace export、`traces open`、`report latest`、增强版 `dashboard`、`report trend`、latest-report HTML export、experimental OTel trace export、connector diagnostics、本地 skill catalog install、plugin doctor、offline mock runs、starter templates、coding/research/data/ops/browser/support/sales/document/QA/release starters 的 `new <scenario>`、Python `AgentApp`、quickstart、run explanation、advice-only JSON/Markdown/HTML run fix packages、browser-MCP failure classification、run listing、run summary、structured trace tree export、executor capability reports、storage contracts 与 real-network scenario proof metadata。
- CLI smoke 已覆盖 `wizard`、增强版 `dashboard`、`runs fix --format html`、`browser doctor`、`browser artifacts`、`connectors doctor`、`connectors test browser`、`task show repo-review`、`task show browser-qa`、`report trend`、`skills catalog list` 与 `new customer-support-agent`。

机器本地的完整执行日志不进入仓库公开文档。
