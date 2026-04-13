# Next Reinforcement

This roadmap starts from the published `0.3.4` baseline.

## Immediate Focus

- Keep the now-green repo-pinned BFCL agentic slice green while extending it toward wider official BFCL v4 coverage.
- Continue hardening OpenAI-compatible provider behavior around strict function calling and structured outputs.
- Keep MCP and federation durability moving forward without widening the public runtime surface unnecessarily.

## Web Search Reinforcement

- Keep SerpApi `/search.json` as the explicit search transport for repo-pinned BFCL evaluation.
- Preserve quota ledger and replay fallback behavior.
- Keep improving result-id grounding so `web.contents` consumes only URLs justified by the latest search step or replay evidence.
- Preserve the shipped exact-title, search-plus-contents, and memory-backed agentic cases as a regression floor.
- Extend the current repo-pinned green path into wider official BFCL v4-style search-plus-contents, multihop, and remaining multi-tool cases, where the final answer should remain grounded to the retrieved evidence.
- Keep a durable per-case search history so later hops can reuse grounded result ids, grounded URLs, and previously fetched page evidence without widening to ungrounded links.
- Extend `web.contents` toward the BFCL v4-style `truncate` / `markdown` / `raw` content modes so answer extraction can choose between concise text, readable document text, and markup-sensitive payloads.
- When a grounded page fetch fails, retry within the grounded search set before falling back to replay-backed contents; do not silently widen the URL boundary.
- Keep the final-answer path compatible with either concise plain text or a structured `{"answer": ..., "context": ...}` payload so answer scoring stays robust without loosening the evaluator.
- Keep memory semantics explicit by validating tool-result truth for read/delete style cases instead of relying on argument matches alone.

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
- Anthropic:
  - map provider-neutral tool-choice controls onto `tool_choice`
  - use `disable_parallel_tool_use` for serialized tool-call cases
  - keep strict-tool emission aligned with the current Claude tool definition surface
- Gemini:
  - map provider-neutral tool-choice controls onto `functionCallingConfig.mode`
  - use `allowedFunctionNames` for forced-tool or required-tool cases
  - keep the schema surface normalized to the supported OpenAPI-style subset before request emission
  - avoid over-claiming explicit single-call enforcement when the provider only exposes mode-level controls

Public regression coverage should continue to assert:

- strict schema transport
- `additionalProperties: false`
- nullable preservation
- optional-to-required-nullable promotion
- single-call and parallel-call controls
- `auto` / `none` / `required` / forced tool-choice behavior
- OpenAI-compatible parity on the current chat-completions style tool surface before claiming broader compatibility

Reference:

- <https://developers.openai.com/api/docs/guides/function-calling>
- <https://developers.openai.com/api/docs/guides/structured-outputs>
- <https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools>
- <https://ai.google.dev/gemini-api/docs/function-calling>
- <https://gorilla.cs.berkeley.edu/blogs/15_bfcl_v4_web_search.html>

## MCP and Federation

Continue reinforcing the official MCP surface:

- `notifications/resources/list_changed`
- `notifications/tools/list_changed`
- `notifications/prompts/list_changed`
- `resources/subscribe`

Reference:

- <https://modelcontextprotocol.io/specification/2025-03-26/server/resources>
- <https://modelcontextprotocol.io/specification/2025-11-25/schema>

## Documentation Policy

- Keep the README formal and score-only.
- Keep detailed results, usage notes, and reinforcement plans in `reference/en/` and `reference/zh/`.
- Keep English README pointing only to English reference documents, and Chinese README pointing only to Chinese reference documents.
