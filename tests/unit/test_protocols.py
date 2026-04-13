from typing import cast

import httpx
import pytest

from agent_common.models import ChatMessage, Protocol, ToolSpec
from agent_config.app import ModelConfig
from agent_protocols.client import AnthropicAdapter, GeminiAdapter, OpenAIAdapter, resolve_protocol


def test_auto_protocol_prefers_openai_for_deepseek() -> None:
    config = ModelConfig(provider='deepseek', protocol=Protocol.AUTO)

    assert resolve_protocol(config).protocol is Protocol.OPENAI


def test_anthropic_adapter_parses_tool_use() -> None:
    adapter = AnthropicAdapter()
    response = adapter.parse_response(
        {
            'content': [
                {'type': 'text', 'text': 'working'},
                {'type': 'tool_use', 'id': 'call_1', 'name': 'python_echo', 'input': {'prompt': 'hi'}},
            ]
        }
    )

    assert response.text == 'working'
    assert response.tool_calls[0].name == 'python_echo'


def test_anthropic_adapter_normalizes_schema_when_strict_enabled() -> None:
    adapter = AnthropicAdapter()
    payload = adapter.build_payload(
        ModelConfig(provider='anthropic', protocol=Protocol.ANTHROPIC),
        [ChatMessage(role='user', content='hello')],
        [
            ToolSpec(
                name='complex_tool',
                description='Complex',
                input_schema={
                    'type': 'dict',
                    'properties': {
                        'value': {'anyOf': [{'type': 'string'}, {'type': 'integer'}]},
                        'nickname': {'type': 'string', 'nullable': True},
                        'amount': {'type': 'float', 'optional': True},
                    },
                    'required': ['missing'],
                },
            )
        ],
    )

    schema = payload['tools'][0]['input_schema']
    assert payload['tools'][0]['strict'] is True
    assert schema['type'] == 'object'
    assert schema['additionalProperties'] is False
    assert schema['properties']['value']['type'] == 'string'
    assert schema['properties']['nickname']['type'] == ['string', 'null']
    assert schema['properties']['amount']['type'] == ['number', 'null']
    assert set(schema['required']) == {'value', 'nickname', 'amount'}


def test_gemini_builds_function_declarations() -> None:
    adapter = GeminiAdapter()
    payload = adapter.build_payload(
        ModelConfig(provider='gemini', protocol=Protocol.GEMINI),
        [ChatMessage(role='user', content='hello')],
        [ToolSpec(name='python_echo', description='Echo', input_schema={'type': 'object'})],
    )

    assert payload['tools'][0]['functionDeclarations'][0]['name'] == 'python_echo'


def test_gemini_adapter_sanitizes_schema_like_openai_path() -> None:
    adapter = GeminiAdapter()
    payload = adapter.build_payload(
        ModelConfig(provider='gemini', protocol=Protocol.GEMINI),
        [ChatMessage(role='user', content='hello')],
        [
            ToolSpec(
                name='complex_tool',
                description='Complex',
                input_schema={
                    'type': 'dict',
                    'properties': {
                        'items': {
                            'type': 'tuple',
                            'items': {'type': 'dict', 'properties': {'value': {'type': 'integer'}}},
                        },
                        'value': {'anyOf': [{'type': 'string', 'format': 'binary'}, {'type': 'integer'}]},
                    },
                    'required': ['items', 'missing'],
                },
            )
        ],
    )

    schema = payload['tools'][0]['functionDeclarations'][0]['parameters']
    assert schema['type'] == 'object'
    assert schema['additionalProperties'] is False
    assert schema['properties']['items']['type'] == 'array'
    assert schema['properties']['value']['type'] == 'string'
    assert set(schema['required']) == {'items', 'value'}


def test_openai_parses_tool_calls() -> None:
    adapter = OpenAIAdapter()
    response = adapter.parse_response(
        {
            'choices': [
                {
                    'message': {
                        'content': '',
                        'tool_calls': [
                            {
                                'id': 'call_1',
                                'function': {'name': 'command_echo', 'arguments': '{"prompt":"hello"}'},
                            }
                        ],
                    }
                }
            ]
        }
    )

    assert response.tool_calls[0].arguments['prompt'] == 'hello'


