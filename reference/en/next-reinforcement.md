# Next Reinforcement

This roadmap starts from the published `0.3.5` baseline.

## Immediate Focus

- Keep reducing runtime complexity by turning large compatibility modules into smaller import-compatible surfaces, with storage contracts and trace helpers split away from SQLite details.
- Make the now-shipped zero-credential onboarding layer a permanent compatibility gate: keep `setup` preflight checks, `mock` quickstart, config explanation, config doctor, starter templates, searchable HTML traces, and run explanation tests as the first smoke layer before live-provider suites.
- Promote the new run summary, searchable HTML trace viewer, standalone latest-report HTML, and trace-tree export into the main debugging workflow, then align the JSON trace shape with OpenTelemetry GenAI semantic conventions when the local shape stabilizes.
- Widen the shipped live provider-specific compatibility evidence beyond the required DeepSeek/OpenAI-compatible baseline, including optional Anthropic and Gemini coverage when credentials are present.
- Extend the raw official BFCL v4 normalization path into wider agentic and multihop coverage with clearer official-category diagnostics.
- Turn the newly shipped `official_source_search` plus `browsecomp_subset` / `simpleqa_subset` support into refreshable scored slices once local dataset exports and grader credentials are available.
- Deepen MCP notification parity around resource updates, prompt-detail refresh, and template diff telemetry without widening the model-facing runtime surface.
- Treat the new wizard, connector diagnostics, built-in task packs, advice-only run fix packages, static dashboard, local skill catalog workflows, and federation-demo checks as the next operator-facing usability layer before adding heavier runtime dependencies.

## Onboarding and Diagnostics

Current public agent-building guidance puts the shortest path first: create one working agent, then add model/provider choices, tools, handoffs, guardrails, tracing, and evaluation as the workflow matures. `easy-agent` should keep matching that sequence in its own developer experience.

Next reinforcement for usability:

- keep `setup --provider mock` and `quickstart --provider mock` as the first commands in docs and CI smoke, because they prove config loading, skills, storage, tool calls, trace persistence, and preflight diagnostics without requiring secrets
- keep `new <scenario>` as the shortest path from intent to a runnable project, and keep `wizard --scenario <name>` as the guided path that adds static checks, next commands, and optional mock smoke before users write YAML by hand
- keep `config doctor` as the static risk gate before live-provider runs, with checks for env readiness, MCP roots/auth, federation auth, executor readiness, storage portability, and human-loop coverage
- keep template variants mapped to shipped runtime contracts, then deepen them with focused smoke tests for approval flow, harness flow, MCP resource catalog flow, federation loopback flow, and workbench-backed coding tasks
- make `runs explain` the default next step after failed runs, then use `runs fix` when the user needs a packaged advice-only repair prompt, safe commands, and task-pack selection without mutating the repository
- keep traces as the debugging source of truth first, use `traces open` and the searchable HTML export for local inspection, use `report latest`, dashboard HTML, and report trend HTML to summarize available evidence, then promote stable trace fields into public evaluation and OpenTelemetry export contracts
- make every new high-level feature ship with a mock-backed smoke path plus an optional live-provider path, so first-run experience stays reliable even when credentials are missing
- keep the Python `AgentApp` facade intentionally thin, so embedded applications use the same config-driven runtime as the CLI instead of drifting into a second orchestration surface
- keep browser work MCP-first: `browser-agent` now uses `browser.enabled: true` with Playwright MCP, default isolated/headless execution, local browser artifacts, and approval-gated sensitive browser actions; next work should harden catalog drift, artifact lifecycle, and browser-specific approval UX rather than adding a second native browser stack prematurely

Reference:

- <https://developers.openai.com/api/docs/guides/agents#choose-your-starting-point>
- <https://developers.openai.com/api/docs/guides/agents/quickstart>
- <https://developers.openai.com/api/docs/guides/agents/integrations-observability>

## Operator Productivity Surfaces

The newest CLI layer should make common work executable without forcing users to know the full YAML/runtime shape first:

