from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from textwrap import dedent
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from agent_config.app import AppConfig, load_config
from agent_runtime import build_runtime
from agent_runtime.diagnostics import explain_run

console = Console()
template_app = typer.Typer(help='Create starter project templates.')
config_app = typer.Typer(help='Validate and explain easy-agent configuration.')


def register(app: typer.Typer) -> None:
    @app.command('setup')
    def setup(
        path: str = typer.Option('easy-agent.yml', '--path', help='Config file to create or reuse.'),
        provider: str = typer.Option('mock', '--provider', help='Provider preset: mock or deepseek.'),
        force: bool = typer.Option(False, '--force', help='Overwrite an existing config file.'),
        skip_smoke: bool = typer.Option(False, '--skip-smoke', help='Create or validate config without running a smoke test.'),
        output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
    ) -> None:
        if provider not in {'mock', 'deepseek'}:
            raise typer.BadParameter('provider must be mock or deepseek')
        if provider == 'deepseek' and not os.environ.get('DEEPSEEK_API_KEY'):
            raise typer.BadParameter('DEEPSEEK_API_KEY is not set. Use --provider mock for offline setup.')
        target = Path(path)
        created = False
        if target.exists() and not force:
            load_config(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(yaml.safe_dump(_setup_config(provider), sort_keys=False), encoding='utf-8')
            created = True
        if skip_smoke:
            payload = {'config': str(target), 'created': created, 'smoke': 'skipped'}
            _print_setup_payload(payload, output_format)
            return

        async def _run() -> None:
            runtime = build_runtime(target)
            try:
                try:
                    result = await runtime.run('Run setup smoke and call python_echo once.')
                    run_id = str(result.get('run_id') or '')
                    payload: dict[str, Any] = {
                        'config': str(target),
                        'created': created,
                        'smoke': result,
                        'next_commands': _run_debug_commands(run_id, target) if run_id else [],
                    }
                    _print_setup_payload(payload, output_format)
                except Exception:
                    runs = runtime.store.list_runs(limit=1)
                    run_id = str(runs[0]['run_id']) if runs else ''
                    payload = {
                        'config': str(target),
                        'created': created,
                        'smoke': 'failed',
                        'diagnostic': explain_run(runtime.store, run_id) if run_id else None,
                    }
                    _print_setup_payload(payload, output_format)
                    raise
            finally:
                await runtime.aclose()

        asyncio.run(_run())

    @app.command('init')
    def init_config(
        path: str = typer.Option('easy-agent.yml', '--path', help='Config file to create.'),
        provider: str = typer.Option('mock', '--provider', help='Provider preset: mock or deepseek.'),
        force: bool = typer.Option(False, '--force', help='Overwrite an existing config file.'),
    ) -> None:
        target = Path(path)
        if target.exists() and not force:
            raise typer.BadParameter(f'{target} already exists; pass --force to overwrite it.')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_starter_config(provider), encoding='utf-8')
        console.print(f'[green]Created[/green] {target}')
        console.print('Next: easy-agent quickstart')

    @app.command('quickstart')
    def quickstart(
        provider: str = typer.Option('mock', '--provider', help='Provider preset: mock or deepseek.'),
    ) -> None:
        if provider == 'deepseek' and not os.environ.get('DEEPSEEK_API_KEY'):
            raise typer.BadParameter('DEEPSEEK_API_KEY is not set. Use --provider mock for offline quickstart.')
        config_path = Path('.easy-agent/quickstart/easy-agent.yml')
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump(_quickstart_config(provider), sort_keys=False), encoding='utf-8')

        async def _run() -> None:
            runtime = build_runtime(config_path)
            try:
                result = await runtime.run('Run the offline quickstart and echo the task once.')
                run_id = str(result.get('run_id') or '')
                console.print_json(json.dumps(result, ensure_ascii=False))
                if run_id:
                    console.print('\nNext debugging commands:')
                    for command in _run_debug_commands(run_id, config_path):
                        console.print(command)
            finally:
                await runtime.aclose()

        asyncio.run(_run())


@template_app.command('list')
def list_templates() -> None:
    table = Table(title='easy-agent templates')
    table.add_column('Name', style='cyan')
    table.add_column('Description', style='green')
    for name, template in _templates().items():
        table.add_row(name, str(template['description']))
    console.print(table)


