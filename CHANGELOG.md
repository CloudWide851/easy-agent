# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added public-A2A-aligned federation compatibility for well-known discovery, richer agent-card metadata, `pushNotificationConfig` set/get/list/delete helpers, and reconnect-safe `sendSubscribe` / `resubscribe` flows.
- Added broader federation CLI coverage for remote task inspection, cancellation, subscription management, push-notification management, and subscribe or resubscribe operations.
- Added repeatable host-backed executor coverage so the real-network suite now exercises offline container restore and `podman_machine` microVM recovery instead of leaving those rows as `skipped`.

### Changed

- Strengthened federation routing, discovery bootstrapping, SSE fallback handling, and task-state persistence so remote task delivery stays closer to the public A2A contract.
- Hardened executor and workbench flows with offline image preload, snapshot-style restore, resource quota controls, session restart or shutdown helpers, and broader host-environment variable passthrough for sandboxed processes.
- Tightened public-eval schema normalization and tool-call prompting to reduce provider-side schema failures on OpenAI-compatible endpoints, while keeping the current DeepSeek BFCL limitations documented.
- Refreshed the bilingual README pair to document shipped A2A compatibility, executor coverage, the latest real-network matrix, and next reinforcement items grounded in current A2A and MCP specifications.

### Verified

- `.\.venv\Scripts\python.exe -m ruff check src tests scripts`
- `.\.venv\Scripts\python.exe -m mypy src tests scripts`
- `.\.venv\Scripts\python.exe -m pytest tests/unit -q --basetemp=%TEMP%\easy-agent-pytest\unit-full-<timestamp>` with `76 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration/test_real_network_eval.py -q -m real --basetemp=%TEMP%\easy-agent-pytest\integration-real-network-<timestamp>` with `1 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-full-<timestamp>` with `4 passed, 1 skipped`
- Python helper calling `run_real_network_suite()` refreshed `.easy-agent/real-network-report.json` with `6 passed`, `0 failed`, `0 skipped`

## [0.3.2] - 2026-03-27

### Added

- Added `src/agent_integrations/executors.py` with named `process`, `container`, and `microvm` executor backends for long-lived workbench sessions.
- Added durable workbench runtime-state persistence so executor session metadata survives reuse, garbage collection, and forked resume flows.
- Added `src/agent_runtime/real_network_eval.py` plus `tests/integration/test_real_network_eval.py` to publish a real-network matrix covering:
  - cross-process federation
  - disconnect/retry chaos
  - process workbench reuse
  - host-gated container reuse
  - host-gated microVM reuse
  - replay/resume failure injection

### Changed

- Hardened OpenAI-compatible tool-schema sanitization to flatten `anyOf`/`oneOf`, list-typed `type`, and format-heavy MCP schemas before sending tool definitions to provider endpoints.
- Narrowed the shell-metacharacter guardrail so plain-text tools such as `python_echo` are not blocked by punctuation-only content.
- Adjusted MCP roots negotiation so stdio filesystem servers fall back to their configured allowed directories instead of advertising an incompatible server-roots capability.
- Updated both READMEs to stay synchronized for release `0.3.2`, publish the refreshed real-network matrix, refreshed benchmark/public-eval snapshots, and expand `Next Reinforcement` around current A2A/MCP protocol surfaces.

### Verified

- `.\.venv\Scripts\ruff.exe check src tests scripts`
- `.\.venv\Scripts\mypy.exe src tests scripts`
- `.\.venv\Scripts\python.exe -m pytest tests/unit -q --basetemp=%TEMP%\easy-agent-pytest\unit-full-<timestamp>` with `74 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-full-<timestamp>` with `5 passed`
- `.\.venv\Scripts\python.exe scripts\benchmark_modes.py --config easy-agent.yml --repeat 1 --output .easy-agent\benchmark-report.json`
- Python helper scripts refreshed:
  - `.easy-agent/public-eval-report.json`
  - `.easy-agent/real-network-report.json`
- Python CLI smoke remained covered through `CliRunner` for `--help`, `doctor`, `teams list`, `harness list`, and `federation list`

## [0.3.1] - 2026-03-27

### Added

