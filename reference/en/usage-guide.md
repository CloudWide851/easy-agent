# Usage Guide

This guide matches the published `0.3.5` documentation set.

## Environment

```bash
uv venv --python 3.12
uv sync --dev
```

## Model Surface

- `model.openai_api_style` defaults to `chat_completions`.
- Set `model.openai_api_style: responses` only for OpenAI-compatible endpoints that explicitly support `/responses`.
- The provider-neutral function-calling controls stay aligned across both OpenAI-compatible styles:
  - `strict`
  - `parallel_tool_calls`
  - `mode`
  - `forced_tool_name`
- The strict baseline in this repository follows the current OpenAI guidance:
  - `strict: true`
  - `additionalProperties: false`
  - optional fields modeled as required plus nullable when strict structured outputs are needed

## Core CLI

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

Harness runs persist durable artifacts under the configured artifact directory and durable session storage, including:

- `bootstrap.md`
- `progress.md`
- `features.json`
- checkpoints
- session and workbench state

## Public Eval Profiles

`full_v4` remains the public score baseline in the README. `official_full_v4` accepts raw official-style manifests in JSON or JSONL form, while the README headline score stays on the repo-pinned baseline.

Useful config fields under `evaluation.public_eval.official_dataset`:

- `category_allowlist`
- `suite_allowlist`
- `case_allowlist`
- `selection_mode`
- `max_cases`
- `max_cases_per_suite`
- `resume`
- `checkpoint_path`

Selection notes:

- `selection_mode: manifest_order` preserves the manifest order and then applies `max_cases`.
- `selection_mode: balanced_per_suite` interleaves cases across normalized suites before applying `max_cases`.
- `category_allowlist` filters on normalized public categories such as `agentic`, `multihop`, `memory`, and `web_search`.
- `max_cases_per_suite` caps one normalized suite before the final `max_cases` limit is applied.

## Web Search Eval Notes

- Repo-pinned BFCL web-search keeps SerpApi `/search.json` as the explicit search transport.
- `web.contents` now follows a stricter grounded path:
  - resolve result ids or URLs only from grounded search results
  - prefer grounded cached contents before network fetch
  - retry alternative grounded URLs with the same grounded title before replay fallback
  - fall back to replay-backed contents only after grounded fetch attempts fail
- Per-case diagnostics now track:
  - grounded source counts
  - cache or network or replay content-source usage
  - grounded retry counts
  - search and contents backend mix
- This keeps the repo-pinned BFCL web-search slice green while exposing when a local refresh relied on replay instead of live search.

## MCP Catalog Notes

- `mcp resources templates <server>` persists durable `resource_templates` snapshots.
- `mcp prompts get <server> <prompt-name>` persists durable prompt-detail cache entries keyed by prompt name plus arguments.
- `notifications/resources/list_changed` refreshes both resource entries and resource templates.
- `notifications/prompts/list_changed` refreshes prompt summaries and marks cached prompt-detail entries as stale until they are fetched again.

## Operational Notes

- Use `uv run easy-agent ...` or Python `CliRunner` against `agent_cli.app:app`; do not use `python -m agent_cli`.
- Stable pytest execution on this Windows machine requires a unique `%TEMP%`-rooted `--basetemp`.
- When README content changes, update both `README.md` and `README.zh-CN.md` in the same round.
- Repository-facing content must not contain local usernames, home directories, absolute workspace paths, or secrets.
