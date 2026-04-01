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


def test_anthropic_adapter_keeps_schema_passthrough() -> None:
    adapter = AnthropicAdapter()
    payload = adapter.build_payload(
        ModelConfig(provider='anthropic', protocol=Protocol.ANTHROPIC),
        [ChatMessage(role='user', content='hello')],
        [
            ToolSpec(
                name='complex_tool',
                description='Complex',
                input_schema={'type': 'dict', 'properties': {'value': {'anyOf': [{'type': 'string'}, {'type': 'integer'}]}}},
            )
        ],
    )

    schema = payload['tools'][0]['input_schema']
    assert schema['type'] == 'dict'
    assert 'anyOf' in schema['properties']['value']


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
    assert schema['properties']['items']['type'] == 'array'
    assert schema['properties']['value']['type'] == 'string'
    assert schema['required'] == ['items']


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
    assert schema['properties']['items']['type'] == 'array'
    assert schema['properties']['items']['items']['type'] == 'object'
    assert schema['properties']['value']['type'] == 'string'
    assert 'format' not in schema['properties']['value']
    assert schema['properties']['params']['items']['type'] == 'string'
    assert schema['properties']['amount']['type'] == 'number'
    assert 'optional' not in schema['properties']['amount']
    assert schema['required'] == ['items']
    assert 'examples' not in schema


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
