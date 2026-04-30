# 下一步补强

本路线图以已发布的 `0.3.5` 基线为起点。

## 当前重点

- 继续降低 runtime 复杂度，把大型兼容模块拆成更小但 import-compatible 的 surface，并把 storage contracts 与 trace helpers 从 SQLite 细节里分离出来。
- 把已经交付的零凭据上手层固定为长期兼容门禁：`setup` preflight checks、`mock` quickstart、config explanation、config doctor、starter templates、可搜索 HTML traces 与 run explanation 测试要先于 live-provider 套件执行。
- 把新的 run summary、可搜索 HTML trace viewer、单文件 latest-report HTML 与 trace-tree export 推进成主要排障入口；等本地 JSON trace 形态稳定后，再对齐 OpenTelemetry GenAI semantic conventions。
- 把已经交付的 live provider-specific 兼容证据继续扩展到必跑的 DeepSeek/OpenAI-compatible 基线之外，在有凭据时补齐 Anthropic 与 Gemini 覆盖。
- 把 raw official BFCL v4 归一化路径继续推进到更广的 agentic 与 multihop 覆盖，并补齐更清晰的官方分类诊断。
- 在拿到本地数据导出与 grader 凭据后，把新交付的 `official_source_search` 与 `browsecomp_subset` / `simpleqa_subset` 支持推进成可刷新分数的评测切片。
- 在不随意扩大 model-facing runtime surface 的前提下，继续深化 MCP 通知对齐，包括 resource updates、prompt-detail refresh 与 template diff telemetry。
- 把新的 wizard、connector diagnostics、workflow YAML doctor/plan/run、`runs inspect`、`runs bundle`、browser smoke/snapshot/audit/seo/a11y/links/report helpers、browser doctor/artifact inspection、browser-specific task packs、advice-only HTML run fix package、带可复制命令的静态 dashboard workflow/template recommendations、本地 skill catalog workflow、Python `AgentApp` workflow/browser/bundle helpers 与 federation-demo checks 作为下一层 operator-facing 易用性入口，再考虑引入更重的 runtime 依赖。

## 上手与诊断补强

当前公开的 agent 构建指引强调先走最短路径：先让一个 agent 跑起来，再逐步加入模型/供应商选择、工具、handoff、guardrails、tracing 与 evaluation。`easy-agent` 自己的开发体验也应该保持这个顺序。

下一步可落地的易用性补强：

- 把 `setup --provider mock` 与 `quickstart --provider mock` 保持为文档和 CI smoke 的第一组命令，因为它们可以在无 secret 的情况下验证 config loading、skills、storage、tool calls、trace persistence 与 preflight diagnostics
- 把 `new <scenario>` 保持为从意图到可运行项目的最短路径，同时把 `wizard --scenario <name>` 作为带静态检查、下一步命令和可选 mock smoke 的 guided path，减少用户一开始手写 YAML 的需求
- 把 `config doctor` 保持为 live-provider 运行前的静态风险门禁，覆盖 env readiness、MCP roots/auth、federation auth、executor readiness、storage portability 与 human-loop coverage
- 模板继续只围绕已交付 runtime contract 扩展，并为 approval flow、harness flow、MCP resource catalog flow、federation loopback flow 与 workbench-backed coding tasks 增加 focused smoke tests
- 把 `runs inspect` 做成失败 run 后默认的下一步，同时保留 `runs explain` 作为原始分类器输出，并保留 `runs triage` 作为紧凑的 severity/actionability 视图；当用户需要可交接的修复建议时，使用 `runs fix` 生成 advice-only repair prompt、安全命令、HTML 交接页和 task-pack 选择；当用户需要完整 handoff 目录时，用 `runs bundle` 同步导出 run summary、triage、fix、trace 与 browser artifact 证据
- 让 trace 先作为排障事实来源，用 `traces open` 和可搜索 HTML export 改善本地检查体验，用 `report latest`、dashboard HTML 与 report trend HTML 汇总可用证据，等字段稳定后再提升为 public evaluation 与 OpenTelemetry export contract
- 每个新的高层能力都配套 mock-backed smoke path 和可选 live-provider path，让首次运行不再依赖本地凭据是否齐全
- 让 Python `AgentApp` facade 保持轻量，让嵌入式应用继续复用 CLI 同款 config-driven runtime，而不是形成第二套 orchestration surface
- 让 browser 能力继续保持 MCP-first：`browser-agent`、`web-monitor-agent`、`seo-agent`、`website-audit-agent` 与 `competitor-research-agent` 现在通过 `browser.enabled: true` 使用 Playwright MCP，默认 isolated/headless、本地浏览器工件目录、browser doctor/artifact/smoke/snapshot/audit/seo/a11y/links/report 命令与敏感浏览器操作审批；下一步重点应放在 catalog drift、artifact lifecycle、page-quality audit evidence 和 browser-specific 审批体验，而不是过早引入第二套 native browser stack

