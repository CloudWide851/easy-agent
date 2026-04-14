# Test Results

## Snapshot Policy

- Release `0.3.5` publishes benchmark, public-eval, Python verification, and real-network snapshots refreshed on April 14, 2026.
- Public docs in this repository intentionally expose methodology and scores only; local collaboration logs are not part of the repository-facing surface.

## Benchmark Snapshot

| Test Set | Score | Avg Duration (s) |
| --- | ---: | ---: |
| benchmark.single_agent | 100.0 | 5.0674 |
| benchmark.sub_agent | 100.0 | 59.2087 |
| benchmark.multi_agent_graph | 100.0 | 12.6349 |
| benchmark.team_round_robin | 100.0 | 9.9354 |
| benchmark.team_selector | 100.0 | 13.9754 |
| benchmark.team_swarm | 100.0 | 11.7101 |

## Public Eval Snapshot

| Test Set | Score | Avg Duration (s) |
| --- | ---: | ---: |
| public_eval.bfcl_simple | 100.0 | 5.0554 |
| public_eval.bfcl_multiple | 100.0 | 6.3535 |
| public_eval.bfcl_parallel_multiple | 100.0 | 8.7009 |
| public_eval.bfcl_irrelevance | 100.0 | 4.3747 |
| public_eval.bfcl_web_search | 100.0 | 6.9273 |
| public_eval.bfcl_memory | 100.0 | 3.9823 |
| public_eval.bfcl_format_sensitivity | 100.0 | 4.1343 |
| public_eval.tau2_mock | 100.0 | 4.9205 |

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
- Raw `official_full_v4` manifests are normalized from JSON or JSONL inputs before filtering and execution, without switching the README headline score away from the repo-pinned baseline.
- The provider compatibility matrix covers OpenAI-compatible chat-completions and Responses API payload or parsing parity on top of the strict function-calling baseline.
- MCP catalog durability includes `resource_templates`, prompt-detail cache entries, and notification-driven stale marking.

Web-search diagnostics from the April 14, 2026 release refresh:

| Metric | Value |
| --- | ---: |
| web_search.content_sources.cache | 0 |
| web_search.content_sources.network | 0 |
| web_search.content_sources.replay | 2 |
| web_search.grounded_retry_count | 0 |
| web_search.grounded_sources_average | 1.4 |

Interpretation notes:

- This release keeps the repo-pinned BFCL web-search slice green while exposing the search or contents source mix separately from the headline pass rate.
- On this machine, the release refresh completed through replay-backed BFCL web-search evidence rather than live SerpApi results, which is reflected in the published diagnostics instead of being hidden behind a simple pass.

## Real-Network Snapshot

Latest generated snapshot timestamp: `2026-04-14T05:58:34Z`

| Test Set | Score | Duration (s) | Notes |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 1.6871 | well-known discovery and send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 11.7853 | loopback federation through the local A2A surface |
| real_network.disconnect_retry_chaos | 100.0 | 10.4526 | callback retry and signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 6.3195 | replay-safe callback and durable task events |
| real_network.workbench_reuse_process | 100.0 | 3.1016 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 34.8270 | container warm-start and snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 51.1865 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 20.9947 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 29.3842 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 7.1407 | replay/resume failure injection |

Warm-start telemetry summary:

| Metric | Value |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 5.7820 |
| telemetry.microvm_warm_start_average_seconds | 9.1022 |
| telemetry.snapshot_drift_ratio_average | 0.5162 |
| telemetry.snapshot_drift_ratio_max | 0.6795 |

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
- Targeted regressions around provider adapters and web-search or BFCL evaluation: `74 passed`
- Full unit coverage: `190 passed`
- Full real integration coverage: `6 passed`, `3 warnings`
- Live benchmark, public-eval, and real-network artifacts were refreshed for this release
- Real integration reran outside the sandbox so live model/network and MCP-backed paths could be revalidated end to end

Exact machine-local execution logs stay outside the repository-facing documentation surface.
