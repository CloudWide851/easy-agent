import json
from pathlib import Path

import httpx
import pytest
import respx

from agent_config.app import AppConfig
from agent_runtime.public_eval import (
    PublicEvalRecord,
    WebSearchQuotaExceeded,
    _aggregate_agentic_summary,
    _aggregate_failure_buckets,
    _aggregate_stage_summary,
    _aggregate_summary,
    _build_eval_tool_handler,
    _build_tool_name_map,
    _classify_failure_bucket,
    _extract_tau_tasks_from_history,
    _fetch_web_contents,
    _is_retryable_provider_400,
    _load_official_full_v4_inputs,
    _load_public_eval_inputs,
    _normalize_schema,
    _normalize_serpapi_search_results,
    _provider_schema_matrix,
    _record_web_search_usage,
    _restore_checkpoint_records,
    _save_public_eval_checkpoint,
    _score_bfcl_case,
    _score_tau_case,
    _select_bfcl_candidate_functions,
    _serpapi_query_params,
    _serpapi_search,
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
                'note': {'type': 'string', 'optional': True},
            },
            'required': ['when'],
            'additionalProperties': False,
        }
    )

    assert schema == {
        'type': 'object',
        'properties': {
            'when': {'type': 'string'},
            'note': {'type': ['string', 'null']},
        },
        'required': ['when', 'note'],
        'additionalProperties': False,
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
    assert matrix['openai_compatible']['features']['strict_flag']['supported'] is True
    assert matrix['openai_compatible']['features']['additional_properties_false']['supported'] is True
    assert matrix['openai_compatible']['features']['nullable_preserved']['supported'] is True
    assert matrix['openai_compatible']['features']['optional_promoted_to_required_nullable']['supported'] is True
    assert matrix['openai_compatible']['features']['parallel_tool_calls_control']['supported'] is True
    assert matrix['openai_compatible']['features']['single_tool_call_control']['supported'] is True
    assert matrix['openai_compatible']['features']['tool_choice_required']['supported'] is True
    assert matrix['openai_compatible']['features']['forced_tool_choice']['supported'] is True
    assert matrix['gemini']['features']['format_removed']['supported'] is True
    assert matrix['gemini']['features']['tool_choice_none']['supported'] is True
    assert matrix['gemini']['features']['forced_tool_choice']['supported'] is True
    assert matrix['anthropic']['features']['root_object_alias']['supported'] is False
    assert matrix['anthropic']['features']['invalid_required_pruned']['supported'] is False
    assert matrix['anthropic']['features']['strict_flag']['supported'] is False
    assert matrix['anthropic']['features']['single_tool_call_control']['supported'] is True


def test_load_public_eval_inputs_expands_full_v4_profile() -> None:
    config = AppConfig.model_validate(
        {
            'graph': {
                'entrypoint': 'coordinator',
                'agents': [{'name': 'coordinator'}],
                'nodes': [],
            },
            'evaluation': {'public_eval': {'profile': 'full_v4'}},
        }
    )

    profile, bfcl_version, bfcl_cases, tau_cases = _load_public_eval_inputs(config)

    assert profile == 'full_v4'
    assert bfcl_version == 'v4'
    assert any(case['suite'] == 'web_search' for case in bfcl_cases)
    assert any(case['suite'] == 'memory' for case in bfcl_cases)
    assert any(case['suite'] == 'format_sensitivity' for case in bfcl_cases)
    assert tau_cases


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


def test_failure_bucket_classification_handles_agentic_v4_cases() -> None:
    search = PublicEvalRecord(
        suite='bfcl_web_search',
        case_id='web_search_0',
        success=False,
        duration_seconds=1.0,
        tool_name_match=0.0,
        argument_match=0.0,
        expected_call_count=1,
        actual_call_count=0,
        result_summary='',
        error='missing SERPAPI_API_KEY for BFCL web search evaluation',
    )
    memory = PublicEvalRecord(
        suite='bfcl_memory',
        case_id='memory_0',
        success=False,
        duration_seconds=1.0,
        tool_name_match=0.0,
        argument_match=0.0,
        expected_call_count=1,
        actual_call_count=0,
        result_summary='',
        error='memory miss',
    )
    format_case = PublicEvalRecord(
        suite='bfcl_format_sensitivity',
        case_id='format_0',
        success=False,
        duration_seconds=1.0,
        tool_name_match=0.0,
        argument_match=0.0,
        expected_call_count=1,
        actual_call_count=0,
        result_summary='',
        error='format mismatch',
    )
    answer_miss = PublicEvalRecord(
        suite='bfcl_web_search',
        case_id='web_search_1',
        success=False,
        duration_seconds=1.0,
        tool_name_match=1.0,
        argument_match=1.0,
        expected_call_count=1,
        actual_call_count=1,
        result_summary='wrong answer',
        answer_match=0.0,
        error='{"actual_calls": [{"name":"web.search","arguments":{"query":"OpenAI Structured Outputs guide"}}]}',
    )

    assert _classify_failure_bucket(search) == 'search_tool_miss'
    assert _classify_failure_bucket(memory) == 'memory_backend_miss'
    assert _classify_failure_bucket(format_case) == 'format_variant_miss'
    assert _classify_failure_bucket(answer_miss) == 'answer_grounding_miss'


def test_aggregate_agentic_summary_counts_v4_suites() -> None:
    summary = _aggregate_agentic_summary(
        [
            PublicEvalRecord('bfcl_web_search', 'a', True, 1.0, 1.0, 1.0, 1, 1, 'ok', answer_match=1.0),
            PublicEvalRecord('bfcl_memory', 'b', False, 1.0, 0.0, 0.0, 1, 0, ''),
            PublicEvalRecord('bfcl_format_sensitivity', 'c', True, 1.0, 1.0, 1.0, 1, 1, 'ok'),
        ]
    )

    assert summary['bfcl_web_search']['pass_rate'] == 1.0
    assert summary['bfcl_web_search']['answer_match_rate'] == 1.0
    assert summary['bfcl_memory']['failures'] == 1


def test_aggregate_summary_uses_bfcl_subcategory_accuracy_for_headline() -> None:
    summary = _aggregate_summary(
        [
            PublicEvalRecord('bfcl_simple', 'simple_0', True, 1.0, 1.0, 1.0, 1, 1, 'ok'),
            PublicEvalRecord('bfcl_simple', 'simple_1', True, 1.0, 1.0, 1.0, 1, 1, 'ok'),
            PublicEvalRecord('bfcl_web_search', 'web_0', False, 1.0, 1.0, 1.0, 1, 1, 'wrong', answer_match=0.0),
            PublicEvalRecord('tau2_mock', 'tau_0', True, 1.0, 1.0, 1.0, 1, 1, 'ok'),
        ]
    )

    assert summary['overall']['bfcl_case_pass_rate'] == 0.6667
    assert summary['overall']['bfcl_subcategory_accuracy'] == 0.5
    assert summary['overall']['bfcl_answer_match_rate'] == 0.6667


def test_load_public_eval_inputs_supports_official_profile(tmp_path: Path) -> None:
    manifest_path = tmp_path / 'bfcl_v4_manifest.json'
    manifest_path.write_text(
        json.dumps(
            {
                'categories': {
                    'web_search': {
                        'cases': [
                            {
                                'id': 'official_web_0',
                                'suite': 'web_search',
                                'messages': [{'role': 'user', 'content': 'Search the web.'}],
                                'functions': [],
                                'ground_truth': [],
                                'expect_no_tool': True,
                            }
                        ]
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )
    config = AppConfig.model_validate(
        {
            'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'nodes': []},
            'evaluation': {
                'public_eval': {
                    'profile': 'official_full_v4',
                    'official_dataset': {'manifest_path': str(manifest_path)},
                }
            },
        }
    )

    profile, bfcl_version, bfcl_cases, tau_cases = _load_public_eval_inputs(config)

    assert profile == 'official_full_v4'
    assert bfcl_version == 'v4'
    assert bfcl_cases[0]['id'] == 'official_web_0'
    assert tau_cases


def test_normalize_serpapi_search_results_keeps_title_link_and_text() -> None:
    results = _normalize_serpapi_search_results(
        {
            'organic_results': [
                {'title': 'Structured Outputs', 'link': 'https://example.com', 'snippet': 'Schema constrained output.'}
            ]
        },
        num_results=3,
    )

    assert results == [
        {
            'position': 1,
            'title': 'Structured Outputs',
            'link': 'https://example.com',
            'source': 'example.com',
            'snippet': 'Schema constrained output.',
        }
    ]


@respx.mock
def test_serpapi_search_uses_api_and_normalizes_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SERPAPI_API_KEY', 'test-key')
    route = respx.get('https://serpapi.example/search.json').mock(
        return_value=httpx.Response(
            200,
            json={'organic_results': [{'title': 'OpenAI', 'link': 'https://platform.openai.com', 'snippet': 'Docs'}]},
        )
    )
    result = _serpapi_search(
        {'query': 'OpenAI Structured Outputs', 'num_results': 3},
        {'replay_results': []},
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'nodes': []},
                'evaluation': {
                    'public_eval': {
                        'web_search': {
                            'endpoint_url': 'https://serpapi.example/search.json',
                            'usage_path': str(tmp_path / 'usage.json'),
                            'api_key_env': 'SERPAPI_API_KEY',
                        }
                    }
                },
            }
        ).evaluation.public_eval.web_search,
    )

    assert route.called is True
    assert result['backend'] == 'serpapi'
    assert result['query'] == 'OpenAI Structured Outputs'
    assert result['results'][0]['link'] == 'https://platform.openai.com'
    assert result['results'][0]['position'] == 1


def test_serpapi_query_params_shapes_query_from_bfcl_wrapper() -> None:
    config = AppConfig.model_validate(
        {
            'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'nodes': []},
            'evaluation': {'public_eval': {'web_search': {'usage_path': 'usage.json'}}},
        }
    ).evaluation.public_eval.web_search

    params = _serpapi_query_params(
        {'query': 'Search the web for OpenAI structured outputs latest notes', 'num_results': 20},
        {'messages': [{'role': 'user', 'content': 'fallback prompt'}]},
        config,
    )

    assert params['q'] == 'OpenAI structured outputs latest notes'
    assert params['num'] == 10


@respx.mock
def test_fetch_web_contents_uses_http_fetch_and_normalizes_response(tmp_path: Path) -> None:
    route = respx.get('https://example.com/doc').mock(
        return_value=httpx.Response(
            200,
            text='<html><body><main>Body</main></body></html>',
            headers={'content-type': 'text/html; charset=utf-8'},
        )
    )
    result = _fetch_web_contents(
        {'urls': ['https://example.com/doc']},
        {'replay_contents': []},
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'nodes': []},
                'evaluation': {'public_eval': {'web_search': {'usage_path': str(tmp_path / 'usage.json')}}},
            }
        ).evaluation.public_eval.web_search,
    )

    assert route.called is True
    assert result['results'][0]['text'] == 'Body'


