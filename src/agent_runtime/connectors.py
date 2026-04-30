from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_config.app import AppConfig, load_config


@dataclass(frozen=True)
class ConnectorCheck:
    name: str
    kind: str
    status: str
    message: str
    action: str


def connector_checks(config_path: str | Path = 'easy-agent.yml') -> list[ConnectorCheck]:
    config = load_config(config_path)
    checks: list[ConnectorCheck] = []
    checks.append(_model_check(config))
    checks.append(_storage_check(config))
    checks.append(_search_check(config))
    checks.extend(_mcp_checks(config))
    checks.extend(_workbench_checks(config))
    checks.extend(_federation_checks(config))
    checks.append(_browser_check(config))
    return checks


def connector_payloads(config_path: str | Path = 'easy-agent.yml') -> list[dict[str, Any]]:
    return [check.__dict__ for check in connector_checks(config_path)]


def connector_summary(checks: list[ConnectorCheck]) -> dict[str, int]:
    return {
        'ok': sum(1 for check in checks if check.status == 'ok'),
        'warn': sum(1 for check in checks if check.status == 'warn'),
        'error': sum(1 for check in checks if check.status == 'error'),
    }


def test_connector(config_path: str | Path, name: str) -> dict[str, Any]:
    checks = [check for check in connector_checks(config_path) if check.name == name]
    if not checks:
        raise ValueError(f'Unknown connector: {name}')
    summary = connector_summary(checks)
    status = 'error' if summary['error'] else 'warn' if summary['warn'] else 'ok'
    return {'name': name, 'status': status, 'summary': summary, 'checks': [check.__dict__ for check in checks]}


def _model_check(config: AppConfig) -> ConnectorCheck:
    if config.model.provider == 'mock':
        return ConnectorCheck('model', 'provider', 'ok', 'Mock provider is configured for offline runs.', 'No action needed.')
    if os.environ.get(config.model.api_key_env):
        return ConnectorCheck('model', 'provider', 'ok', f'{config.model.api_key_env} is present.', 'No action needed.')
    return ConnectorCheck('model', 'provider', 'warn', f'{config.model.api_key_env} is missing.', f'Set {config.model.api_key_env} before live provider runs.')


def _storage_check(config: AppConfig) -> ConnectorCheck:
    storage_path = Path(config.storage.path)
    parent = storage_path.parent if storage_path.parent != Path('.') else Path.cwd()
    if storage_path.is_absolute():
        return ConnectorCheck('storage', 'sqlite', 'warn', 'Storage path is absolute.', 'Prefer project-relative storage for portable configs.')
    if parent.exists():
        return ConnectorCheck('storage', 'sqlite', 'ok', f'Storage parent {parent} exists.', 'No action needed.')
    return ConnectorCheck('storage', 'sqlite', 'warn', f'Storage parent {parent} does not exist yet.', 'It will be created when storage opens.')


def _search_check(config: AppConfig) -> ConnectorCheck:
    public_eval = config.evaluation.public_eval
    search_profiles = {'browsecomp_subset', 'simpleqa_subset', 'simple_evals_subset'}
    if public_eval.web_search.provider == 'serpapi' and public_eval.profile in search_profiles:
        if os.environ.get(public_eval.web_search.api_key_env):
            return ConnectorCheck('search:public_eval', 'search', 'ok', f'{public_eval.web_search.api_key_env} is present.', 'No action needed.')
        return ConnectorCheck('search:public_eval', 'search', 'warn', f'{public_eval.web_search.api_key_env} is missing.', 'Set it for live web-search evals or use replay data.')
    return ConnectorCheck('search:public_eval', 'search', 'ok', 'No live search credential is required by the current eval profile.', 'No action needed.')


