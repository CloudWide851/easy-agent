from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
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
            loaded = load_config(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(yaml.safe_dump(_setup_config(provider), sort_keys=False), encoding='utf-8')
            created = True
            loaded = load_config(target)
        checks = _diagnostic_checks(loaded, target)
        if skip_smoke:
            payload = {'config': str(target), 'created': created, 'smoke': 'skipped', 'checks': checks}
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
                        'checks': checks,
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
                        'checks': checks,
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

    @app.command('new')
    def new_scenario(
        scenario: str = typer.Argument(..., help='Starter scenario name.'),
        dest: str | None = typer.Argument(None, help='Destination directory. Defaults to the scenario name.'),
        force: bool = typer.Option(False, '--force', help='Overwrite generated files when they already exist.'),
    ) -> None:
        destination = Path(dest) if dest else Path(scenario)
        _create_template(scenario, destination, force)


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
    _create_template(name, Path(dest), force)


def _create_template(name: str, destination: Path, force: bool) -> None:
    templates = _templates()
    if name not in templates:
        raise typer.BadParameter(f"Unknown template '{name}'. Run 'easy-agent template list'.")
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


@config_app.command('doctor')
def doctor_config(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    output_format: str = typer.Option('pretty', '--format', help='Output format: pretty or json.'),
) -> None:
    loaded = load_config(config)
    checks = _diagnostic_checks(loaded, Path(config))
    payload = {'config': config, 'status': _overall_status(checks), 'summary': _check_summary(checks), 'checks': checks}
    if output_format == 'json':
        console.print_json(json.dumps(payload, ensure_ascii=False))
    else:
        table = Table(title=f'easy-agent config doctor: {config}')
        table.add_column('Status', style='cyan')
        table.add_column('Check', style='green')
        table.add_column('Message')
        table.add_column('Action')
        for check in checks:
            table.add_row(
                str(check['status']),
                str(check['check']),
                str(check['message']),
                str(check['action']),
            )
        console.print(table)
    if payload['status'] == 'error':
        raise typer.Exit(1)


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
        'coding-agent': {
            'description': 'Business-ready coding assistant with a process workbench smoke path.',
            'files': _template_files(
                'coding-agent',
                'A practical starter for coding tasks, local files, and workbench-backed tool runs.',
                _coding_agent_template_config(),
            ),
        },
        'research-agent': {
            'description': 'Business-ready research assistant with official-source search wiring.',
            'files': _template_files(
                'research-agent',
                'A practical starter for source-first research tasks with optional live search.',
                _research_agent_template_config(),
            ),
        },
        'data-agent': {
            'description': 'Business-ready data assistant for CSV, JSON, logs, and metric summaries.',
            'files': _template_files(
                'data-agent',
                'A practical starter for data summaries, lightweight analysis, and evidence-backed recommendations.',
                _data_agent_template_config(),
            ),
        },
        'ops-agent': {
            'description': 'Business-ready ops assistant for runbooks, diagnostics, and release checks.',
            'files': _template_files(
                'ops-agent',
                'A practical starter for operational diagnostics and runbook-style task planning.',
                _ops_agent_template_config(),
            ),
        },
        'browser-agent': {
            'description': 'Business-ready browser task planner with a mock-first smoke path.',
            'files': _template_files(
                'browser-agent',
                'A practical starter for browser task planning before wiring a real browser connector.',
                _browser_agent_template_config(),
            ),
        },
    }


