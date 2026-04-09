from pathlib import Path

from agent_common.models import RunContext
from agent_integrations.guardrails import GuardrailEngine, GuardrailViolation
from agent_integrations.tool_validation import normalize_and_validate_tool_arguments


def build_context() -> RunContext:
    return RunContext(run_id='run-1', workdir=Path.cwd(), node_id=None)


def test_tool_validation_normalizes_numbers_and_booleans() -> None:
    result = normalize_and_validate_tool_arguments(
        {
            'type': 'object',
            'properties': {
                'count': {'type': 'integer'},
                'ratio': {'type': 'float'},
                'enabled': {'type': 'boolean'},
            },
            'required': ['count', 'ratio', 'enabled'],
        },
        {'count': '5', 'ratio': '3.5', 'enabled': 'true'},
    )

    assert result.errors == []
    assert result.normalized == {'count': 5, 'ratio': 3.5, 'enabled': True}


def test_tool_validation_reports_missing_required_arguments() -> None:
    result = normalize_and_validate_tool_arguments(
        {
            'type': 'object',
            'properties': {'count': {'type': 'integer'}},
            'required': ['count'],
        },
        {},
    )

    assert result.errors == ['missing required argument: count']


def test_tool_validation_accepts_required_nullable_arguments() -> None:
    result = normalize_and_validate_tool_arguments(
        {
            'type': 'object',
            'properties': {
                'count': {'type': 'integer'},
                'note': {'type': ['string', 'null']},
            },
            'required': ['count', 'note'],
            'additionalProperties': False,
        },
        {'count': 5, 'note': None},
    )

    assert result.errors == []
    assert result.normalized == {'count': 5, 'note': None}


def test_guardrail_blocks_shell_metacharacters_in_tool_input() -> None:
    engine = GuardrailEngine()
    decisions = engine.check_tool_input('command_echo', {'prompt': 'hello && shutdown now'}, build_context())

    assert decisions[0].outcome == 'block'
    try:
        engine.ensure_allowed('tool_input', decisions)
    except GuardrailViolation as exc:
        assert 'block_shell_metacharacters' in str(exc)
    else:
        raise AssertionError('expected guardrail violation')


def test_guardrail_blocks_secret_like_final_output() -> None:
    engine = GuardrailEngine()
    decisions = engine.check_final_output('DEEPSEEK_API_KEY=sk-abcdef1234567890', build_context())

    assert any(item.outcome == 'block' for item in decisions)


def test_guardrail_ignores_plain_text_tools_with_shell_like_punctuation() -> None:
    engine = GuardrailEngine()
    decisions = engine.check_tool_input('python_echo', {'prompt': 'Summarize alpha; beta; gamma.'}, build_context())

    assert decisions[0].outcome == 'allow'
