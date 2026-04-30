from __future__ import annotations

from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch
from typer.testing import CliRunner

from agent_cli.app import app
from agent_config.app import load_config


def test_init_creates_mock_config_and_protects_existing_file(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / 'easy-agent.yml'

    first = runner.invoke(app, ['init', '--path', str(config_path), '--provider', 'mock'])
    second = runner.invoke(app, ['init', '--path', str(config_path), '--provider', 'mock'])

    assert first.exit_code == 0
    assert 'Created' in first.output
    assert 'provider: mock' in config_path.read_text(encoding='utf-8')
    assert 'protocol: mock' in config_path.read_text(encoding='utf-8')
    assert second.exit_code != 0
    assert 'already exists' in second.output


def test_template_commands_list_and_create(tmp_path: Path) -> None:
    runner = CliRunner()
    destination = tmp_path / 'starter'

    listed = runner.invoke(app, ['template', 'list'])
    created = runner.invoke(app, ['template', 'create', 'basic-agent', str(destination)])

    assert listed.exit_code == 0
    assert 'basic-agent' in listed.output
    assert 'longrun-harness' in listed.output
    assert 'coding-agent' in listed.output
    assert 'research-agent' in listed.output
    assert 'data-agent' in listed.output
    assert 'ops-agent' in listed.output
    assert 'browser-agent' in listed.output
    assert 'customer-support-agent' in listed.output
    assert 'sales-agent' in listed.output
    assert 'document-agent' in listed.output
    assert 'qa-agent' in listed.output
    assert 'release-agent' in listed.output
    assert created.exit_code == 0
    assert (destination / 'easy-agent.yml').exists()
    assert 'easy-agent config doctor' in (destination / 'README.md').read_text(encoding='utf-8')
    assert 'DEEPSEEK_API_KEY=<SECRET>' in (destination / '.env.local.example').read_text(encoding='utf-8')


def test_all_templates_create_valid_configs(tmp_path: Path) -> None:
    runner = CliRunner()
    templates = [
        'basic-agent',
        'tool-agent',
        'human-approval-agent',
        'longrun-harness',
        'mcp-filesystem-agent',
        'eval-smoke',
        'federation-loopback',
        'workbench-coding-agent',
        'coding-agent',
        'research-agent',
        'data-agent',
        'ops-agent',
        'browser-agent',
        'customer-support-agent',
        'sales-agent',
        'document-agent',
        'qa-agent',
        'release-agent',
    ]

    listed = runner.invoke(app, ['template', 'list'])

    assert listed.exit_code == 0
    for template in templates:
        assert template in listed.output
        destination = tmp_path / template
        result = runner.invoke(app, ['template', 'create', template, str(destination)])
        assert result.exit_code == 0
        load_config(destination / 'easy-agent.yml')
        assert (destination / 'README.md').exists()
        assert (destination / '.env.local.example').exists()


def test_browser_template_enables_playwright_mcp_browser(tmp_path: Path) -> None:
    runner = CliRunner()
    destination = tmp_path / 'browser-starter'

    result = runner.invoke(app, ['template', 'create', 'browser-agent', str(destination)])
    config = load_config(destination / 'easy-agent.yml')

    assert result.exit_code == 0
    assert config.browser.enabled is True
    assert config.browser.provider == 'playwright_mcp'
    assert config.browser.server_name == 'playwright'
    assert config.browser.require_approval is True
    assert 'browser:' in (destination / 'easy-agent.yml').read_text(encoding='utf-8')
    assert 'easy-agent connectors test browser' in (destination / 'README.md').read_text(encoding='utf-8')


def test_new_command_creates_business_scenarios(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    coding = runner.invoke(app, ['new', 'coding-agent'])
    research = runner.invoke(app, ['new', 'research-agent', 'research-starter'])
    data = runner.invoke(app, ['new', 'data-agent'])
    ops = runner.invoke(app, ['new', 'ops-agent'])
    browser = runner.invoke(app, ['new', 'browser-agent', 'browser-starter'])
    release = runner.invoke(app, ['new', 'release-agent'])

    assert coding.exit_code == 0
    assert research.exit_code == 0
    assert data.exit_code == 0
    assert ops.exit_code == 0
    assert browser.exit_code == 0
    assert release.exit_code == 0
    load_config(tmp_path / 'coding-agent' / 'easy-agent.yml')
    load_config(tmp_path / 'research-starter' / 'easy-agent.yml')
    load_config(tmp_path / 'data-agent' / 'easy-agent.yml')
    load_config(tmp_path / 'ops-agent' / 'easy-agent.yml')
    load_config(tmp_path / 'browser-starter' / 'easy-agent.yml')
    load_config(tmp_path / 'release-agent' / 'easy-agent.yml')
    assert 'workbench' in (tmp_path / 'coding-agent' / 'easy-agent.yml').read_text(encoding='utf-8')
    assert 'official_source_search' in (tmp_path / 'research-starter' / 'easy-agent.yml').read_text(encoding='utf-8')
    assert 'data_agent' in (tmp_path / 'data-agent' / 'easy-agent.yml').read_text(encoding='utf-8')
    assert 'ops_agent' in (tmp_path / 'ops-agent' / 'easy-agent.yml').read_text(encoding='utf-8')
    assert 'browser_agent' in (tmp_path / 'browser-starter' / 'easy-agent.yml').read_text(encoding='utf-8')
    assert 'release_agent' in (tmp_path / 'release-agent' / 'easy-agent.yml').read_text(encoding='utf-8')
    assert 'SERPAPI_API_KEY=<SECRET>' in (tmp_path / 'research-starter' / '.env.local.example').read_text(encoding='utf-8')


def test_quickstart_runs_offline_mock_provider(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ['quickstart', '--provider', 'mock'])

    assert result.exit_code == 0
    assert 'Mock final answer based on tool result' in result.output
    assert 'easy-agent runs explain' in result.output


def test_setup_creates_config_and_runs_mock_smoke(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ['setup', '--provider', 'mock'])

    assert result.exit_code == 0
    assert (tmp_path / 'easy-agent.yml').exists()
    assert 'succeeded' in result.output
    assert 'Checks' in result.output
    assert 'easy-agent traces export' in result.output


def test_wizard_creates_scenario_non_interactively(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    target = tmp_path / 'wizard-basic'

    result = runner.invoke(
        app,
        [
            'wizard',
            '--scenario',
            'basic-agent',
            '--target-dir',
            str(target),
            '--provider',
            'mock',
            '--skip-smoke',
            '--format',
            'json',
        ],
    )

    assert result.exit_code == 0
    assert (target / 'easy-agent.yml').exists()
    assert '"scenario": "basic-agent"' in result.output
    assert '"smoke": "skipped"' in result.output
    assert 'easy-agent dashboard' in result.output
    load_config(target / 'easy-agent.yml')


def test_wizard_skips_browser_smoke_for_mcp_connector(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    target = tmp_path / 'wizard-browser'

    result = runner.invoke(
        app,
        [
            'wizard',
            '--scenario',
            'browser-agent',
            '--target-dir',
            str(target),
            '--provider',
            'mock',
            '--format',
            'json',
        ],
    )

    assert result.exit_code == 0
    assert '"scenario": "browser-agent"' in result.output
    assert 'connectors test browser' in result.output
    assert 'skipped: browser-agent uses a live Playwright MCP connector' in result.output
    assert load_config(target / 'easy-agent.yml').browser.enabled is True


def test_config_validate_explain_and_doctor(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    setup_result = runner.invoke(app, ['setup', '--provider', 'mock', '--skip-smoke'])

    validate_result = runner.invoke(app, ['config', 'validate', '-c', 'easy-agent.yml'])
    explain_result = runner.invoke(app, ['config', 'explain', '-c', 'easy-agent.yml', '--format', 'json'])
    doctor_result = runner.invoke(app, ['config', 'doctor', '-c', 'easy-agent.yml', '--format', 'json'])

    assert setup_result.exit_code == 0
    assert validate_result.exit_code == 0
    assert '"valid": true' in validate_result.output
    assert explain_result.exit_code == 0
    assert '"entrypoint_type": "agent"' in explain_result.output
    assert '"required_env"' in explain_result.output
    assert doctor_result.exit_code == 0
    assert '"checks"' in doctor_result.output
    assert '"python.version"' in doctor_result.output
