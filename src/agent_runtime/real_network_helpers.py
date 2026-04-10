from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(slots=True)
class RealNetworkRecord:
    scenario: str
    transport: str
    live_model: bool
    host_dependency: str
    status: str
    duration_seconds: float
    notes: str
    telemetry: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScenarioOutcome:
    notes: str
    telemetry: dict[str, Any] = field(default_factory=dict)


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


def cache_hit_from_note(note: str) -> bool:
    lowered = note.lower()
    return 'already present' in lowered or 'ssh ready' in lowered


def cache_source_from_note(note: str) -> str:
    lowered = note.lower()
    if 'already present' in lowered:
        return 'warm_cache'
    if 'loaded image' in lowered:
        return 'archive_load'
    if 'ssh ready' in lowered:
        return 'ssh_warm_cache'
    return 'unknown'


def budget_status(value: float | None, budget: float | None) -> str:
    if value is None or budget is None:
        return 'not_applicable'
    return 'within_budget' if value <= budget else 'exceeds_budget'


def snapshot_drift(cold_start: float | None, warm_start: float | None) -> tuple[float | None, float | None]:
    if cold_start is None or warm_start is None:
        return None, None
    drift = abs(warm_start - cold_start)
    base = cold_start if cold_start > 0 else 0.001
    return round(drift, 4), round(drift / base, 4)
