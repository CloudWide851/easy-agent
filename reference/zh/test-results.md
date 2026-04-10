# 测试详情

## 快照策略

- 本次未发布轮次沿用了上一轮 benchmark 与 public-eval artifacts。
- Python verification 与 real-network validation 在本轮单独刷新。
- 事实源：
  - `.easy-agent/benchmark-report.json`
  - `.easy-agent/public-eval-report.json`
  - `.easy-agent/real-network-report.json`

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
| public_eval.bfcl_simple | 100.0 | 9.6949 |
| public_eval.bfcl_multiple | 87.5 | 9.9134 |
| public_eval.bfcl_parallel_multiple | 100.0 | 10.3764 |
| public_eval.bfcl_irrelevance | 100.0 | 4.7704 |
| public_eval.bfcl_web_search | 0.0 | 17.5827 |
| public_eval.bfcl_memory | 0.0 | 4.5688 |
| public_eval.bfcl_format_sensitivity | 100.0 | 3.9376 |
| public_eval.tau2_mock | 100.0 | 4.4561 |

当前 category summary：

| 类别 | 分数 |
| --- | ---: |
| public_eval.bfcl_core | 95.83 |
| public_eval.bfcl_agentic | 33.33 |
| public_eval.tau2_mock | 100.0 |

当前保留 artifact 中仍需继续压缩的 blocker：

- `public_eval.bfcl_web_search`
- `public_eval.bfcl_memory`
- `public_eval.bfcl_multiple.case multiple_7`

## Real-Network 快照

最新 artifact 时间：`2026-04-10T04:02:21Z`

| 测试集 | 分数 | 耗时（秒） | 说明 |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 1.7601 | well-known discovery 与 send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 12.3902 | 通过本地 A2A surface 的 loopback federation |
| real_network.disconnect_retry_chaos | 100.0 | 5.9927 | callback retry 与 signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 4.4213 | replay-safe callback 与 durable task events |
| real_network.workbench_reuse_process | 100.0 | 2.2774 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 34.0342 | container warm-start 与 snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 51.6455 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 22.0955 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 32.3036 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 8.2912 | replay/resume failure injection |

Warm-start telemetry summary：

| 指标 | 数值 |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 6.0167 |
| telemetry.microvm_warm_start_average_seconds | 9.3689 |
| telemetry.snapshot_drift_ratio_average | 0.3080 |
| telemetry.snapshot_drift_ratio_max | 0.5323 |

## 同类项目对比

README 中只保留高层摘要，本页保留映射证据。

| 项目 | 证据来源 | Sessions / Memory | Replay / Resume | Tool Calling | Isolation | Public Evals |
| --- | --- | --- | --- | --- | --- | --- |
| easy-agent | 仓库本地测试证据 | session_id + session_messages + session_state + harness_state | resume、replay、fork、checkpoints | strict function calling + SerpApi web-search eval + provider schema matrix | process / container / microvm | BFCL + tau2 + real-network telemetry |
| OpenAI Agents SDK | 官方文档映射 | 文档已覆盖 | 不是以 graph replay runtime 为核心定位 | 官方 function calling / structured outputs | 文档中不是一等 executor matrix | 文档中没有 BFCL 风格内建评测面 |
| AutoGen | 官方文档映射 | 文档已覆盖 | 文档覆盖 stateful workflows | 文档覆盖 multi-agent tool use | 文档覆盖 code execution | 文档中没有仓库本地 BFCL 风格矩阵 |
| LangGraph | 官方文档映射 | 文档已覆盖 | 文档覆盖 durable execution 与 time-travel | 文档覆盖 graph tool calling | 文档中没有 container / microVM executor matrix | 文档中没有 BFCL / real-network 内建矩阵 |

## Python 验证

本轮只使用 Python-based verification。BFCL/public-eval live 套件在本轮刻意不重跑，继续沿用之前的 artifact 作为对外参考。

当前轮次的完整命令日志会在验证通过后写入 `MEMORY.md` 与 `AGENTS.md`。
