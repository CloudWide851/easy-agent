from __future__ import annotations

import asyncio
from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch
from typer.testing import CliRunner

from agent_cli.app import app
from agent_common.models import RunStatus
from agent_runtime import build_runtime


def _mock_config(path: Path) -> None:
    storage_path = str(path.parent / 'state').replace('\\', '/')
    path.write_text(
        f"""
model:
  provider: mock
  protocol: mock
  model: mock-agent
  base_url: mock://local
  api_key_env: EASY_AGENT_MOCK_API_KEY
graph:
  entrypoint: assistant
  agents:
    - name: assistant
      tools:
        - python_echo
      max_iterations: 3
skills:
  - path: skills/examples
storage:
  path: {storage_path}
  database: state.db
""",
        encoding='utf-8',
    )


def test_connectors_doctor_and_test(tmp_path: Path) -> None:
    config = tmp_path / 'easy-agent.yml'
    _mock_config(config)

    doctor = CliRunner().invoke(app, ['connectors', 'doctor', '-c', str(config), '--format', 'json'])
    tested = CliRunner().invoke(app, ['connectors', 'test', 'model', '-c', str(config), '--format', 'json'])

    assert doctor.exit_code == 0
    assert '"model"' in doctor.output
    assert '"browser"' in doctor.output
    assert tested.exit_code == 0
    assert '"status": "ok"' in tested.output


