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
        'tool_calling': 'OpenAI-compatible strict function calling + SerpApi-backed web search eval + provider schema matrix',
        'isolation': 'process / container / microvm workbench executors',
        'evals': 'repo-pinned BFCL + official_full_v4 resume path + tau2 + real-network telemetry',
    },
    {
        'project': 'OpenHands',
        'basis': 'official docs mapping',
        'sessions_memory': 'conversation and state surfaces documented',
        'handoffs_teams': 'task delegation and multi-agent collaboration documented',
        'guardrails': 'human approval and sandbox controls documented',
        'resume_replay': 'persistent task continuation documented, not a replay-first runtime',
        'tool_calling': 'coding-agent tool and browser actions documented',
        'isolation': 'sandbox/runtime isolation documented',
        'evals': 'no BFCL-style built-in public eval matrix in docs',
    },
    {
        'project': 'Skyvern',
        'basis': 'official docs mapping',
        'sessions_memory': 'workflow and run history documented',
        'handoffs_teams': 'browser workflow orchestration documented',
        'guardrails': 'approval and website action controls documented',
        'resume_replay': 'workflow rerun and recovery documented, less checkpoint-centric',
        'tool_calling': 'browser and action execution documented',
        'isolation': 'hosted browser/runtime boundary documented',
        'evals': 'no BFCL-style public eval matrix in docs',
    },
    {
        'project': 'AutoGPT Platform',
        'basis': 'official docs mapping',
        'sessions_memory': 'agents, workflows, and run state documented',
        'handoffs_teams': 'agent and workflow composition documented',
        'guardrails': 'workflow controls and approvals documented',
        'resume_replay': 'workflow reruns documented, not a graph replay runtime',
        'tool_calling': 'agent blocks and integrations documented',
        'isolation': 'platform execution boundary documented',
        'evals': 'no BFCL-style built-in public eval matrix in docs',
    },
]


def load_json(path: str | Path) -> dict[str, Any]:
    return dict(json.loads(Path(path).read_text(encoding='utf-8')))


def comparison_rows() -> list[dict[str, str]]:
    return list(COMPARISON_ROWS)
