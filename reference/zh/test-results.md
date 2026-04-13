# 测试详情

## 快照策略

- `0.3.4` 版本保留 2026 年 4 月 9 日的 benchmark 快照。
- 本次发布采用 2026 年 4 月 13 日刷新后的 public-eval、Python verification 与 real-network 快照。
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
| public_eval.bfcl_simple | 100.0 | 8.7802 |
| public_eval.bfcl_multiple | 100.0 | 10.3507 |
| public_eval.bfcl_parallel_multiple | 100.0 | 13.2126 |
| public_eval.bfcl_irrelevance | 100.0 | 6.5760 |
| public_eval.bfcl_web_search | 100.0 | 9.8743 |
| public_eval.bfcl_memory | 100.0 | 6.7686 |
| public_eval.bfcl_format_sensitivity | 100.0 | 6.4398 |
| public_eval.tau2_mock | 100.0 | 7.7507 |

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
- 现在也支持受控的 `official_full_v4` manifest slice，用来继续扩大覆盖面，而不直接切换 README headline score 的基线。

## Real-Network 快照

最新快照时间：`2026-04-13T11:49:06Z`

| 测试集 | 分数 | 耗时（秒） | 说明 |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 0.9074 | well-known discovery 与 send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 7.4315 | 通过本地 A2A surface 的 loopback federation |
| real_network.disconnect_retry_chaos | 100.0 | 3.8996 | callback retry 与 signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 3.9662 | replay-safe callback 与 durable task events |
| real_network.workbench_reuse_process | 100.0 | 1.9268 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 32.2239 | container warm-start 与 snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 50.8435 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 20.5576 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 28.9500 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 6.5188 | replay/resume failure injection |

Warm-start telemetry summary：

| 指标 | 数值 |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 5.7206 |
| telemetry.microvm_warm_start_average_seconds | 8.4611 |
| telemetry.snapshot_drift_ratio_average | 0.4033 |
| telemetry.snapshot_drift_ratio_max | 0.6701 |

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
- 定向回归：provider adapters、BFCL retry routing、official-manifest filtering、MCP catalog durability、README snapshots
- 全量 unit tests：`182 passed`
- 全量 real integration：`6 passed`、`3 warnings`
- 在最终 BFCL 单调用重试调整后，重新以临时 checkpoint 和 usage ledger 刷新 live public-eval

机器本地的完整执行日志不进入仓库公开文档。