- use `connectors list`, `connectors doctor`, and `connectors test <name>` as static readiness checks for model, storage, search, MCP, workbench, federation, and browser-facing surfaces
- keep browser diagnostics explicit about the current boundary: Playwright MCP configuration and readiness are shipped, but live navigation, screenshots, forms, and downloads still depend on the user's local Node/npm, browser, MCP startup, and approval settings
- use `task list`, `task show`, and `task run` as a packaged workflow layer for repository review, bug fixing, docs refresh, release checks, data summaries, and federation loopback validation
- use `dashboard` as a static local operations page before adding a persistent web server, because it keeps the operator view dependency-free and easy to archive with run evidence
- keep `task run --dry-run` useful for prompt review and approvals before a task is sent to a model-backed agent
- use `report trend` to compare local benchmark, public-eval, and real-network artifacts over time instead of reading individual JSON files by hand
- keep `traces export --otel-json` explicitly experimental while the local trace tree remains the source of truth, because OpenTelemetry GenAI conventions are still evolving
- use `skills catalog list/install` and `plugins doctor` as a local install/readiness path before adding remote marketplace or signed plugin distribution
- use `integration federation-demo` as a lightweight federation proof before running the complete real-network matrix
- keep skill metadata fields such as risk, dependencies, and smoke prompts current so CLI and docs can expose which skills are safe to run in mock-first or live-provider flows

## Web Search Reinforcement

- Keep SerpApi `/search.json` as the explicit search transport for repo-pinned BFCL evaluation.
- Preserve quota ledger and replay fallback behavior.
- Keep tightening result-id grounding so `web.contents` consumes only URLs justified by the latest grounded search step or replay evidence.
- Preserve the shipped exact-title, search-plus-contents, and memory-backed agentic cases as a regression floor.
- Extend the current repo-pinned green path and the official manifest slice path into wider official BFCL v4-style search-plus-contents, multihop, and remaining agentic cases, where the final answer stays grounded to retrieved evidence.
- Keep a durable per-case search history plus source ledger so later hops can reuse grounded result ids, grounded URLs, cached contents, and already justified sources without widening to ungrounded links.
- Keep `web.contents` aligned to BFCL v4-style `truncate` / `markdown` / `raw` content modes so answer extraction can choose between concise text, readable document text, and markup-sensitive payloads.
- When a grounded page fetch fails, retry within the grounded search set before falling back to replay-backed contents; do not silently widen the URL boundary.
- Keep exposing whether a case used cache, network, or replay-backed contents so long-term BFCL web-search quality can be tracked separately from headline pass or fail.
- Keep using query normalization only as a wrapper-removal step, for example through `x-easy-agent-normalizer: web_search_query`, so score improvements come from better grounding instead of looser matching.
- Extend the final-answer path to stay compatible with either concise plain text or a structured `{"answer": ..., "context": ...}` payload so answer scoring remains strict without becoming brittle.
- Keep memory semantics explicit by validating tool-result truth for read/delete style cases instead of relying on argument matches alone.

Better next directions after the current baseline:

- add grounded-source visibility closer to the source-oriented evidence shape now exposed by OpenAI web-search responses
- add domain-aware or source-aware query constraints for cases where official-doc grounding matters more than generic search recall
- add wider official BFCL web-search multihop slices that explicitly separate query-planning misses from fetch-grounding misses
- align the local BrowseComp/SimpleQA ingestion path with the current OpenAI `simple-evals` repository layout without vendoring benchmark question content into this repository
- keep the grader path explicit so official or official-style grading does not silently downgrade to heuristic exact-match mode

## Provider Compatibility

Use the official OpenAI constraints as the baseline:

- `strict: true`
- `additionalProperties: false`
- nullable and optional parameter modeling
- parallel tool-call controls
- single-tool-call enforcement for BFCL-style single-call cases
- `tool_choice` / forced-tool / no-tool / required-tool mode parity
- all-fields-required plus nullable promotion for optional fields under strict structured outputs

Then keep the provider-specific adaptation layers explicit:

- OpenAI-compatible:
  - keep strict structured outputs as the default path
  - preserve nullable-as-required modeling for official JSON Schema constraints
  - keep `parallel_tool_calls` and forced function selection observable in telemetry
  - keep both `chat_completions` and `responses` payload paths under one regression matrix
- Anthropic:
  - map provider-neutral tool-choice controls onto `tool_choice`
  - use `disable_parallel_tool_use` for serialized tool-call cases
  - keep strict-tool emission aligned with the current Claude tool definition surface
  - normalize tool input schemas before request emission so strict object shape, `additionalProperties: false`, and nullable-required promotion stay regression-covered instead of docs-only
- Gemini:
  - map provider-neutral tool-choice controls onto `functionCallingConfig.mode`
  - use `allowedFunctionNames` for forced-tool or required-tool cases
  - keep the schema surface normalized to the supported OpenAPI-style subset before request emission, including the current strict nullable or optional modeling path
  - avoid over-claiming explicit single-call enforcement when the provider only exposes mode-level controls

The shipped regression floor now covers:

