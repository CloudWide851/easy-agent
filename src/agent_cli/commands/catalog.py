from __future__ import annotations

import asyncio
import json
import time

import typer
from rich.console import Console
from rich.table import Table

from agent_cli.shared import with_runtime
from agent_runtime import EasyAgentRuntime, build_runtime

console = Console()
skills_app = typer.Typer(help='Inspect registered skills.')
mcp_app = typer.Typer(help='Inspect discovered MCP tools.')
plugins_app = typer.Typer(help='Inspect loaded plugins.')
teams_app = typer.Typer(help='Inspect configured agent teams.')
federation_app = typer.Typer(help='Inspect and serve federated agent surfaces.')
workbench_app = typer.Typer(help='Inspect and manage isolated workbench sessions.')
mcp_auth_app = typer.Typer(help='Manage MCP remote authorization.')
mcp_roots_app = typer.Typer(help='Inspect and refresh MCP roots.')
mcp_app.add_typer(mcp_auth_app, name='auth')
mcp_app.add_typer(mcp_roots_app, name='roots')


@skills_app.command('list')
def list_skills(config: str = typer.Option('easy-agent.yml', '-c', '--config')) -> None:
    runtime = build_runtime(config)
    try:
        table = Table(title='skills')
        table.add_column('Name', style='cyan')
        table.add_column('Description', style='green')
        for skill in runtime.skills:
            table.add_row(skill.name, skill.description)
        console.print(table)
    finally:
        asyncio.run(runtime.aclose())


@mcp_app.command('list')
def list_mcp(config: str = typer.Option('easy-agent.yml', '-c', '--config')) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        table = Table(title='mcp tools')
        table.add_column('Server', style='cyan')
        table.add_column('Transport', style='green')
        table.add_column('Tool', style='yellow')
        servers = await runtime.mcp_manager.list_servers()
        for server_name, tools in servers.items():
            transport = runtime.config.mcp_map[server_name].transport
            for tool in tools:
                table.add_row(server_name, transport, tool.name)
        console.print(table)

    asyncio.run(with_runtime(config, _run))


@mcp_roots_app.command('list')
def list_mcp_roots(
    server_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.mcp_manager.list_roots(server_name), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@mcp_roots_app.command('refresh')
def refresh_mcp_roots(
    server_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        await runtime.mcp_manager.refresh_roots(server_name)
        console.print_json(json.dumps({'server': server_name, 'status': 'roots_refreshed'}, ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@mcp_auth_app.command('status')
def mcp_auth_status(
    server_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    runtime = build_runtime(config)
    try:
        console.print_json(json.dumps(runtime.mcp_manager.auth_status(server_name), ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())


@mcp_auth_app.command('login')
def mcp_auth_login(
    server_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        async def _redirect(url: str) -> None:
            console.print(f'Open this URL to authorize:\n{url}')

        async def _callback() -> tuple[str, str | None]:
            code = await asyncio.to_thread(console.input, 'Authorization code: ')
            state = await asyncio.to_thread(console.input, 'Returned state (blank if none): ')
            return code.strip(), state.strip() or None

        runtime.mcp_manager.set_oauth_handlers(_redirect, _callback)
        await runtime.mcp_manager.authorize(server_name)
        console.print_json(json.dumps(runtime.mcp_manager.auth_status(server_name), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@mcp_auth_app.command('logout')
def mcp_auth_logout(
    server_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        await runtime.mcp_manager.logout(server_name)
        console.print_json(json.dumps({'server': server_name, 'status': 'logged_out'}, ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@plugins_app.command('list')
def list_plugins(config: str = typer.Option('easy-agent.yml', '-c', '--config')) -> None:
    runtime = build_runtime(config)
    try:
        table = Table(title='plugins')
        table.add_column('Source', style='cyan')
        for source in runtime.loaded_sources:
            table.add_row(source)
        console.print(table)
    finally:
        asyncio.run(runtime.aclose())


@teams_app.command('list')
def list_teams(config: str = typer.Option('easy-agent.yml', '-c', '--config')) -> None:
    runtime = build_runtime(config)
    try:
        table = Table(title='teams')
        table.add_column('Name', style='cyan')
        table.add_column('Mode', style='green')
        table.add_column('Members', style='yellow')
        table.add_column('Max Turns', style='magenta')
        table.add_column('Termination', style='white')
        for team in runtime.config.graph.teams:
            table.add_row(
                team.name,
                team.mode.value,
                ', '.join(team.members),
                str(team.max_turns),
                team.termination_text,
            )
        console.print(table)
    finally:
        asyncio.run(runtime.aclose())


@federation_app.command('list')
def list_federation(config: str = typer.Option('easy-agent.yml', '-c', '--config')) -> None:
    runtime = build_runtime(config)
    try:
        table = Table(title='federation')
        table.add_column('Type', style='cyan')
        table.add_column('Name', style='green')
        table.add_column('Target', style='yellow')
        for remote in runtime.config.federation.remotes:
            table.add_row('remote', remote.name, remote.base_url)
        for export in runtime.config.federation.exports:
            table.add_row('export', export.name, f'{export.target_type}:{export.target}')
        console.print(table)
    finally:
        asyncio.run(runtime.aclose())


@federation_app.command('inspect')
def inspect_federation(
    remote_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.inspect_remote(remote_name), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@federation_app.command('serve')
def serve_federation(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    runtime = build_runtime(config)
    try:
        asyncio.run(runtime.start())
        status = runtime.serve_federation()
        console.print_json(json.dumps(status, ensure_ascii=False))
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        runtime.stop_federation()
    finally:
        asyncio.run(runtime.aclose())


@workbench_app.command('list')
def list_workbench(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    owner_run_id: str | None = typer.Option(None, '--run-id'),
) -> None:
    runtime = build_runtime(config)
    try:
        console.print_json(json.dumps(runtime.list_workbench_sessions(owner_run_id=owner_run_id), ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())


@workbench_app.command('gc')
def gc_workbench(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    runtime = build_runtime(config)
    try:
        removed = runtime.gc_workbench_sessions()
        console.print_json(json.dumps({'removed_sessions': removed}, ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())