参考：

- <https://developers.openai.com/api/docs/guides/agents#choose-your-starting-point>
- <https://developers.openai.com/api/docs/guides/agents/quickstart>
- <https://developers.openai.com/api/docs/guides/agents/integrations-observability>
- <https://github.com/microsoft/playwright-mcp>

## Operator Productivity Surfaces

最新 CLI 层应该让常见工作可以直接执行，而不是要求用户先理解完整 YAML/runtime 结构：

- 用 `connectors list`、`connectors doctor` 和 `connectors test <name>` 做 model、storage、search、MCP、workbench、federation 与 browser-facing surface 的静态就绪检查
- browser 诊断需要明确当前边界：Playwright MCP 配置、static doctor 和 artifact listing 已经交付，但真实导航、截图、表单与下载仍取决于用户本地 Node/npm、browser、MCP startup 与审批设置
- 用 `task list`、`task show` 和 `task run` 承载 repo review、bug fix、docs refresh、release check、data summary、browser QA/research/form checks 与 federation loopback validation 等打包 workflow
- 用 `workflow list`、`workflow show`、`workflow init`、`workflow doctor`、`workflow plan` 和 `workflow run` 把 task packs 包成 operator-facing workflow，通过可携带的 `workflow.yml`、static preflight checks、可选 bundle-on-completion 与 next commands，再进入 model-backed execution
- 用 `runs inspect` 作为 dashboard suggestions、browser report 与 workflow result 里的短路径失败诊断入口
- 用 `dashboard` 作为静态本地运营页面，先不引入持久 Web server；这样 operator view 仍然无依赖，并且方便把 failed runs、approvals、browser readiness、browser artifacts、workflow recommendations、template recommendations、可复制 inspect/bundle 命令和 run evidence 一起归档
- 保持 `task run --dry-run`、`workflow plan` 与 `workflow run --dry-run` 对 prompt review 与审批前检查有用，避免任务直接进入 model-backed agent
- 用 `report trend` 对比本地 benchmark、public-eval 与 real-network artifacts 的变化，而不是手工阅读多个 JSON 文件
- `traces export --otel-json` 继续标记为 experimental；在 OpenTelemetry GenAI conventions 仍在演进时，本地 trace tree 仍是事实来源
- 用 `skills catalog list/install` 和 `plugins doctor` 作为本地安装/就绪路径，再推进远程 marketplace 或 signed plugin distribution
- 用 `integration federation-demo` 作为轻量 federation proof，再运行完整 real-network matrix
- 持续维护 skill metadata 中的 risk、dependencies 与 smoke prompt，让 CLI 和文档可以说明哪些 skill 适合 mock-first 或 live-provider flow

这一层的外部对齐方向：

- Playwright MCP 文档强调 snapshot-first browser state、screenshots、headless/isolated execution 与 output-directory artifact collection，这和当前 browser doctor/artifact/smoke/snapshot/report split 保持一致。
- MCP 2025-11-25 继续把 roots、sampling、elicitation 与 user-consent boundaries 显式化，所以 browser 与 filesystem 权限扩展仍应留在 runtime-owned approvals 后面。
- OpenAI Agents 指引把 tracing 放在正式 evaluation loop 之前，因此静态 dashboard 与 failure-to-fix HTML 应继续作为证据视图，而不是自动修复面。
- OpenTelemetry GenAI agent/tool spans 仍在演进中，所以 `traces export --otel-json` 在本地 trace 字段稳定前应继续保持 experimental。
- A2A task/event/push surfaces 仍应先通过 real-network proof rows 固化证据，再把 public federation API 视为 compatibility complete。

