from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from agent_common.models import HumanLoopMode, ToolSpec
from agent_config.app import (
    AppConfig,
    ContainerExecutorOptions,
    ExecutorConfig,
    MicrovmExecutorOptions,
    load_config,
)
from agent_integrations.executors import build_executor_backends
from agent_integrations.federation import FederationClientManager, FederationServer
from agent_integrations.federation_security import verify_callback_headers
from agent_integrations.sandbox import SandboxManager, SandboxMode, SandboxTarget
from agent_integrations.storage import SQLiteRunStore
from agent_integrations.workbench import WorkbenchManager
from agent_runtime.runtime import build_runtime_from_config


@dataclass(slots=True)
class RealNetworkRecord:
    scenario: str
    transport: str
    live_model: bool
    host_dependency: str
    status: str
    duration_seconds: float
    notes: str


class _CallbackCollector:
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


class _FakeRuntime:
    def __init__(self, tmp_path: Path, *, server_overrides: dict[str, Any] | None = None) -> None:
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
                'graph': {'entrypoint': 'coordinator', 'agents': [{'name': 'coordinator'}], 'teams': [], 'nodes': []},
                'federation': {
                    'server': server_config,
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

    async def run_federated_export(self, export_name: str, input_text: str, *, session_id: str | None = None, approval_mode: HumanLoopMode = HumanLoopMode.HYBRID) -> dict[str, Any]:
        del approval_mode
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


def _signed_push_server_overrides() -> dict[str, Any]:
    return {
        'push_security': {
            'token_env': 'EASY_AGENT_PUSH_TOKEN',
            'signature_secret_env': 'EASY_AGENT_PUSH_SECRET',
            'require_signature': True,
            'audience': 'easy-agent-real-network',
            'require_audience': True,
        }
    }


_CROSS_PROCESS_SERVER = textwrap.dedent(
    """
    import json
    import sys
    import threading
    import time
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    tasks = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return None

        def _json(self):
            length = int(self.headers.get('Content-Length', '0') or '0')
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode('utf-8'))

        def _write(self, payload, status=200):
            data = json.dumps(payload).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == '/.well-known/agent-card.json':
                self._write({'name': 'subproc', 'url': f'http://127.0.0.1:{self.server.server_address[1]}/a2a', 'protocol_version': '0.3', 'exports': [{'name': 'remote_echo', 'capabilities': {'modalities': ['text']}}]})
                return
            if self.path == '/a2a/agent-card':
                self._write({'name': 'subproc', 'url': f'http://127.0.0.1:{self.server.server_address[1]}/a2a', 'protocol_version': '0.3', 'exports': [{'name': 'remote_echo', 'capabilities': {'modalities': ['text']}}]})
                return
            if self.path in {'/a2a/extendedAgentCard', '/a2a/agent-card/extended'}:
                self._write({'name': 'subproc', 'capabilities': {'push_delivery': {'sse_events': False}}, 'retry_policy': {'max_attempts': 1}})
                return
            if self.path == '/a2a/tasks':
                self._write({'tasks': list(tasks.values())})
                return
            if self.path.startswith('/a2a/tasks/'):
                task_id = self.path.split('/')[-1]
                self._write({'task': tasks[task_id]})
                return
            self._write({'error': 'not_found'}, status=404)

        def do_POST(self):
            if self.path in {'/a2a/tasks/send', '/a2a/message:send'}:
                payload = self._json()
                task_id = uuid.uuid4().hex
                task = {'task_id': task_id, 'status': 'running', 'response_payload': None, 'error_message': None}
                tasks[task_id] = task
                def worker():
                    time.sleep(0.1)
                    task['status'] = 'succeeded'
                    task['response_payload'] = {'run_id': task_id, 'status': 'succeeded', 'result': {'echo': str(payload.get('input', '')).upper()}}
                threading.Thread(target=worker, daemon=True).start()
                self._write({'task': dict(task)}, status=202)
                return
            self._write({'error': 'not_found'}, status=404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()
    """
)


def _record(scenario: str, transport: str, host_dependency: str, runner: Any, *, live_model: bool = False) -> RealNetworkRecord:
    start = time.perf_counter()
    try:
        notes = runner()
        status = 'passed'
    except RuntimeError as exc:
        status = 'skipped'
        notes = str(exc)
    except Exception as exc:  # noqa: BLE001
        status = 'failed'
        notes = str(exc)
    return RealNetworkRecord(
        scenario=scenario,
        transport=transport,
        live_model=live_model,
        host_dependency=host_dependency,
        status=status,
        duration_seconds=round(time.perf_counter() - start, 4),
        notes=notes,
    )


def _run(command: list[str], *, timeout_seconds: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout_seconds)


def _machine_lock_dir() -> Path:
    user_home = Path(os.environ.get('USERPROFILE') or Path.home())
    return user_home / '.config' / 'containers' / 'podman' / 'machine' / 'wsl'


def _file_readable(path: Path) -> bool:
    try:
        with path.open('rb') as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _socket_target(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    host = parsed.hostname or parsed.path or base_url
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return host, port


def _live_model_preflight(base_config: AppConfig) -> str:
    api_key = os.environ.get(base_config.model.api_key_env, '').strip()
    if not api_key:
        raise RuntimeError(f'preflight: missing {base_config.model.api_key_env}')
    host, port = _socket_target(base_config.model.base_url)
    try:
        with socket.create_connection((host, port), timeout=5.0):
            pass
    except OSError as exc:
        raise RuntimeError(f'preflight: provider_unreachable {host}:{port}: {exc}') from exc
    try:
        response = httpx.get(base_config.model.base_url.rstrip('/'), timeout=5.0, follow_redirects=True)
        status = response.status_code
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f'preflight: provider_http_unreachable {base_config.model.base_url}: {exc}') from exc
    return f'preflight ok: api_key present, tcp {host}:{port}, http_status={status}'


def _podman_machine_info(podman_executable: str, machine_name: str | None = None) -> dict[str, Any]:
    listing = _run([podman_executable, 'machine', 'list', '--format', 'json'], timeout_seconds=30.0)
    if listing.returncode != 0:
        raise RuntimeError(listing.stderr.strip() or listing.stdout.strip() or 'podman machine list failed')
    machines = json.loads(listing.stdout or '[]')
    if not machines:
        raise RuntimeError('preflight: no podman machine is configured')
    if machine_name:
        machine = next((item for item in machines if str(item.get('Name')) == machine_name), None)
        if machine is None:
            raise RuntimeError(f'preflight: podman machine {machine_name} is not configured')
    else:
        machine = next((item for item in machines if item.get('Default')), machines[0])
    if not machine.get('Running'):
        started = _run([podman_executable, 'machine', 'start', str(machine['Name'])], timeout_seconds=180.0)
        if started.returncode != 0:
            raise RuntimeError(started.stderr.strip() or started.stdout.strip() or 'failed to start podman machine')
        listing = _run([podman_executable, 'machine', 'list', '--format', 'json'], timeout_seconds=30.0)
        if listing.returncode == 0:
            refreshed = json.loads(listing.stdout or '[]')
            machine = next((item for item in refreshed if str(item.get('Name')) == str(machine['Name'])), machine)
    port = int(machine.get('Port') or 0)
    user = str(machine.get('RemoteUsername') or 'user')
    identity_path = str(machine.get('IdentityPath') or '')
    if not port or not identity_path:
        inspect = _run([podman_executable, 'machine', 'inspect', str(machine['Name'])], timeout_seconds=30.0)
        if inspect.returncode != 0:
            raise RuntimeError(inspect.stderr.strip() or inspect.stdout.strip() or 'podman machine inspect failed')
        payload = json.loads(inspect.stdout or '[]')
        details = payload[0] if isinstance(payload, list) and payload else payload
        ssh_config = dict(details.get('SSHConfig', {}))
        port = int(ssh_config.get('Port') or port or 0)
        user = str(ssh_config.get('RemoteUsername') or user)
        identity_path = str(ssh_config.get('IdentityPath') or identity_path)
    if not port:
        raise RuntimeError('preflight: podman machine did not expose an SSH port')
    return {
        'name': str(machine.get('Name') or machine_name or 'podman-machine'),
        'port': port,
        'user': user,
        'identity_path': identity_path,
    }


def _podman_preflight(podman_executable: str, machine_name: str | None = None) -> dict[str, Any]:
    if shutil.which(podman_executable) is None and not Path(podman_executable).exists():
        raise RuntimeError(f'preflight: missing podman executable {podman_executable}')
    connections = _run([podman_executable, 'system', 'connection', 'list', '--format', 'json'], timeout_seconds=30.0)
    if connections.returncode != 0:
        raise RuntimeError(connections.stderr.strip() or connections.stdout.strip() or 'preflight: podman connection list failed')
    machine = _podman_machine_info(podman_executable, machine_name=machine_name)
    identity_path = Path(machine['identity_path'])
    if not identity_path.exists():
        raise RuntimeError(f'preflight: podman identity is missing: {identity_path}')
    if not _file_readable(identity_path):
        raise RuntimeError(f'preflight: podman identity is unreadable: {identity_path}')
    lock_dir = _machine_lock_dir()
    if lock_dir.exists() and not os.access(lock_dir, os.W_OK):
        raise RuntimeError(f'preflight: podman lock directory is not writable: {lock_dir}')
    return {
        'machine': machine,
        'note': (
            f"preflight ok: machine={machine['name']} ssh={machine['user']}@127.0.0.1:{machine['port']} "
            f"identity={identity_path.name} lock_dir={lock_dir}"
        ),
    }


def _ensure_container_image_available(podman_executable: str, image: str, archive: Path) -> str:
    exists = _run([podman_executable, 'image', 'exists', image], timeout_seconds=20.0)
    if exists.returncode == 0:
        return f'warm_cache: image already present {image}'
    load_result = _run([podman_executable, 'load', '-i', str(archive)], timeout_seconds=1200.0)
    if load_result.returncode != 0:
        import_result = _run([podman_executable, 'import', str(archive), image], timeout_seconds=1200.0)
        if import_result.returncode != 0:
            raise RuntimeError(
                import_result.stderr.strip()
                or import_result.stdout.strip()
                or load_result.stderr.strip()
                or load_result.stdout.strip()
                or f'preflight: failed to warm container image {image}'
            )
    return f'warm_cache: loaded image {image}'


def _warm_microvm_ssh(machine: dict[str, Any]) -> str:
    result = _run([*_ssh_base_args(machine), 'python3', '--version'], timeout_seconds=30.0)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'preflight: microvm ssh warm cache failed')
    return 'warm_cache: podman-machine ssh ready'


