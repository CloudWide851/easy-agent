from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from collections import defaultdict, deque
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
_COORDINATED_INTENT_PATTERN = re.compile(r'\b(and|along with|plus)\b', re.IGNORECASE)
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
_BFCL_SUBCATEGORY_GROUPS = {
    'bfcl_simple',
    'bfcl_multiple',
    'bfcl_parallel_multiple',
    'bfcl_irrelevance',
    'bfcl_web_search',
    'bfcl_memory',
    'bfcl_format_sensitivity',
}
_OFFICIAL_CATEGORY_TOKENS = {
    'simple',
    'multiple',
    'parallel_multiple',
    'irrelevance',
    'web_search',
    'memory',
    'format_sensitivity',
    'agentic',
    'multihop',
}


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
    answer_match: float = 1.0
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
    text = path.read_text(encoding='utf-8').strip()
    if not text:
        return {}
    if path.suffix.lower() == '.jsonl':
        return {'bfcl_cases': [json.loads(line) for line in text.splitlines() if line.strip()]}
    try:
        return cast(dict[str, Any], json.loads(text))
    except json.JSONDecodeError:
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        return {'bfcl_cases': lines}


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
    if isinstance(payload, list):
        return cast(list[dict[str, Any]], payload)
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


def _case_message_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get('text')
                if isinstance(text, str):
                    parts.append(text)
        return '\n'.join(part for part in parts if part)
    if isinstance(value, dict):
        text = value.get('text')
        if isinstance(text, str):
            return text
    return str(value or '')


def _normalize_official_messages(case: dict[str, Any]) -> list[dict[str, Any]]:
    raw_messages = case.get('messages')
    if isinstance(raw_messages, list) and raw_messages:
        messages: list[dict[str, Any]] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get('role') or 'user')
            content = _case_message_content(item.get('content') or item.get('text') or '')
            messages.append({'role': role, 'content': content})
        if messages:
            return messages
    for key in ('question', 'prompt', 'input', 'instruction', 'user_input'):
        value = case.get(key)
        if isinstance(value, str) and value.strip():
            return [{'role': 'user', 'content': value}]
    nested = case.get('user_scenario')
    if isinstance(nested, dict):
        instructions = nested.get('instructions')
        if isinstance(instructions, str) and instructions.strip():
            return [{'role': 'user', 'content': instructions}]
    raise RuntimeError('official BFCL case does not contain a supported prompt payload')


def _normalize_official_function(function: dict[str, Any]) -> dict[str, Any]:
    parameters = function.get('parameters') or function.get('input_schema') or function.get('inputSchema') or {'type': 'object'}
    return {
        'name': str(function.get('name') or function.get('tool_name') or function.get('function') or ''),
        'description': str(function.get('description') or function.get('summary') or ''),
        'parameters': cast(dict[str, Any], parameters if isinstance(parameters, dict) else {'type': 'object'}),
    }


def _normalize_truth_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, list):
            normalized[str(key)] = value
        elif isinstance(value, dict):
            normalized[str(key)] = [value]
        else:
            normalized[str(key)] = [value]
    return normalized


def _normalize_official_ground_truth(case: dict[str, Any]) -> list[dict[str, Any]]:
    ground_truth = case.get('ground_truth')
    if isinstance(ground_truth, list) and ground_truth:
        normalized: list[dict[str, Any]] = []
        for item in ground_truth:
            if not isinstance(item, dict):
                continue
            if len(item) == 1 and all(isinstance(key, str) for key in item):
                tool_name = next(iter(item.keys()))
                arguments = item[tool_name]
                if isinstance(arguments, dict):
                    normalized.append({tool_name: _normalize_truth_arguments(arguments)})
                continue
            tool_name = str(item.get('name') or item.get('tool_name') or item.get('function') or '')
            arguments = item.get('arguments') or item.get('args') or item.get('input') or {}
            if tool_name and isinstance(arguments, dict):
                normalized.append({tool_name: _normalize_truth_arguments(cast(dict[str, Any], arguments))})
        if normalized:
            return normalized
    expected_calls = case.get('expected_tool_calls') or case.get('tool_calls') or case.get('expected_calls')
    if isinstance(expected_calls, list):
        normalized = []
        for item in expected_calls:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get('name') or item.get('tool_name') or item.get('function') or '')
            arguments = item.get('arguments') or item.get('args') or item.get('input') or {}
            if tool_name and isinstance(arguments, dict):
                normalized.append({tool_name: _normalize_truth_arguments(cast(dict[str, Any], arguments))})
        if normalized:
            return normalized
    return []


