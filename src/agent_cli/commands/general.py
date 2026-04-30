from __future__ import annotations

import asyncio
import json
import platform
import sys
import webbrowser
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
from agent_runtime.dashboard import dashboard_html, dashboard_payload
from agent_runtime.diagnostics import build_fix_package, explain_run, fix_package_markdown
from agent_runtime.reports import (
    build_report_trend,
    latest_report_html,
    latest_report_payload,
    report_trend_html,
)
from agent_runtime.trace_export import trace_tree_to_otel_json

console = Console()
runs_app = typer.Typer(help='Inspect durable run records.')
traces_app = typer.Typer(help='Export structured run traces.')
report_app = typer.Typer(help='Summarize local verification and run reports.')



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


@runs_app.command('fix')
def fix_run_command(
    run_id: str = typer.Argument(..., help='Existing run id.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    task_pack: str = typer.Option('auto', '--task-pack', help='Task pack: auto, bug-fix, docs-refresh, release-check, or repo-review.'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty, json, or markdown.'),
    output: str | None = typer.Option(None, '-o', '--output', help='Optional output file for json or markdown formats.'),
) -> None:
    runtime = build_runtime(config)
    try:
        payload = build_fix_package(runtime.store, run_id, task_pack=task_pack)
    finally:
        asyncio.run(runtime.aclose())
    if output_format == 'json':
        content = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        if output:
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding='utf-8')
            console.print_json(json.dumps({'run_id': run_id, 'output': str(output_path)}, ensure_ascii=False))
        else:
            console.print_json(content)
        return
    if output_format == 'markdown':
        content = fix_package_markdown(payload)
        if output:
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding='utf-8')
            console.print_json(json.dumps({'run_id': run_id, 'output': str(output_path)}, ensure_ascii=False))
        else:
            console.print(content)
        return
    if output_format != 'pretty':
        raise typer.BadParameter('format must be pretty, json, or markdown')
    explanation = payload['explanation']
    table = Table(title=f'run fix advice: {run_id}')
    table.add_column('Field', style='cyan')
    table.add_column('Value', style='green')
    table.add_row('Mode', str(payload['mode']))
    table.add_row('Layer', str(explanation['likely_layer']))
    table.add_row('Status', str(explanation['status']))
    table.add_row('Task Pack', str(payload['selected_task_pack']))
    table.add_row('Probable Cause', str(payload['probable_cause']))
    table.add_row('Recommended Commands', '\n'.join(str(item) for item in payload['recommended_commands']))
    console.print(table)


