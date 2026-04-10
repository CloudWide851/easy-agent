# Test Results

## Snapshot Policy

- This unreleased round keeps the retained benchmark snapshot.
- Public eval, Python verification, and the real-network snapshot are refreshed independently in this round.
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
| public_eval.bfcl_simple | 100.0 | 4.7372 |
| public_eval.bfcl_multiple | 87.5 | 6.0188 |
| public_eval.bfcl_parallel_multiple | 100.0 | 10.2508 |
| public_eval.bfcl_irrelevance | 100.0 | 3.5373 |
| public_eval.bfcl_web_search | 0.0 | 4.1557 |
| public_eval.bfcl_memory | 0.0 | 4.1773 |
| public_eval.bfcl_format_sensitivity | 100.0 | 3.3635 |
| public_eval.tau2_mock | 100.0 | 4.3855 |

Current headline scores:

| Category | Score |
| --- | ---: |
| public_eval.bfcl_overall | 69.64 |
| public_eval.bfcl_case_pass_rate | 78.79 |
| public_eval.bfcl_core | 96.88 |
| public_eval.bfcl_agentic | 33.33 |
| public_eval.tau2_mock | 100.0 |

Scoring notes:

- `public_eval.bfcl_overall` is the official-style subcategory accuracy over the BFCL suites currently evaluated in this repository scope. It is not the raw case pass rate.
- `public_eval.bfcl_case_pass_rate` remains available as a diagnostic metric for individual-case success.
- `public_eval.bfcl_web_search` is tracked as normalized final-answer accuracy, with tool-call match rates kept as diagnostic signals.

Current retained blockers:

- `public_eval.bfcl_web_search.case web_search_0`
- `public_eval.bfcl_web_search.case web_search_1`
- `public_eval.bfcl_web_search.case web_search_2`
- `public_eval.bfcl_memory`
- `public_eval.bfcl_multiple.case multiple_7`

## Real-Network Snapshot

Latest generated snapshot timestamp: `2026-04-10T06:28:24Z`

| Test Set | Score | Duration (s) | Notes |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 1.5606 | well-known discovery and send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 10.3670 | loopback federation through the local A2A surface |
| real_network.disconnect_retry_chaos | 100.0 | 5.3995 | callback retry and signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 4.3246 | replay-safe callback and durable task events |
| real_network.workbench_reuse_process | 100.0 | 1.7866 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 32.3282 | container warm-start and snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 50.4138 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 18.5648 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 26.7154 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 5.4515 | replay/resume failure injection |

Warm-start telemetry summary:

| Metric | Value |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 6.0310 |
| telemetry.microvm_warm_start_average_seconds | 7.5851 |
| telemetry.snapshot_drift_ratio_average | 0.4274 |
| telemetry.snapshot_drift_ratio_max | 0.8538 |

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
- Targeted regressions around provider adapters, BFCL aggregation, and README snapshots
- Full unit coverage
- Real integration coverage with public-eval and non-public-eval real-network scenarios validated separately

Exact machine-local execution logs stay outside the repository-facing documentation surface.
