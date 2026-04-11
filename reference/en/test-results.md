# Test Results

## Snapshot Policy

- This unreleased round keeps the retained benchmark snapshot from April 9, 2026.
- Public eval, Python verification, and the real-network snapshot are refreshed independently on April 11, 2026 in this round.
- Public docs in this repository intentionally expose methodology and scores only; local collaboration logs are not part of the repository-facing surface.

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
| public_eval.bfcl_simple | 100.0 | 4.9657 |
| public_eval.bfcl_multiple | 100.0 | 6.2071 |
| public_eval.bfcl_parallel_multiple | 100.0 | 7.1866 |
| public_eval.bfcl_irrelevance | 100.0 | 4.0190 |
| public_eval.bfcl_web_search | 100.0 | 4.2093 |
| public_eval.bfcl_memory | 0.0 | 4.0552 |
| public_eval.bfcl_format_sensitivity | 100.0 | 3.6896 |
| public_eval.tau2_mock | 100.0 | 4.2969 |

Current headline scores:

| Category | Score |
| --- | ---: |
| public_eval.bfcl_overall | 85.71 |
| public_eval.bfcl_case_pass_rate | 90.91 |
| public_eval.bfcl_core | 96.88 |
| public_eval.bfcl_agentic | 66.67 |
| public_eval.tau2_mock | 100.0 |

Scoring notes:

- `public_eval.bfcl_overall` is the official-style subcategory accuracy over the BFCL suites currently evaluated in this repository scope. It is not the raw case pass rate.
- `public_eval.bfcl_case_pass_rate` remains available as a diagnostic metric for individual-case success.
- `public_eval.bfcl_web_search` is tracked as normalized final-answer accuracy, with tool-call match rates kept as diagnostic signals.
- The repo-pinned BFCL web-search slice is currently green in this snapshot; the remaining BFCL misses are all in the memory slice.

Current retained blockers:

- `public_eval.bfcl_memory.case memory_0`
- `public_eval.bfcl_memory.case memory_1`
- `public_eval.bfcl_memory.case memory_2`

## Real-Network Snapshot

Latest generated snapshot timestamp: `2026-04-11T05:08:31Z`

| Test Set | Score | Duration (s) | Notes |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 0.9272 | well-known discovery and send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 8.1009 | loopback federation through the local A2A surface |
| real_network.disconnect_retry_chaos | 100.0 | 4.3419 | callback retry and signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 3.9684 | replay-safe callback and durable task events |
| real_network.workbench_reuse_process | 100.0 | 1.9216 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 31.4660 | container warm-start and snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 50.3835 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 20.2400 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 28.3170 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 5.7929 | replay/resume failure injection |

Warm-start telemetry summary:

| Metric | Value |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 5.3533 |
| telemetry.microvm_warm_start_average_seconds | 8.2094 |
| telemetry.snapshot_drift_ratio_average | 0.3456 |
| telemetry.snapshot_drift_ratio_max | 0.5177 |

## Similar Agent Project Comparison

The README keeps the comparison high level. This page keeps the public evidence mapping.

| Project | Evidence Basis | Sessions / Memory | Replay / Resume | Tool Calling | Isolation | Public Evals |
| --- | --- | --- | --- | --- | --- | --- |
| easy-agent | repo-local tested evidence | session_id + session_messages + session_state + harness_state | resume, replay, fork, checkpoints | strict function calling + SerpApi web-search eval + provider schema matrix | process / container / microvm | BFCL + tau2 + real-network telemetry |
| OpenHands | official docs mapping | conversation and state surfaces documented | persistent task continuation documented, not a replay-first runtime | coding-agent tool and browser actions documented | sandbox/runtime isolation documented | no BFCL-style built-in public eval matrix in docs |
| Skyvern | official docs mapping | workflow and run history documented | workflow rerun and recovery documented, less checkpoint-centric | browser and action execution documented | hosted browser/runtime boundary documented | no BFCL-style public eval matrix in docs |
| AutoGPT Platform | official docs mapping | agents, workflows, and run state documented | workflow reruns documented, not a graph replay runtime | agent blocks and integrations documented | platform execution boundary documented | no BFCL-style built-in public eval matrix in docs |

## Python Verification

This round uses Python-based verification only.

- Static checks: `ruff` and `mypy`
- Targeted regressions around provider adapters, BFCL web-search grounding, and README snapshots
- Full unit coverage
- Full real integration coverage, including the refreshed public-eval and real-network paths

Exact machine-local execution logs stay outside the repository-facing documentation surface.
