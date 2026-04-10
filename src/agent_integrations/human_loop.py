from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from agent_common.models import HumanLoopMode, HumanRequest, HumanRequestStatus, RunContext
from agent_config.app import HumanLoopConfig
from agent_integrations.storage import SQLiteRunStore

InlineApprovalResolver = Callable[[HumanRequest], Awaitable[tuple[HumanRequestStatus, dict[str, Any] | None]]]


class ApprovalRequired(RuntimeError):
    def __init__(self, request: HumanRequest) -> None:
        super().__init__(f"Run '{request.run_id}' is waiting for approval request '{request.request_id}'")
        self.request = request


class RunInterrupted(RuntimeError):
    def __init__(self, run_id: str, payload: dict[str, Any]) -> None:
        reason = str(payload.get('reason') or 'interrupted')
        super().__init__(f"Run '{run_id}' interrupted: {reason}")
        self.run_id = run_id
        self.payload = payload


class HumanLoopManager:
    def __init__(self, store: SQLiteRunStore, config: HumanLoopConfig) -> None:
        self.store = store
        self.config = config
        self._inline_resolver: InlineApprovalResolver | None = None

    def set_inline_resolver(self, resolver: InlineApprovalResolver | None) -> None:
        self._inline_resolver = resolver

    def is_sensitive_tool(self, tool_name: str) -> bool:
        return tool_name in set(self.config.sensitive_tools)

    async def check_interrupt(self, context: RunContext, point: str) -> None:
        if not self.config.interruptible:
            return
        payload = self.store.consume_interrupt(context.run_id)
        if payload is None:
            return
        payload = {'point': point, **payload}
        self.store.record_event(
            context.run_id,
            'run_interrupt_consumed',
            payload,
            scope='human',
            node_id=context.node_id,
            span_id=f'human:interrupt:{point}',
        )
        raise RunInterrupted(context.run_id, payload)

    async def require_approval(
        self,
        context: RunContext,
        *,
        request_key: str,
        kind: str,
        title: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = self.store.load_human_request_by_key(context.run_id, request_key)
        if request is None:
            request = self.store.create_human_request(context.run_id, request_key, kind, title, payload)
            self.store.record_event(
                context.run_id,
                'human_request_created',
                request.model_dump(),
                scope='human',
                node_id=context.node_id,
                span_id=f'human:{kind}:{request.request_id}',
            )
        if request.status is HumanRequestStatus.APPROVED:
            return request.response_payload or {}
        if request.status is HumanRequestStatus.REJECTED:
            raise RunInterrupted(context.run_id, {'reason': f"approval rejected: {request.title}", 'request_id': request.request_id})
        if request.status is HumanRequestStatus.CANCELLED:
            raise RunInterrupted(context.run_id, {'reason': f"approval cancelled: {request.title}", 'request_id': request.request_id})
        mode = self._effective_mode(context.approval_mode)
        if mode is HumanLoopMode.INLINE and self._inline_resolver is not None:
            status, response_payload = await self._inline_resolver(request)
            response_payload = normalize_human_request_resolution(request, status=status, response_payload=response_payload)
            resolved = self.store.resolve_human_request(
                request.request_id,
                status=status,
                response_payload=response_payload,
            )
            self.store.record_event(
                context.run_id,
                'human_request_resolved',
                resolved.model_dump(),
                scope='human',
                node_id=context.node_id,
                span_id=f'human:{kind}:{request.request_id}',
            )
            if status is HumanRequestStatus.APPROVED:
                return response_payload or {}
            reason = 'approval cancelled' if status is HumanRequestStatus.CANCELLED else 'approval rejected'
            raise RunInterrupted(context.run_id, {'reason': f"{reason}: {resolved.title}", 'request_id': resolved.request_id})
        raise ApprovalRequired(request)

    def approval_payload(self, **payload: Any) -> dict[str, Any]:
        return payload

    @staticmethod
    def stable_key(*parts: Any) -> str:
        encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha1(encoded.encode('utf-8')).hexdigest()
        return digest

    def _effective_mode(self, requested: HumanLoopMode) -> HumanLoopMode:
        configured = self.config.mode
        if configured is HumanLoopMode.DEFERRED:
            return HumanLoopMode.DEFERRED
        if configured is HumanLoopMode.INLINE:
            return HumanLoopMode.INLINE
        if requested is HumanLoopMode.DEFERRED:
            return HumanLoopMode.DEFERRED
        return HumanLoopMode.INLINE if self._inline_resolver is not None else HumanLoopMode.DEFERRED


def normalize_human_request_resolution(
    request: HumanRequest,
    *,
    status: HumanRequestStatus,
    response_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if request.kind != 'mcp_elicitation':
        return response_payload
    payload = dict(response_payload or {})
    mode = str(request.payload.get('mode') or '').lower()
    if status is HumanRequestStatus.APPROVED:
        payload['action'] = 'accept'
        if mode == 'url':
            completion = dict(payload.get('completion') or {})
            completion.setdefault('status', 'pending')
            completion.setdefault('elicitation_id', request.payload.get('elicitation_id'))
            payload['completion'] = completion
        return payload
    if status is HumanRequestStatus.REJECTED:
        return {'action': 'decline'}
    if status is HumanRequestStatus.CANCELLED:
        return {'action': 'cancel'}
    return payload
