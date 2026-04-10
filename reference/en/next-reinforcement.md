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
