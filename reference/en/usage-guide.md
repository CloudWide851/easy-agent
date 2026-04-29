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
uv run easy-agent setup --provider mock
uv run easy-agent new coding-agent
uv run easy-agent new research-agent <target-dir>
uv run easy-agent new data-agent
uv run easy-agent new ops-agent
uv run easy-agent new browser-agent <target-dir>
uv run easy-agent init --provider mock
uv run easy-agent quickstart --provider mock
uv run easy-agent template list
uv run easy-agent template create basic-agent <target-dir>
uv run easy-agent config validate -c easy-agent.yml
uv run easy-agent config explain -c easy-agent.yml
uv run easy-agent config doctor -c easy-agent.yml
uv run easy-agent doctor -c easy-agent.yml
uv run easy-agent teams list -c configs/teams.example.yml
uv run easy-agent harness list -c configs/harness.example.yml
uv run easy-agent federation list -c easy-agent.yml
uv run easy-agent runs list -c easy-agent.yml
uv run easy-agent runs show <run_id> -c easy-agent.yml
uv run easy-agent runs explain <run_id> -c easy-agent.yml
uv run easy-agent traces export <run_id> -c easy-agent.yml
uv run easy-agent traces export <run_id> -c easy-agent.yml --html --output trace.html
uv run easy-agent traces open <run_id> -c easy-agent.yml --no-browser
uv run easy-agent report latest -c easy-agent.yml
uv run easy-agent report latest -c easy-agent.yml --html --output report.html
uv run easy-agent mcp resources list <server> -c easy-agent.yml
uv run easy-agent mcp resources read <server> <uri> -c easy-agent.yml
uv run easy-agent mcp resources templates <server> -c easy-agent.yml
uv run easy-agent mcp resources subscribe <server> <uri> -c easy-agent.yml
uv run easy-agent mcp resources unsubscribe <server> <uri> -c easy-agent.yml
uv run easy-agent mcp prompts list <server> -c easy-agent.yml
uv run easy-agent mcp prompts get <server> <prompt-name> --arguments '{"topic":"notes"}' -c easy-agent.yml
```

## Onboarding Flow

Use the `mock` provider when you want to verify the runtime, tools, storage, and trace surfaces without any model credentials.

- `setup --provider mock` creates or reuses a local config, runs static preflight checks, validates it, runs a deterministic smoke test, and prints the next run-inspection commands.
- `init --provider mock` writes a starter config that uses `protocol: mock`.
- `quickstart --provider mock` creates a temporary local config, runs one deterministic tool-using agent turn, and prints the follow-up `runs show`, `runs explain`, and `traces export` commands for the new run id.
- `new <scenario> [target-dir]` is the shortest project creation path. It wraps `template create`, defaults the target directory to the scenario name, and keeps the older template commands available.
- `template list` shows starter project shapes.
- `template create basic-agent <target-dir>` creates a minimal single-agent project.
- `template create human-approval-agent <target-dir>` creates the same local starter with `python_echo` marked as a sensitive tool.
- `template create longrun-harness <target-dir>` creates a minimal initializer / worker / evaluator harness.
- `template create mcp-filesystem-agent <target-dir>` creates a filesystem-MCP starter.
- `template create eval-smoke <target-dir>` creates a public-eval smoke starter.
- `template create federation-loopback <target-dir>` creates a local federation export starter.
- `template create workbench-coding-agent <target-dir>` creates a process-workbench starter.
- `template create coding-agent <target-dir>` creates a business-oriented coding starter with process workbench configuration.
- `template create research-agent <target-dir>` creates a business-oriented research starter with `official_source_search` wired beside the mock-first smoke tool.
- `template create data-agent <target-dir>` creates a business-oriented data starter for CSV, JSON, logs, metric summaries, and evidence-backed recommendations.
- `template create ops-agent <target-dir>` creates a business-oriented operations starter for diagnostics, runbooks, incident notes, and release checks.
- `template create browser-agent <target-dir>` creates a mock-first browser task-planning starter. It does not install a browser runtime; wire a real connector only after the planning flow and safety checks are clear.
- `config explain` summarizes model/provider choices, entrypoint type, agents, tools, teams, harnesses, MCP, storage, executors, federation, eval settings, and required environment variables without printing secret values.
- `config doctor` performs static risk checks without starting model clients or MCP servers. It reports Python baseline drift, missing live env vars, missing local tools, MCP roots/auth gaps, federation auth gaps, workbench executor readiness, human-loop coverage, storage portability, and eval credential readiness.
- Generated templates include a local README, a minimal `.env.local.example`, and a mock-first smoke command path. Template smoke starts with `config doctor`, then runs a short task and exports an HTML trace for the new run id.

Use `--provider deepseek` only after `DEEPSEEK_API_KEY` is present in the environment.

## Run and Trace Inspection

Durable run inspection now has two layers:

- `runs list` shows recent run ids, status, kind, session id, and creation time.
- `runs show <run_id>` returns a run summary with event, node, checkpoint, approval, and child-run counts.
- `runs explain <run_id>` classifies common failure causes such as missing provider credentials, schema validation failures, guardrail blocks, MCP failures, iteration loops, and known Windows cleanup warnings.
- `traces export <run_id>` returns a structured trace tree by default.
- `traces export <run_id> --raw` returns the historical raw trace payload.
- `traces export <run_id> --html --output trace.html` writes a standalone HTML trace viewer for the structured tree, including summary cards, status/error highlighting, span-kind filters, text search, and the raw JSON payload.
- `traces open <run_id>` writes the same standalone HTML viewer and opens it in the default browser. Use `--no-browser` for headless terminals, CI, or tests.

## Latest Report

`report latest` is a read-only status dashboard for local evidence:

- benchmark report availability, success count, and score
- public-eval profile, completed record count, and headline BFCL score
- real-network pass/fail/skip count and generated timestamp
- recent durable run status counts from the configured storage

If a report file is absent, the command marks that surface as `missing` and still returns the rest of the dashboard. Use the report path override flags when comparing temporary or archived artifacts.

Use `report latest --html --output report.html` when the terminal table is too dense. The exported file is standalone and includes the same benchmark, public-eval, real-network, recent-run, and raw JSON evidence.

Trace-tree spans are derived from the existing runtime event envelope and include stable `span_id`, `parent_span_id`, `kind`, `status`, duration, input/output hashes, retry count, checkpoint id, and child spans. This keeps the current JSON trace path lightweight while leaving a future OpenTelemetry export path open.

## Python Facade

Use the lightweight facade when embedding the runtime in Python code and you do not need the full CLI surface:

```python
from agent_runtime import AgentApp

