<p align="center">
  <img src="./logo.svg" alt="easy-agent logo" width="160">
</p>

<h1 align="center">easy-agent</h1>

<p align="center">
  一个可检查、可测试、可扩展的白盒 Python Agent 运行时底座。
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

`easy-agent` 面向的是 Agent 产品下面的那层运行时基础设施，而不是具体业务应用本身。它把编排、工具调用、持久化、人审、联邦与评测能力显式保留在框架里，方便团队持续演进而不是被黑盒抽象锁死。

当前已发布的小版本是 `0.3.4`。

## 这个项目到底是什么

很多 Agent 项目会很快从“调用模型”走到“交付应用”。中间的运行时层随后会堆积大量隐式假设，例如工具调用约束、记忆、审批、传输和恢复流程。

`easy-agent` 的目标就是把这层中间件做成显式能力：

- 把运行时工程与业务逻辑分离。
- 让调度、编排、协议适配保持可检查。
- 让 tools、skills、MCP servers、plugins 可以持续挂载，而不是反复重写核心。
- 用真实的 harness、checkpoint 与 replay 取代一个越来越大的 prompt。

## 适合谁用

- 需要一套可复用运行时而不是一次性 demo 的 Agent 工程团队。
- 希望直接掌控工具调用、审批、持久化和恢复流程的开发者。
- 需要随着模型 API、MCP 和多 Agent 模式持续演进的项目。

## 技术栈

- Runtime：Python `3.12`、`uv`、`AnyIO`、`Typer`
- Model Surface：OpenAI-compatible、Anthropic-style、Gemini-style 载荷适配
- Persistence：SQLite + JSONL traces
- Integration Surface：direct tools、command skills、Python hook skills、MCP、plugins
- Isolation Surface：process、container、microVM workbench executors

## 能力一览

- 白盒运行时分层，明确暴露 scheduler、orchestrator、tool registry、storage 和 protocol adapters。
- 同时支持 `single_agent`、`sub_agent`、graph workflows、`Agent Teams` 与长任务 harness。
- 提供 session memory、checkpoints、replay、branchable resume 与审批感知恢复。
- 内置 guardrails、schema-aware tool validation、runtime event streaming 与 traces。
- 提供 A2A 风格远程联邦、持久化联邦任务状态以及签名 callback 校验。
- 提供 benchmark、BFCL、tau2 mock、provider schema compatibility 与 real-network 回归能力。

## Human Loop、Replay 与 MCP

`easy-agent` 已经把很多项目还停留在 roadmap 的可靠性能力做成了已实现特性：

- 敏感工具、swarm handoff 与 resume 可以进入同一套 durable approval 流程。
- 运行支持 safe-point interrupt、checkpoint list、replay 与 forked resume。
- MCP 已支持显式 roots、root snapshot、`notifications/roots/list_changed`、elicitation 审批状态、`streamable_http` 与持久化 OAuth state。

参考文档：
- 详细使用说明：[reference/zh/usage-guide.md](./reference/zh/usage-guide.md)
- 详细补强路线：[reference/zh/next-reinforcement.md](./reference/zh/next-reinforcement.md)

## A2A Remote Agent Federation

联邦层可以把本地 agents、teams、harnesses 通过耐用的 A2A 风格接口公开出去：

- 支持 well-known discovery、丰富 card、push/poll 投递、retry 与 resubscribe。
- 支持远端 federation client 的 OAuth/OIDC token 获取与刷新。
- 支持 signed card 与 signed callback 的 JWKS/JWS 校验。
- 在暴露或修改联邦状态之前，先执行更严格的 tenant/task 授权边界检查。

运行细节与同类项目对比详见 [reference/zh/test-results.md](./reference/zh/test-results.md)。

## Executor / Workbench Isolation

executor/workbench 层给长生命周期工具和 MCP 子进程提供了可复用的运行边界：

- 统一命名 executor：`process`、`container`、`microvm`
- 持久化 workbench sessions、manifests、snapshots 与 TTL cleanup
- 针对 warm-start latency 与 snapshot drift 提供 real-network 回归覆盖

详细操作说明见 [reference/zh/usage-guide.md](./reference/zh/usage-guide.md)。

## 架构说明

这个运行时是刻意模块化、可观测的：

- `scheduler` 负责 direct-agent 与 graph 执行
- `orchestrator` 负责 agent 和 team turn
- `harness` 负责 initializer、worker、evaluator 循环
- `registry` 负责 tools、skills、MCP tools、plugins
- `storage` 负责 runs、checkpoints、approvals、sessions、federation state、workbench state

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

