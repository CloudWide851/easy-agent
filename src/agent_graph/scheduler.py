from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio

from agent_common.models import (
    ChatMessage,
    HumanLoopMode,
    NodeStatus,
    NodeType,
    RunContext,
    RunStatus,
)
from agent_common.tools import ToolRegistry
from agent_config.app import AppConfig, GraphNodeConfig
from agent_graph.orchestrator import AgentOrchestrator
from agent_integrations.federation import FederationClientManager
from agent_integrations.guardrails import GuardrailEngine
from agent_integrations.human_loop import ApprovalRequired, HumanLoopManager, RunInterrupted
from agent_integrations.storage import SQLiteRunStore
from agent_integrations.tool_validation import normalize_and_validate_tool_arguments
from agent_integrations.workbench import WorkbenchManager


class GraphScheduler:
    def __init__(
        self,
        config: AppConfig,
        registry: ToolRegistry,
        orchestrator: AgentOrchestrator,
        store: SQLiteRunStore,
        mcp_manager: Any,
        guardrail_engine: GuardrailEngine,
        human_loop: HumanLoopManager | None = None,
        *,
        workbench_manager: WorkbenchManager | None = None,
        federation_manager: FederationClientManager | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.orchestrator = orchestrator
        self.store = store
        self.mcp_manager = mcp_manager
        self.guardrail_engine = guardrail_engine
        self.human_loop = human_loop or HumanLoopManager(store, config.security.human_loop)
        self.workbench_manager = workbench_manager
        self.federation_manager = federation_manager

    async def run(
        self,
        input_text: str,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        self.store.create_run(run_id, self.config.graph.name, {'input': input_text}, session_id=session_id)
        self.store.record_event(
            run_id,
            'run_started',
            {'graph_name': self.config.graph.name, 'input': input_text, 'session_id': session_id},
            scope='run',
            span_id=f'run:{run_id}',
        )
        return await self._execute_run(
            run_id,
            lambda target_run_id: self._run_internal(target_run_id, input_text, session_id, approval_mode),
        )

    async def run_agent_target(
        self,
        agent_name: str,
        input_text: str,
        *,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        self.store.create_run(run_id, agent_name, {'input': input_text}, session_id=session_id, run_kind='agent')
        self.store.record_event(
            run_id,
            'run_started',
            {'graph_name': agent_name, 'input': input_text, 'session_id': session_id, 'run_kind': 'agent'},
            scope='run',
            span_id=f'run:{run_id}',
        )
        return await self._execute_run(
            run_id,
            lambda target_run_id: self._run_named_agent(target_run_id, agent_name, input_text, session_id, approval_mode),
        )

    async def run_team_target(
        self,
        team_name: str,
        input_text: str,
        *,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        self.store.create_run(run_id, team_name, {'input': input_text}, session_id=session_id, run_kind='team')
        self.store.record_event(
            run_id,
            'run_started',
            {'graph_name': team_name, 'input': input_text, 'session_id': session_id, 'run_kind': 'team'},
            scope='run',
            span_id=f'run:{run_id}',
        )
        return await self._execute_run(
            run_id,
            lambda target_run_id: self._run_named_team(target_run_id, team_name, input_text, session_id, approval_mode),
        )

    async def resume(
        self,
        run_id: str,
        checkpoint_id: int | None = None,
        *,
        fork: bool = False,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        run_payload = self.store.load_run(run_id)
        checkpoint = self.store.load_checkpoint(run_id, checkpoint_id) if checkpoint_id is not None else self.store.load_latest_checkpoint(run_id)
        if checkpoint is None:
            raise RuntimeError(f"Run '{run_id}' does not have a resumable checkpoint")
        if not fork and run_payload['status'] == RunStatus.SUCCEEDED.value:
            raise RuntimeError(f"Run '{run_id}' has already succeeded")
        target_run_id = run_id
        active_checkpoint = dict(checkpoint)
        if fork:
            target_run_id = uuid.uuid4().hex
            self.store.create_run(
                target_run_id,
                run_payload['graph_name'],
                run_payload['input_payload'],
                session_id=run_payload['session_id'],
                run_kind=run_payload['run_kind'],
                parent_run_id=run_id,
                source_run_id=run_id,
                source_checkpoint_id=checkpoint['checkpoint_id'],
                resume_strategy='fork',
            )
            payload = dict(active_checkpoint['payload'])
            if self.workbench_manager is not None:
                payload['workbench'] = self.workbench_manager.clone_manifest(target_run_id, dict(payload.get('workbench', {})))
            active_checkpoint['payload'] = payload
            self.store.record_event(
                target_run_id,
                'run_started',
                {
                    'graph_name': run_payload['graph_name'],
                    'source_run_id': run_id,
                    'source_checkpoint_id': checkpoint['checkpoint_id'],
                    'resume_strategy': 'fork',
                },
                scope='run',
                span_id=f'run:{target_run_id}',
            )
        else:
            self.store.mark_run_running(target_run_id)
        self.store.record_event(
            target_run_id,
            'run_resumed',
            {
                'checkpoint_kind': checkpoint['kind'],
                'checkpoint_id': checkpoint['checkpoint_id'],
                'source_run_id': run_id,
                'fork': fork,
            },
            scope='run',
            span_id=f'run:{target_run_id}',
        )
        input_text = str(run_payload['input_payload'].get('input', ''))
        session_id = run_payload['session_id']
        return await self._execute_run(
            target_run_id,
            lambda active_run_id: self._resume_from_checkpoint(
                active_run_id,
                active_checkpoint,
                input_text=input_text,
                session_id=session_id,
                approval_mode=approval_mode,
            ),
        )

    async def replay(self, run_id: str, checkpoint_id: int) -> dict[str, Any]:
        checkpoint = self.store.load_checkpoint(run_id, checkpoint_id)
        if checkpoint is None:
            raise RuntimeError(f"Checkpoint '{checkpoint_id}' not found for run '{run_id}'")
        payload = checkpoint['payload']
        body: dict[str, Any] = {
            'run_id': run_id,
            'checkpoint_id': checkpoint_id,
            'checkpoint_kind': checkpoint['kind'],
            'created_at': checkpoint['created_at'],
        }
        if checkpoint['kind'] == 'graph':
            body['state'] = {
                'results': payload.get('results', {}),
                'remaining': payload.get('remaining', []),
                'shared_state': payload.get('shared_state', {}),
                'workbench': payload.get('workbench', {}),
            }
        elif checkpoint['kind'] == 'team':
            body['state'] = {
                'team': payload.get('team'),
                'turns': payload.get('turns', []),
                'shared_messages': payload.get('shared_messages', []),
                'current_speaker': payload.get('current_speaker'),
                'next_turn_index': payload.get('next_turn_index'),
                'shared_state': payload.get('shared_state', {}),
                'workbench': payload.get('workbench', {}),
            }
        elif checkpoint['kind'] == 'agent':
            body['state'] = payload
        else:
            body['state'] = payload
        return body

    def list_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.list_checkpoints(run_id)

    async def _execute_run(self, run_id: str, runner: Callable[[str], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        try:
            output: dict[str, Any] = await runner(run_id)
        except ApprovalRequired as exc:
            waiting = {'run_id': run_id, 'status': RunStatus.WAITING_APPROVAL.value, 'request_id': exc.request.request_id}
            self.store.mark_run_waiting_approval(run_id, waiting)
            self.store.record_event(
                run_id,
                'run_waiting_approval',
                waiting,
                scope='run',
                span_id=f'run:{run_id}',
            )
            return waiting
        except RunInterrupted as exc:
            interrupted = {'run_id': run_id, 'status': RunStatus.INTERRUPTED.value, 'payload': exc.payload}
            self.store.mark_run_interrupted(run_id, interrupted)
            self.store.record_event(
                run_id,
                'run_interrupted',
                interrupted,
                scope='run',
                span_id=f'run:{run_id}',
            )
            return interrupted
        except Exception as exc:
            failure = {'error': str(exc)}
            self.store.finish_run(run_id, RunStatus.FAILED.value, failure)
            self.store.record_event(
                run_id,
                'run_failed',
                failure,
                scope='run',
                span_id=f'run:{run_id}',
            )
            raise RuntimeError(f'Run {run_id} failed: {exc}') from exc
        self.store.finish_run(run_id, RunStatus.SUCCEEDED.value, output)
        self.store.record_event(
            run_id,
            'run_succeeded',
            {'result': output},
            scope='run',
            span_id=f'run:{run_id}',
        )
        return output

    async def _resume_from_checkpoint(
        self,
        run_id: str,
        checkpoint: dict[str, Any],
        *,
        input_text: str,
        session_id: str | None,
        approval_mode: HumanLoopMode,
    ) -> dict[str, Any]:
        if checkpoint['kind'] == 'graph':
            payload = checkpoint['payload']
            shared_state = dict(payload.get('shared_state', {}))
            shared_state['input'] = input_text
            output = await self._run_graph_flow(
                run_id=run_id,
                input_text=input_text,
                shared_state=shared_state,
                session_id=session_id,
                results=payload.get('results', {}),
                remaining=payload.get('remaining', []),
                checkpoint_initial=False,
                approval_mode=approval_mode,
            )
            if session_id is not None:
                self.store.save_session_state(session_id, self.config.graph.name, shared_state)
            return self._apply_final_output_guardrails(output, run_id)
        if checkpoint['kind'] == 'team':
            payload = checkpoint['payload']
            shared_state = dict(payload.get('shared_state', {}))
            shared_state['input'] = input_text
            context = RunContext(
                run_id=run_id,
                workdir=Path.cwd(),
                node_id=None,
                shared_state=shared_state,
                session_id=session_id,
                approval_mode=approval_mode,
            )
            result = await self.orchestrator.run_team_stateful(
                payload['team'],
                input_text,
                context,
                restored_state=payload,
                checkpointing=True,
            )
            if session_id is not None:
                self.store.save_session_messages(session_id, self.config.graph.name, result.shared_messages)
            return self._apply_final_output_guardrails(self._build_output(run_id, result.payload, session_id=session_id), run_id)
        if checkpoint['kind'] == 'agent':
            payload = checkpoint['payload']
            shared_state = dict(payload.get('shared_state', {}))
            shared_state['input'] = input_text
            messages = [ChatMessage.model_validate(item) for item in payload.get('shared_messages', [])]
            context = RunContext(
                run_id=run_id,
                workdir=Path.cwd(),
                node_id=None,
                shared_state=shared_state,
                session_id=session_id,
                approval_mode=approval_mode,
            )
            agent_result = await self.orchestrator.run_agent_with_messages(payload['agent'], messages, context)
            if session_id is not None:
                self.store.save_session_messages(session_id, self.config.graph.name, agent_result.shared_messages)
            return self._apply_final_output_guardrails(self._build_output(run_id, agent_result.text, session_id=session_id), run_id)
        raise RuntimeError(f"Unsupported checkpoint kind: {checkpoint['kind']}")

    async def _run_internal(
        self,
        run_id: str,
        input_text: str,
        session_id: str | None,
        approval_mode: HumanLoopMode,
    ) -> dict[str, Any]:
        if self.config.graph.entrypoint in self.config.agent_map and not self.config.graph.nodes:
            return await self._run_named_agent(run_id, self.config.graph.entrypoint, input_text, session_id, approval_mode)
        if self.config.graph.entrypoint in self.config.team_map and not self.config.graph.nodes:
            return await self._run_named_team(run_id, self.config.graph.entrypoint, input_text, session_id, approval_mode)
        shared_state = self.store.load_session_state(session_id) if session_id is not None else {}
        shared_state = dict(shared_state)
        shared_state['input'] = input_text
        output = await self._run_graph_flow(run_id, input_text, shared_state, session_id, approval_mode=approval_mode)
        if session_id is not None:
            self.store.save_session_state(session_id, self.config.graph.name, shared_state)
        return self._apply_final_output_guardrails(output, run_id)

    async def _run_named_agent(
        self,
        run_id: str,
        agent_name: str,
        input_text: str,
        session_id: str | None,
        approval_mode: HumanLoopMode,
    ) -> dict[str, Any]:
        shared_messages = []
        if session_id is not None:
            shared_messages.extend(self.store.load_session_messages(session_id))
        shared_messages.append(ChatMessage(role='user', content=input_text))
        self.store.create_checkpoint(
            run_id,
            'agent',
            {
                'agent': agent_name,
                'shared_messages': [message.model_dump() for message in shared_messages],
                'shared_state': {'input': input_text},
                'workbench': self._workbench_manifest(run_id),
            },
        )
        context = RunContext(
            run_id=run_id,
            workdir=Path.cwd(),
            node_id=None,
            shared_state={'input': input_text},
            session_id=session_id,
            approval_mode=approval_mode,
        )
        result = await self.orchestrator.run_agent_with_messages(agent_name, shared_messages, context)
        if session_id is not None:
            self.store.save_session_messages(session_id, self.config.graph.name, result.shared_messages)
        return self._apply_final_output_guardrails(self._build_output(run_id, result.text, session_id=session_id), run_id)

    async def _run_named_team(
        self,
        run_id: str,
        team_name: str,
        input_text: str,
        session_id: str | None,
        approval_mode: HumanLoopMode,
    ) -> dict[str, Any]:
        shared_messages = []
        if session_id is not None:
            shared_messages.extend(self.store.load_session_messages(session_id))
        shared_messages.append(ChatMessage(role='user', content=input_text))
        context = RunContext(
            run_id=run_id,
            workdir=Path.cwd(),
            node_id=None,
            shared_state={'input': input_text},
            session_id=session_id,
            approval_mode=approval_mode,
        )
        result = await self.orchestrator.run_team_stateful(
            team_name,
            input_text,
            context,
            initial_messages=shared_messages,
            checkpointing=True,
        )
        if session_id is not None:
            self.store.save_session_messages(session_id, self.config.graph.name, result.shared_messages)
        return self._apply_final_output_guardrails(self._build_output(run_id, result.payload, session_id=session_id), run_id)

    async def _run_graph_flow(
        self,
        run_id: str,
        input_text: str,
        shared_state: dict[str, Any],
        session_id: str | None,
        results: dict[str, Any] | None = None,
        remaining: list[str] | None = None,
        checkpoint_initial: bool = True,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        nodes = {node.id: node for node in self.config.graph.nodes}
        graph_results = dict(results or {})
        graph_remaining = set(nodes) if remaining is None else set(remaining)
        context = RunContext(
            run_id=run_id,
            workdir=Path.cwd(),
            node_id=None,
            shared_state=shared_state,
            session_id=session_id,
            approval_mode=approval_mode,
        )
        if checkpoint_initial:
            self.store.create_checkpoint(
                run_id,
                'graph',
                {
                    'results': graph_results,
                    'remaining': sorted(graph_remaining),
                    'shared_state': context.shared_state,
                    'workbench': self._workbench_manifest(run_id),
                },
            )
        while graph_remaining:
            ready = [nodes[node_id] for node_id in graph_remaining if all(dep in graph_results for dep in nodes[node_id].deps)]
            if not ready:
                raise RuntimeError('Graph contains unresolved dependencies or a cycle')
            for node in ready:
                await self.human_loop.check_interrupt(context, f'graph_node:{node.id}')
                output = await self._execute_node(node, graph_results, context)
                graph_results[node.id] = output
                shared_state[node.id] = output
                graph_remaining.remove(node.id)
                self.store.create_checkpoint(
                    run_id,
                    'graph',
                    {
                        'results': graph_results,
                        'remaining': sorted(graph_remaining),
                        'shared_state': context.shared_state,
                        'workbench': self._workbench_manifest(run_id),
                    },
                )
        final_output = graph_results[self.config.graph.entrypoint]
        return self._build_output(run_id, final_output, nodes=graph_results, session_id=session_id)

    async def _execute_node(
        self,
        node: GraphNodeConfig,
        results: dict[str, Any],
        parent_context: RunContext,
    ) -> Any:
        template_values = {**parent_context.shared_state, **results}
        prompt = node.input_template.format(**template_values)
        node_context = RunContext(
            run_id=parent_context.run_id,
            workdir=parent_context.workdir,
            node_id=node.id,
            shared_state=parent_context.shared_state,
            depth=parent_context.depth,
            session_id=parent_context.session_id,
            approval_mode=parent_context.approval_mode,
            workbench_session_id=parent_context.workbench_session_id,
        )
        last_error: Exception | None = None
        for attempt in range(node.retries + 1):
            self.store.record_node(parent_context.run_id, node.id, NodeStatus.RUNNING.value, attempt + 1, None, None)
            try:
                with anyio.fail_after(node.timeout_seconds):
                    output = await self._dispatch_node(node, prompt, node_context)
                self.store.record_node(parent_context.run_id, node.id, NodeStatus.SUCCEEDED.value, attempt + 1, output, None)
                return output
            except Exception as exc:
                last_error = exc
                self.store.record_node(parent_context.run_id, node.id, NodeStatus.FAILED.value, attempt + 1, None, str(exc))
        if last_error is None:
            raise RuntimeError(f"Node '{node.id}' failed without an exception")
        raise last_error

    async def _dispatch_node(self, node: GraphNodeConfig, prompt: str, context: RunContext) -> Any:
        if node.type is NodeType.AGENT:
            if node.target is None:
                raise ValueError('Agent node requires target')
            return await self.orchestrator.run_agent(node.target, prompt, context)
        if node.type is NodeType.TEAM:
            if node.target is None:
                raise ValueError('Team node requires target')
            return await self.orchestrator.run_team(node.target, prompt, context)
        if node.type in (NodeType.TOOL, NodeType.SKILL):
            if node.target is None:
                raise ValueError('Tool/skill node requires target')
            payload = {'prompt': prompt, **node.arguments}
            tool_spec = self.registry.get_spec(node.target)
            validation = normalize_and_validate_tool_arguments(tool_spec.input_schema, payload)
            if validation.errors:
                raise RuntimeError(f"Node '{node.id}' tool validation failed: {'; '.join(validation.errors)}")
            if self.human_loop.is_sensitive_tool(node.target):
                await self.human_loop.require_approval(
                    context,
                    request_key=f'graph_node_tool:{node.id}:{node.target}:{self.human_loop.stable_key(validation.normalized)}',
                    kind='tool',
                    title=f'Approve sensitive tool {node.target}',
                    payload={'tool_name': node.target, 'arguments': validation.normalized, 'node_id': node.id},
                )
            await self.human_loop.check_interrupt(context, f'graph_node_tool:{node.id}:{node.target}')
            decisions = self.guardrail_engine.check_tool_input(node.target, validation.normalized, context)
            for decision in decisions:
                self.store.record_event(
                    context.run_id,
                    'tool_guardrail_result',
                    {
                        'tool_name': node.target,
                        'guardrail': decision.guardrail,
                        'outcome': decision.outcome,
                        'reason': decision.reason,
                        'payload': decision.payload,
                    },
                    scope='guardrail',
                    node_id=context.node_id,
                    span_id=f'guardrail:{decision.guardrail}',
                    parent_span_id=f'node:{node.id}',
                )
            self.guardrail_engine.ensure_allowed('tool_input', decisions)
            self.store.record_event(
                context.run_id,
                'tool_call_started',
                {'tool_name': node.target, 'arguments': validation.normalized, 'source': node.type.value},
                scope='tool',
                node_id=context.node_id,
                span_id=f'tool:{node.target}',
                parent_span_id=f'node:{node.id}',
            )
            try:
                result = await self.registry.call(node.target, validation.normalized, context)
            except Exception as exc:
                self.store.record_event(
                    context.run_id,
                    'tool_call_failed',
                    {'tool_name': node.target, 'arguments': validation.normalized, 'error': str(exc), 'source': node.type.value},
                    scope='tool',
                    node_id=context.node_id,
                    span_id=f'tool:{node.target}',
                    parent_span_id=f'node:{node.id}',
                )
                raise
            self.store.record_event(
                context.run_id,
                'tool_call_succeeded',
                {'tool_name': node.target, 'arguments': validation.normalized, 'result': result, 'source': node.type.value},
                scope='tool',
                node_id=context.node_id,
                span_id=f'tool:{node.target}',
                parent_span_id=f'node:{node.id}',
            )
            return result
        if node.type is NodeType.MCP_TOOL:
            if node.target is None or '/' not in node.target:
                raise ValueError("mcp_tool target must be in the format 'server/tool'")
            server_name, tool_name = node.target.split('/', 1)
            payload = {'prompt': prompt, **node.arguments}
            if self.human_loop.is_sensitive_tool(node.target) or self.human_loop.is_sensitive_tool(tool_name):
                await self.human_loop.require_approval(
                    context,
                    request_key=f'mcp_tool:{node.id}:{server_name}:{tool_name}:{self.human_loop.stable_key(payload)}',
                    kind='tool',
                    title=f'Approve sensitive MCP tool {server_name}/{tool_name}',
                    payload={'server': server_name, 'tool_name': tool_name, 'arguments': payload, 'node_id': node.id},
                )
            await self.human_loop.check_interrupt(context, f'graph_node_mcp:{node.id}:{server_name}:{tool_name}')
            decisions = self.guardrail_engine.check_tool_input(node.target, payload, context)
            for decision in decisions:
                self.store.record_event(
                    context.run_id,
                    'tool_guardrail_result',
                    {
                        'tool_name': node.target,
                        'guardrail': decision.guardrail,
                        'outcome': decision.outcome,
                        'reason': decision.reason,
                        'payload': decision.payload,
                    },
                    scope='guardrail',
                    node_id=context.node_id,
                    span_id=f'guardrail:{decision.guardrail}',
                    parent_span_id=f'node:{node.id}',
                )
            self.guardrail_engine.ensure_allowed('tool_input', decisions)
            return await self.mcp_manager.call_tool(server_name, tool_name, payload, context=context)
        if node.type is NodeType.FEDERATED:
            if node.target is None or '/' not in node.target:
                raise ValueError("federated target must be in the format 'remote/export'")
            if self.federation_manager is None:
                raise RuntimeError('Federation manager is not configured')
            remote_name, export_name = node.target.split('/', 1)
            return await self.federation_manager.run_remote(
                remote_name,
                export_name,
                prompt,
                session_id=context.session_id,
                metadata={'node_id': node.id, 'arguments': node.arguments},
            )
        if node.type is NodeType.JOIN:
            return {dep: context.shared_state[dep] for dep in node.deps}
        raise ValueError(f'Unsupported node type: {node.type}')

    def _apply_final_output_guardrails(self, output: dict[str, Any], run_id: str) -> dict[str, Any]:
        context = RunContext(run_id=run_id, workdir=Path.cwd(), node_id=None, shared_state={})
        decisions = self.guardrail_engine.check_final_output(output.get('result'), context)
        for decision in decisions:
            self.store.record_event(
                run_id,
                'output_guardrail_result',
                {
                    'guardrail': decision.guardrail,
                    'outcome': decision.outcome,
                    'reason': decision.reason,
                    'payload': decision.payload,
                },
                scope='guardrail',
                span_id=f'guardrail:{decision.guardrail}',
                parent_span_id=f'run:{run_id}',
            )
        self.guardrail_engine.ensure_allowed('final_output', decisions)
        return output

    def _workbench_manifest(self, run_id: str) -> dict[str, Any]:
        if self.workbench_manager is None:
            return {'sessions': []}
        return self.workbench_manager.snapshot_manifest(run_id)

    @staticmethod
    def _build_output(
        run_id: str,
        result: Any,
        nodes: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {'run_id': run_id, 'result': result, 'status': RunStatus.SUCCEEDED.value}
        if nodes is not None:
            payload['nodes'] = nodes
        if session_id is not None:
            payload['session_id'] = session_id
        return payload