def _mcp_checks(config: AppConfig) -> list[ConnectorCheck]:
    if not config.mcp:
        return [ConnectorCheck('mcp', 'mcp', 'ok', 'No MCP servers are configured.', 'Add mcp entries when connector tools are needed.')]
    checks: list[ConnectorCheck] = []
    for server in config.mcp:
        status = 'ok'
        messages: list[str] = []
        actions: list[str] = []
        if server.transport == 'stdio' and server.command:
            executable = str(server.command[0])
            if shutil.which(executable):
                messages.append(f'{executable} is available.')
            else:
                status = 'warn'
                messages.append(f'{executable} is not on PATH.')
                actions.append(f'Install {executable} or update mcp.{server.name}.command.')
        if server.transport == 'stdio' and not server.roots:
            status = 'warn'
            messages.append('No explicit roots are declared.')
            actions.append('Declare mcp.roots to make filesystem boundaries clear.')
        if server.transport in {'http_sse', 'streamable_http'} and not (server.url or server.rpc_url or server.sse_url):
            status = 'error'
            messages.append('Remote MCP URL is missing.')
            actions.append('Set url, rpc_url, or sse_url.')
        if server.auth.token_env and not os.environ.get(server.auth.token_env):
            status = 'warn' if status != 'error' else status
            messages.append(f'{server.auth.token_env} is missing.')
            actions.append(f'Set {server.auth.token_env}.')
        checks.append(
            ConnectorCheck(
                f'mcp:{server.name}',
                'mcp',
                status,
                ' '.join(messages) if messages else 'MCP server config is statically valid.',
                ' '.join(actions) if actions else 'No action needed.',
            )
        )
    return checks


def _workbench_checks(config: AppConfig) -> list[ConnectorCheck]:
    if not config.executors:
        return [ConnectorCheck('workbench:default', 'workbench', 'ok', 'Default process workbench is available.', 'No action needed.')]
    checks: list[ConnectorCheck] = []
    for executor in config.executors:
        if executor.kind == 'process':
            checks.append(ConnectorCheck(f'workbench:{executor.name}', 'workbench', 'ok', 'Process executor is available for local trusted work.', 'Use container or microVM for stronger isolation.'))
        elif executor.kind == 'container' and executor.container:
            available = shutil.which(executor.container.executable) is not None
            checks.append(ConnectorCheck(f'workbench:{executor.name}', 'workbench', 'ok' if available else 'warn', f'{executor.container.executable} {"is available" if available else "is not on PATH"}.', 'No action needed.' if available else 'Install the container runtime or use process executor.'))
        elif executor.kind == 'microvm' and executor.microvm:
            available = shutil.which(executor.microvm.executable) is not None
            checks.append(ConnectorCheck(f'workbench:{executor.name}', 'workbench', 'ok' if available else 'warn', f'{executor.microvm.executable} {"is available" if available else "is not on PATH"}.', 'No action needed.' if available else 'Install the microVM runtime and SSH tools or use process executor.'))
    return checks


def _federation_checks(config: AppConfig) -> list[ConnectorCheck]:
    if not config.federation.remotes:
        return [ConnectorCheck('federation', 'federation', 'ok', 'No federation remotes are configured.', 'Add remotes when cross-agent calls are needed.')]
    checks: list[ConnectorCheck] = []
    for remote in config.federation.remotes:
        status = 'ok'
        message = 'Federation remote is configured.'
        action = 'No action needed.'
        if remote.auth.token_env and not os.environ.get(remote.auth.token_env):
            status = 'warn'
            message = f'{remote.auth.token_env} is missing.'
            action = f'Set {remote.auth.token_env} before calling the remote.'
        checks.append(ConnectorCheck(f'federation:{remote.name}', 'federation', status, message, action))
    return checks


def _browser_check(config: AppConfig) -> ConnectorCheck:
    browser = config.browser
    if not browser.enabled:
        return ConnectorCheck(
            'browser',
            'browser',
            'warn',
            'No first-class browser connector is configured; browser-agent is a planning starter.',
            'Enable browser.provider=playwright_mcp or add a browser MCP server before executing navigation, form, download, or screenshot tasks.',
        )
    if browser.provider == 'playwright_mcp':
        npx_available = shutil.which('npx') is not None
        approval = 'approval required' if browser.require_approval else 'approval disabled'
        if npx_available:
            return ConnectorCheck(
                'browser',
                'browser',
                'ok',
                f'Playwright MCP is configured as mcp:{browser.server_name}; npx is available; {approval}.',
                'Run mcp list or a browser task smoke when live browser automation is needed.',
            )
        return ConnectorCheck(
            'browser',
            'browser',
            'warn',
            f'Playwright MCP is configured as mcp:{browser.server_name}, but npx is not on PATH; {approval}.',
            'Install Node.js/npm or set up an explicit MCP browser server command.',
        )
    return ConnectorCheck(
        'browser',
        'browser',
        'error',
        f'Unsupported browser provider: {browser.provider}.',
        'Use provider=playwright_mcp.',
    )
