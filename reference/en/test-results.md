# Test Results

## Snapshot Policy

- Release `0.3.6` keeps the April 14 benchmark and headline public-eval score snapshot while publishing the April 30 Python verification refresh and workflow/browser/dashboard/failure-to-fix CLI checks, including the expanded `runs inspect` Markdown/HTML export, run notes, `workflow validate/explain`, MCP static doctor/test, federation graph export, template filtering/show/recommendation, cost reports, the read-only local console, and Python facade workflow/browser/bundle/dashboard/cost helpers.
- The live provider compatibility matrix remains the April 20 verification snapshot.
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
- `browsecomp_subset`, `simpleqa_subset`, and `simple_evals_subset` are now supported as local reinforcement profiles, but they are not part of the retained headline score snapshot because this repository does not vendor those benchmark questions.
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

## Provider Compatibility Live Verification

This matrix is the latest April 20, 2026 live verification pass, separate from the retained April 14 benchmark and headline public-eval score snapshot.

| Target | Status | Notes |
| --- | --- | --- |
| openai_live | passed | Required DeepSeek/OpenAI-compatible baseline passed on `chat_completions`; strict-schema, `tool_choice: none`, required-tool, and forced-tool checks all passed. |
| anthropic_live | skipped | Optional target; no local `ANTHROPIC_API_KEY` was present in this verification pass. |
| gemini_live | skipped | Optional target; no local `GEMINI_API_KEY` was present in this verification pass. |

Compatibility notes:

- `single_tool_call_control` is now reported as `best_effort` for non-OpenAI OpenAI-compatible providers instead of incorrectly failing the whole provider row when the field is exposed but not enforced strictly at runtime.
- BFCL web-search query arguments now support the `x-easy-agent-normalizer: web_search_query` path so wrapper text can be normalized before scoring without weakening exact answer checks.

## Real-Network Snapshot

Latest generated snapshot timestamp: `2026-04-29T08:32:11Z`

| Test Set | Score | Duration (s) | Notes |
| --- | ---: | ---: | --- |
| real_network.cross_process_federation | 100.0 | 1.1644 | well-known discovery and send/poll federation |
| real_network.live_model_federation_roundtrip | 100.0 | 8.5967 | loopback federation through the local A2A surface |
| real_network.disconnect_retry_chaos | 100.0 | 5.1623 | callback retry, push notifications, and signed webhook delivery |
| real_network.duplicate_delivery_replay_resilience | 100.0 | 5.1668 | replay-safe callback and durable task events |
| real_network.workbench_reuse_process | 100.0 | 2.1623 | process workbench reuse |
| real_network.workbench_reuse_container | 100.0 | 29.8542 | container warm-start and snapshot restore |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 | 46.8568 | incremental container snapshot reuse |
| real_network.workbench_reuse_microvm | 100.0 | 16.7065 | SSH-backed microVM reuse |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 | 24.5266 | incremental microVM snapshot reuse |
| real_network.replay_resume_failure_injection | 100.0 | 5.6834 | replay/resume failure injection |

Warm-start telemetry summary:

| Metric | Value |
| --- | ---: |
| telemetry.cache_hit_rate | 100.0 |
| telemetry.container_warm_start_average_seconds | 4.6869 |
| telemetry.microvm_warm_start_average_seconds | 6.8618 |
| telemetry.snapshot_drift_ratio_average | 0.4081 |
| telemetry.snapshot_drift_ratio_max | 0.7116 |

Scenario proof fields are now emitted with each real-network record so score rows can be traced back to an executable scenario contract:

| Scenario | Command | Expected Artifact | Pass Criteria |
| --- | --- | --- | --- |
| resume after failure | `uv run easy-agent integration real-network` | real-network report row for replay/resume failure injection | checkpoint replay or resume completes without rerunning completed work |
| human approval pending then continue | `uv run easy-agent run ... --approval-mode deferred` plus `uv run easy-agent approvals approve ...` | run summary, approval record, and trace tree | sensitive work enters durable approval and resumes after approval |
| MCP server restart | `uv run easy-agent mcp resources list ...` after transport refresh | MCP catalog snapshot and subscription state | catalog entries, prompt details, and subscription state survive refresh or restart |
| provider tool schema rejection then repair | `uv run easy-agent integration public-eval --profile full_v4` | public-eval provider matrix and failure-stage diagnostics | provider schema rejection enters strict-schema repair or classified best-effort evidence |
| federation disconnect and retry | `uv run easy-agent integration real-network` | real-network row for disconnect retry chaos | callback retry, signed delivery, sendSubscribe, and resubscribe remain durable |
| workbench snapshot restore | `uv run easy-agent integration real-network` | real-network workbench restore rows | process, container, or microVM sessions restore state within the configured budget |

