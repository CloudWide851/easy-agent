from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from agent_config.app import PublicEvalWebSearchConfig

_SEARCH_PREFIX_PATTERN = re.compile(
    r'^(search(\s+the)?\s+web(\s+for)?|web\s+search(\s+for)?|look\s+up|find)\s*[:,-]?\s*',
    re.IGNORECASE,
)
_TRAILING_TITLE_QUESTION_PATTERN = re.compile(
    r'[\s,;:-]*(what\s+is\s+the\s+(exact\s+)?page\s+title|what\s+is\s+its\s+title|return\s+the\s+exact\s+page\s+title)\??\s*$',
    re.IGNORECASE,
)
_QUOTE_STRIP = "\"'` "
_MAX_QUERY_LENGTH = 180
_SUPPORTED_CONTENT_MODES = {'truncate', 'raw', 'markdown'}


class WebSearchQuotaExceeded(RuntimeError):
    def __init__(self, wait_seconds: float, *, scope: str) -> None:
        rounded = max(0.0, round(wait_seconds, 2))
        super().__init__(f'web search quota exceeded for {scope}; retry after {rounded:.2f}s')
        self.wait_seconds = rounded
        self.scope = scope


def _case_prompt(case: dict[str, Any]) -> str:
    messages = cast(list[dict[str, Any]], case.get('messages', []))
    if messages:
        return str(messages[0].get('content', ''))
    return str(case.get('prompt', ''))


def _shape_web_search_query(raw_query: str, case: dict[str, Any]) -> str:
    prompt = _case_prompt(case).strip()
    query = raw_query.strip() or prompt
    query = _SEARCH_PREFIX_PATTERN.sub('', query, count=1)
    query = _TRAILING_TITLE_QUESTION_PATTERN.sub('', query)
    query = re.sub(r'\s+', ' ', query).strip(_QUOTE_STRIP)
    query = query.rstrip(' .,:;!?')
    if 'official' in prompt.casefold() and 'official' not in query.casefold():
        query = f'official {query}'.strip()
    if len(query) <= _MAX_QUERY_LENGTH:
        return query
    trimmed = query[:_MAX_QUERY_LENGTH].rsplit(' ', 1)[0].strip()
    return trimmed or query[:_MAX_QUERY_LENGTH].strip()


def _normalize_num_results(value: Any, *, default: int = 5, maximum: int = 10) -> int:
    try:
        number = int(value or default)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


def _load_web_search_usage(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding='utf-8'))
    entries = payload.get('requests', [])
    if isinstance(entries, list):
        return [cast(dict[str, Any], item) for item in entries if isinstance(item, dict)]
    return []


def _save_web_search_usage(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'requests': entries}, ensure_ascii=False, indent=2), encoding='utf-8')


