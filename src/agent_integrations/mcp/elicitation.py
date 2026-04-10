from __future__ import annotations

import re
from typing import Any, cast

import mcp.types as mcp_types

from agent_common.schema_utils import normalize_json_schema
from agent_integrations.tool_validation import normalize_and_validate_tool_arguments


def normalize_requested_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_json_schema(schema)
    if normalized.get('type') != 'object':
        normalized = {'type': 'object', 'properties': {}, 'required': []}
    properties = cast(dict[str, Any], normalized.get('properties', {}))
    normalized['properties'] = {key: value for key, value in properties.items() if isinstance(value, dict)}
    required = normalized.get('required', [])
    normalized['required'] = [str(item) for item in required if str(item) in normalized['properties']]
    return normalized


def sampling_message_to_text(message: mcp_types.SamplingMessage) -> str:
    content = message.content
    if isinstance(content, list):
        parts = [content_block_to_text(item) for item in content]
        if any(part is None for part in parts):
            return ''
        return '\n'.join(part for part in parts if part)
    single = content_block_to_text(content)
    return single or ''


def content_block_to_text(content: Any) -> str | None:
    if isinstance(content, mcp_types.TextContent):
        return content.text
    text = getattr(content, 'text', None)
    content_type = getattr(content, 'type', None)
    if isinstance(text, str) and content_type == 'text':
        return text
    return None


def sampling_content_types(message: mcp_types.SamplingMessage) -> list[str]:
    content = message.content
    if isinstance(content, list):
        return [str(getattr(item, 'type', 'unknown')) for item in content]
    return [str(getattr(content, 'type', 'unknown'))]


def sensitive_elicitation_text(text: str) -> bool:
    return re.search(r'(token|secret|password|credential|api[_-]?key|oauth|payment|card|bank|otp|code)', text, re.IGNORECASE) is not None


def classify_sampling_request(params: mcp_types.CreateMessageRequestParams) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if params.tools:
        reasons.append('sampling request exposes tools')
    if params.includeContext == 'allServers':
        reasons.append('sampling request asks for allServers context')
    for item in params.messages:
        for content_type in sampling_content_types(item):
            if content_type in {'tool_use', 'tool_result', 'resource', 'resource_link'}:
                reasons.append(f'sampling content includes {content_type}')
            elif content_type != 'text':
                reasons.append(f'sampling content includes non-text block: {content_type}')
    unique_reasons = list(dict.fromkeys(reasons))
    return ('high', unique_reasons) if unique_reasons else ('low', [])


def classify_elicitation_request(params: mcp_types.ElicitRequestParams) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if params.mode == 'url':
        reasons.append('url-mode elicitation requires out-of-band navigation')
        return 'high', reasons
    normalized_schema = normalize_requested_schema(params.requestedSchema)
    for field_name, field_schema in cast(dict[str, dict[str, Any]], normalized_schema.get('properties', {})).items():
        description = str(field_schema.get('description', ''))
        if sensitive_elicitation_text(field_name) or sensitive_elicitation_text(description):
            reasons.append(f'form field looks sensitive: {field_name}')
    unique_reasons = list(dict.fromkeys(reasons))
    return ('high', unique_reasons) if unique_reasons else ('low', [])


def coerce_form_elicitation_content(
    requested_schema: dict[str, Any],
    raw_content: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    if raw_content is None:
        raw_payload: dict[str, Any] = {}
    elif isinstance(raw_content, dict):
        raw_payload = dict(raw_content)
    else:
        return None, ['form elicitation content must be a JSON object']
    normalized_schema = normalize_requested_schema(requested_schema)
    properties = cast(dict[str, Any], normalized_schema.get('properties', {}))
    filtered_payload = {key: value for key, value in raw_payload.items() if key in properties}
    validation = normalize_and_validate_tool_arguments(normalized_schema, filtered_payload)
    if validation.errors:
        return None, validation.errors
    return validation.normalized, []


def coerce_elicitation_result(
    params: mcp_types.ElicitRequestParams,
    response_payload: dict[str, Any],
) -> mcp_types.ElicitResult | mcp_types.ErrorData:
    action = str(response_payload.get('action') or 'accept').lower()
    if action not in {'accept', 'decline', 'cancel'}:
        action = 'accept'
    if action != 'accept':
        return mcp_types.ElicitResult(action=cast(Any, action), content=None)
    if params.mode == 'url':
        return mcp_types.ElicitResult(action='accept', content=None)
    content, errors = coerce_form_elicitation_content(params.requestedSchema, response_payload.get('content'))
    if errors:
        return mcp_types.ErrorData(code=mcp_types.INVALID_REQUEST, message='; '.join(errors))
    return mcp_types.ElicitResult(action='accept', content=cast(dict[str, Any], content))


def url_host(url: str) -> str:
    match = re.match(r'^[a-z]+://([^/]+)', url, re.IGNORECASE)
    return match.group(1) if match else url
