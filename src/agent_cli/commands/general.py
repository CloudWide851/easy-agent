from __future__ import annotations

import asyncio
import json
import platform
import sys
from html import escape
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from agent_cli.shared import build_cli_inline_resolver, with_runtime
from agent_common.models import HumanLoopMode, RunContext
from agent_common.version import runtime_version
from agent_protocols import resolve_protocol
from agent_runtime import EasyAgentRuntime, build_runtime
from agent_runtime.diagnostics import explain_run

console = Console()
runs_app = typer.Typer(help='Inspect durable run records.')
traces_app = typer.Typer(help='Export structured run traces.')



def _entrypoint_type(runtime: Any) -> str:
    entrypoint = runtime.config.graph.entrypoint
    if runtime.config.graph.nodes:
        return 'graph'
    if entrypoint in runtime.config.agent_map:
        return 'agent'
    if entrypoint in runtime.config.team_map:
        return 'team'
    return 'unknown'



def _mcp_transport_summary(runtime: Any) -> str:
    if not runtime.config.mcp:
        return 'none'
    return ', '.join(f'{server.name}:{server.transport}' for server in runtime.config.mcp)



def _doctor_rows(runtime: Any) -> list[tuple[str, str]]:
    adapter = resolve_protocol(runtime.config.model)
    sandbox = runtime.sandbox_manager.describe()
    human_loop = runtime.config.security.human_loop
    workbench = runtime.workbench_manager.describe()
    federation = runtime.config.federation
    executor_summary = ', '.join(
        f"{name}:{'yes' if item['available'] else 'no'}"
        for name, item in workbench.get('executors', {}).items()
    ) or 'none'
    return [
        ('Python', sys.version.split()[0]),
        ('Platform', platform.platform()),
        ('Provider', runtime.config.model.provider),
        ('Model', runtime.config.model.model),
        ('Protocol', adapter.protocol.value),
        ('Runtime Version', runtime_version()),
        ('Entrypoint', runtime.config.graph.entrypoint),
        ('Entrypoint Type', _entrypoint_type(runtime)),
        ('Skills', str(len(runtime.skills))),
        ('Teams', str(len(runtime.config.graph.teams))),
        ('Harnesses', str(len(runtime.config.harnesses))),
        ('Configured MCP Servers', str(len(runtime.config.mcp))),
        ('MCP Transports', _mcp_transport_summary(runtime)),
        ('Federation Remotes', str(len(federation.remotes))),
        ('Federation Exports', str(len(federation.exports))),
        ('Federation Server', f"{federation.server.host}:{federation.server.port}{federation.server.base_path}"),
        ('Federation Push', 'polling, webhook_subscribe, sse_events'),
        ('Human Loop Mode', human_loop.mode.value),
        ('Sensitive Tools', ', '.join(human_loop.sensitive_tools) if human_loop.sensitive_tools else 'none'),
        ('Tool Guardrails', ', '.join(runtime.config.guardrails.tool_input_hooks)),
        ('Output Guardrails', ', '.join(runtime.config.guardrails.final_output_hooks)),
        ('Event Stream', str(runtime.config.observability.enable_event_stream)),
        ('Loaded Sources', str(len(runtime.loaded_sources))),
        ('Sandbox Mode', sandbox['mode']),
        ('Sandbox Targets', ', '.join(sandbox['targets'])),
        ('Windows Sandbox', str(sandbox['windows_sandbox_available'])),
        ('Sandbox Fallback', sandbox['windows_sandbox_fallback']),
        ('Workbench Root', workbench['base_root']),
        ('Configured Executors', str(len(workbench.get('executors', {})))),
        ('Executor Availability', executor_summary),
        ('Workbench Executor', workbench['default_executor']),
        ('Workbench Sessions', str(workbench['active_sessions'])),
        ('Storage', str(runtime.store.base_path.resolve())),
    ]



def _render_event(event: dict[str, Any], mode: str) -> None:
    if mode == 'ndjson':
        console.print(json.dumps(event, ensure_ascii=False))
        return
    summary = event.get('payload', {})
    console.print(
        f"[{event['sequence']:03d}] {event['scope']}::{event['kind']} "
        f"run={event['run_id']} node={event.get('node_id') or '-'} payload={json.dumps(summary, ensure_ascii=False)}"
    )