## Web Search 补强

- 继续以 SerpApi `/search.json` 作为 repo-pinned BFCL 评测的显式搜索链路。
- 保留 quota ledger 与 replay fallback。
- 继续收紧 result-id grounding，让 `web.contents` 只消费由最近一次 grounded search 或 replay 证据支撑的 URL。
- 把当前已经交付的 exact-title、search-plus-contents 与 memory-backed agentic case 作为回归基线。
- 在此基础上继续把 repo-pinned green path 与 official manifest slice path 扩展到更广的官方 BFCL v4 风格 search-plus-contents、multihop 与剩余 agentic case，并保持最终答案对检索证据可回溯。
- 为每个 case 保留 durable search history 与 source ledger，让后续 hop 可以复用 grounded result id、grounded URL、缓存过的 contents 和已经成立的证据来源，而不是放宽到未 grounding 的链接。
- 继续把 `web.contents` 对齐到更接近 BFCL v4 的 `truncate` / `markdown` / `raw` 内容模式，让答案抽取可以在简洁文本、可读文档文本与 markup-sensitive 载荷之间切换。
- 当 grounded page fetch 失败时，先在 grounded search set 内重试，再退回 replay-backed contents；不要静默扩大 URL 边界。
- 持续暴露每个 case 到底用了 cache、network 还是 replay-backed contents，这样长期跟踪 BFCL web-search 质量时，就能把 headline pass/fail 和来源质量拆开看。
- 继续把查询归一化限定在去包装语这一步，例如通过 `x-easy-agent-normalizer: web_search_query`，这样分数提升来自更好的 grounding，而不是更松的匹配规则。
- 让最终答案同时兼容简洁纯文本或 `{"answer": ..., "context": ...}` 这样的结构化载荷，这样 answer scoring 可以继续严格，而不是靠放松 evaluator 来提分。
- 对 memory read/delete 这类 case，继续保持 tool-result truth 校验，而不是只看 arguments 命中。

在当前基线之上的更好发展方向：

- 把 grounded-source visibility 继续推进到更接近 OpenAI web-search 响应里 source-oriented evidence 的形态
- 为“官方文档优先”这类 case 增加 domain-aware / source-aware query constraints，而不是只追求泛化搜索召回
- 在更广的 official BFCL web-search multihop 切片里，把 query planning miss 与 fetch grounding miss 分开统计
- 把本地 BrowseComp/SimpleQA 数据接入路径继续对齐当前 OpenAI `simple-evals` 仓库布局，但不要把 benchmark 题目直接 vendored 到本仓库
- 保持 grader 路径显式化，避免官方或官方风格 grading 在缺少凭据时静默回退成 heuristic exact match

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
  - 让 `chat_completions` 与 `responses` 两条路径都落在同一套回归矩阵里
- Anthropic：
  - 把 provider-neutral tool-choice controls 映射到 `tool_choice`
  - 在串行工具调用场景使用 `disable_parallel_tool_use`
  - 让 strict-tool 发包继续对齐当前 Claude tools 定义面
  - 在发请求前对 tool input schema 做归一化，这样 strict object shape、`additionalProperties: false` 与 nullable-required promotion 就是测试覆盖的真实能力，而不只是文档描述
- Gemini：
  - 把 provider-neutral tool-choice controls 映射到 `functionCallingConfig.mode`
  - 对 forced-tool / required-tool 场景使用 `allowedFunctionNames`
  - 在发请求前继续把 schema 收敛到 provider 支持的 OpenAPI-style 子集，并覆盖当前 strict nullable / optional 参数建模路径
  - 不要把 provider 只有 mode-level 控制的能力误写成显式 single-call enforcement

当前已经交付的回归基线包括：

- strict schema transport
- `additionalProperties: false`
- nullable preservation
- optional-to-required-nullable promotion
- 单调用与并行调用控制
- `auto` / `none` / `required` / forced tool-choice 行为
- 当 `required` 或 `force` 模式在过滤后没有可用工具时，显式失败而不是静默降级
- OpenAI-compatible Responses payload 对齐
- OpenAI-compatible Responses response parsing 对齐
- 针对 DeepSeek/OpenAI-compatible 的 live 验证，覆盖 strict-schema、no-tool、required-tool 与 forced-tool 流程
- 当非 OpenAI 官方的 OpenAI-compatible provider 暴露 single-tool 控制字段但运行时不严格执行时，显式标记为 `best_effort`