def _template_files(name: str, description: str, config: str) -> dict[str, str]:
    env_example = _template_env_example(name)
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
            easy-agent config doctor -c easy-agent.yml
            easy-agent run "Hello from the template" -c easy-agent.yml
            easy-agent runs list -c easy-agent.yml
            ```

            ## Smoke

            ```bash
            easy-agent config doctor -c easy-agent.yml
            easy-agent run "Run the template smoke path once." -c easy-agent.yml
            easy-agent traces export <run_id> -c easy-agent.yml --html --output trace.html
            ```
            """
        ).lstrip(),
        '.env.local.example': env_example,
    }


def _template_env_example(name: str) -> str:
    lines = ['# Optional live-provider credentials. Keep real values in your shell or .env.local only.']
    if name == 'eval-smoke':
        lines.append('SERPAPI_API_KEY=<SECRET>')
    elif name == 'federation-loopback':
        lines.append('EASY_AGENT_FEDERATION_TOKEN=<SECRET>')
    elif name in {'mcp-filesystem-agent', 'workbench-coding-agent', 'coding-agent', 'data-agent', 'ops-agent', 'browser-agent'}:
        lines.append('# No credentials are required for the mock-backed smoke path.')
    elif name == 'research-agent':
        lines.append('# No credentials are required for the mock-backed smoke path.')
        lines.append('SERPAPI_API_KEY=<SECRET>')
    else:
        lines.append('DEEPSEEK_API_KEY=<SECRET>')
    return '\n'.join(lines) + '\n'


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


def _coding_agent_template_config() -> str:
    return dedent(
        """
        model:
          provider: mock
          protocol: mock
          model: mock-agent
          base_url: mock://local
          api_key_env: EASY_AGENT_MOCK_API_KEY

        graph:
          name: coding_agent
          entrypoint: coder
          agents:
            - name: coder
              description: Coding assistant for local repository tasks.
              system_prompt: |
                You are a pragmatic coding assistant. For mock smoke runs, call python_echo once.
                For real coding work, inspect the task, keep changes scoped, and summarize files changed.
              tools:
                - python_echo
              max_iterations: 4
          nodes: []

        skills:
          - path: skills/examples

        executors:
          - name: process
            kind: process
            default_timeout_seconds: 30

        workbench:
          root: .easy-agent/workbench
          default_executor: process
          session_ttl_seconds: 3600

        storage:
          path: .easy-agent
          database: state.db

        security:
          sandbox:
            mode: auto
            working_root: .
        """
    ).lstrip()


def _research_agent_template_config() -> str:
    return dedent(
        """
        model:
          provider: mock
          protocol: mock
          model: mock-agent
          base_url: mock://local
          api_key_env: EASY_AGENT_MOCK_API_KEY

        graph:
          name: research_agent
          entrypoint: researcher
          agents:
            - name: researcher
              description: Source-first research assistant.
              system_prompt: |
                You are a source-first research assistant. For mock smoke runs, call python_echo once.
                For live research, prefer official_source_search with preferred official domains before using general sources.
              tools:
                - python_echo
                - official_source_search
              max_iterations: 5
          nodes: []

        skills:
          - path: skills/examples

        storage:
          path: .easy-agent
          database: state.db

        security:
          sandbox:
            mode: auto
            working_root: .
        """
    ).lstrip()


def _data_agent_template_config() -> str:
    return dedent(
        """
        model:
          provider: mock
          protocol: mock
          model: mock-agent
          base_url: mock://local
          api_key_env: EASY_AGENT_MOCK_API_KEY

        graph:
          name: data_agent
          entrypoint: analyst
          agents:
            - name: analyst
              description: Data assistant for CSV, JSON, logs, metrics, and lightweight summaries.
              system_prompt: |
                You are a careful data assistant. For mock smoke runs, call python_echo once.
                For real work, inspect available evidence, summarize assumptions, and separate findings from recommendations.
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
          sandbox:
            mode: auto
            working_root: .
        """
    ).lstrip()


def _ops_agent_template_config() -> str:
    return dedent(
        """
        model:
          provider: mock
          protocol: mock
          model: mock-agent
          base_url: mock://local
          api_key_env: EASY_AGENT_MOCK_API_KEY

        graph:
          name: ops_agent
          entrypoint: operator
          agents:
            - name: operator
              description: Ops assistant for diagnostics, runbooks, incident notes, and release checks.
              system_prompt: |
                You are a cautious operations assistant. For mock smoke runs, call python_echo once.
                For real work, prefer read-only diagnostics, list risk before action, and request approval before sensitive changes.
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
          sandbox:
            mode: auto
            working_root: .
        """
    ).lstrip()


def _browser_agent_template_config() -> str:
    return dedent(
        """
        model:
          provider: mock
          protocol: mock
          model: mock-agent
          base_url: mock://local
          api_key_env: EASY_AGENT_MOCK_API_KEY

        graph:
          name: browser_agent
          entrypoint: browser_planner
          agents:
            - name: browser_planner
              description: Browser task planner for research, QA, and workflow automation.
              system_prompt: |
                You are a browser-task planning assistant. For mock smoke runs, call python_echo once.
                For real browser work, produce a clear navigation plan, expected evidence, and safety checks before connector execution.
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
          sandbox:
            mode: auto
            working_root: .
        """
    ).lstrip()


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
    raw_checks = payload.get('checks')
    if isinstance(raw_checks, list):
        checks = [item for item in raw_checks if isinstance(item, dict)]
        table.add_row('Checks', json.dumps(_check_summary(checks), ensure_ascii=False))
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


def _diagnostic_checks(config: AppConfig, config_path: Path) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    checks.append(_check_python_version())
    checks.append(_check_path('config_file', config_path, required=True))
    checks.append(_check_storage(config))
    checks.extend(_env_checks(config))
    checks.extend(_tool_checks(config))
    checks.extend(_mcp_checks(config))
    checks.extend(_federation_checks(config))
    checks.extend(_workbench_checks(config))
    checks.extend(_human_loop_checks(config))
    checks.extend(_evaluation_checks(config))
    return checks


def _check_python_version() -> dict[str, str]:
    version = f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'
    if sys.version_info[:2] == (3, 12):
        return _check('ok', 'python.version', f'Python {version} matches the project baseline.', 'No action needed.')
    return _check('warn', 'python.version', f'Python {version} differs from the 3.12 baseline.', 'Use uv venv --python 3.12.')


def _check_path(name: str, path: Path, *, required: bool) -> dict[str, str]:
    if path.exists():
        return _check('ok', name, f'{path} exists.', 'No action needed.')
    status = 'error' if required else 'warn'
    return _check(status, name, f'{path} does not exist.', f'Create {path} or update the config.')


def _check_storage(config: AppConfig) -> dict[str, str]:
    storage_path = Path(config.storage.path)
    if storage_path.is_absolute():
        return _check('warn', 'storage.path', 'Storage uses an absolute path.', 'Prefer a project-relative storage path for portable configs.')
    parent = storage_path.parent if storage_path.parent != Path('.') else Path.cwd()
    if parent.exists():
        return _check('ok', 'storage.path', f'Storage parent {parent} is available.', 'No action needed.')
    return _check('warn', 'storage.path', f'Storage parent {parent} does not exist yet.', 'It will be created when the runtime opens storage.')


def _env_checks(config: AppConfig) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    for name in _required_env_vars(config):
        if os.environ.get(name):
            checks.append(_check('ok', f'env.{name}', f'{name} is present.', 'No action needed.'))
        else:
            checks.append(_check('warn', f'env.{name}', f'{name} is not set.', f'Set {name} before using the related live feature.'))
    return checks


def _tool_checks(config: AppConfig) -> list[dict[str, str]]:
    commands = {'uv'}
    if any(server.transport == 'stdio' and server.command for server in config.mcp):
        commands.update(str(server.command[0]) for server in config.mcp if server.transport == 'stdio' and server.command)
    for executor in config.executors:
        if executor.kind == 'container' and executor.container:
            commands.add(executor.container.executable)
        if executor.kind == 'microvm' and executor.microvm:
            commands.add(executor.microvm.executable)
            commands.update({'ssh', 'scp'})
    return [
        _check(
            'ok' if shutil.which(command) else 'warn',
            f'tool.{command}',
            f'{command} is {"available" if shutil.which(command) else "not available on PATH"}.',
            'No action needed.' if shutil.which(command) else f'Install {command} or adjust the related config before using that feature.',
        )
        for command in sorted(commands)
        if command
    ]


def _mcp_checks(config: AppConfig) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    for server in config.mcp:
        if server.transport == 'stdio' and not server.roots:
            checks.append(_check('warn', f'mcp.{server.name}.roots', 'stdio MCP server has no explicit roots.', 'Declare mcp.roots to make filesystem boundaries clear.'))
        if server.transport == 'streamable_http' and server.auth.type.value == 'none':
            checks.append(_check('warn', f'mcp.{server.name}.auth', 'streamable_http MCP server has no configured auth.', 'Use bearer_env, header_env, or an approved OAuth flow for remote servers.'))
        if server.transport in {'http_sse', 'streamable_http'} and not (server.url or server.rpc_url or server.sse_url):
            checks.append(_check('error', f'mcp.{server.name}.url', 'Remote MCP transport is missing a URL.', 'Set url, rpc_url, or sse_url.'))
    if not checks:
        checks.append(_check('ok', 'mcp', 'No MCP risks detected from static config.', 'No action needed.'))
    return checks


def _federation_checks(config: AppConfig) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    for remote in config.federation.remotes:
        if remote.auth.type.value == 'none':
            checks.append(_check('warn', f'federation.remote.{remote.name}.auth', 'Federation remote has no auth configured.', 'Use bearer_env, header_env, OAuth/OIDC, or mTLS for non-local remotes.'))
    if config.federation.server.enabled and not config.federation.server.security_schemes:
        checks.append(_check('warn', 'federation.server.security', 'Federation server is enabled without security schemes.', 'Add security_schemes before exposing the server outside localhost.'))
    if not checks:
        checks.append(_check('ok', 'federation', 'No federation risks detected from static config.', 'No action needed.'))
    return checks


def _workbench_checks(config: AppConfig) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    if not config.workbench.enabled:
        return [_check('warn', 'workbench.enabled', 'Workbench is disabled.', 'Enable it when commands, MCP, or skills need isolated per-run roots.')]
    for executor in config.executors:
        if executor.kind == 'process':
            checks.append(_check('ok', f'executor.{executor.name}', 'Process executor is available for trusted local work.', 'Use container or microVM for stronger isolation.'))
        elif executor.kind == 'container' and executor.container:
            checks.append(_check('ok' if shutil.which(executor.container.executable) else 'warn', f'executor.{executor.name}', f'Container executor uses {executor.container.executable}.', 'Ensure the executable and image are available before live runs.'))
        elif executor.kind == 'microvm' and executor.microvm:
            checks.append(_check('ok' if shutil.which(executor.microvm.executable) else 'warn', f'executor.{executor.name}', f'MicroVM executor uses {executor.microvm.executable}.', 'Ensure the executable, image, ssh, and scp are available before live runs.'))
    return checks


def _human_loop_checks(config: AppConfig) -> list[dict[str, str]]:
    human_loop = config.security.human_loop
    if human_loop.mode.value == 'deferred' and not human_loop.sensitive_tools:
        return [_check('warn', 'human_loop.sensitive_tools', 'Deferred human loop has no sensitive tools configured.', 'Add sensitive_tools when approvals should gate risky actions.')]
    if human_loop.sensitive_tools:
        return [_check('ok', 'human_loop.sensitive_tools', 'Sensitive tools are configured for approval-aware runs.', 'No action needed.')]
    return [_check('ok', 'human_loop', 'Human loop config is present.', 'No action needed.')]


def _evaluation_checks(config: AppConfig) -> list[dict[str, str]]:
    public_eval = config.evaluation.public_eval
    search_profiles = {'browsecomp_subset', 'simpleqa_subset', 'simple_evals_subset'}
    if public_eval.web_search.provider == 'serpapi' and public_eval.profile in search_profiles and not os.environ.get(public_eval.web_search.api_key_env):
        return [_check('warn', 'evaluation.web_search', f'{public_eval.web_search.api_key_env} is not set.', 'Set it before live web-search evals, or use replay-only eval data.')]
    return [_check('ok', 'evaluation', 'No evaluation credential risks detected from static config.', 'No action needed.')]


def _check(status: str, name: str, message: str, action: str) -> dict[str, str]:
    return {'status': status, 'check': name, 'message': message, 'action': action}


def _check_summary(checks: list[dict[str, Any]]) -> dict[str, int]:
    return {
        'ok': sum(1 for check in checks if check['status'] == 'ok'),
        'warn': sum(1 for check in checks if check['status'] == 'warn'),
        'error': sum(1 for check in checks if check['status'] == 'error'),
    }


def _overall_status(checks: list[dict[str, str]]) -> str:
    summary = _check_summary(checks)
    if summary['error']:
        return 'error'
    if summary['warn']:
        return 'warn'
    return 'ok'


def _entrypoint_type(config: AppConfig) -> str:
    if config.graph.nodes:
        return 'graph'
    if config.graph.entrypoint in config.agent_map:
        return 'agent'
    if config.graph.entrypoint in config.team_map:
        return 'team'
    return 'unknown'


def _required_env_vars(config: AppConfig) -> list[str]:
    names: set[str | None] = set()
    if config.model.provider != 'mock':
        names.add(config.model.api_key_env)
    public_eval = config.evaluation.public_eval
    search_profiles = {'browsecomp_subset', 'simpleqa_subset', 'simple_evals_subset'}
    if public_eval.web_search.provider == 'serpapi' and public_eval.profile in search_profiles:
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
