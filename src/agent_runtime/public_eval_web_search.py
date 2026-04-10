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
_QUOTE_STRIP = "\"'` "
_MAX_QUERY_LENGTH = 180


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
    query = raw_query.strip() or _case_prompt(case).strip()
    query = _SEARCH_PREFIX_PATTERN.sub('', query, count=1)
    query = re.sub(r'\s+', ' ', query).strip(_QUOTE_STRIP)
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


def _strip_html_text(value: str) -> str:
    collapsed = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', value, flags=re.IGNORECASE | re.DOTALL)
    collapsed = re.sub(r'<[^>]+>', ' ', collapsed)
    collapsed = re.sub(r'\s+', ' ', collapsed)
    return collapsed.strip()


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
    grounded_urls: set[str] | None = None,
) -> list[str]:
    urls = arguments.get('urls') or arguments.get('links') or []
    if isinstance(urls, list):
        direct_urls = [str(item).strip() for item in urls if str(item).strip()]
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
    grounded_urls: set[str] | None = None,
) -> dict[str, Any]:
    replay_contents = cast(list[dict[str, Any]], case.get('replay_contents', []))
    urls = _resolve_content_urls(arguments, case, grounded_urls=grounded_urls)
    if not urls:
        if replay_contents:
            return {'results': replay_contents, 'backend': 'replay', 'source': 'replay'}
        raise RuntimeError('web.contents requires urls/links grounded in search results or replay_contents')
    try:
        _record_web_search_usage(web_search, kind='contents')
    except WebSearchQuotaExceeded:
        if web_search.quota_policy == 'replay' and replay_contents:
            return {'results': replay_contents, 'backend': 'quota_replay', 'source': 'replay'}
        raise
    results: list[dict[str, Any]] = []
    for url in urls:
        try:
            response = httpx.get(url, timeout=web_search.timeout_seconds, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError:
            continue
        content_type = response.headers.get('content-type', '').lower()
        body = response.text
        text = _strip_html_text(body) if 'html' in content_type or '<html' in body.lower() else body.strip()
        results.append({'title': url, 'link': url, 'text': text[:4000]})
    if not results and replay_contents:
        return {'results': replay_contents, 'backend': 'service_unavailable_replay', 'source': 'replay'}
    return {'results': _normalize_web_contents_results({'results': results}), 'backend': 'http_fetch', 'source': 'network'}
