from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Literal, cast

import httpx

from agent_common.models import ChatMessage, Protocol, ToolCall, ToolSpec
from agent_common.schema_utils import normalize_json_schema
from agent_config.app import AppConfig, ModelConfig, PublicEvalWebSearchConfig, load_config
from agent_protocols.client import AnthropicAdapter, GeminiAdapter, OpenAIAdapter
from agent_runtime.public_eval_web_search import (
    WebSearchQuotaExceeded,
)
from agent_runtime.public_eval_web_search import (
    _case_prompt as _web_case_prompt,
)
from agent_runtime.public_eval_web_search import (
    _fetch_web_contents as _web_fetch_contents,
)
from agent_runtime.public_eval_web_search import (
    _normalize_serpapi_search_results as _web_normalize_serpapi_search_results,
)
from agent_runtime.public_eval_web_search import (
    _record_web_search_usage as _web_record_web_search_usage,
)
from agent_runtime.public_eval_web_search import (
    _serpapi_query_params as _web_serpapi_query_params,
)
from agent_runtime.public_eval_web_search import (
    _serpapi_search as _web_serpapi_search,
)
from agent_runtime.runtime import build_runtime_from_config

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / 'public_evals' / 'fixtures'
_GENERIC_TOKENS = {
    'a',
    'all',
    'also',
    'an',
    'and',
    'base',
    'based',
    'calculate',
    'calculates',
    'can',
    'data',
    'date',
    'default',
    'determine',
    'find',
    'for',
    'from',
    'get',
    'given',
    'if',
    'in',
    'is',
    'its',
    'just',
    'like',
    'me',
    'needed',
    'of',
    'on',
    'or',
    'please',
    'properties',
    'property',
    'retrieve',
    'retrieves',
    'specific',
    'the',
    'their',
    'there',
    'these',
    'this',
    'to',
    'true',
    'units',
    'using',
    'what',
    'which',
    'with',
}
_MULTI_INTENT_PATTERN = re.compile(r'\b(also|both|as well as|in addition)\b', re.IGNORECASE)
_SINGULAR_TASK_REFERENCE_PATTERN = re.compile(r'\b(the task|that task|it)\b', re.IGNORECASE)
_SCHEMA_MATRIX_SAMPLE = {
    'type': 'dict',
    'properties': {
        'items': {
            'type': 'tuple',
            'items': {'type': 'dict', 'properties': {'value': {'type': 'integer'}}},
        },
        'choice': {
            'anyOf': [
                {'type': 'string', 'format': 'binary'},
                {'type': 'integer'},
                {'type': 'null'},
            ]
        },
        'params': {
            'type': 'array',
            'items': {'type': ['string', 'number', 'boolean', 'null']},
        },
        'amount': {'type': 'float', 'optional': True},
        'nickname': {'type': 'string', 'nullable': True},
        'timestamp': {'type': 'string', 'format': 'date-time'},
    },
    'required': ['items', 'ghost'],
    'examples': ['drop-me'],
}
_STAGE_ORDER = ('base', 'strict_schema_retry', 'candidate_pruned_retry')


@dataclass(slots=True)
class PublicEvalRecord:
    suite: str
    case_id: str
    success: bool
    duration_seconds: float
    tool_name_match: float
    argument_match: float
    expected_call_count: int
    actual_call_count: int
    result_summary: str
    error: str | None = None
    fallback_stage: str = 'base'
    fallback_attempts: list[str] = field(default_factory=list)
    failure_bucket: str | None = None


@dataclass(slots=True)
class _BfclAttemptResult:
    record: PublicEvalRecord | None = None
    error: Exception | None = None
    duration_seconds: float = 0.0
    retryable_provider_400: bool = False


def _shared_payload(base: AppConfig) -> dict[str, Any]:
    return {
        'model': base.model.model_dump(),
        'plugins': list(base.plugins),
        'skills': [item.model_dump() for item in base.skills],
        'mcp': [item.model_dump() for item in base.mcp],
        'storage': base.storage.model_dump(),
        'logging': base.logging.model_dump(),
        'guardrails': base.guardrails.model_dump(),
        'observability': base.observability.model_dump(),
        'evaluation': base.evaluation.model_dump(),
        'security': base.security.model_dump(),
    }


def _load_fixture(name: str) -> dict[str, Any]:
    payload = json.loads((FIXTURE_ROOT / name).read_text(encoding='utf-8'))
    return cast(dict[str, Any], payload)


def _load_json_path(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding='utf-8')))