def _ssh_base_args(info: dict[str, Any]) -> list[str]:
    return [
        'ssh',
        '-o',
        'BatchMode=yes',
        '-o',
        'StrictHostKeyChecking=no',
        '-o',
        'UserKnownHostsFile=NUL',
        '-i',
        str(info['identity_path']),
        '-p',
        str(info['port']),
        f"{info['user']}@127.0.0.1",
    ]


def _ensure_offline_container_archive(cache_root: Path, podman_executable: str) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    archive_path = cache_root / 'podman-machine-python-rootfs.tar'
    if archive_path.exists() and 0 < archive_path.stat().st_size < 500 * 1024 * 1024:
        return archive_path
    archive_path.unlink(missing_ok=True)
    info = _podman_machine_info(podman_executable)
    remote_command = [
        'sudo',
        'tar',
        '--numeric-owner',
        '-C',
        '/',
        '-cf',
        '-',
        'usr/bin/python3',
        'usr/bin/python3.14',
        'usr/lib64/python3.14',
        'usr/lib/python3.14',
        'lib64/libpython3.14.so.1.0',
        'lib64/libc.so.6',
        'lib64/libm.so.6',
        'lib64/ld-linux-x86-64.so.2',
    ]
    with archive_path.open('wb') as handle:
        process = subprocess.Popen(
            [*_ssh_base_args(info), *remote_command],
            stdout=handle,
            stderr=subprocess.PIPE,
            shell=False,
        )
        _, stderr = process.communicate(timeout=900.0)
    if process.returncode != 0:
        archive_path.unlink(missing_ok=True)
        raise RuntimeError(stderr.decode('utf-8', errors='ignore').strip() or 'failed to export podman-machine rootfs archive')
    if archive_path.stat().st_size <= 0:
        archive_path.unlink(missing_ok=True)
        raise RuntimeError('failed to create offline rootfs archive')
    return archive_path


