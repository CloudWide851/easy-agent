from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.real_network_eval import run_real_network_suite

pytestmark = [pytest.mark.real]



def test_real_network_suite_generates_matrix_report() -> None:
    report = run_real_network_suite()

    scenarios = {item['scenario']: item for item in report['records']}
    assert report['summary']['runs'] >= 10
    assert 'cross_process_federation' in scenarios
    assert 'disconnect_retry_chaos' in scenarios
    assert 'live_model_federation_roundtrip' in scenarios
    assert 'duplicate_delivery_replay_resilience' in scenarios
    assert 'workbench_reuse_process' in scenarios
    assert 'replay_resume_failure_injection' in scenarios
    assert scenarios['cross_process_federation']['status'] == 'passed'
    assert scenarios['disconnect_retry_chaos']['status'] == 'passed'
    assert scenarios['live_model_federation_roundtrip']['status'] in {'passed', 'skipped'}
    assert scenarios['duplicate_delivery_replay_resilience']['status'] == 'passed'
    assert scenarios['workbench_reuse_process']['status'] == 'passed'
    assert scenarios['workbench_reuse_container']['status'] in {'passed', 'skipped'}
    assert scenarios['workbench_incremental_snapshot_reuse_container']['status'] in {'passed', 'skipped'}
    assert scenarios['workbench_reuse_microvm']['status'] in {'passed', 'skipped'}
    assert scenarios['workbench_incremental_snapshot_reuse_microvm']['status'] in {'passed', 'skipped'}
    assert scenarios['replay_resume_failure_injection']['status'] == 'passed'
    assert report['summary']['failed'] == 0
    assert 'telemetry_summary' in report
    assert Path('.easy-agent/real-network-history.jsonl').is_file()
    assert Path('.easy-agent/real-network-report.json').is_file()