@traces_app.command('export')
def export_trace(
    run_id: str = typer.Argument(..., help='Existing run id.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    tree: bool = typer.Option(True, '--tree/--raw', help='Export structured trace tree by default, or raw trace with --raw.'),
    html: bool = typer.Option(False, '--html', help='Export the structured trace tree as a standalone HTML file.'),
    otel_json: bool = typer.Option(False, '--otel-json', help='Export an experimental OpenTelemetry-style JSON mapping.'),
    output: str | None = typer.Option(None, '-o', '--output', help='Output file for --html exports.'),
) -> None:
    if html and not tree:
        raise typer.BadParameter('--html requires the structured tree export; remove --raw.')
    if otel_json and not tree:
        raise typer.BadParameter('--otel-json requires the structured tree export; remove --raw.')
    if html and otel_json:
        raise typer.BadParameter('Use only one export format: --html or --otel-json.')
    if (html or otel_json) and output is None:
        raise typer.BadParameter('--html and --otel-json require --output <path>.')
    runtime = build_runtime(config)
    try:
        payload = runtime.store.load_trace_tree(run_id) if tree else runtime.store.load_trace(run_id)
        if html:
            output_path = Path(str(output))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(_trace_tree_html(payload), encoding='utf-8')
            console.print_json(json.dumps({'run_id': run_id, 'output': str(output_path)}, ensure_ascii=False))
            return
        if otel_json:
            output_path = Path(str(output))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            mapped = trace_tree_to_otel_json(payload)
            output_path.write_text(json.dumps(mapped, ensure_ascii=False, indent=2), encoding='utf-8')
            console.print_json(json.dumps({'run_id': run_id, 'output': str(output_path), 'experimental': True}, ensure_ascii=False))
            return
        console.print_json(json.dumps(payload, ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())


@traces_app.command('open')
def open_trace(
    run_id: str = typer.Argument(..., help='Existing run id.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    output: str | None = typer.Option(None, '-o', '--output', help='HTML output path.'),
    no_browser: bool = typer.Option(False, '--no-browser', help='Write the HTML file without launching a browser.'),
) -> None:
    runtime = build_runtime(config)
    try:
        payload = runtime.store.load_trace_tree(run_id)
        output_path = Path(output) if output else Path(f'trace-{_html_token(run_id)}.html')
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(_trace_tree_html(payload), encoding='utf-8')
        opened = False
        if not no_browser:
            opened = webbrowser.open(output_path.resolve().as_uri())
        console.print_json(json.dumps({'run_id': run_id, 'output': str(output_path), 'opened': opened}, ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())


@report_app.command('latest')
def latest_report(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
    benchmark_report: str = typer.Option('.easy-agent/benchmark-report.json', '--benchmark-report'),
    public_eval_report: str = typer.Option('.easy-agent/public-eval-report.json', '--public-eval-report'),
    real_network_report: str = typer.Option('.easy-agent/real-network-report.json', '--real-network-report'),
    run_limit: int = typer.Option(50, '--run-limit', min=1, max=500),
    html: bool = typer.Option(False, '--html', help='Write the latest report as a standalone HTML file.'),
    output: str | None = typer.Option(None, '-o', '--output', help='Output file for --html exports.'),
) -> None:
    payload = latest_report_payload(
        Path(config),
        benchmark_report=Path(benchmark_report),
        public_eval_report=Path(public_eval_report),
        real_network_report=Path(real_network_report),
        run_limit=run_limit,
    )
    if html:
        if output is None:
            raise typer.BadParameter('--html requires --output <path>.')
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(latest_report_html(payload), encoding='utf-8')
        console.print_json(json.dumps({'output': str(output_path), 'reports': payload['reports'], 'runs': payload['runs']}, ensure_ascii=False))
        return
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    if output_format != 'pretty':
        raise typer.BadParameter('format must be pretty or json')
    table = Table(title='easy-agent latest report')
    table.add_column('Surface', style='cyan')
    table.add_column('Status', style='green')
    table.add_column('Score', style='yellow')
    table.add_column('Summary')
    for name, item in payload['reports'].items():
        table.add_row(
            name,
            str(item['status']),
            str(item.get('score') if item.get('score') is not None else '-'),
            str(item.get('summary') or '-'),
        )
    runs = payload['runs']
    table.add_row('runs', str(runs['status']), '-', json.dumps(runs.get('summary', {}), ensure_ascii=False))
    console.print(table)


@report_app.command('trend')
def trend_report(
    history: str = typer.Option('.easy-agent', '--history', help='Directory containing local report JSON artifacts.'),
    limit: int = typer.Option(10, '--limit', min=1, max=100),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
    html: bool = typer.Option(False, '--html', help='Write the trend report as a standalone HTML file.'),
    output: str | None = typer.Option(None, '-o', '--output', help='Output file for --html exports.'),
) -> None:
    payload = build_report_trend(Path(history), limit=limit)
    if html:
        if output is None:
            raise typer.BadParameter('--html requires --output <path>.')
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_trend_html(payload), encoding='utf-8')
        console.print_json(json.dumps({'output': str(output_path), 'trend': payload}, ensure_ascii=False))
        return
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    if output_format != 'pretty':
        raise typer.BadParameter('format must be pretty or json')
    table = Table(title='easy-agent report trend')
    table.add_column('Surface', style='cyan')
    table.add_column('Latest Score', style='green')
    table.add_column('Delta', style='yellow')
    table.add_column('Summary')
    surfaces = payload.get('surfaces', {})
    if isinstance(surfaces, dict):
        for name, raw_item in surfaces.items():
            item = raw_item if isinstance(raw_item, dict) else {}
            raw_latest = item.get('latest')
            latest = raw_latest if isinstance(raw_latest, dict) else {}
            latest_score = latest.get('score')
            table.add_row(
                str(name),
                str(latest_score if latest_score is not None else '-'),
                str(item.get('score_delta') if item.get('score_delta') is not None else '-'),
                str(latest.get('summary') or latest.get('status') or '-'),
            )
    console.print(table)


def register(app: typer.Typer) -> None:
    @app.command('dashboard')
    def dashboard(
        config: str = typer.Option('easy-agent.yml', '-c', '--config'),
        history: str = typer.Option('.easy-agent', '--history', help='Directory containing local report JSON artifacts.'),
        run_limit: int = typer.Option(30, '--run-limit', min=1, max=500),
        output: str = typer.Option('dashboard.html', '-o', '--output', help='Standalone HTML output path.'),
        open_browser: bool = typer.Option(False, '--open', help='Open the generated dashboard in the default browser.'),
        output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
    ) -> None:
        payload = dashboard_payload(Path(config), history=Path(history), run_limit=run_limit)
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(dashboard_html(payload), encoding='utf-8')
        opened = False
        if open_browser:
            opened = webbrowser.open(output_path.resolve().as_uri())
        response = {
            'output': str(output_path),
            'opened': opened,
            'connector_summary': payload['connectors']['summary'],
            'run_count': len(payload['runs']),
            'pending_approvals': len(payload['approvals']['pending']),
        }
        if output_format == 'json':
            console.print_json(json.dumps(response, ensure_ascii=False))
            return
        if output_format != 'pretty':
            raise typer.BadParameter('format must be pretty or json')
        table = Table(title='easy-agent dashboard')
        table.add_column('Field', style='cyan')
        table.add_column('Value', style='green')
        for key, value in response.items():
            table.add_row(key, json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value))
        console.print(table)

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
    summary = _trace_summary(payload, tree)
    filter_buttons = ''.join(
        f'<button type="button" data-filter="{escape(item)}">{escape(item)}</button>'
        for item in ['all', 'error', 'model', 'tool', 'mcp', 'guardrail']
    )
    summary_cards = ''.join(
        f'<div class="summary-card"><strong>{escape(key)}</strong><span>{escape(str(value))}</span></div>'
        for key, value in summary.items()
    )
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
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 18px; }}
    .summary-card {{ border: 1px solid #334155; border-radius: 8px; padding: 12px; background: #111827; }}
    .summary-card strong {{ display: block; color: #93c5fd; font-size: 12px; text-transform: uppercase; }}
    .summary-card span {{ display: block; margin-top: 6px; font-size: 18px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 18px; }}
    .toolbar input {{ min-width: 240px; flex: 1; border: 1px solid #475569; border-radius: 8px; padding: 9px 10px; background: #020617; color: #e2e8f0; }}
    .toolbar button {{ border: 1px solid #475569; border-radius: 8px; padding: 8px 10px; background: #1e293b; color: #e2e8f0; cursor: pointer; }}
    .toolbar button.active {{ border-color: #38bdf8; color: #bae6fd; }}
    .span {{ margin: 10px 0; padding: 12px 14px; border: 1px solid #334155; border-radius: 8px; background: #111827; }}
    .span.status-failed, .span.status-error {{ border-color: #f87171; box-shadow: inset 4px 0 0 #ef4444; }}
    .span.status-succeeded {{ box-shadow: inset 4px 0 0 #22c55e; }}
    .span h2 {{ margin: 0 0 8px; font-size: 16px; }}
    .span-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px; font-size: 13px; }}
    .badge {{ display: inline-block; margin-right: 6px; border: 1px solid #475569; border-radius: 999px; padding: 2px 8px; font-size: 12px; color: #cbd5e1; }}
    details {{ margin-top: 10px; }}
    pre {{ overflow: auto; padding: 12px; border-radius: 8px; background: #020617; color: #cbd5e1; }}
    .children {{ margin-left: 22px; border-left: 1px solid #334155; padding-left: 14px; }}
    .hidden {{ display: none; }}
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
    <section class="summary">{summary_cards}</section>
    <section class="toolbar">
      <input id="trace-search" type="search" placeholder="Search span JSON, names, and event kinds">
      {filter_buttons}
    </section>
    <section id="trace-tree">{spans or '<p>No spans recorded.</p>'}</section>
    <details>
      <summary>Raw trace JSON</summary>
      <pre>{raw_json}</pre>
    </details>
  </main>
  <script>
    const searchInput = document.getElementById('trace-search');
    const buttons = Array.from(document.querySelectorAll('[data-filter]'));
    const spansList = Array.from(document.querySelectorAll('.span'));
    let activeFilter = 'all';
    function matchesFilter(span) {{
      if (activeFilter === 'all') return true;
      if (activeFilter === 'error') return span.dataset.status.includes('fail') || span.dataset.status.includes('error');
      return span.dataset.kind.includes(activeFilter) || span.dataset.search.includes(activeFilter);
    }}
    function applyFilters() {{
      const query = (searchInput.value || '').toLowerCase();
      for (const span of spansList) {{
        const textMatch = !query || span.dataset.search.includes(query);
        span.classList.toggle('hidden', !(textMatch && matchesFilter(span)));
      }}
    }}
    for (const button of buttons) {{
      button.addEventListener('click', () => {{
        activeFilter = button.dataset.filter || 'all';
        buttons.forEach((item) => item.classList.toggle('active', item === button));
        applyFilters();
      }});
    }}
    if (buttons[0]) buttons[0].classList.add('active');
    searchInput.addEventListener('input', applyFilters);
  </script>
</body>
</html>
"""


def _span_html(span: dict[str, Any], depth: int) -> str:
    raw_children = span.get('children')
    children: list[Any] = raw_children if isinstance(raw_children, list) else []
    events = dict(span.get('attributes') or {}).get('events', [])
    event_count = len(events) if isinstance(events, list) else 0
    kind = str(span.get('kind') or '-')
    status = str(span.get('status') or '-')
    span_id = str(span.get('span_id') or '')
    status_token = _html_token(status)
    kind_token = _html_token(kind)
    search_payload = json.dumps({key: value for key, value in span.items() if key != 'children'}, ensure_ascii=False, default=str).lower()
    child_html = '\n'.join(_span_html(child, depth + 1) for child in children if isinstance(child, dict))
    return f"""<article class="span status-{status_token} kind-{kind_token}" data-kind="{escape(kind.lower(), quote=True)}" data-status="{escape(status.lower(), quote=True)}" data-search="{escape(search_payload, quote=True)}" style="margin-left:{depth * 4}px">
  <h2>{escape(str(span.get('name') or span.get('span_id') or 'span'))}</h2>
  <div><span class="badge">{escape(kind)}</span><span class="badge">{escape(status)}</span></div>
  <div class="span-grid">
    <div>span_id: {escape(span_id or '-')}</div>
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


def _trace_summary(payload: dict[str, Any], tree: list[Any]) -> dict[str, Any]:
    raw_spans = payload.get('spans')
    spans = raw_spans if isinstance(raw_spans, list) else _flatten_spans(tree)
    raw_events = payload.get('events')
    events = raw_events if isinstance(raw_events, list) else []
    retry_count = sum(int(span.get('retry_count') or 0) for span in spans if isinstance(span, dict))
    error_count = sum(1 for span in spans if isinstance(span, dict) and _is_error_span(span))
    tool_count = sum(1 for span in spans if isinstance(span, dict) and _is_kind(span, 'tool'))
    mcp_count = sum(1 for span in spans if isinstance(span, dict) and _is_kind(span, 'mcp'))
    guardrail_count = sum(1 for span in spans if isinstance(span, dict) and _is_kind(span, 'guardrail'))
    return {
        'spans': len(spans),
        'events': len(events),
        'errors': error_count,
        'retries': retry_count,
        'tools': tool_count,
        'mcp': mcp_count,
        'guardrails': guardrail_count,
        'checkpoints': dict(payload.get('run') or {}).get('checkpoint_count', 0),
    }


def _flatten_spans(items: list[Any]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        spans.append(item)
        raw_children = item.get('children')
        children = raw_children if isinstance(raw_children, list) else []
        spans.extend(_flatten_spans(children))
    return spans


def _is_error_span(span: dict[str, Any]) -> bool:
    status = str(span.get('status') or '').lower()
    return 'fail' in status or 'error' in status


def _is_kind(span: dict[str, Any], token: str) -> bool:
    haystack = f"{span.get('kind') or ''} {span.get('span_id') or ''} {span.get('name') or ''}".lower()
    return token in haystack


def _html_token(value: str) -> str:
    token = ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '-' for ch in value.lower())
    return token or 'unknown'