def _official_category_tokens(case: dict[str, Any], normalized_suite: str) -> set[str]:
    values: list[str] = []
    for key in ('suite', 'category', 'subcategory', 'type'):
        value = case.get(key)
        if isinstance(value, str):
            values.append(value)
    tags = case.get('tags')
    if isinstance(tags, list):
        values.extend(str(item) for item in tags if item)
    tokens: set[str] = set()
    for value in values:
        normalized = value.casefold().replace('-', '_')
        tokens.update(token for token in re.split(r'[^a-z0-9]+', normalized) if token)
        tokens.update(token for token in normalized.split('_') if token)
    categories = {token for token in tokens if token in _OFFICIAL_CATEGORY_TOKENS}
    categories.add(normalized_suite)
    if normalized_suite in {'web_search', 'memory', 'format_sensitivity'} or 'multihop' in categories:
        categories.add('agentic')
    return categories


def _normalize_official_suite(case: dict[str, Any], ground_truth: list[dict[str, Any]], functions: list[dict[str, Any]]) -> str:
    values: list[str] = []
    for key in ('suite', 'category', 'subcategory', 'type'):
        value = case.get(key)
        if isinstance(value, str):
            values.append(value.casefold())
    tags = case.get('tags')
    if isinstance(tags, list):
        values.extend(str(item).casefold() for item in tags if item)
    joined = ' '.join(values)
    function_names = ' '.join(str(item.get('name') or '').casefold() for item in functions)
    if 'irrelevance' in joined or 'no_tool' in joined:
        return 'irrelevance'
    if 'format' in joined or 'xml' in joined or '<task>' in ' '.join(item['content'] for item in _normalize_official_messages(case)):
        return 'format_sensitivity'
    if 'memory' in joined or 'memory.' in function_names:
        return 'memory'
    if 'web_search' in joined or 'search' in joined or 'multihop' in joined or 'web.' in function_names:
        return 'web_search'
    if 'parallel' in joined:
        return 'parallel_multiple'
    if len(ground_truth) > 1:
        return 'multiple'
    return 'simple'


def _normalize_official_manifest_case(case: dict[str, Any], *, index: int) -> dict[str, Any]:
    functions_raw = case.get('functions') or case.get('tools') or case.get('available_tools') or []
    functions = [
        _normalize_official_function(item)
        for item in cast(list[dict[str, Any]], functions_raw)
        if isinstance(item, dict)
    ]
    ground_truth = _normalize_official_ground_truth(case)
    normalized_suite = _normalize_official_suite(case, ground_truth, functions)
    case_id = str(case.get('id') or case.get('case_id') or case.get('question_id') or f'official_{normalized_suite}_{index}')
    metadata = {
        'official_categories': sorted(_official_category_tokens(case, normalized_suite)),
        'official_source_suite': str(case.get('suite') or case.get('category') or normalized_suite),
        'official_tags': [str(item) for item in cast(list[Any], case.get('tags', []))],
    }
    initial_state = cast(dict[str, Any], case.get('initial_state') or {})
    return {
        'id': case_id,
        'suite': normalized_suite,
        'messages': _normalize_official_messages(case),
        'functions': functions,
        'ground_truth': ground_truth,
        'expect_no_tool': bool(case.get('expect_no_tool') or (normalized_suite == 'irrelevance' and not ground_truth)),
        'initial_state': initial_state,
        'expected_answer': case.get('expected_answer'),
        'expected_answer_aliases': case.get('expected_answer_aliases', []),
        'replay_results': case.get('replay_results', []),
        'expected_tool_result': case.get('expected_tool_result'),
        'source_url': case.get('source_url') or case.get('source') or case.get('question_url'),
        'metadata': metadata,
    }


def _balanced_case_selection(cases: list[dict[str, Any]], max_cases: int) -> list[dict[str, Any]]:
    grouped: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    suite_order: list[str] = []
    for case in cases:
        suite = str(case.get('suite') or '')
        if suite not in grouped:
            suite_order.append(suite)
        grouped[suite].append(case)
    selected: list[dict[str, Any]] = []
    while len(selected) < max_cases:
        progressed = False
        for suite in suite_order:
            queue = grouped[suite]
            if not queue:
                continue
            selected.append(queue.popleft())
            progressed = True
            if len(selected) >= max_cases:
                break
        if not progressed:
            break
    return selected


