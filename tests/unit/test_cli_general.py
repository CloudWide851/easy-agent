from __future__ import annotations

from types import SimpleNamespace

from agent_cli.commands.general import _doctor_rows, _entrypoint_type, _mcp_transport_summary
from agent_config.app import AppConfig, ModelConfig
from agent_integrations.sandbox import SandboxMode


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
                ],
                'teams': [
                    {'name': 'writer_team', 'mode': 'round_robin', 'members': ['planner', 'closer']}
                ],
                'nodes': [],
            },
            'mcp': [{'name': 'filesystem', 'transport': 'stdio'}],
        }
    )

    runtime = _runtime_from_config(config)
    rows = dict(_doctor_rows(runtime))

    assert rows['Provider'] == 'deepseek'
    assert rows['Model'] == 'deepseek-chat'
    assert rows['Entrypoint'] == 'writer_team'
    assert rows['Entrypoint Type'] == 'team'
    assert rows['Configured MCP Servers'] == '1'
    assert rows['MCP Transports'] == 'filesystem:stdio'
    assert rows['Sandbox Fallback'] == 'process'
