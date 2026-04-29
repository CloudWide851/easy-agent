from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from agent_cli.app import app
from agent_cli.commands.general import _doctor_rows, _entrypoint_type, _mcp_transport_summary
from agent_common.models import RunStatus
from agent_common.version import runtime_version
from agent_config.app import AppConfig, ModelConfig
from agent_integrations.sandbox import SandboxMode
from agent_runtime import build_runtime


class FakeWorkbenchManager:
    def describe(self) -> dict[str, object]:
        return {
            'base_root': 'H:/easy-agent/.easy-agent/workbench',
            'default_executor': 'process',
            'session_ttl_seconds': 3600,
            'active_sessions': 0,
            'executors': {
                'process': {'available': True},
                'containerized': {'available': False},
            },
        }


class FakeSandboxManager:
    def describe(self) -> dict[str, object]:
        return {
            'mode': SandboxMode.AUTO.value,
            'targets': ['command_skill', 'stdio_mcp'],
            'windows_sandbox_available': False,
            'windows_sandbox_fallback': SandboxMode.PROCESS.value,
        }


def _runtime_from_config(config: AppConfig) -> SimpleNamespace:
    store = SimpleNamespace(base_path=SimpleNamespace(resolve=lambda: 'H:/easy-agent/.easy-agent'))
    return SimpleNamespace(
        config=config,
        skills=[SimpleNamespace(name='python_echo')],
        loaded_sources=['InlineRuntimePlugin'],
        sandbox_manager=FakeSandboxManager(),
        workbench_manager=FakeWorkbenchManager(),
        store=store,
    )


def test_entrypoint_type_reports_graph_when_nodes_exist() -> None:
    config = AppConfig.model_validate(
        {
            'graph': {
                'entrypoint': 'aggregate',
                'agents': [{'name': 'worker'}],
                'teams': [],
                'nodes': [{'id': 'aggregate', 'type': 'join'}],
            }
        }
    )

    runtime = _runtime_from_config(config)

    assert _entrypoint_type(runtime) == 'graph'



def test_mcp_transport_summary_lists_configured_servers() -> None:
    config = AppConfig.model_validate(
        {
            'graph': {
                'entrypoint': 'agent_a',
                'agents': [{'name': 'agent_a'}],
                'teams': [],
                'nodes': [],
            },
            'mcp': [
                {'name': 'filesystem', 'transport': 'stdio'},
                {'name': 'remote_tools', 'transport': 'http_sse', 'rpc_url': 'https://example.test/rpc'},
            ],
        }
    )

    runtime = _runtime_from_config(config)

    assert _mcp_transport_summary(runtime) == 'filesystem:stdio, remote_tools:http_sse'



def test_doctor_rows_include_runtime_stack_details() -> None:
    config = AppConfig.model_validate(
        {
            'model': ModelConfig(provider='deepseek', model='deepseek-chat').model_dump(),
            'graph': {
                'entrypoint': 'writer_team',
                'agents': [
                    {'name': 'planner', 'description': 'Plans work.'},
                    {'name': 'closer', 'description': 'Closes work.'},
                    {'name': 'evaluator', 'description': 'Evaluates work.'},
                ],
                'teams': [
                    {'name': 'writer_team', 'mode': 'round_robin', 'members': ['planner', 'closer']}
                ],
                'nodes': [],
            },
            'harnesses': [
                {
                    'name': 'delivery_loop',
                    'initializer_agent': 'planner',
                    'worker_target': 'writer_team',
                    'evaluator_agent': 'evaluator',
                    'completion_contract': 'Finish the work.',
                    'artifacts_dir': '.easy-agent/harness',
                }
            ],
            'mcp': [{'name': 'filesystem', 'transport': 'stdio'}],
        }
    )

    runtime = _runtime_from_config(config)
    rows = dict(_doctor_rows(runtime))

    assert rows['Provider'] == 'deepseek'
    assert rows['Model'] == 'deepseek-chat'
    assert rows['Runtime Version'] == runtime_version()
    assert rows['Entrypoint'] == 'writer_team'
    assert rows['Entrypoint Type'] == 'team'
    assert rows['Harnesses'] == '1'
    assert rows['Configured MCP Servers'] == '1'
    assert rows['MCP Transports'] == 'filesystem:stdio'
    assert rows['Federation Remotes'] == '0'
    assert rows['Federation Exports'] == '0'
    assert rows['Federation Push'] == 'polling, webhook_subscribe, sse_events'
    assert rows['Configured Executors'] == '2'
    assert rows['Executor Availability'] == 'process:yes, containerized:no'
    assert rows['Tool Guardrails'] == 'block_shell_metacharacters'
    assert rows['Output Guardrails'] == 'require_non_empty_output, block_secret_leaks'
    assert rows['Event Stream'] == 'True'
    assert rows['Sandbox Fallback'] == 'process'


