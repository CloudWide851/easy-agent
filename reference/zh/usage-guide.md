# 使用说明

本文档对应已发布的 `0.3.5` 文档集合。

## 环境准备

```bash
uv venv --python 3.12
uv sync --dev
```

## Model Surface

- `model.openai_api_style` 默认是 `chat_completions`。
- 只有在 OpenAI-compatible endpoint 明确支持 `/responses` 时，才把它切到 `responses`。
- 两条 OpenAI-compatible 路径共用同一套 provider-neutral function-calling 控制面：
  - `strict`
  - `parallel_tool_calls`
  - `mode`
  - `forced_tool_name`
- 这个仓库里的 strict 基线对齐当前 OpenAI 官方约束：
  - `strict: true`
  - `additionalProperties: false`
  - 在 strict structured outputs 场景下，把 optional 字段建模成 required + nullable

## 核心 CLI

```bash
uv run easy-agent --help
uv run easy-agent setup --provider mock
uv run easy-agent wizard --scenario coding-agent --target-dir my-agent --provider mock
uv run easy-agent new coding-agent
uv run easy-agent new research-agent <target-dir>
uv run easy-agent new data-agent
uv run easy-agent new ops-agent
uv run easy-agent new browser-agent <target-dir>
uv run easy-agent new customer-support-agent
uv run easy-agent new sales-agent
uv run easy-agent new document-agent
uv run easy-agent new qa-agent
uv run easy-agent new release-agent
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
uv run easy-agent runs fix <run_id> -c easy-agent.yml --format markdown --output fix.md
uv run easy-agent traces export <run_id> -c easy-agent.yml
uv run easy-agent traces export <run_id> -c easy-agent.yml --html --output trace.html
uv run easy-agent traces open <run_id> -c easy-agent.yml --no-browser
uv run easy-agent traces export <run_id> -c easy-agent.yml --otel-json --output trace-otel.json
uv run easy-agent report latest -c easy-agent.yml
uv run easy-agent report latest -c easy-agent.yml --html --output report.html
uv run easy-agent report trend --history reports --html --output trend.html
uv run easy-agent dashboard -c easy-agent.yml --output dashboard.html
uv run easy-agent connectors list -c easy-agent.yml
uv run easy-agent connectors doctor -c easy-agent.yml
uv run easy-agent connectors test model -c easy-agent.yml
uv run easy-agent connectors test browser -c easy-agent.yml
uv run easy-agent task list
uv run easy-agent task show repo-review
uv run easy-agent task run repo-review -c easy-agent.yml --dry-run
uv run easy-agent skills catalog list
uv run easy-agent skills catalog install python_echo --target skills/installed --force
uv run easy-agent plugins doctor -c easy-agent.yml
uv run easy-agent integration federation-demo -c easy-agent.yml
uv run easy-agent mcp resources list <server> -c easy-agent.yml
uv run easy-agent mcp resources read <server> <uri> -c easy-agent.yml
uv run easy-agent mcp resources templates <server> -c easy-agent.yml
uv run easy-agent mcp resources subscribe <server> <uri> -c easy-agent.yml
uv run easy-agent mcp resources unsubscribe <server> <uri> -c easy-agent.yml
uv run easy-agent mcp prompts list <server> -c easy-agent.yml
uv run easy-agent mcp prompts get <server> <prompt-name> --arguments '{"topic":"notes"}' -c easy-agent.yml
```

## 上手流程

如果只是想先验证 runtime、tools、storage 和 trace surface，不需要任何模型凭据，可以使用 `mock` provider。

