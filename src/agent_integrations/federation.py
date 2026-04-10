
from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlparse

import httpx

from agent_common.models import FederationAuthType, RunStatus, ToolSpec
from agent_common.tools import ToolRegistry
from agent_common.version import runtime_version
from agent_config.app import FederationConfig, FederationExportConfig, FederationRemoteConfig
from agent_integrations.federation_security import (
    build_auth_hint_payload,
    build_callback_headers,
    build_callback_jws,
    build_mtls_client_kwargs,
    build_security_scheme_payload,
    build_server_jwks,
    sign_server_token,
    validate_callback_url,
    verify_jwt,
)
from agent_integrations.federation_utils import (
    iso_now as _iso_now,
)
from agent_integrations.federation_utils import (
    join_url as _join_url,
)
from agent_integrations.federation_utils import (
    paginate_events_payload as _paginate_events_payload,
)
from agent_integrations.federation_utils import (
    paginate_tasks_payload as _paginate_tasks_payload,
)
from agent_integrations.federation_utils import (
    pagination_params as _pagination_params,
)
from agent_integrations.federation_utils import (
    safe_json as _safe_json,
)
from agent_integrations.federation_utils import (
    site_origin as _site_origin,
)
from agent_integrations.storage import SQLiteRunStore

TERMINAL_TASK_STATUSES = {
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.WAITING_APPROVAL.value,
    'cancelled',
}


