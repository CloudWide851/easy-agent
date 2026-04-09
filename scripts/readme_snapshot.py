from __future__ import annotations

import json
from pathlib import Path
from typing import Any

COMPARISON_ROWS: list[dict[str, str]] = [
    {
        'project': 'easy-agent',
        'basis': 'repo-local tested evidence',
        'sessions_memory': 'session_id + session_messages + session_state + harness_state',
        'handoffs_teams': 'agent teams, graph handoff, harness worker routing',
        'guardrails': 'tool-input and final-output hooks',
        'resume_replay': 'resume, replay, fork, checkpoints',
        'tool_calling': 'OpenAI-compatible strict function calling + Exa-backed web search eval + provider schema matrix',
        'isolation': 'process / container / microvm workbench executors',
        'evals': 'repo-pinned BFCL + official_full_v4 resume path + tau2 + real-network telemetry',
    },
    {
        'project': 'OpenAI Agents SDK',
        'basis': 'official docs mapping',
        'sessions_memory': 'sessions documented',
        'handoffs_teams': 'handoffs documented',
        'guardrails': 'guardrails documented',
        'resume_replay': 'not positioned as graph replay/time-travel runtime',
        'tool_calling': 'official function calling / structured outputs surface',
        'isolation': 'not a first-class executor matrix in docs',
        'evals': 'no BFCL-style built-in public eval surface in docs',
    },
    {
        'project': 'AutoGen',
        'basis': 'official docs mapping',
        'sessions_memory': 'memory and state patterns documented',
        'handoffs_teams': 'teams documented',
        'guardrails': 'policy / approval patterns documented',
        'resume_replay': 'stateful workflows documented, less replay-centric than easy-agent',
        'tool_calling': 'multi-agent tool use documented',
        'isolation': 'code execution documented',
        'evals': 'no repo-local BFCL-style matrix in docs',
    },
    {
        'project': 'LangGraph',
        'basis': 'official docs mapping',
        'sessions_memory': 'state persistence documented',
        'handoffs_teams': 'graph routing documented',
        'guardrails': 'app-defined guardrail patterns',
        'resume_replay': 'durable execution and time-travel documented',
        'tool_calling': 'tool-calling graph patterns documented',
        'isolation': 'not a built-in container / microvm executor matrix in docs',
        'evals': 'no built-in BFCL / real-network matrix in docs',
    },
]


def load_json(path: str | Path) -> dict[str, Any]:
    return dict(json.loads(Path(path).read_text(encoding='utf-8')))


def comparison_rows() -> list[dict[str, str]]:
    return list(COMPARISON_ROWS)
