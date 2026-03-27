from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any, cast

from agent_common.models import ChatMessage, ToolCall, ToolSpec
from agent_config.app import AppConfig, load_config
from agent_runtime.runtime import build_runtime_from_config

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / 'public_evals' / 'fixtures'


@dataclass(slots=True)
class PublicEvalRecord:
    suite: str
    case_id: str
    success: bool
    duration_seconds: float
    tool_name_match: float
    argument_match: float
    expected_call_count: int
    actual_call_count: int
    result_summary: str
    error: str | None = None


def _shared_payload(base: AppConfig) -> dict[str, Any]:
    return {
        'model': base.model.model_dump(),
        'plugins': list(base.plugins),
        'skills': [item.model_dump() for item in base.skills],
        'mcp': [item.model_dump() for item in base.mcp],
        'storage': base.storage.model_dump(),
        'logging': base.logging.model_dump(),
        'guardrails': base.guardrails.model_dump(),
        'observability': base.observability.model_dump(),
        'security': base.security.model_dump(),
    }


def _load_fixture(name: str) -> dict[str, Any]:
    payload = json.loads((FIXTURE_ROOT / name).read_text(encoding='utf-8'))
    return cast(dict[str, Any], payload)


def _bfcl_system_prompt() -> str:
    return (
        'You are evaluating tool-calling behavior. Choose the best available tool based on the user request. '
        'If the request is irrelevant to the available tools, answer directly without any tool call. '
        'When multiple independent tool calls are required, issue all of them. '
        'Arguments must match the tool schema exactly. If a validation error is returned, correct the arguments and try again.'
    )


def _tau_system_prompt() -> str:
    return (
        'You are a precise task assistant. Use the provided task-management tools when the user requests an action. '
        'Acknowledge successful completion concisely. If previous conversation state is present, continue from it.'
    )


def _sanitize_tool_name(name: str) -> str:
    sanitized = re.sub(r'[^A-Za-z0-9_-]+', '_', name).strip('_')
    if not sanitized:
        sanitized = 'tool'
    if sanitized[0].isdigit():
        sanitized = f'tool_{sanitized}'
    return sanitized[:64]


def _normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(schema)
    schema_type = str(normalized.get('type', ''))
    if schema_type == 'dict':
        normalized['type'] = 'object'
    elif schema_type == 'tuple':
        normalized['type'] = 'array'
    if normalized.get('type') == 'object':
        properties = cast(dict[str, Any], normalized.get('properties', {}))
        normalized['properties'] = {
            key: _normalize_schema(value) if isinstance(value, dict) else value for key, value in properties.items()
        }
    items = normalized.get('items')
    if isinstance(items, dict):
        normalized['items'] = _normalize_schema(items)
    return normalized


