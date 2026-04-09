# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added explicit `model.function_calling` config with `strict` and `parallel_tool_calls` controls for OpenAI-compatible providers.
- Added strict-schema compatibility coverage for:
  - recursive `additionalProperties: false`
  - required nullable modeling for optional fields
  - explicit nullable field preservation
  - single-call versus parallel-call control surfaces
- Added regression coverage for duplicate tool-call suppression when a later call is an optional subset of an earlier successful call.

### Changed

- Hardened the shared JSON-schema normalizer so the OpenAI-compatible path can emit strict-friendly schemas while keeping the broader non-strict cleanup path for other providers.
- Updated the OpenAI-compatible adapter to emit `strict: true` by default, forward `parallel_tool_calls`, and keep the schema payload aligned with the stricter OpenAI function-calling and structured-output constraints.
- Updated the public-eval harness so BFCL and tau runs can control `parallel_tool_calls` per scenario, and refreshed the provider schema matrix to report strict-mode capabilities more explicitly.
- Tightened benchmark execution defaults around function-calling strictness and serialized tool execution so the live benchmark suite stays more stable.
- Refreshed `README.md` and `README.zh-CN.md` together for the April 9, 2026 verification pass, including the latest benchmark, public-eval, and real-network artifacts plus expanded `Next Reinforcement` items.

### Verified

- `.\.venv\Scripts\python.exe -m ruff check src tests scripts`
- `.\.venv\Scripts\python.exe -m mypy src tests scripts`
- `.\.venv\Scripts\python.exe -m pytest tests/unit -q --basetemp=%TEMP%\easy-agent-pytest\unit-full-<timestamp>` with `117 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration/test_public_eval_real.py -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-public-eval-<timestamp>` with `1 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration/test_real_network_eval.py -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-real-network-<timestamp>` with `1 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration/test_teams_real.py -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-teams-<timestamp>` with `1 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-full-<timestamp>` with `5 passed`
- `.\.venv\Scripts\python.exe scripts\benchmark_modes.py --config easy-agent.yml --repeat 1 --output .easy-agent\benchmark-report.json`
- Repo-local Python helper refreshed `.easy-agent/public-eval-report.json` with `overall.bfcl_pass_rate = 0.9167` and `tau2_mock_pass_rate = 1.0000`
- `.easy-agent/real-network-report.json` refreshed with `10 passed`, `0 failed`, `0 skipped`

## [0.3.3] - 2026-04-01

### Added

- Added `src/agent_common/schema_utils.py` so protocol adapters, MCP integration, and public-eval all reuse the same JSON-schema normalization rules.
- Added risk-aware MCP sampling and elicitation handling with deferred approval escalation for high-risk remote requests plus richer form / URL elicitation payload processing.
- Added provider-aware BFCL fallback tracking in public eval with `fallback_stage`, `fallback_attempts`, candidate-pruned retry paths for OpenAI-compatible `400` responses, stage summaries, failure buckets, and a provider schema compatibility matrix.
- Added federation security negotiation helpers for `securitySchemes` / `security`, callback signing plus audience headers, cursor page-token encoding, and optional client-side mTLS handshake kwargs.
- Added `src/agent_integrations/github_automation.py` with local GitHub automation helpers for:
  - `github_issue_list`
  - `github_issue_prepare_fix`
  - `git_commit_local`
  - `github_release_publish`
- Added optional local skill-path loading so `.easy-agent/local-skills/github_automation` can stay untracked while still mounting repo-specific automation when present.
- Extended the real-network suite with:
  - `live_model_federation_roundtrip`
  - `duplicate_delivery_replay_resilience`
  - `workbench_incremental_snapshot_reuse_container`
  - `workbench_incremental_snapshot_reuse_microvm`
- Added Windows-safe HTTP client close handling so successful live-model real-network runs are not downgraded by `Event loop is closed` cleanup noise.

### Changed

- Switched OpenAI-compatible tool-schema sanitization to the shared schema normalizer and tightened BFCL schema coercion for complex function definitions.
- Updated the inline CLI approval resolver so MCP form elicitation responses are collected and validated as structured JSON instead of being treated as free-form text.
- Moved the default coordinator tool order in `easy-agent.yml` so GitHub issue listing, repair-package prep, local commit, and release publishing are available before the demo echo tools.
- Tightened duplicate successful tool-call suppression so a second call that only adds optional schema-declared arguments reuses the first successful result instead of executing again.
- Grounded tau public-eval cases more aggressively from prior tool history by extracting known task ids into a synthetic memory message and prompt-grounding the latest discussed task.
- Hardened federation client delivery so `run_remote()` auto-discovers the remote base path before sending tasks, fixed the real-network replay resilience scenario to read the task payload returned by `get_task()` correctly, and normalized callback-token checks to be case-insensitive.
- Extended federation client and server negotiation with richer `agent-card` / `extended-agent-card` metadata, `ListTasks` / `ListTaskEvents` cursor pagination, signed webhook delivery, callback audience handling, and fail-fast remote auth readiness checks for bearer, header, OAuth/OIDC, and optional mTLS paths.
- Refreshed the bilingual README pair for the March 31, 2026 verification pass, synchronized the latest real-network and public-eval artifacts, and rewrote `Next Reinforcement` against the latest public A2A, MCP, and OpenAI tool-calling surfaces while keeping the older benchmark artifact marked as a retained snapshot.

### Verified

- `.\.venv\Scripts\python.exe -m ruff check src tests scripts`
- `.\.venv\Scripts\python.exe -m mypy src tests scripts`
- `.\.venv\Scripts\python.exe -m pytest tests/unit -q --basetemp=%TEMP%\easy-agent-pytest\unit-final-<timestamp>` with `113 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration/test_real_network_eval.py -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-real-network-final-<timestamp>` with `1 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration/test_public_eval_real.py -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-public-eval-<timestamp>` with `1 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-full-final-<timestamp>` with `5 passed`
- Repo-local Python helpers refreshed:
  - `.easy-agent/public-eval-report.json` with `overall.bfcl_pass_rate = 0.8750` and `tau2_mock_pass_rate = 1.0000`
  - `.easy-agent/real-network-report.json` with `10 passed`, `0 failed`, and `0 skipped` across 10 scenarios
- The full real integration suite still emitted known Windows asyncio cleanup warnings after success; they were treated as cleanup debt rather than functional failures.

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
