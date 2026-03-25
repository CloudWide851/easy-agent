from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from agent_common.models import Protocol
from agent_config.app import AppConfig, load_config


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
  entrypoint: coordinator
  agents:
    - name: coordinator
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
