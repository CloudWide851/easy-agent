# easy-agent

[English](./README.md) | [简体中文](./README.zh-CN.md)

`easy-agent` is a white-box, business-agnostic, extensible Python foundation for building agents. It focuses on runtime engineering instead of domain logic, so teams, sub-agents, skills, MCP servers, plugins, session memory, and future protocol changes can be mounted without coupling the framework to a single product.

## Tech Stack

<table>
  <tr>
    <td valign="top" width="25%">
      <strong>Runtime</strong><br>
      <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white"><br>
      <img alt="uv" src="https://img.shields.io/badge/uv-managed-4B5563"><br>
      <img alt="AnyIO" src="https://img.shields.io/badge/AnyIO-async-0F766E"><br>
      <img alt="Typer" src="https://img.shields.io/badge/Typer-CLI-059669">
    </td>
    <td valign="top" width="25%">
      <strong>Agent Core</strong><br>
      <img alt="Protocols" src="https://img.shields.io/badge/Protocols-OpenAI%20%7C%20Anthropic%20%7C%20Gemini-2563EB"><br>
      <img alt="Teams" src="https://img.shields.io/badge/Teams-round__robin%20%7C%20selector%20%7C%20swarm-7C3AED"><br>
      <img alt="Graph" src="https://img.shields.io/badge/Graph-DAG%20scheduler-9333EA"><br>
      <img alt="Tools" src="https://img.shields.io/badge/Tools-tool%20calling%202.0-1D4ED8">
    </td>
    <td valign="top" width="25%">
      <strong>Integration</strong><br>
      <img alt="Skills" src="https://img.shields.io/badge/Skills-Python%20hook%20%7C%20command-F59E0B"><br>
      <img alt="MCP" src="https://img.shields.io/badge/MCP-stdio%20%7C%20HTTP%2FSSE-DC2626"><br>
      <img alt="Plugins" src="https://img.shields.io/badge/Plugins-path%20%7C%20manifest%20%7C%20entry%20point-0891B2"><br>
      <img alt="Sandbox" src="https://img.shields.io/badge/Sandbox-process%20%7C%20windows__sandbox-374151">
    </td>
    <td valign="top" width="25%">
      <strong>State & Observability</strong><br>
      <img alt="Storage" src="https://img.shields.io/badge/Storage-SQLite%20%2B%20JSONL-0EA5E9"><br>
      <img alt="Guardrails" src="https://img.shields.io/badge/Guardrails-tool__input%20%2B%20final__output-DC2626"><br>
      <img alt="Streaming" src="https://img.shields.io/badge/Streaming-runtime__events-0284C7"><br>
      <img alt="Recovery" src="https://img.shields.io/badge/Recovery-session__memory%20%2B%20checkpoints-16A34A">
    </td>
  </tr>
</table>

## Why This Project Exists

- Keep agent infrastructure white-box and easy to extend.
- Separate agent engineering concerns from business logic.
- Provide one runtime that can host direct tools, skills, MCP, plugins, teams, session memory, and graph workflows.
- Keep the runtime easy to evolve as protocols, tool schemas, and orchestration patterns improve.

## Features

- White-box runtime assembly with explicit scheduler, orchestrator, registry, storage, guardrail, and protocol layers.
- Protocol-adapted model requests for `OpenAI`, `Anthropic`, and `Gemini` style payloads.
- Tool Calling 2.0 oriented runtime with direct tools, command skills, Python hook skills, MCP tools, and plugin mounting.
- `single_agent`, `sub_agent`, `multi_agent_graph`, and `Agent Teams` collaboration modes.
- Explicit guardrail hooks before tool execution and before final output emission.
- Schema-aware tool-call validation plus a self-repair loop when the model emits invalid arguments.
- Session-oriented memory with durable `session_id` support for message history and shared graph state.
- Resumable checkpoints for long-running graph workflows and top-level team workflows.
- Enriched tracing and event streaming across run, agent, team, tool, guardrail, and MCP boundaries.
- Sandboxed execution for command skills and `stdio` MCP with Windows fallback handling.
- Public evaluation harnesses for BFCL subset cases and tau2 mock cases.

## Current Architecture

### Runtime Topology

```mermaid
flowchart LR
    User[User or Script] --> CLI[Typer CLI]
    CLI --> Runtime[EasyAgentRuntime]
    Runtime --> Scheduler[GraphScheduler]
    Scheduler --> Orchestrator[AgentOrchestrator]
    Scheduler --> Guardrails[GuardrailEngine]
    Orchestrator --> Registry[ToolRegistry]
    Orchestrator --> Model[HttpModelClient]
    Model --> Adapter[ProtocolAdapter]
    Adapter --> Provider[Provider API]
    Runtime --> Plugins[Plugin Host]
    Plugins --> Registry
    Runtime --> MCP[McpClientManager]
    MCP --> Stdio[stdio transport]
    MCP --> Remote[HTTP_SSE transport]
    Registry --> Skills[Skills and Local Tools]
    Scheduler --> Store[SQLiteRunStore]
    Orchestrator --> Store
    Runtime --> Stream[Event Stream]
    Stream --> CLI
    Sandbox[SandboxManager] -. isolates .-> Skills
    Sandbox -. isolates .-> Stdio
```

