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
from agent_integrations.federation_security import verify_callback_headers
from agent_integrations.storage import SQLiteRunStore


class FakeRuntime:
    def __init__(
        self,
        tmp_path: Path,
        *,
        server_overrides: dict[str, Any] | None = None,
        exports: list[dict[str, Any]] | None = None,
    ) -> None:
        server_config = {
            'enabled': True,
            'host': '127.0.0.1',
            'port': 0,
            'base_path': '/a2a',
            'retry_max_attempts': 3,
            'retry_initial_backoff_seconds': 0.1,
            'retry_backoff_multiplier': 1.0,
            'subscription_lease_seconds': 30,
        }
        if server_overrides:
            server_config.update(server_overrides)
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
                    'server': server_config,
                    'exports': exports
                    or [
                        {
                            'name': 'local_echo',
                            'target_type': 'agent',
                            'target': 'coordinator',
                            'description': 'Echo target',
                            'modalities': ['text'],
                            'capabilities': ['streaming', 'interrupts'],
                            'artifacts': [{'name': 'result_text', 'modality': 'text/plain'}],
                            'parts': [{'name': 'text', 'modality': 'text/plain'}],
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
        self.requests: list[dict[str, Any]] = []
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
                raw = self.rfile.read(length) if length else b''
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                collector.attempts += 1
                collector.requests.append(
                    {
                        'path': self.path,
                        'payload': payload,
                        'raw': raw,
                        'headers': {key: value for key, value in self.headers.items()},
                    }
                )
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
        result = await manager.run_remote('loopback', 'local_echo', 'hello', session_id='demo-session')
        remote = await manager.inspect_remote('loopback')
        stream_events = await manager.stream_remote('loopback', 'local_echo', 'streamed')
        tasks_page_one = await manager.list_tasks('loopback', page_size=1)
        next_page = await manager.list_tasks('loopback', page_token=tasks_page_one['nextPageToken'], page_size=1)
        task_id = str(tasks_page_one['tasks'][0]['task_id'])
        task_events_page_one = await manager.list_task_events('loopback', task_id, page_size=1)
        task_events_page_two = await manager.list_task_events(
            'loopback',
            task_id,
            page_token=task_events_page_one['nextPageToken'],
            page_size=1,
        )
        streamed_task_events = await manager.stream_task_events('loopback', task_id)
        resubscribe = await manager.resubscribe_task('loopback', task_id, from_sequence=0)
        invalid_page = await manager._client('loopback').get('/a2a/tasks', params={'pageToken': 'not-a-valid-token'})
    finally:
        await manager.aclose()
        server.stop()

    assert remote['card']['exports'][0]['name'] == 'local_echo'
    assert remote['card']['well_known_url'].endswith('/.well-known/agent-card.json')
    assert remote['card']['protocol_version'] == '0.3'
    assert remote['card']['exports'][0]['capabilities']['modalities'] == ['text']
    assert remote['card']['exports'][0]['artifacts'][0]['name'] == 'result_text'
    assert remote['card']['exports'][0]['parts'][0]['name'] == 'text'
    assert remote['card']['defaultInputModes'] == ['text']
    assert remote['card']['notificationCompatibility']['pushNotificationConfig'] is True
    assert remote['extended_card']['capabilities']['push_delivery']['sse_events'] is True
    assert remote['extended_card']['capabilities']['pagination']['pageToken'] is True
    assert remote['extended_card']['retry_policy']['max_attempts'] == 3
    assert result['result']['echo'] == 'HELLO'
    assert stream_events[-1]['task']['status'] == 'succeeded'
    assert len(tasks_page_one['tasks']) == 1
    assert tasks_page_one['nextPageToken']
    assert len(next_page['tasks']) == 1
    assert task_events_page_one['events']
    assert task_events_page_one['nextPageToken']
    assert len(task_events_page_two['events']) == 1
    assert any(event['event_name'] == 'task_succeeded' for event in streamed_task_events)
    assert resubscribe['events'][-1]['event_kind'] == 'task_succeeded'
    assert invalid_page.status_code == HTTPStatus.BAD_REQUEST


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
async def test_federation_subscription_retry_lifecycle_and_signed_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('EASY_AGENT_PUSH_SECRET', 'unit-secret')
    monkeypatch.setenv('EASY_AGENT_PUSH_TOKEN', 'unit-token')
    runtime = FakeRuntime(
        tmp_path,
        server_overrides={
            'push_security': {
                'token_env': 'EASY_AGENT_PUSH_TOKEN',
                'signature_secret_env': 'EASY_AGENT_PUSH_SECRET',
                'require_signature': True,
                'audience': 'easy-agent-tests',
                'require_audience': True,
            }
        },
    )
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
        deadline = asyncio.get_running_loop().time() + 5.0
        refreshed: dict[str, Any] | None = None
        while asyncio.get_running_loop().time() < deadline:
            subscriptions = await manager.list_push_notifications('loopback', task_id)
            if subscriptions and subscriptions[0]['status'] == 'delivered':
                refreshed = subscriptions[0]
                break
            await asyncio.sleep(0.2)
        assert refreshed is not None
        loaded = await manager.get_push_notification('loopback', task_id, str(refreshed['subscription_id']))
        replay = await manager.resubscribe_task('loopback', task_id, from_sequence=0)
        renewed = await manager.renew_subscription('loopback', task_id, str(refreshed['subscription_id']), lease_seconds=60)
        cancelled = await manager.delete_push_notification('loopback', task_id, str(refreshed['subscription_id']))
    finally:
        await manager.aclose()
        callback.stop()
        server.stop()

    last_request = callback.requests[-1]
    verify_callback_headers(
        last_request['headers'],
        last_request['raw'],
        last_request['path'],
        runtime.config.federation.server.push_security,
        expected_secret='unit-secret',
        expected_audience='easy-agent-tests',
    )
    assert callback.attempts >= 2
    assert last_request['payload']['task_id'] == task_id
    assert last_request['payload']['events'][-1]['event_kind'] == 'task_succeeded'
    normalized_headers = {str(key).lower(): value for key, value in last_request['headers'].items()}
    assert normalized_headers['x-a2a-notification-token'] == 'unit-token'
    assert refreshed['status'] == 'delivered'
    assert loaded['subscription_id'] == refreshed['subscription_id']
    assert replay['events'][-1]['event_kind'] == 'task_succeeded'
    assert renewed['status'] == 'active'
    assert cancelled['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_federation_remote_security_readiness_blocks_unsupported_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = FakeRuntime(
        tmp_path,
        server_overrides={
            'security_schemes': [
                {
                    'name': 'oidc_main',
                    'type': 'oidc',
                    'openid_config_url': 'https://issuer.example/.well-known/openid-configuration',
                    'audience': 'easy-agent',
                }
            ],
            'security_requirements': [{'oidc_main': []}],
        },
    )
    server = FederationServer(runtime)
    status = server.start()
    base_url = f'http://127.0.0.1:{status["port"]}'
    insecure_manager = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url}]},
            }
        ).federation
    )
    secure_manager = FederationClientManager(
        AppConfig.model_validate(
            {
                'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                'federation': {
                    'remotes': [
                        {
                            'name': 'loopback',
                            'base_url': base_url,
                            'auth': {
                                'type': 'oidc',
                                'token_env': 'EASY_AGENT_REMOTE_TOKEN',
                                'oauth': {'audience': 'easy-agent', 'openid_config_url': 'https://issuer.example/.well-known/openid-configuration'},
                            },
                        }
                    ]
                },
            }
        ).federation
    )
    monkeypatch.setenv('EASY_AGENT_REMOTE_TOKEN', 'remote-token')
    await insecure_manager.start()
    await secure_manager.start()
    try:
        remote = await insecure_manager.inspect_remote('loopback')
        with pytest.raises(RuntimeError, match='unsupported federation auth'):
            await insecure_manager.run_remote('loopback', 'local_echo', 'blocked')
        allowed = await secure_manager.run_remote('loopback', 'local_echo', 'allowed')
    finally:
        await insecure_manager.aclose()
        await secure_manager.aclose()
        server.stop()

    assert 'oidc_main' in remote['card']['securitySchemes']
    assert allowed['result']['echo'] == 'ALLOWED'