class FederationClientManager:
    def __init__(self, config: FederationConfig, store: SQLiteRunStore | None = None) -> None:
        self.config = config
        self.store = store
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._started = False
        self._remote_cards: dict[str, dict[str, Any]] = {}
        self._remote_bases: dict[str, str] = {}
        self._remote_push_paths: dict[str, str] = {}
        self._federation_auth_state: dict[str, dict[str, Any]] = {}
        self._oauth_redirect_handler: Any = None
        self._oauth_callback_handler: Any = None

    async def start(self) -> None:
        if self._started:
            return
        for remote in self.config.remotes:
            if self.store is not None:
                persisted = self.store.load_federation_auth_state(remote.name)
                if persisted is not None:
                    self._federation_auth_state[remote.name] = persisted
            self._clients[remote.name] = httpx.AsyncClient(
                base_url=remote.base_url.rstrip('/'),
                timeout=remote.timeout_seconds,
                headers=dict(remote.headers),
                trust_env=False,
                **build_mtls_client_kwargs(remote.auth.mtls),
            )
        self._started = True

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients = {}
        self._started = False

    def set_oauth_handlers(self, redirect_handler: Any, callback_handler: Any) -> None:
        self._oauth_redirect_handler = redirect_handler
        self._oauth_callback_handler = callback_handler

    def auth_status(self, remote_name: str) -> dict[str, Any]:
        remote = self._remote(remote_name)
        state = self._auth_state(remote_name)
        tokens = cast(dict[str, Any], state.get('tokens') or {})
        return {
            'remote': remote_name,
            'type': remote.auth.type.value,
            'has_env_header': bool(remote.auth.header_env),
            'has_env_token': bool(remote.auth.token_env),
            'grant_type': remote.auth.oauth.grant_type,
            'authenticated': bool(tokens.get('access_token') or remote.auth.token_env or remote.auth.header_env),
            'expires_at': tokens.get('expires_at'),
            'scope': tokens.get('scope'),
            'has_refresh_token': bool(tokens.get('refresh_token')),
        }

    async def authorize(self, remote_name: str) -> dict[str, Any]:
        remote = self._remote(remote_name)
        if remote.auth.type not in {FederationAuthType.OAUTH, FederationAuthType.OIDC}:
            raise RuntimeError(f'remote {remote_name} is not configured for federation OAuth/OIDC')
        if remote.auth.token_env or remote.auth.header_env:
            await self._prepare_client_auth(remote_name)
            return self.auth_status(remote_name)
        metadata = await self._authorization_metadata(remote_name, refresh=True)
        if remote.auth.oauth.grant_type == 'client_credentials':
            tokens = await self._exchange_client_credentials(remote_name, metadata)
        else:
            tokens = await self._interactive_authorization_code(remote_name, metadata)
        self._save_auth_state(remote_name, tokens=tokens, metadata=metadata)
        await self._prepare_client_auth(remote_name)
        return self.auth_status(remote_name)

    async def refresh_authorization(self, remote_name: str) -> dict[str, Any]:
        metadata = await self._authorization_metadata(remote_name, refresh=True)
        tokens = await self._refresh_access_token(remote_name, metadata)
        self._save_auth_state(remote_name, tokens=tokens, metadata=metadata)
        await self._prepare_client_auth(remote_name)
        return self.auth_status(remote_name)

    async def logout(self, remote_name: str) -> None:
        self._federation_auth_state.pop(remote_name, None)
        if self.store is not None:
            self.store.clear_federation_auth_state(remote_name)
        client = self._client(remote_name)
        auth = self._remote(remote_name).auth
        client.headers.pop(auth.header_name, None)

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
                'auth_type': remote.auth.type.value,
            }
            for remote in self.config.remotes
        ]

    def _auth_state(self, remote_name: str) -> dict[str, Any]:
        return self._federation_auth_state.setdefault(remote_name, {'tokens': None, 'metadata': None, 'jwks': None, 'pkce': None})

    def _save_auth_state(
        self,
        remote_name: str,
        *,
        tokens: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        jwks: dict[str, Any] | None = None,
        pkce: dict[str, Any] | None = None,
    ) -> None:
        state = self._auth_state(remote_name)
        if tokens is not None:
            state['tokens'] = tokens
        if metadata is not None:
            state['metadata'] = metadata
        if jwks is not None:
            state['jwks'] = jwks
        if pkce is not None:
            state['pkce'] = pkce
        if self.store is not None:
            self.store.save_federation_auth_state(
                remote_name,
                tokens=cast(dict[str, Any] | None, state.get('tokens')),
                metadata=cast(dict[str, Any] | None, state.get('metadata')),
                jwks=cast(dict[str, Any] | None, state.get('jwks')),
                pkce=cast(dict[str, Any] | None, state.get('pkce')),
            )

    @staticmethod
    def _token_expired(tokens: dict[str, Any] | None, *, skew_seconds: int) -> bool:
        if not tokens:
            return True
        expires_at = tokens.get('expires_at')
        if expires_at is None:
            return False
        return float(expires_at) <= (time.time() + max(0, skew_seconds))

    def _resolve_client_id(self, remote: FederationRemoteConfig) -> str | None:
        if remote.auth.oauth.client_id:
            return remote.auth.oauth.client_id
        env_name = remote.auth.oauth.client_id_env
        if env_name:
            value = os.environ.get(env_name, '').strip()
            if value:
                return value
        return None

    def _resolve_client_secret(self, remote: FederationRemoteConfig) -> str | None:
        if remote.auth.oauth.client_secret:
            return remote.auth.oauth.client_secret
        env_name = remote.auth.oauth.client_secret_env
        if env_name:
            value = os.environ.get(env_name, '').strip()
            if value:
                return value
        return None

    async def _prepare_client_auth(self, remote_name: str) -> None:
        remote = self._remote(remote_name)
        client = self._client(remote_name)
        auth = remote.auth
        client.headers.update(dict(remote.headers))
        client.headers.pop(auth.header_name, None)
        if auth.type in {FederationAuthType.BEARER_ENV, FederationAuthType.OAUTH, FederationAuthType.OIDC} and auth.token_env:
            token = os.environ.get(auth.token_env, '').strip()
            if token:
                client.headers[auth.header_name] = f'{auth.value_prefix}{token}'
                return
        if auth.type in {FederationAuthType.HEADER_ENV, FederationAuthType.OAUTH, FederationAuthType.OIDC} and auth.header_env:
            raw = os.environ.get(auth.header_env, '').strip()
            if raw:
                client.headers[auth.header_name] = raw
                return
        if auth.type not in {FederationAuthType.OAUTH, FederationAuthType.OIDC}:
            return
        state = self._auth_state(remote_name)
        tokens = cast(dict[str, Any] | None, state.get('tokens'))
        if self._token_expired(tokens, skew_seconds=auth.oauth.token_refresh_skew_seconds):
            metadata = await self._authorization_metadata(remote_name)
            tokens = await self._refresh_access_token(remote_name, metadata)
            self._save_auth_state(remote_name, tokens=tokens, metadata=metadata)
        access_token = str((tokens or {}).get('access_token') or '').strip()
        if not access_token:
            raise RuntimeError(f'remote {remote_name} does not have an access token; run federation auth login first')
        client.headers[auth.header_name] = f'{auth.value_prefix}{access_token}'

    @asynccontextmanager
    async def _stream_request(self, remote_name: str, method: str, url: str, **kwargs: Any) -> Any:
        await self._prepare_client_auth(remote_name)
        async with self._client(remote_name).stream(method, url, **kwargs) as response:
            yield response

    async def _request(self, remote_name: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
        await self._prepare_client_auth(remote_name)
        return await self._client(remote_name).request(method, url, **kwargs)

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
        await self._ensure_remote_ready(remote_name)
        response = await self._request(
            remote_name,
            'POST',
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
        await self._ensure_remote_ready(remote_name)
        payload = {
            'target': target,
            'input': input_text,
            'session_id': session_id,
            'metadata': metadata or {},
        }
        events: list[dict[str, Any]] = []
        for path in ('/message:stream', '/tasks/send-stream'):
            try:
                async with self._stream_request(remote_name, 'POST', _join_url(self._base_path(remote_name), path), json=payload) as response:
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
        await self._ensure_remote_ready(remote_name)
        response = await self._request(remote_name, 'GET', _join_url(self._base_path(remote_name), f'/tasks/{task_id}'))
        payload = _safe_json(response)
        return cast(dict[str, Any], payload['task'])

    async def list_tasks(
        self,
        remote_name: str,
        *,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        await self._ensure_remote_ready(remote_name)
        response = await self._request(
            remote_name,
            'GET',
            _join_url(self._base_path(remote_name), '/tasks'),
            params=_pagination_params(page_token, page_size),
        )
        payload = _safe_json(response)
        return {
            'tasks': cast(list[dict[str, Any]], payload.get('tasks', [])),
            'nextPageToken': cast(str | None, payload.get('nextPageToken')),
        }

    async def list_task_events(
        self,
        remote_name: str,
        task_id: str,
        after_sequence: int = 0,
        *,
        page_token: str | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        await self._ensure_remote_ready(remote_name)
        params = _pagination_params(page_token, page_size)
        if not page_token and after_sequence > 0:
            params['after_sequence'] = after_sequence
        response = await self._request(
            remote_name,
            'GET',
            _join_url(self._base_path(remote_name), f'/tasks/{task_id}/events'),
            params=params,
        )
        payload = _safe_json(response)
        return {
            'events': cast(list[dict[str, Any]], payload.get('events', [])),
            'nextPageToken': cast(str | None, payload.get('nextPageToken')),
        }

    async def stream_task_events(self, remote_name: str, task_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        latest_task = await self.get_task(remote_name, task_id)
        if str(latest_task['status']) in TERMINAL_TASK_STATUSES:
            terminal_payload = await self.list_task_events(remote_name, task_id, after_sequence)
            terminal_events = cast(list[dict[str, Any]], terminal_payload.get('events', []))
            for event in terminal_events:
                event.setdefault('event_name', str(event.get('event_kind', 'task_event')))
            return terminal_events
        events: list[dict[str, Any]] = []
        current_after = after_sequence
        reconnect_budget = 3
        while reconnect_budget > 0:
            reconnect_budget -= 1
            try:
                async with self._stream_request(
                    remote_name,
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
        backlog_payload = await self.list_task_events(remote_name, task_id, current_after)
        backlog = cast(list[dict[str, Any]], backlog_payload.get('events', []))
        for event in backlog:
            event.setdefault('event_name', str(event.get('event_kind', 'task_event')))
            events.append(event)
        return events

    async def cancel_task(self, remote_name: str, task_id: str) -> dict[str, Any]:
        await self._ensure_remote_ready(remote_name)
        for path in (f'/tasks/{task_id}:cancel', f'/tasks/{task_id}/cancel'):
            response = await self._request(remote_name, 'POST', _join_url(self._base_path(remote_name), path), json={})
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
        await self._ensure_remote_ready(remote_name)
        payload = {'callback_url': callback_url, 'lease_seconds': lease_seconds, 'from_sequence': from_sequence}
        for path in (f'/tasks/{task_id}:subscribe', f'/tasks/{task_id}/subscribe'):
            response = await self._request(remote_name, 'POST', _join_url(self._base_path(remote_name), path), json=payload)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            return cast(dict[str, Any], body.get('push_notification_config') or body.get('subscription', {}))
        raise RuntimeError(f'remote task subscribe route not found for {remote_name}')

    async def list_subscriptions(self, remote_name: str, task_id: str) -> list[dict[str, Any]]:
        await self._ensure_remote_ready(remote_name)
        response = await self._request(remote_name, 'GET', _join_url(self._base_path(remote_name), f'/tasks/{task_id}/subscriptions'))
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
        await self._ensure_remote_ready(remote_name)
        response = await self._request(
            remote_name,
            'POST',
            _join_url(self._base_path(remote_name), f'/tasks/{task_id}/subscriptions/{subscription_id}/renew'),
            json={'lease_seconds': lease_seconds},
        )
        payload = _safe_json(response)
        return cast(dict[str, Any], payload['subscription'])

    async def cancel_subscription(self, remote_name: str, task_id: str, subscription_id: str) -> dict[str, Any]:
        await self._ensure_remote_ready(remote_name)
        response = await self._request(
            remote_name,
            'POST',
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
        await self._ensure_remote_ready(remote_name)
        payload = {'callback_url': callback_url, 'lease_seconds': lease_seconds, 'from_sequence': from_sequence}
        candidates = (
            ('POST', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfigs')),
            ('POST', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfig/set')),
        )
        for method, path in candidates:
            response = await self._request(remote_name, method, path, json=payload)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            return cast(dict[str, Any], body.get('push_notification_config') or body.get('subscription', {}))
        raise RuntimeError(f'push notification set route not found for {remote_name}')

    async def get_push_notification(self, remote_name: str, task_id: str, config_id: str) -> dict[str, Any]:
        await self._ensure_remote_ready(remote_name)
        candidates = (
            ('GET', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfigs/{config_id}'), None),
            ('GET', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfig/get'), {'config_id': config_id}),
        )
        for method, path, params in candidates:
            response = await self._request(remote_name, method, path, params=params)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            return cast(dict[str, Any], body.get('push_notification_config') or body.get('subscription', {}))
        raise RuntimeError(f'push notification get route not found for {remote_name}')

    async def list_push_notifications(self, remote_name: str, task_id: str) -> list[dict[str, Any]]:
        await self._ensure_remote_ready(remote_name)
        candidates = (
            _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfigs'),
            _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfig/list'),
        )
        for path in candidates:
            response = await self._request(remote_name, 'GET', path)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            return cast(list[dict[str, Any]], body.get('push_notification_configs') or body.get('subscriptions', []))
        raise RuntimeError(f'push notification list route not found for {remote_name}')

    async def delete_push_notification(self, remote_name: str, task_id: str, config_id: str) -> dict[str, Any]:
        await self._ensure_remote_ready(remote_name)
        candidates = (
            ('DELETE', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfigs/{config_id}'), None),
            ('POST', _join_url(self._push_path(remote_name), f'/tasks/{task_id}/pushNotificationConfig/delete'), {'config_id': config_id}),
        )
        for method, path, payload in candidates:
            response = await self._request(remote_name, method, path, json=payload)
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
        await self._ensure_remote_ready(remote_name)
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
            response = await self._request(remote_name, 'POST', path, json=payload)
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
        await self._ensure_remote_ready(remote_name)
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
            response = await self._request(remote_name, 'POST', path, json=payload)
            if response.status_code == HTTPStatus.NOT_FOUND:
                continue
            body = _safe_json(response)
            task = cast(dict[str, Any], body.get('task') or await self.get_task(remote_name, task_id))
            event_payload = body if 'events' in body else await self.list_task_events(remote_name, task_id, from_sequence)
            events = cast(list[dict[str, Any]], event_payload.get('events', []))
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
                async with self._stream_request(
                    remote_name,
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

    async def _authorization_metadata(self, remote_name: str, *, refresh: bool = False) -> dict[str, Any]:
        remote = self._remote(remote_name)
        state = self._auth_state(remote_name)
        cached = cast(dict[str, Any] | None, state.get('metadata'))
        if cached and not refresh:
            return cached
        oauth = remote.auth.oauth
        metadata: dict[str, Any] = {}
        metadata_url = oauth.metadata_url or oauth.openid_config_url
        if metadata_url:
            async with httpx.AsyncClient(timeout=remote.timeout_seconds, trust_env=False) as client:
                response = await client.get(metadata_url)
                metadata = _safe_json(response)
        if oauth.issuer_url:
            metadata.setdefault('issuer', oauth.issuer_url)
        if oauth.authorization_url:
            metadata.setdefault('authorization_endpoint', oauth.authorization_url)
        if oauth.token_url:
            metadata.setdefault('token_endpoint', oauth.token_url)
        if oauth.jwks_url:
            metadata.setdefault('jwks_uri', oauth.jwks_url)
        self._save_auth_state(remote_name, metadata=metadata)
        return metadata

    async def _exchange_client_credentials(self, remote_name: str, metadata: dict[str, Any]) -> dict[str, Any]:
        remote = self._remote(remote_name)
        token_url = str(metadata.get('token_endpoint') or remote.auth.oauth.token_url or '').strip()
        if not token_url:
            raise RuntimeError(f'remote {remote_name} does not publish a token endpoint')
        client_id = self._resolve_client_id(remote)
        client_secret = self._resolve_client_secret(remote)
        if not client_id or not client_secret:
            raise RuntimeError(f'remote {remote_name} is missing client credentials')
        form = {
            'grant_type': 'client_credentials',
            'client_id': client_id,
            'client_secret': client_secret,
        }
        if remote.auth.oauth.scopes:
            form['scope'] = ' '.join(remote.auth.oauth.scopes)
        if remote.auth.oauth.audience:
            form['audience'] = remote.auth.oauth.audience
        async with httpx.AsyncClient(timeout=remote.timeout_seconds, trust_env=False) as client:
            response = await client.post(token_url, data=form)
            payload = _safe_json(response)
        return self._normalize_token_payload(payload)

    async def _interactive_authorization_code(self, remote_name: str, metadata: dict[str, Any]) -> dict[str, Any]:
        remote = self._remote(remote_name)
        authorization_url = str(metadata.get('authorization_endpoint') or remote.auth.oauth.authorization_url or '').strip()
        token_url = str(metadata.get('token_endpoint') or remote.auth.oauth.token_url or '').strip()
        if not authorization_url or not token_url:
            raise RuntimeError(f'remote {remote_name} does not publish authorization/token endpoints')
        if self._oauth_redirect_handler is None or self._oauth_callback_handler is None:
            raise RuntimeError('federation OAuth authorization-code flow requires configured redirect/callback handlers')
        client_id = self._resolve_client_id(remote)
        client_secret = self._resolve_client_secret(remote)
        if not client_id:
            raise RuntimeError(f'remote {remote_name} is missing client_id')
        state_token = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        params = {
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': remote.auth.oauth.redirect_uri,
            'state': state_token,
        }
        if remote.auth.oauth.scopes:
            params['scope'] = ' '.join(remote.auth.oauth.scopes)
        if remote.auth.oauth.audience:
            params['audience'] = remote.auth.oauth.audience
        if remote.auth.oauth.use_pkce:
            challenge = secrets.token_urlsafe(1)
            del challenge
            import base64
            import hashlib

            code_challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('utf-8')).digest()).decode('ascii').rstrip('=')
            params['code_challenge'] = code_challenge
            params['code_challenge_method'] = 'S256'
        auth_url = f'{authorization_url}?{urllib_parse.urlencode(params)}'
        await self._oauth_redirect_handler(auth_url)
        code, returned_state = await self._oauth_callback_handler()
        if returned_state and returned_state != state_token:
            raise RuntimeError('federation OAuth state mismatch')
        form = {
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': client_id,
            'redirect_uri': remote.auth.oauth.redirect_uri,
        }
        if client_secret:
            form['client_secret'] = client_secret
        if remote.auth.oauth.use_pkce:
            form['code_verifier'] = verifier
        async with httpx.AsyncClient(timeout=remote.timeout_seconds, trust_env=False) as client:
            response = await client.post(token_url, data=form)
            payload = _safe_json(response)
        return self._normalize_token_payload(payload)

    async def _refresh_access_token(self, remote_name: str, metadata: dict[str, Any]) -> dict[str, Any]:
        remote = self._remote(remote_name)
        state = self._auth_state(remote_name)
        current = cast(dict[str, Any] | None, state.get('tokens')) or {}
        refresh_token = str(current.get('refresh_token') or '').strip()
        if refresh_token:
            token_url = str(metadata.get('token_endpoint') or remote.auth.oauth.token_url or '').strip()
            if not token_url:
                raise RuntimeError(f'remote {remote_name} does not publish a token endpoint')
            form = {
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
                'client_id': self._resolve_client_id(remote) or '',
            }
            client_secret = self._resolve_client_secret(remote)
            if client_secret:
                form['client_secret'] = client_secret
            async with httpx.AsyncClient(timeout=remote.timeout_seconds, trust_env=False) as client:
                response = await client.post(token_url, data=form)
                refreshed = _safe_json(response)
            if 'refresh_token' not in refreshed:
                refreshed['refresh_token'] = refresh_token
            return self._normalize_token_payload(refreshed)
        if remote.auth.oauth.grant_type == 'client_credentials':
            return await self._exchange_client_credentials(remote_name, metadata)
        raise RuntimeError(f'remote {remote_name} requires interactive re-authentication')

    @staticmethod
    def _normalize_token_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        expires_in = payload.get('expires_in')
        if expires_in is not None:
            normalized['expires_at'] = time.time() + float(expires_in)
        return normalized

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
        details = {'card': card, 'extended_card': extended_card, 'discovery_url': remote.discovery_url}
        await self._verify_signed_card(remote_name, details)
        self._remote_cards[remote_name] = details
        self._remote_bases[remote_name] = base_url.rstrip('/')
        self._remote_push_paths[remote_name] = base_url.rstrip('/')

    async def _verify_signed_card(self, remote_name: str, details: dict[str, Any]) -> None:
        card = cast(dict[str, Any], details.get('card', {}))
        signed_card = str(card.get('signed_card') or card.get('signedCard') or '').strip()
        if not signed_card:
            return
        remote = self._remote(remote_name)
        metadata = await self._authorization_metadata(remote_name)
        jwks_url = str(
            metadata.get('jwks_uri')
            or remote.auth.oauth.jwks_url
            or card.get('jwks_url')
            or card.get('jwksUrl')
            or ''
        ).strip()
        if not jwks_url:
            raise RuntimeError(f'remote {remote_name} published a signed card without JWKS metadata')
        async with httpx.AsyncClient(timeout=remote.timeout_seconds, trust_env=False) as client:
            response = await client.get(jwks_url)
            jwks = _safe_json(response)
        claims = verify_jwt(
            signed_card,
            jwks=jwks,
            audience=remote.auth.oauth.audience,
            issuer=remote.auth.oauth.issuer_url or metadata.get('issuer'),
            allowed_algorithms=remote.auth.oauth.allowed_algorithms,
            leeway_seconds=30,
        )
        signed_payload = cast(dict[str, Any], claims.get('card', {}))
        if signed_payload and signed_payload.get('url') != card.get('url'):
            raise RuntimeError(f'remote {remote_name} published a signed card that does not match the discovered card payload')
        self._save_auth_state(remote_name, jwks=jwks)

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

    async def _ensure_remote_ready(self, remote_name: str) -> None:
        await self._ensure_remote_metadata(remote_name)
        self._validate_remote_security(remote_name)
        await self._prepare_client_auth(remote_name)

    def _validate_remote_security(self, remote_name: str) -> None:
        details = self._remote_cards[remote_name]
        requirements = self._remote_security_requirements(details)
        if not requirements:
            return
        schemes = self._remote_security_schemes(details)
        if not schemes:
            raise RuntimeError(f'remote {remote_name} requires federation security but did not publish any security schemes')
        remote = self._remote(remote_name)
        for requirement in requirements:
            if self._security_requirement_satisfied(remote, schemes, requirement):
                return
        raise RuntimeError(f'remote {remote_name} requires unsupported federation auth; inspect the published securitySchemes/security metadata first')

    @staticmethod
    def _remote_security_schemes(details: dict[str, Any]) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for card_key in ('card', 'extended_card'):
            payload = cast(dict[str, Any], details.get(card_key, {}))
            for key in ('securitySchemes', 'security_schemes'):
                published = payload.get(key)
                if isinstance(published, dict):
                    for name, scheme in published.items():
                        if isinstance(name, str) and isinstance(scheme, dict):
                            merged[name] = dict(cast(dict[str, Any], scheme))
        return merged

    @staticmethod
    def _remote_security_requirements(details: dict[str, Any]) -> list[dict[str, list[str]]]:
        for card_key in ('card', 'extended_card'):
            payload = cast(dict[str, Any], details.get(card_key, {}))
            published = payload.get('security')
            if isinstance(published, dict):
                return [
                    {
                        str(name): [str(item) for item in value] if isinstance(value, list) else []
                        for name, value in published.items()
                    }
                ]
            if isinstance(published, list):
                requirements: list[dict[str, list[str]]] = []
                for item in published:
                    if not isinstance(item, dict):
                        continue
                    requirements.append(
                        {
                            str(name): [str(scope) for scope in value] if isinstance(value, list) else []
                            for name, value in item.items()
                        }
                    )
                if requirements:
                    return requirements
        return []

    def _security_requirement_satisfied(
        self,
        remote: FederationRemoteConfig,
        schemes: dict[str, dict[str, Any]],
        requirement: dict[str, list[str]],
    ) -> bool:
        for scheme_name in requirement:
            scheme = schemes.get(scheme_name)
            if scheme is None or not self._supports_security_scheme(remote, scheme):
                return False
        return True

    @staticmethod
    def _supports_security_scheme(remote: FederationRemoteConfig, scheme: dict[str, Any]) -> bool:
        scheme_type = str(scheme.get('type') or '').strip()
        auth = remote.auth
        if scheme_type == 'noAuth':
            return auth.type is FederationAuthType.NONE
        if scheme_type == 'mutualTLS':
            return auth.mtls.enabled
        if scheme_type == 'http' and str(scheme.get('scheme') or '').strip().lower() == 'bearer':
            return auth.type in {FederationAuthType.BEARER_ENV, FederationAuthType.OAUTH, FederationAuthType.OIDC} and bool(
                auth.token_env or auth.header_env or remote.auth.oauth.client_id or remote.auth.oauth.client_id_env
            )
        if scheme_type == 'apiKey' and str(scheme.get('in') or '').strip().lower() == 'header':
            expected_header = str(scheme.get('name') or auth.header_name)
            return auth.type in {FederationAuthType.HEADER_ENV, FederationAuthType.BEARER_ENV} and auth.header_name == expected_header and bool(
                auth.header_env or auth.token_env
            )
        if scheme_type in {'oauth2', 'openIdConnect'}:
            if auth.type not in {FederationAuthType.OAUTH, FederationAuthType.OIDC}:
                return False
            if not (auth.token_env or auth.header_env or remote.auth.oauth.client_id or remote.auth.oauth.client_id_env):
                return False
            audience = str(scheme.get('x-audience') or '').strip()
            return not audience or not auth.oauth.audience or auth.oauth.audience == audience
        return False


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
            default_input_modes = item.default_input_modes or item.input_modes
            default_output_modes = item.default_output_modes or item.output_modes
            notification_compatibility = self._notification_compatibility(item)
            exports.append(
                {
                    'name': item.name,
                    'description': item.description,
                    'target_type': item.target_type,
                    'tags': item.tags,
                    'input_modes': item.input_modes,
                    'output_modes': item.output_modes,
                    'inputModes': item.input_modes,
                    'outputModes': item.output_modes,
                    'default_input_modes': default_input_modes,
                    'default_output_modes': default_output_modes,
                    'defaultInputModes': default_input_modes,
                    'defaultOutputModes': default_output_modes,
                    'modalities': item.modalities,
                    'artifacts': item.artifacts,
                    'parts': item.parts,
                    'notification_compatibility': notification_compatibility,
                    'notificationCompatibility': notification_compatibility,
                    'capabilities': self._export_capabilities(item),
                }
            )
        security_schemes = self._security_schemes_payload()
        security_requirements = [dict(item) for item in self.config.server.security_requirements]
        auth_hints = self._auth_hints_payload()
        notification_compatibility = self._notification_compatibility()
        payload = {
            'name': 'easy-agent-federation',
            'description': 'A2A-style export surface for local easy-agent targets.',
            'url': public_url,
            'public_base_url': public_url,
            'agent_endpoint': public_url,
            'well_known_url': _join_url(origin, self.config.server.well_known_path),
            'legacy_well_known_url': _join_url(origin, self.config.server.legacy_well_known_path),
            'jwks_url': _join_url(origin, self.config.server.jwt.jwks_path),
            'version': runtime_version(),
            'protocol_version': self.config.server.protocol_version,
            'card_schema_version': self.config.server.card_schema_version,
            'default_input_modes': ['text'],
            'default_output_modes': ['text'],
            'defaultInputModes': ['text'],
            'defaultOutputModes': ['text'],
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
            'auth_hints': auth_hints,
            'securitySchemes': security_schemes,
            'security_schemes': security_schemes,
            'security': security_requirements,
            'notification_compatibility': notification_compatibility,
            'notificationCompatibility': notification_compatibility,
            'compatibility': {
                'runtime': 'easy-agent',
                'runtime_version': runtime_version(),
                'minimum_card_schema_version': self.config.server.card_schema_version,
                'supported_interfaces': ['well-known', 'a2a-http', 'a2a-legacy-http'],
            },
            'exports': exports,
        }
        if self.config.server.jwt.enabled:
            payload['signed_card'] = sign_server_token(self.config.server.jwt, {'card': payload})
        return payload

    def extended_agent_card(self) -> dict[str, Any]:
        payload = {
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
                'pagination': {
                    'pageToken': True,
                    'pageSize': True,
                    'nextPageToken': True,
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
                'list_tasks': _join_url(self.public_base_url(), '/tasks'),
                'list_task_events': _join_url(self.public_base_url(), '/tasks/{task_id}/events'),
                'send_subscribe': _join_url(self.public_base_url(), '/tasks/sendSubscribe'),
                'resubscribe': _join_url(self.public_base_url(), '/tasks/resubscribe'),
                'push_notification_configs': _join_url(self.public_base_url(), '/tasks/{task_id}/pushNotificationConfigs'),
            },
        }
        if self.config.server.jwt.enabled:
            payload['signed_card'] = sign_server_token(self.config.server.jwt, {'card': payload})
        return payload

    def jwks_payload(self) -> dict[str, Any]:
        return build_server_jwks(self.config.server.jwt)

    def _authorization_context(self, headers: dict[str, str]) -> dict[str, Any]:
        jwt_config = self.config.server.jwt
        requirements = self.config.server.security_requirements
        if not requirements:
            return {'tenant_id': None, 'subject_id': None, 'task_scope': []}
        normalized = {str(key).lower(): value for key, value in headers.items()}
        bearer = normalized.get('authorization', '')
        if bearer.lower().startswith('bearer '):
            token = bearer.split(' ', 1)[1].strip()
        else:
            token = ''
        if not token:
            raise RuntimeError('missing bearer token for federated request')
        if not jwt_config.enabled:
            raise RuntimeError('server JWT auth is not enabled')
        claims = verify_jwt(
            token,
            jwks=self.jwks_payload(),
            audience=jwt_config.audience,
            issuer=jwt_config.issuer,
            allowed_algorithms=jwt_config.allowed_algorithms,
            leeway_seconds=jwt_config.leeway_seconds,
        )
        task_scope = claims.get(jwt_config.task_scope_claim) or []
        if isinstance(task_scope, str):
            task_scope = [task_scope]
        return {
            'tenant_id': str(claims.get(jwt_config.tenant_claim) or '') or None,
            'subject_id': str(claims.get(jwt_config.subject_claim) or '') or None,
            'task_scope': [str(item) for item in task_scope] if isinstance(task_scope, list) else [],
            'claims': claims,
        }

    @staticmethod
    def _is_task_visible(task: dict[str, Any], auth_context: dict[str, Any] | None) -> bool:
        if auth_context is None:
            return True
        tenant_id = auth_context.get('tenant_id')
        if tenant_id and task.get('tenant_id') and task.get('tenant_id') != tenant_id:
            return False
        task_scope = cast(list[str], auth_context.get('task_scope') or [])
        return not task_scope or str(task.get('task_id')) in task_scope

    def _require_task_access(self, task_id: str, auth_context: dict[str, Any] | None) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not self._is_task_visible(task, auth_context):
            raise RuntimeError('federated task access denied by tenant/task scope policy')
        return task

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

            def _auth_context(self) -> dict[str, Any] | None:
                try:
                    return server._authorization_context({key: value for key, value in self.headers.items()})
                except RuntimeError as exc:
                    self._write({'error': 'unauthorized', 'detail': str(exc)}, status=401)
                    return None

            def _server_call(self, callback: Any) -> Any:
                try:
                    return callback()
                except RuntimeError as exc:
                    self._write({'error': 'forbidden', 'detail': str(exc)}, status=403)
                    return None

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
                if path == server.config.server.jwt.jwks_path.rstrip('/'):
                    self._write(server.jwks_payload())
                    return
                if path in {f'{base}/agent-card', f'{base}/agentCard'}:
                    self._write(server.agent_card())
                    return
                if path in {f'{base}/agent-card/extended', f'{base}/extendedAgentCard'}:
                    self._write(server.extended_agent_card())
                    return
                if path == f'{base}/tasks':
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    try:
                        page_token = str(query.get('pageToken', [''])[0] or '').strip() or None
                        page_size = int(query.get('pageSize', ['0'])[0]) if query.get('pageSize') else None
                        tasks = self._server_call(lambda: server.list_tasks(auth_context))
                        if tasks is None:
                            return
                        self._write(_paginate_tasks_payload(tasks, page_token, page_size))
                    except ValueError as exc:
                        self._write({'error': 'invalid_page_token', 'detail': str(exc)}, status=400)
                    return
                if path.endswith('/events/stream') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-3]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    after_sequence = int(query.get('after_sequence', ['0'])[0])
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('Connection', 'keep-alive')
                    self.end_headers()
                    while True:
                        events = self._server_call(
                            lambda after_sequence=after_sequence: server.list_task_events(task_id, after_sequence, auth_context)
                        )
                        if events is None:
                            return
                        for event in events:
                            payload = {'event': event, 'task': event['task']}
                            self._write_sse(str(event['event_kind']), payload)
                            after_sequence = int(event['sequence'])
                        task = self._server_call(lambda: server.get_task(task_id, auth_context))
                        if task is None:
                            return
                        remaining = self._server_call(
                            lambda after_sequence=after_sequence: server.list_task_events(task_id, after_sequence, auth_context)
                        )
                        if remaining is None:
                            return
                        if str(task['status']) in TERMINAL_TASK_STATUSES and not remaining:
                            break
                        time.sleep(0.1)
                    return
                if path.endswith('/events') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    try:
                        page_token = str(query.get('pageToken', [''])[0] or '').strip() or None
                        page_size = int(query.get('pageSize', ['0'])[0]) if query.get('pageSize') else None
                        after_sequence = 0 if page_token else int(query.get('after_sequence', ['0'])[0])
                        events = self._server_call(lambda: server.list_task_events(task_id, after_sequence, auth_context))
                        if events is None:
                            return
                        self._write(_paginate_events_payload(events, page_token, page_size))
                    except ValueError as exc:
                        self._write({'error': 'invalid_page_token', 'detail': str(exc)}, status=400)
                    return
                if path.endswith('/subscriptions') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    subscriptions = self._server_call(lambda: server.list_subscriptions(task_id, auth_context))
                    if subscriptions is None:
                        return
                    self._write({'subscriptions': subscriptions})
                    return
                if path.endswith('/pushNotificationConfig/get') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-3]
                    config_id = str(query.get('config_id', [''])[0])
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    config_payload = self._server_call(lambda: server.get_push_notification(task_id, config_id, auth_context))
                    if config_payload is None:
                        return
                    self._write({'push_notification_config': config_payload})
                    return
                if path.endswith('/pushNotificationConfig/list') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-3]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    configs = self._server_call(lambda: server.list_push_notifications(task_id, auth_context))
                    if configs is None:
                        return
                    self._write({'push_notification_configs': configs, 'subscriptions': configs})
                    return
                if '/pushNotificationConfigs/' in path and path.startswith(f'{base}/tasks/'):
                    parts = path.split('/')
                    task_id = parts[-3]
                    config_id = parts[-1]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    config_payload = self._server_call(lambda: server.get_push_notification(task_id, config_id, auth_context))
                    if config_payload is None:
                        return
                    self._write({'push_notification_config': config_payload})
                    return
                if path.endswith('/pushNotificationConfigs') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    configs = self._server_call(lambda: server.list_push_notifications(task_id, auth_context))
                    if configs is None:
                        return
                    self._write({'push_notification_configs': configs, 'subscriptions': configs})
                    return
                if path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-1]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    task = self._server_call(lambda: server.get_task(task_id, auth_context))
                    if task is None:
                        return
                    self._write({'task': task})
                    return
                self._write({'error': 'not_found'}, status=404)

            def do_DELETE(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip('/')
                base = server.config.server.base_path.rstrip('/')
                if '/pushNotificationConfigs/' in path and path.startswith(f'{base}/tasks/'):
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    parts = path.split('/')
                    task_id = parts[-3]
                    config_id = parts[-1]
                    deleted = server.delete_push_notification(task_id, config_id, auth_context)
                    self._write({'push_notification_config': deleted, 'subscription': deleted})
                    return
                self._write({'error': 'not_found'}, status=404)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip('/')
                base = server.config.server.base_path.rstrip('/')
                payload = self._json()
                if path in {f'{base}/tasks/send', f'{base}/message:send'}:
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    task = server.start_task(
                        str(payload.get('target', '')),
                        str(payload.get('input', '')),
                        session_id=cast(str | None, payload.get('session_id')),
                        metadata=dict(cast(dict[str, Any], payload.get('metadata', {}))),
                        auth_context=auth_context,
                    )
                    callback_url = str(payload.get('callback_url', '')).strip()
                    if callback_url:
                        subscription = server.set_push_notification(
                            str(task['task_id']),
                            callback_url,
                            lease_seconds=cast(int | None, payload.get('lease_seconds')),
                            from_sequence=int(payload.get('from_sequence', 0) or 0),
                            auth_context=auth_context,
                        )
                        self._write({'task': task, 'push_notification_config': subscription, 'subscription': subscription}, status=202)
                        return
                    self._write({'task': task}, status=202)
                    return
                if path in {f'{base}/tasks/send-stream', f'{base}/message:stream'}:
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    task = server.start_task(
                        str(payload.get('target', '')),
                        str(payload.get('input', '')),
                        session_id=cast(str | None, payload.get('session_id')),
                        metadata=dict(cast(dict[str, Any], payload.get('metadata', {}))),
                        auth_context=auth_context,
                    )
                    task_id = str(task['task_id'])
                    after_sequence = 0
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
                    self.end_headers()
                    while True:
                        events = server.list_task_events(task_id, after_sequence, auth_context)
                        for event in events:
                            line = json.dumps({'event': event, 'task': event['task']}, ensure_ascii=False).encode() + b'\n'
                            self.wfile.write(line)
                            self.wfile.flush()
                            after_sequence = int(event['sequence'])
                        current = server.get_task(task_id, auth_context)
                        if str(current['status']) in TERMINAL_TASK_STATUSES and not server.list_task_events(task_id, after_sequence, auth_context):
                            break
                        time.sleep(0.1)
                    return
                if path == f'{base}/tasks/sendSubscribe':
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    response = server.send_subscribe(
                        str(payload.get('target', '')),
                        str(payload.get('input', '')),
                        str(payload.get('callback_url', '')).strip(),
                        session_id=cast(str | None, payload.get('session_id')),
                        metadata=dict(cast(dict[str, Any], payload.get('metadata', {}))),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                        auth_context=auth_context,
                    )
                    self._write(response, status=202)
                    return
                if path == f'{base}/tasks/resubscribe':
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    response = server.resubscribe_task(
                        str(payload.get('task_id', '')).strip(),
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                        callback_url=cast(str | None, payload.get('callback_url')),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        auth_context=auth_context,
                    )
                    self._write(response)
                    return
                if path.endswith(':cancel') and path.startswith(f'{base}/tasks/'):
                    task_id = path.rsplit('/tasks/', 1)[1].split(':', 1)[0]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    self._write({'task': server.cancel_task(task_id, auth_context)})
                    return
                if path.endswith('/cancel') and '/subscriptions/' not in path and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    self._write({'task': server.cancel_task(task_id, auth_context)})
                    return
                if path.endswith(':subscribe') and path.startswith(f'{base}/tasks/'):
                    task_id = path.rsplit('/tasks/', 1)[1].split(':', 1)[0]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    response = server.resubscribe_task(
                        task_id,
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                        callback_url=cast(str | None, payload.get('callback_url')),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        auth_context=auth_context,
                    )
                    self._write(response, status=202)
                    return
                if path.endswith('/subscribe') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    subscription = server.subscribe_task(
                        task_id,
                        str(payload.get('callback_url', '')),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                        auth_context=auth_context,
                    )
                    self._write({'task': server.get_task(task_id, auth_context), 'push_notification_config': subscription, 'subscription': subscription}, status=202)
                    return
                if path.endswith('/pushNotificationConfigs') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-2]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    subscription = server.set_push_notification(
                        task_id,
                        str(payload.get('callback_url', '')),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                        auth_context=auth_context,
                    )
                    self._write({'push_notification_config': subscription, 'subscription': subscription}, status=202)
                    return
                if path.endswith('/pushNotificationConfig/set') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-3]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    subscription = server.set_push_notification(
                        task_id,
                        str(payload.get('callback_url', '')),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                        auth_context=auth_context,
                    )
                    self._write({'push_notification_config': subscription, 'subscription': subscription}, status=202)
                    return
                if path.endswith('/pushNotificationConfig/delete') and path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-3]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    deleted = server.delete_push_notification(task_id, str(payload.get('config_id', '')), auth_context)
                    self._write({'push_notification_config': deleted, 'subscription': deleted})
                    return
                if '/subscriptions/' in path and path.endswith('/renew'):
                    parts = path.split('/')
                    task_id = parts[-4]
                    subscription_id = parts[-2]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    subscription = server.renew_subscription(
                        task_id,
                        subscription_id,
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        auth_context=auth_context,
                    )
                    self._write({'subscription': subscription})
                    return
                if '/subscriptions/' in path and path.endswith('/cancel'):
                    parts = path.split('/')
                    task_id = parts[-4]
                    subscription_id = parts[-2]
                    auth_context = self._auth_context()
                    if auth_context is None and server.config.server.security_requirements:
                        return
                    subscription = server.cancel_subscription(task_id, subscription_id, auth_context)
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
            'jwks_url': _join_url(_site_origin(self.public_base_url()), self.config.server.jwt.jwks_path),
            'version': runtime_version(),
            'push_delivery': ['polling', 'webhook_subscribe', 'sse_events'],
            'security_schemes': list(self._security_schemes_payload()),
            'security_requirements': [dict(item) for item in self.config.server.security_requirements],
            'notification_compatibility': self._notification_compatibility(),
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
        auth_context: dict[str, Any] | None = None,
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
            'tenant_id': auth_context.get('tenant_id') if auth_context else None,
            'subject_id': auth_context.get('subject_id') if auth_context else None,
            'task_scope': list(cast(list[str], auth_context.get('task_scope') or [])) if auth_context else [],
            'subscribers': [],
            'created_at': _iso_now(),
            'updated_at': _iso_now(),
        }
        with self._lock:
            self._tasks[task_id] = task
        self.store.create_federated_task(
            task_id,
            export.name,
            export.target_type,
            str(task['status']),
            cast(dict[str, Any], task['input_payload']),
            tenant_id=cast(str | None, task.get('tenant_id')),
            subject_id=cast(str | None, task.get('subject_id')),
            task_scope=cast(list[str], task.get('task_scope') or []),
        )
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
        auth_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task = self.start_task(export_name, input_text, session_id=session_id, metadata=metadata, auth_context=auth_context)
        subscription = self.set_push_notification(
            str(task['task_id']),
            callback_url,
            lease_seconds=lease_seconds,
            from_sequence=from_sequence,
            auth_context=auth_context,
        )
        return {'task': task, 'push_notification_config': subscription, 'subscription': subscription}

    def get_task(self, task_id: str, auth_context: dict[str, Any] | None = None) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is not None:
            resolved = dict(task)
        else:
            resolved = self.store.load_federated_task(task_id)
        if not self._is_task_visible(resolved, auth_context):
            raise RuntimeError('federated task access denied by tenant/task scope policy')
        return resolved

    def list_tasks(self, auth_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        tasks = [dict(item) for item in self._tasks.values()] if self._tasks else self.store.list_federated_tasks()
        return [task for task in tasks if self._is_task_visible(task, auth_context)]

    def list_task_events(
        self,
        task_id: str,
        after_sequence: int = 0,
        auth_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self._require_task_access(task_id, auth_context)
        events = self.store.list_federated_task_events(task_id, after_sequence)
        for event in events:
            payload = cast(dict[str, Any], event.get('payload', {}))
            event['task'] = cast(dict[str, Any], payload.get('task', self.get_task(task_id, auth_context)))
        return events

    def cancel_task(self, task_id: str, auth_context: dict[str, Any] | None = None) -> dict[str, Any]:
        task = self._require_task_access(task_id, auth_context)
        local_run_id = task.get('local_run_id')
        if local_run_id:
            self.runtime.interrupt_run(str(local_run_id), {'reason': 'federation cancel'})
        self._update_task(task_id, status='cancelled', error_message='cancelled by remote caller')
        return self.get_task(task_id, auth_context)

    def subscribe_task(
        self,
        task_id: str,
        callback_url: str,
        *,
        lease_seconds: int | None,
        from_sequence: int,
        auth_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task = self._require_task_access(task_id, auth_context)
        if not callback_url:
            raise RuntimeError('callback_url is required for webhook subscriptions')
        validate_callback_url(callback_url, self.config.server.push_security)
        subscription_id = uuid.uuid4().hex
        lease = lease_seconds or self.config.server.subscription_lease_seconds
        lease_expires_at = (datetime.now(UTC) + timedelta(seconds=lease)).isoformat()
        self.store.create_federated_subscription(
            subscription_id=subscription_id,
            task_id=task_id,
            mode='webhook',
            callback_url=callback_url,
            status='active',
            tenant_id=cast(str | None, task.get('tenant_id')),
            subject_id=cast(str | None, task.get('subject_id')),
            lease_expires_at=lease_expires_at,
            from_sequence=from_sequence,
        )
        subscription = self.store.load_federated_subscription(subscription_id)
        backlog = self.list_task_events(task_id, after_sequence=from_sequence, auth_context=auth_context)
        if backlog:
            self._dispatch_subscription_events(subscription, backlog)
        return self.store.load_federated_subscription(subscription_id)

    def list_subscriptions(self, task_id: str, auth_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        task = self._require_task_access(task_id, auth_context)
        subscriptions = [self._refresh_subscription(item) for item in self.store.list_federated_subscriptions(task_id)]
        tenant_id = task.get('tenant_id')
        subject_id = task.get('subject_id')
        subscriptions = [
            item
            for item in subscriptions
            if (not tenant_id or item.get('tenant_id') == tenant_id) and (not subject_id or item.get('subject_id') == subject_id)
        ]
        return subscriptions

    def renew_subscription(
        self,
        task_id: str,
        subscription_id: str,
        *,
        lease_seconds: int | None,
        auth_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_task_access(task_id, auth_context)
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

    def cancel_subscription(self, task_id: str, subscription_id: str, auth_context: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_task_access(task_id, auth_context)
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
        auth_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.subscribe_task(
            task_id,
            callback_url,
            lease_seconds=lease_seconds,
            from_sequence=from_sequence,
            auth_context=auth_context,
        )

    def get_push_notification(self, task_id: str, config_id: str, auth_context: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_task_access(task_id, auth_context)
        subscription = self.store.load_federated_subscription(config_id)
        if subscription['task_id'] != task_id:
            raise RuntimeError(f'Push notification config {config_id} does not belong to task {task_id}')
        return self._refresh_subscription(subscription)

    def list_push_notifications(self, task_id: str, auth_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.list_subscriptions(task_id, auth_context)

    def delete_push_notification(self, task_id: str, config_id: str, auth_context: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.cancel_subscription(task_id, config_id, auth_context)

    def resubscribe_task(
        self,
        task_id: str,
        *,
        from_sequence: int,
        callback_url: str | None,
        lease_seconds: int | None,
        auth_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id, auth_context)
        events = self.list_task_events(task_id, after_sequence=from_sequence, auth_context=auth_context)
        payload: dict[str, Any] = {'task': task, 'events': events}
        if callback_url:
            subscription = self.set_push_notification(
                task_id,
                callback_url,
                lease_seconds=lease_seconds,
                from_sequence=from_sequence,
                auth_context=auth_context,
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

    def _deliver_subscription_event(self, callback_url: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        headers = build_callback_headers(callback_url, encoded, self.config.server.push_security)
        if self.config.server.push_security.jws_enabled and self.config.server.jwt.enabled:
            headers[self.config.server.push_security.jws_header] = build_callback_jws(
                callback_url,
                encoded,
                self.config.server.jwt,
                audience=self.config.server.push_security.audience,
            )
        request = urllib_request.Request(
            callback_url,
            data=encoded,
            headers=headers,
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
        capabilities.setdefault('artifacts', bool(item.artifacts))
        capabilities.setdefault('parts', bool(item.parts))
        return capabilities

    def _security_schemes_payload(self) -> dict[str, Any]:
        return {item.name: build_security_scheme_payload(item) for item in self.config.server.security_schemes}

    def _auth_hints_payload(self) -> list[dict[str, Any]]:
        hints = [build_auth_hint_payload(item) for item in self.config.server.security_schemes]
        if hints:
            return hints
        return [
            {
                'type': 'none',
                'header_name': 'Authorization',
                'note': 'Server-side auth enforcement is not configured by default in easy-agent federation.',
            }
        ]

    def _notification_compatibility(self, export: FederationExportConfig | None = None) -> dict[str, Any]:
        push_security = self.config.server.push_security
        payload: dict[str, Any] = {
            'pushNotificationConfig': True,
            'supportsPushNotificationConfig': True,
            'delivery': ['polling', 'webhook_subscribe', 'sse_events'],
            'callbackUrlPolicy': push_security.callback_url_policy,
            'auth': {
                'tokenHeader': push_security.token_header,
                'signatureHeader': push_security.signature_header,
                'timestampHeader': push_security.timestamp_header,
                'audienceHeader': push_security.audience_header,
                'requiresSignature': push_security.require_signature,
                'requiresAudience': push_security.require_audience,
            },
        }
        if push_security.audience:
            payload['auth']['audience'] = push_security.audience
        if export is not None and export.notification_compatibility:
            payload.update(export.notification_compatibility)
        return payload
