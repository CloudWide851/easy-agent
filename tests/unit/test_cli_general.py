from __future__ import annotations

import asyncio
import json
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
    open_path = tmp_path / 'trace-open.html'
    open_result = runner.invoke(app, ['traces', 'open', 'run_cli', '-c', str(config_path), '--output', str(open_path), '--no-browser'])
    invalid_html_result = runner.invoke(app, ['traces', 'export', 'run_cli', '-c', str(config_path), '--raw', '--html', '--output', str(html_path)])
    otel_path = tmp_path / 'trace-otel.json'
    otel_result = runner.invoke(app, ['traces', 'export', 'run_cli', '-c', str(config_path), '--otel-json', '--output', str(otel_path)])

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
    assert open_result.exit_code == 0
    assert open_path.exists()
    assert '"opened": false' in open_result.output
    assert otel_result.exit_code == 0
    assert otel_path.exists()
    assert '"experimental": true' in otel_result.output
    assert '"resource_spans"' in otel_path.read_text(encoding='utf-8')
    assert invalid_html_result.exit_code != 0


def test_report_latest_summarizes_artifacts_and_runs(tmp_path: Path) -> None:
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
        runtime.store.create_run('run_report', 'baseline', {'input': 'hello'})
        runtime.store.finish_run('run_report', RunStatus.SUCCEEDED.value, {'output': 'done'})
    finally:
        asyncio.run(runtime.aclose())

    benchmark = tmp_path / 'benchmark.json'
    benchmark.write_text(
        json.dumps({'summary': {'single_agent': {'runs': 2, 'successes': 2, 'failures': 0}}}),
        encoding='utf-8',
    )
    public_eval = tmp_path / 'public-eval.json'
    public_eval.write_text(
        json.dumps(
            {
                'profile': 'full_v4',
                'case_counts': {'completed_records': 3},
                'summary': {'overall': {'bfcl_subcategory_accuracy': 1.0}},
            }
        ),
        encoding='utf-8',
    )
    real_network = tmp_path / 'real-network.json'
    real_network.write_text(
        json.dumps({'summary': {'runs': 4, 'passed': 3, 'failed': 0, 'skipped': 1}, 'generated_at': '2026-04-29T00:00:00Z'}),
        encoding='utf-8',
    )

    result = CliRunner().invoke(
        app,
        [
            'report',
            'latest',
            '-c',
            str(config_path),
            '--format',
            'json',
            '--benchmark-report',
            str(benchmark),
            '--public-eval-report',
            str(public_eval),
            '--real-network-report',
            str(real_network),
        ],
    )
    html_path = tmp_path / 'latest-report.html'
    html_result = CliRunner().invoke(
        app,
        [
            'report',
            'latest',
            '-c',
            str(config_path),
            '--html',
            '--output',
            str(html_path),
            '--benchmark-report',
            str(benchmark),
            '--public-eval-report',
            str(public_eval),
            '--real-network-report',
            str(real_network),
        ],
    )

    assert result.exit_code == 0
    assert '"benchmark"' in result.output
    assert '"score": 100.0' in result.output
    assert '"score": 75.0' in result.output
    assert '"latest_run_id": "run_report"' in result.output
    assert html_result.exit_code == 0
    assert html_path.exists()
    html = html_path.read_text(encoding='utf-8')
    assert '<!doctype html>' in html
    assert 'easy-agent latest report' in html
    assert 'public_eval' in html
    assert 'run_report' in html


def test_report_trend_summarizes_history(tmp_path: Path) -> None:
    benchmark = tmp_path / 'benchmark-report.json'
    benchmark.write_text(
        json.dumps({'summary': {'single_agent': {'runs': 2, 'successes': 2, 'failures': 0}}}),
        encoding='utf-8',
    )
    real_network = tmp_path / 'real-network-report.json'
    real_network.write_text(
        json.dumps({'summary': {'runs': 4, 'passed': 3, 'failed': 0, 'skipped': 1}}),
        encoding='utf-8',
    )
    html_path = tmp_path / 'trend.html'

    result = CliRunner().invoke(app, ['report', 'trend', '--history', str(tmp_path), '--format', 'json'])
    html_result = CliRunner().invoke(app, ['report', 'trend', '--history', str(tmp_path), '--html', '--output', str(html_path)])

    assert result.exit_code == 0
    assert '"benchmark"' in result.output
    assert '"score": 100.0' in result.output
    assert '"score": 75.0' in result.output
    assert html_result.exit_code == 0
    assert html_path.exists()
    assert 'easy-agent report trend' in html_path.read_text(encoding='utf-8')


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


