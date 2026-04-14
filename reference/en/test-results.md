# Test Results

## Snapshot Policy

- Release `0.3.4` keeps the retained benchmark snapshot from April 9, 2026.
- Public eval, Python verification, and the real-network snapshot are published from the April 13, 2026 refresh for this release.
- The unreleased April 14, 2026 reinforcement reran the Python verification suites after adding OpenAI Responses API parity, raw official BFCL manifest normalization, and deeper MCP template-refresh coordination, without changing the README score baseline.
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
| public_eval.bfcl_simple | 100.0 | 8.7802 |
| public_eval.bfcl_multiple | 100.0 | 10.3507 |
| public_eval.bfcl_parallel_multiple | 100.0 | 13.2126 |
| public_eval.bfcl_irrelevance | 100.0 | 6.5760 |
| public_eval.bfcl_web_search | 100.0 | 9.8743 |
| public_eval.bfcl_memory | 100.0 | 6.7686 |
| public_eval.bfcl_format_sensitivity | 100.0 | 6.4398 |
| public_eval.tau2_mock | 100.0 | 7.7507 |

Current headline scores:

| Category | Score |
| --- | ---: |
| public_eval.bfcl_overall | 100.0 |
| public_eval.bfcl_case_pass_rate | 100.0 |
| public_eval.bfcl_core | 100.0 |
| public_eval.bfcl_agentic | 100.0 |
| public_eval.tau2_mock | 100.0 |

Scoring notes:

- `public_eval.bfcl_overall` is the official-style subcategory accuracy over the BFCL suites currently evaluated in this repository scope. It is not the raw case pass rate.
- `public_eval.bfcl_case_pass_rate` remains available as a diagnostic metric for individual-case success.
- `public_eval.bfcl_web_search` is tracked as normalized final-answer accuracy, with tool-call match rates kept as diagnostic signals.
- The repo-pinned `full_v4` BFCL slice is fully green in this snapshot, including the core multi-tool cases plus the added search-plus-contents and memory-backed cases.
- Raw `official_full_v4` manifests are now normalized from JSON or JSONL inputs before filtering and execution, without switching the README headline score away from the repo-pinned baseline.
- The provider compatibility matrix now covers OpenAI-compatible chat-completions and Responses API payload or parsing parity on top of the strict function-calling baseline.
- MCP catalog durability now includes `resource_templates`, prompt-detail cache entries, and notification-driven stale marking.

## Real-Network Snapshot

Latest generated snapshot timestamp: `2026-04-13T11:49:06Z`

| Test Set | Score | Duration (s) | Notes |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 0.9074 | well-known discovery and send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 7.4315 | loopback federation through the local A2A surface |
| real_network.disconnect_retry_chaos | 100.0 | 3.8996 | callback retry and signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 3.9662 | replay-safe callback and durable task events |
| real_network.workbench_reuse_process | 100.0 | 1.9268 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 32.2239 | container warm-start and snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 50.8435 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 20.5576 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 28.9500 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 6.5188 | replay/resume failure injection |

Warm-start telemetry summary:

| Metric | Value |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 5.7206 |
| telemetry.microvm_warm_start_average_seconds | 8.4611 |
| telemetry.snapshot_drift_ratio_average | 0.4033 |
| telemetry.snapshot_drift_ratio_max | 0.6701 |

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
- Targeted regressions around provider adapters, BFCL retry routing, raw official-manifest normalization, MCP catalog durability, and README snapshots
- Full unit coverage: `185 passed`
- Full real integration coverage: `6 passed`, `3 warnings`
- Real integration rerun completed outside the sandbox so live model/network and MCP-backed paths could be revalidated after the temp-root fix

Exact machine-local execution logs stay outside the repository-facing documentation surface.
