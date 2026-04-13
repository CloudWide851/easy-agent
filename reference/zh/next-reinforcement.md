# 下一步补强

本路线图以已发布的 `0.3.4` 基线为起点。

## 当前重点

- 在保持 repo-pinned BFCL agentic 子集全绿的前提下，继续向更广的 official BFCL v4 覆盖面推进。
- 继续强化 OpenAI-compatible provider 在 strict function calling 与 structured outputs 约束下的兼容行为。
- 在不随意扩大 public runtime surface 的前提下，继续推进 MCP 与 federation 的 durable coordination。

## Web Search 补强

- 继续以 SerpApi `/search.json` 作为 repo-pinned BFCL 评测的显式搜索链路。
- 保留 quota ledger 与 replay fallback。
- 继续收紧 result-id grounding，让 `web.contents` 只消费由最近一次 search 或 replay 证据支撑的 URL。
- 把当前已经交付的 exact-title、search-plus-contents 与 memory-backed agentic case 作为回归基线。
- 在此基础上继续扩展到更广的官方 BFCL v4 风格 search-plus-contents、multihop 与剩余 multi-tool case，并保持最终答案对检索证据可回溯。
- 为每个 case 保留 durable search history，让后续 hop 可以复用 grounded result id、grounded URL 和已抓取页面证据，而不是放宽到未 grounding 的链接。
- 继续把 `web.contents` 推进到更接近 BFCL v4 的 `truncate` / `markdown` / `raw` 内容模式，让答案抽取可以在简洁文本、可读文档文本与 markup-sensitive 载荷之间切换。
- 当 grounded page fetch 失败时，先在 grounded search set 内重试，再退回 replay-backed contents；不要静默扩大 URL 边界。
- 让最终答案同时兼容简洁纯文本或 `{\"answer\": ..., \"context\": ...}` 这样的结构化载荷，这样可以增强 answer scoring，而不是放松 evaluator。
- 对 memory read/delete 这类 case，继续保持 tool-result truth 校验，而不是只看 arguments 命中。

## Provider 兼容性

以 OpenAI 官方约束为基线继续推进：

- `strict: true`
- `additionalProperties: false`
- nullable 与 optional 参数建模
- parallel tool-call controls
- BFCL 单调用场景的一次调用约束
- `tool_choice` / forced-tool / no-tool / required-tool 的模式对齐
- strict structured outputs 下 optional-to-required-nullable 的官方建模方式

同时把 provider-specific 适配层继续显式化：

- OpenAI-compatible：
  - 保持 strict structured outputs 作为默认路径
  - 按官方 JSON Schema 约束继续保留 nullable-as-required 建模
  - 让 `parallel_tool_calls` 与 forced function selection 保持可观测
- Anthropic：
  - 把 provider-neutral tool-choice controls 映射到 `tool_choice`
  - 在串行工具调用场景使用 `disable_parallel_tool_use`
  - 让 strict-tool 发包继续对齐当前 Claude tools 定义面
- Gemini：
  - 把 provider-neutral tool-choice controls 映射到 `functionCallingConfig.mode`
  - 对 forced-tool / required-tool 场景使用 `allowedFunctionNames`
  - 在发请求前继续把 schema 收敛到 provider 支持的 OpenAPI-style 子集
  - 不要把 provider 只有 mode-level 控制的能力误写成显式 single-call enforcement

公开回归覆盖需要继续断言：

- strict schema transport
- `additionalProperties: false`
- nullable preservation
- optional-to-required-nullable promotion
- 单调用与并行调用控制
- `auto` / `none` / `required` / forced tool-choice 行为
- 在声称更广兼容性之前，先确保 OpenAI-compatible chat-completions 风格工具面保持可验证对齐

参考：

- <https://developers.openai.com/api/docs/guides/function-calling>
- <https://developers.openai.com/api/docs/guides/structured-outputs>
- <https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools>
- <https://ai.google.dev/gemini-api/docs/function-calling>
- <https://gorilla.cs.berkeley.edu/blogs/15_bfcl_v4_web_search.html>

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