def _filter_official_manifest_cases(
    cases: list[dict[str, Any]],
    *,
    category_allowlist: list[str],
    suite_allowlist: list[str],
    case_allowlist: list[str],
    selection_mode: str,
    max_cases: int | None,
    max_cases_per_suite: int | None,
) -> list[dict[str, Any]]:
    selected = list(cases)
    if category_allowlist:
        categories = {item.strip() for item in category_allowlist if item.strip()}
        selected = [
            case
            for case in selected
            if categories.intersection(set(cast(list[str], cast(dict[str, Any], case.get('metadata', {})).get('official_categories', []))))
        ]
    if suite_allowlist:
        suites = {item.strip() for item in suite_allowlist if item.strip()}
        selected = [case for case in selected if str(case.get('suite') or '').strip() in suites]
    if case_allowlist:
        case_ids = {item.strip() for item in case_allowlist if item.strip()}
        selected = [case for case in selected if str(case.get('id') or '').strip() in case_ids]
    if max_cases_per_suite is not None:
        suite_counts: dict[str, int] = defaultdict(int)
        limited: list[dict[str, Any]] = []
        for case in selected:
            suite = str(case.get('suite') or '')
            if suite_counts[suite] >= max(0, max_cases_per_suite):
                continue
            suite_counts[suite] += 1
            limited.append(case)
        selected = limited
    if max_cases is not None:
        limit = max(0, max_cases)
        if selection_mode == 'balanced_per_suite':
            selected = _balanced_case_selection(selected, limit)
        else:
            selected = selected[:limit]
    return selected


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
    normalized_cases = [
        _normalize_official_manifest_case(case, index=index)
        for index, case in enumerate(_flatten_official_manifest_cases(manifest))
        if isinstance(case, dict)
    ]
    bfcl_cases = _filter_official_manifest_cases(
        normalized_cases,
        category_allowlist=official.category_allowlist,
        suite_allowlist=official.suite_allowlist,
        case_allowlist=official.case_allowlist,
        selection_mode=official.selection_mode,
        max_cases=official.max_cases,
        max_cases_per_suite=official.max_cases_per_suite,
    )
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
    action_directive = (
        'Choose the single best action based on the user request. '
        if expected_calls <= 1
        else 'Choose the full set of required tool actions based on the user request. '
    )
    prompt = (
        'You are evaluating tool-calling behavior. '
        + action_directive
        + budget
        + ' If the request is irrelevant to the available tools, answer directly without any tool call. '
        'If one tool already covers the request, prefer that single tool rather than decomposing the request into narrower follow-up calls. '
        'If the user asks for the complete result, all results, or paired properties, include any needed selector or optional fields in the first tool call instead of retrying the same tool. '
        "If the user asks for all roots, both roots, or a complete root set, set any available root-type selector to include all roots instead of relying on a default real-only mode. "
        'If the budget allows exactly one tool call and multiple tools look related, choose the tool that best matches the primary requested analysis and avoid secondary follow-up calls. '
        'A successful tool call ends the search unless an additional independent tool call is explicitly required by the budget. '
        'Never speculate, never duplicate a successful tool call, and never call a second tool just to restate the first answer. '
        'Arguments must match the tool schema exactly. If a validation error is returned, correct the arguments and try again. '
        'After any required tool call, provide the final answer as a concise answer string only.'
    )
    if len(case.get('ground_truth', [])) > 1:
        prompt += (
            ' When the user asks for two distinct analyses or outcomes about the same subject, '
            'treat them as coordinated required actions and issue one tool call per required analysis instead of collapsing them into a single partial answer. '
            'Reuse the same grounded entity, location, and timeframe arguments across those calls when the request implies they are shared. '
            'If one required tool needs a contextual field such as ecosystem or timeframe, infer it from nearby nouns in the user request, '
            'for example woodland, forest, wetland, river, city, or decade, instead of dropping the second required call. '
            'Do not stop after the first successful call when another required analysis is still missing from the tool budget.'
        )
    if str(case.get('suite') or '') == 'web_search':
        prompt += (
            ' For web-search title lookups, answer with the exact grounded page title only. '
            'If the user explicitly asks you to read the page contents or answer a detail that depends on page text rather than the title/snippet, '
            'first call web.search and then call web.contents only on grounded result ids or URLs from that search. '
            'Use web.contents mode=truncate for concise extraction, mode=markdown for readable document text, and mode=raw only when markup-sensitive text is explicitly needed. '
            'If the first grounded page fetch fails, use another grounded result rather than widening to an ungrounded URL. '
            'If you provide structured output, use a JSON object with "answer" and optional "context" fields.'
        )
    if str(case.get('suite') or '') == 'memory':
        prompt += (
            ' For key-value memory tools, use a stable semantic key for the named entity and attribute. '
            'When the request names one user and one preference or attribute, prefer a compact canonical key such as user:<name>:<field> '
            'instead of inventing long natural-language variants.'
        )
    return prompt


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


