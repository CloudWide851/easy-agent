
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlparse

import httpx

from agent_common.models import RunStatus, ToolSpec
from agent_common.tools import ToolRegistry
from agent_common.version import runtime_version
from agent_config.app import FederationConfig, FederationExportConfig, FederationRemoteConfig
from agent_integrations.storage import SQLiteRunStore

TERMINAL_TASK_STATUSES = {
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.WAITING_APPROVAL.value,
    'cancelled',
}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _site_origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url.rstrip('/')
    return f"{parsed.scheme}://{parsed.netloc}"


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}" + (path if path.startswith('/') else f"/{path}")


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    return cast(dict[str, Any], response.json())


class FederationClientManager:
    def __init__(self, config: FederationConfig, store: SQLiteRunStore | None = None) -> None:
        self.config = config
        self.store = store
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._started = False
        self._remote_cards: dict[str, dict[str, Any]] = {}
        self._remote_bases: dict[str, str] = {}
        self._remote_push_paths: dict[str, str] = {}

    async def start(self) -> None:
        if self._started:
            return
        for remote in self.config.remotes:
            self._clients[remote.name] = httpx.AsyncClient(
                base_url=remote.base_url.rstrip('/'),
                timeout=remote.timeout_seconds,
                headers=self._build_headers(remote),
            )
        self._started = True

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients = {}
        self._started = False

    def register_tools(self, registry: ToolRegistry) -> None:
        for remote in self.config.remotes:
            tool_name = f'federation__{remote.name}'
            if registry.has(tool_name):
                continue

            async def _handler(arguments: dict[str, Any], context: Any, *, bound_remote: str = remote.name) -> Any:
                target = str(arguments.get('target', '')).strip()
                if not target:
                    raise ValueError('federation target is required')
                input_text = str(arguments.get('input', arguments.get('prompt', '')))
                session_id = arguments.get('session_id') or getattr(context, 'session_id', None)
                return await self.run_remote(
                    bound_remote,
                    target,
                    input_text,
                    session_id=session_id,
                    metadata=dict(arguments.get('metadata', {})),
                )

            registry.register(
                ToolSpec(
                    name=tool_name,
                    description=f'Call remote federated targets through {remote.name}.',
                    input_schema={
                        'type': 'object',
                        'properties': {
                            'target': {'type': 'string'},
                            'input': {'type': 'string'},
                            'prompt': {'type': 'string'},
                            'session_id': {'type': 'string'},
                            'metadata': {'type': 'object'},
                        },
                        'required': ['target'],
                    },
                ),
                _handler,
            )

    async def list_remotes(self) -> list[dict[str, Any]]:
        return [
            {
                'name': remote.name,
                'base_url': remote.base_url,
                'timeout_seconds': remote.timeout_seconds,
                'push_preference': remote.push_preference,
            }
            for remote in self.config.remotes
        ]

    async def inspect_remote(self, remote_name: str) -> dict[str, Any]:
        await self._ensure_remote_metadata(remote_name)
        return dict(self._remote_cards[remote_name])

    async def run_remote(
        self,
        remote_name: str,
        target: str,
        input_text: str,
        *,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._client(remote_name).post(
            _join_url(self._base_path(remote_name), '/tasks/send'),
            json={
                'target': target,
                'input': input_text,
                'session_id': session_id,
                'metadata': metadata or {},
            },
        )
        task = cast(dict[str, Any], _safe_json(response)['task'])
        if str(task['status']) not in TERMINAL_TASK_STATUSES:
            if await self._should_use_sse(remote_name):
                task = await self._await_task_via_sse(remote_name, str(task['task_id']))
            else:
                task = await self._await_task_via_poll(remote_name, str(task['task_id']))
        return self._coerce_task_result(task)

    async def stream_remote(
        self,
        remote_name: str,
        target: str,
        input_text: str,
        *,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        await self._ensure_remote_metadata(remote_name)
        client = self._client(remote_name)
        payload = {
            'target': target,
            'input': input_text,
            'session_id': session_id,
            'metadata': metadata or {},
        }
        events: list[dict[str, Any]] = []
        for path in ('/message:stream', '/tasks/send-stream'):
            try:
                async with client.stream('POST', _join_url(self._base_path(remote_name), path), json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        events.append(cast(dict[str, Any], json.loads(line)))
                if events:
                    return events
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == HTTPStatus.NOT_FOUND:
                    continue
                raise
        return events

    async def get_task(self, remote_name: str, task_id: str) -> dict[str, Any]:
        await self._ensure_remote_metadata(remote_name)
        response = await self._client(remote_name).get(_join_url(self._base_path(remote_name), f'/tasks/{task_id}'))
        payload = _safe_json(response)
        return cast(dict[str, Any], payload['task'])

    async def list_tasks(self, remote_name: str) -> list[dict[str, Any]]:
        await self._ensure_remote_metadata(remote_name)
        response = await self._client(remote_name).get(_join_url(self._base_path(remote_name), '/tasks'))
        payload = _safe_json(response)
        return cast(list[dict[str, Any]], payload['tasks'])

    async def list_task_events(self, remote_name: str, task_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        await self._ensure_remote_metadata(remote_name)
        response = await self._client(remote_name).get(
            _join_url(self._base_path(remote_name), f'/tasks/{task_id}/events'),
            params={'after_sequence': after_sequence},
        )
        payload = _safe_json(response)
        return cast(list[dict[str, Any]], payload['events'])

    async def stream_task_events(self, remote_name: str, task_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        latest_task = await self.get_task(remote_name, task_id)
        if str(latest_task['status']) in TERMINAL_TASK_STATUSES:
            terminal_events = await self.list_task_events(remote_name, task_id, after_sequence)
            for event in terminal_events:
                event.setdefault('event_name', str(event.get('event_kind', 'task_event')))
            return terminal_events
        events: list[dict[str, Any]] = []
        current_after = after_sequence
        reconnect_budget = 3
        while reconnect_budget > 0:
            reconnect_budget -= 1
            try:
                async with self._client(remote_name).stream(
                    'GET',
                    _join_url(self._base_path(remote_name), f'/tasks/{task_id}/events/stream'),
                    params={'after_sequence': current_after},
                ) as response:
                    response.raise_for_status()
                    current_event: str | None = None
                    async for line in response.aiter_lines():
                        if not line:
                            current_event = None
                            continue
                        if line.startswith('event:'):
                            current_event = line.split(':', 1)[1].strip()
                            continue
                        if not line.startswith('data:'):
                            continue
                        body = cast(dict[str, Any], json.loads(line.split(':', 1)[1].strip()))
                        if current_event:
                            body.setdefault('event_name', current_event)
                        event = cast(dict[str, Any], body.get('event', body))
                        current_after = max(current_after, int(event.get('sequence', current_after)))
                        events.append(body)
                        task = cast(dict[str, Any], body.get('task', latest_task))
                        latest_task = task
                        if str(task['status']) in TERMINAL_TASK_STATUSES:
                            return events
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.StreamError, httpx.ReadTimeout):
                await asyncio.sleep(self._remote(remote_name).sse_reconnect_seconds)
                continue
            break
        backlog = await self.list_task_events(remote_name, task_id, current_after)
        for event in backlog:
            event.setdefault('event_name', str(event.get('event_kind', 'task_event')))
            events.append(event)
        return events

    async def cancel_task(self, remote_name: str, task_id: str) -> dict[str, Any]:
        await self._ensure_remote_metadata(remote_name)
        client = self._client(remote_name)
        for path in (f'/tasks/{task_id}:cancel', f'/tasks/{task_id}/cancel'):
            response = await client.post(_join_url(self._base_path(remote_name), path), json={})
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            payload = _safe_json(response)
            return cast(dict[str, Any], payload['task'])
        raise RuntimeError(f'remote task cancel route not found for {remote_name}')

    async def subscribe_task(
        self,
        remote_name: str,
        task_id: str,
        callback_url: str,
        *,
        lease_seconds: int | None = None,
        from_sequence: int = 0,
    ) -> dict[str, Any]:
        await self._ensure_remote_metadata(remote_name)
        client = self._client(remote_name)
        payload = {'callback_url': callback_url, 'lease_seconds': lease_seconds, 'from_sequence': from_sequence}
        for path in (f'/tasks/{task_id}:subscribe', f'/tasks/{task_id}/subscribe'):
            response = await client.post(_join_url(self._base_path(remote_name), path), json=payload)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            return cast(dict[str, Any], body.get('push_notification_config') or body.get('subscription', {}))
        raise RuntimeError(f'remote task subscribe route not found for {remote_name}')

    async def list_subscriptions(self, remote_name: str, task_id: str) -> list[dict[str, Any]]:
        await self._ensure_remote_metadata(remote_name)
        response = await self._client(remote_name).get(_join_url(self._base_path(remote_name), f'/tasks/{task_id}/subscriptions'))
        payload = _safe_json(response)
        return cast(list[dict[str, Any]], payload['subscriptions'])

    async def renew_subscription(
        self,
        remote_name: str,
        task_id: str,
        subscription_id: str,
        *,
        lease_seconds: int | None = None,
    ) -> dict[str, Any]:
        await self._ensure_remote_metadata(remote_name)
        response = await self._client(remote_name).post(
            _join_url(self._base_path(remote_name), f'/tasks/{task_id}/subscriptions/{subscription_id}/renew'),
            json={'lease_seconds': lease_seconds},
        )
        payload = _safe_json(response)
        return cast(dict[str, Any], payload['subscription'])

    async def cancel_subscription(self, remote_name: str, task_id: str, subscription_id: str) -> dict[str, Any]:
        await self._ensure_remote_metadata(remote_name)
        response = await self._client(remote_name).post(
            _join_url(self._base_path(remote_name), f'/tasks/{task_id}/subscriptions/{subscription_id}/cancel'),
            json={},
        )
        payload = _safe_json(response)
        return cast(dict[str, Any], payload['subscription'])

    async def set_push_notification(
        self,
        remote_name: str,
        task_id: str,
        callback_url: str,
        *,
        lease_seconds: int | None = None,
        from_sequence: int = 0,
    ) -> dict[str, Any]:
        await self._ensure_remote_metadata(remote_name)
        client = self._client(remote_name)
        payload = {'callback_url': callback_url, 'lease_seconds': lease_seconds, 'from_sequence': from_sequence}
        candidates = (
            ('POST', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfigs')),
            ('POST', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfig/set')),
        )
        for method, path in candidates:
            response = await client.request(method, path, json=payload)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            return cast(dict[str, Any], body.get('push_notification_config') or body.get('subscription', {}))
        raise RuntimeError(f'push notification set route not found for {remote_name}')

    async def get_push_notification(self, remote_name: str, task_id: str, config_id: str) -> dict[str, Any]:
        await self._ensure_remote_metadata(remote_name)
        client = self._client(remote_name)
        candidates = (
            ('GET', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfigs/{config_id}'), None),
            ('GET', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfig/get'), {'config_id': config_id}),
        )
        for method, path, params in candidates:
            response = await client.request(method, path, params=params)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            return cast(dict[str, Any], body.get('push_notification_config') or body.get('subscription', {}))
        raise RuntimeError(f'push notification get route not found for {remote_name}')

    async def list_push_notifications(self, remote_name: str, task_id: str) -> list[dict[str, Any]]:
        await self._ensure_remote_metadata(remote_name)
        client = self._client(remote_name)
        candidates = (
            _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfigs'),
            _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfig/list'),
        )
        for path in candidates:
            response = await client.get(path)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            return cast(list[dict[str, Any]], body.get('push_notification_configs') or body.get('subscriptions', []))
        raise RuntimeError(f'push notification list route not found for {remote_name}')

    async def delete_push_notification(self, remote_name: str, task_id: str, config_id: str) -> dict[str, Any]:
        await self._ensure_remote_metadata(remote_name)
        client = self._client(remote_name)
        candidates = (
            ('DELETE', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfigs/{config_id}'), None),
            ('POST', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfig/delete'), {'config_id': config_id}),
        )
        for method, path, payload in candidates:
            response = await client.request(method, path, json=payload)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            return cast(dict[str, Any], body.get('push_notification_config') or body.get('subscription', {}))
        raise RuntimeError(f'push notification delete route not found for {remote_name}')

    async def send_subscribe(
        self,
        remote_name: str,
        target: str,
        input_text: str,
        callback_url: str,
        *,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        lease_seconds: int | None = None,
        from_sequence: int = 0,
    ) -> dict[str, Any]:
        await self._ensure_remote_metadata(remote_name)
        client = self._client(remote_name)
        payload = {
            'target': target,
            'input': input_text,
            'session_id': session_id,
            'metadata': metadata or {},
            'callback_url': callback_url,
            'lease_seconds': lease_seconds,
            'from_sequence': from_sequence,
        }
        candidates = (
            _join_url(self._base_path(remote_name), '/tasks/sendSubscribe'),
            _join_url(self._base_path(remote_name), '/message:send'),
        )
        for path in candidates:
            response = await client.post(path, json=payload)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            return {
                'task': cast(dict[str, Any], body['task']),
                'subscription': cast(dict[str, Any], body.get('push_notification_config') or body.get('subscription', {})),
            }
        raise RuntimeError(f'sendSubscribe route not found for {remote_name}')

    async def resubscribe_task(
        self,
        remote_name: str,
        task_id: str,
        *,
        from_sequence: int = 0,
        callback_url: str | None = None,
        lease_seconds: int | None = None,
    ) -> dict[str, Any]:
        await self._ensure_remote_metadata(remote_name)
        client = self._client(remote_name)
        payload = {
            'task_id': task_id,
            'from_sequence': from_sequence,
            'callback_url': callback_url,
            'lease_seconds': lease_seconds,
        }
        candidates = (
            _join_url(self._base_path(remote_name), '/tasks/resubscribe'),
            _join_url(self._base_path(remote_name), f'/tasks/{task_id}:subscribe'),
        )
        for path in candidates:
            response = await client.post(path, json=payload)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            task = cast(dict[str, Any], body.get('task') or await self.get_task(remote_name, task_id))
            events = cast(list[dict[str, Any]], body.get('events') or await self.list_task_events(remote_name, task_id, from_sequence))
            subscription = cast(dict[str, Any], body.get('push_notification_config') or body.get('subscription', {}))
            return {'task': task, 'events': events, 'subscription': subscription}
        raise RuntimeError(f'resubscribe route not found for {remote_name}')

    async def _should_use_sse(self, remote_name: str) -> bool:
        remote = self._remote(remote_name)
        if remote.push_preference == 'poll':
            return False
        if remote.push_preference == 'sse':
            return True
        details = await self.inspect_remote(remote_name)
        capabilities = cast(dict[str, Any], details['extended_card'].get('capabilities', {}))
        push_delivery = cast(dict[str, Any], capabilities.get('push_delivery', {}))
        return bool(push_delivery.get('sse_events') or capabilities.get('send_streaming_message'))

    async def _await_task_via_poll(self, remote_name: str, task_id: str) -> dict[str, Any]:
        task = await self.get_task(remote_name, task_id)
        while str(task['status']) not in TERMINAL_TASK_STATUSES:
            await asyncio.sleep(self._remote(remote_name).poll_seconds)
            task = await self.get_task(remote_name, task_id)
        return task

    async def _await_task_via_sse(self, remote_name: str, task_id: str) -> dict[str, Any]:
        latest_task = await self.get_task(remote_name, task_id)
        after_sequence = 0
        reconnect_budget = 3
        while reconnect_budget > 0:
            reconnect_budget -= 1
            try:
                async with self._client(remote_name).stream(
                    'GET',
                    _join_url(self._base_path(remote_name), f'/tasks/{task_id}/events/stream'),
                    params={'after_sequence': after_sequence},
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line or not line.startswith('data:'):
                            continue
                        payload = cast(dict[str, Any], json.loads(line.split(':', 1)[1].strip()))
                        event = cast(dict[str, Any], payload.get('event', {}))
                        task = cast(dict[str, Any], payload.get('task', latest_task))
                        latest_task = task
                        after_sequence = max(after_sequence, int(event.get('sequence', after_sequence)))
                        if str(task['status']) in TERMINAL_TASK_STATUSES:
                            return latest_task
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.StreamError, httpx.ReadTimeout):
                await asyncio.sleep(self._remote(remote_name).sse_reconnect_seconds)
                continue
            break
        return await self._await_task_via_poll(remote_name, task_id)

    @staticmethod
    def _coerce_task_result(task: dict[str, Any]) -> dict[str, Any]:
        status = str(task['status'])
        if status == RunStatus.SUCCEEDED.value:
            return dict(cast(dict[str, Any], task.get('response_payload', {})))
        if status == RunStatus.WAITING_APPROVAL.value:
            return {
                'status': status,
                'task_id': str(task['task_id']),
                'request_id': task.get('request_id'),
            }
        if status == 'cancelled':
            return {'status': 'cancelled', 'task_id': str(task['task_id'])}
        raise RuntimeError(str(task.get('error_message') or f'remote task failed: {task}'))

    def _client(self, remote_name: str) -> httpx.AsyncClient:
        if not self._started:
            raise RuntimeError('federation manager is not started')
        return self._clients[remote_name]

    def _remote(self, remote_name: str) -> FederationRemoteConfig:
        return self.config.remote_map[remote_name]

    def _base_path(self, remote_name: str) -> str:
        return self._remote_bases.get(remote_name, self._remote(remote_name).base_url.rstrip('/'))

    def _push_path(self, remote_name: str) -> str:
        return self._remote_push_paths.get(remote_name, self._base_path(remote_name))

    async def _ensure_remote_metadata(self, remote_name: str) -> None:
        if remote_name in self._remote_cards and remote_name in self._remote_bases:
            return
        remote = self._remote(remote_name)
        client = self._client(remote_name)
        discovery_errors: list[str] = []
        card: dict[str, Any] | None = None
        base_url = remote.base_url.rstrip('/')
        for candidate in self._discovery_candidates(remote):
            try:
                response = await client.get(candidate)
                if response.status_code >= 400:
                    discovery_errors.append(f'{candidate}:{response.status_code}')
                    continue
                payload = cast(dict[str, Any], response.json())
                card = payload
                base_url = self._maybe_parse_base(payload.get('url')) or self._maybe_parse_base(candidate) or base_url
                break
            except httpx.HTTPError as exc:
                discovery_errors.append(f'{candidate}:{exc}')
        if card is None:
            raise RuntimeError(f'failed to discover remote {remote_name}: {"; ".join(discovery_errors)}')
        extended_card = card
        for candidate in (
            _join_url(base_url, '/extendedAgentCard'),
            _join_url(base_url, '/agent-card/extended'),
        ):
            try:
                response = await client.get(candidate)
                if response.status_code >= 400:
                    continue
                extended_card = cast(dict[str, Any], response.json())
                break
            except httpx.HTTPError:
                continue
        self._remote_cards[remote_name] = {'card': card, 'extended_card': extended_card, 'discovery_url': remote.discovery_url}
        self._remote_bases[remote_name] = base_url.rstrip('/')
        self._remote_push_paths[remote_name] = base_url.rstrip('/')

    def _discovery_candidates(self, remote: FederationRemoteConfig) -> list[str]:
        origin = _site_origin(remote.discovery_url or remote.base_url)
        candidates: list[str] = []
        if remote.discovery_url:
            candidates.append(remote.discovery_url)
        candidates.extend(
            [
                _join_url(origin, '/.well-known/agent-card.json'),
                _join_url(origin, '/.well-known/agent.json'),
                _join_url(remote.base_url.rstrip('/'), '/agent-card'),
                _join_url(remote.base_url.rstrip('/'), '/a2a/agent-card'),
            ]
        )
        seen: set[str] = set()
        unique: list[str] = []
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)
        return unique

    @staticmethod
    def _maybe_parse_base(value: Any) -> str | None:
        if not value:
            return None
        text = str(value).rstrip('/')
        parsed = urlparse(text)
        if not parsed.scheme or not parsed.netloc:
            return None
        for suffix in (
            '/.well-known/agent-card.json',
            '/.well-known/agent.json',
            '/extendedAgentCard',
            '/agent-card/extended',
            '/agent-card',
        ):
            if parsed.path.endswith(suffix):
                base_path = parsed.path[: -len(suffix)].rstrip('/')
                return f'{parsed.scheme}://{parsed.netloc}{base_path}'
        return text

    @staticmethod
    def _build_headers(remote: FederationRemoteConfig) -> dict[str, str]:
        headers = dict(remote.headers)
        auth = remote.auth
        if auth.type.value == 'bearer_env' and auth.token_env:
            token = os.environ.get(auth.token_env, '').strip()
            if token:
                headers[auth.header_name] = f'{auth.value_prefix}{token}'
        if auth.type.value == 'header_env' and auth.header_env:
            raw = os.environ.get(auth.header_env, '').strip()
            if raw:
                headers[auth.header_name] = raw
        return headers


class FederationServer:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.config = runtime.config.federation
        self.store: SQLiteRunStore = runtime.store
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def public_base_url(self) -> str:
        if self.config.server.public_url:
            return str(self.config.server.public_url).rstrip('/')
        host = self.config.server.host
        if host in {'0.0.0.0', '::'}:
            host = '127.0.0.1'
        return f'http://{host}:{self.config.server.port}{self.config.server.base_path.rstrip("/")}'

    def agent_card(self) -> dict[str, Any]:
        public_url = self.public_base_url()
        origin = _site_origin(public_url)
        exports = []
        for item in self.config.exports:
            exports.append(
                {
                    'name': item.name,
                    'description': item.description,
                    'target_type': item.target_type,
                    'tags': item.tags,
                    'input_modes': item.input_modes,
                    'output_modes': item.output_modes,
                    'modalities': item.modalities,
                    'capabilities': self._export_capabilities(item),
                }
            )
        return {
            'name': 'easy-agent-federation',
            'description': 'A2A-style export surface for local easy-agent targets.',
            'url': public_url,
            'public_base_url': public_url,
            'agent_endpoint': public_url,
            'well_known_url': _join_url(origin, self.config.server.well_known_path),
            'legacy_well_known_url': _join_url(origin, self.config.server.legacy_well_known_path),
            'version': runtime_version(),
            'protocol_version': self.config.server.protocol_version,
            'card_schema_version': self.config.server.card_schema_version,
            'default_input_modes': ['text'],
            'default_output_modes': ['text'],
            'push_delivery': {
                'polling': True,
                'webhook_subscribe': True,
                'sse_events': True,
            },
            'interfaces': {
                'well_known': True,
                'message_send': True,
                'message_stream': True,
                'send_subscribe': True,
                'resubscribe': True,
                'push_notification_config': True,
            },
            'auth_hints': [
                {
                    'type': 'none',
                    'header_name': 'Authorization',
                    'note': 'Server-side auth enforcement is not configured by default in easy-agent federation.',
                }
            ],
            'compatibility': {
                'runtime': 'easy-agent',
                'runtime_version': runtime_version(),
                'minimum_card_schema_version': self.config.server.card_schema_version,
                'supported_interfaces': ['well-known', 'a2a-http', 'a2a-legacy-http'],
            },
            'exports': exports,
        }

    def extended_agent_card(self) -> dict[str, Any]:
        return {
            **self.agent_card(),
            'capabilities': {
                'send_message': True,
                'send_streaming_message': True,
                'get_task': True,
                'list_tasks': True,
                'cancel_task': True,
                'subscribe_to_task': True,
                'send_subscribe': True,
                'resubscribe': True,
                'push_notification_config': True,
                'push_delivery': {
                    'polling': True,
                    'webhook_subscribe': True,
                    'sse_events': True,
                },
            },
            'subscribe_policy': {
                'lease_seconds_default': self.config.server.subscription_lease_seconds,
                'renewable': True,
                'supports_backfill': True,
            },
            'retry_policy': {
                'max_attempts': self.config.server.retry_max_attempts,
                'initial_backoff_seconds': self.config.server.retry_initial_backoff_seconds,
                'backoff_multiplier': self.config.server.retry_backoff_multiplier,
            },
            'endpoints': {
                'message_send': _join_url(self.public_base_url(), '/message:send'),
                'message_stream': _join_url(self.public_base_url(), '/message:stream'),
                'send_subscribe': _join_url(self.public_base_url(), '/tasks/sendSubscribe'),
                'resubscribe': _join_url(self.public_base_url(), '/tasks/resubscribe'),
                'push_notification_configs': _join_url(self.public_base_url(), '/tasks/{task_id}/pushNotificationConfigs'),
            },
        }

    def start(self) -> dict[str, Any]:
        if self._server is not None:
            return self.status()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                del format, args
                return None

            def _json(self) -> dict[str, Any]:
                length = int(self.headers.get('Content-Length', '0') or '0')
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                return cast(dict[str, Any], json.loads(raw.decode('utf-8')))

            def _query(self) -> dict[str, list[str]]:
                parsed = urlparse(self.path)
                return parse_qs(parsed.query)

            def _write(self, payload: dict[str, Any], status: int = 200) -> None:
                encoded = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _write_sse(self, event_name: str, payload: dict[str, Any]) -> None:
                encoded = (
                    f'event: {event_name}\n'
                    f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
                ).encode()
                self.wfile.write(encoded)
                self.wfile.flush()

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip('/') or '/'
                query = self._query()
                base = server.config.server.base_path.rstrip('/')
                if path in {server.config.server.well_known_path, server.config.server.legacy_well_known_path}:
                    self._write(server.agent_card())
                    return
                if path in {f'{base}/agent-card', f'{base}/agentCard'}:
                    self._write(server.agent_card())
                    return
                if path in {f'{base}/agent-card/extended', f'{base}/extendedAgentCard'}:
                    self._write(server.extended_agent_card())
                    return
                if path == f'{base}/tasks':
                    self._write({'tasks': server.list_tasks()})
                    return
                if path.endswith('/events/stream') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-3]
                    after_sequence = int(query.get('after_sequence', ['0'])[0])
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('Connection', 'keep-alive')
                    self.end_headers()
                    while True:
                        events = server.list_task_events(task_id, after_sequence)
                        for event in events:
                            payload = {'event': event, 'task': event['task']}
                            self._write_sse(str(event['event_kind']), payload)
                            after_sequence = int(event['sequence'])
                        task = server.get_task(task_id)
                        if str(task['status']) in TERMINAL_TASK_STATUSES and not server.list_task_events(task_id, after_sequence):
                            break
                        time.sleep(0.1)
                    return
                if path.endswith('/events') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    after_sequence = int(query.get('after_sequence', ['0'])[0])
                    self._write({'events': server.list_task_events(task_id, after_sequence)})
                    return
                if path.endswith('/subscriptions') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    self._write({'subscriptions': server.list_subscriptions(task_id)})
                    return
                if path.endswith('/pushNotificationConfig/get') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-3]
                    config_id = str(query.get('config_id', [''])[0])
                    self._write({'push_notification_config': server.get_push_notification(task_id, config_id)})
                    return
                if path.endswith('/pushNotificationConfig/list') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-3]
                    configs = server.list_push_notifications(task_id)
                    self._write({'push_notification_configs': configs, 'subscriptions': configs})
                    return
                if '/pushNotificationConfigs/' in path and path.startswith(f'{base}/tasks/'):
                    parts = path.split('/')
                    task_id = parts[-3]
                    config_id = parts[-1]
                    self._write({'push_notification_config': server.get_push_notification(task_id, config_id)})
                    return
                if path.endswith('/pushNotificationConfigs') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    configs = server.list_push_notifications(task_id)
                    self._write({'push_notification_configs': configs, 'subscriptions': configs})
                    return
                if path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-1]
                    self._write({'task': server.get_task(task_id)})
                    return
                self._write({'error': 'not_found'}, status=404)

            def do_DELETE(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip('/')
                base = server.config.server.base_path.rstrip('/')
                if '/pushNotificationConfigs/' in path and path.startswith(f'{base}/tasks/'):
                    parts = path.split('/')
                    task_id = parts[-3]
                    config_id = parts[-1]
                    deleted = server.delete_push_notification(task_id, config_id)
                    self._write({'push_notification_config': deleted, 'subscription': deleted})
                    return
                self._write({'error': 'not_found'}, status=404)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip('/')
                base = server.config.server.base_path.rstrip('/')
                payload = self._json()
                if path in {f'{base}/tasks/send', f'{base}/message:send'}:
                    task = server.start_task(
                        str(payload.get('target', '')),
                        str(payload.get('input', '')),
                        session_id=cast(str | None, payload.get('session_id')),
                        metadata=dict(cast(dict[str, Any], payload.get('metadata', {}))),
                    )
                    callback_url = str(payload.get('callback_url', '')).strip()
                    if callback_url:
                        subscription = server.set_push_notification(
                            str(task['task_id']),
                            callback_url,
                            lease_seconds=cast(int | None, payload.get('lease_seconds')),
                            from_sequence=int(payload.get('from_sequence', 0) or 0),
                        )
                        self._write({'task': task, 'push_notification_config': subscription, 'subscription': subscription}, status=202)
                        return
                    self._write({'task': task}, status=202)
                    return
                if path in {f'{base}/tasks/send-stream', f'{base}/message:stream'}:
                    task = server.start_task(
                        str(payload.get('target', '')),
                        str(payload.get('input', '')),
                        session_id=cast(str | None, payload.get('session_id')),
                        metadata=dict(cast(dict[str, Any], payload.get('metadata', {}))),
                    )
                    task_id = str(task['task_id'])
                    after_sequence = 0
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
                    self.end_headers()
                    while True:
                        events = server.list_task_events(task_id, after_sequence)
                        for event in events:
                            line = json.dumps({'event': event, 'task': event['task']}, ensure_ascii=False).encode() + b'\n'
                            self.wfile.write(line)
                            self.wfile.flush()
                            after_sequence = int(event['sequence'])
                        current = server.get_task(task_id)
                        if str(current['status']) in TERMINAL_TASK_STATUSES and not server.list_task_events(task_id, after_sequence):
                            break
                        time.sleep(0.1)
                    return
                if path == f'{base}/tasks/sendSubscribe':
                    response = server.send_subscribe(
                        str(payload.get('target', '')),
                        str(payload.get('input', '')),
                        str(payload.get('callback_url', '')).strip(),
                        session_id=cast(str | None, payload.get('session_id')),
                        metadata=dict(cast(dict[str, Any], payload.get('metadata', {}))),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                    )
                    self._write(response, status=202)
                    return
                if path == f'{base}/tasks/resubscribe':
                    response = server.resubscribe_task(
                        str(payload.get('task_id', '')).strip(),
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                        callback_url=cast(str | None, payload.get('callback_url')),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                    )
                    self._write(response)
                    return
                if path.endswith(':cancel') and path.startswith(f'{base}/tasks/'):
                    task_id = path.rsplit('/tasks/', 1)[1].split(':', 1)[0]
                    self._write({'task': server.cancel_task(task_id)})
                    return
                if path.endswith('/cancel') and '/subscriptions/' not in path and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    self._write({'task': server.cancel_task(task_id)})
                    return
                if path.endswith(':subscribe') and path.startswith(f'{base}/tasks/'):
                    task_id = path.rsplit('/tasks/', 1)[1].split(':', 1)[0]
                    response = server.resubscribe_task(
                        task_id,
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                        callback_url=cast(str | None, payload.get('callback_url')),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                    )
                    self._write(response, status=202)
                    return
                if path.endswith('/subscribe') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    subscription = server.subscribe_task(
                        task_id,
                        str(payload.get('callback_url', '')),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                    )
                    self._write({'task': server.get_task(task_id), 'push_notification_config': subscription, 'subscription': subscription}, status=202)
                    return
                if path.endswith('/pushNotificationConfigs') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    subscription = server.set_push_notification(
                        task_id,
                        str(payload.get('callback_url', '')),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                    )
                    self._write({'push_notification_config': subscription, 'subscription': subscription}, status=202)
                    return
                if path.endswith('/pushNotificationConfig/set') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-3]
                    subscription = server.set_push_notification(
                        task_id,
                        str(payload.get('callback_url', '')),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                    )
                    self._write({'push_notification_config': subscription, 'subscription': subscription}, status=202)
                    return
                if path.endswith('/pushNotificationConfig/delete') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-3]
                    deleted = server.delete_push_notification(task_id, str(payload.get('config_id', '')))
                    self._write({'push_notification_config': deleted, 'subscription': deleted})
                    return
                if '/subscriptions/' in path and path.endswith('/renew'):
                    parts = path.split('/')
                    task_id = parts[-4]
                    subscription_id = parts[-2]
                    subscription = server.renew_subscription(task_id, subscription_id, lease_seconds=cast(int | None, payload.get('lease_seconds')))
                    self._write({'subscription': subscription})
                    return
                if '/subscriptions/' in path and path.endswith('/cancel'):
                    parts = path.split('/')
                    task_id = parts[-4]
                    subscription_id = parts[-2]
                    subscription = server.cancel_subscription(task_id, subscription_id)
                    self._write({'subscription': subscription})
                    return
                self._write({'error': 'not_found'}, status=404)

        self._server = ThreadingHTTPServer((self.config.server.host, self.config.server.port), Handler)
        if self.config.server.port == 0:
            self.config.server.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            'running': self._server is not None,
            'host': self.config.server.host,
            'port': self.config.server.port,
            'base_path': self.config.server.base_path,
            'public_base_url': self.public_base_url(),
            'well_known_url': _join_url(_site_origin(self.public_base_url()), self.config.server.well_known_path),
            'legacy_well_known_url': _join_url(_site_origin(self.public_base_url()), self.config.server.legacy_well_known_path),
            'version': runtime_version(),
            'push_delivery': ['polling', 'webhook_subscribe', 'sse_events'],
        }

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    def start_task(
        self,
        export_name: str,
        input_text: str,
        *,
        session_id: str | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        export = self._export(export_name)
        task_id = uuid.uuid4().hex
        task: dict[str, Any] = {
            'task_id': task_id,
            'export_name': export.name,
            'target_type': export.target_type,
            'status': 'queued',
            'input_payload': {'input': input_text, 'session_id': session_id, 'metadata': metadata},
            'response_payload': None,
            'error_message': None,
            'local_run_id': None,
            'request_id': None,
            'subscribers': [],
            'created_at': _iso_now(),
            'updated_at': _iso_now(),
        }
        with self._lock:
            self._tasks[task_id] = task
        self.store.create_federated_task(task_id, export.name, export.target_type, str(task['status']), cast(dict[str, Any], task['input_payload']))
        self._record_task_event(task_id, 'task_queued')
        thread = threading.Thread(
            target=self._run_task,
            args=(task_id, export, input_text, session_id, metadata),
            daemon=True,
        )
        thread.start()
        return self.get_task(task_id)

    def send_subscribe(
        self,
        export_name: str,
        input_text: str,
        callback_url: str,
        *,
        session_id: str | None,
        metadata: dict[str, Any],
        lease_seconds: int | None,
        from_sequence: int,
    ) -> dict[str, Any]:
        task = self.start_task(export_name, input_text, session_id=session_id, metadata=metadata)
        subscription = self.set_push_notification(
            str(task['task_id']),
            callback_url,
            lease_seconds=lease_seconds,
            from_sequence=from_sequence,
        )
        return {'task': task, 'push_notification_config': subscription, 'subscription': subscription}

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is not None:
            return dict(task)
        return self.store.load_federated_task(task_id)

    def list_tasks(self) -> list[dict[str, Any]]:
        if self._tasks:
            return [dict(item) for item in self._tasks.values()]
        return self.store.list_federated_tasks()

    def list_task_events(self, task_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        events = self.store.list_federated_task_events(task_id, after_sequence)
        for event in events:
            payload = cast(dict[str, Any], event.get('payload', {}))
            event['task'] = cast(dict[str, Any], payload.get('task', self.get_task(task_id)))
        return events

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        local_run_id = task.get('local_run_id')
        if local_run_id:
            self.runtime.interrupt_run(str(local_run_id), {'reason': 'federation cancel'})
        self._update_task(task_id, status='cancelled', error_message='cancelled by remote caller')
        return self.get_task(task_id)

    def subscribe_task(
        self,
        task_id: str,
        callback_url: str,
        *,
        lease_seconds: int | None,
        from_sequence: int,
    ) -> dict[str, Any]:
        if not callback_url:
            raise RuntimeError('callback_url is required for webhook subscriptions')
        subscription_id = uuid.uuid4().hex
        lease = lease_seconds or self.config.server.subscription_lease_seconds
        lease_expires_at = (datetime.now(UTC) + timedelta(seconds=lease)).isoformat()
        self.store.create_federated_subscription(
            subscription_id=subscription_id,
            task_id=task_id,
            mode='webhook',
            callback_url=callback_url,
            status='active',
            lease_expires_at=lease_expires_at,
            from_sequence=from_sequence,
        )
        subscription = self.store.load_federated_subscription(subscription_id)
        backlog = self.list_task_events(task_id, after_sequence=from_sequence)
        if backlog:
            self._dispatch_subscription_events(subscription, backlog)
        return self.store.load_federated_subscription(subscription_id)

    def list_subscriptions(self, task_id: str) -> list[dict[str, Any]]:
        subscriptions = [self._refresh_subscription(item) for item in self.store.list_federated_subscriptions(task_id)]
        return subscriptions

    def renew_subscription(self, task_id: str, subscription_id: str, *, lease_seconds: int | None) -> dict[str, Any]:
        subscription = self.store.load_federated_subscription(subscription_id)
        if subscription['task_id'] != task_id:
            raise RuntimeError(f'Subscription {subscription_id} does not belong to task {task_id}')
        lease = lease_seconds or self.config.server.subscription_lease_seconds
        lease_expires_at = (datetime.now(UTC) + timedelta(seconds=lease)).isoformat()
        self.store.update_federated_subscription(
            subscription_id,
            status='active',
            lease_expires_at=lease_expires_at,
            last_error=None,
            next_retry_at=None,
        )
        return self.store.load_federated_subscription(subscription_id)

    def cancel_subscription(self, task_id: str, subscription_id: str) -> dict[str, Any]:
        subscription = self.store.load_federated_subscription(subscription_id)
        if subscription['task_id'] != task_id:
            raise RuntimeError(f'Subscription {subscription_id} does not belong to task {task_id}')
        self.store.update_federated_subscription(subscription_id, status='cancelled', next_retry_at=None)
        return self.store.load_federated_subscription(subscription_id)

    def set_push_notification(
        self,
        task_id: str,
        callback_url: str,
        *,
        lease_seconds: int | None,
        from_sequence: int,
    ) -> dict[str, Any]:
        return self.subscribe_task(task_id, callback_url, lease_seconds=lease_seconds, from_sequence=from_sequence)

    def get_push_notification(self, task_id: str, config_id: str) -> dict[str, Any]:
        subscription = self.store.load_federated_subscription(config_id)
        if subscription['task_id'] != task_id:
            raise RuntimeError(f'Push notification config {config_id} does not belong to task {task_id}')
        return self._refresh_subscription(subscription)

    def list_push_notifications(self, task_id: str) -> list[dict[str, Any]]:
        return self.list_subscriptions(task_id)

    def delete_push_notification(self, task_id: str, config_id: str) -> dict[str, Any]:
        return self.cancel_subscription(task_id, config_id)

    def resubscribe_task(
        self,
        task_id: str,
        *,
        from_sequence: int,
        callback_url: str | None,
        lease_seconds: int | None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        events = self.list_task_events(task_id, after_sequence=from_sequence)
        payload: dict[str, Any] = {'task': task, 'events': events}
        if callback_url:
            subscription = self.set_push_notification(
                task_id,
                callback_url,
                lease_seconds=lease_seconds,
                from_sequence=from_sequence,
            )
            payload['push_notification_config'] = subscription
            payload['subscription'] = subscription
        return payload

    def _run_task(
        self,
        task_id: str,
        export: FederationExportConfig,
        input_text: str,
        session_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        del metadata
        self._update_task(task_id, status='running')
        try:
            result = asyncio.run(self.runtime.run_federated_export(export.name, input_text, session_id=session_id))
        except Exception as exc:
            if str(self.get_task(task_id).get('status')) == 'cancelled':
                return
            self._update_task(task_id, status=RunStatus.FAILED.value, error_message=str(exc))
            return
        if str(self.get_task(task_id).get('status')) == 'cancelled':
            return
        payload = dict(cast(dict[str, Any], result))
        local_run_id = payload.get('run_id')
        status = str(payload.get('status') or RunStatus.SUCCEEDED.value)
        request_id = payload.get('request_id')
        self._update_task(
            task_id,
            status=status,
            local_run_id=local_run_id,
            response_payload=payload,
            request_id=request_id,
            error_message=None,
        )

    def _update_task(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            task = self._tasks.setdefault(task_id, self.store.load_federated_task(task_id))
            previous_status = str(task.get('status', 'queued'))
            task.update(changes)
            task['updated_at'] = _iso_now()
            subscribers = list(cast(list[str], task.get('subscribers', [])))
        self.store.update_federated_task(task_id, **changes, updated_at=task['updated_at'], subscribers=subscribers)
        current_status = str(task['status'])
        if current_status != previous_status:
            self._record_task_event(task_id, f'task_{current_status}')
        else:
            self._record_task_event(task_id, 'task_updated')

    def _record_task_event(self, task_id: str, event_kind: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        event = self.store.create_federated_task_event(task_id, event_kind, {'task': task})
        subscriptions: list[dict[str, Any]] = []
        for item in self.store.list_federated_subscriptions(task_id):
            refreshed = self._refresh_subscription(item, dispatch_pending=False)
            if refreshed['status'] in {'active', 'retrying'}:
                subscriptions.append(refreshed)
        if subscriptions:
            self._dispatch_subscription_events_batch(subscriptions, [event])
        return event

    def _dispatch_subscription_events(self, subscription: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        if not events:
            return subscription
        if subscription['status'] not in {'active', 'retrying'}:
            return subscription
        if subscription['mode'] != 'webhook':
            return subscription
        return self._deliver_subscription_events(subscription, events)

    def _dispatch_subscription_events_batch(
        self,
        subscriptions: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [self._dispatch_subscription_events(subscription, events) for subscription in subscriptions]

    def _deliver_subscription_events(
        self,
        subscription: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not events:
            return subscription
        now = datetime.now(UTC)
        lease_expires_at = subscription.get('lease_expires_at')
        if lease_expires_at:
            lease_deadline = datetime.fromisoformat(str(lease_expires_at))
            if lease_deadline <= now:
                self.store.update_federated_subscription(
                    str(subscription['subscription_id']),
                    status='expired',
                    next_retry_at=None,
                    last_error='subscription lease expired before delivery',
                )
                return self.store.load_federated_subscription(str(subscription['subscription_id']))
        next_retry_at = subscription.get('next_retry_at')
        if subscription['status'] == 'retrying' and next_retry_at:
            retry_deadline = datetime.fromisoformat(str(next_retry_at))
            if retry_deadline > now:
                return subscription
        callback_url = str(subscription.get('callback_url') or '').strip()
        if not callback_url:
            self.store.update_federated_subscription(
                str(subscription['subscription_id']),
                status='failed',
                last_error='missing callback_url',
                next_retry_at=None,
            )
            return self.store.load_federated_subscription(str(subscription['subscription_id']))
        task = self.get_task(str(subscription['task_id']))
        payload = {
            'subscription_id': str(subscription['subscription_id']),
            'task_id': str(subscription['task_id']),
            'delivery_mode': 'webhook',
            'task': task,
            'events': events,
        }
        try:
            self._deliver_subscription_event(callback_url, payload)
        except Exception as exc:
            attempts = int(subscription.get('delivery_attempts', 0)) + 1
            max_attempts = self.config.server.retry_max_attempts
            retryable = attempts < max_attempts
            backoff_seconds = self.config.server.retry_initial_backoff_seconds * (
                self.config.server.retry_backoff_multiplier ** max(0, attempts - 1)
            )
            next_retry = (datetime.now(UTC) + timedelta(seconds=backoff_seconds)).isoformat() if retryable else None
            self.store.update_federated_subscription(
                str(subscription['subscription_id']),
                status='retrying' if retryable else 'failed',
                delivery_attempts=attempts,
                last_error=str(exc),
                next_retry_at=next_retry,
            )
            return self.store.load_federated_subscription(str(subscription['subscription_id']))
        latest_sequence = max(int(event['sequence']) for event in events)
        final_status = 'active'
        if str(task['status']) in TERMINAL_TASK_STATUSES:
            terminal_backlog = self.store.list_federated_task_events(str(subscription['task_id']), latest_sequence)
            if not terminal_backlog:
                final_status = 'delivered'
        self.store.update_federated_subscription(
            str(subscription['subscription_id']),
            status=final_status,
            last_delivered_sequence=latest_sequence,
            delivery_attempts=0,
            last_error=None,
            next_retry_at=None,
        )
        return self.store.load_federated_subscription(str(subscription['subscription_id']))

    @staticmethod
    def _deliver_subscription_event(callback_url: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        request = urllib_request.Request(
            callback_url,
            data=encoded,
            headers={'Content-Type': 'application/json; charset=utf-8'},
            method='POST',
        )
        with urllib_request.urlopen(request, timeout=10) as response:
            status = getattr(response, 'status', HTTPStatus.OK)
            if int(status) >= 400:
                raise RuntimeError(f'callback delivery failed with status {status}')

    def _refresh_subscription(
        self,
        subscription: dict[str, Any],
        *,
        dispatch_pending: bool = True,
    ) -> dict[str, Any]:
        current = self.store.load_federated_subscription(str(subscription['subscription_id']))
        if current['status'] in {'cancelled', 'expired', 'failed'}:
            return current
        lease_expires_at = current.get('lease_expires_at')
        if lease_expires_at and datetime.fromisoformat(str(lease_expires_at)) <= datetime.now(UTC):
            self.store.update_federated_subscription(
                str(current['subscription_id']),
                status='expired',
                next_retry_at=None,
                last_error='subscription lease expired',
            )
            return self.store.load_federated_subscription(str(current['subscription_id']))
        if not dispatch_pending:
            return current
        after_sequence = max(int(current.get('from_sequence', 0)), int(current.get('last_delivered_sequence', 0)))
        pending_events = self.list_task_events(str(current['task_id']), after_sequence)
        if not pending_events:
            task = self.get_task(str(current['task_id']))
            if current['status'] == 'active' and str(task['status']) in TERMINAL_TASK_STATUSES:
                self.store.update_federated_subscription(str(current['subscription_id']), status='delivered', next_retry_at=None)
                return self.store.load_federated_subscription(str(current['subscription_id']))
            return current
        return self._dispatch_subscription_events(current, pending_events)

    def _export(self, export_name: str) -> FederationExportConfig:
        try:
            return cast(FederationExportConfig, self.config.export_map[export_name])
        except KeyError as exc:
            raise RuntimeError(f'Unknown federation export: {export_name}') from exc

    @staticmethod
    def _export_capabilities(item: FederationExportConfig) -> dict[str, Any]:
        capabilities: dict[str, Any] = {key: True for key in item.capabilities}
        capabilities.setdefault('modalities', item.modalities)
        capabilities.setdefault('streaming', 'streaming' in item.capabilities)
        capabilities.setdefault('interrupts', 'interrupts' in item.capabilities)
        return capabilities