def test_browser_connector_reports_disabled_and_playwright_ready(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    config = tmp_path / 'easy-agent.yml'
    _mock_config(config)

    disabled = CliRunner().invoke(app, ['connectors', 'test', 'browser', '-c', str(config), '--format', 'json'])

    assert disabled.exit_code == 0
    assert '"status": "warn"' in disabled.output
    assert 'No first-class browser connector is configured' in disabled.output

    config.write_text(
        config.read_text(encoding='utf-8')
        + """
browser:
  enabled: true
  provider: playwright_mcp
  server_name: playwright
  require_approval: true
""",
        encoding='utf-8',
    )
    monkeypatch.setattr('agent_runtime.connectors.shutil.which', lambda command: 'npx.cmd' if command == 'npx' else None)

    enabled = CliRunner().invoke(app, ['connectors', 'test', 'browser', '-c', str(config), '--format', 'json'])

    assert enabled.exit_code == 0
    assert '"status": "ok"' in enabled.output
    assert 'Playwright MCP is configured as mcp:playwright' in enabled.output

    artifact_root = tmp_path / 'browser-artifacts'
    artifact_root.mkdir()
    (artifact_root / 'snapshot.json').write_text('{"ok": true}', encoding='utf-8')
    (artifact_root / 'screen.png').write_bytes(b'png')
    artifact_dir = str(artifact_root).replace('\\', '/')
    config.write_text(
        config.read_text(encoding='utf-8')
        + f"""
browser:
  enabled: true
  provider: playwright_mcp
  server_name: playwright
  artifacts_dir: {artifact_dir}
  require_approval: true
""",
        encoding='utf-8',
    )

    doctor = CliRunner().invoke(app, ['browser', 'doctor', '-c', str(config), '--format', 'json'])
    artifacts = CliRunner().invoke(app, ['browser', 'artifacts', '-c', str(config), '--format', 'json'])

    assert doctor.exit_code == 0
    assert '"server_name": "playwright"' in doctor.output
    assert '"npx_available": true' in doctor.output
    assert artifacts.exit_code == 0
    assert '"count": 2' in artifacts.output
    assert '"snapshot"' in artifacts.output


def test_task_pack_show_dry_run_and_run(tmp_path: Path) -> None:
    config = tmp_path / 'easy-agent.yml'
    _mock_config(config)

    listed = CliRunner().invoke(app, ['task', 'list'])
    shown = CliRunner().invoke(app, ['task', 'show', 'repo-review', '--format', 'json'])
    browser_task = CliRunner().invoke(app, ['task', 'show', 'browser-qa', '--format', 'json'])
    dry = CliRunner().invoke(app, ['task', 'run', 'repo-review', '-c', str(config), '--dry-run', '--context', 'focus tests'])
    run = CliRunner().invoke(app, ['task', 'run', 'repo-review', '-c', str(config), '--context', 'focus tests'])

    assert listed.exit_code == 0
    assert 'repo-review' in listed.output
    assert 'browser-qa' in listed.output
    assert shown.exit_code == 0
    assert '"acceptance_criteria"' in shown.output
    assert browser_task.exit_code == 0
    assert '"recommended_scenario": "browser-agent"' in browser_task.output
    assert dry.exit_code == 0
    assert 'focus tests' in dry.output
    assert run.exit_code == 0
    assert '"status": "succeeded"' in run.output


def test_workflow_run_and_browser_helpers(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    config = tmp_path / 'easy-agent.yml'
    _mock_config(config)
    browser_config = tmp_path / 'easy-agent-browser.yml'
    _mock_config(browser_config)
    browser_config.write_text(
        browser_config.read_text(encoding='utf-8')
        + """
browser:
  enabled: true
  provider: playwright_mcp
  server_name: playwright
  require_approval: true
""",
        encoding='utf-8',
    )
    monkeypatch.setattr('agent_runtime.connectors.shutil.which', lambda command: 'npx.cmd' if command == 'npx' else None)

    listed = CliRunner().invoke(app, ['workflow', 'list', '--format', 'json'])
    workflow_file = tmp_path / 'workflow.yml'
    initialized = CliRunner().invoke(
        app,
        ['workflow', 'init', 'browser-audit', '--output', str(workflow_file), '--context', 'audit home', '--bundle-on-completion', '--format', 'json'],
    )
    doctor = CliRunner().invoke(app, ['workflow', 'doctor', str(workflow_file), '-c', str(browser_config), '--format', 'json'])
    plan = CliRunner().invoke(app, ['workflow', 'plan', str(workflow_file), '-c', str(browser_config), '--format', 'json'])
    file_dry = CliRunner().invoke(app, ['workflow', 'run', str(workflow_file), '-c', str(browser_config), '--dry-run', '--format', 'json'])
    dry = CliRunner().invoke(app, ['workflow', 'run', 'browser-qa', '-c', str(browser_config), '--dry-run', '--context', 'home page', '--format', 'json'])
    run = CliRunner().invoke(app, ['workflow', 'run', 'repo-review', '-c', str(config), '--context', 'focus tests', '--format', 'json'])
    smoke = CliRunner().invoke(app, ['browser', 'smoke', 'https://example.com', '-c', str(browser_config), '--format', 'json'])
    snapshot = CliRunner().invoke(app, ['browser', 'snapshot', 'https://example.com', '-c', str(browser_config), '--format', 'json'])
    audit = CliRunner().invoke(app, ['browser', 'audit', 'https://example.com', '-c', str(browser_config), '--format', 'json'])
    seo = CliRunner().invoke(app, ['browser', 'seo', 'https://example.com', '-c', str(browser_config), '--format', 'json'])
    a11y = CliRunner().invoke(app, ['browser', 'a11y', 'https://example.com', '-c', str(browser_config), '--format', 'json'])
    links = CliRunner().invoke(app, ['browser', 'links', 'https://example.com', '-c', str(browser_config), '--format', 'json'])

    runtime = build_runtime(browser_config)
    try:
        runtime.store.create_run('run_browser_report', 'baseline', {'input': 'hello'})
        runtime.store.record_event('run_browser_report', 'tool_call_failed', {'tool': 'browser_snapshot', 'error': 'Playwright MCP failed'})
        runtime.store.finish_run('run_browser_report', RunStatus.FAILED.value, {'error': 'browser snapshot failed'})
    finally:
        asyncio.run(runtime.aclose())
    report = CliRunner().invoke(app, ['browser', 'report', 'run_browser_report', '-c', str(browser_config), '--format', 'json'])

    assert listed.exit_code == 0
    assert '"workflows"' in listed.output
    assert initialized.exit_code == 0
    assert workflow_file.exists()
    assert 'browser-audit' in workflow_file.read_text(encoding='utf-8')
    assert doctor.exit_code == 0
    assert '"status": "ok"' in doctor.output
    assert plan.exit_code == 0
    assert '"doctor"' in plan.output
    assert '"pack": "browser-audit"' in plan.output
    assert file_dry.exit_code == 0
    assert '"pack": "browser-audit"' in file_dry.output
    assert 'title, meta description' in file_dry.output
    assert dry.exit_code == 0
    assert '"pack": "browser-qa"' in dry.output
    assert 'easy-agent browser doctor' in dry.output
    assert run.exit_code == 0
    assert '"status": "succeeded"' in run.output
    assert smoke.exit_code == 0
    assert '"mode": "plan_only"' in smoke.output
    assert 'https://example.com' in smoke.output
    assert snapshot.exit_code == 0
    assert 'accessibility-tree' in snapshot.output
    assert audit.exit_code == 0
    assert '"pack": "browser-audit"' in audit.output
    assert 'canonical signals' in audit.output
    assert seo.exit_code == 0
    assert 'prioritized SEO fixes' in seo.output
    assert a11y.exit_code == 0
    assert 'accessibility fixes' in a11y.output
    assert links.exit_code == 0
    assert 'link-quality' in links.output
    assert report.exit_code == 0
    assert '"likely_layer": "browser_mcp"' in report.output


def test_skill_catalog_and_plugins_doctor(tmp_path: Path) -> None:
    config = tmp_path / 'easy-agent.yml'
    _mock_config(config)
    target = tmp_path / 'installed'

    catalog = CliRunner().invoke(app, ['skills', 'catalog', 'list', '--format', 'json'])
    installed = CliRunner().invoke(app, ['skills', 'catalog', 'install', 'python_echo', '--target', str(target), '--force'])
    plugins = CliRunner().invoke(app, ['plugins', 'doctor', '-c', str(config), '--format', 'json'])

    assert catalog.exit_code == 0
    assert '"python_echo"' in catalog.output
    assert installed.exit_code == 0
    assert (target / 'python_echo' / 'skill.yaml').exists()
    assert plugins.exit_code == 0
    assert '"checks"' in plugins.output
