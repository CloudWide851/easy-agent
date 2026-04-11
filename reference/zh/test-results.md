# 测试详情

## 快照策略

- 本次未发布轮次保留 2026 年 4 月 9 日的 benchmark 快照。
- public-eval、Python verification 与 real-network 快照在本轮于 2026 年 4 月 11 日单独刷新。
- 仓库公开文档只保留方法说明与分数，不暴露机器本地协作日志。

## Benchmark 快照

| 测试集 | 分数 | 平均耗时（秒） |
| --- | ---: | ---: |
| benchmark.single_agent | 100.0 | 4.2843 |
| benchmark.sub_agent | 100.0 | 20.7399 |
| benchmark.multi_agent_graph | 100.0 | 9.8910 |
| benchmark.team_round_robin | 100.0 | 7.7402 |
| benchmark.team_selector | 100.0 | 9.7480 |
| benchmark.team_swarm | 100.0 | 8.2819 |

## Public Eval 快照

| 测试集 | 分数 | 平均耗时（秒） |
| --- | ---: | ---: |
| public_eval.bfcl_simple | 100.0 | 5.3071 |
| public_eval.bfcl_multiple | 87.5 | 6.8439 |
| public_eval.bfcl_parallel_multiple | 100.0 | 8.9665 |
| public_eval.bfcl_irrelevance | 100.0 | 5.1847 |
| public_eval.bfcl_web_search | 100.0 | 8.3509 |
| public_eval.bfcl_memory | 100.0 | 4.9598 |
| public_eval.bfcl_format_sensitivity | 100.0 | 4.7317 |
| public_eval.tau2_mock | 100.0 | 5.8595 |

当前 headline 分数：

| 类别 | 分数 |
| --- | ---: |
| public_eval.bfcl_overall | 98.21 |
| public_eval.bfcl_case_pass_rate | 97.22 |
| public_eval.bfcl_core | 96.88 |
| public_eval.bfcl_agentic | 100.0 |
| public_eval.tau2_mock | 100.0 |

计分说明：

- `public_eval.bfcl_overall` 使用当前仓库已覆盖 BFCL 子类的 official-style subcategory accuracy，不再直接等同于 raw case pass rate。
- `public_eval.bfcl_case_pass_rate` 保留为诊断指标，用来观察单 case 成功率。
- `public_eval.bfcl_web_search` 以规范化最终答案准确率为主，tool-call 命中率继续保留为诊断信号。
- 这次 repo-pinned BFCL agentic 子集已经全绿，包括新增的 search-plus-contents 与 memory-backed cases。
- 当前剩余 BFCL miss 只剩一个 core multi-tool case，而不是 web-search 或 memory 回归。

当前保留 blocker：

- `public_eval.bfcl_multiple.case multiple_7`

## Real-Network 快照

最新快照时间：`2026-04-11T06:35:04Z`

| 测试集 | 分数 | 耗时（秒） | 说明 |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 0.7472 | well-known discovery 与 send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 6.8836 | 通过本地 A2A surface 的 loopback federation |
| real_network.disconnect_retry_chaos | 100.0 | 4.0732 | callback retry 与 signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 3.9030 | replay-safe callback 与 durable task events |
| real_network.workbench_reuse_process | 100.0 | 1.8304 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 31.5884 | container warm-start 与 snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 50.9175 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 20.0722 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 28.3898 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 5.6027 | replay/resume failure injection |

Warm-start telemetry summary：

| 指标 | 数值 |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 5.6855 |
| telemetry.microvm_warm_start_average_seconds | 8.2140 |
| telemetry.snapshot_drift_ratio_average | 0.3801 |
| telemetry.snapshot_drift_ratio_max | 0.6447 |

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
- 定向回归：provider adapters、BFCL search-plus-contents grounding、memory semantics、README snapshots
- 全量 unit tests：`166 passed`
- 全量 real integration：`5 passed`、`5 warnings`
- 在最终 BFCL memory alias 调整后，重新以临时 checkpoint 和 usage ledger 刷新 live public-eval

机器本地的完整执行日志不进入仓库公开文档。