- strict schema transport
- `additionalProperties: false`
- nullable preservation
- optional-to-required-nullable promotion
- single-call and parallel-call controls
- `auto` / `none` / `required` / forced tool-choice behavior
- explicit failure when `required` or `force` mode ends up with no selected tool after filtering
- OpenAI-compatible Responses payload parity
- OpenAI-compatible Responses response parsing parity
- live DeepSeek/OpenAI-compatible verification for strict-schema, no-tool, required-tool, and forced-tool flows
- explicit `best_effort` classification for non-OpenAI OpenAI-compatible single-tool control when the field is exposed but not enforced strictly at runtime

Better next directions after the current baseline:

- expand live provider-specific compatibility runs to optional Anthropic and Gemini targets when credentials are available, plus OpenAI-compatible `/responses` surfaces where providers actually support them
- keep the provider capability matrix explicit about what is normalized, what is enforced, and what still depends on provider-specific best effort
- reduce the remaining best-effort gap around serialized tool calls on OpenAI-compatible providers without weakening BFCL single-call regression checks
- extend the same explicit matrix discipline into future non-OpenAI-compatible realtime or streaming tool surfaces only after the current live matrix is stable

Reference:

- <https://developers.openai.com/api/docs/guides/function-calling>
- <https://developers.openai.com/api/docs/guides/structured-outputs>
- <https://developers.openai.com/api/docs/guides/tools-web-search>
- <https://developers.openai.com/api/docs/guides/latest-model#using-reasoning-models>
- <https://developers.openai.com/api/docs/libraries#install-the-agents-sdk>
- <https://github.com/openai/simple-evals>
- <https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview>
- <https://ai.google.dev/gemini-api/docs/function-calling>
- <https://gorilla.cs.berkeley.edu/blogs/15_bfcl_v4_web_search.html>
- <https://serpapi.com/search-api>

## MCP and Federation

The current durable MCP baseline includes:

- `resources/list`
- `resources/read`
- `resources/templates/list`
- `resources/subscribe`
- `resources/unsubscribe`
- `prompts/list`
- `prompts/get`
- durable catalog snapshots for tools, resources, and prompts
- durable catalog snapshots for resource templates and prompt-detail cache entries
- durable resource-subscription state

Next reinforcement should continue around the official MCP surface:

- static auth and roots diagnostics for stdio, HTTP SSE, and streamable HTTP transports before runtime connection
- `notifications/resources/list_changed`
- `notifications/tools/list_changed`
- `notifications/prompts/list_changed`
- `notifications/resources/updated`
- prompt or resource template refresh coordination and richer cached metadata
- prompt-detail refresh telemetry and diff-aware invalidation

Federation should continue to track the public A2A surface rather than inventing a private transport:

- keep agent-card metadata, push notification config, streaming, and resubscribe checks visible in config diagnostics and real-network scenarios
- keep well-known agent-card discovery, send, sendSubscribe, resubscribe, task events, and push notification config flows visible in the real-network matrix
- keep signed callback and task authorization evidence in the report instead of relying on headline pass/fail
- keep skipped host-gated rows visible so missing container or microVM dependencies are reported as coverage gaps, not silent omissions

## Observability and Storage Contracts

The next runtime-hardening layer should move from raw event logs toward trace contracts:

- keep `runs list`, `runs show`, and `traces export` as the primary debugging surface
- keep span ids stable across run, graph node, agent turn, model call, tool call, MCP call, approval, harness, and federation boundaries
- record duration, status, input/output hashes, retry count, and checkpoint id on each span
- keep storage repository contracts explicit so future PostgreSQL support can implement the same run, session, checkpoint, human-request, MCP, federation, workbench, and trace interfaces
- map the stable JSON trace shape to OpenTelemetry GenAI spans only after local semantics stop moving, especially around agent/model/tool spans, error types, and operation attributes

## Executor Trust Boundary

Executor reports should keep describing what each backend does and does not isolate:

- process executor is a development and trusted-workload path, not a production sandbox by itself
- container executor must report bind mounts, runtime network defaults, resource constraints, env injection, and checkpoint-image behavior
- microVM executor must report guest sync boundaries, SSH command channel behavior, host dependencies, and snapshot drift
- real-network rows should keep pairing performance telemetry with safety assertions so warm-start success does not hide weak isolation assumptions

Reference:

- <https://modelcontextprotocol.io/specification/2025-03-26/server/resources>
- <https://modelcontextprotocol.io/specification/2025-11-25/schema>
- <https://github.com/microsoft/playwright-mcp>
- <https://a2a-protocol.org/latest/specification/>
- <https://opentelemetry.io/docs/specs/semconv/gen-ai/>

## Documentation Policy

- Keep the README formal and score-only.
- Keep detailed results, usage notes, and reinforcement plans in `reference/en/` and `reference/zh/`.
- Keep English README pointing only to English reference documents, and Chinese README pointing only to Chinese reference documents.
