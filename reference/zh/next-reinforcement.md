# 下一步补强

## 当前重点

- 继续压缩 BFCL web-search miss，重点围绕 single-call enforcement、query shaping、result grounding 与 replay-backed contents recovery。
- 继续强化 OpenAI-compatible provider 在 strict function calling 与 structured outputs 约束下的兼容行为。
- 在不随意扩大 public runtime surface 的前提下，继续推进 MCP 与 federation 的 durable coordination。

## Web Search 补强

- 继续以 SerpApi `/search.json` 作为显式搜索链路。
- 保留 quota ledger 与 replay fallback。
- 继续收紧 grounding，让 `web.contents` 只消费由最近一次 search 或 replay 证据支撑的 URL。

## Provider 兼容性

以 OpenAI 官方约束为基线继续推进：

- `strict: true`
- `additionalProperties: false`
- nullable 与 optional 参数建模
- parallel tool-call controls

参考：

- <https://platform.openai.com/docs/guides/function-calling?api-mode=chat>
- <https://platform.openai.com/docs/guides/structured-outputs>

## MCP 与 Federation

继续围绕官方 MCP surface 推进：

- `notifications/resources/list_changed`
- `notifications/tools/list_changed`
- `notifications/prompts/list_changed`
- `resources/subscribe`

参考：

- <https://modelcontextprotocol.io/specification/2025-03-26/server/resources>
- <https://modelcontextprotocol.io/specification/2025-11-25/schema>

## 文档策略

- README 保持正式、精简、只展示分数。
- 详细结果、详细使用说明、详细补强路线统一放到 `reference/en/` 与 `reference/zh/`。
- 英文 README 只链接英文 reference 文档，中文 README 只链接中文 reference 文档。
