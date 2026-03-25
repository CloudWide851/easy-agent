from __future__ import annotations

import os

import pytest

from agent_runtime.benchmark import run_default_suite

pytestmark = [pytest.mark.real]


@pytest.mark.skipif(not os.environ.get('DEEPSEEK_API_KEY'), reason='requires DEEPSEEK_API_KEY')
def test_real_team_modes_execute_with_live_model() -> None:
    report = run_default_suite('easy-agent.yml', repeat=1)

    expected_modes = {'team_round_robin', 'team_selector', 'team_swarm'}
    assert expected_modes.issubset(report['summary'])
    assert all(report['summary'][mode]['failures'] == 0 for mode in expected_modes)
