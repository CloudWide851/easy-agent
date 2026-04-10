# Next Reinforcement

## Immediate Focus

- Continue compressing BFCL web-search misses around single-call enforcement, query shaping, result grounding, and replay-backed contents recovery.
- Continue hardening OpenAI-compatible provider behavior around strict function calling and structured outputs.
- Keep MCP and federation durability moving forward without widening the public runtime surface unnecessarily.

## Web Search Reinforcement

- Keep SerpApi `/search.json` as the explicit search transport.
- Preserve quota ledger and replay fallback behavior.
- Continue improving grounding so `web.contents` only consumes URLs justified by the latest search step or replay evidence.

## Provider Compatibility

Use the official OpenAI constraints as the baseline:

- `strict: true`
- `additionalProperties: false`
- nullable and optional parameter modeling
- parallel tool-call controls
- single-tool-call enforcement for BFCL-style single-call cases
- `tool_choice` / forced-tool / no-tool / required-tool mode parity

Then keep the provider-specific adaptation layers explicit:

- OpenAI-compatible:
  - keep strict structured outputs as the default path
  - preserve nullable-as-required modeling for official JSON Schema constraints
  - keep `parallel_tool_calls` and forced function selection observable in telemetry
- Anthropic:
  - map provider-neutral tool-choice controls onto `tool_choice`
  - use `disable_parallel_tool_use` for serialized tool-call cases
  - preserve the provider's looser `input_schema` pass-through when strict JSON Schema flags are unavailable
- Gemini:
  - map provider-neutral tool-choice controls onto `functionCallingConfig.mode`
  - use `allowedFunctionNames` for forced-tool or required-tool cases
  - keep the schema surface normalized to the supported OpenAPI-style subset before request emission

Public regression coverage should continue to assert:

- strict schema transport
- `additionalProperties: false`
- nullable preservation
- optional-to-required-nullable promotion
- single-call and parallel-call controls
- `auto` / `none` / `required` / forced tool-choice behavior

Reference:

- <https://platform.openai.com/docs/guides/function-calling?api-mode=chat>
- <https://platform.openai.com/docs/guides/structured-outputs>

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
