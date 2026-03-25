# easy-agent

[![Linux.do](https://linux.do/logo-128.svg)](https://linux.do/)

`easy-agent` is a white-box, business-agnostic, extensible Agent engineering foundation for Python. It is designed to stay focused on Agent runtime concerns instead of business logic, so skills, MCP servers, plugins, teams, sub-agents, and future protocol changes can be mounted with minimal friction.

`easy-agent` 是一个白板化、业务无关、可工程化扩展的 Python Agent 开发底座。它的目标不是封装某个垂直业务，而是稳定承接 Agent Runtime、tool calling、skills、MCP、plugins、teams、subAgent 与后续协议演进，让二开和长期演进更容易。

## Highlights | 核心特性

- Unified tool-calling adaptation for `OpenAI`, `Anthropic`, and `Gemini`, with DeepSeek `deepseek-chat` used as the default verification model through the OpenAI-compatible path.
- White-box runtime composition for agents, graph nodes, team nodes, storage, sandboxing, skills, MCP, and plugins.
- Multiple collaboration modes: `single_agent`, `sub_agent`, DAG `multi_agent_graph`, and `Agent Teams` with `round_robin`, `selector`, and `swarm`.
- Plugin-style mounting through `runtime.load(...)`, local manifests, and Python entry points.
- Sandboxed command-skill and stdio MCP execution, including Windows-friendly fallback behavior.
- SQLite + JSONL trace persistence for benchmark, debugging, replay, and later evaluation.
- 标准工程化结构，CLI、配置、运行时、协议、集成和测试分层清晰。
- 支持多 Agent、subAgent、Agent Teams、tool calling 2.0 风格工具协作与真实长时间验证。

## Architecture | 架构概览

```text
src/
  agent_cli/           CLI entrypoints and commands
  agent_common/        shared models and tool abstractions
  agent_config/        typed config models and validation
  agent_graph/         orchestration, graph scheduling, team runtime
  agent_integrations/  skills, MCP, plugins, sandbox, storage
  agent_protocols/     protocol adapters and model client
  agent_runtime/       runtime assembly, benchmarks, long-run flows
skills/
  examples/            local demo skills
  real/                real validation skills
configs/
  longrun.example.yml  real MCP + skill validation
  teams.example.yml    Agent Teams examples
scripts/
  benchmark_modes.py   live benchmark for six execution modes
  windows/             easy-agent.ps1 / easy-agent.bat
tests/
  unit/                fast isolated unit tests
  integration/         real live-service integration tests
```

## Quick Start | 快速开始

### 1. Environment | 环境准备

```powershell
uv venv --python 3.12
uv sync --dev
$env:DEEPSEEK_API_KEY = "your-key"
```

Baseline:

- Python: `3.12.x`
- Virtual env: `uv venv --python 3.12`
- Dependency sync: `uv sync --dev`
- CLI: `easy-agent`

### 2. Smoke Commands | 常用命令

```powershell
uv run easy-agent doctor -c easy-agent.yml
uv run easy-agent skills list -c easy-agent.yml
uv run easy-agent plugins list -c easy-agent.yml
uv run easy-agent teams list -c configs/teams.example.yml
uv run easy-agent run "Use a tool and respond briefly" -c easy-agent.yml
uv run python scripts/benchmark_modes.py --config easy-agent.yml --repeat 1
```

### 3. Windows Launchers | Windows 快速入口

```powershell
powershell -ExecutionPolicy Bypass -File scripts/windows/easy-agent.ps1 --help
cmd /c scripts/windows/easy-agent.bat --help
```

## Collaboration Modes | 协作模式

### Core Modes | 基础模式

- `single_agent`: one agent uses tools directly.
- `sub_agent`: one coordinator delegates focused work through `subagent__*` tools.
- `multi_agent_graph`: graph nodes schedule multiple agents and joins.

### Agent Teams | 团队协作

`Agent Teams` is the new orchestration layer for multi-role collaboration:

- `round_robin`: members speak in fixed order.
- `selector`: the model selects the next speaker from typed team members.
- `swarm`: the active speaker hands work off with generated `handoff__{agent}` tools.

Example:

```yaml
graph:
  entrypoint: round_robin_team
  agents:
    - name: planner
      description: Create an initial plan.
      tools: [python_echo]
    - name: closer
      description: Finish and terminate.
      tools: [python_echo]
  teams:
    - name: round_robin_team
      mode: round_robin
      members: [planner, closer]
      max_turns: 4
      termination_text: TERMINATE
```

See [`configs/teams.example.yml`](/H:/闲鱼项目/Python项目/Simple系列/easy-agent/configs/teams.example.yml) for all three modes.

## Plugin Mounting | 插件挂载

`easy-agent` keeps business logic out of the core and mounts external capability through a unified runtime interface:

```python
from pathlib import Path

from agent_runtime.runtime import build_runtime

runtime = build_runtime("easy-agent.yml")
runtime.load(Path("skills/examples"))
runtime.load("third_party_plugin")
```

Supported loading paths:

- skill directory or skill root
- plugin manifest such as `plugin.yaml` or `easy-agent-plugin.yaml`
- Python packages exposing `agent_runtime.plugins` entry points
- configured MCP servers and local/real skills

This keeps the repository flexible, white-box, and ready for MCP, skills, and future extensions.

## Sandbox and Safety | 安全与沙盒

The runtime includes sandbox controls for command skills and stdio MCP transports:

- `off`: no isolation
- `process`: isolated child-process execution
- `auto`: choose the best available strategy
- `windows_sandbox`: prefer Windows Sandbox when available

The current design goal is pragmatic safety:

- keep dangerous commands out of the default allowlist
- isolate command skill execution paths
- isolate stdio MCP runtime where possible
- preserve local fallback behavior on Windows when full sandbox features are unavailable
- keep credentials in environment variables instead of tracked files

## Real Validation | 真实验证

Real validation in this repository is not a mock-only story. The project already runs live checks for:

- DeepSeek `deepseek-chat`
- local skills
- filesystem MCP
- Redis MCP
- PostgreSQL MCP
- `single_agent`
- `sub_agent`
- `multi_agent_graph`
- `team_round_robin`
- `team_selector`
- `team_swarm`

Manual real test example:

```powershell
$env:DEEPSEEK_API_KEY = "your-key"
$env:PG_HOST = "127.0.0.1"
$env:PG_PORT = "5432"
$env:PG_USER = "postgres"
$env:PG_PASSWORD = "your-password"
$env:PG_DATABASE = "postgres"
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
uv run python -m pytest tests/integration -m real -q
```

If you want to inspect real MCP servers from [`configs/longrun.example.yml`](/H:/闲鱼项目/Python项目/Simple系列/easy-agent/configs/longrun.example.yml), make sure `.easy-agent/longrun/artifacts` exists before running the CLI.

## Real Usage Results | 真实使用效果

The following benchmark was generated from the live report in `.easy-agent/benchmark-report.json` on `2026-03-25` with DeepSeek through the OpenAI-compatible path.

| Mode | Success | Avg Seconds | Avg Tool Calls | Avg SubAgent Calls |
| --- | --- | ---: | ---: | ---: |
| `single_agent` | 1/1 | 6.1493 | 1 | 0 |
| `sub_agent` | 1/1 | 20.6691 | 1 | 1 |
| `multi_agent_graph` | 1/1 | 14.4803 | 2 | 0 |
| `team_round_robin` | 1/1 | 11.2187 | 1 | 0 |
| `team_selector` | 1/1 | 15.1416 | 1 | 0 |
| `team_swarm` | 1/1 | 11.0792 | 2 | 0 |

This is the current real execution baseline, not synthetic documentation output.

## CLI | 命令行

```powershell
easy-agent doctor -c easy-agent.yml
easy-agent run "your task" -c easy-agent.yml
easy-agent trace <run_id> -c easy-agent.yml
easy-agent skills list -c easy-agent.yml
easy-agent plugins list -c easy-agent.yml
easy-agent mcp list -c configs/longrun.example.yml
easy-agent teams list -c configs/teams.example.yml
```

## Testing | 测试方式

Backend verification:

```powershell
uv run ruff check src tests scripts
uv run mypy src tests scripts
uv run python -m pytest tests/unit -q
uv run python -m pytest tests/integration -m real -q
```

Windows launcher smoke:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/windows/easy-agent.ps1 teams list -c configs/teams.example.yml
cmd /c scripts/windows/easy-agent.bat teams list -c configs/teams.example.yml
```

## Open-Source Inspired Improvements | 参考优秀 Agent 项目的优化方向

The current release already absorbs a few useful patterns from strong open-source agent systems:

- team conversation modes similar to group-chat and handoff orchestration patterns
- stronger config validation so agent, team, and graph entrypoints fail early
- runtime-level capability mounting instead of hardwiring business code into the framework
- benchmark-first validation so orchestration changes are measured against real model calls

Next high-value improvements worth implementing:

- durable checkpoints and resume after partial failure
- eval datasets and regression scoring for prompts, tool usage, and team routing
- team-level memory budgets, role policies, and context compaction
- stricter tool ACLs, approval hooks, and per-plugin trust boundaries
- streaming events and richer observability for long-running multi-agent sessions

## README Notes for Linux.do | Linux.do 说明

Thanks to the Linux.do community for its open sharing culture and AI/tooling discussions.

- Community homepage: [Linux.do](https://linux.do/)
- Community guidelines: [Guidelines](https://linux.do/guidelines)
- FAQ / promotion-related rules: [FAQ](https://linux.do/faq)

If this project is later posted there as a public-welfare/open-source promotion, the README should not be copy-pasted as the final post body. Based on the currently visible Linux.do rules, public-welfare promotion posts should follow the latest community guidelines, use the correct tag, avoid AI-polished ad copy, avoid indirect traffic diversion, and use `LINUX DO Connect` when a login gate exists. Always re-check the latest rules before posting.

## Acknowledgements | 致谢

- [Linux.do](https://linux.do/) for community discussion, public knowledge sharing, and broader open tooling culture.
- DeepSeek for the model endpoint used in the real verification flow of this repository.

## License

MIT
