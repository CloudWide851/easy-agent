<p align="center">
  <img src="./logo.svg" alt="easy-agent logo" width="160">
</p>

<h1 align="center">easy-agent</h1>

<p align="center">
  A white-box Python foundation for inspectable, testable, and extensible agent runtimes.
</p>

<p align="center">
  <a href="./README.md">English</a> |
  <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="uv managed" src="https://img.shields.io/badge/uv-managed-4B5563">
  <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-059669">
  <img alt="Release line" src="https://img.shields.io/badge/Release-0.3.x-2563EB">
</p>

`easy-agent` is the runtime layer underneath an agent product, not the product itself. It keeps orchestration, tool calling, persistence, approvals, federation, and evaluation explicit so teams can evolve their systems without hiding critical behavior behind opaque framework abstractions.

The latest published patch remains `0.3.3`. This repository state contains unreleased work on top of that line.

## What This Project Is

Most agent projects move quickly from "call a model" to "ship an application". The runtime layer in the middle then accumulates hidden assumptions around tools, memory, approvals, transport, and recovery.

`easy-agent` exists to keep that middle layer explicit:

- It separates runtime engineering from product logic.
- It keeps scheduling, orchestration, and protocol adaptation inspectable.
- It lets you mount tools, skills, MCP servers, and plugins without rewriting the core.
- It provides durable harnesses, checkpoints, and replay instead of relying on one oversized prompt.

## Who It Is For

- Engineering teams building agent products that need a reusable runtime instead of a one-off demo.
- Developers who want direct control over tool calling, approvals, persistence, and resume behavior.
- Projects that need to evolve with provider APIs, MCP, and multi-agent patterns over time.

## Tech Stack

- Runtime: Python `3.12`, `uv`, `AnyIO`, `Typer`
- Model surface: OpenAI-compatible, Anthropic-style, and Gemini-style payload adaptation
- Persistence: SQLite + JSONL traces
- Integration surface: direct tools, command skills, Python hook skills, MCP, plugins
- Isolation surface: process, container, and microVM workbench executors

## Features

- White-box runtime layers for scheduler, orchestrator, tool registry, storage, and protocol adapters.
- Support for `single_agent`, `sub_agent`, graph workflows, `Agent Teams`, and long-running harnesses.
- Session memory, checkpoints, replay, branchable resume, and approval-aware recovery.
- Guardrails, schema-aware tool validation, runtime event streaming, and persistent traces.
- A2A-style remote federation with durable task state and signed callback verification.
- Public evaluation helpers for benchmark, BFCL, tau2 mock, provider-schema compatibility, and real-network regression tracking.

## Human Loop, Replay, and MCP

`easy-agent` already ships the reliability controls that many projects leave as future work:

- Sensitive tools, swarm handoffs, and resumptions can enter a durable approval flow.
- Runs expose safe-point interrupts, checkpoint listing, replay, and forked resume.
- MCP integrations support explicit roots, root snapshots, `notifications/roots/list_changed`, elicitation approval state, `streamable_http`, and persisted OAuth state.

Reference:
- Detailed usage: [reference/en/usage-guide.md](./reference/en/usage-guide.md)
- Detailed reinforcement plan: [reference/en/next-reinforcement.md](./reference/en/next-reinforcement.md)

## A2A Remote Agent Federation

The federation layer publishes local agents, teams, and harnesses through a durable A2A-style surface:

- Well-known discovery, richer cards, push or poll delivery, retry, and resubscribe flows.
- OAuth/OIDC token acquisition and refresh for remote federation clients.
- JWKS/JWS validation for signed cards and signed callbacks.
- Stricter tenant/task authorization boundaries before federated state is revealed or mutated.

Operational detail and comparison notes are documented in [reference/en/test-results.md](./reference/en/test-results.md).

## Executor / Workbench Isolation

The executor/workbench layer gives long-lived tools and MCP subprocesses a reusable runtime boundary:

- Named executors for `process`, `container`, and `microvm`.
- Persistent workbench sessions, manifests, snapshots, and TTL cleanup.
- Real-network regression coverage for warm-start latency and snapshot drift.

Detailed operational notes are documented in [reference/en/usage-guide.md](./reference/en/usage-guide.md).

## Architecture

The runtime is intentionally modular and observable:

- `scheduler` coordinates direct-agent and graph execution.
- `orchestrator` runs agent and team turns.
- `harness` manages initializer, worker, and evaluator loops.
- `registry` exposes tools, skills, MCP tools, and mounted plugins.
- `storage` persists runs, checkpoints, approvals, sessions, federation state, and workbench state.

```mermaid
flowchart LR
    User[User] --> CLI[Typer CLI]
    CLI --> Runtime[EasyAgentRuntime]
    Runtime --> Scheduler[GraphScheduler]
    Runtime --> Harness[HarnessRuntime]
    Scheduler --> Orchestrator[AgentOrchestrator]
    Harness --> Orchestrator
    Orchestrator --> Registry[ToolRegistry]
    Orchestrator --> Store[SQLiteRunStore]
    Orchestrator --> Client[HttpModelClient]
    Client --> Adapter[ProtocolAdapter]
    Adapter --> Provider[Provider API]
```