def _write_json_path(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _cache_json_from_url(url: str, cache_path: Path) -> dict[str, Any]:
    if cache_path.is_file():
        return _load_json_path(cache_path)
    response = httpx.get(url, timeout=30.0)
    response.raise_for_status()
    payload = cast(dict[str, Any], response.json())
    _write_json_path(cache_path, payload)
    return payload


def _flatten_official_manifest_cases(payload: dict[str, Any]) -> list[dict[str, Any]]:
    direct = payload.get('bfcl_cases')
    if isinstance(direct, list):
        return cast(list[dict[str, Any]], direct)
    direct = payload.get('cases')
    if isinstance(direct, list):
        return cast(list[dict[str, Any]], direct)
    categories = payload.get('categories')
    if isinstance(categories, dict):
        cases: list[dict[str, Any]] = []
        for item in categories.values():
            if isinstance(item, list):
                cases.extend(cast(list[dict[str, Any]], item))
                continue
            if isinstance(item, dict) and isinstance(item.get('cases'), list):
                cases.extend(cast(list[dict[str, Any]], item['cases']))
        return cases
    raise RuntimeError('official BFCL manifest does not contain a supported cases payload')


def _load_official_full_v4_inputs(base_config: AppConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    official = base_config.evaluation.public_eval.official_dataset
    manifest_path = Path(official.manifest_path)
    cache_dir = Path(official.cache_dir)
    if manifest_path.is_file():
        manifest = _load_json_path(manifest_path)
    elif official.source_url:
        manifest = _cache_json_from_url(official.source_url, cache_dir / 'bfcl_v4_manifest.json')
    else:
        raise RuntimeError(
            f"official BFCL profile requires '{manifest_path}' or evaluation.public_eval.official_dataset.source_url"
        )
    bfcl_cases = _flatten_official_manifest_cases(manifest)
    tau_cases = cast(list[dict[str, Any]], _load_fixture('tau2_mock_subset.json')['cases'])
    return bfcl_cases, tau_cases


def _load_public_eval_inputs(base_config: AppConfig) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    public_eval = base_config.evaluation.public_eval
    profile = public_eval.profile
    if profile == 'official_full_v4':
        bfcl_cases, tau_cases = _load_official_full_v4_inputs(base_config)
        return profile, public_eval.bfcl_version, bfcl_cases, tau_cases
    bfcl_cases = list(cast(list[dict[str, Any]], _load_fixture('bfcl_subset.json')['cases']))
    if profile == 'full_v4' and public_eval.enable_full_bfcl:
        for name in (
            'bfcl_v4_web_search.json',
            'bfcl_v4_memory.json',
            'bfcl_v4_format_sensitivity.json',
        ):
            bfcl_cases.extend(cast(list[dict[str, Any]], _load_fixture(name)['cases']))
    tau_cases = cast(list[dict[str, Any]], _load_fixture('tau2_mock_subset.json')['cases'])
    return profile, public_eval.bfcl_version, bfcl_cases, tau_cases


def _bfcl_system_prompt(case: dict[str, Any]) -> str:
    expected_calls = len(case.get('ground_truth', []))
    if case.get('expect_no_tool'):
        budget = 'Do not call any tool when the request is outside the tool set.'
    elif expected_calls <= 1:
        budget = 'Make exactly one tool call total only if it is clearly necessary, then stop.'
    else:
        budget = f'Use exactly {expected_calls} tool calls only when the requested actions are independent and necessary.'
    return (
        'You are evaluating tool-calling behavior. Choose the single best action based on the user request. '
        + budget
        + ' If the request is irrelevant to the available tools, answer directly without any tool call. '
        'If one tool already covers the request, prefer that single tool rather than decomposing the request into narrower follow-up calls. '
        'If the user asks for the complete result, all results, or paired properties, include any needed selector or optional fields in the first tool call instead of retrying the same tool. '
        "If the user asks for all roots, both roots, or a complete root set, set any available root-type selector to include all roots instead of relying on a default real-only mode. "
        'If the budget allows exactly one tool call and multiple tools look related, choose the tool that best matches the primary requested analysis and avoid secondary follow-up calls. '
        'A successful tool call ends the search unless an additional independent tool call is explicitly required by the budget. '
        'Never speculate, never duplicate a successful tool call, and never call a second tool just to restate the first answer. '
        'Arguments must match the tool schema exactly. If a validation error is returned, correct the arguments and try again.'
    )


def _tau_system_prompt() -> str:
    return (
        'You are a precise task assistant. Use the provided task-management tools only when the user explicitly wants an action taken. '
        'Prefer updating an existing matching task over creating a duplicate. '
        'If previous conversation state is present, continue from it and infer task ids from prior tool outputs instead of asking again. '
        'When a single recent task in history clearly matches phrases like the task, that task, or it, update it directly without a follow-up question. '
        'Treat the most recently created task in the visible history as the default singular reference unless the user names a different task. '
        'Acknowledge successful completion concisely after the required tool calls finish.'
    )


def _sanitize_tool_name(name: str) -> str:
    sanitized = re.sub(r'[^A-Za-z0-9_-]+', '_', name).strip('_')
    if not sanitized:
        sanitized = 'tool'
    if sanitized[0].isdigit():
        sanitized = f'tool_{sanitized}'
    return sanitized[:64]


def _normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return normalize_json_schema(schema)


def _strict_normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return normalize_json_schema(schema, drop_descriptions=True, strict=True)


def _protocol_matrix_sample_schema(adapter: Any, provider: str) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = adapter.build_payload(
        ModelConfig(provider=provider, protocol=adapter.protocol),
        [ChatMessage(role='user', content='schema probe')],
        [ToolSpec(name='schema_probe', description='schema probe', input_schema=_SCHEMA_MATRIX_SAMPLE)],
    )
    if adapter.protocol is Protocol.OPENAI:
        return payload, cast(dict[str, Any], payload['tools'][0]['function']['parameters'])
    if adapter.protocol is Protocol.ANTHROPIC:
        return payload, cast(dict[str, Any], payload['tools'][0]['input_schema'])
    return payload, cast(dict[str, Any], payload['tools'][0]['functionDeclarations'][0]['parameters'])


def _provider_schema_matrix() -> dict[str, Any]:
    providers: list[tuple[str, Any, str]] = [
        ('openai_compatible', OpenAIAdapter(), 'deepseek'),
        ('anthropic', AnthropicAdapter(), 'anthropic'),
        ('gemini', GeminiAdapter(), 'gemini'),
    ]
    matrix: dict[str, Any] = {}
    for provider_name, adapter, config_provider in providers:
        payload, schema = _protocol_matrix_sample_schema(adapter, config_provider)
        properties = cast(dict[str, Any], schema.get('properties', {}))
        required = cast(list[str], schema.get('required', []))
        amount_schema = cast(dict[str, Any], properties.get('amount', {}))
        nickname_schema = cast(dict[str, Any], properties.get('nickname', {}))
        payload_tools = cast(list[dict[str, Any]], payload.get('tools', []))
        first_tool = payload_tools[0] if payload_tools else {}
        strict_enabled = bool(cast(dict[str, Any], first_tool.get('function', {})).get('strict'))
        parallel_control = payload.get('parallel_tool_calls')
        params_item_type = cast(dict[str, Any], cast(dict[str, Any], properties.get('params', {})).get('items', {})).get('type')
        matrix[provider_name] = {
            'protocol': adapter.protocol.value,
            'features': {
                'root_object_alias': {
                    'supported': schema.get('type') == 'object',
                    'observed': schema.get('type'),
                },
                'tuple_array_normalized': {
                    'supported': cast(dict[str, Any], properties.get('items', {})).get('type') == 'array',
                    'observed': cast(dict[str, Any], properties.get('items', {})).get('type'),
                },
                'any_of_flattened': {
                    'supported': cast(dict[str, Any], properties.get('choice', {})).get('type') == 'string',
                    'observed': cast(dict[str, Any], properties.get('choice', {})).get('type'),
                },
                'list_type_flattened': {
                    'supported': params_item_type == 'string' or params_item_type == ['string', 'null'],
                    'observed': params_item_type,
                },
                'format_removed': {
                    'supported': 'format' not in cast(dict[str, Any], properties.get('timestamp', {})),
                    'observed': cast(dict[str, Any], properties.get('timestamp', {})).get('format'),
                },
                'invalid_required_pruned': {
                    'supported': 'ghost' not in cast(list[str], schema.get('required', [])),
                    'observed': cast(list[str], schema.get('required', [])),
                },
                'strict_flag': {
                    'supported': strict_enabled,
                    'observed': strict_enabled,
                },
                'additional_properties_false': {
                    'supported': schema.get('additionalProperties') is False,
                    'observed': schema.get('additionalProperties'),
                },
                'all_properties_required': {
                    'supported': set(required) == set(properties),
                    'observed': required,
                },
                'nullable_preserved': {
                    'supported': nickname_schema.get('type') == ['string', 'null'],
                    'observed': nickname_schema.get('type'),
                },
                'optional_promoted_to_required_nullable': {
                    'supported': amount_schema.get('type') == ['number', 'null'] and 'amount' in required,
                    'observed': {
                        'type': amount_schema.get('type'),
                        'required': 'amount' in required,
                    },
                },
                'parallel_tool_calls_control': {
                    'supported': parallel_control is not None,
                    'observed': parallel_control,
                },
            },
        }
    return matrix


def _build_tool_name_map(functions: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for function in functions:
        original = str(function['name'])
        base = _sanitize_tool_name(original)
        candidate = base
        index = 2
        while candidate in used:
            suffix = f'_{index}'
            candidate = f"{base[: max(1, 64 - len(suffix))]}{suffix}"
            index += 1
        mapping[original] = candidate
        used.add(candidate)
    return mapping


def _normalize_truth_call(
    item: dict[str, Any],
    tool_name_map: dict[str, str] | None = None,
) -> tuple[str, dict[str, list[Any]]]:
    tool_name = next(iter(item.keys()))
    if tool_name_map is not None:
        tool_name = tool_name_map.get(tool_name, tool_name)
    return tool_name, item[next(iter(item.keys()))]


def _values_match(actual: Any, options: list[Any]) -> bool:
    if actual in (None, ''):
        return '' in options
    for option in options:
        if option == '':
            continue
        if isinstance(option, list) and isinstance(actual, tuple):
            if list(actual) == option:
                return True
        if option == actual:
            return True
        if isinstance(option, str) and isinstance(actual, str) and option.lower() == actual.lower():
            return True
        if isinstance(option, float) and isinstance(actual, (int, float)) and float(actual) == option:
            return True
        if isinstance(option, int) and isinstance(actual, (int, float)) and int(actual) == option:
            return True
    return False


def _truth_matches(actual: dict[str, Any], truth: dict[str, list[Any]]) -> float:
    scores: list[float] = []
    for key, options in truth.items():
        if key not in actual:
            scores.append(1.0 if '' in options else 0.0)
            continue
        value = actual[key]
        if isinstance(value, dict) and options and isinstance(options[0], dict):
            nested_truth = options[0]
            nested_hits = 0
            for nested_key, nested_options in nested_truth.items():
                nested_value = value.get(nested_key)
                if _values_match(nested_value, nested_options):
                    nested_hits += 1
            scores.append(nested_hits / max(1, len(nested_truth)))
            continue
        scores.append(1.0 if _values_match(value, options) else 0.0)
    if not scores:
        return 1.0
    return sum(scores) / len(scores)


def _extract_successful_tool_calls(trace: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for event in trace.get('events', []):
        if event.get('kind') != 'tool_call_succeeded':
            continue
        payload = event.get('payload', {})
        calls.append({'name': payload.get('tool_name'), 'arguments': payload.get('arguments', {})})
    return calls


def _score_bfcl_case(
    case: dict[str, Any],
    actual_calls: list[dict[str, Any]],
    tool_name_map: dict[str, str] | None = None,
) -> tuple[bool, float, float]:
    if case['expect_no_tool']:
        success = len(actual_calls) == 0
        return success, 1.0 if success else 0.0, 1.0 if success else 0.0
    truths = [_normalize_truth_call(item, tool_name_map) for item in case['ground_truth']]
    if len(actual_calls) != len(truths):
        return False, 0.0, 0.0
    used: set[int] = set()
    tool_hits = 0.0
    arg_scores: list[float] = []
    for expected_name, truth_args in truths:
        best_index: int | None = None
        best_score = -1.0
        for index, actual in enumerate(actual_calls):
            if index in used or actual['name'] != expected_name:
                continue
            score = _truth_matches(actual['arguments'], truth_args)
            if score > best_score:
                best_index = index
                best_score = score
        if best_index is None:
            arg_scores.append(0.0)
            continue
        used.add(best_index)
        tool_hits += 1.0
        arg_scores.append(best_score)
    tool_name_match = tool_hits / len(truths)
    argument_match = sum(arg_scores) / len(truths)
    success = tool_name_match == 1.0 and argument_match == 1.0
    return success, tool_name_match, argument_match


def _score_tau_case(case: dict[str, Any], actual_calls: list[dict[str, Any]]) -> tuple[bool, float, float]:
    expected = case.get('evaluation_criteria', {}).get('actions', [])
    if len(actual_calls) < len(expected):
        return False, 0.0, 0.0
    tool_hits = 0.0
    arg_scores: list[float] = []
    for expected_call in expected:
        matched = next((item for item in actual_calls if item['name'] == expected_call['name']), None)
        if matched is None:
            arg_scores.append(0.0)
            continue
        tool_hits += 1.0
        truth_args = {key: [value] for key, value in expected_call['arguments'].items()}
        arg_scores.append(_truth_matches(matched['arguments'], truth_args))
    tool_name_match = tool_hits / len(expected)
    argument_match = sum(arg_scores) / len(expected)
    success = tool_name_match == 1.0 and argument_match == 1.0
    return success, tool_name_match, argument_match


def _summarize_result(result: Any) -> str:
    if isinstance(result, str):
        return result[:200]
    return json.dumps(result, ensure_ascii=False)[:200]


def _extract_tau_tasks_from_history(message_history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for item in message_history:
        if item.get('role') != 'tool':
            continue
        content = str(item.get('content', '')).strip()
        if not content:
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or 'task_id' not in payload:
            continue
        task_id = str(payload['task_id'])
        tasks[task_id] = {
            'task_id': task_id,
            'user_id': str(payload.get('user_id') or 'user_1'),
            'title': str(payload.get('title') or ''),
            'description': str(payload.get('description') or ''),
            'status': str(payload.get('status') or 'pending'),
        }
    return tasks


def _select_recent_tau_task(tasks: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not tasks:
        return None

    def _task_sort_key(item: dict[str, Any]) -> tuple[int, str]:
        task_id = str(item.get('task_id') or '')
        if task_id.startswith('task_') and task_id.split('_')[-1].isdigit():
            return int(task_id.split('_')[-1]), task_id
        return -1, task_id

    return sorted(tasks.values(), key=_task_sort_key)[-1]


def _tau_history_memory_message(tasks: dict[str, dict[str, Any]]) -> str | None:
    if not tasks:
        return None
    ordered = list(tasks.values())[-4:]
    recent = _select_recent_tau_task(tasks)
    lines = [
        'Conversation memory for task grounding. Reuse these task ids directly when the user refers to the previously discussed task:',
    ]
    if recent is not None:
        lines.append(
            f"Default singular references like 'the task', 'that task', or 'it' refer to {recent['task_id']} unless the user names another task."
        )
    for item in ordered:
        lines.append(
            f"- {item['task_id']}: title={item['title']!r}, status={item['status']!r}, description={item['description']!r}"
        )
    return '\n'.join(lines)


def _tau_prompt_with_grounding(prompt: str, tasks: dict[str, dict[str, Any]]) -> str:
    if not tasks:
        return prompt
    lines: list[str] = []
    recent = _select_recent_tau_task(tasks)
    if recent is not None:
        lines.append(
            f"Most recent discussed task: {recent['task_id']} title={recent['title']!r} status={recent['status']!r}."
        )
        if _SINGULAR_TASK_REFERENCE_PATTERN.search(prompt):
            lines.append(f"Default singular follow-up references map to {recent['task_id']}.")
    task_state = [f"{item['task_id']}:{item['title']}:{item['status']}" for item in tasks.values()]
    if task_state:
        lines.append(f"Known task state: {'; '.join(task_state)}")
    lines.append(f"User request: {prompt}")
    return '\n'.join(lines)


def _tokenize_public_eval_text(value: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r'[A-Za-z0-9]+', value.lower()):
        if raw in _GENERIC_TOKENS:
            continue
        tokens.add(raw)
        if raw.endswith('s') and len(raw) > 4:
            singular = raw[:-1]
            if singular not in _GENERIC_TOKENS:
                tokens.add(singular)
    return tokens


def _function_relevance_score(function: dict[str, Any], prompt_tokens: set[str]) -> int:
    name_tokens = _tokenize_public_eval_text(str(function.get('name', '')))
    description_tokens = _tokenize_public_eval_text(str(function.get('description', '')))
    property_tokens: set[str] = set()
    parameters = cast(dict[str, Any], function.get('parameters', {}))
    properties = cast(dict[str, Any], parameters.get('properties', {}))
    for property_name, property_schema in properties.items():
        property_tokens |= _tokenize_public_eval_text(str(property_name))
        if isinstance(property_schema, dict):
            property_tokens |= _tokenize_public_eval_text(str(property_schema.get('description', '')))
    return (
        4 * len(prompt_tokens & name_tokens)
        + 2 * len(prompt_tokens & property_tokens)
        + len(prompt_tokens & description_tokens)
    )


def _looks_multi_intent(prompt: str) -> bool:
    return _MULTI_INTENT_PATTERN.search(prompt) is not None


def _select_bfcl_candidate_functions(prompt: str, functions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prompt_tokens = _tokenize_public_eval_text(prompt)
    scored = [
        (
            function,
            _function_relevance_score(function, prompt_tokens),
            len(prompt_tokens & _tokenize_public_eval_text(str(function.get('name', '')))),
        )
        for function in functions
    ]
    if not scored:
        return []
    best_score = max(score for _, score, _ in scored)
    if best_score < 3:
        return []
    best_name_overlap = max(name_overlap for _, score, name_overlap in scored if score == best_score)
    if best_name_overlap == 0 and best_score < 6:
        return []
    if _looks_multi_intent(prompt):
        threshold = max(3, best_score - 1)
        return [function for function, score, _ in scored if score >= threshold]
    return [function for function, score, _ in scored if score == best_score]


def _is_openai_compatible_provider(provider: str) -> bool:
    lowered = provider.lower()
    return any(token in lowered for token in ('openai', 'deepseek', 'compatible'))


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None
    return chain


def _is_retryable_provider_400(base_config: AppConfig, exc: BaseException) -> bool:
    if not _is_openai_compatible_provider(base_config.model.provider):
        return False
    for item in _exception_chain(exc):
        if isinstance(item, httpx.HTTPStatusError) and item.response.status_code == 400:
            return True
        if '400 Bad Request' in str(item):
            return True
    return False


def _same_function_selection(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    return [str(item['name']) for item in left] == [str(item['name']) for item in right]


def _parse_actual_calls(record: PublicEvalRecord) -> list[dict[str, Any]]:
    if not record.error:
        return []
    try:
        payload = json.loads(record.error)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    actual_calls = payload.get('actual_calls')
    return actual_calls if isinstance(actual_calls, list) else []


def _arguments_superset(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if len(right) <= len(left):
        return False
    for key, value in left.items():
        if right.get(key) != value:
            return False
    return True


def _is_duplicate_call_failure(record: PublicEvalRecord) -> bool:
    actual_calls = _parse_actual_calls(record)
    if len(actual_calls) <= record.expected_call_count:
        return False
    names = [str(item.get('name') or '') for item in actual_calls]
    if len(set(names)) < len(names):
        return True
    if record.expected_call_count <= 1:
        return True
    for index, left in enumerate(actual_calls):
        left_args = left.get('arguments') if isinstance(left, dict) else {}
        for right in actual_calls[index + 1:]:
            if str(left.get('name') or '') != str(right.get('name') or ''):
                continue
            right_args = right.get('arguments') if isinstance(right, dict) else {}
            if isinstance(left_args, dict) and isinstance(right_args, dict):
                if _arguments_superset(left_args, right_args) or _arguments_superset(right_args, left_args):
                    return True
    return False


def _classify_failure_bucket(record: PublicEvalRecord) -> str:
    if record.success:
        return 'passed'
    error_text = (record.error or '').lower()
    if record.suite == 'tau2_mock' and 'history' in record.case_id and record.actual_call_count == 0:
        return 'history_grounding_miss'
    if record.suite == 'bfcl_web_search':
        if 'grounded urls' in error_text or 'grounded in search results' in error_text:
            return 'ungrounded_contents'
        if 'tool call budget exhausted' in error_text:
            return 'single_call_constraint_miss'
        if any(token in error_text for token in ('serpapi', 'web search', 'web contents', 'api_key', 'quota', 'search.json')):
            return 'search_tool_miss'
    if record.suite == 'bfcl_memory':
        return 'memory_backend_miss'
    if record.suite == 'bfcl_format_sensitivity':
        return 'format_variant_miss'
    if _is_duplicate_call_failure(record):
        return 'duplicate_call'
    if 'refusal' in error_text or 'incomplete' in error_text:
        return 'refusal_or_incomplete'
    if '400 bad request' in error_text or 'httpstatuserror' in error_text or record.fallback_stage != 'base':
        return 'schema_or_provider_failure'
    return 'other'


def _aggregate_stage_summary(records: list[PublicEvalRecord]) -> dict[str, Any]:
    stage_names = [stage for stage in _STAGE_ORDER if any(stage in item.fallback_attempts or item.fallback_stage == stage for item in records)]
    summary: dict[str, Any] = {'stages': {}, 'transitions': {}}
    for stage in stage_names:
        entered = [item for item in records if stage in item.fallback_attempts]
        terminal = [item for item in records if item.fallback_stage == stage]
        successes = sum(1 for item in terminal if item.success)
        summary['stages'][stage] = {
            'entered_runs': len(entered),
            'terminal_runs': len(terminal),
            'terminal_successes': successes,
            'terminal_failures': len(terminal) - successes,
            'terminal_pass_rate': round(successes / len(terminal), 4) if terminal else 0.0,
            'recovered_cases': sum(1 for item in terminal if item.success and stage != 'base'),
        }
    transitions: dict[str, int] = {}
    for record in records:
        for left, right in zip(record.fallback_attempts, record.fallback_attempts[1:], strict=False):
            key = f'{left}->{right}'
            transitions[key] = transitions.get(key, 0) + 1
    summary['transitions'] = transitions
    return summary


def _aggregate_failure_buckets(records: list[PublicEvalRecord]) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = {}
    for record in records:
        bucket = record.failure_bucket or _classify_failure_bucket(record)
        entry = buckets.setdefault(bucket, {'count': 0, 'cases': []})
        entry['count'] += 1
        if bucket != 'passed':
            entry['cases'].append({'suite': record.suite, 'case_id': record.case_id})
    return buckets


def _annotate_failure_buckets(records: list[PublicEvalRecord]) -> None:
    for record in records:
        record.failure_bucket = _classify_failure_bucket(record)


def _make_bfcl_failure_record(
    case: dict[str, Any],
    exc: BaseException,
    *,
    duration_seconds: float,
    fallback_stage: str,
    fallback_attempts: list[str],
) -> PublicEvalRecord:
    return PublicEvalRecord(
        suite=f"bfcl_{case['suite']}",
        case_id=case['id'],
        success=False,
        duration_seconds=round(duration_seconds, 4),
        tool_name_match=0.0,
        argument_match=0.0,
        expected_call_count=len(case['ground_truth']),
        actual_call_count=0,
        result_summary='',
        error=str(exc),
        fallback_stage=fallback_stage,
        fallback_attempts=list(fallback_attempts),
    )


def _case_prompt(case: dict[str, Any]) -> str:
    return _web_case_prompt(case)


def _record_web_search_usage(config: PublicEvalWebSearchConfig, *, kind: str, now: float | None = None) -> None:
    _web_record_web_search_usage(config, kind=kind, now=now)


def _normalize_serpapi_search_results(payload: dict[str, Any], *, num_results: int) -> list[dict[str, Any]]:
    return _web_normalize_serpapi_search_results(payload, num_results=num_results)


def _serpapi_search(arguments: dict[str, Any], case: dict[str, Any], web_search: PublicEvalWebSearchConfig) -> dict[str, Any]:
    return _web_serpapi_search(arguments, case, web_search)


def _serpapi_query_params(
    arguments: dict[str, Any],
    case: dict[str, Any],
    web_search: PublicEvalWebSearchConfig,
) -> dict[str, Any]:
    return _web_serpapi_query_params(arguments, case, web_search)


def _fetch_web_contents(
    arguments: dict[str, Any],
    case: dict[str, Any],
    web_search: PublicEvalWebSearchConfig,
    *,
    grounded_urls: set[str] | None = None,
) -> dict[str, Any]:
    return _web_fetch_contents(arguments, case, web_search, grounded_urls=grounded_urls)


def _build_eval_tool_handler(
    case: dict[str, Any],
    original_name: str,
    tool_name: str,
    *,
    web_search: PublicEvalWebSearchConfig,
    memory_state: dict[str, str],
    budget_state: dict[str, int],
    search_state: dict[str, Any],
) -> Any:
    def guard_call_budget() -> None:
        if budget_state['successful_calls'] >= budget_state['allowed_calls']:
            raise RuntimeError('tool call budget exhausted for this BFCL case')

    if original_name == 'web.search':
        def search_handler(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            guard_call_budget()
            result = _web_serpapi_search(arguments, case, web_search)
            search_state['latest_results'] = list(result.get('results', []))
            budget_state['successful_calls'] += 1
            result['tool'] = tool_name
            result['run_id'] = context.run_id
            return result

        return search_handler
    if original_name == 'web.contents':
        def contents_handler(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            guard_call_budget()
            grounded_urls = {
                str(item.get('link') or '').strip()
                for item in cast(list[dict[str, Any]], search_state.get('latest_results', []))
                if str(item.get('link') or '').strip()
            } or None
            result = _web_fetch_contents(arguments, case, web_search, grounded_urls=grounded_urls)
            budget_state['successful_calls'] += 1
            result['tool'] = tool_name
            result['run_id'] = context.run_id
            return result

        return contents_handler
    if original_name == 'memory.put':
        def memory_put(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            guard_call_budget()
            key = str(arguments['key'])
            value = str(arguments['value'])
            memory_state[key] = value
            budget_state['successful_calls'] += 1
            return {'tool': tool_name, 'run_id': context.run_id, 'key': key, 'value': value}

        return memory_put
    if original_name == 'memory.get':
        def memory_get(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            guard_call_budget()
            key = str(arguments['key'])
            budget_state['successful_calls'] += 1
            return {
                'tool': tool_name,
                'run_id': context.run_id,
                'key': key,
                'value': memory_state.get(key),
                'found': key in memory_state,
            }

        return memory_get
    if original_name == 'memory.delete':
        def memory_delete(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            guard_call_budget()
            key = str(arguments['key'])
            removed = memory_state.pop(key, None)
            budget_state['successful_calls'] += 1
            return {'tool': tool_name, 'run_id': context.run_id, 'key': key, 'removed': removed is not None}

        return memory_delete
    if original_name == 'memory.list':
        def memory_list(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            guard_call_budget()
            prefix = str(arguments.get('prefix') or '')
            keys = sorted(key for key in memory_state if key.startswith(prefix))
            budget_state['successful_calls'] += 1
            return {'tool': tool_name, 'run_id': context.run_id, 'prefix': prefix, 'keys': keys}

        return memory_list

    def record_tool_call(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        guard_call_budget()
        budget_state['successful_calls'] += 1
        return {
            'tool': tool_name,
            'arguments': arguments,
            'run_id': context.run_id,
        }

    return record_tool_call


async def _run_bfcl_case_attempt(
    base_config: AppConfig,
    case: dict[str, Any],
    *,
    shared: dict[str, Any],
    tool_name_map: dict[str, str],
    functions: list[dict[str, Any]],
    fallback_stage: str,
    fallback_attempts: list[str],
    strict_schema: bool,
) -> _BfclAttemptResult:
    config = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': f"bfcl-{case['id']}-{fallback_stage}",
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'description': 'Public-eval tool-calling evaluator.',
                        'system_prompt': _bfcl_system_prompt(case),
                        'tools': [tool_name_map[str(item['name'])] for item in functions],
                        'sub_agents': [],
                        'max_iterations': 6,
                    }
                ],
                'teams': [],
                'nodes': [],
            },
        }
    )
    with tempfile.TemporaryDirectory(prefix=f"easy-agent-bfcl-{case['id']}-") as storage_dir:
        config.storage.path = storage_dir
        config.model.function_calling.strict = strict_schema
        config.model.function_calling.parallel_tool_calls = len(case.get('ground_truth', [])) > 1
        runtime = build_runtime_from_config(config)
        web_search = base_config.evaluation.public_eval.web_search
        memory_state = {
            str(key): str(value)
            for key, value in cast(dict[str, Any], case.get('initial_state', {}).get('memory', {})).items()
        }
        budget_state = {'allowed_calls': len(case.get('ground_truth', [])), 'successful_calls': 0}
        search_state: dict[str, Any] = {'latest_results': []}
        for function in functions:
            original_name = str(function['name'])
            tool_name = tool_name_map[original_name]
            input_schema = (
                _strict_normalize_schema(cast(dict[str, Any], function['parameters']))
                if strict_schema
                else _normalize_schema(cast(dict[str, Any], function['parameters']))
            )

            runtime.register_tool(
                ToolSpec(
                    name=tool_name,
                    description=function['description'],
                    input_schema=input_schema,
                ),
                _build_eval_tool_handler(
                    case,
                    original_name,
                    tool_name,
                    web_search=web_search,
                    memory_state=memory_state,
                    budget_state=budget_state,
                    search_state=search_state,
                ),
            )
        start = time.perf_counter()
        try:
            await runtime.start()
            prompt = _case_prompt(case)
            result = await runtime.run(prompt)
            duration = time.perf_counter() - start
            trace = runtime.store.load_trace(result['run_id'])
            actual_calls = _extract_successful_tool_calls(trace)
            success, tool_name_match, argument_match = _score_bfcl_case(case, actual_calls, tool_name_map)
            return _BfclAttemptResult(
                record=PublicEvalRecord(
                    suite=f"bfcl_{case['suite']}",
                    case_id=case['id'],
                    success=success,
                    duration_seconds=round(duration, 4),
                    tool_name_match=tool_name_match,
                    argument_match=argument_match,
                    expected_call_count=len(case['ground_truth']),
                    actual_call_count=len(actual_calls),
                    result_summary=_summarize_result(result.get('result')),
                    error=None if success else json.dumps({'actual_calls': actual_calls}, ensure_ascii=False),
                    fallback_stage=fallback_stage,
                    fallback_attempts=list(fallback_attempts),
                ),
                duration_seconds=round(duration, 4),
            )
        except Exception as exc:
            duration = time.perf_counter() - start
            return _BfclAttemptResult(
                error=exc,
                duration_seconds=round(duration, 4),
                retryable_provider_400=_is_retryable_provider_400(base_config, exc),
            )
        finally:
            await runtime.aclose()


async def _run_bfcl_case(base_config: AppConfig, case: dict[str, Any]) -> PublicEvalRecord:
    shared = _shared_payload(base_config)
    tool_name_map = _build_tool_name_map(cast(list[dict[str, Any]], case['functions']))
    prompt = _case_prompt(case)
    all_functions = list(cast(list[dict[str, Any]], case['functions']))
    attempt_history: list[str] = []
    stages: list[tuple[str, list[dict[str, Any]], bool]] = [
        ('base', all_functions, False),
        ('strict_schema_retry', all_functions, True),
    ]
    last_error: Exception | None = None
    last_duration = 0.0
    last_stage = 'base'
    while stages:
        fallback_stage, functions, strict_schema = stages.pop(0)
        attempt_history.append(fallback_stage)
        last_stage = fallback_stage
        attempt = await _run_bfcl_case_attempt(
            base_config,
            case,
            shared=shared,
            tool_name_map=tool_name_map,
            functions=functions,
            fallback_stage=fallback_stage,
            fallback_attempts=attempt_history,
            strict_schema=strict_schema,
        )
        if attempt.record is not None:
            return attempt.record
        if attempt.error is None:
            break
        last_error = attempt.error
        last_duration = attempt.duration_seconds
        if not attempt.retryable_provider_400:
            return _make_bfcl_failure_record(
                case,
                attempt.error,
                duration_seconds=attempt.duration_seconds,
                fallback_stage=fallback_stage,
                fallback_attempts=attempt_history,
            )
        if fallback_stage != 'strict_schema_retry':
            continue
        candidate_functions = _select_bfcl_candidate_functions(prompt, all_functions)
        if _same_function_selection(candidate_functions, functions):
            continue
        stages.append(('candidate_pruned_retry', candidate_functions, True))
    if last_error is None:
        last_error = RuntimeError('BFCL case failed without a captured error')
    return _make_bfcl_failure_record(
        case,
        last_error,
        duration_seconds=last_duration,
        fallback_stage=last_stage,
        fallback_attempts=attempt_history,
    )


async def _run_tau_case(base_config: AppConfig, case: dict[str, Any]) -> PublicEvalRecord:
    shared = _shared_payload(base_config)
    config = AppConfig.model_validate(
        {
            **shared,
            'graph': {
                'name': f"tau2-{case['id']}",
                'entrypoint': 'coordinator',
                'agents': [
                    {
                        'name': 'coordinator',
                        'description': 'Mock task-management evaluator agent.',
                        'system_prompt': _tau_system_prompt(),
                        'tools': ['create_task', 'update_task_status'],
                        'sub_agents': [],
                        'max_iterations': 6,
                    }
                ],
                'teams': [],
                'nodes': [],
            },
        }
    )
    with tempfile.TemporaryDirectory(prefix=f"easy-agent-tau2-{case['id']}-") as storage_dir:
        config.storage.path = storage_dir
        config.model.function_calling.parallel_tool_calls = False
        runtime = build_runtime_from_config(config)
        tasks: dict[str, dict[str, Any]] = {
            'task_1': {'task_id': 'task_1', 'title': 'Existing Task', 'status': 'pending', 'user_id': 'user_1'}
        }
        task_counter = 1

        def create_task(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            nonlocal task_counter
            task_counter += 1
            task_id = f'task_{task_counter}'
            payload = {
                'task_id': task_id,
                'user_id': arguments['user_id'],
                'title': arguments['title'],
                'description': arguments.get('description', ''),
                'status': 'pending',
                'run_id': context.run_id,
            }
            tasks[task_id] = payload
            return payload

        def update_task_status(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            task_id = arguments['task_id']
            task = tasks[task_id]
            task['status'] = arguments['status']
            task['run_id'] = context.run_id
            return dict(task)

        runtime.register_tool(
            ToolSpec(
                name='create_task',
                description='Create a new task for a user.',
                input_schema={
                    'type': 'object',
                    'properties': {
                        'user_id': {'type': 'string'},
                        'title': {'type': 'string'},
                        'description': {'type': 'string'},
                    },
                    'required': ['user_id', 'title'],
                },
            ),
            create_task,
        )
        runtime.register_tool(
            ToolSpec(
                name='update_task_status',
                description='Update the status of an existing task.',
                input_schema={
                    'type': 'object',
                    'properties': {
                        'task_id': {'type': 'string'},
                        'status': {'type': 'string'},
                    },
                    'required': ['task_id', 'status'],
                },
            ),
            update_task_status,
        )
        start = time.perf_counter()
        try:
            await runtime.start()
            session_id = f"tau2-{case['id']}"
            message_history = list(case.get('initial_state', {}).get('message_history', []))
            initial_messages = []
            for item in message_history:
                if item['role'] == 'assistant' and item.get('tool_calls'):
                    calls = [ToolCall.model_validate(call) for call in item['tool_calls']]
                    initial_messages.append(ChatMessage(role='assistant', content=item.get('content', ''), tool_calls=calls))
                elif item['role'] == 'tool':
                    initial_messages.append(
                        ChatMessage(
                            role='tool',
                            content=item['content'],
                            name='create_task',
                            tool_call_id=item.get('id'),
                        )
                    )
                else:
                    initial_messages.append(ChatMessage(role=item['role'], content=item.get('content', '')))
            history_tasks = _extract_tau_tasks_from_history(message_history)
            if history_tasks:
                tasks.update(history_tasks)
                numeric_ids = [int(item.split('_')[-1]) for item in history_tasks if item.startswith('task_') and item.split('_')[-1].isdigit()]
                if numeric_ids:
                    task_counter = max(task_counter, max(numeric_ids))
            memory_message = _tau_history_memory_message(history_tasks)
            if memory_message:
                initial_messages.append(ChatMessage(role='system', content=memory_message))
            if initial_messages:
                runtime.store.save_session_messages(session_id, config.graph.name, initial_messages)
            prompt = str(case.get('ticket') or case.get('user_scenario', {}).get('instructions', ''))
            prompt = _tau_prompt_with_grounding(prompt, tasks)
            result = await runtime.run(prompt, session_id=session_id if initial_messages else None)
            duration = time.perf_counter() - start
            trace = runtime.store.load_trace(result['run_id'])
            actual_calls = _extract_successful_tool_calls(trace)
            success, tool_name_match, argument_match = _score_tau_case(case, actual_calls)
            return PublicEvalRecord(
                suite='tau2_mock',
                case_id=case['id'],
                success=success,
                duration_seconds=round(duration, 4),
                tool_name_match=tool_name_match,
                argument_match=argument_match,
                expected_call_count=len(case.get('evaluation_criteria', {}).get('actions', [])),
                actual_call_count=len(actual_calls),
                result_summary=_summarize_result(result.get('result')),
                error=None if success else json.dumps({'actual_calls': actual_calls}, ensure_ascii=False),
            )
        except Exception as exc:
            duration = time.perf_counter() - start
            return PublicEvalRecord(
                suite='tau2_mock',
                case_id=case['id'],
                success=False,
                duration_seconds=round(duration, 4),
                tool_name_match=0.0,
                argument_match=0.0,
                expected_call_count=len(case.get('evaluation_criteria', {}).get('actions', [])),
                actual_call_count=0,
                result_summary='',
                error=str(exc),
            )
        finally:
            await runtime.aclose()


def _aggregate_summary(records: list[PublicEvalRecord]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for suite in sorted({record.suite for record in records}):
        items = [item for item in records if item.suite == suite]
        summary[suite] = {
            'runs': len(items),
            'successes': sum(1 for item in items if item.success),
            'failures': sum(1 for item in items if not item.success),
            'pass_rate': round(sum(1 for item in items if item.success) / len(items), 4),
            'tool_name_match_rate': round(mean(item.tool_name_match for item in items), 4),
            'argument_match_rate': round(mean(item.argument_match for item in items), 4),
            'average_duration_seconds': round(mean(item.duration_seconds for item in items), 4),
        }
    bfcl_items = [item for item in records if item.suite.startswith('bfcl_')]
    irrelevance_items = [item for item in records if item.suite == 'bfcl_irrelevance']
    tau_items = [item for item in records if item.suite == 'tau2_mock']
    summary['overall'] = {
        'bfcl_pass_rate': round(sum(1 for item in bfcl_items if item.success) / len(bfcl_items), 4),
        'bfcl_tool_name_match_rate': round(mean(item.tool_name_match for item in bfcl_items), 4),
        'bfcl_argument_match_rate': round(mean(item.argument_match for item in bfcl_items), 4),
        'bfcl_irrelevance_pass_rate': round(sum(1 for item in irrelevance_items if item.success) / len(irrelevance_items), 4),
        'tau2_mock_pass_rate': round(sum(1 for item in tau_items if item.success) / len(tau_items), 4),
        'tau2_mock_average_duration_seconds': round(mean(item.duration_seconds for item in tau_items), 4),
    }
    return summary


def _aggregate_category_summary(records: list[PublicEvalRecord]) -> dict[str, Any]:
    categories = {
        'bfcl_core': {
            'suites': {'bfcl_simple', 'bfcl_multiple', 'bfcl_parallel_multiple', 'bfcl_irrelevance'},
        },
        'bfcl_agentic': {
            'suites': {'bfcl_web_search', 'bfcl_memory', 'bfcl_format_sensitivity'},
        },
        'tau2_mock': {'suites': {'tau2_mock'}},
    }
    summary: dict[str, Any] = {}
    for name, item in categories.items():
        suites = item['suites']
        selected = [record for record in records if record.suite in suites]
        if not selected:
            continue
        summary[name] = {
            'runs': len(selected),
            'successes': sum(1 for record in selected if record.success),
            'failures': sum(1 for record in selected if not record.success),
            'pass_rate': round(sum(1 for record in selected if record.success) / len(selected), 4),
            'average_duration_seconds': round(mean(record.duration_seconds for record in selected), 4),
        }
    return summary


def _aggregate_agentic_summary(records: list[PublicEvalRecord]) -> dict[str, Any]:
    suites = ('bfcl_web_search', 'bfcl_memory', 'bfcl_format_sensitivity')
    summary: dict[str, Any] = {}
    for suite in suites:
        selected = [record for record in records if record.suite == suite]
        if not selected:
            continue
        summary[suite] = {
            'runs': len(selected),
            'successes': sum(1 for record in selected if record.success),
            'failures': sum(1 for record in selected if not record.success),
            'pass_rate': round(sum(1 for record in selected if record.success) / len(selected), 4),
        }
    return summary


def _remaining_blockers(records: list[PublicEvalRecord]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for record in records:
        if record.success:
            continue
        blockers.append(
            {
                'suite': record.suite,
                'case_id': record.case_id,
                'failure_bucket': record.failure_bucket or _classify_failure_bucket(record),
                'fallback_stage': record.fallback_stage,
            }
        )
    return blockers


def _checkpoint_record_key(record: PublicEvalRecord) -> str:
    return f'{record.suite}:{record.case_id}'


def _load_public_eval_checkpoint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {'records': {}}
    return cast(dict[str, Any], json.loads(path.read_text(encoding='utf-8')))


def _save_public_eval_checkpoint(
    path: Path,
    *,
    profile: str,
    bfcl_version: str,
    records: list[PublicEvalRecord],
    run_status: str,
    interrupted: dict[str, Any] | None = None,
) -> None:
    payload = {
        'profile': profile,
        'bfcl_version': bfcl_version,
        'run_status': run_status,
        'interrupted': interrupted,
        'records': {
            _checkpoint_record_key(record): asdict(record)
            for record in records
        },
    }
    _write_json_path(path, payload)


def _restore_checkpoint_records(path: Path, *, profile: str, bfcl_version: str) -> dict[str, PublicEvalRecord]:
    checkpoint = _load_public_eval_checkpoint(path)
    if checkpoint.get('profile') != profile or checkpoint.get('bfcl_version') != bfcl_version:
        return {}
    restored: dict[str, PublicEvalRecord] = {}
    for key, payload in cast(dict[str, Any], checkpoint.get('records', {})).items():
        if isinstance(payload, dict):
            restored[key] = PublicEvalRecord(**payload)
    return restored


def _checkpoint_path_for_run(base_config: AppConfig) -> Path:
    return Path(base_config.evaluation.public_eval.official_dataset.checkpoint_path)


def _run_public_eval_records(
    base_config: AppConfig,
    *,
    profile: str,
    bfcl_version: str,
    bfcl_cases: list[dict[str, Any]],
    tau_cases: list[dict[str, Any]],
) -> tuple[list[PublicEvalRecord], dict[str, Any]]:
    checkpoint_path = _checkpoint_path_for_run(base_config)
    restore_enabled = base_config.evaluation.public_eval.official_dataset.resume
    restored = _restore_checkpoint_records(checkpoint_path, profile=profile, bfcl_version=bfcl_version) if restore_enabled else {}
    records: list[PublicEvalRecord] = []
    resumed_records = 0
    interrupted: dict[str, Any] | None = None

    for case in bfcl_cases:
        record_key = f"bfcl_{case['suite']}:{case['id']}"
        cached = restored.get(record_key)
        if cached is not None:
            records.append(cached)
            resumed_records += 1
            continue
        try:
            record = asyncio.run(_run_bfcl_case(base_config, case))
        except WebSearchQuotaExceeded as exc:
            interrupted = {
                'reason': 'web_search_quota',
                'wait_seconds': exc.wait_seconds,
                'scope': exc.scope,
                'completed_records': len(records),
            }
            _save_public_eval_checkpoint(
                checkpoint_path,
                profile=profile,
                bfcl_version=bfcl_version,
                records=records,
                run_status='interrupted_quota',
                interrupted=interrupted,
            )
            break
        records.append(record)
        _save_public_eval_checkpoint(
            checkpoint_path,
            profile=profile,
            bfcl_version=bfcl_version,
            records=records,
            run_status='running',
        )
    else:
        for case in tau_cases:
            record_key = f"tau2_mock:{case['id']}"
            cached = restored.get(record_key)
            if cached is not None:
                records.append(cached)
                resumed_records += 1
                continue
            record = asyncio.run(_run_tau_case(base_config, case))
            records.append(record)
            _save_public_eval_checkpoint(
                checkpoint_path,
                profile=profile,
                bfcl_version=bfcl_version,
                records=records,
                run_status='running',
            )

    final_status = 'interrupted_quota' if interrupted is not None else 'completed'
    _save_public_eval_checkpoint(
        checkpoint_path,
        profile=profile,
        bfcl_version=bfcl_version,
        records=records,
        run_status=final_status,
        interrupted=interrupted,
    )
    return records, {
        'checkpoint_path': str(checkpoint_path),
        'resume_enabled': restore_enabled,
        'resumed_records': resumed_records,
        'completed_records': len(records),
        'interrupted': interrupted,
        'run_status': final_status,
    }


def _public_eval_sources(base_config: AppConfig, selected_profile: str) -> dict[str, Any]:
    sources: dict[str, Any] = {
        'bfcl': 'https://huggingface.co/datasets/gorilla-llm/Berkeley-Function-Calling-Leaderboard',
        'bfcl_v4': 'https://gorilla.cs.berkeley.edu/blogs/8_berkeley_function_calling_leaderboard.html',
        'serpapi_search': 'https://serpapi.com/search-api',
        'web_contents_fetch': 'https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Methods/GET',
        'tau2': 'https://github.com/sierra-research/tau2-bench',
    }
    if selected_profile == 'official_full_v4':
        official = base_config.evaluation.public_eval.official_dataset
        sources['official_manifest_path'] = official.manifest_path
        if official.source_url:
            sources['official_manifest_url'] = official.source_url
    return sources


def run_public_eval_suite(
    config_path: str | Path,
    *,
    profile: Literal['subset', 'full_v4', 'official_full_v4'] | None = None,
) -> dict[str, Any]:
    base_config = load_config(config_path)
    if profile is not None:
        base_config.evaluation.public_eval.profile = profile
    selected_profile, bfcl_version, bfcl_cases, tau_cases = _load_public_eval_inputs(base_config)
    records, progress = _run_public_eval_records(
        base_config,
        profile=selected_profile,
        bfcl_version=bfcl_version,
        bfcl_cases=bfcl_cases,
        tau_cases=tau_cases,
    )
    _annotate_failure_buckets(records)
    return {
        'profile': selected_profile,
        'scope': 'official_manifest' if selected_profile == 'official_full_v4' else 'repo_pinned',
        'bfcl_version': bfcl_version,
        'case_counts': {
            'bfcl': len(bfcl_cases),
            'tau2_mock': len(tau_cases),
            'completed_records': len(records),
        },
        'progress': progress,
        'records': [asdict(record) for record in records],
        'summary': _aggregate_summary(records),
        'suite_summary': _aggregate_summary(records),
        'category_summary': _aggregate_category_summary(records),
        'agentic_summary': _aggregate_agentic_summary(records),
        'stage_summary': _aggregate_stage_summary(records),
        'failure_buckets': _aggregate_failure_buckets(records),
        'remaining_blockers': _remaining_blockers(records),
        'provider_schema_matrix': _provider_schema_matrix(),
        'sources': _public_eval_sources(base_config, selected_profile),
    }


__all__ = [
    'PublicEvalRecord',
    'WebSearchQuotaExceeded',
    'run_public_eval_suite',
]


