from __future__ import annotations

import asyncio
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_config.app import AppConfig, ModelConfig
from agent_integrations.federation import FederationClientManager, FederationServer
from agent_integrations.storage import SQLiteRunStore


class FakeRuntime:
    def __init__(self, tmp_path: Path) -> None:
        self.config = AppConfig.model_validate(
            {
                'model': ModelConfig().model_dump(),
                'graph': {
                    'entrypoint': 'coordinator',
                    'agents': [{'name': 'coordinator'}],
                    'teams': [],
                    'nodes': [],
                },
                'federation': {
                    'server': {
                        'enabled': True,
                        'host': '127.0.0.1',
                        'port': 0,
                        'base_path': '/a2a',
                        'retry_max_attempts': 3,
                        'retry_initial_backoff_seconds': 0.1,
                        'retry_backoff_multiplier': 1.0,
                        'subscription_lease_seconds': 30,
                    },
                    'exports': [
                        {
                            'name': 'local_echo',
                            'target_type': 'agent',
                            'target': 'coordinator',
                            'description': 'Echo target',
                            'modalities': ['text'],
                            'capabilities': ['streaming', 'interrupts'],
                        }
                    ],
                },
                'storage': {'path': str(tmp_path), 'database': 'state.db'},
            }
        )
        self.store = SQLiteRunStore(tmp_path, 'state.db')

    async def run_federated_export(self, export_name: str, input_text: str, *, session_id: str | None = None) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return {
            'run_id': 'local-run',
            'status': 'succeeded',
            'export': export_name,
            'session_id': session_id,
            'result': {'echo': input_text.upper()},
        }

    def interrupt_run(self, run_id: str, payload: dict[str, Any] | None = None) -> None:
        del run_id, payload
        return None


class CallbackCollector:
    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.attempts = 0
        self.deliveries: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                del format, args
                return None

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get('Content-Length', '0') or '0')
                payload = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
                collector.attempts += 1
                collector.deliveries.append(payload)
                status = HTTPStatus.INTERNAL_SERVER_ERROR if collector.fail_first and collector.attempts == 1 else HTTPStatus.OK
                self.send_response(status)
                self.end_headers()

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f'http://127.0.0.1:{port}/callback'

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None


@pytest.mark.asyncio
async def test_federation_loopback_server_and_client(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    server = FederationServer(runtime)
    status = server.start()
    base_url = f'http://127.0.0.1:{status["port"]}'
    manager = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url, 'push_preference': 'sse'}]},
            }
        ).federation
    )
    await manager.start()
    try:
        remote = await manager.inspect_remote('loopback')
        result = await manager.run_remote('loopback', 'local_echo', 'hello', session_id='demo-session')
        stream_events = await manager.stream_remote('loopback', 'local_echo', 'streamed')
        tasks = await manager.list_tasks('loopback')
        task_id = str(tasks[-1]['task_id'])
        task_events = await manager.list_task_events('loopback', task_id)
        streamed_task_events = await manager.stream_task_events('loopback', task_id)
        resubscribe = await manager.resubscribe_task('loopback', task_id, from_sequence=0)
    finally:
        await manager.aclose()
        server.stop()

    assert remote['card']['exports'][0]['name'] == 'local_echo'
    assert remote['card']['well_known_url'].endswith('/.well-known/agent-card.json')
    assert remote['card']['protocol_version'] == '0.3'
    assert remote['card']['exports'][0]['capabilities']['modalities'] == ['text']
    assert remote['extended_card']['capabilities']['push_delivery']['sse_events'] is True
    assert remote['extended_card']['retry_policy']['max_attempts'] == 3
    assert result['result']['echo'] == 'HELLO'
    assert stream_events[-1]['task']['status'] == 'succeeded'
    assert task_events[-1]['event_kind'] == 'task_succeeded'
    assert any(event['event_name'] == 'task_succeeded' for event in streamed_task_events)
    assert resubscribe['events'][-1]['event_kind'] == 'task_succeeded'
    assert len(tasks) >= 2


@pytest.mark.asyncio
async def test_federation_cancel_marks_task(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    server = FederationServer(runtime)
    status = server.start()
    base_url = f'http://127.0.0.1:{status["port"]}'
    manager = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url}]},
            }
        ).federation
    )
    await manager.start()
    try:
        client = manager._client('loopback')
        response = await client.post('/a2a/tasks/send', json={'target': 'local_echo', 'input': 'cancel-me'})
        task_id = response.json()['task']['task_id']
        cancelled = await manager.cancel_task('loopback', task_id)
    finally:
        await manager.aclose()
        server.stop()

    assert cancelled['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_federation_subscription_retry_and_lifecycle(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    server = FederationServer(runtime)
    status = server.start()
    base_url = f'http://127.0.0.1:{status["port"]}'
    callback = CallbackCollector(fail_first=True)
    callback_url = callback.start()
    manager = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url}]},
            }
        ).federation
    )
    await manager.start()
    try:
        client = manager._client('loopback')
        response = await client.post('/a2a/tasks/send', json={'target': 'local_echo', 'input': 'deliver-me'})
        task_id = str(response.json()['task']['task_id'])
        await asyncio.sleep(0.2)
        subscription = await manager.set_push_notification('loopback', task_id, callback_url, from_sequence=0)
        assert subscription['status'] in {'retrying', 'active', 'delivered'}
        await asyncio.sleep(0.2)
        subscriptions = await manager.list_push_notifications('loopback', task_id)
        refreshed = subscriptions[0]
        loaded = await manager.get_push_notification('loopback', task_id, str(refreshed['subscription_id']))
        replay = await manager.resubscribe_task('loopback', task_id, from_sequence=0)
        renewed = await manager.renew_subscription('loopback', task_id, str(refreshed['subscription_id']), lease_seconds=60)
        cancelled = await manager.delete_push_notification('loopback', task_id, str(refreshed['subscription_id']))
    finally:
        await manager.aclose()
        callback.stop()
        server.stop()

    assert callback.attempts >= 2
    assert callback.deliveries[-1]['task_id'] == task_id
    assert callback.deliveries[-1]['events'][-1]['event_kind'] == 'task_succeeded'
    assert refreshed['status'] == 'delivered'
    assert loaded['subscription_id'] == refreshed['subscription_id']
    assert replay['events'][-1]['event_kind'] == 'task_succeeded'
    assert renewed['status'] == 'active'
    assert cancelled['status'] == 'cancelled'


