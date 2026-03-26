# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