def _approval_mode(value: str) -> HumanLoopMode:
    return HumanLoopMode(value)



def _configure_inline_resolver(runtime: EasyAgentRuntime, approval_mode: HumanLoopMode) -> None:
    if approval_mode is HumanLoopMode.DEFERRED:
        runtime.set_inline_approval_resolver(None)
        return
    runtime.set_inline_approval_resolver(build_cli_inline_resolver(console))


@runs_app.command('list')
def list_runs(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    limit: int = typer.Option(20, '--limit', min=1, max=500),
    status: str | None = typer.Option(None, '--status'),
    run_kind: str | None = typer.Option(None, '--kind'),
) -> None:
    runtime = build_runtime(config)
    try:
        table = Table(title='easy-agent runs')
        table.add_column('Run ID', style='cyan')
        table.add_column('Kind', style='green')
        table.add_column('Status', style='yellow')
        table.add_column('Session', style='magenta')
        table.add_column('Created', style='blue')
        for run in runtime.store.list_runs(limit=limit, status=status, run_kind=run_kind):
            table.add_row(
                str(run['run_id']),
                str(run['run_kind']),
                str(run['status']),
                str(run.get('session_id') or '-'),
                str(run['created_at']),
            )
        console.print(table)
    finally:
        asyncio.run(runtime.aclose())


