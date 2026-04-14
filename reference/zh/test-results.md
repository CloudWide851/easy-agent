# 测试详情

## 快照策略

- `0.3.5` 版本发布的是 2026 年 4 月 14 日刷新后的 benchmark、public-eval、Python verification 与 real-network 快照。
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

## Real-Network 快照

最新快照时间：`2026-04-14T05:58:34Z`

| 测试集 | 分数 | 耗时（秒） | 说明 |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 1.6871 | well-known discovery 与 send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 11.7853 | 通过本地 A2A surface 的 loopback federation |
| real_network.disconnect_retry_chaos | 100.0 | 10.4526 | callback retry 与 signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 6.3195 | replay-safe callback 与 durable task events |
| real_network.workbench_reuse_process | 100.0 | 3.1016 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 34.8270 | container warm-start 与 snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 51.1865 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 20.9947 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 29.3842 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 7.1407 | replay/resume failure injection |

Warm-start telemetry summary：

| 指标 | 数值 |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 5.7820 |
| telemetry.microvm_warm_start_average_seconds | 9.1022 |
| telemetry.snapshot_drift_ratio_average | 0.5162 |
| telemetry.snapshot_drift_ratio_max | 0.6795 |

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
- 定向回归：provider adapters 与 web-search / BFCL evaluation，结果 `74 passed`
- 全量 unit tests：`190 passed`
- 全量 real integration：`6 passed`、`3 warnings`
- 本次 release 同步刷新了 benchmark、public-eval 与 real-network 三类 artifact
- 这轮 real integration 在沙箱外重跑，用来重新验证 live model/network 与 MCP-backed 路径

机器本地的完整执行日志不进入仓库公开文档。