def _build_tool_name_map(functions: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for function in functions:
        original = str(function['name'])
        base = _sanitize_tool_name(original)
        candidate = base
        index = 2
        while candidate in used:
            suffix = f'_{index}'
            candidate = f"{base[: max(1, 64 - len(suffix))]}{suffix}"
            index += 1
        mapping[original] = candidate
        used.add(candidate)
    return mapping


def _normalize_truth_call(
    item: dict[str, Any],
    tool_name_map: dict[str, str] | None = None,
) -> tuple[str, dict[str, list[Any]]]:
    tool_name = next(iter(item.keys()))
    if tool_name_map is not None:
        tool_name = tool_name_map.get(tool_name, tool_name)
    return tool_name, item[next(iter(item.keys()))]


def _values_match(actual: Any, options: list[Any]) -> bool:
    if actual in (None, ''):
        return '' in options
    for option in options:
        if option == '':
            continue
        if isinstance(option, list) and isinstance(actual, tuple):
            if list(actual) == option:
                return True
        if option == actual:
            return True
        if isinstance(option, str) and isinstance(actual, str) and option.lower() == actual.lower():
            return True
        if isinstance(option, float) and isinstance(actual, (int, float)) and float(actual) == option:
            return True
        if isinstance(option, int) and isinstance(actual, (int, float)) and int(actual) == option:
            return True
    return False


def _truth_matches(actual: dict[str, Any], truth: dict[str, list[Any]]) -> float:
    scores: list[float] = []
    for key, options in truth.items():
        if key not in actual:
            scores.append(1.0 if '' in options else 0.0)
            continue
        value = actual[key]
        if isinstance(value, dict) and options and isinstance(options[0], dict):
            nested_truth = options[0]
            nested_hits = 0
            for nested_key, nested_options in nested_truth.items():
                nested_value = value.get(nested_key)
                if _values_match(nested_value, nested_options):
                    nested_hits += 1
            scores.append(nested_hits / max(1, len(nested_truth)))
            continue
        scores.append(1.0 if _values_match(value, options) else 0.0)
    if not scores:
        return 1.0
    return sum(scores) / len(scores)


def _extract_successful_tool_calls(trace: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for event in trace.get('events', []):
        if event.get('kind') != 'tool_call_succeeded':
            continue
        payload = event.get('payload', {})
        calls.append({'name': payload.get('tool_name'), 'arguments': payload.get('arguments', {})})
    return calls


def _score_bfcl_case(
    case: dict[str, Any],
    actual_calls: list[dict[str, Any]],
    tool_name_map: dict[str, str] | None = None,
) -> tuple[bool, float, float]:
    if case['expect_no_tool']:
        success = len(actual_calls) == 0
        return success, 1.0 if success else 0.0, 1.0 if success else 0.0
    truths = [_normalize_truth_call(item, tool_name_map) for item in case['ground_truth']]
    if len(actual_calls) != len(truths):
        return False, 0.0, 0.0
    used: set[int] = set()
    tool_hits = 0.0
    arg_scores: list[float] = []
    for expected_name, truth_args in truths:
        best_index: int | None = None
        best_score = -1.0
        for index, actual in enumerate(actual_calls):
            if index in used or actual['name'] != expected_name:
                continue
            score = _truth_matches(actual['arguments'], truth_args)
            if score > best_score:
                best_index = index
                best_score = score
        if best_index is None:
            arg_scores.append(0.0)
            continue
        used.add(best_index)
        tool_hits += 1.0
        arg_scores.append(best_score)
    tool_name_match = tool_hits / len(truths)
    argument_match = sum(arg_scores) / len(truths)
    success = tool_name_match == 1.0 and argument_match == 1.0
    return success, tool_name_match, argument_match


def _score_tau_case(case: dict[str, Any], actual_calls: list[dict[str, Any]]) -> tuple[bool, float, float]:
    expected = case.get('evaluation_criteria', {}).get('actions', [])
    if len(actual_calls) < len(expected):
        return False, 0.0, 0.0
    tool_hits = 0.0
    arg_scores: list[float] = []
    for expected_call in expected:
        matched = next((item for item in actual_calls if item['name'] == expected_call['name']), None)
        if matched is None:
            arg_scores.append(0.0)
            continue
        tool_hits += 1.0
        truth_args = {key: [value] for key, value in expected_call['arguments'].items()}
        arg_scores.append(_truth_matches(matched['arguments'], truth_args))
    tool_name_match = tool_hits / len(expected)
    argument_match = sum(arg_scores) / len(expected)
    success = tool_name_match == 1.0 and argument_match == 1.0
    return success, tool_name_match, argument_match


def _summarize_result(result: Any) -> str:
    if isinstance(result, str):
        return result[:200]
    return json.dumps(result, ensure_ascii=False)[:200]


async def _run_bfcl_case(base_config: AppConfig, case: dict[str, Any]) -> PublicEvalRecord:
    shared = _shared_payload(base_config)
    tool_name_map = _build_tool_name_map(cast(list[dict[str, Any]], case['functions']))
    config = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': f"bfcl-{case['id']}",
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'description': 'Public-eval tool-calling evaluator.',
                        'system_prompt': _bfcl_system_prompt(),
                        'tools': [tool_name_map[str(item['name'])] for item in case['functions']],
                        'sub_agents': [],
                        'max_iterations': 6,
                    }
                ],
                'teams': [],
                'nodes': [],
            },
        }
    )
    with tempfile.TemporaryDirectory(prefix=f"easy-agent-bfcl-{case['id']}-") as storage_dir:
        config.storage.path = storage_dir
        runtime = build_runtime_from_config(config)
        for function in case['functions']:
            original_name = str(function['name'])
            tool_name = tool_name_map[original_name]
            input_schema = _normalize_schema(cast(dict[str, Any], function['parameters']))

            def record_tool_call(arguments: dict[str, Any], context: Any, *, bound_name: str = tool_name) -> dict[str, Any]:
                return {
                    'tool': bound_name,
                    'arguments': arguments,
                    'run_id': context.run_id,
                }

            runtime.register_tool(
                ToolSpec(
                    name=tool_name,
                    description=function['description'],
                    input_schema=input_schema,
                ),
                record_tool_call,
            )
        start = time.perf_counter()
        try:
            await runtime.start()
            prompt = str(case['messages'][0]['content'])
            result = await runtime.run(prompt)
            duration = time.perf_counter() - start
            trace = runtime.store.load_trace(result['run_id'])
            actual_calls = _extract_successful_tool_calls(trace)
            success, tool_name_match, argument_match = _score_bfcl_case(case, actual_calls, tool_name_map)
            return PublicEvalRecord(
                suite=f"bfcl_{case['suite']}",
                case_id=case['id'],
                success=success,
                duration_seconds=round(duration, 4),
                tool_name_match=tool_name_match,
                argument_match=argument_match,
                expected_call_count=len(case['ground_truth']),
                actual_call_count=len(actual_calls),
                result_summary=_summarize_result(result.get('result')),
                error=None if success else json.dumps({'actual_calls': actual_calls}, ensure_ascii=False),
            )
        except Exception as exc:
            duration = time.perf_counter() - start
            return PublicEvalRecord(
                suite=f"bfcl_{case['suite']}",
                case_id=case['id'],
                success=False,
                duration_seconds=round(duration, 4),
                tool_name_match=0.0,
                argument_match=0.0,
                expected_call_count=len(case['ground_truth']),
                actual_call_count=0,
                result_summary='',
                error=str(exc),
            )
        finally:
            await runtime.aclose()