- `setup --provider mock` 会创建或复用本地配置，运行静态 preflight checks，完成配置验证，运行一次确定性 smoke test，并输出后续 run inspection 命令。
- `wizard --scenario <name> --target-dir <dir> --provider mock` 会创建 starter、运行静态检查、可选执行 mock smoke path，并输出后续 `config doctor`、`connectors doctor`、`task`、trace 和 dashboard 命令。对于 `browser-agent`，wizard 默认跳过 run smoke，并先引导用户检查 browser connector。
- `init --provider mock` 会写出使用 `protocol: mock` 的 starter config。
- `quickstart --provider mock` 会创建一个临时本地配置，运行一次确定性的工具调用 agent，并输出新 run id 对应的 `runs show`、`runs explain` 与 `traces export` 后续命令。
- `new <scenario> [target-dir]` 是最短的项目创建路径。它包装 `template create`，默认把目标目录设为 scenario 名称，同时保留旧的 template 命令。
- `template list` 展示可用 starter 项目形态。
- `template create basic-agent <target-dir>` 创建最小单 agent 项目。
- `template create human-approval-agent <target-dir>` 创建同样的本地 starter，并把 `python_echo` 标为敏感工具。
- `template create longrun-harness <target-dir>` 创建最小 initializer / worker / evaluator harness。
- `template create mcp-filesystem-agent <target-dir>` 创建 filesystem-MCP starter。
- `template create eval-smoke <target-dir>` 创建 public-eval smoke starter。
- `template create federation-loopback <target-dir>` 创建本地 federation export starter。
- `template create workbench-coding-agent <target-dir>` 创建 process-workbench starter。
- `template create coding-agent <target-dir>` 创建面向业务编码任务的 starter，并带 process workbench 配置。
- `template create research-agent <target-dir>` 创建面向资料调研任务的 starter，把 `official_source_search` 和 mock-first smoke 工具一起挂载。
- `template create data-agent <target-dir>` 创建面向 CSV、JSON、日志、指标摘要与证据化建议的数据分析 starter。
- `template create ops-agent <target-dir>` 创建面向 diagnostics、runbooks、incident notes 与 release checks 的运维 starter。
- `template create browser-agent <target-dir>` 创建 mock-first starter，并写入 `browser.enabled: true` 与 `provider: playwright_mcp`。配置启动时，runtime 会把 Playwright MCP 挂成 stdio MCP server，把工件写到 `.easy-agent/browser`，并默认要求敏感浏览器工具先走审批。
- `template create customer-support-agent <target-dir>` 创建面向 support triage 与回复草拟的 starter。
- `template create sales-agent <target-dir>` 创建面向 sales qualification 与 follow-up 的 starter。
- `template create document-agent <target-dir>` 创建面向文档摘要、抽取和 docs refresh 的 starter。
- `template create qa-agent <target-dir>` 创建面向 QA planning 与 acceptance checks 的 starter。
- `template create release-agent <target-dir>` 创建面向 release readiness 与 evidence review 的 starter。
- `config explain` 会汇总 model/provider、entrypoint type、agents、tools、teams、harnesses、MCP、storage、executors、federation、eval settings 与 required environment variables，但不会打印 secret 值。
- `config doctor` 做静态风险检查，不启动 model client，也不启动 MCP server。它会报告 Python baseline drift、缺失的 live env、缺失的本地工具、MCP roots/auth 缺口、federation auth 缺口、workbench executor readiness、human-loop 覆盖、storage 可移植性与 eval 凭据状态。
- 生成的模板会带本地 README、最小 `.env.local.example` 与 mock-first smoke 命令路径。模板 smoke 从 `config doctor` 开始，再运行一个短任务，并为新 run id 导出 HTML trace。

只有在环境变量里已经有 `DEEPSEEK_API_KEY` 时，才使用 `--provider deepseek`。

## Run 与 Trace 检查

耐用 run 检查现在分成两层：

- `runs list` 展示最近 run id、status、kind、session id 与创建时间。
- `runs show <run_id>` 返回 run summary，包括 event、node、checkpoint、approval 与 child-run 数量。
- `runs explain <run_id>` 会归类常见失败原因，包括 provider 凭据缺失、schema validation failure、guardrail block、MCP failure、iteration loop，以及 Windows cleanup warning。
- `runs fix <run_id>` 会生成 advice-only 修复包。它复用已存储的 run explanation，自动选择 `bug-fix` 或 `release-check` 等内置 task pack，列出安全下一步命令，并可以输出 JSON 或 Markdown；该命令不会修改文件，也不会重新运行 agent。
- `traces export <run_id>` 默认返回结构化 trace tree。
- `traces export <run_id> --raw` 返回历史 raw trace payload。
- `traces export <run_id> --html --output trace.html` 会为 structured tree 写出单文件 HTML trace viewer，包含 summary cards、status/error highlighting、span-kind filters、文本搜索与 raw JSON payload。
- `traces export <run_id> --otel-json --output trace-otel.json` 会写出 experimental OpenTelemetry-style JSON mapping。在 GenAI semantic conventions 稳定前，仍以 native trace tree 作为事实来源。
- `traces open <run_id>` 会写出同一个单文件 HTML viewer，并尝试用默认浏览器打开。无头终端、CI 或测试场景使用 `--no-browser`。

## 最新报告

`report latest` 是只读的本地证据状态面板：

- benchmark report 是否存在、成功数量与分数
- public-eval profile、已完成记录数与 BFCL headline score
- real-network pass/fail/skip 数量与生成时间
- 当前配置 storage 中最近 run 的状态计数

如果某个 report 文件不存在，命令会把该项标为 `missing`，但仍继续返回其他面板信息。比较临时或归档 artifact 时，可以使用 report path override flags。

当终端表格太密时，可以使用 `report latest --html --output report.html`。导出的文件是独立 HTML，包含同一组 benchmark、public-eval、real-network、recent-run 与 raw JSON 证据。

