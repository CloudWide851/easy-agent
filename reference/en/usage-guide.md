# Usage Guide

## Environment

```bash
uv venv --python 3.12
uv sync --dev
```

## Core CLI

```bash
uv run easy-agent --help
uv run easy-agent doctor -c easy-agent.yml
uv run easy-agent teams list -c configs/teams.example.yml
uv run easy-agent harness list -c configs/harness.example.yml
uv run easy-agent federation list -c easy-agent.yml
```

## Local Credentials

Keep real credentials in environment variables only. Do not place secrets in tracked files.

Common local variables:

- `DEEPSEEK_API_KEY`
- `SERPAPI_API_KEY`
- `PG_PASSWORD`
- `REDIS_URL`

Executor- and host-gated real-network coverage may also require:

- `EASY_AGENT_PODMAN_EXE`
- `EASY_AGENT_CONTAINER_IMAGE`
- `EASY_AGENT_QEMU_EXE`
- `EASY_AGENT_QEMU_BASE_IMAGE`
- `EASY_AGENT_QEMU_SSH_KEY`
- `EASY_AGENT_QEMU_SSH_USER`

## Harness Outputs

Harness runs persist durable artifacts under `.easy-agent/`, including:

- `bootstrap.md`
- `progress.md`
- `features.json`
- checkpoints
- session and workbench state

## Operational Notes

- Use `uv run easy-agent ...` or Python `CliRunner` against `agent_cli.app:app`; do not use `python -m agent_cli`.
- Stable pytest execution on this Windows machine requires a unique `%TEMP%`-rooted `--basetemp`.
- When README content changes, update both `README.md` and `README.zh-CN.md` in the same round.
- Repository-facing content must not contain local usernames, home directories, absolute workspace paths, or secrets.
