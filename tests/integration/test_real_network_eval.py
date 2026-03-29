from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.real_network_eval import run_real_network_suite

pytestmark = [pytest.mark.real]



def test_real_network_suite_generates_matrix_report() -> None:
    report = run_real_network_suite()

    scenarios = {item['scenario']: item for item in report['records']}
    assert report['summary']['runs'] >= 6
    assert 'cross_process_federation' in scenarios
    assert 'disconnect_retry_chaos' in scenarios
    assert 'workbench_reuse_process' in scenarios
    assert 'replay_resume_failure_injection' in scenarios
    assert scenarios['cross_process_federation']['status'] == 'passed'
    assert scenarios['disconnect_retry_chaos']['status'] == 'passed'
    assert scenarios['workbench_reuse_process']['status'] == 'passed'
    assert scenarios['workbench_reuse_container']['status'] == 'passed'
    assert scenarios['workbench_reuse_microvm']['status'] == 'passed'
    assert scenarios['replay_resume_failure_injection']['status'] == 'passed'
    assert report['summary']['skipped'] == 0
    assert Path('.easy-agent/real-network-report.json').is_file()

