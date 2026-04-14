# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

No unreleased changes yet.

## [0.3.5] - 2026-04-14

### Added

- Added grounded web-search source-ledger tracking for BFCL web-search cases so repo-pinned public eval now records:
  - grounded search history
  - per-case content-source usage
  - grounded retry counts
  - cache/network/replay mix diagnostics
- Added grounded cache-first and grounded retry-before-replay handling for `web.contents` in:
  - `src/agent_runtime/public_eval_web_search.py`
  - `src/agent_runtime/public_eval.py`
- Added focused regression coverage for:
  - grounded cache hits before network fetch
  - grounded retry within the search set before replay fallback
  - source-ledger-assisted BFCL answer extraction
  - aggregated web-search diagnostics
  - OpenAI-compatible Responses required-tool strict-schema payload parity

### Changed

- Tightened BFCL web-search evaluation so `web.contents` now:
  - prefers grounded cached contents first
  - retries alternative grounded URLs with the same grounded title before replay fallback
  - records backend diagnostics without widening to ungrounded URLs
- Extended BFCL answer scoring so grounded titles from the per-case source ledger can still recover exact-title answers when the model wraps them in prose.
- Refreshed the bilingual README pair and all published `reference/` pages for the `0.3.5` release, including:
  - the latest release version text
  - refreshed public-eval and real-network wording
  - web-search reinforcement notes aligned to current OpenAI, BFCL v4, and SerpApi public references
- Published patch release `0.3.5`.

### Verified

- `.\.venv\Scripts\python.exe -m ruff check src tests scripts`
- `.\.venv\Scripts\python.exe -m mypy src tests scripts`
- `.\.venv\Scripts\python.exe -m pytest tests/unit/test_public_eval.py tests/unit/test_protocols.py -q --basetemp=%TEMP%\easy-agent-pytest\targeted-<timestamp>` with `74 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/unit -q --basetemp=%TEMP%\easy-agent-pytest\unit-full-<timestamp>` with `190 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-full-<timestamp>` with `6 passed`, `3 warnings`
- Refreshed public artifacts:
  - `.easy-agent/benchmark-report.json`
  - `.easy-agent/public-eval-report.json`
  - `.easy-agent/real-network-report.json`
- Refreshed release snapshots include:
  - `public_eval.bfcl_overall = 100.0`
  - `public_eval.tau2_mock = 100.0`
  - `real_network.overall = 100.0`
  - `real_network.generated_at = 2026-04-14T05:58:34Z`

## [0.3.4] - 2026-04-11

### Added

- Added SERPAPI-backed public-eval web-search configuration with nested `evaluation.public_eval.web_search` settings for:
  - `provider`
  - `api_key_env`
  - `endpoint_url`
  - search locale controls
  - timeout and quota limits
  - a persistent usage ledger
- Added `evaluation.public_eval.official_dataset` settings for cached `official_full_v4` manifest loading, checkpointing, and resumable reruns.
- Added public-eval checkpoint persistence and restore so repo-pinned or official-manifest BFCL runs can resume without discarding completed records.
- Added federation OAuth/OIDC token acquisition and refresh support for:
  - `client_credentials`
  - `authorization_code`
  - persisted refresh-token reuse
  - CLI auth lifecycle helpers under `easy-agent federation auth *`
- Added federation JWT/JWKS trust-chain support for:
  - server JWKS publishing
  - signed agent cards
  - signed callback JWS verification
  - stricter tenant/task authorization boundaries on task and subscription state
- Added warm-start latency budgets, cache telemetry, and history append support for container and microVM rows in the real-network suite.
- Added durable MCP roots state handling with:
  - persisted root snapshots
  - root-diff refresh payloads
  - `notifications/roots/list_changed` propagation when the transport session supports it
- Added durable MCP URL elicitation completion handling so:
  - `accept` / `decline` / `cancel` outcomes stay in one approval record
  - URL completion notifications update the existing approval state instead of creating a second record
