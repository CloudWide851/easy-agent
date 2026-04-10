from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any


def encode_payload(payload: Any) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False)


def decode_payload(payload: str | None) -> Any:
    if not payload:
        return None
    return json.loads(payload)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def upsert_session(connection: sqlite3.Connection, session_id: str, graph_name: str, updated_at: str) -> None:
    connection.execute(
        """
        INSERT INTO sessions(session_id, graph_name, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            graph_name = excluded.graph_name,
            updated_at = excluded.updated_at
        """,
        (session_id, graph_name, updated_at, updated_at),
    )