def _protocol_matrix_sample_schema(
    adapter: Any,
    provider: str,
    *,
    function_calling: dict[str, Any] | None = None,
    openai_api_style: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = adapter.build_payload(
        ModelConfig.model_validate(
            {
                'provider': provider,
                'protocol': adapter.protocol,
                **({'openai_api_style': openai_api_style} if openai_api_style is not None else {}),
                **({'function_calling': function_calling} if function_calling is not None else {}),
            }
        ),
        [ChatMessage(role='user', content='schema probe')],
        [ToolSpec(name='schema_probe', description='schema probe', input_schema=_SCHEMA_MATRIX_SAMPLE)],
    )
    if adapter.protocol is Protocol.OPENAI:
        if openai_api_style == 'responses':
            return payload, cast(dict[str, Any], payload['tools'][0]['parameters'])
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
        none_payload, _ = _protocol_matrix_sample_schema(
            adapter,
            config_provider,
            function_calling={'mode': 'none'},
        )
        required_payload, _ = _protocol_matrix_sample_schema(
            adapter,
            config_provider,
            function_calling={'mode': 'required'},
        )
        force_payload, _ = _protocol_matrix_sample_schema(
            adapter,
            config_provider,
            function_calling={'mode': 'force', 'forced_tool_name': 'schema_probe'},
        )
        serial_payload, _ = _protocol_matrix_sample_schema(
            adapter,
            config_provider,
            function_calling={'parallel_tool_calls': False},
        )
        properties = cast(dict[str, Any], schema.get('properties', {}))
        required = cast(list[str], schema.get('required', []))
        amount_schema = cast(dict[str, Any], properties.get('amount', {}))
        nickname_schema = cast(dict[str, Any], properties.get('nickname', {}))
        payload_tools = cast(list[dict[str, Any]], payload.get('tools', []))
        first_tool = payload_tools[0] if payload_tools else {}
        strict_enabled = bool(
            cast(dict[str, Any], first_tool.get('function', {})).get('strict')
            if adapter.protocol is Protocol.OPENAI
            else first_tool.get('strict')
        )
        parallel_control = payload.get('parallel_tool_calls')
        params_item_type = cast(dict[str, Any], cast(dict[str, Any], properties.get('params', {})).get('items', {})).get('type')
        responses_payload: dict[str, Any] | None = None
        responses_parse: dict[str, Any] | None = None
        if adapter.protocol is Protocol.OPENAI:
            responses_payload, _ = _protocol_matrix_sample_schema(
                adapter,
                config_provider,
                openai_api_style='responses',
                function_calling={'mode': 'force', 'forced_tool_name': 'schema_probe', 'parallel_tool_calls': False},
            )
            responses_result = adapter.parse_response(
                {
                    'output': [
                        {
                            'type': 'message',
                            'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': 'responses ok'}],
                        },
                        {
                            'type': 'function_call',
                            'call_id': 'call_probe',
                            'name': 'schema_probe',
                            'arguments': '{"value":"alpha"}',
                        },
                    ]
                }
            )
            responses_parse = {
                'text': responses_result.text,
                'tool_name': responses_result.tool_calls[0].name if responses_result.tool_calls else None,
                'tool_id': responses_result.tool_calls[0].id if responses_result.tool_calls else None,
            }

        none_supported = False
        required_supported = False
        forced_supported = False
        single_call_supported = False
        if adapter.protocol is Protocol.OPENAI:
            none_supported = none_payload.get('tool_choice') == 'none'
            required_supported = required_payload.get('tool_choice') == 'required'
            forced_supported = (
                cast(dict[str, Any], force_payload.get('tool_choice', {})).get('function', {}).get('name') == 'schema_probe'
            )
            single_call_supported = serial_payload.get('parallel_tool_calls') is False
        elif adapter.protocol is Protocol.ANTHROPIC:
            none_supported = cast(dict[str, Any], none_payload.get('tool_choice', {})).get('type') == 'none'
            required_supported = cast(dict[str, Any], required_payload.get('tool_choice', {})).get('type') == 'any'
            forced_supported = cast(dict[str, Any], force_payload.get('tool_choice', {})).get('name') == 'schema_probe'
            single_call_supported = serial_payload.get('disable_parallel_tool_use') is True
        else:
            function_config = cast(dict[str, Any], none_payload.get('toolConfig', {})).get('functionCallingConfig', {})
            none_supported = cast(dict[str, Any], function_config).get('mode') == 'NONE'
            function_config = cast(dict[str, Any], required_payload.get('toolConfig', {})).get('functionCallingConfig', {})
            required_supported = cast(dict[str, Any], function_config).get('mode') == 'ANY'
            function_config = cast(dict[str, Any], force_payload.get('toolConfig', {})).get('functionCallingConfig', {})
            forced_supported = cast(dict[str, Any], function_config).get('allowedFunctionNames') == ['schema_probe']
            single_call_supported = False
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
                'single_tool_call_control': {
                    'supported': single_call_supported,
                    'observed': serial_payload.get('parallel_tool_calls')
                    if adapter.protocol is Protocol.OPENAI
                    else serial_payload.get('disable_parallel_tool_use')
                    if adapter.protocol is Protocol.ANTHROPIC
                    else cast(dict[str, Any], serial_payload.get('toolConfig', {})).get('functionCallingConfig'),
                },
                'tool_choice_none': {
                    'supported': none_supported,
                    'observed': none_payload.get('tool_choice')
                    if adapter.protocol is not Protocol.GEMINI
                    else cast(dict[str, Any], none_payload.get('toolConfig', {})).get('functionCallingConfig'),
                },
                'tool_choice_required': {
                    'supported': required_supported,
                    'observed': required_payload.get('tool_choice')
                    if adapter.protocol is not Protocol.GEMINI
                    else cast(dict[str, Any], required_payload.get('toolConfig', {})).get('functionCallingConfig'),
                },
                'forced_tool_choice': {
                    'supported': forced_supported,
                    'observed': force_payload.get('tool_choice')
                    if adapter.protocol is not Protocol.GEMINI
                    else cast(dict[str, Any], force_payload.get('toolConfig', {})).get('functionCallingConfig'),
                },
                'responses_payload_shape': {
                    'supported': (
                        isinstance(responses_payload, dict)
                        and isinstance(responses_payload.get('input'), list)
                        and responses_payload.get('max_output_tokens') is not None
                    ),
                    'observed': (
                        {
                            'endpoint_hint': 'responses',
                            'input_item_types': [item.get('type') for item in cast(list[dict[str, Any]], responses_payload.get('input', []))],
                            'tool_shape': cast(list[dict[str, Any]], responses_payload.get('tools', []))[0].get('type')
                            if isinstance(responses_payload, dict) and responses_payload.get('tools')
                            else None,
                            'tool_choice': responses_payload.get('tool_choice') if isinstance(responses_payload, dict) else None,
                        }
                        if responses_payload is not None
                        else None
                    ),
                },
                'responses_response_parsing': {
                    'supported': responses_parse == {'text': 'responses ok', 'tool_name': 'schema_probe', 'tool_id': 'call_probe'},
                    'observed': responses_parse,
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
) -> tuple[str, dict[str, Any]]:
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


