from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from agent_common.models import (
    ChatMessage,
    HumanRequest,
    HumanRequestStatus,
    RunStatus,
    RuntimeEvent,
)


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
                    created_at TEXT NOT NULL,
                    session_id TEXT,
                    run_kind TEXT NOT NULL DEFAULT 'graph',
                    parent_run_id TEXT,
                    source_run_id TEXT,
                    source_checkpoint_id INTEGER,
                    resume_strategy TEXT
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

                CREATE TABLE IF NOT EXISTS harness_state (
                    session_id TEXT NOT NULL,
                    harness_name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, harness_name)
                );

                CREATE TABLE IF NOT EXISTS human_requests (
                    request_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    response_payload TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    UNIQUE(run_id, request_key)
                );

                CREATE TABLE IF NOT EXISTS interrupt_requests (
                    run_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    consumed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS oauth_state (
                    server_name TEXT PRIMARY KEY,
                    tokens_payload TEXT,
                    client_info_payload TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workbench_sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_run_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    executor_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_payload TEXT NOT NULL,
                    branch_parent_session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT
                );

                CREATE TABLE IF NOT EXISTS workbench_executions (
                    execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    command_payload TEXT NOT NULL,
                    returncode INTEGER NOT NULL,
                    stdout TEXT NOT NULL,
                    stderr TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS federated_tasks (
                    task_id TEXT PRIMARY KEY,
                    export_name TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_payload TEXT NOT NULL,
                    response_payload TEXT,
                    error_message TEXT,
                    local_run_id TEXT,
                    request_id TEXT,
                    subscribers_payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_runs_column(connection, 'session_id', 'TEXT')
            self._ensure_runs_column(connection, 'run_kind', "TEXT NOT NULL DEFAULT 'graph'")
            self._ensure_runs_column(connection, 'parent_run_id', 'TEXT')
            self._ensure_runs_column(connection, 'source_run_id', 'TEXT')
            self._ensure_runs_column(connection, 'source_checkpoint_id', 'INTEGER')
            self._ensure_runs_column(connection, 'resume_strategy', 'TEXT')
            connection.commit()

    @staticmethod
    def _ensure_runs_column(connection: sqlite3.Connection, column_name: str, column_type: str) -> None:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(runs)').fetchall()}
        if column_name not in columns:
            connection.execute(f'ALTER TABLE runs ADD COLUMN {column_name} {column_type}')

    def create_run(
        self,
        run_id: str,
        graph_name: str,
        input_payload: Any,
        session_id: str | None = None,
        run_kind: str = 'graph',
        parent_run_id: str | None = None,
        source_run_id: str | None = None,
        source_checkpoint_id: int | None = None,
        resume_strategy: str | None = None,
    ) -> None:
        self._event_sequences.setdefault(run_id, 0)
        with closing(self._connect()) as connection:
            connection.execute(
                (
                    'INSERT INTO runs('
                    'run_id, graph_name, status, input_payload, created_at, session_id, run_kind, '
                    'parent_run_id, source_run_id, source_checkpoint_id, resume_strategy'
                    ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
                ),
                (
                    run_id,
                    graph_name,
                    RunStatus.RUNNING.value,
                    self._encode(input_payload),
                    self._now(),
                    session_id,
                    run_kind,
                    parent_run_id,
                    source_run_id,
                    source_checkpoint_id,
                    resume_strategy,
                ),
            )
            connection.commit()

    def mark_run_running(self, run_id: str) -> None:
        self._set_run_state(run_id, RunStatus.RUNNING, None)

    def mark_run_waiting_approval(self, run_id: str, output_payload: Any) -> None:
        self._set_run_state(run_id, RunStatus.WAITING_APPROVAL, output_payload)

    def mark_run_interrupted(self, run_id: str, output_payload: Any) -> None:
        self._set_run_state(run_id, RunStatus.INTERRUPTED, output_payload)

    def _set_run_state(self, run_id: str, status: RunStatus, output_payload: Any) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                'UPDATE runs SET status = ?, output_payload = ? WHERE run_id = ?',
                (status.value, self._encode(output_payload), run_id),
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
                (
                    'SELECT graph_name, status, input_payload, output_payload, created_at, session_id, run_kind, '
                    'parent_run_id, source_run_id, source_checkpoint_id, resume_strategy '
                    'FROM runs WHERE run_id = ?'
                ),
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
            'run_kind': row[6] or 'graph',
            'parent_run_id': row[7],
            'source_run_id': row[8],
            'source_checkpoint_id': row[9],
            'resume_strategy': row[10],
        }

    def list_child_runs(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                (
                    'SELECT run_id, graph_name, status, created_at, source_checkpoint_id, resume_strategy '
                    'FROM runs WHERE parent_run_id = ? ORDER BY created_at ASC'
                ),
                (run_id,),
            ).fetchall()
        return [
            {
                'run_id': row[0],
                'graph_name': row[1],
                'status': row[2],
                'created_at': row[3],
                'source_checkpoint_id': row[4],
                'resume_strategy': row[5],
            }
            for row in rows
        ]

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
        encoded_envelope = cast(str, self._encode(envelope))
        with (self.trace_path / f'{run_id}.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(encoded_envelope + '\n')
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

    def save_harness_state(self, session_id: str, harness_name: str, payload: dict[str, Any]) -> None:
        updated_at = self._now()
        with closing(self._connect()) as connection:
            connection.execute(
                'INSERT INTO harness_state(session_id, harness_name, payload, updated_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(session_id, harness_name) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at',
                (session_id, harness_name, self._encode(payload), updated_at),
            )
            connection.commit()

    def load_harness_state(self, session_id: str, harness_name: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload FROM harness_state WHERE session_id = ? AND harness_name = ?',
                (session_id, harness_name),
            ).fetchone()
        if row is None:
            return {}
        return cast(dict[str, Any], self._decode(row[0]))

    def create_checkpoint(self, run_id: str, kind: str, payload: Any) -> int:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                'INSERT INTO checkpoints(run_id, kind, payload, created_at) VALUES (?, ?, ?, ?)',
                (run_id, kind, self._encode(payload), self._now()),
            )
            connection.commit()
            if cursor.lastrowid is None:
                raise RuntimeError('checkpoint insert did not return an id')
            checkpoint_id = int(cursor.lastrowid)
        return checkpoint_id

    def list_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT checkpoint_id, kind, payload, created_at FROM checkpoints WHERE run_id = ? ORDER BY checkpoint_id ASC',
                (run_id,),
            ).fetchall()
        return [
            {
                'checkpoint_id': row[0],
                'kind': row[1],
                'payload': self._decode(row[2]),
                'created_at': row[3],
            }
            for row in rows
        ]

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

    def load_checkpoint(self, run_id: str, checkpoint_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT checkpoint_id, kind, payload, created_at FROM checkpoints WHERE run_id = ? AND checkpoint_id = ?',
                (run_id, checkpoint_id),
            ).fetchone()
        if row is None:
            return None
        return {
            'checkpoint_id': row[0],
            'kind': row[1],
            'payload': self._decode(row[2]),
            'created_at': row[3],
        }

    def create_human_request(
        self,
        run_id: str,
        request_key: str,
        kind: str,
        title: str,
        payload: dict[str, Any],
    ) -> HumanRequest:
        existing = self.load_human_request_by_key(run_id, request_key)
        if existing is not None:
            return existing
        request_id = uuid4().hex
        created_at = self._now()
        with closing(self._connect()) as connection:
            connection.execute(
                (
                    'INSERT INTO human_requests('
                    'request_id, run_id, request_key, kind, status, title, payload, response_payload, created_at, resolved_at'
                    ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
                ),
                (
                    request_id,
                    run_id,
                    request_key,
                    kind,
                    HumanRequestStatus.PENDING.value,
                    title,
                    self._encode(payload),
                    None,
                    created_at,
                    None,
                ),
            )
            connection.commit()
        return HumanRequest(
            request_id=request_id,
            run_id=run_id,
            request_key=request_key,
            kind=kind,
            status=HumanRequestStatus.PENDING,
            title=title,
            payload=payload,
            response_payload=None,
            created_at=created_at,
            resolved_at=None,
        )

    def load_human_request(self, request_id: str) -> HumanRequest:
        with closing(self._connect()) as connection:
            row = connection.execute(
                (
                    'SELECT run_id, request_key, kind, status, title, payload, response_payload, created_at, resolved_at '
                    'FROM human_requests WHERE request_id = ?'
                ),
                (request_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f'Human request not found: {request_id}')
        return HumanRequest(
            request_id=request_id,
            run_id=row[0],
            request_key=row[1],
            kind=row[2],
            status=HumanRequestStatus(row[3]),
            title=row[4],
            payload=cast(dict[str, Any], self._decode(row[5])),
            response_payload=cast(dict[str, Any] | None, self._decode(row[6])),
            created_at=row[7],
            resolved_at=row[8],
        )

    def load_human_request_by_key(self, run_id: str, request_key: str) -> HumanRequest | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT request_id FROM human_requests WHERE run_id = ? AND request_key = ?',
                (run_id, request_key),
            ).fetchone()
        if row is None:
            return None
        return self.load_human_request(str(row[0]))

    def list_human_requests(
        self,
        *,
        status: HumanRequestStatus | None = None,
        run_id: str | None = None,
    ) -> list[HumanRequest]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append('status = ?')
            params.append(status.value)
        if run_id is not None:
            clauses.append('run_id = ?')
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
        query = (
            'SELECT request_id FROM human_requests '
            f'{where} ORDER BY created_at ASC'
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self.load_human_request(str(row[0])) for row in rows]

    def resolve_human_request(
        self,
        request_id: str,
        *,
        status: HumanRequestStatus,
        response_payload: dict[str, Any] | None = None,
    ) -> HumanRequest:
        resolved_at = self._now()
        with closing(self._connect()) as connection:
            connection.execute(
                'UPDATE human_requests SET status = ?, response_payload = ?, resolved_at = ? WHERE request_id = ?',
                (status.value, self._encode(response_payload), resolved_at, request_id),
            )
            connection.commit()
        return self.load_human_request(request_id)

    def request_interrupt(self, run_id: str, payload: dict[str, Any] | None = None) -> None:
        requested_at = self._now()
        with closing(self._connect()) as connection:
            connection.execute(
                'INSERT INTO interrupt_requests(run_id, payload, requested_at, consumed_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(run_id) DO UPDATE SET payload = excluded.payload, requested_at = excluded.requested_at, consumed_at = NULL',
                (run_id, self._encode(payload or {}), requested_at, None),
            )
            connection.commit()

    def consume_interrupt(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload FROM interrupt_requests WHERE run_id = ? AND consumed_at IS NULL',
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            payload = cast(dict[str, Any], self._decode(row[0]))
            connection.execute(
                'UPDATE interrupt_requests SET consumed_at = ? WHERE run_id = ?',
                (self._now(), run_id),
            )
            connection.commit()
        return payload

    def save_oauth_tokens(self, server_name: str, payload: dict[str, Any] | None) -> None:
        updated_at = self._now()
        with closing(self._connect()) as connection:
            connection.execute(
                'INSERT INTO oauth_state(server_name, tokens_payload, client_info_payload, updated_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(server_name) DO UPDATE SET tokens_payload = excluded.tokens_payload, updated_at = excluded.updated_at',
                (server_name, self._encode(payload), None, updated_at),
            )
            connection.commit()

    def load_oauth_tokens(self, server_name: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT tokens_payload FROM oauth_state WHERE server_name = ?',
                (server_name,),
            ).fetchone()
        if row is None:
            return None
        return cast(dict[str, Any] | None, self._decode(row[0]))

    def save_oauth_client_info(self, server_name: str, payload: dict[str, Any] | None) -> None:
        updated_at = self._now()
        with closing(self._connect()) as connection:
            connection.execute(
                'INSERT INTO oauth_state(server_name, tokens_payload, client_info_payload, updated_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(server_name) DO UPDATE SET client_info_payload = excluded.client_info_payload, updated_at = excluded.updated_at',
                (server_name, None, self._encode(payload), updated_at),
            )
            connection.commit()

    def load_oauth_client_info(self, server_name: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT client_info_payload FROM oauth_state WHERE server_name = ?',
                (server_name,),
            ).fetchone()
        if row is None:
            return None
        return cast(dict[str, Any] | None, self._decode(row[0]))

    def clear_oauth_state(self, server_name: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute('DELETE FROM oauth_state WHERE server_name = ?', (server_name,))
            connection.commit()

    def create_workbench_session(
        self,
        *,
        session_id: str,
        owner_run_id: str,
        name: str,
        root_path: str,
        executor_name: str,
        metadata: dict[str, Any] | None,
        expires_at: str | None,
        branch_parent_session_id: str | None = None,
    ) -> None:
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute(
                (
                    'INSERT INTO workbench_sessions('
                    'session_id, owner_run_id, name, root_path, executor_name, status, metadata_payload, '
                    'branch_parent_session_id, created_at, updated_at, expires_at'
                    ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
                ),
                (
                    session_id,
                    owner_run_id,
                    name,
                    root_path,
                    executor_name,
                    'active',
                    self._encode(metadata or {}),
                    branch_parent_session_id,
                    now,
                    now,
                    expires_at,
                ),
            )
            connection.commit()

    def load_workbench_session(self, session_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                (
                    'SELECT owner_run_id, name, root_path, executor_name, status, metadata_payload, '
                    'branch_parent_session_id, created_at, updated_at, expires_at '
                    'FROM workbench_sessions WHERE session_id = ?'
                ),
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f'Workbench session not found: {session_id}')
        return {
            'session_id': session_id,
            'owner_run_id': row[0],
            'name': row[1],
            'root_path': row[2],
            'executor_name': row[3],
            'status': row[4],
            'metadata': cast(dict[str, Any], self._decode(row[5]) or {}),
            'branch_parent_session_id': row[6],
            'created_at': row[7],
            'updated_at': row[8],
            'expires_at': row[9],
        }

    def load_workbench_session_by_owner(self, owner_run_id: str, name: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT session_id FROM workbench_sessions WHERE owner_run_id = ? AND name = ?',
                (owner_run_id, name),
            ).fetchone()
        if row is None:
            return None
        return self.load_workbench_session(str(row[0]))

    def list_workbench_sessions(self, owner_run_id: str | None = None) -> list[dict[str, Any]]:
        query = 'SELECT session_id FROM workbench_sessions'
        params: list[Any] = []
        if owner_run_id is not None:
            query += ' WHERE owner_run_id = ?'
            params.append(owner_run_id)
        query += ' ORDER BY created_at ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self.load_workbench_session(str(row[0])) for row in rows]

    def touch_workbench_session(self, session_id: str, expires_at: str | None) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                'UPDATE workbench_sessions SET updated_at = ?, expires_at = ? WHERE session_id = ?',
                (self._now(), expires_at, session_id),
            )
            connection.commit()

    def update_workbench_session_status(self, session_id: str, status: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                'UPDATE workbench_sessions SET status = ?, updated_at = ? WHERE session_id = ?',
                (status, self._now(), session_id),
            )
            connection.commit()

    def record_workbench_execution(
        self,
        *,
        session_id: str,
        command: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                (
                    'INSERT INTO workbench_executions(session_id, command_payload, returncode, stdout, stderr, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?)'
                ),
                (session_id, self._encode(command), returncode, stdout, stderr, self._now()),
            )
            connection.commit()

    def create_federated_task(
        self,
        task_id: str,
        export_name: str,
        target_type: str,
        status: str,
        input_payload: dict[str, Any],
    ) -> None:
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute(
                (
                    'INSERT INTO federated_tasks('
                    'task_id, export_name, target_type, status, input_payload, response_payload, error_message, '
                    'local_run_id, request_id, subscribers_payload, created_at, updated_at'
                    ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
                ),
                (task_id, export_name, target_type, status, self._encode(input_payload), None, None, None, None, self._encode([]), now, now),
            )
            connection.commit()

    def load_federated_task(self, task_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                (
                    'SELECT export_name, target_type, status, input_payload, response_payload, error_message, '
                    'local_run_id, request_id, subscribers_payload, created_at, updated_at '
                    'FROM federated_tasks WHERE task_id = ?'
                ),
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f'Federated task not found: {task_id}')
        return {
            'task_id': task_id,
            'export_name': row[0],
            'target_type': row[1],
            'status': row[2],
            'input_payload': cast(dict[str, Any], self._decode(row[3])),
            'response_payload': cast(dict[str, Any] | None, self._decode(row[4])),
            'error_message': row[5],
            'local_run_id': row[6],
            'request_id': row[7],
            'subscribers': cast(list[str], self._decode(row[8]) or []),
            'created_at': row[9],
            'updated_at': row[10],
        }

    def list_federated_tasks(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute('SELECT task_id FROM federated_tasks ORDER BY created_at ASC').fetchall()
        return [self.load_federated_task(str(row[0])) for row in rows]

    def update_federated_task(self, task_id: str, **changes: Any) -> None:
        mapping = {
            'status': 'status',
            'response_payload': 'response_payload',
            'error_message': 'error_message',
            'local_run_id': 'local_run_id',
            'request_id': 'request_id',
            'updated_at': 'updated_at',
            'subscribers': 'subscribers_payload',
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, column in mapping.items():
            if key not in changes:
                continue
            assignments.append(f'{column} = ?')
            value = changes[key]
            if key in {'response_payload', 'subscribers'}:
                value = self._encode(value)
            params.append(value)
        if not assignments:
            return
        params.append(task_id)
        with closing(self._connect()) as connection:
            connection.execute(
                f'UPDATE federated_tasks SET {", ".join(assignments)} WHERE task_id = ?',
                params,
            )
            connection.commit()

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
            'run_kind': run_row['run_kind'],
            'status': run_row['status'],
            'session_id': run_row['session_id'],
            'input_payload': run_row['input_payload'],
            'output_payload': run_row['output_payload'],
            'created_at': run_row['created_at'],
            'lineage': {
                'parent_run_id': run_row['parent_run_id'],
                'source_run_id': run_row['source_run_id'],
                'source_checkpoint_id': run_row['source_checkpoint_id'],
                'resume_strategy': run_row['resume_strategy'],
                'child_runs': self.list_child_runs(run_id),
            },
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
            'human_requests': [item.model_dump() for item in self.list_human_requests(run_id=run_id)],
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
            event_id=uuid4().hex,
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
    def _encode(payload: Any) -> str | None:
        if payload is None:
            return None
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




