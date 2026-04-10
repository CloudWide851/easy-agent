# Test Results

## Snapshot Policy

- This unreleased round keeps the prior benchmark and public-eval artifacts.
- Python verification and real-network validation are refreshed independently in this round.
- Artifact sources:
  - `.easy-agent/benchmark-report.json`
  - `.easy-agent/public-eval-report.json`
  - `.easy-agent/real-network-report.json`

## Benchmark Snapshot

| Test Set | Score | Avg Duration (s) |
| --- | ---: | ---: |
| benchmark.single_agent | 100.0 | 4.2843 |
| benchmark.sub_agent | 100.0 | 20.7399 |
| benchmark.multi_agent_graph | 100.0 | 9.8910 |
| benchmark.team_round_robin | 100.0 | 7.7402 |
| benchmark.team_selector | 100.0 | 9.7480 |
| benchmark.team_swarm | 100.0 | 8.2819 |

## Public Eval Snapshot

| Test Set | Score | Avg Duration (s) |
| --- | ---: | ---: |
| public_eval.bfcl_simple | 100.0 | 9.6949 |
| public_eval.bfcl_multiple | 87.5 | 9.9134 |
| public_eval.bfcl_parallel_multiple | 100.0 | 10.3764 |
| public_eval.bfcl_irrelevance | 100.0 | 4.7704 |
| public_eval.bfcl_web_search | 0.0 | 17.5827 |
| public_eval.bfcl_memory | 0.0 | 4.5688 |
| public_eval.bfcl_format_sensitivity | 100.0 | 3.9376 |
| public_eval.tau2_mock | 100.0 | 4.4561 |

Current category summary:

| Category | Score |
| --- | ---: |
| public_eval.bfcl_core | 95.83 |
| public_eval.bfcl_agentic | 33.33 |
| public_eval.tau2_mock | 100.0 |

Current remaining blockers from the retained artifact:

- `public_eval.bfcl_web_search`
- `public_eval.bfcl_memory`
- `public_eval.bfcl_multiple.case multiple_7`

## Real-Network Snapshot

Latest generated artifact timestamp: `2026-04-10T04:02:21Z`

| Test Set | Score | Duration (s) | Notes |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 1.7601 | well-known discovery and send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 12.3902 | loopback federation through local A2A surface |
| real_network.disconnect_retry_chaos | 100.0 | 5.9927 | callback retry and signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 4.4213 | replay-safe callback and durable task events |
| real_network.workbench_reuse_process | 100.0 | 2.2774 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 34.0342 | container warm-start and snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 51.6455 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 22.0955 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 32.3036 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 8.2912 | replay/resume failure injection |

Warm-start telemetry summary:

| Metric | Value |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 6.0167 |
| telemetry.microvm_warm_start_average_seconds | 9.3689 |
| telemetry.snapshot_drift_ratio_average | 0.3080 |
| telemetry.snapshot_drift_ratio_max | 0.5323 |

## Similar Project Comparison

The README keeps the comparison high level. This page keeps the evidence mapping.

| Project | Evidence Basis | Sessions / Memory | Replay / Resume | Tool Calling | Isolation | Public Evals |
| --- | --- | --- | --- | --- | --- | --- |
| easy-agent | repo-local tested evidence | session_id + session_messages + session_state + harness_state | resume, replay, fork, checkpoints | strict function calling + SerpApi-backed web-search eval + provider schema matrix | process / container / microvm | BFCL + tau2 + real-network telemetry |
| OpenAI Agents SDK | official docs mapping | documented | not positioned as graph replay runtime | official function calling / structured outputs | not a first-class executor matrix in docs | no BFCL-style built-in eval surface in docs |
| AutoGen | official docs mapping | documented | stateful workflows documented | multi-agent tool use documented | code execution documented | no repo-local BFCL-style matrix in docs |
| LangGraph | official docs mapping | documented | durable execution and time-travel documented | graph-based tool calling documented | not a built-in container / microvm executor matrix in docs | no built-in BFCL / real-network matrix in docs |

## Python Verification

The current round uses Python-based verification only. BFCL/public-eval live reruns are intentionally skipped in this round because the retained artifact remains the published reference for those suites.

The exact command log for the current round is recorded into `MEMORY.md` and `AGENTS.md` after verification passes.