The same report carries security assertions for executor and federation rows, including credential redaction, loopback-only test servers, signed callback verification, scoped workbench roots, and explicit host-gated dependencies.

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
- Targeted regressions around setup preflight, wizard, config explanation, config doctor, searchable HTML trace export, local trace opening, latest-report summarization and HTML export, static dashboard export, suggested dashboard next steps, copyable dashboard inspect/bundle commands, dashboard workflow/template recommendations, report trend, cost reports, read-only console dry run, experimental OTel trace export, connector diagnostics, MCP static doctor/test, federation graph export, workflow packs, workflow YAML init/doctor/validate/explain/plan/run, run triage/inspect/notes/bundle, Playwright MCP browser readiness, browser doctor/artifact/smoke/snapshot/audit/seo/a11y/links/report helpers, browser task packs, local skill catalog, plugin doctor, Python facade workflow/browser/bundle/dashboard/cost embedding, mock provider, onboarding CLI, scenario creation, new business templates, run explanation/fix packages, provider compatibility, config validation, guardrails, BFCL evaluation, official-source search, and simple-evals profile support: latest focused set `33 passed`; retained compatibility sets `55 passed`, `23 passed`, `36 passed`, `89 passed`, and `4 passed`
- Full unit coverage: `233 passed`
- Targeted live provider-compatibility regression: `1 passed`
- Full real integration coverage: `7 passed`, `2 warnings`
- The retained benchmark and headline public-eval scores still point at the April 14 release snapshot, the live provider-compatibility evidence remains the April 20 snapshot, and the detailed real-network artifact remains the April 29 snapshot while the April 30 live integration command revalidated the suite
- The remaining warnings are known Windows asyncio subprocess cleanup warnings after successful completion
- New focused regressions cover setup preflight, wizard scenario creation, template `workflow.yml` generation, template metadata filters/show/recommendation, browser template Playwright MCP config, browser doctor/artifact/smoke/snapshot/audit/seo/a11y/links/report helpers, browser task packs, workflow packs, workflow YAML init/doctor/validate/explain/plan/run, MCP static doctor/test, federation graph export, run triage, run inspect Markdown/HTML export, run notes, run bundle evidence export, config explanation, config doctor, searchable HTML trace export, `traces open`, `report latest`, `report costs`, enhanced cost-aware and copyable-command `dashboard`, read-only `console --dry-run`, dashboard workflow/template recommendation sections, `report trend`, latest-report HTML export, experimental OTel trace export, connector diagnostics, local skill catalog install, plugin doctor, offline mock runs, starter templates, `new <scenario>` for coding/research/data/ops/browser/web-monitor/SEO/website-audit/website-release/API-regression/incident-review/GitHub-issue/GitHub-PR/daily-report/weekly-report/data-quality/competitor-research/meeting-notes/content-pipeline/support/sales/document/QA/release starters, Python `AgentApp` workflow/browser/bundle/dashboard/cost helpers, quickstart, run explanation, advice-only JSON/Markdown/HTML run fix packages, browser-MCP failure classification, run listing, run summary, structured trace tree export, executor capability reports, storage contracts, and real-network scenario proof metadata.
- CLI smoke passed for `workflow list`, `workflow init`, `workflow doctor`, `workflow plan`, `workflow run workflow.yml --dry-run`, `workflow run --dry-run`, `runs triage`, `runs inspect`, `runs bundle`, enhanced `dashboard`, `browser smoke`, `browser snapshot`, `browser audit`, `browser seo`, `browser a11y`, `browser links`, `browser report`, `browser doctor`, `browser artifacts`, `connectors doctor`, `connectors test browser`, `task show repo-review`, `task show browser-qa`, `report trend`, `skills catalog list`, and `new web-monitor-agent`.

Exact machine-local execution logs stay outside the repository-facing documentation surface.
