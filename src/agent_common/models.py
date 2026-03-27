from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class Protocol(StrEnum):
    AUTO = 'auto'
    OPENAI = 'openai'
    ANTHROPIC = 'anthropic'
    GEMINI = 'gemini'


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    role: Literal['system', 'user', 'assistant', 'tool']
    content: str = ''
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class AssistantResponse(BaseModel):
    text: str = ''
    tool_calls: list[ToolCall] = Field(default_factory=list)
    protocol: Protocol
    raw: dict[str, Any] = Field(default_factory=dict)


class RuntimeEvent(BaseModel):
    event_id: str
    sequence: int
    run_id: str
    timestamp: str
    kind: str
    scope: str
    payload: dict[str, Any] = Field(default_factory=dict)
    span_id: str | None = None
    parent_span_id: str | None = None
    node_id: str | None = None


class RunStatus(StrEnum):
    RUNNING = 'running'
    WAITING_APPROVAL = 'waiting_approval'
    INTERRUPTED = 'interrupted'
    FAILED = 'failed'
    SUCCEEDED = 'succeeded'


class HumanLoopMode(StrEnum):
    DEFERRED = 'deferred'
    INLINE = 'inline'
    HYBRID = 'hybrid'


class HumanRequestStatus(StrEnum):
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'


class GuardrailDecision(BaseModel):
    outcome: Literal['allow', 'block']
    guardrail: str
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)


class NodeType(StrEnum):
    AGENT = 'agent'
    TEAM = 'team'
    TOOL = 'tool'
    SKILL = 'skill'
    MCP_TOOL = 'mcp_tool'
    FEDERATED = 'federated'
    JOIN = 'join'


class TeamMode(StrEnum):
    ROUND_ROBIN = 'round_robin'
    SELECTOR = 'selector'
    SWARM = 'swarm'


class NodeStatus(StrEnum):
    PENDING = 'pending'
    RUNNING = 'running'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    SKIPPED = 'skipped'


class McpAuthType(StrEnum):
    NONE = 'none'
    BEARER_ENV = 'bearer_env'
    HEADER_ENV = 'header_env'
    OAUTH = 'oauth'


@dataclass(slots=True)
class RunContext:
    run_id: str
    workdir: Path
    node_id: str | None
    shared_state: dict[str, Any] = field(default_factory=dict)
    depth: int = 0
    session_id: str | None = None
    approval_mode: HumanLoopMode = HumanLoopMode.HYBRID
    workbench_session_id: str | None = None


class HumanRequest(BaseModel):
    request_id: str
    run_id: str
    request_key: str
    kind: str
    status: HumanRequestStatus
    title: str
    payload: dict[str, Any] = Field(default_factory=dict)
    response_payload: dict[str, Any] | None = None
    created_at: str
    resolved_at: str | None = None