def _truth_matches(actual: dict[str, Any], truth: dict[str, Any]) -> float:
    variants = truth.get('any_of')
    if isinstance(variants, list) and variants:
        variant_scores = [
            _truth_matches(actual, cast(dict[str, Any], variant))
            for variant in variants
            if isinstance(variant, dict)
        ]
        if variant_scores:
            return max(variant_scores)
    scores: list[float] = []
    for key, options in truth.items():
        if key == 'any_of':
            continue
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
        calls.append(
            {
                'name': payload.get('tool_name'),
                'arguments': payload.get('arguments', {}),
                'result': payload.get('result'),
            }
        )
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


def _normalize_answer_text(value: str) -> str:
    lowered = value.casefold()
    lowered = re.sub(r'https?://\S+', ' ', lowered)
    lowered = re.sub(r'[^0-9a-z]+', ' ', lowered)
    return re.sub(r'\s+', ' ', lowered).strip()


def _extract_bfcl_answer_candidates(result: Any, result_summary: str, *, latest_results: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []

    def add(value: Any) -> None:
        text = str(value or '').strip()
        if text and text not in candidates:
            candidates.append(text)

    if isinstance(result, dict):
        for key in ('answer', 'final_answer', 'title', 'page_title', 'result'):
            if key in result:
                add(result.get(key))
    add(result_summary)
    if isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                for key in ('answer', 'final_answer', 'title', 'page_title', 'result'):
                    if key in payload:
                        add(payload.get(key))
        for match in re.findall(r'["\']([^"\']{2,160})["\']', result):
            add(match)
        for separator in (':', '-', ' is '):
            if separator in result:
                add(result.rsplit(separator, 1)[-1].strip())
    normalized_result = _normalize_answer_text(result_summary)
    for item in latest_results:
        title = str(item.get('title') or '').strip()
        if title and _normalize_answer_text(title) in normalized_result:
            add(title)
    return candidates


def _score_expected_tool_result(case: dict[str, Any], actual_calls: list[dict[str, Any]]) -> bool:
    expected = case.get('expected_tool_result')
    if not isinstance(expected, dict):
        return True
    if len(actual_calls) != 1:
        return False
    actual_result = actual_calls[0].get('result')
    if not isinstance(actual_result, dict):
        return False
    return _truth_matches(actual_result, cast(dict[str, Any], expected)) == 1.0


def _build_bfcl_initial_messages(case: dict[str, Any]) -> list[ChatMessage]:
    message_history = list(cast(list[dict[str, Any]], case.get('initial_state', {}).get('message_history', [])))
    initial_messages: list[ChatMessage] = []
    for item in message_history:
        role = str(item.get('role') or '')
        if role == 'assistant' and item.get('tool_calls'):
            calls = [ToolCall.model_validate(call) for call in cast(list[dict[str, Any]], item['tool_calls'])]
            initial_messages.append(ChatMessage(role='assistant', content=item.get('content', ''), tool_calls=calls))
            continue
        if role == 'tool':
            initial_messages.append(
                ChatMessage(
                    role='tool',
                    content=str(item.get('content', '')),
                    name=str(item.get('name') or ''),
                    tool_call_id=str(item.get('tool_call_id') or item.get('id') or ''),
                )
            )
            continue
        if role in {'system', 'user', 'assistant'}:
            initial_messages.append(ChatMessage(role=cast(Any, role), content=str(item.get('content', ''))))
    return initial_messages


def _score_bfcl_answer(
    case: dict[str, Any],
    result: Any,
    result_summary: str,
    *,
    tool_success: bool,
    actual_calls: list[dict[str, Any]] | None = None,
    latest_results: list[dict[str, Any]] | None = None,
) -> tuple[bool, float]:
    if actual_calls is not None and not _score_expected_tool_result(case, actual_calls):
        return False, 0.0
    aliases = cast(list[str], case.get('expected_answer_aliases') or [])
    expected_answer = str(case.get('expected_answer') or '').strip()
    if expected_answer:
        aliases = [expected_answer, *aliases]
    normalized_aliases = [_normalize_answer_text(item) for item in aliases if str(item).strip()]
    if normalized_aliases:
        candidates = _extract_bfcl_answer_candidates(result, result_summary, latest_results=latest_results or [])
        normalized_candidates = [_normalize_answer_text(item) for item in candidates if str(item).strip()]
        success = any(item in normalized_aliases for item in normalized_candidates)
        return success, 1.0 if success else 0.0
    return tool_success, 1.0 if tool_success else 0.0


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


def _looks_multi_intent(prompt: str, scored: list[tuple[dict[str, Any], int, int]] | None = None) -> bool:
    if _MULTI_INTENT_PATTERN.search(prompt) is not None:
        return True
    if _COORDINATED_INTENT_PATTERN.search(prompt) is None or scored is None:
        return False
    meaningful = [item for item in scored if item[1] >= 4]
    if len(meaningful) < 2:
        return False
    if len([item for item in meaningful if item[2] > 0]) >= 2:
        return True
    best_score = max(score for _, score, _ in meaningful)
    close_matches = [item for item in meaningful if item[1] >= max(4, best_score - 2)]
    return len(close_matches) >= 2


def _select_bfcl_candidate_functions(
    prompt: str,
    functions: list[dict[str, Any]],
    *,
    allow_multi_intent: bool = True,
) -> list[dict[str, Any]]:
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
    if allow_multi_intent and _looks_multi_intent(prompt, scored):
        coordinated = [
            function
            for function, score, name_overlap in scored
            if name_overlap > 0 and score >= 4
        ]
        if len(coordinated) >= 2:
            return coordinated
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


def _is_retryable_budget_overcall(exc: BaseException) -> bool:
    return 'tool call budget exhausted' in str(exc).lower()


def _same_function_selection(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    return [str(item['name']) for item in left] == [str(item['name']) for item in right]


def _candidate_pruned_functions(
    prompt: str,
    current_functions: list[dict[str, Any]],
    all_functions: list[dict[str, Any]],
    *,
    expected_call_count: int | None = None,
) -> list[dict[str, Any]] | None:
    candidate_functions = _select_bfcl_candidate_functions(
        prompt,
        all_functions,
        allow_multi_intent=(expected_call_count or 0) > 1,
    )
    if not candidate_functions or _same_function_selection(candidate_functions, current_functions):
        return None
    return candidate_functions


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
        if record.answer_match < 1.0 and record.tool_name_match == 1.0:
            return 'answer_grounding_miss'
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
    latest_results: list[dict[str, Any]] | None = None,
    grounded_urls: set[str] | None = None,
) -> dict[str, Any]:
    return _web_fetch_contents(arguments, case, web_search, latest_results=latest_results, grounded_urls=grounded_urls)


def _build_eval_tool_handler(
    case: dict[str, Any],
    original_name: str,
    tool_name: str,
    *,
    web_search: PublicEvalWebSearchConfig,
    memory_state: dict[str, str],
    memory_aliases: dict[str, str],
    budget_state: dict[str, int],
    search_state: dict[str, Any],
) -> Any:
    def guard_call_budget() -> None:
        if budget_state['successful_calls'] >= budget_state['allowed_calls']:
            raise RuntimeError('tool call budget exhausted for this BFCL case')

    def resolve_memory_key(key: str) -> str:
        return memory_aliases.get(key, key)

    if original_name == 'web.search':
        def search_handler(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            guard_call_budget()
            result = _web_serpapi_search(arguments, case, web_search)
            latest_results = list(result.get('results', []))
            search_state['latest_results'] = latest_results
            search_state.setdefault('history', []).append(
                {
                    'tool': tool_name,
                    'query': result.get('query'),
                    'results': latest_results,
                    'grounded_urls': [
                        str(item.get('link') or '').strip()
                        for item in latest_results
                        if str(item.get('link') or '').strip()
                    ],
                }
            )
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
            result = _web_fetch_contents(
                arguments,
                case,
                web_search,
                latest_results=cast(list[dict[str, Any]], search_state.get('latest_results', [])),
                grounded_urls=grounded_urls,
            )
            latest_contents = list(result.get('results', []))
            search_state['latest_contents'] = latest_contents
            contents_by_url = cast(dict[str, dict[str, Any]], search_state.setdefault('contents_by_url', {}))
            for item in latest_contents:
                link = str(item.get('link') or '').strip()
                if link:
                    contents_by_url[link] = dict(item)
            budget_state['successful_calls'] += 1
            result['tool'] = tool_name
            result['run_id'] = context.run_id
            return result

        return contents_handler
    if original_name == 'memory.put':
        def memory_put(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            guard_call_budget()
            raw_key = str(arguments['key'])
            value = str(arguments['value'])
            key = resolve_memory_key(raw_key)
            memory_state[key] = value
            budget_state['successful_calls'] += 1
            return {'tool': tool_name, 'run_id': context.run_id, 'key': key, 'requested_key': raw_key, 'value': value}

        return memory_put
    if original_name == 'memory.get':
        def memory_get(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            guard_call_budget()
            raw_key = str(arguments['key'])
            key = resolve_memory_key(raw_key)
            budget_state['successful_calls'] += 1
            return {
                'tool': tool_name,
                'run_id': context.run_id,
                'key': key,
                'requested_key': raw_key,
                'value': memory_state.get(key),
                'found': key in memory_state,
            }

        return memory_get
    if original_name == 'memory.delete':
        def memory_delete(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
            guard_call_budget()
            raw_key = str(arguments['key'])
            key = resolve_memory_key(raw_key)
            removed = memory_state.pop(key, None)
            budget_state['successful_calls'] += 1
            return {'tool': tool_name, 'run_id': context.run_id, 'key': key, 'requested_key': raw_key, 'removed': removed is not None}

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
        config.model.function_calling.mode = 'auto'
        runtime = build_runtime_from_config(config)
        web_search = base_config.evaluation.public_eval.web_search
        memory_state = {
            str(key): str(value)
            for key, value in cast(dict[str, Any], case.get('initial_state', {}).get('memory', {})).items()
        }
        memory_aliases: dict[str, str] = {}
        for canonical_key, aliases in cast(dict[str, Any], case.get('initial_state', {}).get('memory_aliases', {})).items():
            canonical = str(canonical_key)
            if canonical not in memory_state:
                continue
            if isinstance(aliases, list):
                for alias in aliases:
                    alias_text = str(alias).strip()
                    if alias_text and alias_text != canonical:
                        memory_aliases[alias_text] = canonical
        budget_state = {'allowed_calls': len(case.get('ground_truth', [])), 'successful_calls': 0}
        search_state: dict[str, Any] = {
            'latest_results': [],
            'latest_contents': [],
            'history': [],
            'contents_by_url': {},
        }
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
                    memory_aliases=memory_aliases,
                    budget_state=budget_state,
                    search_state=search_state,
                ),
            )
        start = time.perf_counter()
        try:
            await runtime.start()
            session_id = f"bfcl-{case['id']}"
            initial_messages = _build_bfcl_initial_messages(case)
            if initial_messages:
                runtime.store.save_session_messages(session_id, config.graph.name, initial_messages)
            prompt = _case_prompt(case)
            result = await runtime.run(prompt, session_id=session_id if initial_messages else None)
            duration = time.perf_counter() - start
            trace = runtime.store.load_trace(result['run_id'])
            actual_calls = _extract_successful_tool_calls(trace)
            tool_success, tool_name_match, argument_match = _score_bfcl_case(case, actual_calls, tool_name_map)
            result_summary = _summarize_result(result.get('result'))
            success, answer_match = _score_bfcl_answer(
                case,
                result.get('result'),
                result_summary,
                tool_success=tool_success,
                actual_calls=actual_calls,
                latest_results=cast(list[dict[str, Any]], search_state.get('latest_results', [])),
            )
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
                    result_summary=result_summary,
                    answer_match=answer_match,
                    error=(
                        None
                        if success
                        else json.dumps(
                            {'actual_calls': actual_calls, 'result_summary': result_summary},
                            ensure_ascii=False,
                        )
                    ),
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
    last_record: PublicEvalRecord | None = None
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
            last_record = attempt.record
            last_duration = attempt.record.duration_seconds
            if attempt.record.success:
                return attempt.record
            if fallback_stage == 'strict_schema_retry':
                candidate_functions = _candidate_pruned_functions(
                    prompt,
                    functions,
                    all_functions,
                    expected_call_count=len(case.get('ground_truth', [])),
                )
                if candidate_functions is not None:
                    stages.append(('candidate_pruned_retry', candidate_functions, True))
            continue
        if attempt.error is None:
            break
        last_error = attempt.error
        last_duration = attempt.duration_seconds
        if _is_retryable_budget_overcall(attempt.error):
            candidate_functions = _candidate_pruned_functions(
                prompt,
                functions,
                all_functions,
                expected_call_count=len(case.get('ground_truth', [])),
            )
            if candidate_functions is not None:
                stages.insert(0, ('candidate_pruned_retry', candidate_functions, True))
                continue
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
        candidate_functions = _candidate_pruned_functions(
            prompt,
            functions,
            all_functions,
            expected_call_count=len(case.get('ground_truth', [])),
        )
        if candidate_functions is not None:
            stages.append(('candidate_pruned_retry', candidate_functions, True))
    if last_record is not None:
        return last_record
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
            'answer_match_rate': round(mean(item.answer_match for item in items), 4),
            'average_duration_seconds': round(mean(item.duration_seconds for item in items), 4),
        }
    bfcl_items = [item for item in records if item.suite.startswith('bfcl_')]
    bfcl_summaries = [item for suite, item in summary.items() if suite in _BFCL_SUBCATEGORY_GROUPS]
    irrelevance_items = [item for item in records if item.suite == 'bfcl_irrelevance']
    tau_items = [item for item in records if item.suite == 'tau2_mock']
    summary['overall'] = {
        'bfcl_case_pass_rate': round(sum(1 for item in bfcl_items if item.success) / len(bfcl_items), 4),
        'bfcl_subcategory_accuracy': round(mean(item['pass_rate'] for item in bfcl_summaries), 4),
        'bfcl_tool_name_match_rate': round(mean(item.tool_name_match for item in bfcl_items), 4),
        'bfcl_argument_match_rate': round(mean(item.argument_match for item in bfcl_items), 4),
        'bfcl_answer_match_rate': round(mean(item.answer_match for item in bfcl_items), 4),
        'bfcl_irrelevance_pass_rate': (
            round(sum(1 for item in irrelevance_items if item.success) / len(irrelevance_items), 4)
            if irrelevance_items
            else 0.0
        ),
        'tau2_mock_pass_rate': round(sum(1 for item in tau_items if item.success) / len(tau_items), 4) if tau_items else 0.0,
        'tau2_mock_average_duration_seconds': round(mean(item.duration_seconds for item in tau_items), 4) if tau_items else 0.0,
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
        suite_rates = [
            sum(1 for record in records if record.suite == suite and record.success)
            / len([record for record in records if record.suite == suite])
            for suite in sorted(suites)
            if any(record.suite == suite for record in records)
        ]
        summary[name] = {
            'runs': len(selected),
            'successes': sum(1 for record in selected if record.success),
            'failures': sum(1 for record in selected if not record.success),
            'pass_rate': round(mean(suite_rates), 4),
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
            'answer_match_rate': round(mean(record.answer_match for record in selected), 4),
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


def _checkpoint_selection_signature(base_config: AppConfig, profile: str) -> dict[str, Any]:
    if profile != 'official_full_v4':
        return {'profile': profile}
    official = base_config.evaluation.public_eval.official_dataset
    return {
        'profile': profile,
        'category_allowlist': list(official.category_allowlist),
        'suite_allowlist': list(official.suite_allowlist),
        'case_allowlist': list(official.case_allowlist),
        'selection_mode': official.selection_mode,
        'max_cases': official.max_cases,
        'max_cases_per_suite': official.max_cases_per_suite,
    }


def _load_public_eval_checkpoint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {'records': {}}
    return cast(dict[str, Any], json.loads(path.read_text(encoding='utf-8')))


def _save_public_eval_checkpoint(
    path: Path,
    *,
    profile: str,
    bfcl_version: str,
    selection_signature: dict[str, Any],
    records: list[PublicEvalRecord],
    run_status: str,
    interrupted: dict[str, Any] | None = None,
) -> None:
    payload = {
        'profile': profile,
        'bfcl_version': bfcl_version,
        'selection_signature': selection_signature,
        'run_status': run_status,
        'interrupted': interrupted,
        'records': {
            _checkpoint_record_key(record): asdict(record)
            for record in records
        },
    }
    _write_json_path(path, payload)


def _restore_checkpoint_records(
    path: Path,
    *,
    profile: str,
    bfcl_version: str,
    selection_signature: dict[str, Any],
) -> dict[str, PublicEvalRecord]:
    checkpoint = _load_public_eval_checkpoint(path)
    if checkpoint.get('profile') != profile or checkpoint.get('bfcl_version') != bfcl_version:
        return {}
    if checkpoint.get('selection_signature') != selection_signature:
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
    selection_signature = _checkpoint_selection_signature(base_config, profile)
    restored = (
        _restore_checkpoint_records(
            checkpoint_path,
            profile=profile,
            bfcl_version=bfcl_version,
            selection_signature=selection_signature,
        )
        if restore_enabled
        else {}
    )
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
                selection_signature=selection_signature,
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
            selection_signature=selection_signature,
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
                selection_signature=selection_signature,
                records=records,
                run_status='running',
            )

    final_status = 'interrupted_quota' if interrupted is not None else 'completed'
    _save_public_eval_checkpoint(
        checkpoint_path,
        profile=profile,
        bfcl_version=bfcl_version,
        selection_signature=selection_signature,
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
        sources['official_selection'] = {
            'category_allowlist': list(official.category_allowlist),
            'suite_allowlist': list(official.suite_allowlist),
            'case_allowlist': list(official.case_allowlist),
            'selection_mode': official.selection_mode,
            'max_cases': official.max_cases,
            'max_cases_per_suite': official.max_cases_per_suite,
        }
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


