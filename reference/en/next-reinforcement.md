# Next Reinforcement

This roadmap starts from the published `0.3.5` baseline.

## Immediate Focus

- Turn the shipped OpenAI-compatible chat-completions and Responses API parity into live provider-specific compatibility evidence.
- Extend the raw official BFCL v4 normalization path into wider agentic and multihop coverage with clearer official-category diagnostics.
- Deepen MCP notification parity around resource updates, prompt-detail refresh, and template diff telemetry without widening the model-facing runtime surface.

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
- Extend the final-answer path to stay compatible with either concise plain text or a structured `{"answer": ..., "context": ...}` payload so answer scoring remains strict without becoming brittle.
- Keep memory semantics explicit by validating tool-result truth for read/delete style cases instead of relying on argument matches alone.

Better next directions after the current baseline:

- add grounded-source visibility closer to the source-oriented evidence shape now exposed by OpenAI web-search responses
- add domain-aware or source-aware query constraints for cases where official-doc grounding matters more than generic search recall
- add wider official BFCL web-search multihop slices that explicitly separate query-planning misses from fetch-grounding misses

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

Better next directions after the current baseline:

- add live provider-specific compatibility runs for the current strict function-calling matrix instead of relying only on static payload inspection
- keep the provider capability matrix explicit about what is normalized, what is enforced, and what still depends on provider-specific best effort
- extend the same explicit matrix discipline into future non-OpenAI-compatible realtime or streaming tool surfaces only after the current live matrix is stable

Reference:

- <https://developers.openai.com/api/docs/guides/function-calling>
- <https://developers.openai.com/api/docs/guides/structured-outputs>
- <https://platform.openai.com/docs/guides/tools-web-search>
- <https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools>
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

- `notifications/resources/list_changed`
- `notifications/tools/list_changed`
- `notifications/prompts/list_changed`
- `notifications/resources/updated`
- prompt or resource template refresh coordination and richer cached metadata
- prompt-detail refresh telemetry and diff-aware invalidation

Reference:

- <https://modelcontextprotocol.io/specification/2025-03-26/server/resources>
- <https://modelcontextprotocol.io/specification/2025-11-25/schema>

## Documentation Policy

- Keep the README formal and score-only.
- Keep detailed results, usage notes, and reinforcement plans in `reference/en/` and `reference/zh/`.
- Keep English README pointing only to English reference documents, and Chinese README pointing only to Chinese reference documents.
