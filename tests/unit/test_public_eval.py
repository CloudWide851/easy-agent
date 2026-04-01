import httpx

from agent_config.app import AppConfig
from agent_runtime.public_eval import (
    PublicEvalRecord,
    _aggregate_failure_buckets,
    _aggregate_stage_summary,
    _build_tool_name_map,
    _classify_failure_bucket,
    _extract_tau_tasks_from_history,
    _is_retryable_provider_400,
    _normalize_schema,
    _provider_schema_matrix,
    _score_bfcl_case,
    _score_tau_case,
    _select_bfcl_candidate_functions,
    _strict_normalize_schema,
    _tau_history_memory_message,
    _tau_prompt_with_grounding,
)


def test_score_bfcl_case_accepts_exact_match() -> None:
    case = {
        'expect_no_tool': False,
        'ground_truth': [{'math.gcd': {'num1': [12], 'num2': [18]}}],
    }
    actual_calls = [{'name': 'math.gcd', 'arguments': {'num1': 12, 'num2': 18}}]

    success, tool_match, arg_match = _score_bfcl_case(case, actual_calls)

    assert success is True
    assert tool_match == 1.0
    assert arg_match == 1.0


def test_score_bfcl_case_handles_irrelevance() -> None:
    case = {'expect_no_tool': True, 'ground_truth': []}

    success, tool_match, arg_match = _score_bfcl_case(case, [])

    assert success is True
    assert tool_match == 1.0
    assert arg_match == 1.0


def test_score_tau_case_requires_expected_action() -> None:
    case = {
        'evaluation_criteria': {
            'actions': [{'name': 'update_task_status', 'arguments': {'task_id': 'task_1', 'status': 'completed'}}]
        }
    }
    actual_calls = [{'name': 'update_task_status', 'arguments': {'task_id': 'task_1', 'status': 'completed'}}]

    success, tool_match, arg_match = _score_tau_case(case, actual_calls)

    assert success is True
    assert tool_match == 1.0
    assert arg_match == 1.0


def test_build_tool_name_map_sanitizes_bfcl_function_names() -> None:
    mapping = _build_tool_name_map([
        {'name': 'math.factorial'},
        {'name': 'math/factorial'},
    ])

    assert mapping['math.factorial'] == 'math_factorial'
    assert mapping['math/factorial'] == 'math_factorial_2'


def test_normalize_schema_converts_non_openai_json_types() -> None:
    schema = _normalize_schema(
        {
            'type': 'dict',
            'properties': {
                'items': {
                    'type': 'tuple',
                    'items': {'type': 'dict', 'properties': {'count': {'type': 'integer'}}},
                },
                'rating': {'type': 'float', 'optional': True},
            },
        }
    )

    assert schema['type'] == 'object'
    assert schema['properties']['items']['type'] == 'array'
    assert schema['properties']['items']['items']['type'] == 'object'
    assert schema['properties']['rating']['type'] == 'number'
    assert 'optional' not in schema['properties']['rating']


def test_strict_normalize_schema_drops_non_core_fields() -> None:
    schema = _strict_normalize_schema(
        {
            'type': 'dict',
            'description': 'root',
            'properties': {
                'when': {'type': 'string', 'description': 'When', 'format': 'date-time'},
            },
            'required': ['when'],
            'additionalProperties': False,
        }
    )

    assert schema == {
        'type': 'object',
        'properties': {'when': {'type': 'string'}},
        'required': ['when'],
    }


def test_select_bfcl_candidate_functions_prunes_irrelevant_tools() -> None:
    prompt = 'Calculate the area of a triangle given the base is 10 meters and height is 5 meters.'
    functions = [
        {
            'name': 'determine_body_mass_index',
            'description': 'Calculate body mass index given weight and height.',
            'parameters': {'type': 'dict', 'properties': {'weight': {'type': 'float'}, 'height': {'type': 'float'}}},
        }
    ]

    assert _select_bfcl_candidate_functions(prompt, functions) == []


def test_select_bfcl_candidate_functions_keeps_multiple_high_relevance_tools() -> None:
    prompt = 'Find the area of a rectangle with length 7 and breadth 3. Also, calculate the area of a circle with radius 5.'
    functions = [
        {
            'name': 'volume_cylinder.calculate',
            'description': 'Calculate the volume of a cylinder given the radius and the height.',
            'parameters': {'type': 'dict', 'properties': {'radius': {'type': 'float'}, 'height': {'type': 'float'}}},
        },
        {
            'name': 'area_rectangle.calculate',
            'description': 'Calculate the area of a rectangle given the length and breadth.',
            'parameters': {'type': 'dict', 'properties': {'length': {'type': 'float'}, 'breadth': {'type': 'float'}}},
        },
        {
            'name': 'area_circle.calculate',
            'description': 'Calculate the area of a circle given the radius.',
            'parameters': {'type': 'dict', 'properties': {'radius': {'type': 'float'}}},
        },
    ]

    selected = _select_bfcl_candidate_functions(prompt, functions)

    assert [item['name'] for item in selected] == ['area_rectangle.calculate', 'area_circle.calculate']