def test_openai_adapter_sanitizes_non_standard_schema_types() -> None:
    adapter = OpenAIAdapter()
    payload = adapter.build_payload(
        ModelConfig(provider='deepseek', protocol=Protocol.OPENAI),
        [ChatMessage(role='user', content='hello')],
        [
            ToolSpec(
                name='complex_tool',
                description='Complex',
                input_schema={
                    'type': 'dict',
                    'properties': {
                        'items': {
                            'type': 'tuple',
                            'items': {'type': 'dict', 'properties': {'value': {'type': 'integer'}}},
                        },
                        'value': {
                            'anyOf': [
                                {'type': 'string', 'format': 'binary'},
                                {'type': 'integer'},
                                {'type': 'null'},
                            ]
                        },
                        'amount': {'type': 'float', 'optional': True},
                        'nickname': {'type': 'string', 'nullable': True},
                        'params': {
                            'type': 'array',
                            'items': {'type': ['string', 'number', 'boolean', 'null']},
                        },
                    },
                    'required': ['items', 'missing'],
                    'examples': ['drop-me'],
                },
            )
        ],
    )

    schema = payload['tools'][0]['function']['parameters']
    assert schema['type'] == 'object'
    assert payload['tools'][0]['function']['strict'] is True
    assert payload['parallel_tool_calls'] is True
    assert schema['additionalProperties'] is False
    assert schema['properties']['items']['type'] == 'array'
    assert schema['properties']['items']['items']['type'] == 'object'
    assert schema['properties']['value']['type'] == 'string'
    assert 'format' not in schema['properties']['value']
    assert schema['properties']['params']['items']['type'] == ['string', 'null']
    assert schema['properties']['amount']['type'] == ['number', 'null']
    assert schema['properties']['nickname']['type'] == ['string', 'null']
    assert 'optional' not in schema['properties']['amount']
    assert set(schema['required']) == {'items', 'value', 'amount', 'nickname', 'params'}
    assert 'examples' not in schema


def test_openai_adapter_can_disable_strict_and_parallel_tool_calls() -> None:
    adapter = OpenAIAdapter()
    payload = adapter.build_payload(
        ModelConfig.model_validate(
            {
                'provider': 'deepseek',
                'protocol': Protocol.OPENAI,
                'function_calling': {'strict': False, 'parallel_tool_calls': False},
            }
        ),
        [ChatMessage(role='user', content='hello')],
        [
            ToolSpec(
                name='simple_tool',
                description='Simple',
                input_schema={
                    'type': 'object',
                    'properties': {'value': {'type': 'string', 'optional': True}},
                },
            )
        ],
    )

    function = payload['tools'][0]['function']
    assert 'strict' not in function
    assert payload['parallel_tool_calls'] is False
    assert function['parameters']['properties']['value']['type'] == 'string'
    assert function['parameters'].get('additionalProperties') is None


def test_openai_adapter_supports_required_and_forced_tool_choice() -> None:
    adapter = OpenAIAdapter()
    required_payload = adapter.build_payload(
        ModelConfig.model_validate(
            {
                'provider': 'deepseek',
                'protocol': Protocol.OPENAI,
                'function_calling': {'mode': 'required'},
            }
        ),
        [ChatMessage(role='user', content='hello')],
        [ToolSpec(name='simple_tool', description='Simple', input_schema={'type': 'object'})],
    )
    forced_payload = adapter.build_payload(
        ModelConfig.model_validate(
            {
                'provider': 'deepseek',
                'protocol': Protocol.OPENAI,
                'function_calling': {'mode': 'force', 'forced_tool_name': 'simple_tool'},
            }
        ),
        [ChatMessage(role='user', content='hello')],
        [ToolSpec(name='simple_tool', description='Simple', input_schema={'type': 'object'})],
    )

    assert required_payload['tool_choice'] == 'required'
    assert forced_payload['tool_choice']['function']['name'] == 'simple_tool'