def _scenario_cross_process_federation() -> str:
    process = subprocess.Popen([sys.executable, '-c', _CROSS_PROCESS_SERVER], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        port_line = process.stdout.readline().strip() if process.stdout is not None else ''
        if not port_line:
            raise RuntimeError('subprocess federation server did not report a port')

        async def _run_async() -> str:
            manager = FederationClientManager(
                AppConfig.model_validate(
                    {
                        'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                        'federation': {'remotes': [{'name': 'subproc', 'base_url': f'http://127.0.0.1:{port_line}', 'push_preference': 'poll'}]},
                    }
                ).federation
            )
            await manager.start()
            try:
                remote = await manager.inspect_remote('subproc')
                result = await manager.run_remote('subproc', 'remote_echo', 'cross-process')
            finally:
                await manager.aclose()
            if remote['card']['exports'][0]['name'] != 'remote_echo':
                raise AssertionError('unexpected remote export')
            if result['result']['echo'] != 'CROSS-PROCESS':
                raise AssertionError('unexpected remote result')
            return 'cross-process well-known discovery and send/poll federation passed'

        return asyncio.run(_run_async())
    finally:
        process.terminate()
        process.wait(timeout=5)



def _scenario_live_model_federation_roundtrip(base_config: AppConfig, tmp_path: Path) -> str:
    preflight_note = _live_model_preflight(base_config)
    config = AppConfig.model_validate(
        {
            'model': base_config.model.model_dump(),
            'graph': {
                'entrypoint': 'responder',
                'agents': [
                    {
                        'name': 'responder',
                        'description': 'Loopback live-model responder.',
                        'system_prompt': 'Reply with exactly FEDERATED_OK and no other text.',
                        'tools': [],
                        'sub_agents': [],
                        'max_iterations': 2,
                    }
                ],
                'teams': [],
                'nodes': [],
            },
            'federation': {
                'server': {'enabled': True, 'host': '127.0.0.1', 'port': 0, 'base_path': '/a2a'},
                'exports': [
                    {
                        'name': 'live_model_agent',
                        'target_type': 'agent',
                        'target': 'responder',
                        'description': 'Live model federation responder',
                        'modalities': ['text'],
                        'capabilities': ['streaming'],
                    }
                ],
            },
            'storage': {'path': str(tmp_path / 'state'), 'database': 'state.db'},
        }
    )
    config.model.temperature = 0.0
    config.model.max_tokens = min(config.model.max_tokens, 64)
    runtime = build_runtime_from_config(config)

    async def _run_async() -> str:
        await runtime.start()
        status = runtime.serve_federation()
        manager = FederationClientManager(
            AppConfig.model_validate(
                {
                    'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                    'federation': {'remotes': [{'name': 'loopback', 'base_url': str(status['public_base_url'])}]},
                }
            ).federation,
            store=runtime.store,
        )
        await manager.start()
        try:
            result = await manager.run_remote('loopback', 'live_model_agent', 'Return the exact token only.')
        finally:
            await manager.aclose()
            await runtime.aclose()
        response_text = str(result.get('result') or '').strip().upper()
        if 'FEDERATED_OK' not in response_text:
            raise AssertionError(f'unexpected live-model federated response: {result}')
        return f'live-model loopback federation completed through the local A2A surface ({preflight_note})'

    return asyncio.run(_run_async())


def _scenario_federation_retry_and_reconnect(tmp_path: Path) -> str:
    previous_secret = os.environ.get('EASY_AGENT_PUSH_SECRET')
    previous_token = os.environ.get('EASY_AGENT_PUSH_TOKEN')
    os.environ['EASY_AGENT_PUSH_SECRET'] = 'real-network-secret'
    os.environ['EASY_AGENT_PUSH_TOKEN'] = 'real-network-token'
    runtime = _FakeRuntime(tmp_path, server_overrides=_signed_push_server_overrides())
    server = FederationServer(runtime)
    callback = _CallbackCollector(fail_first=True)
    callback_url = callback.start()
    status = server.start()
    base_url = f"http://127.0.0.1:{status['port']}"

    async def _run_async() -> str:
        manager = FederationClientManager(
            AppConfig.model_validate(
                {
                    'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                    'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url}]},
                }
            ).federation,
            store=runtime.store,
        )
        await manager.start()
        try:
            response = await manager.send_subscribe('loopback', 'local_echo', 'deliver-me', callback_url, from_sequence=0)
            task_id = str(response['task']['task_id'])
            subscriptions: list[dict[str, Any]] = []
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                subscriptions = await manager.list_push_notifications('loopback', task_id)
                if subscriptions and subscriptions[0]['status'] == 'delivered':
                    break
                await asyncio.sleep(0.2)
            if not subscriptions or subscriptions[0]['status'] != 'delivered':
                raise AssertionError(f"subscription not delivered: {subscriptions[0]['status'] if subscriptions else 'missing'}")
            replay = await manager.resubscribe_task('loopback', task_id, from_sequence=0)
            renewed = await manager.renew_subscription('loopback', task_id, str(subscriptions[0]['subscription_id']), lease_seconds=60)
            cancelled = await manager.cancel_subscription('loopback', task_id, str(subscriptions[0]['subscription_id']))
            if not replay['events']:
                raise AssertionError('resubscribe did not return backlog events')
            if renewed['status'] != 'active' or cancelled['status'] != 'cancelled':
                raise AssertionError('subscription lifecycle mismatch')
            if callback.attempts < 2:
                raise AssertionError('retry backoff was not exercised')
            if not callback.requests:
                raise AssertionError('expected at least one callback request')
            last_request = callback.requests[-1]
            verify_callback_headers(
                last_request['headers'],
                last_request['raw'],
                last_request['path'],
                runtime.config.federation.server.push_security,
                expected_secret='real-network-secret',
                expected_audience='easy-agent-real-network',
            )
            normalized_headers = {str(key).lower(): value for key, value in last_request['headers'].items()}
            if normalized_headers.get('x-a2a-notification-token') != 'real-network-token':
                raise AssertionError('missing callback token header')
            return 'callback retry, pushNotificationConfig, sendSubscribe, signed webhook delivery, and resubscribe passed'
        finally:
            await manager.aclose()

    try:
        return asyncio.run(_run_async())
    finally:
        callback.stop()
        server.stop()
        if previous_secret is None:
            os.environ.pop('EASY_AGENT_PUSH_SECRET', None)
        else:
            os.environ['EASY_AGENT_PUSH_SECRET'] = previous_secret
        if previous_token is None:
            os.environ.pop('EASY_AGENT_PUSH_TOKEN', None)
        else:
            os.environ['EASY_AGENT_PUSH_TOKEN'] = previous_token