## Long-Running Harness Design

Harnesses are first-class runtime objects rather than prompt conventions. Each harness defines:

- an `initializer_agent`
- a `worker_target`
- an `evaluator_agent`
- an explicit `completion_contract`

The worker loop persists artifacts and checkpoints so long-running tasks can continue, replan, or resume without discarding state.

## Protocol and Tool Model

- Model protocols: OpenAI-compatible, Anthropic-style, and Gemini-style payload normalization.
- Tool calling: strict schema normalization, validation-repair loops, and provider-schema compatibility telemetry.
- Web-search eval hardening: SerpApi `/search.json`, replay-backed contents, quota ledger, result grounding, and single-call regression guards.

Provider behavior details and structured-output notes live in [reference/en/next-reinforcement.md](./reference/en/next-reinforcement.md).

## Project Layout

```text
src/
  agent_cli/
  agent_common/
  agent_config/
  agent_graph/
  agent_integrations/
  agent_protocols/
  agent_runtime/
skills/
configs/
tests/
reference/
  en/
  zh/
```

## Quick Start

```bash
uv venv --python 3.12
uv sync --dev
uv run easy-agent --help
uv run easy-agent doctor -c easy-agent.yml
```

Detailed setup, local credentials, CLI commands, and examples:
- [reference/en/usage-guide.md](./reference/en/usage-guide.md)

## What a Harness Run Produces

A harness run persists durable artifacts under `.easy-agent/` and session storage, including:

- bootstrap and progress markdown
- feature snapshots
- checkpoints and replay state
- workbench session metadata

Artifact details are documented in [reference/en/usage-guide.md](./reference/en/usage-guide.md).

## Verification

This unreleased round keeps the previous benchmark and public-eval artifacts, while the detailed Python verification and real-network validation are tracked separately. The full command log, exact artifact notes, and similar-project comparison live in [reference/en/test-results.md](./reference/en/test-results.md).

### Score Summary

| Test Set | Score |
| --- | ---: |
| benchmark.single_agent | 100.0 |
| benchmark.sub_agent | 100.0 |
| benchmark.multi_agent_graph | 100.0 |
| benchmark.team_round_robin | 100.0 |
| benchmark.team_selector | 100.0 |
| benchmark.team_swarm | 100.0 |
| public_eval.bfcl_simple | 100.0 |
| public_eval.bfcl_multiple | 87.5 |
| public_eval.bfcl_parallel_multiple | 100.0 |
| public_eval.bfcl_irrelevance | 100.0 |
| public_eval.bfcl_web_search | 0.0 |
| public_eval.bfcl_memory | 0.0 |
| public_eval.bfcl_format_sensitivity | 100.0 |
| public_eval.tau2_mock | 100.0 |

## Real Network Test Set Results

The real-network matrix is reported as score-only in this README. Durations, telemetry, warm-start budgets, and snapshot-drift detail are tracked in [reference/en/test-results.md](./reference/en/test-results.md).

| Test Set | Score |
| --- | ---: |
| real_network.cross_process_federation | 100.0 |
| real_network.live_model_federation_roundtrip | 100.0 |
| real_network.disconnect_retry_chaos | 100.0 |
| real_network.duplicate_delivery_replay_resilience | 100.0 |
| real_network.workbench_reuse_process | 100.0 |
| real_network.workbench_reuse_container | 100.0 |
| real_network.workbench_incremental_snapshot_reuse_container | 100.0 |
| real_network.workbench_reuse_microvm | 100.0 |
| real_network.workbench_incremental_snapshot_reuse_microvm | 100.0 |
| real_network.replay_resume_failure_injection | 100.0 |

## Next Reinforcement

The next reinforcement track is documented in full at [reference/en/next-reinforcement.md](./reference/en/next-reinforcement.md). The near-term focus remains:

- further compressing BFCL web-search misses around query shaping, grounding, and replay-backed contents
- tightening provider compatibility around OpenAI function calling and structured outputs
- expanding durable MCP and federation coordination without widening the public runtime surface unnecessarily

## Design References

- OpenAI function calling: <https://platform.openai.com/docs/guides/function-calling?api-mode=chat>
- OpenAI structured outputs: <https://platform.openai.com/docs/guides/structured-outputs>
- Model Context Protocol: <https://modelcontextprotocol.io/specification>
- SerpApi Search API: <https://serpapi.com/search-api>
- FastAPI README style reference: <https://github.com/fastapi/fastapi>
- uv README style reference: <https://github.com/astral-sh/uv>

## Acknowledgements

- OpenAI, Anthropic, Google, and the MCP ecosystem for the protocol and interoperability surface this runtime tracks.
- SerpApi for the replay-compatible search transport used in BFCL web-search evaluation.
- Linux.do for early community discussion around practical agent engineering.

## License

MIT. See [LICENSE](./LICENSE).