### Communication Flow

```mermaid
sequenceDiagram
    participant CLI as CLI
    participant Scheduler as GraphScheduler
    participant Guard as GuardrailEngine
    participant Orchestrator as AgentOrchestrator
    participant Model as HttpModelClient
    participant Provider as Provider API
    participant Tool as Tool or Skill or MCP
    participant Store as SQLiteRunStore
    CLI->>Scheduler: run(input, session_id)
    Scheduler->>Orchestrator: dispatch entrypoint
    Orchestrator->>Model: complete(messages, tools)
    Model->>Provider: HTTP request
    Provider-->>Model: assistant text and tool calls
    Model-->>Orchestrator: normalized response
    Orchestrator->>Guard: validate tool input
    Guard-->>Orchestrator: allow or block
    Orchestrator->>Tool: invoke tool call
    Tool-->>Orchestrator: tool result
    Orchestrator->>Store: record trace and events
    Scheduler->>Guard: validate final output
    Guard-->>Scheduler: allow or block
    Scheduler->>Store: persist run, checkpoint, session state
    Scheduler-->>CLI: result or resumable failure state
```

### Current Communication Model

- Model calls go through `HttpModelClient`, then through a protocol adapter for `OpenAI`, `Anthropic`, or `Gemini` style payloads.
- Skills register as runtime tools through Python hooks or local command wrappers.
- MCP communication currently supports `stdio` and `HTTP/SSE` in the implemented codebase.
- Guardrails run before tool execution and before final output emission.
- Runtime traces, session messages, session state, checkpoints, and event envelopes are persisted into SQLite plus JSONL traces.
- CLI streaming uses the same runtime event channel that records agent, team, tool, guardrail, and MCP transitions.

## State, Memory, and Recovery

- Direct agent and top-level team runs can reuse prior conversation context by passing `--session-id`.
- Graph runs persist `shared_state` under the same `session_id`, so later runs can continue from durable graph context.
- Graph checkpoints are created at graph start and after each successful node.
- Top-level team checkpoints are created at team start and after each completed turn.
- `easy-agent resume <run_id>` restores the latest checkpoint for resumable graph and top-level team runs.

## Project Layout

```text
src/
  agent_cli/           CLI entrypoints and commands
  agent_common/        shared models and tool abstractions
  agent_config/        typed config models and validation
  agent_graph/         orchestration, graph scheduling, team runtime
  agent_integrations/  skills, MCP, plugins, sandbox, storage, guardrails
  agent_protocols/     protocol adapters and model client
  agent_runtime/       runtime assembly, benchmarks, long-run flows, public eval
skills/
  examples/            local demo skills
  real/                real validation skills
configs/
  longrun.example.yml  real MCP + skill validation
  teams.example.yml    Agent Teams examples
public_evals/
  fixtures/            vendored BFCL and tau2 public subsets
scripts/
  benchmark_modes.py   live benchmark for six execution modes
  windows/             easy-agent.ps1 / easy-agent.bat
tests/
  unit/                fast isolated unit tests
  integration/         live-service integration tests
```

## Collaboration Modes

- `single_agent`: one agent uses tools directly.
- `sub_agent`: one coordinator delegates focused work through `subagent__*` tools.
- `multi_agent_graph`: graph nodes schedule multiple agents and join outputs.
- `Agent Teams`:
  - `round_robin`
  - `selector`
  - `swarm`

## Plugins, Skills, and MCP

```python
from pathlib import Path

from agent_runtime.runtime import build_runtime

runtime = build_runtime('easy-agent.yml')
runtime.load(Path('skills/examples'))
runtime.load('third_party_plugin')
```

Supported mounting paths:

- local skill directories
- plugin manifests such as `plugin.yaml` or `easy-agent-plugin.yaml`
- Python package entry points in `agent_runtime.plugins`
- configured MCP servers from YAML config

## Quick Start

### Environment

```powershell
uv venv --python 3.12
uv sync --dev
```

### Local Credentials

The runtime supports a local-only `.env.local` file. Use it for machine-specific secrets so you do not need to export environment variables every time.

Example keys:

```dotenv
DEEPSEEK_API_KEY=your-key
PG_HOST=127.0.0.1
PG_PORT=5432
PG_USER=postgres
PG_PASSWORD=your-password
PG_DATABASE=postgres
REDIS_URL=redis://127.0.0.1:6379/0
```

### Common Commands