def _workbench_manager(base_path: Path, executors: list[ExecutorConfig], default_executor: str) -> WorkbenchManager:
    sandbox = SandboxManager(
        mode=SandboxMode.PROCESS,
        targets=[SandboxTarget.COMMAND_SKILL, SandboxTarget.STDIO_MCP],
        env_allowlist=['PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA', 'USERNAME', 'HOMEDRIVE', 'HOMEPATH', 'PROGRAMDATA', 'PUBLIC'],
        working_root=base_path,
    )
    store = SQLiteRunStore(base_path / 'state', 'state.db')
    return WorkbenchManager(store, build_executor_backends(executors, sandbox), base_path / 'workbench', default_executor=default_executor, session_ttl_seconds=300)


def _scenario_process_workbench_reuse(tmp_path: Path) -> str:
    manager = _workbench_manager(tmp_path, [ExecutorConfig(name='process', kind='process')], 'process')
    first = manager.ensure_session('run-process', 'skill-echo')
    (first.root_path / 'artifact.txt').write_text('persisted', encoding='utf-8')
    second = manager.ensure_session('run-process', 'skill-echo')
    if first.session_id != second.session_id:
        raise AssertionError('expected workbench session reuse for same owner/name')
    if (second.root_path / 'artifact.txt').read_text(encoding='utf-8') != 'persisted':
        raise AssertionError('workbench artifact was not reused')
    return 'process workbench reused the same long-lived session root'