def _prune_web_search_usage(entries: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    return [item for item in entries if now - float(item.get('timestamp', 0.0)) < 86400.0]


def _window_wait_seconds(entries: list[dict[str, Any]], now: float, *, seconds: float, limit: int) -> float:
    if limit <= 0:
        return 0.0
    window_entries = sorted(
        float(item.get('timestamp', 0.0))
        for item in entries
        if now - float(item.get('timestamp', 0.0)) < seconds
    )
    if len(window_entries) < limit:
        return 0.0
    oldest_relevant = window_entries[-limit]
    return max(0.0, seconds - (now - oldest_relevant))


def _record_web_search_usage(config: PublicEvalWebSearchConfig, *, kind: str, now: float | None = None) -> None:
    moment = time.time() if now is None else now
    usage_path = Path(config.usage_path)
    entries = _prune_web_search_usage(_load_web_search_usage(usage_path), moment)
    hourly_wait = _window_wait_seconds(entries, moment, seconds=3600.0, limit=config.hourly_limit)
    daily_wait = _window_wait_seconds(entries, moment, seconds=86400.0, limit=config.daily_limit)
    wait_seconds = max(hourly_wait, daily_wait)
    if wait_seconds > 0:
        if config.quota_policy == 'replay':
            raise WebSearchQuotaExceeded(wait_seconds, scope='quota_replay_fallback')
        if config.quota_policy in {'resume_later', 'fail'}:
            raise WebSearchQuotaExceeded(wait_seconds, scope='quota_resume')
    entries.append({'timestamp': moment, 'kind': kind})
    _save_web_search_usage(usage_path, entries)


def _should_use_replay_results(case: dict[str, Any], web_search: PublicEvalWebSearchConfig) -> bool:
    return web_search.provider == 'replay_only' or bool(case.get('replay_results'))


def _site_name(url: str, fallback: str = 'unknown') -> str:
    parsed = urlparse(url)
    return parsed.netloc or fallback


def _replay_web_search(case: dict[str, Any], *, query: str, num_results: int, backend: str) -> dict[str, Any]:
    replay_results = cast(list[dict[str, Any]], case.get('replay_results', []))
    if not replay_results:
        raise RuntimeError('missing replay_results for BFCL web search evaluation')
    normalized = _normalize_serpapi_search_results({'organic_results': replay_results}, num_results=num_results)
    return {'query': query, 'results': normalized, 'backend': backend, 'source': 'replay'}


def _normalize_serpapi_search_results(payload: dict[str, Any], *, num_results: int) -> list[dict[str, Any]]:
    raw_results = payload.get('organic_results', [])
    if not isinstance(raw_results, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_results[:num_results], start=1):
        if not isinstance(item, dict):
            continue
        link = str(item.get('link') or item.get('url') or '').strip()
        normalized.append(
            {
                'position': int(item.get('position') or index),
                'title': str(item.get('title') or item.get('name') or '').strip(),
                'link': link,
                'source': str(item.get('source') or item.get('displayed_link') or _site_name(link)),
                'snippet': str(item.get('text') or item.get('snippet') or '').strip(),
            }
        )
    return normalized


def _normalize_web_contents_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_results = payload.get('results', [])
    if not isinstance(raw_results, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        link = str(item.get('url') or item.get('link') or '').strip()
        normalized.append(
            {
                'title': str(item.get('title') or link).strip(),
                'link': link,
                'text': str(item.get('text') or item.get('snippet') or '').strip(),
            }
        )
    return normalized


def _normalize_contents_mode(value: Any) -> str:
    mode = str(value or 'truncate').strip().casefold()
    return mode if mode in _SUPPORTED_CONTENT_MODES else 'truncate'


def _normalize_title_key(value: str) -> str:
    lowered = value.casefold()
    lowered = re.sub(r'[^0-9a-z]+', ' ', lowered)
    return re.sub(r'\s+', ' ', lowered).strip()


def _result_title_for_url(
    url: str,
    *,
    latest_results: list[dict[str, Any]] | None = None,
    search_history: list[dict[str, Any]] | None = None,
) -> str:
    candidates: list[dict[str, Any]] = []
    if latest_results:
        candidates.extend(latest_results)
    if search_history:
        for entry in search_history:
            results = entry.get('results', [])
            if isinstance(results, list):
                candidates.extend(item for item in results if isinstance(item, dict))
    cleaned = str(url).strip()
    for item in candidates:
        if str(item.get('link') or '').strip() == cleaned:
            return str(item.get('title') or '').strip()
    return ''


def _grounded_retry_urls(
    url: str,
    *,
    latest_results: list[dict[str, Any]] | None = None,
    search_history: list[dict[str, Any]] | None = None,
    grounded_urls: set[str] | None = None,
) -> list[str]:
    if grounded_urls is None:
        return []
    title_key = _normalize_title_key(
        _result_title_for_url(url, latest_results=latest_results, search_history=search_history)
    )
    if not title_key:
        return []
    candidates: list[dict[str, Any]] = []
    if latest_results:
        candidates.extend(latest_results)
    if search_history:
        for entry in search_history:
            results = entry.get('results', [])
            if isinstance(results, list):
                candidates.extend(item for item in results if isinstance(item, dict))
    retry_urls: list[str] = []
    for item in candidates:
        candidate_url = str(item.get('link') or '').strip()
        if not candidate_url or candidate_url == url or candidate_url not in grounded_urls:
            continue
        candidate_title = _normalize_title_key(str(item.get('title') or ''))
        if candidate_title != title_key or candidate_url in retry_urls:
            continue
        retry_urls.append(candidate_url)
    return retry_urls


def _strip_html_text(value: str) -> str:
    collapsed = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', value, flags=re.IGNORECASE | re.DOTALL)
    collapsed = re.sub(r'<[^>]+>', ' ', collapsed)
    collapsed = re.sub(r'\s+', ' ', collapsed)
    return collapsed.strip()


def _html_to_markdown_like_text(value: str) -> str:
    text = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', value, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<\s*br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*/?\s*p\s*>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*/?\s*div\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*h([1-6])\s*>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*/\s*h[1-6]\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*li\s*>', '\n- ', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*/\s*li\s*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


def _render_contents_text(body: str, content_type: str, *, mode: str) -> str:
    lowered_body = body.lower()
    looks_html = 'html' in content_type or '<html' in lowered_body or '<body' in lowered_body
    if mode == 'raw':
        return body.strip()
    if mode == 'markdown':
        return _html_to_markdown_like_text(body) if looks_html else body.strip()
    return _strip_html_text(body) if looks_html else body.strip()


def _serpapi_query_params(arguments: dict[str, Any], case: dict[str, Any], web_search: PublicEvalWebSearchConfig) -> dict[str, Any]:
    query = _shape_web_search_query(str(arguments.get('query') or ''), case)
    num_results = _normalize_num_results(arguments.get('num_results'))
    return {
        'engine': web_search.engine,
        'q': query,
        'num': num_results,
        'google_domain': web_search.google_domain,
        'hl': web_search.hl,
        'gl': web_search.gl,
    }


def _is_retryable_search_unavailable(response: httpx.Response) -> bool:
    if response.status_code not in {429, 503}:
        return False
    lowered = response.text.lower()
    return (
        'quota' in lowered
        or 'rate limit' in lowered
        or 'service unavailable' in lowered
        or 'temporarily unavailable' in lowered
    )


def _serpapi_search(arguments: dict[str, Any], case: dict[str, Any], web_search: PublicEvalWebSearchConfig) -> dict[str, Any]:
    params = _serpapi_query_params(arguments, case, web_search)
    query = str(params['q'])
    num_results = int(params['num'])
    api_key = os.environ.get(web_search.api_key_env, '').strip()
    if not api_key:
        if _should_use_replay_results(case, web_search):
            return _replay_web_search(case, query=query, num_results=num_results, backend='replay')
        raise RuntimeError(f'missing {web_search.api_key_env} for BFCL web search evaluation')
    try:
        _record_web_search_usage(web_search, kind='search')
    except WebSearchQuotaExceeded:
        if web_search.quota_policy == 'replay' and case.get('replay_results'):
            return _replay_web_search(case, query=query, num_results=num_results, backend='quota_replay')
        raise
    response = httpx.get(
        web_search.endpoint_url,
        params={**params, 'api_key': api_key},
        timeout=web_search.timeout_seconds,
    )
    if _is_retryable_search_unavailable(response) and case.get('replay_results'):
        return _replay_web_search(case, query=query, num_results=num_results, backend='service_unavailable_replay')
    response.raise_for_status()
    payload = cast(dict[str, Any], response.json())
    return {
        'query': query,
        'results': _normalize_serpapi_search_results(payload, num_results=num_results),
        'backend': 'serpapi',
        'source': 'network',
    }


def _resolve_content_urls(
    arguments: dict[str, Any],
    case: dict[str, Any],
    *,
    latest_results: list[dict[str, Any]] | None = None,
    grounded_urls: set[str] | None = None,
) -> list[str]:
    latest = latest_results or []

    def accept(url: str) -> str | None:
        cleaned = str(url).strip()
        if not cleaned:
            return None
        if grounded_urls is not None and cleaned not in grounded_urls:
            return None
        return cleaned

    def resolve_result_id(item: Any) -> str | None:
        if not latest:
            return None
        if isinstance(item, int):
            for entry in latest:
                if int(entry.get('position') or 0) == item:
                    return accept(str(entry.get('link') or ''))
            if 0 <= item < len(latest):
                return accept(str(latest[item].get('link') or ''))
            if 1 <= item <= len(latest):
                return accept(str(latest[item - 1].get('link') or ''))
            return None
        text = str(item).strip()
        if not text:
            return None
        if text.startswith('http'):
            return accept(text)
        if text.isdigit():
            return resolve_result_id(int(text))
        title_key = _normalize_title_key(text)
        for entry in latest:
            if _normalize_title_key(str(entry.get('title') or '')) == title_key:
                return accept(str(entry.get('link') or ''))
        return None

    urls = arguments.get('urls') or arguments.get('links') or []
    if isinstance(urls, list):
        direct_urls = [accepted for item in urls if (accepted := accept(str(item)))]
        if grounded_urls is not None:
            direct_urls = [item for item in direct_urls if item in grounded_urls]
        if direct_urls:
            return direct_urls
        if urls and grounded_urls:
            raise RuntimeError('web.contents requires grounded urls from the latest web.search results')
    result_ids = arguments.get('ids') or arguments.get('result_ids') or []
    replay_results = cast(list[dict[str, Any]], case.get('replay_results', []))
    resolved: list[str] = []
    if isinstance(result_ids, list):
        for item in result_ids:
            resolved_item = resolve_result_id(item)
            if resolved_item:
                resolved.append(resolved_item)
                continue
            if isinstance(item, int) and 0 <= item < len(replay_results):
                link = str(replay_results[item].get('link') or '').strip()
                if link and (grounded_urls is None or link in grounded_urls):
                    resolved.append(link)
                continue
            text = str(item).strip()
            if text.startswith('http') and (grounded_urls is None or text in grounded_urls):
                resolved.append(text)
    return resolved


def _fetch_web_contents(
    arguments: dict[str, Any],
    case: dict[str, Any],
    web_search: PublicEvalWebSearchConfig,
    *,
    latest_results: list[dict[str, Any]] | None = None,
    grounded_urls: set[str] | None = None,
    contents_by_url: dict[str, dict[str, Any]] | None = None,
    search_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    replay_contents = cast(list[dict[str, Any]], case.get('replay_contents', []))
    mode = _normalize_contents_mode(arguments.get('mode'))
    urls = _resolve_content_urls(arguments, case, latest_results=latest_results, grounded_urls=grounded_urls)
    if not urls:
        if replay_contents:
            return {
                'results': replay_contents,
                'backend': 'replay',
                'source': 'replay',
                'mode': mode,
                'diagnostics': {
                    'content_sources': {'cache': 0, 'network': 0, 'replay': len(replay_contents)},
                    'grounded_retry_count': 0,
                    'requested_urls': [],
                },
            }
        raise RuntimeError('web.contents requires urls/links grounded in search results or replay_contents')
    diagnostics: dict[str, Any] = {
        'content_sources': {'cache': 0, 'network': 0, 'replay': 0},
        'grounded_retry_count': 0,
        'requested_urls': list(urls),
        'attempted_urls': [],
    }
    cached_results: list[dict[str, Any]] = []
    pending_urls: list[str] = []
    cache = contents_by_url or {}
    for url in urls:
        cached = cache.get(url)
        if cached is not None:
            cached_results.append(dict(cached))
            diagnostics['content_sources']['cache'] += 1
            continue
        pending_urls.append(url)
    try:
        _record_web_search_usage(web_search, kind='contents')
    except WebSearchQuotaExceeded:
        if web_search.quota_policy == 'replay' and replay_contents:
            diagnostics['content_sources']['replay'] = len(replay_contents)
            return {
                'results': replay_contents,
                'backend': 'quota_replay',
                'source': 'replay',
                'mode': mode,
                'diagnostics': diagnostics,
            }
        raise
    results = list(cached_results)
    network_results = 0
    for url in pending_urls:
        candidate_urls = [url, *_grounded_retry_urls(url, latest_results=latest_results, search_history=search_history, grounded_urls=grounded_urls)]
        for index, candidate_url in enumerate(candidate_urls):
            diagnostics['attempted_urls'].append(candidate_url)
            cached = cache.get(candidate_url)
            if cached is not None:
                results.append(dict(cached))
                diagnostics['content_sources']['cache'] += 1
                if index > 0:
                    diagnostics['grounded_retry_count'] += 1
                break
            try:
                response = httpx.get(candidate_url, timeout=web_search.timeout_seconds, follow_redirects=True)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            content_type = response.headers.get('content-type', '').lower()
            body = response.text
            text = _render_contents_text(body, content_type, mode=mode)
            title = _result_title_for_url(candidate_url, latest_results=latest_results, search_history=search_history) or candidate_url
            results.append({'title': title, 'link': candidate_url, 'text': text[:4000]})
            diagnostics['content_sources']['network'] += 1
            if index > 0:
                diagnostics['grounded_retry_count'] += 1
            network_results += 1
            break
    if len(results) == len(cached_results) and replay_contents:
        diagnostics['content_sources']['replay'] = len(replay_contents)
        return {
            'results': replay_contents,
            'backend': 'service_unavailable_replay',
            'source': 'replay',
            'mode': mode,
            'diagnostics': diagnostics,
        }
    backend = 'cache'
    source = 'cache'
    if diagnostics['content_sources']['network'] and diagnostics['content_sources']['cache']:
        backend = 'cache_plus_network'
        source = 'mixed'
    elif diagnostics['content_sources']['network']:
        backend = 'http_fetch'
        source = 'network'
    return {
        'results': _normalize_web_contents_results({'results': results}),
        'backend': backend,
        'source': source,
        'mode': mode,
        'diagnostics': diagnostics,
    }
