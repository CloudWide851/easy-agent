from __future__ import annotations

from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch
from typer.testing import CliRunner

from agent_cli.app import app


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


def test_task_pack_show_dry_run_and_run(tmp_path: Path) -> None:
    config = tmp_path / 'easy-agent.yml'
    _mock_config(config)

    listed = CliRunner().invoke(app, ['task', 'list'])
    shown = CliRunner().invoke(app, ['task', 'show', 'repo-review', '--format', 'json'])
    dry = CliRunner().invoke(app, ['task', 'run', 'repo-review', '-c', str(config), '--dry-run', '--context', 'focus tests'])
    run = CliRunner().invoke(app, ['task', 'run', 'repo-review', '-c', str(config), '--context', 'focus tests'])

    assert listed.exit_code == 0
    assert 'repo-review' in listed.output
    assert shown.exit_code == 0
    assert '"acceptance_criteria"' in shown.output
    assert dry.exit_code == 0
    assert 'focus tests' in dry.output
    assert run.exit_code == 0
    assert '"status": "succeeded"' in run.output


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
