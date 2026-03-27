import os
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from agent_common.models import Protocol
from agent_config.app import AppConfig, load_config, load_local_env


def test_load_config_expands_environment_variables(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv('EA_STORAGE', str(tmp_path / 'state'))
    config_path = tmp_path / 'easy-agent.yml'
    config_path.write_text(
        '''
model:
  provider: deepseek
  protocol: auto
graph:
  entrypoint: agent-a
  agents:
    - name: agent-a
  nodes: []
storage:
  path: ${EA_STORAGE}
        ''',
        encoding='utf-8',
    )

    config = load_config(config_path)

    assert config.model.protocol is Protocol.AUTO
    assert Path(config.storage.path) == tmp_path / 'state'
    assert config.graph.teams == []
    assert config.harnesses == []


def test_graph_allows_team_entrypoint() -> None:
    config = AppConfig.model_validate(
        {
            'graph': {
                'entrypoint': 'writer_team',
                'agents': [
                    {
                        'name': 'planner',
                        'description': 'Plans the work.',
                    },
                    {
                        'name': 'closer',
                        'description': 'Closes the work.',
                    },
                ],
                'teams': [
                    {
                        'name': 'writer_team',
                        'mode': 'round_robin',
                        'members': ['planner', 'closer'],
                    }
                ],
                'nodes': [],
            }
        }
    )

    assert config.graph.entrypoint == 'writer_team'
    assert config.team_map['writer_team'].mode.value == 'round_robin'


def test_harness_validation_accepts_agent_and_team_targets() -> None:
    config = AppConfig.model_validate(
        {
            'graph': {
                'entrypoint': 'planner',
                'agents': [
                    {'name': 'planner', 'description': 'Plans the work.'},
                    {'name': 'worker', 'description': 'Works the task.'},
                    {'name': 'evaluator', 'description': 'Evaluates the task.'},
                ],
                'teams': [
                    {
                        'name': 'worker_team',
                        'mode': 'round_robin',
                        'members': ['planner', 'worker'],
                    }
                ],
                'nodes': [],
            },
            'harnesses': [
                {
                    'name': 'delivery_loop',
                    'initializer_agent': 'planner',
                    'worker_target': 'worker_team',
                    'evaluator_agent': 'evaluator',
                    'completion_contract': 'Finish the run.',
                    'artifacts_dir': '.easy-agent/harness',
                }
            ],
        }
    )

    assert config.harness_map['delivery_loop'].worker_target == 'worker_team'


def test_harness_validation_rejects_unknown_targets() -> None:
    with pytest.raises(ValueError, match='unknown worker_target'):
        AppConfig.model_validate(
            {
                'graph': {
                    'entrypoint': 'planner',
                    'agents': [
                        {'name': 'planner', 'description': 'Plans the work.'},
                        {'name': 'evaluator', 'description': 'Evaluates the task.'},
                    ],
                    'teams': [],
                    'nodes': [],
                },
                'harnesses': [
                    {
                        'name': 'delivery_loop',
                        'initializer_agent': 'planner',
                        'worker_target': 'missing-worker',
                        'evaluator_agent': 'evaluator',
                        'completion_contract': 'Finish the run.',
                        'artifacts_dir': '.easy-agent/harness',
                    }
                ],
            }
        )


def test_selector_team_requires_member_descriptions() -> None:
    with pytest.raises(ValueError, match='requires non-empty agent descriptions'):
        AppConfig.model_validate(
            {
                'graph': {
                    'entrypoint': 'selector_team',
                    'agents': [
                        {'name': 'researcher', 'description': ''},
                        {'name': 'closer', 'description': 'Closes the run.'},
                    ],
                    'teams': [
                        {
                            'name': 'selector_team',
                            'mode': 'selector',
                            'members': ['researcher', 'closer'],
                        }
                    ],
                    'nodes': [],
                }
            }
        )


def test_graph_rejects_duplicate_agent_team_and_node_names() -> None:
    with pytest.raises(ValueError, match='must be unique'):
        AppConfig.model_validate(
            {
                'graph': {
                    'entrypoint': 'shared',
                    'agents': [{'name': 'shared'}],
                    'teams': [{'name': 'shared', 'mode': 'round_robin', 'members': ['shared']}],
                    'nodes': [],
                }
            }
        )


def test_load_local_env_reads_repo_local_file_once(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    env_path = tmp_path / '.env.local'
    config_path = tmp_path / 'easy-agent.yml'
    env_path.write_text(
        '\n'.join(
            [
                '# local only',
                'DEEPSEEK_API_KEY=test-local-key',
                'PG_HOST=127.0.0.1',
                'PG_PORT=5432',
            ]
        ),
        encoding='utf-8',
    )
    config_path.write_text('graph:\n  entrypoint: noop\n  agents:\n    - name: noop\n', encoding='utf-8')

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('PG_HOST', raising=False)
    monkeypatch.delenv('PG_PORT', raising=False)

    load_local_env(config_path)
    load_local_env(config_path)

    assert os.environ['DEEPSEEK_API_KEY'] == 'test-local-key'
    assert os.environ['PG_HOST'] == '127.0.0.1'
    assert os.environ['PG_PORT'] == '5432'


def test_federation_export_validation_accepts_agent_team_and_harness() -> None:
    config = AppConfig.model_validate(
        {
            'graph': {
                'entrypoint': 'planner',
                'agents': [
                    {'name': 'planner', 'description': 'Plans work.'},
                    {'name': 'worker', 'description': 'Works tasks.'},
                    {'name': 'evaluator', 'description': 'Evaluates work.'},
                ],
                'teams': [{'name': 'worker_team', 'mode': 'round_robin', 'members': ['planner', 'worker']}],
                'nodes': [],
            },
            'harnesses': [
                {
                    'name': 'delivery_loop',
                    'initializer_agent': 'planner',
                    'worker_target': 'worker_team',
                    'evaluator_agent': 'evaluator',
                    'completion_contract': 'Finish the run.',
                    'artifacts_dir': '.easy-agent/harness',
                }
            ],
            'federation': {
                'exports': [
                    {'name': 'agent_export', 'target_type': 'agent', 'target': 'planner'},
                    {'name': 'team_export', 'target_type': 'team', 'target': 'worker_team'},
                    {'name': 'harness_export', 'target_type': 'harness', 'target': 'delivery_loop'},
                ]
            },
        }
    )

    assert set(config.federation_export_map) == {'agent_export', 'team_export', 'harness_export'}



def test_workbench_validation_rejects_unknown_executor() -> None:
    with pytest.raises(ValueError, match='workbench.default_executor'):
        AppConfig.model_validate(
            {
                'graph': {
                    'entrypoint': 'planner',
                    'agents': [{'name': 'planner'}],
                    'teams': [],
                    'nodes': [],
                },
                'executors': [{'name': 'process', 'kind': 'process'}],
                'workbench': {'default_executor': 'missing-executor'},
            }
        )
