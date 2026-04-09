from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from agent_config.app import AppConfig, load_config
from agent_runtime.runtime import build_runtime_from_config


@dataclass(slots=True)
class BenchmarkRecord:
    mode: str
    repetition: int
    success: bool
    duration_seconds: float
    protocol: str
    tool_call_count: int
    subagent_call_count: int
    graph_node_count: int
    result_summary: str
    error: str | None = None


@dataclass(slots=True)
class BenchmarkCase:
    mode: str
    prompt: str
    config: AppConfig


def _shared_payload(base: AppConfig) -> dict[str, Any]:
    return {
        'model': base.model.model_dump(),
        'plugins': list(base.plugins),
        'skills': [item.model_dump() for item in base.skills],
        'mcp': [item.model_dump() for item in base.mcp],
        'storage': base.storage.model_dump(),
        'logging': base.logging.model_dump(),
        'security': base.security.model_dump(),
    }


def build_default_cases(base_config: AppConfig) -> list[BenchmarkCase]:
    shared = _shared_payload(base_config)
    shared['model'] = {
        **shared['model'],
        'function_calling': {
            **shared['model'].get('function_calling', {}),
            'strict': True,
            'parallel_tool_calls': False,
        },
    }
    single_agent = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': 'single-agent',
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'description': 'Main coordinator for direct tool execution.',
                        'system_prompt': 'You must use the command_echo tool when the user explicitly requests it.',
                        'tools': ['command_echo', 'python_echo'],
                        'sub_agents': [],
                        'max_iterations': 6,
                    }
                ],
                'teams': [],
                'nodes': [],
            },
        }
    )
    sub_agent = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': 'sub-agent',
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'description': 'Delegates focused analysis to an analyst agent.',
                        'system_prompt': 'You must delegate once to the analyst sub-agent when the user asks for delegation.',
                        'tools': ['python_echo'],
                        'sub_agents': ['analyst'],
                        'max_iterations': 6,
                    },
                    {
                        'name': 'analyst',
                        'description': 'Produces concise analysis artifacts.',
                        'system_prompt': 'Use python_echo when asked to produce a compact analysis artifact.',
                        'tools': ['python_echo'],
                        'sub_agents': [],
                        'max_iterations': 4,
                    },
                ],
                'teams': [],
                'nodes': [],
            },
        }
    )
    multi_agent_graph = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': 'multi-agent-graph',
                'entrypoint': 'aggregate',
                'agents': [
                    {
                        'name': 'researcher',
                        'description': 'Restates and expands research tasks.',
                        'system_prompt': 'Use python_echo once to restate the research task.',
                        'tools': ['python_echo'],
                        'sub_agents': [],
                        'max_iterations': 4,
                    },
                    {
                        'name': 'reviewer',
                        'description': 'Produces concise review notes.',
                        'system_prompt': 'Use python_echo once to produce a review note.',
                        'tools': ['python_echo'],
                        'sub_agents': [],
                        'max_iterations': 4,
                    },
                ],
                'teams': [],
                'nodes': [
                    {
                        'id': 'research',
                        'type': 'agent',
                        'target': 'researcher',
                        'input_template': 'Use python_echo exactly once to restate this task: {input}',
                    },
                    {
                        'id': 'review',
                        'type': 'agent',
                        'target': 'reviewer',
                        'deps': ['research'],
                        'input_template': 'Use python_echo exactly once to review this output: {research}',
                    },
                    {
                        'id': 'aggregate',
                        'type': 'join',
                        'deps': ['research', 'review'],
                    },
                ],
            },
        }
    )
    team_round_robin = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': 'team-round-robin',
                'entrypoint': 'round_robin_team',
                'agents': [
                    {
                        'name': 'planner',
                        'description': 'Creates an initial task plan.',
                        'system_prompt': 'Use python_echo exactly once, summarize the task, and do not terminate.',
                        'tools': ['python_echo'],
                        'sub_agents': [],
                        'max_iterations': 4,
                    },
                    {
                        'name': 'closer',
                        'description': 'Closes the team run after verification.',
                        'system_prompt': 'Use python_echo exactly once, summarize the closure, and end with TERMINATE.',
                        'tools': ['python_echo'],
                        'sub_agents': [],
                        'max_iterations': 4,
                    },
                ],
                'teams': [
                    {
                        'name': 'round_robin_team',
                        'mode': 'round_robin',
                        'members': ['planner', 'closer'],
                        'max_turns': 4,
                        'termination_text': 'TERMINATE',
                    }
                ],
                'nodes': [],
            },
        }
    )
    team_selector = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': 'team-selector',
                'entrypoint': 'selector_team',
                'agents': [
                    {
                        'name': 'researcher',
                        'description': 'Use this member first to create a short research note when no research note exists yet.',
                        'system_prompt': 'Use python_echo exactly once and say research ready without TERMINATE.',
                        'tools': ['python_echo'],
                        'sub_agents': [],
                        'max_iterations': 4,
                    },
                    {
                        'name': 'closer',
                        'description': 'Use this member after research is ready to close the run with TERMINATE.',
                        'system_prompt': 'Use python_echo exactly once and finish with TERMINATE.',
                        'tools': ['python_echo'],
                        'sub_agents': [],
                        'max_iterations': 4,
                    },
                ],
                'teams': [
                    {
                        'name': 'selector_team',
                        'mode': 'selector',
                        'members': ['researcher', 'closer'],
                        'max_turns': 4,
                        'termination_text': 'TERMINATE',
                        'selector_prompt': (
                            'Choose exactly one agent name. Choose researcher if the transcript does not yet mention '
                            'research ready. Otherwise choose closer.'
                        ),
                    }
                ],
                'nodes': [],
            },
        }
    )
    team_swarm = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': 'team-swarm',
                'entrypoint': 'swarm_team',
                'agents': [
                    {
                        'name': 'dispatcher',
                        'description': 'Routes work to the finisher using handoff tools.',
                        'system_prompt': 'Immediately hand off to the finisher with a brief message about the task.',
                        'tools': [],
                        'sub_agents': [],
                        'max_iterations': 4,
                    },
                    {
                        'name': 'finisher',
                        'description': 'Completes the task after a handoff and finishes with TERMINATE.',
                        'system_prompt': 'Use python_echo exactly once on the handoff task and end with TERMINATE.',
                        'tools': ['python_echo'],
                        'sub_agents': [],
                        'max_iterations': 4,
                    },
                ],
                'teams': [
                    {
                        'name': 'swarm_team',
                        'mode': 'swarm',
                        'members': ['dispatcher', 'finisher'],
                        'max_turns': 4,
                        'termination_text': 'TERMINATE',
                    }
                ],
                'nodes': [],
            },
        }
    )
    return [
        BenchmarkCase(
            mode='single_agent',
            prompt='Use the command_echo tool exactly once and echo the text single-agent-check.',
            config=single_agent,
        ),
        BenchmarkCase(
            mode='sub_agent',
            prompt='Delegate exactly once to the analyst sub-agent, ask it to analyze sub-agent-check, then summarize.',
            config=sub_agent,
        ),
        BenchmarkCase(
            mode='multi_agent_graph',
            prompt='graph-multi-agent-check',
            config=multi_agent_graph,
        ),
        BenchmarkCase(
            mode='team_round_robin',
            prompt='Collaborate as a round robin team on team-round-robin-check and terminate when complete.',
            config=team_round_robin,
        ),
        BenchmarkCase(
            mode='team_selector',
            prompt='Collaborate as a selector team on team-selector-check and terminate when complete.',
            config=team_selector,
        ),
        BenchmarkCase(
            mode='team_swarm',
            prompt='Collaborate as a swarm team on team-swarm-check and terminate when complete.',
            config=team_swarm,
        ),
    ]


