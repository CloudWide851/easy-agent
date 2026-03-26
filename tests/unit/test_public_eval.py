from agent_runtime.public_eval import (
    _build_tool_name_map,
    _normalize_schema,
    _score_bfcl_case,
    _score_tau_case,
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
                }
            },
        }
    )

    assert schema['type'] == 'object'
    assert schema['properties']['items']['type'] == 'array'
    assert schema['properties']['items']['items']['type'] == 'object'