app = AgentApp.from_config("easy-agent.yml")
try:
    result = app.run("Summarize this task")
finally:
    app.close()
```

The facade delegates to the same `EasyAgentRuntime` used by the CLI, so storage, session memory, guardrails, MCP, federation, and workbench behavior still come from the config file.

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

## Practical Official-Source Search

The shipped `skills/examples/official_source_search` skill is meant to be used as a practical source-prioritized search tool instead of a benchmark-only helper.

- Mounting `skills/examples` now exposes `official_source_search`.
- The tool can prioritize primary documentation domains through:
  - `mode: preferred_first | preferred_only | general`
  - `preferred_domains`
- Optional fetched-page extraction is available through:
  - `fetch_contents`
  - `content_mode: truncate | markdown | raw`

Typical config expectations:

- Keep `SERPAPI_API_KEY` in the environment only.
- Put the tool name on the agent that should be allowed to browse.
- Treat `preferred_domains` as a ranking policy hint, not as a hidden allowlist.

## Harness Outputs

Harness runs persist durable artifacts under the configured artifact directory and durable session storage, including:

- `bootstrap.md`
- `progress.md`
- `features.json`
- checkpoints
- session and workbench state

## Public Eval Profiles

`full_v4` remains the public score baseline in the README. `official_full_v4` accepts raw official-style manifests in JSON or JSONL form, while the README headline score stays on the repo-pinned baseline.

Additional local reinforcement profiles are now available:

- `browsecomp_subset`
- `simpleqa_subset`
- `simple_evals_subset`

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

Useful config fields under `evaluation.public_eval.simple_evals`:

- `browsecomp_path`
- `browsecomp_source_url`
- `browsecomp_case_allowlist`
- `browsecomp_max_cases`
- `simpleqa_path`
- `simpleqa_source_url`
- `simpleqa_case_allowlist`
- `simpleqa_max_cases`

Grader notes:

- `evaluation.public_eval.grader.enabled` keeps the strict grading path explicit.
- If grader mode is enabled, the configured credential env var must be present; the runtime no longer silently downgrades that path.
- Benchmark questions are still not vendored into this repository. Point the config at your own JSON or JSONL export, or an explicit dataset-export URL, instead of an evaluator source file.

## Provider Compatibility Live Matrix

Use `evaluation.public_eval.provider_compatibility` to run live provider checks without rewriting the main public-eval profile:

- `enabled` turns the matrix on or off.
- `targets[*].name` gives each live target a stable report key.
- `targets[*].protocol` selects the adapter surface: `openai`, `anthropic`, or `gemini`.
- `targets[*].openai_api_styles` lets OpenAI-compatible targets opt into `chat_completions`, `responses`, or both.
- `targets[*].optional` keeps missing credentials visible as `skipped` instead of silently removing the target.

Current interpretation rules:

- required checks such as strict schema requests, `tool_choice: none`, required-tool mode, and forced-tool mode remain release-blocking
- `single_tool_call_control` is explicitly classified as `best_effort` for non-OpenAI OpenAI-compatible providers, because some providers expose the field but do not always enforce it at runtime
- the resulting matrix is designed to explain what is normalized, what is enforced, and what was only observed on a best-effort basis

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
- BFCL web-search tool schemas can opt into `x-easy-agent-normalizer: web_search_query` so scoring stays strict while wrapper phrases such as "search the web for ..." are normalized before argument comparison.
- This keeps the repo-pinned BFCL web-search slice green while exposing when a local refresh relied on replay instead of live search.

## MCP Catalog Notes

- `mcp resources templates <server>` persists durable `resource_templates` snapshots.
- `mcp prompts get <server> <prompt-name>` persists durable prompt-detail cache entries keyed by prompt name plus arguments.
- `notifications/resources/list_changed` refreshes both resource entries and resource templates.
- `notifications/prompts/list_changed` refreshes prompt summaries and marks cached prompt-detail entries as stale until they are fetched again.

## Executor Capability Reports

`doctor` and `workbench list` surfaces can use executor `capability_report` details from each configured backend:

- process: workbench-root scoping with host-process network and process behavior
- container: bind-mounted workbench root, explicit command environment, force-remove shutdown, and checkpoint image restore when enabled
- microVM: guest workdir sync boundary, SSH command channel, and runtime-state or guest-sync restore when enabled

Treat these reports as operational claims rather than a formal sandbox proof. Production deployments should still harden the host runtime, images, network policy, and secret injection path.

## Operational Notes

- Use `uv run easy-agent ...` or Python `CliRunner` against `agent_cli.app:app`; do not use `python -m agent_cli`.
- Stable pytest execution on this Windows machine requires a unique `%TEMP%`-rooted `--basetemp`.
- When README content changes, update both `README.md` and `README.zh-CN.md` in the same round.
- Repository-facing content must not contain local usernames, home directories, absolute workspace paths, or secrets.
