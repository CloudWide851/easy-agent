from __future__ import annotations

import asyncio
import json
import time
from typing import Any

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
federation_auth_app = typer.Typer(help='Manage federation remote authorization.')
workbench_app = typer.Typer(help='Inspect and manage isolated workbench sessions.')
mcp_auth_app = typer.Typer(help='Manage MCP remote authorization.')
mcp_roots_app = typer.Typer(help='Inspect and refresh MCP roots.')
mcp_resources_app = typer.Typer(help='Inspect and manage MCP resources.')
mcp_prompts_app = typer.Typer(help='Inspect MCP prompts.')
mcp_app.add_typer(mcp_auth_app, name='auth')
mcp_app.add_typer(mcp_roots_app, name='roots')
mcp_app.add_typer(mcp_resources_app, name='resources')
mcp_app.add_typer(mcp_prompts_app, name='prompts')
federation_app.add_typer(federation_auth_app, name='auth')


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
        console.print_json(json.dumps(await runtime.mcp_manager.refresh_roots(server_name), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@mcp_resources_app.command('list')
def list_mcp_resources(
    server_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.mcp_manager.list_resources(server_name), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@mcp_resources_app.command('read')
def read_mcp_resource(
    server_name: str = typer.Argument(...),
    uri: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.mcp_manager.read_resource(server_name, uri), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@mcp_resources_app.command('templates')
def list_mcp_resource_templates(
    server_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(
            json.dumps(await runtime.mcp_manager.list_resource_templates(server_name), ensure_ascii=False)
        )

    asyncio.run(with_runtime(config, _run))


@mcp_resources_app.command('subscribe')
def subscribe_mcp_resource(
    server_name: str = typer.Argument(...),
    uri: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.mcp_manager.subscribe_resource(server_name, uri), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@mcp_resources_app.command('unsubscribe')
def unsubscribe_mcp_resource(
    server_name: str = typer.Argument(...),
    uri: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(
            json.dumps(await runtime.mcp_manager.unsubscribe_resource(server_name, uri), ensure_ascii=False)
        )

    asyncio.run(with_runtime(config, _run))


@mcp_prompts_app.command('list')
def list_mcp_prompts(
    server_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.mcp_manager.list_prompts(server_name), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@mcp_prompts_app.command('get')
def get_mcp_prompt(
    server_name: str = typer.Argument(...),
    prompt_name: str = typer.Argument(...),
    arguments: str | None = typer.Option(None, '--arguments'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        payload = json.loads(arguments) if arguments else None
        console.print_json(
            json.dumps(await runtime.mcp_manager.get_prompt(server_name, prompt_name, arguments=payload), ensure_ascii=False)
        )

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


@federation_auth_app.command('status')
def federation_auth_status(
    remote_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    runtime = build_runtime(config)
    try:
        console.print_json(json.dumps(runtime.federation_auth_status(remote_name), ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())


@federation_auth_app.command('login')
def federation_auth_login(
    remote_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        async def _redirect(url: str) -> None:
            console.print(f'Open this URL to authorize:\n{url}')

        async def _callback() -> tuple[str, str | None]:
            code = await asyncio.to_thread(console.input, 'Authorization code: ')
            state = await asyncio.to_thread(console.input, 'Returned state (blank if none): ')
            return code.strip(), state.strip() or None

        runtime.federation_manager.set_oauth_handlers(_redirect, _callback)
        console.print_json(json.dumps(await runtime.federation_authorize(remote_name), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@federation_auth_app.command('refresh')
def federation_auth_refresh(
    remote_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.federation_refresh_auth(remote_name), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@federation_auth_app.command('logout')
def federation_auth_logout(
    remote_name: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        await runtime.federation_logout(remote_name)
        console.print_json(json.dumps({'remote': remote_name, 'status': 'logged_out'}, ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@federation_app.command('tasks')
def list_federation_tasks(
    remote_name: str = typer.Argument(...),
    page_token: str | None = typer.Option(None, '--page-token'),
    page_size: int | None = typer.Option(None, '--page-size'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(
            json.dumps(
                await runtime.list_remote_tasks(remote_name, page_token=page_token, page_size=page_size),
                ensure_ascii=False,
            )
        )

    asyncio.run(with_runtime(config, _run))


@federation_app.command('events')
def list_federation_events(
    remote_name: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    after_sequence: int = typer.Option(0, '--after-sequence'),
    page_token: str | None = typer.Option(None, '--page-token'),
    page_size: int | None = typer.Option(None, '--page-size'),
    stream: bool = typer.Option(False, '--stream'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        payload: Any
        if stream:
            payload = await runtime.stream_remote_task_events(remote_name, task_id, after_sequence)
        else:
            payload = await runtime.list_remote_task_events(
                remote_name,
                task_id,
                after_sequence,
                page_token=page_token,
                page_size=page_size,
            )
        console.print_json(json.dumps(payload, ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@federation_app.command('cancel-task')
def cancel_federation_task(
    remote_name: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.cancel_remote_task(remote_name, task_id), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@federation_app.command('subscriptions')
def list_federation_subscriptions(
    remote_name: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.list_remote_subscriptions(remote_name, task_id), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@federation_app.command('renew-subscription')
def renew_federation_subscription(
    remote_name: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    subscription_id: str = typer.Argument(...),
    lease_seconds: int | None = typer.Option(None, '--lease-seconds'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(
            json.dumps(
                await runtime.renew_remote_subscription(
                    remote_name,
                    task_id,
                    subscription_id,
                    lease_seconds=lease_seconds,
                ),
                ensure_ascii=False,
            )
        )

    asyncio.run(with_runtime(config, _run))


@federation_app.command('cancel-subscription')
def cancel_federation_subscription(
    remote_name: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    subscription_id: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.cancel_remote_subscription(remote_name, task_id, subscription_id), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@federation_app.command('push-set')
def set_federation_push_notification(
    remote_name: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    callback_url: str = typer.Argument(...),
    lease_seconds: int | None = typer.Option(None, '--lease-seconds'),
    from_sequence: int = typer.Option(0, '--from-sequence'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(
            json.dumps(
                await runtime.set_remote_push_notification(
                    remote_name,
                    task_id,
                    callback_url,
                    lease_seconds=lease_seconds,
                    from_sequence=from_sequence,
                ),
                ensure_ascii=False,
            )
        )

    asyncio.run(with_runtime(config, _run))


@federation_app.command('push-get')
def get_federation_push_notification(
    remote_name: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    config_id: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.get_remote_push_notification(remote_name, task_id, config_id), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@federation_app.command('push-list')
def list_federation_push_notifications(
    remote_name: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.list_remote_push_notifications(remote_name, task_id), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@federation_app.command('push-delete')
def delete_federation_push_notification(
    remote_name: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    config_id: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(json.dumps(await runtime.delete_remote_push_notification(remote_name, task_id, config_id), ensure_ascii=False))

    asyncio.run(with_runtime(config, _run))


@federation_app.command('send-subscribe')
def send_subscribe_federation(
    remote_name: str = typer.Argument(...),
    target: str = typer.Argument(...),
    input_text: str = typer.Argument(...),
    callback_url: str = typer.Argument(...),
    session_id: str | None = typer.Option(None, '--session-id'),
    lease_seconds: int | None = typer.Option(None, '--lease-seconds'),
    from_sequence: int = typer.Option(0, '--from-sequence'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(
            json.dumps(
                await runtime.send_subscribe_remote(
                    remote_name,
                    target,
                    input_text,
                    callback_url,
                    session_id=session_id,
                    lease_seconds=lease_seconds,
                    from_sequence=from_sequence,
                ),
                ensure_ascii=False,
            )
        )

    asyncio.run(with_runtime(config, _run))


@federation_app.command('resubscribe')
def resubscribe_federation(
    remote_name: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    from_sequence: int = typer.Option(0, '--from-sequence'),
    callback_url: str | None = typer.Option(None, '--callback-url'),
    lease_seconds: int | None = typer.Option(None, '--lease-seconds'),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    async def _run(runtime: EasyAgentRuntime) -> None:
        console.print_json(
            json.dumps(
                await runtime.resubscribe_remote_task(
                    remote_name,
                    task_id,
                    from_sequence=from_sequence,
                    callback_url=callback_url,
                    lease_seconds=lease_seconds,
                ),
                ensure_ascii=False,
            )
        )

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

