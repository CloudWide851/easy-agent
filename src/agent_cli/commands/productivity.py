from __future__ import annotations

import asyncio
import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from agent_cli.shared import with_runtime
from agent_common.models import HumanLoopMode
from agent_runtime import EasyAgentRuntime
from agent_runtime.connectors import (
    browser_artifacts,
    browser_doctor,
    connector_checks,
    connector_payloads,
    connector_summary,
    test_connector,
)
from agent_runtime.tasks import (
    get_task_pack,
    list_task_packs,
    render_task_prompt,
    task_pack_payload,
)

console = Console()
connectors_app = typer.Typer(help='Inspect configured external connectors.')
task_app = typer.Typer(help='Run built-in task packs.')
browser_app = typer.Typer(help='Inspect MCP-first browser workflow readiness.')


@connectors_app.command('list')
def list_connectors(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    payload = connector_payloads(config)
    if output_format == 'json':
        console.print_json(json.dumps({'connectors': payload}, ensure_ascii=False))
        return
    table = Table(title='easy-agent connectors')
    table.add_column('Name', style='cyan')
    table.add_column('Kind', style='green')
    table.add_column('Status', style='yellow')
    for item in payload:
        table.add_row(str(item['name']), str(item['kind']), str(item['status']))
    console.print(table)


@connectors_app.command('doctor')
def doctor_connectors(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    checks = connector_checks(config)
    payload = {'summary': connector_summary(checks), 'connectors': [check.__dict__ for check in checks]}
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title='easy-agent connector doctor')
    table.add_column('Status', style='cyan')
    table.add_column('Name', style='green')
    table.add_column('Message')
    table.add_column('Action')
    for check in checks:
        table.add_row(check.status, check.name, check.message, check.action)
    console.print(table)


@connectors_app.command('test')
def test_connector_command(
    name: str = typer.Argument(..., help='Connector name from connectors list.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    payload = test_connector(config, name)
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title=f'connector test: {name}')
    table.add_column('Status', style='cyan')
    table.add_column('Check', style='green')
    table.add_column('Action')
    for check in payload['checks']:
        table.add_row(str(check['status']), str(check['message']), str(check['action']))
    console.print(table)
    if payload['status'] == 'error':
        raise typer.Exit(1)


@task_app.command('list')
def list_tasks(output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.')) -> None:
    packs = [task_pack_payload(pack) for pack in list_task_packs()]
    if output_format == 'json':
        console.print_json(json.dumps({'tasks': packs}, ensure_ascii=False))
        return
    table = Table(title='easy-agent task packs')
    table.add_column('Name', style='cyan')
    table.add_column('Recommended Scenario', style='green')
    table.add_column('Description')
    for pack in packs:
        table.add_row(str(pack['name']), str(pack['recommended_scenario']), str(pack['description']))
    console.print(table)


@task_app.command('show')
def show_task(
    pack: str = typer.Argument(..., help='Task pack name.'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    payload = task_pack_payload(get_task_pack(pack))
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print_json(json.dumps(payload, ensure_ascii=False))


@task_app.command('run')
def run_task(
    pack: str = typer.Argument(..., help='Task pack name.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    context: str | None = typer.Option(None, '--context', help='Additional task context.'),
    session_id: str | None = typer.Option(None, '--session-id'),
    approval_mode: str = typer.Option('hybrid', '--approval-mode'),
    dry_run: bool = typer.Option(False, '--dry-run', help='Render the task prompt without running the agent.'),
) -> None:
    prompt = render_task_prompt(pack, context)
    if dry_run:
        console.print_json(json.dumps({'pack': pack, 'prompt': prompt}, ensure_ascii=False))
        return

    async def _run(runtime: EasyAgentRuntime) -> None:
        result: dict[str, Any] = await runtime.run(
            prompt,
            session_id=session_id,
            approval_mode=HumanLoopMode(approval_mode),
        )
        console.print_json(json.dumps(result, ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@browser_app.command('doctor')
def doctor_browser(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    payload = browser_doctor(config)
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title='easy-agent browser doctor')
    table.add_column('Field', style='cyan')
    table.add_column('Value', style='green')
    for key in [
        'enabled',
        'provider',
        'server_name',
        'headless',
        'isolated',
        'artifacts_dir',
        'require_approval',
        'npx_available',
        'mcp_server_declared',
    ]:
        table.add_row(key, str(payload.get(key)))
    console.print(table)


@browser_app.command('artifacts')
def list_browser_artifacts(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    limit: int = typer.Option(50, '--limit', min=1, max=500),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    payload = browser_artifacts(config, limit=limit)
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title='easy-agent browser artifacts')
    table.add_column('Kind', style='cyan')
    table.add_column('Path', style='green')
    table.add_column('Bytes', style='yellow')
    for item in payload['artifacts']:
        table.add_row(str(item['kind']), str(item['relative_path']), str(item['size_bytes']))
    if not payload['artifacts']:
        table.add_row('-', 'No browser artifacts found.', '-')
    console.print(table)
