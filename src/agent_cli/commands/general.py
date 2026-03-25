from __future__ import annotations

import asyncio
import json
import platform
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agent_cli.shared import with_runtime
from agent_common.models import RunContext
from agent_protocols import resolve_protocol
from agent_runtime import EasyAgentRuntime, build_runtime

console = Console()


def register(app: typer.Typer) -> None:
    @app.command()
    def doctor(
        config: str = typer.Option('easy-agent.yml', '-c', '--config'),
        smoke: bool = False,
    ) -> None:
        async def _run(runtime: EasyAgentRuntime) -> None:
            adapter = resolve_protocol(runtime.config.model)
            sandbox = runtime.sandbox_manager.describe()
            table = Table(title='easy-agent doctor')
            table.add_column('Check', style='cyan')
            table.add_column('Value', style='green')
            table.add_row('Python', sys.version.split()[0])
            table.add_row('Platform', platform.platform())
            table.add_row('Protocol', adapter.protocol.value)
            table.add_row('Skills', str(len(runtime.skills)))
            table.add_row('Teams', str(len(runtime.config.graph.teams)))
            table.add_row('MCP Servers', str(len(runtime.mcp_manager._clients)))
            table.add_row('Loaded Sources', str(len(runtime.loaded_sources)))
            table.add_row('Sandbox Mode', sandbox['mode'])
            table.add_row('Sandbox Targets', ', '.join(sandbox['targets']))
            table.add_row('Windows Sandbox', str(sandbox['windows_sandbox_available']))
            table.add_row('Storage', str(runtime.store.base_path.resolve()))
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
