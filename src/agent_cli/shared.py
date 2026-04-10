from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from rich.console import Console

from agent_common.models import HumanRequest, HumanRequestStatus
from agent_common.schema_utils import normalize_json_schema
from agent_integrations.tool_validation import normalize_and_validate_tool_arguments
from agent_runtime import EasyAgentRuntime, build_runtime


async def with_runtime(config_path: str, callback: Callable[[EasyAgentRuntime], Awaitable[Any]]) -> Any:
    runtime = build_runtime(config_path)
    try:
        await runtime.start()
        return await callback(runtime)
    finally:
        await runtime.aclose()



def _normalize_form_response_content(request_payload: dict[str, Any], raw_content: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    requested_schema = request_payload.get('requested_schema')
    if not isinstance(requested_schema, dict):
        return dict(raw_content), []
    normalized_schema = normalize_json_schema(requested_schema)
    if normalized_schema.get('type') != 'object':
        normalized_schema = {'type': 'object', 'properties': {}, 'required': []}
    properties = normalized_schema.get('properties', {})
    if not isinstance(properties, dict):
        properties = {}
    normalized_schema['properties'] = {key: value for key, value in properties.items() if isinstance(value, dict)}
    normalized_schema['required'] = [
        str(item) for item in normalized_schema.get('required', []) if str(item) in normalized_schema['properties']
    ]
    filtered = {key: value for key, value in raw_content.items() if key in normalized_schema['properties']}
    validation = normalize_and_validate_tool_arguments(normalized_schema, filtered)
    if validation.errors:
        return None, validation.errors
    return validation.normalized, []



def build_cli_inline_resolver(console: Console) -> Callable[[HumanRequest], Awaitable[tuple[HumanRequestStatus, dict[str, Any] | None]]]:
    async def _resolve(request: HumanRequest) -> tuple[HumanRequestStatus, dict[str, Any] | None]:
        def _prompt() -> tuple[HumanRequestStatus, dict[str, Any] | None]:
            console.print(f"[bold yellow]Approval Required:[/bold yellow] {request.title}")
            console.print_json(json.dumps(request.payload, ensure_ascii=False))
            if request.kind == 'mcp_elicitation':
                mode = str(request.payload.get('mode') or 'form').lower()
                action = console.input('Action [accept/decline/cancel]: ').strip().lower() or 'accept'
                if action not in {'accept', 'decline', 'cancel'}:
                    action = 'accept'
                if action == 'decline':
                    return HumanRequestStatus.REJECTED, {'action': 'decline'}
                if action == 'cancel':
                    return HumanRequestStatus.CANCELLED, {'action': 'cancel'}
                if mode == 'url':
                    if request.payload.get('url'):
                        console.print(f"Open URL if needed: {request.payload['url']}")
                    return HumanRequestStatus.APPROVED, {'action': 'accept'}
                while True:
                    raw = console.input('Structured JSON content (blank for {}): ').strip()
                    content = json.loads(raw) if raw else {}
                    if not isinstance(content, dict):
                        console.print('[red]Form content must be a JSON object.[/red]')
                        continue
                    normalized, errors = _normalize_form_response_content(request.payload, content)
                    if errors:
                        console.print(f"[red]{'; '.join(errors)}[/red]")
                        continue
                    return HumanRequestStatus.APPROVED, {'action': 'accept', 'content': normalized or {}}
            answer = console.input('Approve? [y/N]: ').strip().lower()
            if answer in {'y', 'yes'}:
                return HumanRequestStatus.APPROVED, {'approved': True}
            return HumanRequestStatus.REJECTED, {'approved': False}

        return await anyio.to_thread.run_sync(_prompt)

    return _resolve