## 长任务 Harness 设计

Harness 不是 prompt 约定，而是运行时的一等公民。每个 harness 会显式定义：

- `initializer_agent`
- `worker_target`
- `evaluator_agent`
- `completion_contract`

worker 循环会把 artifacts 与 checkpoints 持久化下来，因此长任务可以继续、replan 或 resume，而不会丢失状态。

## 协议与工具模型

- 模型协议：OpenAI-compatible、Anthropic-style、Gemini-style 载荷归一化
- 工具调用：strict schema transport、nullable/optional 参数建模、validation-repair loops、provider-neutral tool-choice controls、provider-schema telemetry
- web-search 评测补强：SerpApi `/search.json`、replay-backed contents、quota ledger、result grounding、single-call regression guards

Provider 行为细节与 structured outputs 约束说明见 [reference/zh/next-reinforcement.md](./reference/zh/next-reinforcement.md)。

## 项目结构

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

## 快速开始

```bash
uv venv --python 3.12
uv sync --dev
uv run easy-agent --help
uv run easy-agent doctor -c easy-agent.yml
```

详细环境准备、本地凭据、CLI 示例与运行方式：
- [reference/zh/usage-guide.md](./reference/zh/usage-guide.md)

## 一次 Harness 运行会留下什么

一次 harness 运行会把以下工件持久化到配置的 artifact 目录与 durable session storage：

- bootstrap / progress markdown
- feature snapshots
- checkpoints 与 replay state
- workbench session metadata

详细说明见 [reference/zh/usage-guide.md](./reference/zh/usage-guide.md)。

## 验证方式

`0.3.4` 版本保留 2026 年 4 月 9 日的 benchmark 快照，同时发布 2026 年 4 月 11 日刷新后的 public-eval、Python verification 与 real-network 快照。当前 repo-pinned BFCL agentic 子集已经转绿。方法说明、公开对比与详细矩阵见 [reference/zh/test-results.md](./reference/zh/test-results.md)。

### 分数摘要

| 测试集 | 分数 |
| --- | ---: |
| benchmark.overall | 100.0 |
| public_eval.bfcl_overall | 98.21 |
| public_eval.tau2_mock | 100.0 |

## 真实网络测试集结果

README 这里只保留分数展示。耗时、telemetry、warm-start budget 与 snapshot drift 的详细内容都放在 [reference/zh/test-results.md](./reference/zh/test-results.md)。

| 测试集 | 分数 |
| --- | ---: |
| real_network.overall | 100.0 |

## 下一步补强

完整补强路线见 [reference/zh/next-reinforcement.md](./reference/zh/next-reinforcement.md)。近期重点仍然是：

- 把已经转绿的 repo-pinned BFCL agentic 子集继续推进到更广的 official-style BFCL v4 覆盖面，并压缩剩余 multi-tool miss
- 把 BFCL web-search 继续推进到 multi-hop search-plus-contents 和内容模式感知检索
- 继续收紧 strict schema transport、provider-specific tool-choice mapping，以及后续 Responses API 对齐这几块 provider 兼容性
- 在不随意扩展公开运行时表面的前提下，继续推进 durable MCP 与 federation 协调

## 设计参考

- OpenAI function calling：<https://developers.openai.com/api/docs/guides/function-calling>
- OpenAI structured outputs：<https://developers.openai.com/api/docs/guides/structured-outputs>
- Anthropic tool use：<https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools>
- Gemini function calling：<https://ai.google.dev/gemini-api/docs/function-calling>
- BFCL v4 web search：<https://gorilla.cs.berkeley.edu/blogs/15_bfcl_v4_web_search.html>
- Model Context Protocol：<https://modelcontextprotocol.io/specification>
- SerpApi Search API：<https://serpapi.com/search-api>
- FastAPI README 风格参考：<https://github.com/fastapi/fastapi>
- uv README 风格参考：<https://github.com/astral-sh/uv>

## 致谢

- [Linux.do](https://linux.do/) 提供了开放的社区讨论与知识分享环境。
- [![DeepSeek](https://img.shields.io/badge/DeepSeek-deepseek--chat-2563EB?style=flat-square)](https://www.deepseek.com/) 为本仓库真实验证流程提供了模型端点基线。

## License

MIT。详见 [LICENSE](./LICENSE)。