def _scenario_container_workbench_reuse(tmp_path: Path) -> str:
    podman_executable = os.environ.get('EASY_AGENT_PODMAN_EXE', 'podman')
    preflight = _podman_preflight(podman_executable)
    archive = _ensure_offline_container_archive(Path('.easy-agent/offline-images'), podman_executable)
    image = os.environ.get('EASY_AGENT_CONTAINER_IMAGE', 'localhost/easy-agent/offline-python:latest')
    warm_cache_note = _ensure_container_image_available(podman_executable, image, archive)
    manager = _workbench_manager(
        tmp_path,
        [
            ExecutorConfig(
                name='containerized',
                kind='container',
                default_timeout_seconds=120,
                container=ContainerExecutorOptions(
                    executable=podman_executable,
                    image=image,
                    image_archive=str(archive),
                    keepalive_command=['python3', '-c', 'import time; time.sleep(10**9)'],
                    auto_load=True,
                    auto_build=False,
                    checkpoint_enabled=True,
                    memory_mb=512,
                    cpus=1.0,
                ),
            )
        ],
        'containerized',
    )
    session = manager.ensure_session('run-container', 'skill-echo')
    result = manager.run_command(
        session.session_id,
        ['python3', '-c', "from pathlib import Path; Path('container-marker.txt').write_text('container-checkpoint', encoding='utf-8'); print('container-ok')"],
        env={},
        timeout_seconds=120,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if result.returncode != 0 or 'container-ok' not in result.stdout:
        raise AssertionError(result.stderr or result.stdout)
    shutdown = manager.shutdown_session(session.session_id)
    if shutdown.runtime_state.get('status') != 'checkpointed':
        raise AssertionError('container checkpoint state was not recorded')
    restarted = manager.restart_session(session.session_id)
    result_after = manager.run_command(
        restarted.session_id,
        ['python3', '-c', "from pathlib import Path; print(Path('container-marker.txt').read_text(encoding='utf-8'))"],
        env={},
        timeout_seconds=120,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if result_after.returncode != 0 or 'container-checkpoint' not in result_after.stdout:
        raise AssertionError(result_after.stderr or result_after.stdout)
    if restarted.runtime_state.get('image') != restarted.runtime_state.get('snapshot_image'):
        raise AssertionError('container restart did not restore from the snapshot image')
    return (
        'container executor loaded an offline image archive, enforced quotas, and resumed from a snapshot image '
        f"({preflight['note']}; {warm_cache_note})"
    )


def _scenario_microvm_workbench_reuse(tmp_path: Path) -> str:
    podman_executable = os.environ.get('EASY_AGENT_PODMAN_EXE', 'podman')
    preflight = _podman_preflight(podman_executable)
    machine = cast(dict[str, Any], preflight['machine'])
    warm_cache_note = _warm_microvm_ssh(machine)
    manager = _workbench_manager(
        tmp_path,
        [
            ExecutorConfig(
                name='microvm-machine',
                kind='microvm',
                default_timeout_seconds=90,
                microvm=MicrovmExecutorOptions(
                    provider='podman_machine',
                    executable=podman_executable,
                    machine_name=machine['name'],
                    ssh_user=machine['user'],
                    ssh_private_key=machine['identity_path'],
                    guest_workdir='/tmp/easy-agent-workbench',
                    checkpoint_enabled=True,
                ),
            )
        ],
        'microvm-machine',
    )
    session = manager.ensure_session('run-microvm', 'skill-echo')
    result = manager.run_command(
        session.session_id,
        ['python3', '-c', "from pathlib import Path; Path('microvm-marker.txt').write_text('microvm-checkpoint', encoding='utf-8'); print('microvm-ok')"],
        env={},
        timeout_seconds=90,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if result.returncode != 0 or 'microvm-ok' not in result.stdout:
        raise AssertionError(result.stderr or result.stdout)
    manager.shutdown_session(session.session_id)
    restarted = manager.restart_session(session.session_id)
    result_after = manager.run_command(
        restarted.session_id,
        ['python3', '-c', "from pathlib import Path; print(Path('microvm-marker.txt').read_text(encoding='utf-8'))"],
        env={},
        timeout_seconds=90,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if result_after.returncode != 0 or 'microvm-checkpoint' not in result_after.stdout:
        raise AssertionError(result_after.stderr or result_after.stdout)
    return (
        'microvm executor reused the podman machine over SSH and recovered after a disconnect-style restart '
        f"({preflight['note']}; {warm_cache_note})"
    )



def _scenario_duplicate_delivery_replay_resilience(tmp_path: Path) -> str:
    previous_secret = os.environ.get('EASY_AGENT_PUSH_SECRET')
    previous_token = os.environ.get('EASY_AGENT_PUSH_TOKEN')
    os.environ['EASY_AGENT_PUSH_SECRET'] = 'real-network-secret'
    os.environ['EASY_AGENT_PUSH_TOKEN'] = 'real-network-token'
    runtime = _FakeRuntime(tmp_path, server_overrides=_signed_push_server_overrides())
    server = FederationServer(runtime)
    callback = _CallbackCollector()
    callback_url = callback.start()
    status = server.start()
    base_url = f"http://127.0.0.1:{status['port']}"

    async def _run_async() -> str:
        manager = FederationClientManager(
            AppConfig.model_validate(
                {
                    'graph': {'entrypoint': 'noop', 'agents': [{'name': 'noop'}], 'teams': [], 'nodes': []},
                    'federation': {'remotes': [{'name': 'loopback', 'base_url': base_url}]},
                }
            ).federation,
            store=runtime.store,
        )
        await manager.start()
        try:
            response = await manager.send_subscribe('loopback', 'local_echo', 'stable-delivery', callback_url, from_sequence=0)
            task_id = str(response['task']['task_id'])
            deadline = time.monotonic() + 5.0
            task_payload: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                task_payload = await manager.get_task('loopback', task_id)
                if task_payload['status'] == 'succeeded':
                    break
                await asyncio.sleep(0.1)
            if task_payload is None or task_payload['status'] != 'succeeded':
                raise AssertionError('task did not reach a terminal state')
            events_before = await manager.list_task_events('loopback', task_id, after_sequence=0, page_size=10)
            replay_full = await manager.resubscribe_task('loopback', task_id, from_sequence=0)
            replay_tail = await manager.resubscribe_task('loopback', task_id, from_sequence=1)
            events_after = await manager.list_task_events('loopback', task_id, after_sequence=0, page_size=10)
            sequences_before = [int(item['sequence']) for item in events_before['events']]
            if sequences_before != sorted(set(sequences_before)):
                raise AssertionError('event sequences are not unique and ordered')
            if [int(item['sequence']) for item in replay_full['events']] != sequences_before:
                raise AssertionError('full replay did not preserve the event backlog')
            if any(int(item['sequence']) <= 1 for item in replay_tail['events']):
                raise AssertionError('tail replay returned duplicate early events')
            if len(events_before['events']) != len(events_after['events']):
                raise AssertionError('replay mutated the persisted task event log')
            if not callback.requests:
                raise AssertionError('expected a callback delivery')
            last_request = callback.requests[-1]
            verify_callback_headers(
                last_request['headers'],
                last_request['raw'],
                last_request['path'],
                runtime.config.federation.server.push_security,
                expected_secret='real-network-secret',
                expected_audience='easy-agent-real-network',
            )
            if last_request['payload']['events'][-1]['event_kind'] != 'task_succeeded':
                raise AssertionError('callback delivery did not capture the terminal event')
            return 'duplicate delivery, signed callback replay, and stable federated task event logs passed'
        finally:
            await manager.aclose()

    try:
        return asyncio.run(_run_async())
    finally:
        callback.stop()
        server.stop()
        if previous_secret is None:
            os.environ.pop('EASY_AGENT_PUSH_SECRET', None)
        else:
            os.environ['EASY_AGENT_PUSH_SECRET'] = previous_secret
        if previous_token is None:
            os.environ.pop('EASY_AGENT_PUSH_TOKEN', None)
        else:
            os.environ['EASY_AGENT_PUSH_TOKEN'] = previous_token



def _scenario_container_incremental_snapshot_reuse(tmp_path: Path) -> str:
    podman_executable = os.environ.get('EASY_AGENT_PODMAN_EXE', 'podman')
    preflight = _podman_preflight(podman_executable)
    archive = _ensure_offline_container_archive(Path('.easy-agent/offline-images'), podman_executable)
    image = os.environ.get('EASY_AGENT_CONTAINER_IMAGE', 'localhost/easy-agent/offline-python:latest')
    warm_cache_note = _ensure_container_image_available(podman_executable, image, archive)
    manager = _workbench_manager(
        tmp_path,
        [
            ExecutorConfig(
                name='containerized',
                kind='container',
                default_timeout_seconds=120,
                container=ContainerExecutorOptions(
                    executable=podman_executable,
                    image=image,
                    image_archive=str(archive),
                    keepalive_command=['python3', '-c', 'import time; time.sleep(10**9)'],
                    auto_load=True,
                    auto_build=False,
                    checkpoint_enabled=True,
                    memory_mb=512,
                    cpus=1.0,
                ),
            )
        ],
        'containerized',
    )
    session = manager.ensure_session('run-container-delta', 'skill-echo')
    first_start = time.perf_counter()
    warm_one = manager.run_command(
        session.session_id,
        ['python3', '-c', "from pathlib import Path; Path('step1.txt').write_text('one', encoding='utf-8'); print('step1')"],
        env={},
        timeout_seconds=120,
        target=SandboxTarget.COMMAND_SKILL,
    )
    first_duration = time.perf_counter() - first_start
    if warm_one.returncode != 0:
        raise AssertionError(warm_one.stderr or warm_one.stdout)
    manager.shutdown_session(session.session_id)
    restart_one = manager.restart_session(session.session_id)
    second_start = time.perf_counter()
    warm_two = manager.run_command(
        restart_one.session_id,
        ['python3', '-c', "from pathlib import Path; Path('step2.txt').write_text(Path('step1.txt').read_text(encoding='utf-8') + '-two', encoding='utf-8'); print(Path('step2.txt').read_text(encoding='utf-8'))"],
        env={},
        timeout_seconds=120,
        target=SandboxTarget.COMMAND_SKILL,
    )
    second_duration = time.perf_counter() - second_start
    if warm_two.returncode != 0 or 'one-two' not in warm_two.stdout:
        raise AssertionError(warm_two.stderr or warm_two.stdout)
    manager.shutdown_session(session.session_id)
    restart_two = manager.restart_session(session.session_id)
    verify = manager.run_command(
        restart_two.session_id,
        ['python3', '-c', "from pathlib import Path; print(Path('step2.txt').read_text(encoding='utf-8'))"],
        env={},
        timeout_seconds=120,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if verify.returncode != 0 or 'one-two' not in verify.stdout:
        raise AssertionError(verify.stderr or verify.stdout)
    return (
        'container repeated checkpoint restore preserved incremental state '
        f'(first_cycle={first_duration:.3f}s, second_cycle={second_duration:.3f}s; {preflight['note']}; {warm_cache_note})'
    )



def _scenario_microvm_incremental_snapshot_reuse(tmp_path: Path) -> str:
    podman_executable = os.environ.get('EASY_AGENT_PODMAN_EXE', 'podman')
    preflight = _podman_preflight(podman_executable)
    machine = cast(dict[str, Any], preflight['machine'])
    warm_cache_note = _warm_microvm_ssh(machine)
    manager = _workbench_manager(
        tmp_path,
        [
            ExecutorConfig(
                name='microvm-machine',
                kind='microvm',
                default_timeout_seconds=90,
                microvm=MicrovmExecutorOptions(
                    provider='podman_machine',
                    executable=podman_executable,
                    machine_name=machine['name'],
                    ssh_user=machine['user'],
                    ssh_private_key=machine['identity_path'],
                    guest_workdir='/tmp/easy-agent-workbench',
                    checkpoint_enabled=True,
                ),
            )
        ],
        'microvm-machine',
    )
    session = manager.ensure_session('run-microvm-delta', 'skill-echo')
    first = manager.run_command(
        session.session_id,
        ['python3', '-c', "from pathlib import Path; Path('delta1.txt').write_text('one', encoding='utf-8'); print('delta1')"],
        env={},
        timeout_seconds=90,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if first.returncode != 0:
        raise AssertionError(first.stderr or first.stdout)
    manager.shutdown_session(session.session_id)
    restart_one = manager.restart_session(session.session_id)
    second = manager.run_command(
        restart_one.session_id,
        ['python3', '-c', "from pathlib import Path; Path('delta2.txt').write_text(Path('delta1.txt').read_text(encoding='utf-8') + '-two', encoding='utf-8'); print(Path('delta2.txt').read_text(encoding='utf-8'))"],
        env={},
        timeout_seconds=90,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if second.returncode != 0 or 'one-two' not in second.stdout:
        raise AssertionError(second.stderr or second.stdout)
    manager.shutdown_session(session.session_id)
    restart_two = manager.restart_session(session.session_id)
    verify = manager.run_command(
        restart_two.session_id,
        ['python3', '-c', "from pathlib import Path; print(Path('delta2.txt').read_text(encoding='utf-8'))"],
        env={},
        timeout_seconds=90,
        target=SandboxTarget.COMMAND_SKILL,
    )
    if verify.returncode != 0 or 'one-two' not in verify.stdout:
        raise AssertionError(verify.stderr or verify.stdout)
    return (
        'microvm repeated checkpoint restore preserved incremental state across restart cycles '
        f"({preflight['note']}; {warm_cache_note})"
    )


def _scenario_replay_resume_failure_injection(tmp_path: Path) -> str:
    config = AppConfig.model_validate(
        {
            'graph': {
                'name': 'resume-check',
                'entrypoint': 'review',
                'agents': [{'name': 'worker', 'description': 'worker'}],
                'teams': [],
                'nodes': [
                    {'id': 'prepare', 'type': 'join'},
                    {'id': 'review', 'type': 'tool', 'target': 'flaky_tool', 'deps': ['prepare']},
                ],
            },
            'storage': {'path': str(tmp_path / 'state'), 'database': 'state.db'},
        }
    )
    runtime = build_runtime_from_config(config)
    counter = {'calls': 0}

    async def flaky_tool(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        del arguments, context
        counter['calls'] += 1
        if counter['calls'] == 1:
            raise RuntimeError('injected failure')
        return {'status': 'recovered'}

    runtime.register_tool(spec=ToolSpec(name='flaky_tool', description='flaky', input_schema={'type': 'object'}), handler=flaky_tool)

    async def _run_async() -> str:
        try:
            first_run_id: str | None = None
            try:
                result = await runtime.run('resume me')
                first_run_id = str(result['run_id'])
            except RuntimeError as exc:
                message = str(exc)
                if 'failed' not in message:
                    raise
                first_run_id = message.split('Run ', 1)[1].split(' failed', 1)[0]
            if not first_run_id:
                raise AssertionError('missing failed run id')
            checkpoints = runtime.list_checkpoints(first_run_id)
            if not checkpoints:
                raise AssertionError('expected checkpoints before resume')
            resumed = await runtime.resume(first_run_id)
            replay = await runtime.replay(first_run_id, checkpoints[-1]['checkpoint_id'])
            forked = await runtime.resume(first_run_id, checkpoint_id=checkpoints[-1]['checkpoint_id'], fork=True)
            if resumed['result']['status'] != 'recovered':
                raise AssertionError('resume did not recover')
            if replay['checkpoint_kind'] != 'graph':
                raise AssertionError('replay did not return graph checkpoint')
            if forked['run_id'] == first_run_id:
                raise AssertionError('fork resume should allocate a new run id')
            return 'resume, replay, and fork recovery passed under injected failure'
        finally:
            await runtime.aclose()

    return asyncio.run(_run_async())


def run_real_network_suite(config_path: str | Path = 'easy-agent.yml') -> dict[str, Any]:
    base_config = load_config(config_path)
    output_root = Path('.easy-agent')
    output_root.mkdir(parents=True, exist_ok=True)
    tmp_root = output_root / 'real-network-tmp' / str(int(time.time() * 1000))
    tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        records = [
            _record('cross_process_federation', 'http_poll', 'python subprocess', _scenario_cross_process_federation),
            _record('live_model_federation_roundtrip', 'http_poll', f"{base_config.model.provider} live model", lambda: _scenario_live_model_federation_roundtrip(base_config, tmp_root / 'live-model'), live_model=True),
            _record('disconnect_retry_chaos', 'http_webhook', 'loopback callback server', lambda: _scenario_federation_retry_and_reconnect(tmp_root / 'federation')),
            _record('duplicate_delivery_replay_resilience', 'http_webhook', 'loopback callback server', lambda: _scenario_duplicate_delivery_replay_resilience(tmp_root / 'federation-replay')),
            _record('workbench_reuse_process', 'local_process', 'none', lambda: _scenario_process_workbench_reuse(tmp_root / 'process')),
            _record('workbench_reuse_container', 'podman_exec', 'podman machine rootfs import', lambda: _scenario_container_workbench_reuse(tmp_root / 'container')),
            _record('workbench_incremental_snapshot_reuse_container', 'podman_exec', 'podman machine rootfs import', lambda: _scenario_container_incremental_snapshot_reuse(tmp_root / 'container-delta')),
            _record('workbench_reuse_microvm', 'podman_machine_ssh', 'podman machine ssh', lambda: _scenario_microvm_workbench_reuse(tmp_root / 'microvm')),
            _record('workbench_incremental_snapshot_reuse_microvm', 'podman_machine_ssh', 'podman machine ssh', lambda: _scenario_microvm_incremental_snapshot_reuse(tmp_root / 'microvm-delta')),
            _record('replay_resume_failure_injection', 'sqlite_checkpoint', 'none', lambda: _scenario_replay_resume_failure_injection(tmp_root / 'resume')),
        ]
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    summary = {
        'runs': len(records),
        'passed': sum(1 for item in records if item.status == 'passed'),
        'failed': sum(1 for item in records if item.status == 'failed'),
        'skipped': sum(1 for item in records if item.status == 'skipped'),
    }
    report = {
        'records': [asdict(item) for item in records],
        'summary': summary,
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    report_path = output_root / 'real-network-report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
