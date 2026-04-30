from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from agent_cli.shared import with_runtime
from agent_common.models import HumanLoopMode
from agent_runtime import EasyAgentRuntime, build_runtime
from agent_runtime.bundles import write_run_bundle
from agent_runtime.connectors import (
    browser_artifacts,
    browser_doctor,
    connector_checks,
    connector_payloads,
    connector_summary,
    test_connector,
)
from agent_runtime.diagnostics import build_triage_package
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
workflow_app = typer.Typer(help='Run guided workflow packs.')


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


@workflow_app.command('list')
def list_workflows(output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.')) -> None:
    packs = [task_pack_payload(pack) for pack in list_task_packs()]
    if output_format == 'json':
        console.print_json(json.dumps({'workflows': packs}, ensure_ascii=False))
        return
    table = Table(title='easy-agent workflow packs')
    table.add_column('Name', style='cyan')
    table.add_column('Recommended Scenario', style='green')
    table.add_column('Description')
    for pack in packs:
        table.add_row(str(pack['name']), str(pack['recommended_scenario']), str(pack['description']))
    console.print(table)


@workflow_app.command('show')
def show_workflow(
    pack: str = typer.Argument(..., help='Workflow pack name.'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    payload = task_pack_payload(get_task_pack(pack))
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print_json(json.dumps(payload, ensure_ascii=False))


@workflow_app.command('doctor')
def doctor_workflow(
    workflow: str = typer.Argument(..., help='Workflow YAML path.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    payload = _workflow_doctor_payload(workflow, config)
    _print_workflow_doctor_payload(payload, output_format)
    if payload['status'] == 'error':
        raise typer.Exit(1)


@workflow_app.command('plan')
def plan_workflow(
    workflow: str = typer.Argument(..., help='Workflow YAML path.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    context: str | None = typer.Option(None, '--context', help='Additional workflow context.'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    doctor = _workflow_doctor_payload(workflow, config)
    if doctor['status'] == 'error':
        _print_workflow_doctor_payload(doctor, output_format)
        raise typer.Exit(1)
    payload = _workflow_plan_payload(workflow, config, context=context, dry_run=True)
    payload['doctor'] = doctor
    _print_workflow_payload(payload, output_format)


@workflow_app.command('init')
def init_workflow(
    pack: str = typer.Argument(..., help='Workflow pack name.'),
    output: str = typer.Option('workflow.yml', '-o', '--output', help='Workflow YAML output path.'),
    context: str | None = typer.Option(None, '--context', help='Default workflow context.'),
    approval_mode: str = typer.Option('hybrid', '--approval-mode'),
    bundle_on_completion: bool = typer.Option(False, '--bundle-on-completion/--no-bundle-on-completion'),
    force: bool = typer.Option(False, '--force', help='Overwrite an existing workflow file.'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    pack_info = task_pack_payload(get_task_pack(pack))
    output_path = Path(output)
    if output_path.exists() and not force:
        raise typer.BadParameter(f'{output_path} already exists; pass --force to overwrite.')
    payload = {
        'version': 1,
        'name': pack,
        'pack': pack,
        'context': context or f'Run the {pack} workflow.',
        'approval_mode': approval_mode,
        'bundle_on_completion': bundle_on_completion,
        'description': pack_info['description'],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding='utf-8')
    response = {'output': str(output_path), 'workflow': payload}
    if output_format == 'json':
        console.print_json(json.dumps(response, ensure_ascii=False))
        return
    if output_format != 'pretty':
        raise typer.BadParameter('format must be pretty or json')
    table = Table(title='workflow init')
    table.add_column('Field', style='cyan')
    table.add_column('Value', style='green')
    table.add_row('Output', str(output_path))
    table.add_row('Pack', pack)
    table.add_row('Bundle On Completion', str(bundle_on_completion))
    console.print(table)


@workflow_app.command('run')
def run_workflow(
    pack: str = typer.Argument(..., help='Workflow pack name or workflow.yml path.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    context: str | None = typer.Option(None, '--context', help='Additional workflow context.'),
    session_id: str | None = typer.Option(None, '--session-id'),
    approval_mode: str = typer.Option('hybrid', '--approval-mode'),
    dry_run: bool = typer.Option(False, '--dry-run', help='Render the workflow plan without running the agent.'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    payload = _workflow_plan_payload(
        pack,
        config,
        context=context,
        approval_mode=approval_mode,
        dry_run=dry_run,
    )
    selected_pack = str(payload['pack'])
    selected_approval_mode = str(payload['approval_mode'])
    prompt = str(payload['prompt'])
    if dry_run:
        _print_workflow_payload(payload, output_format)
        return

    async def _run(runtime: EasyAgentRuntime) -> None:
        result: dict[str, Any] = await runtime.run(
            prompt,
            session_id=session_id,
            approval_mode=HumanLoopMode(selected_approval_mode),
        )
        payload['result'] = result
        run_id = str(result.get('run_id') or '')
        if run_id:
            payload['next_commands'] = _workflow_next_commands(selected_pack, run_id=run_id)
            raw_workflow = payload.get('workflow')
            workflow_payload: dict[str, Any] = raw_workflow if isinstance(raw_workflow, dict) else {}
            if workflow_payload.get('bundle_on_completion'):
                payload['bundle'] = write_run_bundle(
                    runtime.store,
                    run_id,
                    Path(f'run-bundle-{_safe_token(run_id)}'),
                    browser_payload=browser_artifacts(config, limit=50),
                    force=True,
                )
        _print_workflow_payload(payload, output_format)

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


@browser_app.command('smoke')
def smoke_browser(
    url: str = typer.Argument(..., help='Target URL for the browser smoke plan.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    context: str | None = typer.Option(None, '--context', help='Additional browser smoke context.'),
    run: bool = typer.Option(False, '--run', help='Run the generated browser-qa prompt through the configured runtime.'),
    approval_mode: str = typer.Option('hybrid', '--approval-mode'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    _browser_plan_command('smoke', url, config, context=context, run=run, approval_mode=approval_mode, output_format=output_format)


@browser_app.command('snapshot')
def snapshot_browser(
    url: str = typer.Argument(..., help='Target URL for the browser snapshot plan.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    context: str | None = typer.Option(None, '--context', help='Additional browser snapshot context.'),
    run: bool = typer.Option(False, '--run', help='Run the generated browser-qa prompt through the configured runtime.'),
    approval_mode: str = typer.Option('hybrid', '--approval-mode'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    _browser_plan_command('snapshot', url, config, context=context, run=run, approval_mode=approval_mode, output_format=output_format)


@browser_app.command('audit')
def audit_browser(
    url: str = typer.Argument(..., help='Target URL for the browser audit plan.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    context: str | None = typer.Option(None, '--context', help='Additional browser audit context.'),
    run: bool = typer.Option(False, '--run', help='Run the generated browser-audit prompt through the configured runtime.'),
    approval_mode: str = typer.Option('hybrid', '--approval-mode'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    _browser_plan_command('audit', url, config, context=context, run=run, approval_mode=approval_mode, output_format=output_format)


@browser_app.command('seo')
def seo_browser(
    url: str = typer.Argument(..., help='Target URL for the browser SEO audit plan.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    context: str | None = typer.Option(None, '--context', help='Additional browser SEO context.'),
    run: bool = typer.Option(False, '--run', help='Run the generated browser-audit prompt through the configured runtime.'),
    approval_mode: str = typer.Option('hybrid', '--approval-mode'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    _browser_plan_command('seo', url, config, context=context, run=run, approval_mode=approval_mode, output_format=output_format)


@browser_app.command('a11y')
def a11y_browser(
    url: str = typer.Argument(..., help='Target URL for the browser accessibility audit plan.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    context: str | None = typer.Option(None, '--context', help='Additional browser accessibility context.'),
    run: bool = typer.Option(False, '--run', help='Run the generated browser-audit prompt through the configured runtime.'),
    approval_mode: str = typer.Option('hybrid', '--approval-mode'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    _browser_plan_command('a11y', url, config, context=context, run=run, approval_mode=approval_mode, output_format=output_format)


@browser_app.command('links')
def links_browser(
    url: str = typer.Argument(..., help='Target URL for the browser link audit plan.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    context: str | None = typer.Option(None, '--context', help='Additional browser link audit context.'),
    run: bool = typer.Option(False, '--run', help='Run the generated browser-audit prompt through the configured runtime.'),
    approval_mode: str = typer.Option('hybrid', '--approval-mode'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    _browser_plan_command('links', url, config, context=context, run=run, approval_mode=approval_mode, output_format=output_format)


@browser_app.command('report')
def report_browser(
    run_id: str = typer.Argument(..., help='Existing run id.'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    runtime = build_runtime(config)
    try:
        payload = {
            'run_id': run_id,
            'triage': build_triage_package(runtime.store, run_id),
            'browser': {
                'doctor': browser_doctor(config),
                'artifacts': browser_artifacts(config, limit=20),
            },
            'next_commands': [
                f'easy-agent runs triage {run_id} -c easy-agent.yml',
                f'easy-agent traces open {run_id} -c easy-agent.yml --no-browser',
                'easy-agent browser artifacts -c easy-agent.yml',
            ],
        }
    finally:
        asyncio.run(runtime.aclose())
    _print_browser_payload(payload, output_format, title=f'browser report: {run_id}')


def _workflow_plan_payload(
    workflow_input: str,
    config: str,
    *,
    context: str | None = None,
    approval_mode: str = 'hybrid',
    dry_run: bool = True,
) -> dict[str, Any]:
    workflow = _load_workflow_input(workflow_input)
    selected_pack = str(workflow.get('pack') or workflow_input)
    selected_context = context if context is not None else str(workflow.get('context') or '')
    selected_approval_mode = approval_mode if approval_mode != 'hybrid' else str(workflow.get('approval_mode') or approval_mode)
    pack_info = task_pack_payload(get_task_pack(selected_pack))
    prompt = render_task_prompt(selected_pack, selected_context)
    checks = connector_checks(config)
    return {
        'pack': selected_pack,
        'workflow': workflow,
        'description': pack_info['description'],
        'recommended_scenario': pack_info['recommended_scenario'],
        'acceptance_criteria': pack_info['acceptance_criteria'],
        'prompt': prompt,
        'approval_mode': selected_approval_mode,
        'dry_run': dry_run,
        'preflight': [check.__dict__ for check in checks],
        'next_commands': _workflow_next_commands(selected_pack),
    }


def _workflow_doctor_payload(workflow_path: str, config: str) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    workflow: dict[str, Any] = {}
    path = Path(workflow_path)
    if not path.exists() or not path.is_file():
        checks.append({'status': 'error', 'name': 'file', 'message': f'Workflow file not found: {workflow_path}', 'action': 'Create one with easy-agent workflow init <pack> --output workflow.yml.'})
    else:
        try:
            loaded = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        except yaml.YAMLError as exc:
            loaded = {}
            checks.append({'status': 'error', 'name': 'yaml', 'message': f'Workflow YAML is invalid: {exc}', 'action': 'Fix the YAML syntax.'})
        if not isinstance(loaded, dict):
            checks.append({'status': 'error', 'name': 'shape', 'message': 'Workflow file must contain a YAML mapping.', 'action': 'Use key/value YAML generated by workflow init.'})
        else:
            workflow = loaded
            if workflow.get('version') != 1:
                checks.append({'status': 'error', 'name': 'version', 'message': 'Workflow file version must be 1.', 'action': 'Regenerate the file with workflow init or set version: 1.'})
            raw_pack = workflow.get('pack')
            if not raw_pack:
                checks.append({'status': 'error', 'name': 'pack', 'message': 'Workflow file must define pack.', 'action': 'Set pack to a known workflow pack.'})
            else:
                try:
                    get_task_pack(str(raw_pack))
                    checks.append({'status': 'ok', 'name': 'pack', 'message': f'Known workflow pack: {raw_pack}', 'action': 'No action needed.'})
                except ValueError:
                    checks.append({'status': 'error', 'name': 'pack', 'message': f'Unknown workflow pack: {raw_pack}', 'action': 'Run easy-agent workflow list.'})
            approval = str(workflow.get('approval_mode') or 'hybrid')
            valid_approvals = {item.value for item in HumanLoopMode}
            if approval not in valid_approvals:
                checks.append({'status': 'error', 'name': 'approval_mode', 'message': f'Invalid approval_mode: {approval}', 'action': f'Use one of: {", ".join(sorted(valid_approvals))}.'})
            else:
                checks.append({'status': 'ok', 'name': 'approval_mode', 'message': f'approval_mode={approval}', 'action': 'No action needed.'})
            if not str(workflow.get('context') or '').strip():
                checks.append({'status': 'warn', 'name': 'context', 'message': 'Workflow context is empty.', 'action': 'Add concrete goal, target files, URL, or acceptance criteria.'})
            raw_bundle = workflow.get('bundle_on_completion', False)
            if not isinstance(raw_bundle, bool):
                checks.append({'status': 'warn', 'name': 'bundle_on_completion', 'message': 'bundle_on_completion should be true or false.', 'action': 'Set bundle_on_completion: false unless evidence export is required.'})
            else:
                checks.append({'status': 'ok', 'name': 'bundle_on_completion', 'message': f'bundle_on_completion={raw_bundle}', 'action': 'No action needed.'})
    preflight = [check.__dict__ for check in connector_checks(config)]
    for item in preflight:
        status = str(item.get('status') or 'ok')
        if status in {'warn', 'error'}:
            checks.append(
                {
                    'status': status,
                    'name': f"connector:{item.get('name')}",
                    'message': str(item.get('message') or '-'),
                    'action': str(item.get('action') or 'Run connectors doctor.'),
                }
            )
    errors = sum(1 for item in checks if item['status'] == 'error')
    warnings = sum(1 for item in checks if item['status'] == 'warn')
    status = 'error' if errors else 'warn' if warnings else 'ok'
    return {
        'workflow_path': workflow_path,
        'workflow': workflow,
        'status': status,
        'summary': {'ok': sum(1 for item in checks if item['status'] == 'ok'), 'warn': warnings, 'error': errors},
        'checks': checks,
        'preflight': preflight,
        'next_commands': [
            f'easy-agent workflow plan {workflow_path} -c easy-agent.yml',
            f'easy-agent workflow run {workflow_path} -c easy-agent.yml --dry-run',
            'easy-agent connectors doctor -c easy-agent.yml',
        ],
    }


def _print_workflow_doctor_payload(payload: dict[str, Any], output_format: str) -> None:
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False, default=str))
        return
    if output_format != 'pretty':
        raise typer.BadParameter('format must be pretty or json')
    table = Table(title=f"workflow doctor: {payload['workflow_path']}")
    table.add_column('Status', style='cyan')
    table.add_column('Check', style='green')
    table.add_column('Message')
    table.add_column('Action')
    for item in payload['checks']:
        table.add_row(str(item['status']), str(item['name']), str(item['message']), str(item['action']))
    console.print(table)


def _workflow_next_commands(pack: str, *, run_id: str | None = None) -> list[str]:
    commands = [
        'easy-agent connectors doctor -c easy-agent.yml',
        'easy-agent dashboard -c easy-agent.yml --output dashboard.html',
    ]
    if pack.startswith('browser-'):
        commands.insert(1, 'easy-agent browser doctor -c easy-agent.yml')
        commands.insert(2, 'easy-agent browser artifacts -c easy-agent.yml')
    if run_id:
        commands.insert(0, f'easy-agent runs triage {run_id} -c easy-agent.yml')
        commands.insert(1, f'easy-agent traces open {run_id} -c easy-agent.yml --no-browser')
    return commands


def _load_workflow_input(value: str) -> dict[str, Any]:
    path = Path(value)
    if path.exists() and path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        if not isinstance(loaded, dict):
            raise typer.BadParameter('workflow file must contain a YAML mapping.')
        if loaded.get('version') != 1:
            raise typer.BadParameter('workflow file version must be 1.')
        if not loaded.get('pack'):
            raise typer.BadParameter('workflow file must define pack.')
        get_task_pack(str(loaded['pack']))
        return loaded
    get_task_pack(value)
    return {'version': 1, 'name': value, 'pack': value, 'context': None, 'approval_mode': 'hybrid', 'bundle_on_completion': False}


def _print_workflow_payload(payload: dict[str, Any], output_format: str) -> None:
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    if output_format != 'pretty':
        raise typer.BadParameter('format must be pretty or json')
    table = Table(title=f"workflow: {payload['pack']}")
    table.add_column('Field', style='cyan')
    table.add_column('Value', style='green')
    for key in ['description', 'recommended_scenario', 'dry_run']:
        table.add_row(key, str(payload.get(key)))
    result = payload.get('result')
    if isinstance(result, dict):
        table.add_row('run_id', str(result.get('run_id') or '-'))
        table.add_row('status', str(result.get('status') or '-'))
    bundle = payload.get('bundle')
    if isinstance(bundle, dict):
        table.add_row('bundle', str(bundle.get('output_dir') or '-'))
    table.add_row('next_commands', '\n'.join(str(item) for item in payload.get('next_commands', [])))
    console.print(table)
    if payload.get('dry_run'):
        console.print_json(json.dumps({'prompt': payload.get('prompt'), 'acceptance_criteria': payload.get('acceptance_criteria')}, ensure_ascii=False))


def _browser_plan_command(
    kind: str,
    url: str,
    config: str,
    *,
    context: str | None,
    run: bool,
    approval_mode: str,
    output_format: str,
) -> None:
    browser_context = _browser_context(kind, url, context)
    task_pack = 'browser-audit' if kind in {'audit', 'seo', 'a11y', 'links'} else 'browser-qa'
    prompt = render_task_prompt(task_pack, browser_context)
    payload: dict[str, Any] = {
        'kind': kind,
        'url': url,
        'pack': task_pack,
        'mode': 'run' if run else 'plan_only',
        'doctor': browser_doctor(config),
        'prompt': prompt,
        'next_commands': [
            'easy-agent browser doctor -c easy-agent.yml',
            'easy-agent connectors test browser -c easy-agent.yml',
            'easy-agent browser artifacts -c easy-agent.yml',
        ],
    }
    if not run:
        _print_browser_payload(payload, output_format, title=f'browser {kind}')
        return

    async def _run(runtime: EasyAgentRuntime) -> None:
        result: dict[str, Any] = await runtime.run(prompt, approval_mode=HumanLoopMode(approval_mode))
        payload['result'] = result
        run_id = str(result.get('run_id') or '')
        if run_id:
            payload['next_commands'] = [
                f'easy-agent browser report {run_id} -c easy-agent.yml',
                f'easy-agent runs triage {run_id} -c easy-agent.yml',
                f'easy-agent traces open {run_id} -c easy-agent.yml --no-browser',
                'easy-agent browser artifacts -c easy-agent.yml',
            ]
        _print_browser_payload(payload, output_format, title=f'browser {kind}')

    asyncio.run(with_runtime(config, _run))


def _browser_context(kind: str, url: str, context: str | None) -> str:
    if kind == 'smoke':
        objective = (
            'Open the page, collect a snapshot/accessibility-tree first, verify title and visible readiness, '
            'then record artifacts and blocked follow-up actions.'
        )
    elif kind == 'audit':
        objective = (
            'Collect Playwright MCP snapshot/accessibility-tree evidence first, then audit title, meta description, canonical signals, '
            'heading hierarchy, visible content, internal and external links, basic accessibility risks, and page-quality gaps.'
        )
    elif kind == 'seo':
        objective = (
            'Collect Playwright MCP snapshot/accessibility-tree evidence first, then check title, meta description, canonical URL, '
            'indexability signals, heading hierarchy, visible content relevance, internal links, and prioritized SEO fixes.'
        )
    elif kind == 'a11y':
        objective = (
            'Collect Playwright MCP snapshot/accessibility-tree evidence first, then check landmarks, heading order, names and labels, '
            'interactive controls, keyboard risks, dialog focus risks, and prioritized accessibility fixes.'
        )
    elif kind == 'links':
        objective = (
            'Collect Playwright MCP snapshot/accessibility-tree evidence first, then map internal links, external links, navigation links, '
            'missing or suspicious hrefs, repeated calls to action, and link-quality follow-up checks.'
        )
    else:
        objective = 'Collect a Playwright MCP snapshot or equivalent accessibility-tree evidence before screenshots, then summarize notable page structure.'
    extra = context or 'No additional context provided.'
    return f'URL: {url}\nObjective: {objective}\nAdditional context: {extra}'


def _print_browser_payload(payload: dict[str, Any], output_format: str, *, title: str) -> None:
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False, default=str))
        return
    if output_format != 'pretty':
        raise typer.BadParameter('format must be pretty or json')
    table = Table(title=title)
    table.add_column('Field', style='cyan')
    table.add_column('Value', style='green')
    for key in ['kind', 'url', 'mode']:
        if key in payload:
            table.add_row(key, str(payload[key]))
    doctor = payload.get('doctor')
    if isinstance(doctor, dict):
        table.add_row('enabled', str(doctor.get('enabled')))
        table.add_row('npx_available', str(doctor.get('npx_available')))
        table.add_row('require_approval', str(doctor.get('require_approval')))
    result = payload.get('result')
    if isinstance(result, dict):
        table.add_row('run_id', str(result.get('run_id') or '-'))
        table.add_row('status', str(result.get('status') or '-'))
    triage = payload.get('triage')
    if isinstance(triage, dict):
        table.add_row('likely_layer', str(triage.get('likely_layer') or '-'))
        table.add_row('severity', str(triage.get('severity') or '-'))
    table.add_row('next_commands', '\n'.join(str(item) for item in payload.get('next_commands', [])))
    console.print(table)


def _safe_token(value: str) -> str:
    return ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '-' for ch in value.lower()) or 'unknown'
