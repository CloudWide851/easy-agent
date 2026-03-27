from __future__ import annotations

import asyncio
import json
import platform
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from agent_cli.shared import build_cli_inline_resolver, with_runtime
from agent_common.models import HumanLoopMode, RunContext
from agent_protocols import resolve_protocol
from agent_runtime import EasyAgentRuntime, build_runtime

console = Console()



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
    return [
        ('Python', sys.version.split()[0]),
        ('Platform', platform.platform()),
        ('Provider', runtime.config.model.provider),
        ('Model', runtime.config.model.model),
        ('Protocol', adapter.protocol.value),
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
