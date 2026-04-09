from __future__ import annotations

from typing import Any

_TYPE_ALIASES = {
    'bool': 'boolean',
    'boolean': 'boolean',
    'dict': 'object',
    'double': 'number',
    'float': 'number',
    'int': 'integer',
    'integer': 'integer',
    'list': 'array',
    'map': 'object',
    'number': 'number',
    'object': 'object',
    'str': 'string',
    'string': 'string',
    'tuple': 'array',
    'array': 'array',
    'decimal': 'number',
    'null': 'null',
}
_NOISY_KEYS = {
    '$defs',
    '$schema',
    'default',
    'definitions',
    'examples',
    'format',
    'nullable',
    'optional',
    'title',
}
_CORE_KEYS = {'items', 'properties', 'required', 'type'}


def normalize_json_schema(
    schema: dict[str, Any],
    *,
    drop_descriptions: bool = False,
    core_only: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    normalized = _normalize_schema_dict(
        schema,
        drop_descriptions=drop_descriptions,
        core_only=core_only,
        strict=strict,
    )
    return normalized if normalized else {'type': 'object'}


def _normalize_schema_dict(
    schema: dict[str, Any],
    *,
    drop_descriptions: bool,
    core_only: bool,
    strict: bool,
) -> dict[str, Any]:
    normalized = dict(schema)
    type_info = _extract_type_info(normalized.get('type'), strict=strict)
    nullable = _consume_flag(normalized, 'nullable')
    optional = _consume_flag(normalized, 'optional')
    collapsed = _collapse_variants(
        normalized,
        drop_descriptions=drop_descriptions,
        core_only=core_only,
        strict=strict,
    )
    if collapsed is not None:
        normalized = collapsed
        type_info = _extract_type_info(normalized.get('type'), strict=strict)
    nullable = nullable or optional or type_info[1]
    for key in _NOISY_KEYS:
        normalized.pop(key, None)
    schema_type = _infer_schema_type(normalized, strict=strict)
    if schema_type == 'object':
        raw_properties = normalized.get('properties')
        properties = raw_properties if isinstance(raw_properties, dict) else {}
        safe_properties: dict[str, Any] = {}
        for key, value in properties.items():
            if isinstance(value, dict):
                safe_properties[key] = _normalize_schema_dict(
                    value,
                    drop_descriptions=drop_descriptions,
                    core_only=core_only,
                    strict=strict,
                )
            else:
                safe_properties[key] = {'type': _normalize_scalar_type(value)}
        normalized['properties'] = safe_properties
        if strict:
            normalized['required'] = list(safe_properties)
            normalized['additionalProperties'] = False
        else:
            required = normalized.get('required')
            if isinstance(required, list):
                normalized['required'] = [str(item) for item in required if str(item) in safe_properties]
            else:
                normalized.pop('required', None)
            additional_properties = normalized.get('additionalProperties')
            if isinstance(additional_properties, bool):
                normalized['additionalProperties'] = additional_properties
            else:
                normalized.pop('additionalProperties', None)
    elif schema_type == 'array':
        raw_items = normalized.get('items')
        if isinstance(raw_items, dict):
            normalized['items'] = _normalize_schema_dict(
                raw_items,
                drop_descriptions=drop_descriptions,
                core_only=core_only,
                strict=strict,
            )
        else:
            normalized['items'] = {'type': _normalize_scalar_type(raw_items)}
        normalized.pop('properties', None)
        normalized.pop('required', None)
        normalized.pop('additionalProperties', None)
    else:
        normalized.pop('properties', None)
        normalized.pop('required', None)
        normalized.pop('items', None)
        normalized.pop('additionalProperties', None)
    if drop_descriptions:
        normalized.pop('description', None)
    elif 'description' in normalized and not isinstance(normalized['description'], str):
        normalized.pop('description', None)
    if core_only:
        normalized = {key: value for key, value in normalized.items() if key in _CORE_KEYS}
    normalized['type'] = _finalize_type(schema_type, nullable, strict=strict)
    return normalized


def _collapse_variants(
    schema: dict[str, Any],
    *,
    drop_descriptions: bool,
    core_only: bool,
    strict: bool,
) -> dict[str, Any] | None:
    for key in ('anyOf', 'oneOf', 'allOf'):
        options = schema.get(key)
        if not isinstance(options, list) or not options:
            continue
        normalized_options = [
            _normalize_schema_dict(item, drop_descriptions=drop_descriptions, core_only=core_only, strict=strict)
            for item in options
            if isinstance(item, dict)
        ]
        non_null = [item for item in normalized_options if not _schema_is_null(item)]
        if len(non_null) == 1:
            selected = dict(non_null[0])
            if strict and len(non_null) != len(normalized_options):
                selected_type = _extract_type_info(selected.get('type'))[0]
                selected['type'] = _finalize_type(selected_type, True, strict=True)
        else:
            candidate_types = {
                _extract_type_info(item.get('type'), strict=strict)[0]
                for item in non_null
            }
            if candidate_types.issubset({'integer', 'number'}):
                selected = {'type': 'number'}
            elif candidate_types == {'boolean'}:
                selected = {'type': 'boolean'}
            elif candidate_types == {'array'}:
                selected = non_null[0] if non_null else {'type': 'array', 'items': {'type': 'string'}}
            elif candidate_types == {'object'}:
                selected = non_null[0] if non_null else {'type': 'object', 'properties': {}}
            elif 'string' in candidate_types:
                selected = {'type': 'string'}
            elif non_null:
                selected = {'type': str(non_null[0].get('type', 'string'))}
            else:
                selected = {'type': 'string'}
        description = schema.get('description')
        if not drop_descriptions and isinstance(description, str) and 'description' not in selected:
            selected['description'] = description
        return selected
    return None


def _infer_schema_type(schema: dict[str, Any], *, strict: bool) -> str:
    del strict
    if 'type' in schema:
        return _extract_type_info(schema.get('type'))[0]
    if 'properties' in schema or 'required' in schema:
        return 'object'
    if 'items' in schema:
        return 'array'
    return 'object'


def _consume_flag(schema: dict[str, Any], key: str) -> bool:
    value = schema.get(key)
    if isinstance(value, bool):
        return value
    return False


def _schema_is_null(schema: dict[str, Any]) -> bool:
    type_name, _ = _extract_type_info(schema.get('type'))
    return type_name == 'null'


def _extract_type_info(value: Any, *, strict: bool = False) -> tuple[str, bool]:
    del strict
    if isinstance(value, list):
        normalized = [_normalize_scalar_type(item) for item in value]
        nullable = 'null' in normalized
        non_null = [item for item in normalized if item != 'null']
        if len(non_null) == 1:
            return non_null[0], nullable
        if set(non_null).issubset({'integer', 'number'}):
            return 'number', nullable
        return non_null[0] if non_null else 'string', nullable
    return _normalize_scalar_type(value), False


def _normalize_scalar_type(value: Any) -> str:
    if isinstance(value, list):
        normalized = [_normalize_scalar_type(item) for item in value]
        non_null = [item for item in normalized if item != 'null']
        if len(non_null) == 1:
            return non_null[0]
        if set(non_null).issubset({'integer', 'number'}):
            return 'number'
        return non_null[0] if non_null else 'string'
    schema_type = str(value or 'object').strip().lower()
    normalized_type = _TYPE_ALIASES.get(schema_type, schema_type or 'object')
    return str(normalized_type)


def _finalize_type(schema_type: str, nullable: bool, *, strict: bool) -> str | list[str]:
    if strict and nullable and schema_type != 'null':
        return [schema_type, 'null']
    return schema_type