def test_fetch_web_contents_rejects_ungrounded_urls(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'nodes': []},
            'evaluation': {'public_eval': {'web_search': {'usage_path': str(tmp_path / 'usage.json')}}},
        }
    ).evaluation.public_eval.web_search

    with pytest.raises(RuntimeError, match='grounded urls'):
        _fetch_web_contents(
            {'urls': ['https://example.com/doc']},
            {'replay_contents': []},
            config,
            grounded_urls={'https://example.com/allowed'},
        )


@respx.mock
def test_serpapi_search_replays_when_service_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SERPAPI_API_KEY', 'test-key')
    respx.get('https://serpapi.example/search.json').mock(
        return_value=httpx.Response(503, text='service unavailable')
    )
    config = AppConfig.model_validate(
        {
            'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'nodes': []},
            'evaluation': {
                'public_eval': {
                    'web_search': {
                        'endpoint_url': 'https://serpapi.example/search.json',
                        'usage_path': str(tmp_path / 'usage.json'),
                        'api_key_env': 'SERPAPI_API_KEY',
                    }
                }
            },
        }
    )

    result = _serpapi_search(
        {'query': 'OpenAI', 'num_results': 1},
        {'replay_results': [{'title': 'Replay', 'link': 'https://example.com', 'snippet': 'cached'}]},
        config.evaluation.public_eval.web_search,
    )

    assert result['backend'] == 'service_unavailable_replay'
    assert result['results'][0]['title'] == 'Replay'


