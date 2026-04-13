# 使用说明

本文档对应已发布的 `0.3.4` 文档集合。

## 环境准备

```bash
uv venv --python 3.12
uv sync --dev
```

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

README 里的公开分数继续以 `full_v4` 为基线。`official_full_v4` 现在支持先跑受控 manifest slice，再逐步扩大官方覆盖面。

`evaluation.public_eval.official_dataset` 下常用字段：

- `suite_allowlist`
- `case_allowlist`
- `max_cases`
- `resume`
- `checkpoint_path`

## 操作说明

- 使用 `uv run easy-agent ...` 或针对 `agent_cli.app:app` 的 Python `CliRunner`；不要使用 `python -m agent_cli`。
- 这台 Windows 机器上的稳定 pytest 执行需要唯一的 `%TEMP%` 根 `--basetemp`。
- README 内容发生变化时，必须同轮同步 `README.md` 与 `README.zh-CN.md`。
- 仓库对外文档里不能出现本地用户名、用户目录、工作区绝对路径或真实 secret。