def _summarize_result(result: Any) -> str:
    if isinstance(result, str):
        return result[:200]
    return json.dumps(result, ensure_ascii=False)[:200]


def summarize_trace(trace: dict[str, Any], protocol: str, output: dict[str, Any], duration: float, mode: str, repetition: int, error: str | None = None) -> BenchmarkRecord:
    tool_call_count = 0
    subagent_call_count = 0
    for event in trace.get('events', []):
        if event.get('kind') != 'agent_response':
            continue
        for call in event.get('payload', {}).get('tool_calls', []):
            tool_call_count += 1
            if call.get('name', '').startswith('subagent__'):
                subagent_call_count += 1
    graph_node_count = len(output.get('nodes', {})) if isinstance(output.get('nodes'), dict) else 0
    return BenchmarkRecord(
        mode=mode,
        repetition=repetition,
        success=error is None,
        duration_seconds=round(duration, 4),
        protocol=protocol,
        tool_call_count=tool_call_count,
        subagent_call_count=subagent_call_count,
        graph_node_count=graph_node_count,
        result_summary=_summarize_result(output.get('result')),
        error=error,
    )


async def _run_case_once(case: BenchmarkCase, repetition: int) -> BenchmarkRecord:
    with tempfile.TemporaryDirectory(prefix=f'easy-agent-bench-{case.mode}-') as storage_dir:
        config = case.config.model_copy(deep=True)
        config.storage.path = storage_dir
        runtime = build_runtime_from_config(config)
        start = time.perf_counter()
        try:
            await runtime.start()
            result = await runtime.run(case.prompt)
            duration = time.perf_counter() - start
            trace = runtime.store.load_trace(result['run_id'])
            protocol = runtime.model_client.adapter.protocol.value
            return summarize_trace(trace, protocol, result, duration, case.mode, repetition)
        except Exception as exc:
            duration = time.perf_counter() - start
            return BenchmarkRecord(
                mode=case.mode,
                repetition=repetition,
                success=False,
                duration_seconds=round(duration, 4),
                protocol=getattr(runtime.model_client.adapter.protocol, 'value', 'unknown'),
                tool_call_count=0,
                subagent_call_count=0,
                graph_node_count=len(config.graph.nodes),
                result_summary='',
                error=str(exc),
            )
        finally:
            await runtime.aclose()


def build_report(records: list[BenchmarkRecord]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for mode in sorted({record.mode for record in records}):
        items = [record for record in records if record.mode == mode]
        summary[mode] = {
            'runs': len(items),
            'successes': sum(1 for item in items if item.success),
            'failures': sum(1 for item in items if not item.success),
            'average_duration_seconds': round(mean(item.duration_seconds for item in items), 4),
            'average_tool_calls': round(mean(item.tool_call_count for item in items), 2),
            'average_subagent_calls': round(mean(item.subagent_call_count for item in items), 2),
        }
    return {
        'records': [asdict(record) for record in records],
        'summary': summary,
    }


def run_default_suite(config_path: str | Path, repeat: int) -> dict[str, Any]:
    base_config = load_config(config_path)
    cases = build_default_cases(base_config)
    records: list[BenchmarkRecord] = []
    for case in cases:
        for repetition in range(1, repeat + 1):
            record = asyncio.run(_run_case_once(case, repetition))
            if not record.success:
                retry = asyncio.run(_run_case_once(case, repetition))
                if retry.success:
                    record = retry
            records.append(record)
    return build_report(records)