def test_serpapi_search_replays_when_api_key_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('SERPAPI_API_KEY', raising=False)
    config = AppConfig.model_validate(
        {
            'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'nodes': []},
            'evaluation': {'public_eval': {'web_search': {'usage_path': str(tmp_path / 'usage.json')}}},
        }
    )

    result = _serpapi_search(
        {'query': 'OpenAI', 'num_results': 2},
        {'replay_results': [{'title': 'Replay', 'link': 'https://example.com', 'snippet': 'cached'}]},
        config.evaluation.public_eval.web_search,
    )

    assert result['backend'] == 'replay'
    assert result['results'][0]['title'] == 'Replay'


def test_record_web_search_usage_enforces_quota(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'nodes': []},
            'evaluation': {
                'public_eval': {
                    'web_search': {
                        'usage_path': str(tmp_path / 'usage.json'),
                        'hourly_limit': 1,
                        'daily_limit': 1,
                        'quota_policy': 'resume_later',
                    }
                }
            },
        }
    ).evaluation.public_eval.web_search

    _record_web_search_usage(config, kind='search', now=100.0)
    try:
        _record_web_search_usage(config, kind='search', now=101.0)
    except WebSearchQuotaExceeded as exc:
        assert exc.wait_seconds > 0
    else:
        raise AssertionError('expected quota exhaustion')