如果想要更完整的本地静态面板，可以使用 `dashboard -c easy-agent.yml --output dashboard.html`。它会把 latest reports、report trend、connector readiness、recent runs、pending approvals 与 raw JSON 放到一个只读 HTML 文件里。

`report trend` 会比较某个目录下的本地 report artifacts，并展示 benchmark、public-eval、real-network 的 latest score、previous score 与 score delta。使用 `--html --output trend.html` 可以生成单文件趋势页。

Trace-tree span 从现有 runtime event envelope 派生，包含稳定的 `span_id`、`parent_span_id`、`kind`、`status`、duration、input/output hash、retry count、checkpoint id 与 child spans。这样当前 JSON trace 仍然轻量，同时为后续 OpenTelemetry export 留出路径。

## Connectors 与 Task Packs

- `connectors list` 展示 model、storage、search、MCP、workbench、federation、browser readiness 等 connector surface。
- `connectors doctor` 做静态 connector 检查，不启动高风险外部流程。
- `connectors test <name>` 聚焦检查列表里的一个 connector。
- 当 `browser.enabled` 为 true 且 `provider: playwright_mcp` 时，browser diagnostics 会检查 `npx` 是否可用，并说明 live browser automation 是否需要审批。Playwright MCP 通过常规 MCP startup 挂载，因此 `mcp list` 仍然是检查 catalog 的入口。
- `task list` 展示内置 task packs。
- `task show <pack>` 输出 prompt template、recommended scenario 与 acceptance criteria。
- `task run <pack>` 会把任务渲染后交给当前配置的 entrypoint。使用 `--dry-run` 可先检查 prompt。

当前内置 task packs 包括 `repo-review`、`bug-fix`、`docs-refresh`、`release-check`、`data-summary` 与 `federation-loopback-demo`。

## Python Facade

在 Python 代码里嵌入 runtime、但不需要完整 CLI surface 时，可以使用轻量 facade：

```python
from agent_runtime import AgentApp

app = AgentApp.from_config("easy-agent.yml")
try:
    result = app.run("Summarize this task")
    task_result = app.run_task("repo-review", context="Focus on tests")
    report = app.report()
    trace = app.trace(str(result["run_id"]))
finally:
    app.close()
```

facade 仍然委托给 CLI 同款 `EasyAgentRuntime`，因此 storage、session memory、guardrails、MCP、federation 与 workbench 行为都继续由配置文件决定。

## 本地凭据

真实凭据只放环境变量，不写入 tracked files。

常见本地变量：

- `DEEPSEEK_API_KEY`
- `SERPAPI_API_KEY`
- `PG_PASSWORD`
- `REDIS_URL`

host-gated real-network 覆盖可能还需要：

- `EASY_AGENT_PODMAN_EXE`
- `EASY_AGENT_CONTAINER_IMAGE`
- `EASY_AGENT_QEMU_EXE`
- `EASY_AGENT_QEMU_BASE_IMAGE`
- `EASY_AGENT_QEMU_SSH_KEY`
- `EASY_AGENT_QEMU_SSH_USER`

## 更实用的官方源搜索

仓库内置的 `skills/examples/official_source_search` 不是只给 benchmark 用的辅助件，而是一个可直接挂到 agent 上的官方源优先搜索工具。

- 挂载 `skills/examples` 后会自动暴露 `official_source_search`
- 这个工具可以通过以下参数优先排序官方或主源域名：
  - `mode: preferred_first | preferred_only | general`
  - `preferred_domains`
- 也支持可选的页面内容抓取：
  - `fetch_contents`
  - `content_mode: truncate | markdown | raw`

典型配置注意点：

- `SERPAPI_API_KEY` 只放环境变量。
- 把这个工具名放到允许浏览的 agent 上。
- `preferred_domains` 是排序策略提示，不是隐藏白名单。

## Harness 工件

Harness 运行会把工件持久化到配置的 artifact 目录与 durable session storage：

- `bootstrap.md`
- `progress.md`
- `features.json`
- checkpoints
- session 与 workbench state

## Public Eval Profiles

README 里的公开分数继续以 `full_v4` 为基线。`official_full_v4` 已经可以直接读取 raw official 风格的 JSON / JSONL manifest，但 README headline score 仍然保持 repo-pinned 基线。

现在还额外提供了几组用于本地补强的 profile：

- `browsecomp_subset`
- `simpleqa_subset`
- `simple_evals_subset`

`evaluation.public_eval.official_dataset` 下常用字段：

- `category_allowlist`
- `suite_allowlist`
- `case_allowlist`
- `selection_mode`
- `max_cases`
- `max_cases_per_suite`
- `resume`
- `checkpoint_path`

选择说明：