@template_app.command('create')
def create_template(
    name: str = typer.Argument(..., help='Template name.'),
    dest: str = typer.Argument(..., help='Destination directory.'),
    force: bool = typer.Option(False, '--force', help='Overwrite generated files when they already exist.'),
) -> None:
    templates = _templates()
    if name not in templates:
        raise typer.BadParameter(f"Unknown template '{name}'. Run 'easy-agent template list'.")
    destination = Path(dest)
    files = templates[name]['files']
    if not isinstance(files, dict):
        raise RuntimeError(f"Template '{name}' is invalid.")
    collisions = [destination / relative for relative in files if (destination / relative).exists()]
    if collisions and not force:
        joined = ', '.join(str(item) for item in collisions)
        raise typer.BadParameter(f'Generated files already exist: {joined}. Pass --force to overwrite them.')
    for relative, content in files.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding='utf-8')
    console.print(f'[green]Created template[/green] {name} at {destination}')


@config_app.command('validate')
def validate_config(config: str = typer.Option('easy-agent.yml', '-c', '--config')) -> None:
    loaded = load_config(config)
    payload = _config_summary(loaded)
    console.print_json(json.dumps({'valid': True, **payload}, ensure_ascii=False))


@config_app.command('explain')
def explain_config(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    loaded = load_config(config)
    payload = _config_explanation(loaded, Path(config))
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title=f'easy-agent config: {config}')
    table.add_column('Field', style='cyan')
    table.add_column('Value', style='green')
    for key, value in payload.items():
        table.add_row(key, json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
    console.print(table)


def _starter_config(provider: str, sensitive_tools: list[str] | None = None) -> str:
    if provider not in {'mock', 'deepseek'}:
        raise typer.BadParameter('provider must be mock or deepseek')
    model_block = (
        dedent(
            """
model:
  provider: mock
  protocol: mock
  model: mock-agent
  base_url: mock://local
  api_key_env: EASY_AGENT_MOCK_API_KEY
"""
        ).strip()
        if provider == 'mock'
        else dedent(
            """
model:
  provider: deepseek
  protocol: auto
  model: deepseek-chat
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
"""
        ).strip()
    )
    body = """graph:
  name: starter
  entrypoint: assistant
  agents:
    - name: assistant
      description: A focused starter assistant.
      system_prompt: |
        You are a concise assistant. Use python_echo once when it helps, then produce a final answer.
      tools:
        - python_echo
      max_iterations: 4
  nodes: []

skills:
  - path: skills/examples

storage:
  path: .easy-agent
  database: state.db

security:
"""
    if sensitive_tools:
        body += """  human_loop:
    mode: deferred
    sensitive_tools:
"""
        body += ''.join(f'      - {tool}\n' for tool in sensitive_tools)
    body += """  sandbox:
    mode: auto
    working_root: .
"""
    return f'{model_block}\n\n{body}'


def _quickstart_config(provider: str) -> dict[str, Any]:
    if provider not in {'mock', 'deepseek'}:
        raise typer.BadParameter('provider must be mock or deepseek')
    model = {
        'provider': 'mock',
        'protocol': 'mock',
        'model': 'mock-agent',
        'base_url': 'mock://local',
        'api_key_env': 'EASY_AGENT_MOCK_API_KEY',
    }
    if provider == 'deepseek':
        model = {
            'provider': 'deepseek',
            'protocol': 'auto',
            'model': 'deepseek-chat',
            'base_url': 'https://api.deepseek.com',
            'api_key_env': 'DEEPSEEK_API_KEY',
        }
    return {
        'model': model,
        'graph': {
            'name': 'quickstart',
            'entrypoint': 'assistant',
            'agents': [
                {
                    'name': 'assistant',
                    'description': 'Offline quickstart assistant.',
                    'system_prompt': 'Use python_echo at most once, then write a concise final answer.',
                    'tools': ['python_echo'],
                    'max_iterations': 4,
                }
            ],
            'nodes': [],
        },
        'skills': [{'path': str(_repo_root() / 'skills' / 'examples').replace('\\', '/')}],
        'storage': {'path': '.easy-agent/quickstart', 'database': 'state.db'},
        'security': {'sandbox': {'mode': 'auto', 'working_root': '.'}},
    }


def _setup_config(provider: str) -> dict[str, Any]:
    config = _quickstart_config(provider)
    config['graph']['name'] = 'setup'
    config['storage'] = {'path': '.easy-agent', 'database': 'state.db'}
    return config


def _templates() -> dict[str, dict[str, Any]]:
    return {
        'basic-agent': {
            'description': 'Single mock-backed agent for local development.',
            'files': _template_files('basic-agent', 'A single local assistant.', _starter_config('mock')),
        },
        'tool-agent': {
            'description': 'Single agent with python_echo mounted as a starter tool.',
            'files': _template_files('tool-agent', 'A tool-using local assistant.', _starter_config('mock')),
        },
        'human-approval-agent': {
            'description': 'Starter config with python_echo marked as a sensitive tool.',
            'files': _template_files(
                'human-approval-agent',
                'A starter approval workflow.',
                _starter_config('mock', sensitive_tools=['python_echo']),
            ),
        },
        'longrun-harness': {
            'description': 'Initializer, worker, and evaluator harness starter.',
            'files': _template_files('longrun-harness', 'A minimal long-running harness starter.', _harness_template_config()),
        },
        'mcp-filesystem-agent': {
            'description': 'Single agent wired for a filesystem MCP server.',
            'files': _template_files(
                'mcp-filesystem-agent',
                'A starter for explicit MCP filesystem roots.',
                _mcp_filesystem_template_config(),
            ),
        },
        'eval-smoke': {
            'description': 'Public-eval smoke config with mock-backed local execution.',
            'files': _template_files('eval-smoke', 'A starter for public-eval smoke runs.', _eval_smoke_template_config()),
        },
        'federation-loopback': {
            'description': 'Local federation export starter for loopback A2A-style checks.',
            'files': _template_files(
                'federation-loopback',
                'A starter for local federation loopback checks.',
                _federation_loopback_template_config(),
            ),
        },
        'workbench-coding-agent': {
            'description': 'Process workbench starter for coding-agent style tool runs.',
            'files': _template_files(
                'workbench-coding-agent',
                'A starter for workbench-backed coding tasks.',
                _workbench_template_config(),
            ),
        },
    }


def _template_files(name: str, description: str, config: str) -> dict[str, str]:
    return {
        'easy-agent.yml': config,
        'README.md': dedent(
            f"""
            # {name}

            {description}

            ## Run

            ```bash
            easy-agent doctor -c easy-agent.yml
            easy-agent config explain -c easy-agent.yml
            easy-agent run "Hello from the template" -c easy-agent.yml
            ```
            """
        ).lstrip(),
        '.env.local.example': 'DEEPSEEK_API_KEY=<SECRET>\nSERPAPI_API_KEY=<SECRET>\n',
    }


def _harness_template_config() -> str:
    return _starter_config('mock') + dedent(
        """

        harnesses:
          - name: delivery_loop
            initializer_agent: assistant
            worker_target: assistant
            evaluator_agent: assistant
            completion_contract: Finish one useful increment and summarize the outcome.
            artifacts_dir: .easy-agent/harness
            max_cycles: 2
            max_replans: 0
        """
    )


def _mcp_filesystem_template_config() -> str:
    return _starter_config('mock') + dedent(
        """

        mcp:
          - name: filesystem
            transport: stdio
            command:
              - npx
              - -y
              - "@modelcontextprotocol/server-filesystem"
              - .
            roots:
              - path: .
                name: project
        """
    )


def _eval_smoke_template_config() -> str:
    return _starter_config('mock') + dedent(
        """

        evaluation:
          public_eval:
            profile: subset
            enable_full_bfcl: false
            provider_compatibility:
              enabled: false
        """
    )


def _federation_loopback_template_config() -> str:
    return _starter_config('mock') + dedent(
        """

        federation:
          exports:
            - name: local_assistant
              target_type: agent
              target: assistant
              description: Local assistant exported for loopback federation checks.
        """
    )


def _workbench_template_config() -> str:
    return _starter_config('mock') + dedent(
        """

        executors:
          - name: process
            kind: process
            default_timeout_seconds: 30
        workbench:
          root: .easy-agent/workbench
          default_executor: process
          session_ttl_seconds: 3600
        """
    )


def _run_debug_commands(run_id: str, config_path: Path) -> list[str]:
    return [
        f'easy-agent runs show {run_id} -c {config_path}',
        f'easy-agent runs explain {run_id} -c {config_path}',
        f'easy-agent traces export {run_id} -c {config_path}',
        f'easy-agent traces export {run_id} -c {config_path} --html --output trace.html',
    ]


def _print_setup_payload(payload: dict[str, Any], output_format: str) -> None:
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title='easy-agent setup')
    table.add_column('Field', style='cyan')
    table.add_column('Value', style='green')
    table.add_row('Config', str(payload['config']))
    table.add_row('Created', str(payload['created']))
    table.add_row('Smoke', str(payload['smoke'] if isinstance(payload['smoke'], str) else payload['smoke'].get('status')))
    console.print(table)
    if payload.get('next_commands'):
        console.print('\nNext debugging commands:')
        for command in payload['next_commands']:
            console.print(command)
    if payload.get('diagnostic'):
        console.print_json(json.dumps(payload['diagnostic'], ensure_ascii=False))


