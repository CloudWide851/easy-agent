from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib import request as urllib_request
from urllib.parse import urlparse

import httpx

from agent_common.models import RunStatus, ToolSpec
from agent_common.tools import ToolRegistry
from agent_config.app import FederationConfig, FederationExportConfig, FederationRemoteConfig
from agent_integrations.storage import SQLiteRunStore


@dataclass(slots=True)
class RemoteAgentCard:
    name: str
    description: str
    url: str
    exports: list[dict[str, Any]]


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
        while str(task['status']) in {'queued', 'running'}:
            await asyncio.sleep(self._remote(remote_name).poll_seconds)
            task = await self.get_task(remote_name, str(task['task_id']))
        if str(task['status']) == RunStatus.SUCCEEDED.value:
            return dict(cast(dict[str, Any], task.get('response_payload', {})))
        if str(task['status']) == RunStatus.WAITING_APPROVAL.value:
            return {
                'status': str(task['status']),
                'task_id': str(task['task_id']),
                'request_id': task.get('request_id'),
            }
        if str(task['status']) == 'cancelled':
            return {'status': 'cancelled', 'task_id': str(task['task_id'])}
        raise RuntimeError(str(task.get('error_message') or f'remote task failed: {task}'))

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

    async def cancel_task(self, remote_name: str, task_id: str) -> dict[str, Any]:
        client = self._client(remote_name)
        response = await client.post(f'/a2a/tasks/{task_id}/cancel', json={})
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(dict[str, Any], payload['task'])

    async def subscribe_task(self, remote_name: str, task_id: str, callback_url: str) -> dict[str, Any]:
        client = self._client(remote_name)
        response = await client.post(f'/a2a/tasks/{task_id}/subscribe', json={'callback_url': callback_url})
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(dict[str, Any], payload['task'])

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
        exports = [
            {
                'name': item.name,
                'description': item.description,
                'target_type': item.target_type,
                'tags': item.tags,
                'input_modes': item.input_modes,
                'output_modes': item.output_modes,
            }
            for item in self.config.exports
        ]
        return {
            'name': 'easy-agent-federation',
            'description': 'A2A-style export surface for local easy-agent targets.',
            'url': public_url,
            'version': '0.1',
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

            def _write(self, payload: dict[str, Any], status: int = 200) -> None:
                encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip('/')
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
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
                    self.end_headers()
                    seen_status: str | None = None
                    while True:
                        current = server.get_task(str(task['task_id']))
                        current_status = str(current['status'])
                        if current_status != seen_status:
                            line = json.dumps({'task': current}, ensure_ascii=False).encode('utf-8') + b'\n'
                            self.wfile.write(line)
                            self.wfile.flush()
                            seen_status = current_status
                        if current_status not in {'queued', 'running'}:
                            break
                        time.sleep(0.1)
                    return
                if path.endswith('/cancel'):
                    task_id = path.split('/')[-2]
                    self._write({'task': server.cancel_task(task_id)})
                    return
                if path.endswith('/subscribe'):
                    task_id = path.split('/')[-2]
                    self._write({'task': server.subscribe_task(task_id, str(payload.get('callback_url', '')))})
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
            'subscribers': [],
            'created_at': _iso_now(),
            'updated_at': _iso_now(),
        }
        with self._lock:
            self._tasks[task_id] = task
        self.store.create_federated_task(task_id, export.name, export.target_type, str(task['status']), cast(dict[str, Any], task['input_payload']))
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

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        local_run_id = task.get('local_run_id')
        if local_run_id:
            self.runtime.interrupt_run(str(local_run_id), {'reason': 'federation cancel'})
        self._update_task(task_id, status='cancelled')
        return self.get_task(task_id)

    def subscribe_task(self, task_id: str, callback_url: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.setdefault(task_id, self.store.load_federated_task(task_id))
            subscribers = list(cast(list[str], task.get('subscribers', [])))
            if callback_url and callback_url not in subscribers:
                subscribers.append(callback_url)
            task['subscribers'] = subscribers
            task['updated_at'] = _iso_now()
        self.store.update_federated_task(task_id, subscribers=subscribers)
        if callback_url:
            self._notify(callback_url, self.get_task(task_id))
        return self.get_task(task_id)

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
        )

    def _update_task(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            task = self._tasks.setdefault(task_id, self.store.load_federated_task(task_id))
            task.update(changes)
            task['updated_at'] = _iso_now()
            subscribers = list(cast(list[str], task.get('subscribers', [])))
        self.store.update_federated_task(task_id, **changes, updated_at=task['updated_at'], subscribers=subscribers)
        for callback_url in subscribers:
            self._notify(callback_url, dict(task))

    def _notify(self, callback_url: str, task: dict[str, Any]) -> None:
        try:
            payload = json.dumps({'task': task}, ensure_ascii=False).encode('utf-8')
            req = urllib_request.Request(callback_url, data=payload, headers={'Content-Type': 'application/json'})
            urllib_request.urlopen(req, timeout=3).read()
        except Exception:
            return None
        return None

    def _export(self, export_name: str) -> FederationExportConfig:
        try:
            return cast(FederationExportConfig, self.config.export_map[export_name])
        except KeyError as exc:
            raise RuntimeError(f'Unknown federation export: {export_name}') from exc



def _iso_now() -> str:
    return datetime.now(UTC).isoformat()