```powershell
uv run easy-agent doctor -c easy-agent.yml
uv run easy-agent skills list -c easy-agent.yml
uv run easy-agent plugins list -c easy-agent.yml
uv run easy-agent teams list -c configs/teams.example.yml
uv run easy-agent run "summarize the repository" --session-id demo-session -c easy-agent.yml
uv run easy-agent resume <run_id> -c configs/teams.example.yml
uv run python scripts/benchmark_modes.py --config easy-agent.yml --repeat 1
uv run easy-agent integration public-eval -c easy-agent.yml --output .easy-agent/public-eval-report.json
uv run easy-agent integration longrun -c configs/longrun.example.yml --cycles 1 --output-root .easy-agent/longrun
```

### Windows Launchers

```powershell
powershell -ExecutionPolicy Bypass -File scripts/windows/easy-agent.ps1 --help
cmd /c scripts/windows/easy-agent.bat --help
```

## Real Usage Results

### Live Benchmark Snapshot

The latest local benchmark snapshot comes from `.easy-agent/benchmark-report.json` on March 26, 2026 using DeepSeek through the OpenAI-compatible path.

| Mode | Success | Avg Seconds | Avg Tool Calls | Avg SubAgent Calls |
| --- | --- | ---: | ---: | ---: |
| `single_agent` | 1/1 | 4.9189 | 1 | 0 |
| `sub_agent` | 1/1 | 15.9533 | 1 | 1 |
| `multi_agent_graph` | 1/1 | 13.4902 | 2 | 0 |
| `team_round_robin` | 1/1 | 7.7634 | 1 | 0 |
| `team_selector` | 1/1 | 26.4179 | 1 | 0 |
| `team_swarm` | 1/1 | 10.5093 | 2 | 0 |

### Public Eval Snapshot

The latest local public-eval snapshot comes from `.easy-agent/public-eval-report.json` on March 26, 2026.

| Suite | Success | Pass Rate | Tool Match | Arg Match | Avg Seconds |
| --- | --- | ---: | ---: | ---: | ---: |
| `bfcl_simple` | 6/8 | 0.7500 | 0.7500 | 0.7500 | 10.6425 |
| `bfcl_multiple` | 2/8 | 0.2500 | 0.2500 | 0.2500 | 4.5149 |
| `bfcl_parallel_multiple` | 0/4 | 0.0000 | 0.0000 | 0.0000 | 13.7242 |
| `bfcl_irrelevance` | 0/4 | 0.0000 | 0.0000 | 0.0000 | 2.3965 |
| `tau2_mock` | 3/3 | 1.0000 | 1.0000 | 1.0000 | 5.9955 |
| `overall_bfcl` | 8/24 | 0.3333 | 0.3333 | 0.3333 | - |

Observed in this DeepSeek baseline:

- The tau2 mock subset is stable and fully passing after adding session-history prompt fallback.
- The BFCL subset is partially passing after normalizing tool schemas and sanitizing function names for OpenAI-compatible tool calling.
- The remaining BFCL failures are concentrated in multi-tool and irrelevance cases. In this March 26, 2026 run, several of those cases still triggered provider-side `400 Bad Request` responses from `https://api.deepseek.com/chat/completions`, so BFCL should currently be treated as a compatibility baseline, not a finished leaderboard score.

## Design References

The current runtime borrows explicit ideas from production-facing agent systems without hiding them behind opaque abstractions.

- OpenAI Agents SDK Sessions: <https://openai.github.io/openai-agents-python/sessions/>
- OpenAI Agents SDK Handoffs: <https://openai.github.io/openai-agents-python/handoffs/>
- OpenAI Agents SDK Guardrails: <https://openai.github.io/openai-agents-python/guardrails/>
- OpenAI Agents SDK Tracing: <https://openai.github.io/openai-agents-python/tracing/>
- AutoGen Teams: <https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html>
- AutoGen Selector Group Chat: <https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/selector-group-chat.html>
- AutoGen Swarm: <https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/swarm.html>
- LangGraph Durable Execution: <https://docs.langchain.com/oss/python/langgraph/durable-execution>
- LangGraph Memory: <https://docs.langchain.com/oss/python/langgraph/memory>
- MCP Transports: <https://modelcontextprotocol.io/docs/concepts/transports>

## Testing

```powershell
uv run ruff check src tests scripts
uv run mypy src tests scripts
uv run python -m pytest tests/unit -q
uv run python -m pytest tests/integration -m real -q
uv run easy-agent --help
uv run easy-agent doctor -c easy-agent.yml
uv run easy-agent teams list -c configs/teams.example.yml
```

For the full live suite, local PostgreSQL and Redis credentials must be available in `.env.local` or environment variables.

## Acknowledgements

- [Linux.do](https://linux.do/) for community discussion and open knowledge sharing.
- [![DeepSeek](https://img.shields.io/badge/DeepSeek-deepseek--chat-2563EB?style=flat-square)](https://www.deepseek.com/) for the real verification baseline and model endpoint.

## License

MIT
