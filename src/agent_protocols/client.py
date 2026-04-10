from __future__ import annotations

import json
import os
from typing import Any
from typing import Protocol as TypingProtocol

import httpx
from tenacity import AsyncRetrying, stop_after_attempt, wait_fixed

from agent_common.models import AssistantResponse, ChatMessage, Protocol, ToolCall, ToolSpec
from agent_common.schema_utils import normalize_json_schema
from agent_config.app import ModelConfig


class ProtocolAdapter(TypingProtocol):
    protocol: Protocol

    def matches(self, config: ModelConfig) -> bool: ...

    def endpoint(self, config: ModelConfig) -> str: ...

    def headers(self, config: ModelConfig, api_key: str) -> dict[str, str]: ...

    def build_payload(
        self,
        config: ModelConfig,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
    ) -> dict[str, Any]: ...

    def parse_response(self, payload: dict[str, Any]) -> AssistantResponse: ...


def _anthropic_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        'name': tool.name,
        'description': tool.description,
        'input_schema': tool.input_schema,
    }


def _openai_safe_schema(schema: dict[str, Any], *, strict: bool = False) -> dict[str, Any]:
    return normalize_json_schema(schema, strict=strict)


def _selected_tools(config: ModelConfig, tools: list[ToolSpec]) -> list[ToolSpec]:
    function_calling = config.function_calling
    selected = list(tools)
    if function_calling.allowed_tool_names:
        allowed = set(function_calling.allowed_tool_names)
        selected = [tool for tool in selected if tool.name in allowed]
    if function_calling.mode == 'force' and function_calling.forced_tool_name:
        selected = [tool for tool in selected if tool.name == function_calling.forced_tool_name]
    return selected


class OpenAIAdapter:
    protocol = Protocol.OPENAI

    def matches(self, config: ModelConfig) -> bool:
        provider = config.provider.lower()
        return any(token in provider for token in ('openai', 'deepseek', 'compatible'))

    def endpoint(self, config: ModelConfig) -> str:
        return f"{config.base_url.rstrip('/')}/chat/completions"

    def headers(self, config: ModelConfig, api_key: str) -> dict[str, str]:
        return {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            **config.extra_headers,
        }

    def build_payload(
        self,
        config: ModelConfig,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
    ) -> dict[str, Any]:
        selected_tools = _selected_tools(config, tools)
        payload_messages: list[dict[str, Any]] = []
        for message in messages:
            item: dict[str, Any] = {'role': message.role, 'content': message.content}
            if message.name:
                item['name'] = message.name
            if message.tool_call_id:
                item['tool_call_id'] = message.tool_call_id
            if message.tool_calls:
                item['tool_calls'] = [
                    {
                        'id': tool_call.id,
                        'type': 'function',
                        'function': {
                            'name': tool_call.name,
                            'arguments': json.dumps(tool_call.arguments),
                        },
                    }
                    for tool_call in message.tool_calls
                ]
            payload_messages.append(item)

        payload: dict[str, Any] = {
            'model': config.model,
            'messages': payload_messages,
            'temperature': config.temperature,
            'max_tokens': config.max_tokens,
        }
        if selected_tools:
            function_calling = config.function_calling
            payload['tools'] = [
                {
                    'type': 'function',
                    'function': {
                        'name': tool.name,
                        'description': tool.description,
                        'parameters': _openai_safe_schema(tool.input_schema, strict=function_calling.strict),
                        **({'strict': True} if function_calling.strict else {}),
                    },
                }
                for tool in selected_tools
            ]
            payload['parallel_tool_calls'] = function_calling.parallel_tool_calls
            if function_calling.mode == 'none':
                payload['tool_choice'] = 'none'
            elif function_calling.mode == 'required':
                payload['tool_choice'] = 'required'
            elif function_calling.mode == 'force' and function_calling.forced_tool_name:
                payload['tool_choice'] = {
                    'type': 'function',
                    'function': {'name': function_calling.forced_tool_name},
                }
            else:
                payload['tool_choice'] = 'auto'
        return payload

    def parse_response(self, payload: dict[str, Any]) -> AssistantResponse:
        message = payload['choices'][0]['message']
        tool_calls: list[ToolCall] = []
        for item in message.get('tool_calls', []):
            tool_calls.append(
                ToolCall(
                    id=item['id'],
                    name=item['function']['name'],
                    arguments=json.loads(item['function'].get('arguments', '{}')),
                )
            )
        return AssistantResponse(
            text=message.get('content') or '',
            tool_calls=tool_calls,
            protocol=self.protocol,
            raw=payload,
        )