@runs_app.command('show')
def show_run(
    run_id: str = typer.Argument(..., help='Existing run id.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    runtime = build_runtime(config)
    try:
        console.print_json(json.dumps(runtime.store.load_run_summary(run_id), ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())


@runs_app.command('explain')
def explain_run_command(
    run_id: str = typer.Argument(..., help='Existing run id.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    runtime = build_runtime(config)
    try:
        payload = explain_run(runtime.store, run_id)
        if output_format == 'json':
            console.print_json(json.dumps(payload, ensure_ascii=False))
            return
        table = Table(title=f'run explanation: {run_id}')
        table.add_column('Field', style='cyan')
        table.add_column('Value', style='green')
        table.add_row('Status', str(payload['status']))
        table.add_row('Likely Layer', str(payload['likely_layer']))
        table.add_row('Headline', str(payload['headline']))
        table.add_row('Counts', json.dumps(payload['counts'], ensure_ascii=False))
        table.add_row('Recommended Actions', '\n'.join(str(item) for item in payload['recommended_actions']))
        console.print(table)
        if payload['evidence']:
            console.print_json(json.dumps({'evidence': payload['evidence']}, ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())


@traces_app.command('export')
def export_trace(
    run_id: str = typer.Argument(..., help='Existing run id.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    tree: bool = typer.Option(True, '--tree/--raw', help='Export structured trace tree by default, or raw trace with --raw.'),
    html: bool = typer.Option(False, '--html', help='Export the structured trace tree as a standalone HTML file.'),
    output: str | None = typer.Option(None, '-o', '--output', help='Output file for --html exports.'),
) -> None:
    if html and not tree:
        raise typer.BadParameter('--html requires the structured tree export; remove --raw.')
    if html and output is None:
        raise typer.BadParameter('--html requires --output <path>.')
    runtime = build_runtime(config)
    try:
        payload = runtime.store.load_trace_tree(run_id) if tree else runtime.store.load_trace(run_id)
        if html:
            output_path = Path(str(output))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(_trace_tree_html(payload), encoding='utf-8')
            console.print_json(json.dumps({'run_id': run_id, 'output': str(output_path)}, ensure_ascii=False))
            return
        console.print_json(json.dumps(payload, ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())



def register(app: typer.Typer) -> None:
    @app.command()
    def doctor(
        config: str = typer.Option('easy-agent.yml', '-c', '--config'),
        smoke: bool = False,
    ) -> None:
        async def _run(runtime: EasyAgentRuntime) -> None:
            table = Table(title='easy-agent doctor')
            table.add_column('Check', style='cyan')
            table.add_column('Value', style='green')
            for check, value in _doctor_rows(runtime):
                table.add_row(check, value)
            console.print(table)
            if smoke:
                context = RunContext(run_id='doctor_smoke', workdir=Path.cwd(), node_id=None, shared_state={'input': 'smoke'})
                if runtime.config.graph.entrypoint in runtime.config.agent_map:
                    result = await runtime.orchestrator.run_agent(
                        runtime.config.graph.entrypoint,
                        'Respond with a short confirmation.',
                        context,
                    )
                elif runtime.config.graph.entrypoint in runtime.config.team_map:
                    result = await runtime.orchestrator.run_team(
                        runtime.config.graph.entrypoint,
                        'Respond with a short confirmation and include TERMINATE.',
                        context,
                    )
                else:
                    raise typer.BadParameter('Smoke test requires graph.entrypoint to be an agent or team.')
                console.print(f'[bold green]Smoke response:[/bold green] {result}')

        asyncio.run(with_runtime(config, _run))

    @app.command()
    def run(
        input_text: str = typer.Argument(..., help='Input text for the graph, entry agent, or entry team.'),
        config: str = typer.Option('easy-agent.yml', '-c', '--config'),
        session_id: str | None = typer.Option(None, '--session-id', help='Optional explicit session id for persistent memory.'),
        stream: str | None = typer.Option(None, '--stream', help='Optional stream format: pretty or ndjson.'),
        approval_mode: str = typer.Option('hybrid', '--approval-mode', help='Approval mode: deferred, inline, or hybrid.'),
    ) -> None:
        async def _run(runtime: EasyAgentRuntime) -> None:
            resolved_mode = _approval_mode(approval_mode)
            _configure_inline_resolver(runtime, resolved_mode)
            if stream:
                async for event in runtime.stream(input_text, session_id=session_id, approval_mode=resolved_mode):
                    _render_event(event, stream)
                return
            result = await runtime.run(input_text, session_id=session_id, approval_mode=resolved_mode)
            console.print_json(json.dumps(result, ensure_ascii=False))

        asyncio.run(with_runtime(config, _run))

    @app.command()
    def resume(
        run_id: str = typer.Argument(..., help='Existing run id to resume from a checkpoint.'),
        config: str = typer.Option('easy-agent.yml', '-c', '--config'),
        stream: str | None = typer.Option(None, '--stream', help='Optional stream format: pretty or ndjson.'),
        checkpoint_id: int | None = typer.Option(None, '--checkpoint-id', help='Optional historical checkpoint id.'),
        fork: bool = typer.Option(False, '--fork', help='Resume into a new child run.'),
        approval_mode: str = typer.Option('hybrid', '--approval-mode', help='Approval mode: deferred, inline, or hybrid.'),
    ) -> None:
        async def _run(runtime: EasyAgentRuntime) -> None:
            resolved_mode = _approval_mode(approval_mode)
            _configure_inline_resolver(runtime, resolved_mode)
            if stream:
                async for event in runtime.resume_stream(run_id, checkpoint_id, fork=fork, approval_mode=resolved_mode):
                    _render_event(event, stream)
                return
            result = await runtime.resume(run_id, checkpoint_id, fork=fork, approval_mode=resolved_mode)
            console.print_json(json.dumps(result, ensure_ascii=False))

        asyncio.run(with_runtime(config, _run))

    @app.command()
    def replay(
        run_id: str = typer.Argument(..., help='Existing run id.'),
        checkpoint_id: int = typer.Option(..., '--checkpoint-id', help='Checkpoint id to replay.'),
        config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    ) -> None:
        async def _run(runtime: EasyAgentRuntime) -> None:
            result = await runtime.replay(run_id, checkpoint_id)
            console.print_json(json.dumps(result, ensure_ascii=False))

        asyncio.run(with_runtime(config, _run))

    @app.command('checkpoints')
    def checkpoints(
        run_id: str = typer.Argument(..., help='Existing run id.'),
        config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    ) -> None:
        runtime = build_runtime(config)
        try:
            table = Table(title=f'checkpoints for {run_id}')
            table.add_column('ID', style='cyan')
            table.add_column('Kind', style='green')
            table.add_column('Created', style='yellow')
            for checkpoint in runtime.list_checkpoints(run_id):
                table.add_row(str(checkpoint['checkpoint_id']), checkpoint['kind'], checkpoint['created_at'])
            console.print(table)
        finally:
            asyncio.run(runtime.aclose())

    @app.command('interrupt')
    def interrupt(
        run_id: str = typer.Argument(..., help='Existing run id.'),
        config: str = typer.Option('easy-agent.yml', '-c', '--config'),
        reason: str = typer.Option('user requested interrupt', '--reason'),
    ) -> None:
        runtime = build_runtime(config)
        try:
            runtime.interrupt_run(run_id, {'reason': reason})
            console.print_json(json.dumps({'run_id': run_id, 'status': 'interrupt_requested', 'reason': reason}, ensure_ascii=False))
        finally:
            asyncio.run(runtime.aclose())

    @app.command('trace')
    def trace(run_id: str, config: str = typer.Option('easy-agent.yml', '-c', '--config')) -> None:
        runtime = build_runtime(config)
        try:
            payload = runtime.store.load_trace(run_id)
            console.print_json(json.dumps(payload, ensure_ascii=False))
        finally:
            asyncio.run(runtime.aclose())


def _trace_tree_html(payload: dict[str, Any]) -> str:
    run = dict(payload.get('run') or {})
    title = f"easy-agent trace {run.get('run_id', '')}".strip()
    raw_tree = payload.get('tree')
    tree: list[Any] = raw_tree if isinstance(raw_tree, list) else []
    spans = '\n'.join(_span_html(span, depth=0) for span in tree if isinstance(span, dict))
    raw_json = escape(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: Inter, Segoe UI, sans-serif; }}
    body {{ margin: 0; background: #0f172a; color: #e2e8f0; }}
    header {{ padding: 24px 32px; border-bottom: 1px solid #334155; background: #111827; }}
    main {{ padding: 24px 32px 40px; max-width: 1200px; margin: 0 auto; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
    .pill {{ border: 1px solid #475569; border-radius: 999px; padding: 4px 10px; background: #1e293b; }}
    .span {{ margin: 10px 0; padding: 12px 14px; border: 1px solid #334155; border-radius: 8px; background: #111827; }}
    .span h2 {{ margin: 0 0 8px; font-size: 16px; }}
    .span-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px; font-size: 13px; }}
    details {{ margin-top: 10px; }}
    pre {{ overflow: auto; padding: 12px; border-radius: 8px; background: #020617; color: #cbd5e1; }}
    .children {{ margin-left: 22px; border-left: 1px solid #334155; padding-left: 14px; }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(title)}</h1>
    <div class="meta">
      <span class="pill">status: {escape(str(run.get('status', '-')))}</span>
      <span class="pill">kind: {escape(str(run.get('run_kind', '-')))}</span>
      <span class="pill">events: {escape(str(run.get('event_count', '-')))}</span>
      <span class="pill">checkpoints: {escape(str(run.get('checkpoint_count', '-')))}</span>
    </div>
  </header>
  <main>
    <section>{spans or '<p>No spans recorded.</p>'}</section>
    <details>
      <summary>Raw trace JSON</summary>
      <pre>{raw_json}</pre>
    </details>
  </main>
</body>
</html>
"""


def _span_html(span: dict[str, Any], depth: int) -> str:
    raw_children = span.get('children')
    children: list[Any] = raw_children if isinstance(raw_children, list) else []
    events = dict(span.get('attributes') or {}).get('events', [])
    event_count = len(events) if isinstance(events, list) else 0
    child_html = '\n'.join(_span_html(child, depth + 1) for child in children if isinstance(child, dict))
    return f"""<article class="span" style="margin-left:{depth * 4}px">
  <h2>{escape(str(span.get('name') or span.get('span_id') or 'span'))}</h2>
  <div class="span-grid">
    <div>kind: {escape(str(span.get('kind', '-')))}</div>
    <div>status: {escape(str(span.get('status', '-')))}</div>
    <div>duration: {escape(str(span.get('duration_seconds', '-')))}</div>
    <div>retry_count: {escape(str(span.get('retry_count', 0)))}</div>
    <div>checkpoint_id: {escape(str(span.get('checkpoint_id') or '-'))}</div>
    <div>events: {event_count}</div>
  </div>
  <details>
    <summary>Span JSON</summary>
    <pre>{escape(json.dumps({key: value for key, value in span.items() if key != 'children'}, ensure_ascii=False, indent=2, default=str))}</pre>
  </details>
  <div class="children">{child_html}</div>
</article>"""