- `selection_mode: manifest_order` 会先保留 manifest 顺序，再应用 `max_cases`。
- `selection_mode: balanced_per_suite` 会先按归一化 suite 交错取样，再应用 `max_cases`。
- `category_allowlist` 面向归一化后的公开类别，例如 `agentic`、`multihop`、`memory`、`web_search`。
- `max_cases_per_suite` 会在最终 `max_cases` 生效前，先限制每个归一化 suite 的样本数量。

`evaluation.public_eval.simple_evals` 下常用字段：

- `browsecomp_path`
- `browsecomp_source_url`
- `browsecomp_case_allowlist`
- `browsecomp_max_cases`
- `simpleqa_path`
- `simpleqa_source_url`
- `simpleqa_case_allowlist`
- `simpleqa_max_cases`

grader 说明：

- `evaluation.public_eval.grader.enabled` 会显式打开严格 grading 路径。
- 一旦开启 grader，就必须提供对应的凭据环境变量；运行时不再静默降级。
- 这类 benchmark 题目仍然不会直接 vendored 到仓库里。请把配置指向你自己的 JSON / JSONL 导出，或者一个明确的数据集导出 URL，而不是 evaluator 源码文件。

## Provider Compatibility Live Matrix

可以用 `evaluation.public_eval.provider_compatibility` 在不改写主 public-eval profile 的前提下跑 live provider 检查：

- `enabled` 控制是否开启该矩阵。
- `targets[*].name` 为每个 live target 提供稳定的报告键。
- `targets[*].protocol` 选择适配面：`openai`、`anthropic` 或 `gemini`。
- `targets[*].openai_api_styles` 允许 OpenAI-compatible target 选择 `chat_completions`、`responses` 或两者都测。
- `targets[*].optional` 会把缺失凭据显示成 `skipped`，而不是静默消失。

当前的解释规则：

- strict schema request、`tool_choice: none`、required-tool mode、forced-tool mode 这类必需检查继续作为硬约束
- 对于非 OpenAI 官方的 OpenAI-compatible provider，`single_tool_call_control` 会显式标成 `best_effort`，因为有些 provider 虽然暴露了字段，但运行时并不总是严格执行
- 这个矩阵的目标是把哪些能力来自归一化、哪些是显式约束、哪些只是 best-effort 观察结果写清楚

## Web Search 评测说明

- repo-pinned BFCL web-search 继续以 SerpApi `/search.json` 作为显式搜索链路。
- `web.contents` 现在走更严格的 grounded 路径：
  - 只从 grounded search results 里解析 result id 或 URL
  - 优先命中 grounded cached contents，再决定是否发起网络抓取
  - 如果首个 grounded URL 抓取失败，会先在同一 grounded title 的替代 URL 上重试
  - 只有 grounded fetch 全部失败后，才退回 replay-backed contents
- 每个 case 的诊断信息现在会跟踪：
  - grounded source 数量
  - cache / network / replay 的 contents 来源占比
  - grounded retry 次数
  - search 与 contents backend 的实际组合
- BFCL web-search 的工具 schema 可以通过 `x-easy-agent-normalizer: web_search_query` 选择查询归一化，这样在参数比对前就能去掉 "search the web for ..." 之类的包装语，同时不放松评分标准。
- 这样既能保持 repo-pinned BFCL web-search 子集全绿，也能明确暴露某次本地刷新到底是 live search 还是 replay 驱动。

## MCP Catalog 说明

- `mcp resources templates <server>` 现在会持久化 `resource_templates` 快照。
- `mcp prompts get <server> <prompt-name>` 现在会按 prompt name + arguments 持久化 prompt detail cache。
- `notifications/resources/list_changed` 会同时刷新 resource entries 和 resource templates。
- `notifications/prompts/list_changed` 会刷新 prompt summaries，并把已有的 prompt detail cache 标记成 stale，直到下一次重新获取。

## Executor Capability Reports

`doctor` 和 `workbench list` 可以复用每个 executor backend 的 `capability_report`：

- process：workbench-root 作用域，网络与进程行为仍跟随 host process。
- container：bind-mounted workbench root、显式 command environment、force-remove shutdown，以及启用时的 checkpoint image restore。
- microVM：guest workdir sync boundary、SSH command channel，以及启用时的 runtime-state 或 guest-sync restore。

这些报告是操作层面的安全断言，不是形式化 sandbox 证明。生产部署仍然需要继续加固 host runtime、镜像、网络策略和 secret 注入路径。

## 操作说明

- 使用 `uv run easy-agent ...` 或针对 `agent_cli.app:app` 的 Python `CliRunner`；不要使用 `python -m agent_cli`。
- 这台 Windows 机器上的稳定 pytest 执行需要唯一的 `%TEMP%` 根 `--basetemp`。
- README 内容发生变化时，必须同轮同步 `README.md` 与 `README.zh-CN.md`。
- 仓库对外文档里不能出现本地用户名、用户目录、工作区绝对路径或真实 secret。