class AnthropicAdapter:
    protocol = Protocol.ANTHROPIC

    def matches(self, config: ModelConfig) -> bool:
        provider = config.provider.lower()
        return 'anthropic' in provider or 'claude' in config.model.lower()

    def endpoint(self, config: ModelConfig) -> str:
        return f"{config.base_url.rstrip('/')}/messages"

    def headers(self, config: ModelConfig, api_key: str) -> dict[str, str]:
        return {
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json',
            **config.extra_headers,
        }

    def build_payload(
        self,
        config: ModelConfig,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
    ) -> dict[str, Any]:
        selected_tools = _selected_tools(config, tools)
        system_parts = [message.content for message in messages if message.role == 'system']
        payload_messages: list[dict[str, Any]] = []
        for message in messages:
            if message.role == 'system':
                continue
            if message.role == 'tool':
                payload_messages.append(
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'tool_result',
                                'tool_use_id': message.tool_call_id or '',
                                'content': message.content,
                            }
                        ],
                    }
                )
                continue
            if message.role == 'assistant' and message.tool_calls:
                content: list[dict[str, Any]] = []
                if message.content:
                    content.append({'type': 'text', 'text': message.content})
                for tool_call in message.tool_calls:
                    content.append(
                        {
                            'type': 'tool_use',
                            'id': tool_call.id,
                            'name': tool_call.name,
                            'input': tool_call.arguments,
                        }
                    )
                payload_messages.append({'role': 'assistant', 'content': content})
                continue
            payload_messages.append({'role': message.role, 'content': message.content})

        payload: dict[str, Any] = {
            'model': config.model,
            'max_tokens': config.max_tokens,
            'messages': payload_messages,
            'temperature': config.temperature,
        }
        if system_parts:
            payload['system'] = '\n'.join(system_parts)
        if selected_tools:
            payload['tools'] = [_anthropic_tool(tool) for tool in selected_tools]
            if config.function_calling.mode == 'none':
                payload['tool_choice'] = {'type': 'none'}
            elif config.function_calling.mode == 'required':
                payload['tool_choice'] = {'type': 'any'}
            elif config.function_calling.mode == 'force' and config.function_calling.forced_tool_name:
                payload['tool_choice'] = {'type': 'tool', 'name': config.function_calling.forced_tool_name}
            else:
                payload['tool_choice'] = {'type': 'auto'}
            if not config.function_calling.parallel_tool_calls:
                payload['disable_parallel_tool_use'] = True
        return payload

    def parse_response(self, payload: dict[str, Any]) -> AssistantResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in payload.get('content', []):
            if item['type'] == 'text':
                text_parts.append(item['text'])
            if item['type'] == 'tool_use':
                tool_calls.append(
                    ToolCall(
                        id=item['id'],
                        name=item['name'],
                        arguments=item.get('input', {}),
                    )
                )
        return AssistantResponse(
            text='\n'.join(text_parts).strip(),
            tool_calls=tool_calls,
            protocol=self.protocol,
            raw=payload,
        )


