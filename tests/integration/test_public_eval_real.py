from __future__ import annotations

import os

import pytest

from agent_runtime.public_eval import run_public_eval_suite

pytestmark = [pytest.mark.real]


@pytest.mark.skipif(not os.environ.get('DEEPSEEK_API_KEY'), reason='requires DEEPSEEK_API_KEY')
def test_public_eval_suite_runs_with_live_model() -> None:
    report = run_public_eval_suite('easy-agent.yml')

    expected_suites = {
        'bfcl_simple',
        'bfcl_multiple',
        'bfcl_parallel_multiple',
        'bfcl_irrelevance',
        'tau2_mock',
        'overall',
    }
    assert expected_suites.issubset(report['summary'])
    assert report['records']
    assert all('suite' in record and 'case_id' in record for record in report['records'])
    assert all(record['duration_seconds'] >= 0 for record in report['records'])