在当前基线之上的更好发展方向：

- 在有凭据时把 live provider-specific 回归继续扩展到 Anthropic、Gemini，以及 provider 真正支持时的 OpenAI-compatible `/responses` surface
- 继续把 provider capability matrix 写清楚哪些能力是归一化实现、哪些是显式约束、哪些仍然依赖 provider-specific best effort
- 在不放松 BFCL 单调用回归约束的前提下，继续缩小 OpenAI-compatible provider 在串行工具调用上的 best-effort 缺口
- 等当前 live matrix 稳定之后，再把同样的显式矩阵方法扩展到未来的 realtime 或 streaming tool surface

参考：

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

## MCP 与 Federation

当前 durable MCP 基线已经包括：

- `resources/list`
- `resources/read`
- `resources/templates/list`
- `resources/subscribe`
- `resources/unsubscribe`
- `prompts/list`
- `prompts/get`
- tools/resources/prompts 的 durable catalog snapshots
- resource templates 与 prompt-detail cache entries 的 durable catalog snapshots
- resource subscription 的 durable state

下一步继续围绕官方 MCP surface 推进：

- 在 runtime connection 前提供 stdio、HTTP SSE 与 streamable HTTP transport 的静态 auth / roots 诊断
- `notifications/resources/list_changed`
- `notifications/tools/list_changed`
- `notifications/prompts/list_changed`
- `notifications/resources/updated`
- prompt 或 resource template refresh coordination 与更丰富的缓存元数据
- prompt-detail refresh telemetry 与 diff-aware invalidation

Federation 继续对齐公开 A2A surface，而不是走私有传输：

- 让 agent-card metadata、push notification config、streaming 与 resubscribe 检查同时出现在 config diagnostics 和 real-network scenarios 中
- 让 well-known agent-card discovery、send、sendSubscribe、resubscribe、task events 与 push notification config 继续出现在 real-network matrix 里
- 把 signed callback 与 task authorization 证据保留在 report 中，而不是只看 headline pass/fail
- host-gated 的 container / microVM 行如果缺少依赖，继续显示为 skipped coverage gap，不要静默删除

## Observability 与 Storage Contracts

下一层 runtime hardening 应该从 raw event log 继续走向 trace contracts：

- 把 `runs list`、`runs show`、`traces export` 保持为主要排障入口
- run、graph node、agent turn、model call、tool call、MCP call、approval、harness 与 federation 边界都要保留稳定 span id
- 每个 span 记录 duration、status、input/output hash、retry count 与 checkpoint id
- storage repository contracts 要保持显式，这样未来 PostgreSQL 可以实现同一套 run、session、checkpoint、human-request、MCP、federation、workbench 与 trace 接口
- 等本地 JSON trace 语义稳定后，再映射到 OpenTelemetry GenAI spans，尤其是 agent/model/tool spans、error types 与 operation attributes

## Executor Trust Boundary

Executor report 应继续说明每个 backend 隔离什么、不隔离什么：

- process executor 是开发和可信 workload 路径，本身不是生产 sandbox
- container executor 需要报告 bind mounts、runtime network defaults、resource constraints、env injection 与 checkpoint-image 行为
- microVM executor 需要报告 guest sync boundary、SSH command channel、host dependencies 与 snapshot drift
- real-network 行应该继续把性能 telemetry 与安全断言放在一起，避免 warm-start 成功掩盖隔离假设不足

参考：

- <https://modelcontextprotocol.io/specification/2025-03-26/server/resources>
- <https://modelcontextprotocol.io/specification/2025-11-25/schema>
- <https://github.com/microsoft/playwright-mcp>
- <https://a2a-protocol.org/latest/specification/>
- <https://opentelemetry.io/docs/specs/semconv/gen-ai/>

## 文档策略

- README 保持正式、精简、只展示分数。
- 详细结果、详细使用说明、详细补强路线统一放到 `reference/en/` 与 `reference/zh/`。
- 英文 README 只链接英文 reference 文档，中文 README 只链接中文 reference 文档。
