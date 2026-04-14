# 使用说明

本文档对应已发布的 `0.3.4` 文档集合。

## 环境准备

```bash
uv venv --python 3.12
uv sync --dev
```

## Model Surface

- `model.openai_api_style` 默认是 `chat_completions`。
- 只有在 OpenAI-compatible endpoint 明确支持 `/responses` 时，才把它切到 `responses`。
- 两条 OpenAI-compatible 路径共用同一套 provider-neutral function-calling 控制面：
  - `strict`
  - `parallel_tool_calls`
  - `mode`
  - `forced_tool_name`

## 核心 CLI

```bash
uv run easy-agent --help
uv run easy-agent doctor -c easy-agent.yml
uv run easy-agent teams list -c configs/teams.example.yml
uv run easy-agent harness list -c configs/harness.example.yml
uv run easy-agent federation list -c easy-agent.yml
uv run easy-agent mcp resources list <server> -c easy-agent.yml
uv run easy-agent mcp resources read <server> <uri> -c easy-agent.yml
uv run easy-agent mcp resources templates <server> -c easy-agent.yml
uv run easy-agent mcp resources subscribe <server> <uri> -c easy-agent.yml
uv run easy-agent mcp resources unsubscribe <server> <uri> -c easy-agent.yml
uv run easy-agent mcp prompts list <server> -c easy-agent.yml
uv run easy-agent mcp prompts get <server> <prompt-name> --arguments '{"topic":"notes"}' -c easy-agent.yml
```

## 本地凭据

真实凭据只放环境变量，不写入 tracked files。

常见本地变量：

- `DEEPSEEK_API_KEY`
- `SERPAPI_API_KEY`
- `PG_PASSWORD`
- `REDIS_URL`

host-gated real-network 覆盖可能还需要：

- `EASY_AGENT_PODMAN_EXE`
- `EASY_AGENT_CONTAINER_IMAGE`
- `EASY_AGENT_QEMU_EXE`
- `EASY_AGENT_QEMU_BASE_IMAGE`
- `EASY_AGENT_QEMU_SSH_KEY`
- `EASY_AGENT_QEMU_SSH_USER`

## Harness 工件

Harness 运行会把工件持久化到配置的 artifact 目录与 durable session storage：

- `bootstrap.md`
- `progress.md`
- `features.json`
- checkpoints
- session 与 workbench state

## Public Eval Profiles

README 里的公开分数继续以 `full_v4` 为基线。`official_full_v4` 现在已经可以直接读取 raw official 风格的 JSON / JSONL manifest，但 README headline score 仍然保持 repo-pinned 基线。

`evaluation.public_eval.official_dataset` 下常用字段：

- `category_allowlist`
- `suite_allowlist`
- `case_allowlist`
- `selection_mode`
- `max_cases`
- `max_cases_per_suite`
- `resume`
- `checkpoint_path`

选择说明：

- `selection_mode: manifest_order` 会先保留 manifest 顺序，再应用 `max_cases`。
- `selection_mode: balanced_per_suite` 会先按归一化 suite 交错取样，再应用 `max_cases`。
- `category_allowlist` 面向归一化后的公开类别，例如 `agentic`、`multihop`、`memory`、`web_search`。
- `max_cases_per_suite` 会在最终 `max_cases` 生效前，先限制每个归一化 suite 的样本数量。

## MCP Catalog 说明

- `mcp resources templates <server>` 现在会持久化 `resource_templates` 快照。
- `mcp prompts get <server> <prompt-name>` 现在会按 prompt name + arguments 持久化 prompt detail cache。
- `notifications/resources/list_changed` 会同时刷新 resource entries 和 resource templates。
- `notifications/prompts/list_changed` 会刷新 prompt summaries，并把已有的 prompt detail cache 标记成 stale，直到下一次重新获取。

## 操作说明

- 使用 `uv run easy-agent ...` 或针对 `agent_cli.app:app` 的 Python `CliRunner`；不要使用 `python -m agent_cli`。
- 这台 Windows 机器上的稳定 pytest 执行需要唯一的 `%TEMP%` 根 `--basetemp`。
- README 内容发生变化时，必须同轮同步 `README.md` 与 `README.zh-CN.md`。
- 仓库对外文档里不能出现本地用户名、用户目录、工作区绝对路径或真实 secret。