def _config_summary(config: AppConfig) -> dict[str, Any]:
    return {
        'provider': config.model.provider,
        'model': config.model.model,
        'protocol': config.model.protocol.value,
        'entrypoint': config.graph.entrypoint,
        'entrypoint_type': _entrypoint_type(config),
        'agents': len(config.graph.agents),
        'teams': len(config.graph.teams),
        'harnesses': len(config.harnesses),
        'skills': len(config.skills),
        'mcp_servers': len(config.mcp),
    }


def _config_explanation(config: AppConfig, config_path: Path) -> dict[str, Any]:
    env_vars = _required_env_vars(config)
    return {
        'config': str(config_path),
        **_config_summary(config),
        'agent_tools': {agent.name: agent.tools for agent in config.graph.agents},
        'teams_detail': [
            {'name': team.name, 'mode': team.mode.value, 'members': team.members}
            for team in config.graph.teams
        ],
        'harnesses_detail': [
            {
                'name': harness.name,
                'initializer_agent': harness.initializer_agent,
                'worker_target': harness.worker_target,
                'evaluator_agent': harness.evaluator_agent,
                'max_cycles': harness.max_cycles,
            }
            for harness in config.harnesses
        ],
        'skills_detail': [source.path for source in config.skills],
        'mcp_detail': [
            {'name': server.name, 'transport': server.transport, 'executor': server.executor or 'default'}
            for server in config.mcp
        ],
        'human_loop': {
            'mode': config.security.human_loop.mode.value,
            'sensitive_tools': config.security.human_loop.sensitive_tools,
        },
        'guardrails': {
            'tool_input': config.guardrails.tool_input_hooks,
            'final_output': config.guardrails.final_output_hooks,
        },
        'storage': {'path': config.storage.path, 'database': config.storage.database},
        'executors': [{'name': executor.name, 'kind': executor.kind} for executor in config.executors],
        'workbench': {'root': config.workbench.root, 'default_executor': config.workbench.default_executor},
        'federation': {
            'remotes': [remote.name for remote in config.federation.remotes],
            'exports': [export.name for export in config.federation.exports],
        },
        'evaluation': {
            'public_eval_profile': config.evaluation.public_eval.profile,
            'provider_compatibility': config.evaluation.public_eval.provider_compatibility.enabled,
        },
        'required_env': [
            {'name': name, 'status': 'present' if os.environ.get(name) else 'missing'}
            for name in env_vars
        ],
    }


def _entrypoint_type(config: AppConfig) -> str:
    if config.graph.nodes:
        return 'graph'
    if config.graph.entrypoint in config.agent_map:
        return 'agent'
    if config.graph.entrypoint in config.team_map:
        return 'team'
    return 'unknown'


def _required_env_vars(config: AppConfig) -> list[str]:
    names = {config.model.api_key_env}
    public_eval = config.evaluation.public_eval
    names.add(public_eval.web_search.api_key_env)
    if public_eval.grader.enabled:
        names.add(public_eval.grader.api_key_env)
    for target in public_eval.provider_compatibility.targets:
        names.add(target.api_key_env)
    for server in config.mcp:
        if server.auth.token_env:
            names.add(server.auth.token_env)
        if server.auth.header_env:
            names.add(server.auth.header_env)
    for remote in config.federation.remotes:
        auth = remote.auth
        if auth.token_env:
            names.add(auth.token_env)
        if auth.header_env:
            names.add(auth.header_env)
        if auth.oauth.client_id_env:
            names.add(auth.oauth.client_id_env)
        if auth.oauth.client_secret_env:
            names.add(auth.oauth.client_secret_env)
    return sorted(name for name in names if name)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