class GeminiAdapter:
    protocol = Protocol.GEMINI

    def matches(self, config: ModelConfig) -> bool:
        provider = config.provider.lower()
        return 'gemini' in provider or 'google' in provider

    def endpoint(self, config: ModelConfig) -> str:
        return f"{config.base_url.rstrip('/')}/models/{config.model}:generateContent"

    def headers(self, config: ModelConfig, api_key: str) -> dict[str, str]:
        return {'x-goog-api-key': api_key, 'Content-Type': 'application/json', **config.extra_headers}

    def build_payload(
        self,
        config: ModelConfig,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
    ) -> dict[str, Any]:
        selected_tools = _selected_tools(config, tools)
        system_parts = [message.content for message in messages if message.role == 'system']
        contents: list[dict[str, Any]] = []
        for message in messages:
            if message.role == 'system':
                continue
            if message.role == 'tool':
                contents.append(
                    {
                        'role': 'user',
                        'parts': [
                            {
                                'functionResponse': {
                                    'name': message.name or '',
                                    'response': {'content': message.content},
                                }
                            }
                        ],
                    }
                )
                continue
            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({'text': message.content})
            for tool_call in message.tool_calls:
                parts.append({'functionCall': {'name': tool_call.name, 'args': tool_call.arguments}})
            contents.append({'role': 'model' if message.role == 'assistant' else 'user', 'parts': parts})

        payload: dict[str, Any] = {
            'contents': contents,
            'generationConfig': {
                'temperature': config.temperature,
                'maxOutputTokens': config.max_tokens,
            },
        }
        if system_parts:
            payload['systemInstruction'] = {'parts': [{'text': '\n'.join(system_parts)}]}
        if selected_tools:
            payload['tools'] = [
                {
                    'functionDeclarations': [
                        {
                            'name': tool.name,
                            'description': tool.description,
                            'parameters': _openai_safe_schema(tool.input_schema),
                        }
                        for tool in selected_tools
                    ]
                }
            ]
            mode = 'AUTO'
            allowed_function_names: list[str] = []
            if config.function_calling.mode == 'none':
                mode = 'NONE'
            elif config.function_calling.mode == 'required':
                mode = 'ANY'
                allowed_function_names = [tool.name for tool in selected_tools]
            elif config.function_calling.mode == 'force' and config.function_calling.forced_tool_name:
                mode = 'ANY'
                allowed_function_names = [config.function_calling.forced_tool_name]
            payload['toolConfig'] = {'functionCallingConfig': {'mode': mode}}
            if allowed_function_names:
                payload['toolConfig']['functionCallingConfig']['allowedFunctionNames'] = allowed_function_names
        return payload

    def parse_response(self, payload: dict[str, Any]) -> AssistantResponse:
        parts = payload['candidates'][0]['content']['parts']
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in parts:
            if 'text' in item:
                text_parts.append(item['text'])
            if 'functionCall' in item:
                call = item['functionCall']
                tool_calls.append(
                    ToolCall(
                        id=call.get('id', call['name']),
                        name=call['name'],
                        arguments=call.get('args', {}),
                    )
                )
        return AssistantResponse(
            text='\n'.join(text_parts).strip(),
            tool_calls=tool_calls,
            protocol=self.protocol,
            raw=payload,
        )


ADAPTERS: list[ProtocolAdapter] = [OpenAIAdapter(), AnthropicAdapter(), GeminiAdapter()]


def resolve_protocol(config: ModelConfig) -> ProtocolAdapter:
    if config.protocol is not Protocol.AUTO:
        for adapter in ADAPTERS:
            if adapter.protocol is config.protocol:
                return adapter
        raise ValueError(f'Unsupported protocol: {config.protocol}')

    for protocol in (Protocol.OPENAI, Protocol.ANTHROPIC, Protocol.GEMINI):
        for adapter in ADAPTERS:
            if adapter.protocol is protocol and adapter.matches(config):
                return adapter
    return OpenAIAdapter()


class HttpModelClient:
    def __init__(self, config: ModelConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.adapter = resolve_protocol(config)
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
    ) -> AssistantResponse:
        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise RuntimeError(f'Missing API key environment variable: {self.config.api_key_env}')
        payload = self.adapter.build_payload(self.config, messages, tools)
        headers = self.adapter.headers(self.config, api_key)

        async for attempt in AsyncRetrying(stop=stop_after_attempt(3), wait=wait_fixed(1), reraise=True):
            with attempt:
                response = await self._client.post(self.adapter.endpoint(self.config), json=payload, headers=headers)
                response.raise_for_status()
                return self.adapter.parse_response(response.json())
        raise RuntimeError('Model request did not complete')

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except RuntimeError as exc:
            # Windows asyncio transport teardown can raise after a successful request.
            if 'Event loop is closed' not in str(exc):
                raise