def test_anthropic_adapter_supports_tool_choice_and_serial_mode() -> None:
    adapter = AnthropicAdapter()
    payload = adapter.build_payload(
        ModelConfig.model_validate(
            {
                'provider': 'anthropic',
                'protocol': Protocol.ANTHROPIC,
                'function_calling': {
                    'mode': 'force',
                    'forced_tool_name': 'complex_tool',
                    'parallel_tool_calls': False,
                },
            }
        ),
        [ChatMessage(role='user', content='hello')],
        [ToolSpec(name='complex_tool', description='Complex', input_schema={'type': 'object'})],
    )

    assert payload['tool_choice'] == {'type': 'tool', 'name': 'complex_tool'}
    assert payload['disable_parallel_tool_use'] is True


def test_gemini_adapter_supports_function_calling_mode_controls() -> None:
    adapter = GeminiAdapter()
    payload = adapter.build_payload(
        ModelConfig.model_validate(
            {
                'provider': 'gemini',
                'protocol': Protocol.GEMINI,
                'function_calling': {'mode': 'force', 'forced_tool_name': 'python_echo'},
            }
        ),
        [ChatMessage(role='user', content='hello')],
        [ToolSpec(name='python_echo', description='Echo', input_schema={'type': 'object'})],
    )

    function_config = payload['toolConfig']['functionCallingConfig']
    assert function_config['mode'] == 'ANY'
    assert function_config['allowedFunctionNames'] == ['python_echo']


def test_allowed_tool_names_filter_payload_tools_across_protocols() -> None:
    messages = [ChatMessage(role='user', content='hello')]
    tools = [
        ToolSpec(name='python_echo', description='Echo', input_schema={'type': 'object'}),
        ToolSpec(name='weather_lookup', description='Weather', input_schema={'type': 'object'}),
    ]

    openai_payload = OpenAIAdapter().build_payload(
        ModelConfig.model_validate(
            {
                'provider': 'deepseek',
                'protocol': Protocol.OPENAI,
                'function_calling': {'allowed_tool_names': ['weather_lookup']},
            }
        ),
        messages,
        tools,
    )
    anthropic_payload = AnthropicAdapter().build_payload(
        ModelConfig.model_validate(
            {
                'provider': 'anthropic',
                'protocol': Protocol.ANTHROPIC,
                'function_calling': {'allowed_tool_names': ['weather_lookup']},
            }
        ),
        messages,
        tools,
    )
    gemini_payload = GeminiAdapter().build_payload(
        ModelConfig.model_validate(
            {
                'provider': 'gemini',
                'protocol': Protocol.GEMINI,
                'function_calling': {'allowed_tool_names': ['weather_lookup'], 'mode': 'required'},
            }
        ),
        messages,
        tools,
    )

    assert [item['function']['name'] for item in openai_payload['tools']] == ['weather_lookup']
    assert [item['name'] for item in anthropic_payload['tools']] == ['weather_lookup']
    assert [item['name'] for item in gemini_payload['tools'][0]['functionDeclarations']] == ['weather_lookup']
    assert gemini_payload['toolConfig']['functionCallingConfig']['allowedFunctionNames'] == ['weather_lookup']


class _RuntimeErrorClosingClient:
    async def aclose(self) -> None:
        raise RuntimeError('Event loop is closed')


class _UnexpectedClosingClient:
    async def aclose(self) -> None:
        raise RuntimeError('different close failure')


@pytest.mark.asyncio
async def test_http_model_client_aclose_ignores_windows_event_loop_cleanup_error() -> None:
    from agent_protocols.client import HttpModelClient

    client = HttpModelClient(
        ModelConfig(provider='deepseek', protocol=Protocol.OPENAI),
        client=cast(httpx.AsyncClient, _RuntimeErrorClosingClient()),
    )

    await client.aclose()


@pytest.mark.asyncio
async def test_http_model_client_aclose_re_raises_other_runtime_errors() -> None:
    from agent_protocols.client import HttpModelClient

    client = HttpModelClient(
        ModelConfig(provider='deepseek', protocol=Protocol.OPENAI),
        client=cast(httpx.AsyncClient, _UnexpectedClosingClient()),
    )

    with pytest.raises(RuntimeError, match='different close failure'):
        await client.aclose()