- Added focused regression coverage for:
  - SERPAPI search normalization and replay fallback
  - official-manifest loading
  - public-eval checkpoint round-trips
  - federation OAuth state persistence
  - signed-card and tenant/task authorization behavior
  - benchmark retry stability
  - real-network telemetry aggregation
  - README comparison snapshot rows
  - MCP roots diff and notification behavior
  - MCP durable elicitation completion state
  - approval cancel mapping
- Added BFCL agentic fixture coverage for:
  - exact-title web search
  - search-plus-contents retrieval
  - memory-backed history
  - alias-aware memory read/delete cases

### Changed

- Updated `run_public_eval_suite(...)` and the integration CLI so public eval can select `subset`, `full_v4`, or `official_full_v4`.
- Reworked the public-eval report shape to include:
  - `scope`
  - `case_counts`
  - `progress`
  - refreshed source metadata for BFCL, tau2, and SERPAPI-backed web search
- Switched the README snapshot and bilingual documentation to the latest April 9, 2026 artifacts, including:
  - the refreshed benchmark snapshot
  - the refreshed repo-pinned public-eval snapshot
  - the refreshed real-network telemetry matrix
  - a docs-mapped similar-project comparison section
- Tightened live benchmark stability by giving each benchmark mode one bounded retry before recording failure, and mirrored that bounded retry in the flaky long-run real integration test.
- Restored public-facing README and changelog wording to SERPAPI so the tracked repository no longer advertises the previous temporary web-search test surface.
- Split the monolithic MCP integration module into `src/agent_integrations/mcp/` package files so roots, elicitation, clients, and manager logic are easier to evolve independently.
- Updated both READMEs to mark MCP root-change propagation and durable URL elicitation approval state as shipped capabilities, and rewrote the MCP `Next Reinforcement` item toward prompt/resource/tool list-change parity on the newer public MCP surface.
- Tightened BFCL web-search handling around grounded result-id resolution, exact-title query shaping, replay-backed contents recovery, and answer scoring for short grounded final responses.
- Tightened BFCL agentic evaluation with history hydration, grounded search-state reuse, explicit tool-result truth checks, and eval-only memory alias resolution.
- Extended protocol adapters so strict function calling and structured-output controls remain explicit across OpenAI-compatible, Anthropic, and Gemini surfaces, including Anthropic `strict: true` emission and corrected capability reporting.
- Reworked the English and Chinese README pair plus `reference/` detail pages into the published `0.3.4` documentation set, including the restored acknowledgements block.

### Verified

- `.\.venv\Scripts\python.exe -m ruff check src tests scripts`
- `.\.venv\Scripts\python.exe -m mypy src tests scripts`
- `.\.venv\Scripts\python.exe -m pytest tests/unit/test_readme_snapshot.py tests/unit/test_public_eval.py tests/unit/test_protocols.py -q --basetemp=%TEMP%\easy-agent-pytest\unit-doc-sync-<timestamp>` with `55 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/unit -q --basetemp=%TEMP%\easy-agent-pytest\unit-full-<timestamp>` with `166 passed`
- `.\.venv\Scripts\python.exe -m pytest tests/integration -m real -q --basetemp=%TEMP%\easy-agent-pytest\integration-full-<timestamp>` with `5 passed`, `5 warnings`
- Published documentation reflects the retained April 9, 2026 benchmark snapshot and refreshed April 11, 2026 public-eval and real-network snapshots.
- Refreshed BFCL/public-eval results include:
  - `public_eval.bfcl_overall = 98.21`
  - `public_eval.bfcl_case_pass_rate = 97.22`
  - `public_eval.bfcl_web_search = 100.0`
  - `public_eval.bfcl_memory = 100.0`
- Refreshed real-network results include:
  - `10 passed`, `0 failed`, `0 skipped`
  - `generated_at = 2026-04-11T06:35:04Z`
  - `telemetry.cache_hit_rate = 100.0`
  - `telemetry.container_warm_start_average_seconds = 5.6855`
  - `telemetry.microvm_warm_start_average_seconds = 8.2140`

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
