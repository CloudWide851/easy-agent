from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from agent_common.models import ChatMessage, RuntimeEvent


class SQLiteRunStore:
    def __init__(self, base_path: Path, database_name: str) -> None:
        self.base_path = base_path
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.base_path / 'traces'
        self.trace_path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.base_path / database_name
        self._event_sequences: dict[str, int] = {}
        self._subscribers: list[MemoryObjectSendStream[dict[str, Any]]] = []
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    graph_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_payload TEXT NOT NULL,
                    output_payload TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS node_events (
                    run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    output_payload TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    graph_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_messages (
                    session_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, position)
                );

                CREATE TABLE IF NOT EXISTS session_state (
                    session_id TEXT PRIMARY KEY,
                    shared_state TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_runs_column(connection, 'session_id', 'TEXT')
            connection.commit()

    @staticmethod
    def _ensure_runs_column(connection: sqlite3.Connection, column_name: str, column_type: str) -> None:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(runs)').fetchall()}
        if column_name not in columns:
            connection.execute(f'ALTER TABLE runs ADD COLUMN {column_name} {column_type}')

    def create_run(self, run_id: str, graph_name: str, input_payload: Any, session_id: str | None = None) -> None:
        self._event_sequences.setdefault(run_id, 0)
        with closing(self._connect()) as connection:
            connection.execute(
                'INSERT INTO runs(run_id, graph_name, status, input_payload, created_at, session_id) VALUES (?, ?, ?, ?, ?, ?)',
                (run_id, graph_name, 'running', self._encode(input_payload), self._now(), session_id),
            )
            connection.commit()

    def mark_run_running(self, run_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                'UPDATE runs SET status = ?, output_payload = ? WHERE run_id = ?',
                ('running', None, run_id),
            )
            connection.commit()

    def finish_run(self, run_id: str, status: str, output_payload: Any) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                'UPDATE runs SET status = ?, output_payload = ? WHERE run_id = ?',
                (status, self._encode(output_payload), run_id),
            )
            connection.commit()

    def load_run(self, run_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT graph_name, status, input_payload, output_payload, created_at, session_id FROM runs WHERE run_id = ?',
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f'Run not found: {run_id}')
        return {
            'run_id': run_id,
            'graph_name': row[0],
            'status': row[1],
            'input_payload': self._decode(row[2]),
            'output_payload': self._decode(row[3]),
            'created_at': row[4],
            'session_id': row[5],
        }

    def record_node(
        self,
        run_id: str,
        node_id: str,
        status: str,
        attempt: int,
        output: Any,
        error: str | None,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO node_events(run_id, node_id, status, attempt, output_payload, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, node_id, status, attempt, self._encode(output), error, self._now()),
            )
            connection.commit()

    def subscribe_events(self, max_buffer: int = 2048) -> MemoryObjectReceiveStream[dict[str, Any]]:
        send, receive = anyio.create_memory_object_stream[dict[str, Any]](max_buffer)
        self._subscribers.append(send)
        return receive

    def record_event(
        self,
        run_id: str,
        kind: str,
        payload: Any,
        *,
        scope: str = 'runtime',
        node_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> dict[str, Any]:
        event = self._build_event(
            run_id,
            kind,
            payload,
            scope=scope,
            node_id=node_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
        )
        encoded = self._encode(
            {
                'event_id': event.event_id,
                'sequence': event.sequence,
                'run_id': event.run_id,
                'scope': event.scope,
                'span_id': event.span_id,
                'parent_span_id': event.parent_span_id,
                'node_id': event.node_id,
                'payload': event.payload,
            }
        )
        with closing(self._connect()) as connection:
            connection.execute(
                'INSERT INTO events(run_id, kind, payload, created_at) VALUES (?, ?, ?, ?)',
                (run_id, kind, encoded, event.timestamp),
            )
            connection.commit()
        envelope = event.model_dump()
        with (self.trace_path / f'{run_id}.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(self._encode(envelope) + '\n')
        self._broadcast_event(envelope)
        return envelope

    def save_session_messages(self, session_id: str, graph_name: str, messages: list[ChatMessage]) -> None:
        created_at = self._now()
        with closing(self._connect()) as connection:
            self._upsert_session(connection, session_id, graph_name, created_at)
            connection.execute('DELETE FROM session_messages WHERE session_id = ?', (session_id,))
            connection.executemany(
                'INSERT INTO session_messages(session_id, position, payload, created_at) VALUES (?, ?, ?, ?)',
                [
                    (session_id, index, self._encode(message.model_dump()), created_at)
                    for index, message in enumerate(messages)
                ],
            )
            connection.commit()

    def load_session_messages(self, session_id: str) -> list[ChatMessage]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload FROM session_messages WHERE session_id = ? ORDER BY position ASC',
                (session_id,),
            ).fetchall()
        return [ChatMessage.model_validate(self._decode(row[0])) for row in rows]

    def save_session_state(self, session_id: str, graph_name: str, shared_state: dict[str, Any]) -> None:
        updated_at = self._now()
        with closing(self._connect()) as connection:
            self._upsert_session(connection, session_id, graph_name, updated_at)
            connection.execute('DELETE FROM session_state WHERE session_id = ?', (session_id,))
            connection.execute(
                'INSERT INTO session_state(session_id, shared_state, updated_at) VALUES (?, ?, ?)',
                (session_id, self._encode(shared_state), updated_at),
            )
            connection.commit()

    def load_session_state(self, session_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT shared_state FROM session_state WHERE session_id = ?',
                (session_id,),
            ).fetchone()
        if row is None:
            return {}
        return cast(dict[str, Any], self._decode(row[0]))

    def create_checkpoint(self, run_id: str, kind: str, payload: Any) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                'INSERT INTO checkpoints(run_id, kind, payload, created_at) VALUES (?, ?, ?, ?)',
                (run_id, kind, self._encode(payload), self._now()),
            )
            connection.commit()

    def load_latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT checkpoint_id, kind, payload, created_at FROM checkpoints WHERE run_id = ? ORDER BY checkpoint_id DESC LIMIT 1',
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            'checkpoint_id': row[0],
            'kind': row[1],
            'payload': self._decode(row[2]),
            'created_at': row[3],
        }

    def load_trace(self, run_id: str) -> dict[str, Any]:
        run_row = self.load_run(run_id)
        with closing(self._connect()) as connection:
            node_rows = connection.execute(
                """
                SELECT node_id, status, attempt, output_payload, error_message, created_at
                FROM node_events WHERE run_id = ? ORDER BY created_at ASC
                """,
                (run_id,),
            ).fetchall()
            event_rows = connection.execute(
                'SELECT kind, payload, created_at FROM events WHERE run_id = ? ORDER BY created_at ASC',
                (run_id,),
            ).fetchall()
            checkpoint_rows = connection.execute(
                'SELECT checkpoint_id, kind, payload, created_at FROM checkpoints WHERE run_id = ? ORDER BY checkpoint_id ASC',
                (run_id,),
            ).fetchall()
        events = []
        for row in event_rows:
            body = cast(dict[str, Any], self._decode(row[1]))
            events.append({'kind': row[0], 'created_at': row[2], **body})
        return {
            'graph_name': run_row['graph_name'],
            'status': run_row['status'],
            'session_id': run_row['session_id'],
            'input_payload': run_row['input_payload'],
            'output_payload': run_row['output_payload'],
            'created_at': run_row['created_at'],
            'nodes': [
                {
                    'node_id': row[0],
                    'status': row[1],
                    'attempt': row[2],
                    'output_payload': self._decode(row[3]),
                    'error_message': row[4],
                    'created_at': row[5],
                }
                for row in node_rows
            ],
            'events': events,
            'checkpoints': [
                {
                    'checkpoint_id': row[0],
                    'kind': row[1],
                    'payload': self._decode(row[2]),
                    'created_at': row[3],
                }
                for row in checkpoint_rows
            ],
        }

    def _build_event(
        self,
        run_id: str,
        kind: str,
        payload: Any,
        *,
        scope: str,
        node_id: str | None,
        span_id: str | None,
        parent_span_id: str | None,
    ) -> RuntimeEvent:
        sequence = self._event_sequences.get(run_id, 0) + 1
        self._event_sequences[run_id] = sequence
        body = payload if isinstance(payload, dict) else {'value': payload}
        return RuntimeEvent(
            event_id=uuid.uuid4().hex,
            sequence=sequence,
            run_id=run_id,
            timestamp=self._now(),
            kind=kind,
            scope=scope,
            payload=body,
            span_id=span_id,
            parent_span_id=parent_span_id,
            node_id=node_id,
        )

    def _broadcast_event(self, event: dict[str, Any]) -> None:
        active: list[MemoryObjectSendStream[dict[str, Any]]] = []
        for stream in self._subscribers:
            try:
                stream.send_nowait(event)
                active.append(stream)
            except (anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.WouldBlock):
                continue
        self._subscribers = active

    @staticmethod
    def _encode(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _decode(payload: str | None) -> Any:
        if payload is None:
            return None
        return json.loads(payload)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _upsert_session(connection: sqlite3.Connection, session_id: str, graph_name: str, updated_at: str) -> None:
        existing = connection.execute(
            'SELECT session_id FROM sessions WHERE session_id = ?',
            (session_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                'INSERT INTO sessions(session_id, graph_name, created_at, updated_at) VALUES (?, ?, ?, ?)',
                (session_id, graph_name, updated_at, updated_at),
            )
            return
        connection.execute(
            'UPDATE sessions SET graph_name = ?, updated_at = ? WHERE session_id = ?',
            (graph_name, updated_at, session_id),
        )
