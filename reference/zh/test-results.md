# 测试详情

## 快照策略

- 本次未发布轮次保留 benchmark 快照。
- public-eval、Python verification 与 real-network 快照在本轮单独刷新。
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
| public_eval.bfcl_simple | 100.0 | 4.7372 |
| public_eval.bfcl_multiple | 87.5 | 6.0188 |
| public_eval.bfcl_parallel_multiple | 100.0 | 10.2508 |
| public_eval.bfcl_irrelevance | 100.0 | 3.5373 |
| public_eval.bfcl_web_search | 0.0 | 4.1557 |
| public_eval.bfcl_memory | 0.0 | 4.1773 |
| public_eval.bfcl_format_sensitivity | 100.0 | 3.3635 |
| public_eval.tau2_mock | 100.0 | 4.3855 |

当前 headline 分数：

| 类别 | 分数 |
| --- | ---: |
| public_eval.bfcl_overall | 69.64 |
| public_eval.bfcl_case_pass_rate | 78.79 |
| public_eval.bfcl_core | 96.88 |
| public_eval.bfcl_agentic | 33.33 |
| public_eval.tau2_mock | 100.0 |

计分说明：

- `public_eval.bfcl_overall` 使用当前仓库已覆盖 BFCL 子类的 official-style subcategory accuracy，不再直接等同于 raw case pass rate。
- `public_eval.bfcl_case_pass_rate` 保留为诊断指标，用来观察单 case 成功率。
- `public_eval.bfcl_web_search` 以规范化最终答案准确率为主，tool-call 命中率继续保留为诊断信号。

当前保留 blocker：

- `public_eval.bfcl_web_search.case web_search_0`
- `public_eval.bfcl_web_search.case web_search_1`
- `public_eval.bfcl_web_search.case web_search_2`
- `public_eval.bfcl_memory`
- `public_eval.bfcl_multiple.case multiple_7`

## Real-Network 快照

最新快照时间：`2026-04-10T06:28:24Z`

| 测试集 | 分数 | 耗时（秒） | 说明 |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 1.5606 | well-known discovery 与 send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 10.3670 | 通过本地 A2A surface 的 loopback federation |
| real_network.disconnect_retry_chaos | 100.0 | 5.3995 | callback retry 与 signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 4.3246 | replay-safe callback 与 durable task events |
| real_network.workbench_reuse_process | 100.0 | 1.7866 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 32.3282 | container warm-start 与 snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 50.4138 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 18.5648 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 26.7154 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 5.4515 | replay/resume failure injection |

Warm-start telemetry summary：

| 指标 | 数值 |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 6.0310 |
| telemetry.microvm_warm_start_average_seconds | 7.5851 |
| telemetry.snapshot_drift_ratio_average | 0.4274 |
| telemetry.snapshot_drift_ratio_max | 0.8538 |

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
- 定向回归：provider adapters、BFCL aggregation、README snapshots
- 全量 unit tests
- real integration：public-eval 与非 public-eval real-network 分开验证

机器本地的完整执行日志不进入仓库公开文档。