def test_runs_fix_outputs_advice_only_package(tmp_path: Path) -> None:
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
        runtime.store.create_run('run_fix', 'baseline', {'input': 'hello'})
        runtime.store.record_event('run_fix', 'tool_validation_failed', {'error': 'bad args'})
        runtime.store.finish_run('run_fix', RunStatus.FAILED.value, {'error': 'bad args'})
    finally:
        asyncio.run(runtime.aclose())

    markdown_path = tmp_path / 'nested' / 'fix.md'
    html_path = tmp_path / 'nested' / 'fix.html'
    json_result = CliRunner().invoke(app, ['runs', 'fix', 'run_fix', '-c', str(config_path), '--format', 'json'])
    markdown_result = CliRunner().invoke(
        app,
        ['runs', 'fix', 'run_fix', '-c', str(config_path), '--format', 'markdown', '--output', str(markdown_path)],
    )
    html_result = CliRunner().invoke(
        app,
        ['runs', 'fix', 'run_fix', '-c', str(config_path), '--format', 'html', '--output', str(html_path)],
    )

    assert json_result.exit_code == 0
    assert '"mode": "advice_only"' in json_result.output
    assert '"selected_task_pack": "bug-fix"' in json_result.output
    triage_result = CliRunner().invoke(app, ['runs', 'triage', 'run_fix', '-c', str(config_path), '--format', 'json'])
    assert triage_result.exit_code == 0
    assert '"mode": "advice_only"' in triage_result.output
    assert '"severity": "high"' in triage_result.output
    assert '"can_retry": true' in triage_result.output
    inspect_result = CliRunner().invoke(app, ['runs', 'inspect', 'run_fix', '-c', str(config_path), '--format', 'json'])
    assert inspect_result.exit_code == 0
    assert '"fix_summary"' in inspect_result.output
    assert '"bundle_command"' in inspect_result.output
    assert markdown_result.exit_code == 0
    assert markdown_path.exists()
    assert '# easy-agent run fix: run_fix' in markdown_path.read_text(encoding='utf-8')
    assert html_result.exit_code == 0
    assert html_path.exists()
    html = html_path.read_text(encoding='utf-8')
    assert '<!doctype html>' in html
    assert 'Recommended Commands' in html
    assert 'Task Prompt' in html

    bundle_path = tmp_path / 'bundle'
    bundle_result = CliRunner().invoke(
        app,
        ['runs', 'bundle', 'run_fix', '-c', str(config_path), '--output', str(bundle_path), '--format', 'json'],
    )
    assert bundle_result.exit_code == 0
    assert '"mode": "advice_only"' in bundle_result.output
    assert (bundle_path / 'run-summary.json').exists()
    assert (bundle_path / 'triage.json').exists()
    assert (bundle_path / 'fix.md').exists()
    assert (bundle_path / 'fix.html').exists()
    assert (bundle_path / 'trace-tree.json').exists()
    assert (bundle_path / 'trace.html').exists()
    assert (bundle_path / 'browser-artifacts.json').exists()
    assert 'advice-only evidence package' in (bundle_path / 'README.md').read_text(encoding='utf-8')


def test_runs_fix_classifies_browser_mcp_failure(tmp_path: Path) -> None:
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
        runtime.store.create_run('run_browser_fix', 'baseline', {'input': 'hello'})
        runtime.store.record_event('run_browser_fix', 'tool_call_failed', {'tool': 'browser_navigate', 'error': 'Playwright MCP npx failed'})
        runtime.store.finish_run('run_browser_fix', RunStatus.FAILED.value, {'error': 'browser_artifacts missing'})
    finally:
        asyncio.run(runtime.aclose())

    result = CliRunner().invoke(app, ['runs', 'fix', 'run_browser_fix', '-c', str(config_path), '--format', 'json'])

    assert result.exit_code == 0
    assert '"likely_layer": "browser_mcp"' in result.output
    assert '"selected_task_pack": "browser-qa"' in result.output
    assert 'easy-agent browser doctor' in result.output


def test_dashboard_writes_static_html(tmp_path: Path) -> None:
    config_path = tmp_path / 'easy-agent.yml'
    storage_path = str(tmp_path / 'state').replace('\\', '/')
    config_path.write_text(
        f"""
model:
  provider: mock
  protocol: mock
  model: mock-agent
  base_url: mock://local
  api_key_env: EASY_AGENT_MOCK_API_KEY
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
        runtime.store.create_run('run_dash', 'baseline', {'input': 'hello'})
        runtime.store.finish_run('run_dash', RunStatus.SUCCEEDED.value, {'output': 'done'})
        runtime.store.create_run('run_failed_dash', 'baseline', {'input': 'bad'})
        runtime.store.finish_run('run_failed_dash', RunStatus.FAILED.value, {'error': 'bad'})
    finally:
        asyncio.run(runtime.aclose())

    output_path = tmp_path / 'dashboard.html'
    result = CliRunner().invoke(
        app,
        ['dashboard', '-c', str(config_path), '--history', str(tmp_path), '--output', str(output_path), '--format', 'json'],
    )

    assert result.exit_code == 0
    assert output_path.exists()
    assert '"run_count": 2' in result.output
    html = output_path.read_text(encoding='utf-8')
    assert '<!doctype html>' in html
    assert 'easy-agent dashboard' in html
    assert 'run_dash' in html
    assert 'Needs Attention' in html
    assert 'Approvals' in html
    assert 'Suggested Next Steps' in html
    assert 'Workflow Recommendations' in html
    assert 'Template Recommendations' in html
    assert 'Browser' in html
    assert 'run_failed_dash' in html
    assert 'runs inspect run_failed_dash' in html
    assert 'runs bundle run_failed_dash' in html
    assert 'runs fix run_failed_dash' in html
    assert 'workflow init bug-fix' in html
