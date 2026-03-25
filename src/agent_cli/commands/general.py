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

from agent_cli.shared import with_runtime
from agent_common.models import RunContext
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
        ('Configured MCP Servers', str(len(runtime.config.mcp))),
        ('MCP Transports', _mcp_transport_summary(runtime)),
        ('Loaded Sources', str(len(runtime.loaded_sources))),
        ('Sandbox Mode', sandbox['mode']),
        ('Sandbox Targets', ', '.join(sandbox['targets'])),
        ('Windows Sandbox', str(sandbox['windows_sandbox_available'])),
        ('Sandbox Fallback', sandbox['windows_sandbox_fallback']),
        ('Storage', str(runtime.store.base_path.resolve())),
    ]


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
    ) -> None:
        async def _run(runtime: EasyAgentRuntime) -> None:
            result = await runtime.run(input_text)
            console.print_json(json.dumps(result, ensure_ascii=False))

        asyncio.run(with_runtime(config, _run))

    @app.command()
    def trace(run_id: str, config: str = typer.Option('easy-agent.yml', '-c', '--config')) -> None:
        runtime = build_runtime(config)
        try:
            payload = runtime.store.load_trace(run_id)
            console.print_json(json.dumps(payload, ensure_ascii=False))
        finally:
            asyncio.run(runtime.aclose())
