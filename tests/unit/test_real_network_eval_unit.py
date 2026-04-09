from agent_runtime.real_network_eval import (
    RealNetworkRecord,
    _aggregate_telemetry_summary,
    _budget_status,
    _snapshot_drift,
)


def test_snapshot_drift_reports_absolute_and_ratio() -> None:
    drift_seconds, drift_ratio = _snapshot_drift(4.0, 3.0)

    assert drift_seconds == 1.0
    assert drift_ratio == 0.25


def test_budget_status_flags_budget_overruns() -> None:
    assert _budget_status(3.0, 5.0) == 'within_budget'
    assert _budget_status(6.0, 5.0) == 'exceeds_budget'


def test_aggregate_telemetry_summary_collects_cache_and_drift_metrics() -> None:
    summary = _aggregate_telemetry_summary(
        [
            RealNetworkRecord(
                scenario='workbench_reuse_container',
                transport='podman_exec',
                live_model=False,
                host_dependency='podman',
                status='passed',
                duration_seconds=10.0,
                notes='ok',
                telemetry={
                    'cache_hit': True,
                    'warm_start_seconds': 4.0,
                    'budget_status': 'within_budget',
                    'snapshot_drift_ratio': 0.1,
                },
            ),
            RealNetworkRecord(
                scenario='workbench_reuse_microvm',
                transport='podman_machine_ssh',
                live_model=False,
                host_dependency='ssh',
                status='passed',
                duration_seconds=12.0,
                notes='ok',
                telemetry={
                    'cache_hit': False,
                    'warm_start_seconds': 6.0,
                    'budget_status': 'exceeds_budget',
                    'snapshot_drift_ratio': 0.3,
                },
            ),
        ]
    )

    assert summary['telemetry_records'] == 2
    assert summary['cache_hit_rate'] == 0.5
    assert summary['budget_statuses']['within_budget'] == 1
    assert summary['budget_statuses']['exceeds_budget'] == 1
    assert summary['snapshot_drift_ratio_max'] == 0.3