def test_runs_and_traces_cli_export_storage_records(tmp_path: Path) -> None:
    config_path = tmp_path / 'easy-agent.yml'
    storage_path = str(tmp_path / 'state').replace('\\', '/')
    config_path.write_text(
        f"""
graph:
  entrypoint: agent_a
  agents:
    - name: agent_a
storage:
  path: {storage_path}
  database: state.db
""",
        encoding='utf-8',
    )
    runtime = build_runtime(config_path)
    try:
        runtime.store.create_run('run_cli', 'baseline', {'input': 'hello'})
        runtime.store.record_event('run_cli', 'run_started', {'input': 'hello'}, span_id='run:run_cli')
    finally:
        asyncio.run(runtime.aclose())

    runner = CliRunner()
    list_result = runner.invoke(app, ['runs', 'list', '-c', str(config_path)])
    show_result = runner.invoke(app, ['runs', 'show', 'run_cli', '-c', str(config_path)])
    trace_result = runner.invoke(app, ['traces', 'export', 'run_cli', '-c', str(config_path)])
    html_path = tmp_path / 'trace.html'
    html_result = runner.invoke(app, ['traces', 'export', 'run_cli', '-c', str(config_path), '--html', '--output', str(html_path)])
    invalid_html_result = runner.invoke(app, ['traces', 'export', 'run_cli', '-c', str(config_path), '--raw', '--html', '--output', str(html_path)])

    assert list_result.exit_code == 0
    assert 'run_cli' in list_result.output
    assert show_result.exit_code == 0
    assert '"run_id": "run_cli"' in show_result.output
    assert trace_result.exit_code == 0
    assert '"tree"' in trace_result.output
    assert html_result.exit_code == 0
    assert html_path.exists()
    html = html_path.read_text(encoding='utf-8')
    assert '<!doctype html>' in html
    assert 'run_cli' in html
    assert 'trace-search' in html
    assert 'data-filter="error"' in html
    assert 'summary-card' in html
    assert invalid_html_result.exit_code != 0


def test_runs_explain_reports_success(tmp_path: Path) -> None:
    config_path = tmp_path / 'easy-agent.yml'
    storage_path = str(tmp_path / 'state').replace('\\', '/')
    config_path.write_text(
        f"""
graph:
  entrypoint: agent_a
  agents:
    - name: agent_a
storage:
  path: {storage_path}
  database: state.db
""",
        encoding='utf-8',
    )
    runtime = build_runtime(config_path)
    try:
        runtime.store.create_run('run_success', 'baseline', {'input': 'hello'})
        runtime.store.finish_run('run_success', RunStatus.SUCCEEDED.value, {'output': 'done'})
        result = CliRunner().invoke(app, ['runs', 'explain', 'run_success', '-c', str(config_path), '--format', 'json'])
    finally:
        asyncio.run(runtime.aclose())

    assert result.exit_code == 0
    assert '"likely_layer": "success"' in result.output
    assert '"headline": "Run completed successfully."' in result.output


def test_runs_explain_reports_missing_api_key(tmp_path: Path) -> None:
    config_path = tmp_path / 'easy-agent.yml'
    storage_path = str(tmp_path / 'state').replace('\\', '/')
    config_path.write_text(
        f"""
graph:
  entrypoint: agent_a
  agents:
    - name: agent_a
storage:
  path: {storage_path}
  database: state.db
""",
        encoding='utf-8',
    )
    runtime = build_runtime(config_path)
    try:
        runtime.store.create_run('run_failure', 'baseline', {'input': 'hello'})
        runtime.store.record_event(
            'run_failure',
            'run_failed',
            {'error': 'Missing API key environment variable: DEEPSEEK_API_KEY'},
        )
        runtime.store.finish_run(
            'run_failure',
            RunStatus.FAILED.value,
            {'error': 'Missing API key environment variable: DEEPSEEK_API_KEY'},
        )
        result = CliRunner().invoke(app, ['runs', 'explain', 'run_failure', '-c', str(config_path), '--format', 'json'])
    finally:
        asyncio.run(runtime.aclose())

    assert result.exit_code == 0
    assert '"likely_layer": "model_provider"' in result.output

