from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast


@dataclass(slots=True)
class ValidationResult:
    normalized: dict[str, Any]
    errors: list[str]


class ToolValidationError(RuntimeError):
    def __init__(self, tool_name: str, errors: list[str], normalized: dict[str, Any]) -> None:
        super().__init__(f"Tool '{tool_name}' arguments failed validation: {'; '.join(errors)}")
        self.tool_name = tool_name
        self.errors = errors
        self.normalized = normalized


_JSON_OBJECT_TYPES = {'object', 'dict'}
_JSON_ARRAY_TYPES = {'array', 'tuple'}


def normalize_and_validate_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> ValidationResult:
    schema_type = str(schema.get('type', 'object'))
    if schema_type not in _JSON_OBJECT_TYPES:
        return ValidationResult(normalized=dict(arguments), errors=[])
    properties = cast(dict[str, dict[str, Any]], schema.get('properties', {}))
    required = [str(item) for item in schema.get('required', [])]
    normalized: dict[str, Any] = {}
    errors: list[str] = []
    for key, value in arguments.items():
        property_schema = properties.get(key, {})
        normalized[key], property_errors = _normalize_value(value, property_schema, path=key)
        errors.extend(property_errors)
    for key in required:
        if key not in normalized:
            errors.append(f'missing required argument: {key}')
    return ValidationResult(normalized=normalized, errors=errors)


def _normalize_value(value: Any, schema: dict[str, Any], path: str) -> tuple[Any, list[str]]:
    expected_type = schema.get('type', '')
    normalized_types = _normalize_expected_types(expected_type)
    if value is None:
        if 'null' in normalized_types:
            return None, []
        if 'string' in normalized_types:
            return '', []
    expected_type = next((item for item in normalized_types if item != 'null'), '')
    if expected_type in ('', 'any'):
        return value, []
    if expected_type in _JSON_OBJECT_TYPES:
        if not isinstance(value, dict):
            return value, [f'{path} expected object']
        result = normalize_and_validate_tool_arguments(schema, value)
        return result.normalized, [f'{path}.{error}' for error in result.errors]
    if expected_type in _JSON_ARRAY_TYPES:
        return _normalize_array(value, schema, path)
    if expected_type == 'integer':
        if isinstance(value, bool):
            return value, [f'{path} expected integer']
        if isinstance(value, int):
            return value, []
        if isinstance(value, float) and value.is_integer():
            return int(value), []
        if isinstance(value, str):
            text = value.strip()
            if text.lstrip('-').isdigit():
                return int(text), []
        return value, [f'{path} expected integer']
    if expected_type in ('float', 'number'):
        if isinstance(value, bool):
            return value, [f'{path} expected number']
        if isinstance(value, (int, float)):
            return float(value), []
        if isinstance(value, str):
            text = value.strip()
            try:
                return float(text), []
            except ValueError:
                return value, [f'{path} expected number']
        return value, [f'{path} expected number']
    if expected_type == 'boolean':
        if isinstance(value, bool):
            return value, []
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {'true', 'yes', '1'}:
                return True, []
            if lowered in {'false', 'no', '0'}:
                return False, []
        return value, [f'{path} expected boolean']
    if expected_type == 'string':
        if isinstance(value, str):
            return value, []
        if isinstance(value, (int, float, bool)):
            return str(value), []
        return value, [f'{path} expected string']
    return value, []


def _normalize_array(value: Any, schema: dict[str, Any], path: str) -> tuple[Any, list[str]]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                pass
    if not isinstance(value, (list, tuple)):
        return value, [f'{path} expected array']
    item_schema = cast(dict[str, Any], schema.get('items', {}))
    normalized: list[Any] = []
    errors: list[str] = []
    for index, item in enumerate(value):
        normalized_item, item_errors = _normalize_value(item, item_schema, f'{path}[{index}]')
        normalized.append(normalized_item)
        errors.extend(item_errors)
    if schema.get('type') == 'tuple':
        return tuple(normalized), errors
    return normalized, errors


def _normalize_expected_types(raw_type: Any) -> list[str]:
    if isinstance(raw_type, list):
        return [str(item).strip().lower() for item in raw_type]
    if raw_type in (None, ''):
        return []
    return [str(raw_type).strip().lower()]
