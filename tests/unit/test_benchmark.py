from pathlib import Path

import pytest

from agent_config.app import AppConfig, ModelConfig
from agent_runtime.benchmark import (
    BenchmarkCase,
    BenchmarkRecord,
    build_default_cases,
    run_default_suite,
    summarize_trace,
)


def build_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            'model': ModelConfig().model_dump(),
            'graph': {
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'description': 'Baseline coordinator.',
                        'tools': ['python_echo'],
                        'sub_agents': [],
                    }
                ],
                'teams': [],
                'nodes': [],
            },
            'skills': [{'path': 'skills/examples'}],
            'mcp': [],
            'storage': {'path': '.easy-agent', 'database': 'state.db'},
            'security': {'allowed_commands': [['cmd', '/c', 'echo']]},
        }
    )


def test_build_default_cases_contains_all_modes() -> None:
    cases = build_default_cases(build_config())

    assert [case.mode for case in cases] == [
        'single_agent',
        'sub_agent',
        'multi_agent_graph',
        'team_round_robin',
        'team_selector',
        'team_swarm',
    ]


def test_summarize_trace_counts_tool_and_subagent_calls() -> None:
    trace = {
        'events': [
            {
                'kind': 'agent_response',
                'payload': {
                    'tool_calls': [
                        {'name': 'python_echo'},
                        {'name': 'subagent__analyst'},
                    ]
                },
            }
        ]
    }
    output = {'result': {'status': 'ok'}, 'nodes': {'a': 1, 'b': 2}}

    record = summarize_trace(trace, 'openai', output, 1.2345, 'sub_agent', 1)

    assert record.protocol == 'openai'
    assert record.tool_call_count == 2
    assert record.subagent_call_count == 1
    assert record.graph_node_count == 2
    assert record.success is True


def test_run_default_suite_retries_failed_case_once(monkeypatch: pytest.MonkeyPatch) -> None:
    case = BenchmarkCase(mode='single_agent', prompt='ping', config=build_config())
    calls = {'count': 0}

    async def fake_run_case_once(case_arg, repetition):  # type: ignore[no-untyped-def]
        del case_arg, repetition
        calls['count'] += 1
        return BenchmarkRecord(
            mode='single_agent',
            repetition=1,
            success=calls['count'] > 1,
            duration_seconds=1.0,
            protocol='openai',
            tool_call_count=1 if calls['count'] > 1 else 0,
            subagent_call_count=0,
            graph_node_count=0,
            result_summary='ok' if calls['count'] > 1 else '',
            error=None if calls['count'] > 1 else 'drift',
        )

    monkeypatch.setattr('agent_runtime.benchmark.load_config', lambda path: build_config())
    monkeypatch.setattr('agent_runtime.benchmark.build_default_cases', lambda config: [case])
    monkeypatch.setattr('agent_runtime.benchmark._run_case_once', fake_run_case_once)

    report = run_default_suite(Path('easy-agent.yml'), 1)

    assert calls['count'] == 2
    assert report['summary']['single_agent']['successes'] == 1
