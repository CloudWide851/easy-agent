# 使用说明

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

## 操作说明

- 使用 `uv run easy-agent ...` 或针对 `agent_cli.app:app` 的 Python `CliRunner`；不要使用 `python -m agent_cli`。
- 这台 Windows 机器上的稳定 pytest 执行需要唯一的 `%TEMP%` 根 `--basetemp`。
- README 内容发生变化时，必须同轮同步 `README.md` 与 `README.zh-CN.md`。
- 仓库对外文档里不能出现本地用户名、用户目录、工作区绝对路径或真实 secret。