def test_build_eval_tool_handler_blocks_calls_beyond_bfcl_budget(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'nodes': []},
            'evaluation': {'public_eval': {'web_search': {'usage_path': str(tmp_path / 'usage.json')}}},
        }
    ).evaluation.public_eval.web_search
    handler = _build_eval_tool_handler(
        {'replay_results': [{'title': 'Replay', 'link': 'https://example.com', 'snippet': 'cached'}], 'ground_truth': [{}]},
        'web.search',
        'web_search',
        web_search=config,
        memory_state={},
        budget_state={'allowed_calls': 1, 'successful_calls': 1},
        search_state={'latest_results': []},
    )

    class _Context:
        run_id = 'run-1'

    with pytest.raises(RuntimeError, match='budget exhausted'):
        handler({'query': 'OpenAI'}, _Context())


def test_public_eval_checkpoint_round_trip_restores_records(tmp_path: Path) -> None:
    checkpoint = tmp_path / 'progress.json'
    records = [PublicEvalRecord('bfcl_web_search', 'case_1', True, 1.0, 1.0, 1.0, 1, 1, 'ok')]

    _save_public_eval_checkpoint(
        checkpoint,
        profile='full_v4',
        bfcl_version='v4',
        records=records,
        run_status='completed',
    )
    restored = _restore_checkpoint_records(checkpoint, profile='full_v4', bfcl_version='v4')

    assert 'bfcl_web_search:case_1' in restored
    assert restored['bfcl_web_search:case_1'].success is True


def test_load_official_full_v4_inputs_reads_manifest_path(tmp_path: Path) -> None:
    manifest_path = tmp_path / 'bfcl_v4_manifest.json'
    manifest_path.write_text(
        json.dumps(
            {
                'bfcl_cases': [
                    {
                        'id': 'official_memory_0',
                        'suite': 'memory',
                        'messages': [{'role': 'user', 'content': 'Remember this.'}],
                        'functions': [],
                        'ground_truth': [],
                        'expect_no_tool': True,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )
    config = AppConfig.model_validate(
        {
            'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'nodes': []},
            'evaluation': {'public_eval': {'official_dataset': {'manifest_path': str(manifest_path)}}},
        }
    )

    bfcl_cases, tau_cases = _load_official_full_v4_inputs(config)

    assert bfcl_cases[0]['suite'] == 'memory'
    assert tau_cases