async def _run_tau_case(base_config: AppConfig, case: dict[str, Any]) -> PublicEvalRecord:
    shared = _shared_payload(base_config)
    config = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': f"tau2-{case['id']}",
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'description': 'Mock task-management evaluator agent.',
                        'system_prompt': _tau_system_prompt(),
                        'tools': ['create_task', 'update_task_status'],
                        'sub_agents': [],
                        'max_iterations': 6,
                    }
                ],
                'teams': [],
                'nodes': [],
            },
        }
    )
    with tempfile.TemporaryDirectory(prefix=f"easy-agent-tau2-{case['id']}-") as storage_dir:
        config.storage.path = storage_dir
        runtime = build_runtime_from_config(config)
        tasks: dict[str, dict[str, Any]] = {
            'task_1': {'task_id': 'task_1', 'title': 'Existing Task', 'status': 'pending', 'user_id': 'user_1'}
        }
        task_counter = 1

        def create_task(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            nonlocal task_counter
            task_counter += 1
            task_id = f'task_{task_counter}'
            payload = {
                'task_id': task_id,
                'user_id': arguments['user_id'],
                'title': arguments['title'],
                'description': arguments.get('description', ''),
                'status': 'pending',
                'run_id': context.run_id,
            }
            tasks[task_id] = payload
            return payload

        def update_task_status(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            task_id = arguments['task_id']
            task = tasks[task_id]
            task['status'] = arguments['status']
            task['run_id'] = context.run_id
            return dict(task)

        runtime.register_tool(
            ToolSpec(
                name='create_task',
                description='Create a new task for a user.',
                input_schema={
                    'type': 'object',
                    'properties': {
                        'user_id': {'type': 'string'},
                        'title': {'type': 'string'},
                        'description': {'type': 'string'},
                    },
                    'required': ['user_id', 'title'],
                },
            ),
            create_task,
        )
        runtime.register_tool(
            ToolSpec(
                name='update_task_status',
                description='Update the status of an existing task.',
                input_schema={
                    'type': 'object',
                    'properties': {
                        'task_id': {'type': 'string'},
                        'status': {'type': 'string'},
                    },
                    'required': ['task_id', 'status'],
                },
            ),
            update_task_status,
        )
        start = time.perf_counter()
        try:
            await runtime.start()
            session_id = f"tau2-{case['id']}"
            initial_messages = []
            for item in case.get('initial_state', {}).get('message_history', []):
                if item['role'] == 'assistant' and item.get('tool_calls'):
                    calls = [ToolCall.model_validate(call) for call in item['tool_calls']]
                    initial_messages.append(ChatMessage(role='assistant', content=item.get('content', ''), tool_calls=calls))
                elif item['role'] == 'tool':
                    initial_messages.append(
                        ChatMessage(
                            role='tool',
                            content=item['content'],
                            name='create_task',
                            tool_call_id=item.get('id'),
                        )
                    )
                else:
                    initial_messages.append(ChatMessage(role=item['role'], content=item.get('content', '')))
            if initial_messages:
                runtime.store.save_session_messages(session_id, config.graph.name, initial_messages)
                tasks['task_2'] = {
                    'task_id': 'task_2',
                    'user_id': 'user_1',
                    'title': 'Project Review',
                    'description': 'Review Q4 project status',
                    'status': 'pending',
                }
                task_counter = 2
            prompt = str(case.get('ticket') or case.get('user_scenario', {}).get('instructions', ''))
            result = await runtime.run(prompt, session_id=session_id if initial_messages else None)
            duration = time.perf_counter() - start
            trace = runtime.store.load_trace(result['run_id'])
            actual_calls = _extract_successful_tool_calls(trace)
            success, tool_name_match, argument_match = _score_tau_case(case, actual_calls)
            return PublicEvalRecord(
                suite='tau2_mock',
                case_id=case['id'],
                success=success,
                duration_seconds=round(duration, 4),
                tool_name_match=tool_name_match,
                argument_match=argument_match,
                expected_call_count=len(case.get('evaluation_criteria', {}).get('actions', [])),
                actual_call_count=len(actual_calls),
                result_summary=_summarize_result(result.get('result')),
                error=None if success else json.dumps({'actual_calls': actual_calls}, ensure_ascii=False),
            )
        except Exception as exc:
            duration = time.perf_counter() - start
            return PublicEvalRecord(
                suite='tau2_mock',
                case_id=case['id'],
                success=False,
                duration_seconds=round(duration, 4),
                tool_name_match=0.0,
                argument_match=0.0,
                expected_call_count=len(case.get('evaluation_criteria', {}).get('actions', [])),
                actual_call_count=0,
                result_summary='',
                error=str(exc),
            )
        finally:
            await runtime.aclose()


def _aggregate_summary(records: list[PublicEvalRecord]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for suite in sorted({record.suite for record in records}):
        items = [item for item in records if item.suite == suite]
        summary[suite] = {
            'runs': len(items),
            'successes': sum(1 for item in items if item.success),
            'failures': sum(1 for item in items if not item.success),
            'pass_rate': round(sum(1 for item in items if item.success) / len(items), 4),
            'tool_name_match_rate': round(mean(item.tool_name_match for item in items), 4),
            'argument_match_rate': round(mean(item.argument_match for item in items), 4),
            'average_duration_seconds': round(mean(item.duration_seconds for item in items), 4),
        }
    bfcl_items = [item for item in records if item.suite.startswith('bfcl_')]
    irrelevance_items = [item for item in records if item.suite == 'bfcl_irrelevance']
    tau_items = [item for item in records if item.suite == 'tau2_mock']
    summary['overall'] = {
        'bfcl_pass_rate': round(sum(1 for item in bfcl_items if item.success) / len(bfcl_items), 4),
        'bfcl_tool_name_match_rate': round(mean(item.tool_name_match for item in bfcl_items), 4),
        'bfcl_argument_match_rate': round(mean(item.argument_match for item in bfcl_items), 4),
        'bfcl_irrelevance_pass_rate': round(sum(1 for item in irrelevance_items if item.success) / len(irrelevance_items), 4),
        'tau2_mock_pass_rate': round(sum(1 for item in tau_items if item.success) / len(tau_items), 4),
        'tau2_mock_average_duration_seconds': round(mean(item.duration_seconds for item in tau_items), 4),
    }
    return summary


def run_public_eval_suite(config_path: str | Path) -> dict[str, Any]:
    base_config = load_config(config_path)
    bfcl_cases = _load_fixture('bfcl_subset.json')['cases']
    tau_cases = _load_fixture('tau2_mock_subset.json')['cases']
    records: list[PublicEvalRecord] = []
    for case in bfcl_cases:
        records.append(asyncio.run(_run_bfcl_case(base_config, case)))
    for case in tau_cases:
        records.append(asyncio.run(_run_tau_case(base_config, case)))
    return {
        'records': [asdict(record) for record in records],
        'summary': _aggregate_summary(records),
        'sources': {
            'bfcl': 'https://huggingface.co/datasets/gorilla-llm/Berkeley-Function-Calling-Leaderboard',
            'tau2': 'https://github.com/sierra-research/tau2-bench',
        },
    }