- Added human-loop controls across the runtime with approval gates for sensitive tools, swarm handoffs, harness resume, MCP sampling, and MCP elicitation.
- Added interrupt requests, waiting/interrupted run states, durable approval storage, and CLI approval management through `easy-agent approvals *`.
- Added checkpoint listing, historical replay, and branchable `resume --fork` support for graph and team workflows, plus lineage tracking in SQLite traces.
- Added richer MCP support for explicit roots, backward-compatible filesystem-root inference for stdio servers, `streamable_http`, auth-aware remote transports, OAuth state persistence, and `easy-agent mcp roots/auth *` commands.
- Added A2A-style remote agent federation with exported local targets, remote inspection, durable federated task tracking, and CLI federation commands.
- Added executor/workbench isolation with per-run isolated roots, execution manifests, TTL cleanup, durable execution logs, and workbench CLI management.
- Added durable push-oriented federation lifecycle support with task event logs, SSE task-event streaming, webhook retry with backoff, subscription leasing, renewal, and cancellation.
- Added federation metadata negotiation through richer `agent-card` and `extended-agent-card` fields for protocol version, schema version, modalities, capabilities, auth hints, and compatibility metadata.

### Changed

- Optimized tool-calling behavior with duplicate successful tool-call suppression and stronger BFCL prompt guidance in the public-eval harness.
- Stabilized the harness worker/evaluator prompts and the public `configs/harness.example.yml` so the real harness integration converges more reliably within bounded cycles.
- Updated runtime and CLI federation surfaces so operators can inspect remote tasks, task events, and subscription state, and renew or cancel remote subscriptions.
- Updated both READMEs to stay synchronized, reflect the `0.3.x` release line, publish the March 27, 2026 real-network verification snapshot, and expand `Next Reinforcement` using current public A2A and MCP protocol references.

### Verified

- `.\.venv\Scripts\ruff.exe check src tests scripts`
- `.\.venv\Scripts\mypy.exe src tests scripts`
- `.\.venv\Scripts\python.exe -m pytest tests/unit -q --basetemp=%TEMP%\easy-agent-pytest\unit-full-<timestamp>` with `65 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-real-<timestamp>` with `4 passed`
- Python CLI smoke via `CliRunner` for `--help`, `doctor`, `teams list`, `harness list`, and `federation list`
- Fresh live public-eval snapshot written to `.easy-agent/public-eval-report.json` with `overall.bfcl_pass_rate = 0.4583`

## [0.3.0] - 2026-03-26

### Added

- Added explicit guardrail hooks before tool execution and before final output emission.
- Added schema-aware tool-call validation with a repair loop for invalid model-emitted arguments.
- Added enriched runtime event streaming and tracing coverage across run, agent, team, tool, guardrail, and MCP boundaries.
- Added a public evaluation harness for vendored BFCL subset cases and tau2 mock cases.
- Added `tests/integration/test_public_eval_real.py` for live public-eval smoke coverage.

### Changed

- Hardened the long-run real suite prompts and node timeouts for stable MCP-backed graph execution on Windows.
- Normalized BFCL tool names and schemas for OpenAI-compatible providers, and added tau2 prompt fallback for history-based cases.
- Stabilized live team and long-run integration tests against single-run model drift and overly long temp-root paths.
- Reworked the README set to document guardrails, event streaming, public evaluation, and the latest measured live results.
- Removed the Linux.do icon from acknowledgements while keeping the Linux.do link and DeepSeek acknowledgement badge.

### Verified

- `ruff check src tests scripts`
- `mypy src tests scripts`
- `pytest tests/unit -q`
- `pytest tests/integration -m real -q`
- `easy-agent --help`
- `easy-agent doctor -c easy-agent.yml`
- `easy-agent teams list -c configs/teams.example.yml`
- Live benchmark snapshot written to `.easy-agent/benchmark-report.json`
- Live public-eval snapshot written to `.easy-agent/public-eval-report.json`

## [0.2.0] - 2026-03-25

### Added

- Added `Agent Teams` with `round_robin`, `selector`, and `swarm` collaboration modes.
- Added team-aware graph scheduling so `graph.entrypoint` and graph nodes can target teams.
- Added team-aware CLI visibility through `easy-agent teams list` and richer `doctor` output.
- Added `configs/teams.example.yml` as the baseline multi-role team example.
- Added real integration coverage for team modes with `tests/integration/test_teams_real.py`.
- Added `CHANGELOG.md` and rewrote the README in bilingual Chinese and English form.

### Changed

- Strengthened config validation for agent names, team names, node names, team membership, and selector/swarm descriptions.
- Extended benchmark coverage from three modes to six modes, including all team execution paths.
- Updated documentation to include plugin mounting, sandboxing, real MCP validation, Windows launchers, and live benchmark results.
- Clarified the repository structure as a white-box, business-agnostic Agent foundation.

### Verified

- `ruff check src tests scripts`
- `mypy src tests scripts`
- `pytest tests/unit`
- `pytest tests/integration -m real`
- `easy-agent --help`
- `easy-agent teams list -c configs/teams.example.yml`
- `easy-agent doctor -c configs/teams.example.yml`
- Windows launcher smoke via `easy-agent.ps1` and `easy-agent.bat`