def test_retryable_provider_400_checks_openai_compatible_provider() -> None:
    request = httpx.Request('POST', 'https://api.deepseek.com/chat/completions')
    response = httpx.Response(400, request=request)
    exc = httpx.HTTPStatusError('bad request', request=request, response=response)
    deepseek_config = AppConfig.model_validate(
        {'model': {'provider': 'deepseek'}, 'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}]}}
    )
    anthropic_config = AppConfig.model_validate(
        {'model': {'provider': 'anthropic'}, 'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}]}}
    )

    assert _is_retryable_provider_400(deepseek_config, exc) is True
    assert _is_retryable_provider_400(anthropic_config, exc) is False


def test_extract_tau_tasks_from_history_reads_tool_payloads() -> None:
    history = [
        {'role': 'user', 'content': 'create a task'},
        {
            'role': 'tool',
            'content': '{"task_id":"task_2","title":"Project Review","description":"Review Q4","status":"pending"}',
        },
    ]

    tasks = _extract_tau_tasks_from_history(history)

    assert tasks['task_2']['title'] == 'Project Review'
    assert tasks['task_2']['status'] == 'pending'


def test_tau_history_memory_message_summarizes_known_tasks() -> None:
    message = _tau_history_memory_message(
        {'task_2': {'task_id': 'task_2', 'title': 'Project Review', 'description': 'Review Q4', 'status': 'pending'}}
    )

    assert message is not None
    assert 'task_2' in message
    assert 'Project Review' in message
    assert 'Default singular references' in message


def test_tau_prompt_with_grounding_marks_recent_task_as_default_reference() -> None:
    prompt = _tau_prompt_with_grounding(
        'Please mark the task as completed.',
        {
            'task_1': {'task_id': 'task_1', 'title': 'Old', 'description': '', 'status': 'pending'},
            'task_2': {'task_id': 'task_2', 'title': 'Project Review', 'description': 'Review Q4', 'status': 'pending'},
        },
    )

    assert 'Most recent discussed task: task_2' in prompt
    assert 'Default singular follow-up references map to task_2.' in prompt


def test_provider_schema_matrix_reflects_adapter_behavior() -> None:
    matrix = _provider_schema_matrix()

    assert matrix['openai_compatible']['features']['root_object_alias']['supported'] is True
    assert matrix['gemini']['features']['format_removed']['supported'] is True
    assert matrix['anthropic']['features']['root_object_alias']['supported'] is False
    assert matrix['anthropic']['features']['invalid_required_pruned']['supported'] is False


def test_aggregate_stage_summary_counts_transitions_and_recoveries() -> None:
    records = [
        PublicEvalRecord(
            suite='bfcl_simple',
            case_id='simple_0',
            success=True,
            duration_seconds=1.0,
            tool_name_match=1.0,
            argument_match=1.0,
            expected_call_count=1,
            actual_call_count=1,
            result_summary='ok',
            fallback_stage='base',
            fallback_attempts=['base'],
        ),
        PublicEvalRecord(
            suite='bfcl_simple',
            case_id='simple_1',
            success=True,
            duration_seconds=1.0,
            tool_name_match=1.0,
            argument_match=1.0,
            expected_call_count=1,
            actual_call_count=1,
            result_summary='ok',
            fallback_stage='candidate_pruned_retry',
            fallback_attempts=['base', 'strict_schema_retry', 'candidate_pruned_retry'],
        ),
    ]

    summary = _aggregate_stage_summary(records)

    assert summary['stages']['base']['entered_runs'] == 2
    assert summary['stages']['candidate_pruned_retry']['recovered_cases'] == 1
    assert summary['transitions']['base->strict_schema_retry'] == 1


def test_failure_bucket_classification_handles_duplicate_and_history_cases() -> None:
    duplicate = PublicEvalRecord(
        suite='bfcl_parallel_multiple',
        case_id='parallel_multiple_3',
        success=False,
        duration_seconds=1.0,
        tool_name_match=0.0,
        argument_match=0.0,
        expected_call_count=2,
        actual_call_count=3,
        result_summary='',
        error='{"actual_calls": [{"name": "get_rectangle_property", "arguments": {"perimeter": 14, "area": 15, "property": "length"}}, {"name": "get_rectangle_property", "arguments": {"perimeter": 14, "area": 15, "property": "width"}}, {"name": "get_rectangle_property", "arguments": {"perimeter": 14, "area": 15, "property": "length", "tolerance": 0.1}}]}',
    )
    history = PublicEvalRecord(
        suite='tau2_mock',
        case_id='update_task_with_message_history',
        success=False,
        duration_seconds=1.0,
        tool_name_match=0.0,
        argument_match=0.0,
        expected_call_count=1,
        actual_call_count=0,
        result_summary='',
        error='{"actual_calls": []}',
    )

    assert _classify_failure_bucket(duplicate) == 'duplicate_call'
    assert _classify_failure_bucket(history) == 'history_grounding_miss'

    duplicate.failure_bucket = _classify_failure_bucket(duplicate)
    history.failure_bucket = _classify_failure_bucket(history)
    buckets = _aggregate_failure_buckets([duplicate, history])

    assert buckets['duplicate_call']['count'] == 1
    assert buckets['history_grounding_miss']['count'] == 1
