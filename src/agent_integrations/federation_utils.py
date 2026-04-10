from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from agent_integrations.federation_security import decode_page_token, encode_page_token


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def site_origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url.rstrip('/')
    return f'{parsed.scheme}://{parsed.netloc}'


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}" + (path if path.startswith('/') else f'/{path}')


def safe_json(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    return cast(dict[str, Any], response.json())


def page_size(value: int | None, *, default: int = 50, maximum: int = 200) -> int:
    if value is None:
        return default
    return max(1, min(int(value), maximum))


def pagination_params(page_token: str | None = None, page_size_value: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if page_token:
        params['pageToken'] = page_token
    if page_size_value is not None:
        params['pageSize'] = page_size_value
    return params


def paginate_tasks_payload(tasks: list[dict[str, Any]], page_token: str | None, page_size_value: int | None) -> dict[str, Any]:
    ordered = sorted(tasks, key=lambda item: (str(item.get('created_at', '')), str(item.get('task_id', ''))))
    start_index = 0
    if page_token:
        cursor = decode_page_token(page_token, 'tasks')
        after = (str(cursor.get('created_at', '')), str(cursor.get('task_id', '')))
        for index, item in enumerate(ordered):
            current = (str(item.get('created_at', '')), str(item.get('task_id', '')))
            if current > after:
                start_index = index
                break
        else:
            start_index = len(ordered)
    size = page_size(page_size_value)
    items = ordered[start_index : start_index + size]
    next_token: str | None = None
    if start_index + size < len(ordered) and items:
        tail = items[-1]
        next_token = encode_page_token('tasks', {'created_at': tail.get('created_at'), 'task_id': tail.get('task_id')})
    return {'tasks': items, 'nextPageToken': next_token}


def paginate_events_payload(events: list[dict[str, Any]], page_token: str | None, page_size_value: int | None) -> dict[str, Any]:
    ordered = sorted(events, key=lambda item: int(item.get('sequence', 0)))
    start_index = 0
    if page_token:
        cursor = decode_page_token(page_token, 'task-events')
        after_sequence = int(cursor.get('sequence', 0))
        for index, item in enumerate(ordered):
            if int(item.get('sequence', 0)) > after_sequence:
                start_index = index
                break
        else:
            start_index = len(ordered)
    size = page_size(page_size_value)
    items = ordered[start_index : start_index + size]
    next_token: str | None = None
    if start_index + size < len(ordered) and items:
        next_token = encode_page_token('task-events', {'sequence': int(items[-1].get('sequence', 0))})
    return {'events': items, 'nextPageToken': next_token}
