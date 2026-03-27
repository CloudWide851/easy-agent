
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


class FederationClientManager:
    def __init__(self, config: FederationConfig, store: SQLiteRunStore | None = None) -> None:
        self.config = config
        self.store = store
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._started = False

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
        client = self._client(remote_name)
        card = cast(dict[str, Any], (await client.get('/a2a/agent-card')).json())
        extended = cast(dict[str, Any], (await client.get('/a2a/agent-card/extended')).json())
        return {'card': card, 'extended_card': extended}

    async def run_remote(
        self,
        remote_name: str,
        target: str,
        input_text: str,
        *,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._client(remote_name)
        response = await client.post(
            '/a2a/tasks/send',
            json={
                'target': target,
                'input': input_text,
                'session_id': session_id,
                'metadata': metadata or {},
            },
        )
        response.raise_for_status()
        task = cast(dict[str, Any], cast(dict[str, Any], response.json())['task'])
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
        client = self._client(remote_name)
        events: list[dict[str, Any]] = []
        async with client.stream(
            'POST',
            '/a2a/tasks/send-stream',
            json={
                'target': target,
                'input': input_text,
                'session_id': session_id,
                'metadata': metadata or {},
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                events.append(cast(dict[str, Any], json.loads(line)))
        return events

    async def get_task(self, remote_name: str, task_id: str) -> dict[str, Any]:
        client = self._client(remote_name)
        response = await client.get(f'/a2a/tasks/{task_id}')
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(dict[str, Any], payload['task'])

    async def list_tasks(self, remote_name: str) -> list[dict[str, Any]]:
        client = self._client(remote_name)
        response = await client.get('/a2a/tasks')
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(list[dict[str, Any]], payload['tasks'])

    async def list_task_events(self, remote_name: str, task_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        client = self._client(remote_name)
        response = await client.get(f'/a2a/tasks/{task_id}/events', params={'after_sequence': after_sequence})
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(list[dict[str, Any]], payload['events'])

    async def stream_task_events(self, remote_name: str, task_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        latest_task = await self.get_task(remote_name, task_id)
        if str(latest_task['status']) in TERMINAL_TASK_STATUSES:
            terminal_events = await self.list_task_events(remote_name, task_id, after_sequence)
            for event in terminal_events:
                event.setdefault('event_name', str(event.get('event_kind', 'task_event')))
            return terminal_events
        client = self._client(remote_name)
        events: list[dict[str, Any]] = []
        async with client.stream('GET', f'/a2a/tasks/{task_id}/events/stream', params={'after_sequence': after_sequence}) as response:
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
                events.append(body)
        return events

    async def cancel_task(self, remote_name: str, task_id: str) -> dict[str, Any]:
        client = self._client(remote_name)
        response = await client.post(f'/a2a/tasks/{task_id}/cancel', json={})
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(dict[str, Any], payload['task'])

    async def subscribe_task(
        self,
        remote_name: str,
        task_id: str,
        callback_url: str,
        *,
        lease_seconds: int | None = None,
        from_sequence: int = 0,
    ) -> dict[str, Any]:
        client = self._client(remote_name)
        response = await client.post(
            f'/a2a/tasks/{task_id}/subscribe',
            json={'callback_url': callback_url, 'lease_seconds': lease_seconds, 'from_sequence': from_sequence},
        )
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(dict[str, Any], payload['subscription'])

    async def list_subscriptions(self, remote_name: str, task_id: str) -> list[dict[str, Any]]:
        client = self._client(remote_name)
        response = await client.get(f'/a2a/tasks/{task_id}/subscriptions')
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(list[dict[str, Any]], payload['subscriptions'])

    async def renew_subscription(
        self,
        remote_name: str,
        task_id: str,
        subscription_id: str,
        *,
        lease_seconds: int | None = None,
    ) -> dict[str, Any]:
        client = self._client(remote_name)
        response = await client.post(
            f'/a2a/tasks/{task_id}/subscriptions/{subscription_id}/renew',
            json={'lease_seconds': lease_seconds},
        )
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(dict[str, Any], payload['subscription'])

    async def cancel_subscription(self, remote_name: str, task_id: str, subscription_id: str) -> dict[str, Any]:
        client = self._client(remote_name)
        response = await client.post(f'/a2a/tasks/{task_id}/subscriptions/{subscription_id}/cancel', json={})
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(dict[str, Any], payload['subscription'])

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
        client = self._client(remote_name)
        latest_task = await self.get_task(remote_name, task_id)
        after_sequence = 0
        async with client.stream('GET', f'/a2a/tasks/{task_id}/events/stream', params={'after_sequence': after_sequence}) as response:
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

    def agent_card(self) -> dict[str, Any]:
        public_url = self.config.server.public_url or f'http://{self.config.server.host}:{self.config.server.port}{self.config.server.base_path}'
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
        }

    def start(self) -> dict[str, Any]:
        if self._server is not None:
            return self.status()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
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
                path = parsed.path.rstrip('/')
                query = self._query()
                base = server.config.server.base_path.rstrip('/')
                if path == f'{base}/agent-card':
                    self._write(server.agent_card())
                    return
                if path == f'{base}/agent-card/extended':
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
                if path.startswith(f'{base}/tasks/'):
                    task_id = path.split('/')[-1]
                    self._write({'task': server.get_task(task_id)})
                    return
                self._write({'error': 'not_found'}, status=404)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip('/')
                base = server.config.server.base_path.rstrip('/')
                payload = self._json()
                if path == f'{base}/tasks/send':
                    task = server.start_task(
                        str(payload.get('target', '')),
                        str(payload.get('input', '')),
                        session_id=cast(str | None, payload.get('session_id')),
                        metadata=dict(cast(dict[str, Any], payload.get('metadata', {}))),
                    )
                    self._write({'task': task}, status=202)
                    return
                if path == f'{base}/tasks/send-stream':
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
                if path.endswith('/cancel') and '/subscriptions/' not in path:
                    task_id = path.split('/')[-2]
                    self._write({'task': server.cancel_task(task_id)})
                    return
                if path.endswith('/subscribe'):
                    task_id = path.split('/')[-2]
                    subscription = server.subscribe_task(
                        task_id,
                        str(payload.get('callback_url', '')),
                        lease_seconds=cast(int | None, payload.get('lease_seconds')),
                        from_sequence=int(payload.get('from_sequence', 0) or 0),
                    )
                    self._write({'task': server.get_task(task_id), 'subscription': subscription}, status=202)
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
        return subscription

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
            self._update_task(task_id, status=RunStatus.FAILED.value, error_message=str(exc))
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
        results: list[dict[str, Any]] = []
        for subscription in subscriptions:
            results.append(self._dispatch_subscription_events(subscription, events))
        return results

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

    def _export_capabilities(self, export: FederationExportConfig) -> dict[str, Any]:
        return {
            'declared': export.capabilities,
            'modalities': export.modalities,
            'input_modes': export.input_modes,
            'output_modes': export.output_modes,
            'supports_sessions': True,
            'supports_interrupts': True,
            'supports_streaming': True,
            'supports_resume_reference': True,
            'supports_human_approval': True,
            'auth_hints': [
                {
                    'type': 'none',
                    'header_name': 'Authorization',
                    'note': 'Override this export behind your own gateway if remote auth enforcement is required.',
                }
            ],
            'compatibility': {
                'protocol_version': self.config.server.protocol_version,
                'card_schema_version': self.config.server.card_schema_version,
                'runtime_version': runtime_version(),
                'target_type': export.target_type,
            },
        }





