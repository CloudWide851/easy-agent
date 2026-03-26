from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import anyio

from agent_common.models import ChatMessage, NodeStatus, NodeType, RunContext
from agent_common.tools import ToolRegistry
from agent_config.app import AppConfig, GraphNodeConfig
from agent_graph.orchestrator import AgentOrchestrator
from agent_integrations.guardrails import GuardrailEngine
from agent_integrations.storage import SQLiteRunStore
from agent_integrations.tool_validation import normalize_and_validate_tool_arguments


class GraphScheduler:
    def __init__(
        self,
        config: AppConfig,
        registry: ToolRegistry,
        orchestrator: AgentOrchestrator,
        store: SQLiteRunStore,
        mcp_manager: Any,
        guardrail_engine: GuardrailEngine,
    ) -> None:
        self.config = config
        self.registry = registry
        self.orchestrator = orchestrator
        self.store = store
        self.mcp_manager = mcp_manager
        self.guardrail_engine = guardrail_engine

    async def run(self, input_text: str, session_id: str | None = None) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        self.store.create_run(run_id, self.config.graph.name, {'input': input_text}, session_id=session_id)
        self.store.record_event(
            run_id,
            'run_started',
            {'graph_name': self.config.graph.name, 'input': input_text, 'session_id': session_id},
            scope='run',
            span_id=f'run:{run_id}',
        )
        try:
            output = await self._run_internal(run_id, input_text, session_id)
        except Exception as exc:
            failure = {'error': str(exc)}
            self.store.finish_run(run_id, 'failed', failure)
            self.store.record_event(
                run_id,
                'run_failed',
                failure,
                scope='run',
                span_id=f'run:{run_id}',
            )
            raise RuntimeError(f'Run {run_id} failed: {exc}') from exc
        self.store.finish_run(run_id, 'succeeded', output)
        self.store.record_event(
            run_id,
            'run_succeeded',
            {'result': output},
            scope='run',
            span_id=f'run:{run_id}',
        )
        return output

    async def resume(self, run_id: str) -> dict[str, Any]:
        run_payload = self.store.load_run(run_id)
        if run_payload['status'] == 'succeeded':
            raise RuntimeError(f"Run '{run_id}' has already succeeded")
        checkpoint = self.store.load_latest_checkpoint(run_id)
        if checkpoint is None:
            raise RuntimeError(f"Run '{run_id}' does not have a resumable checkpoint")
        self.store.mark_run_running(run_id)
        self.store.record_event(
            run_id,
            'run_resumed',
            {'checkpoint_kind': checkpoint['kind']},
            scope='run',
            span_id=f'run:{run_id}',
        )
        input_text = str(run_payload['input_payload'].get('input', ''))
        session_id = run_payload['session_id']
        try:
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
                )
                if session_id is not None:
                    self.store.save_session_state(session_id, self.config.graph.name, shared_state)
            elif checkpoint['kind'] == 'team':
                payload = checkpoint['payload']
                shared_state = dict(payload.get('shared_state', {}))
                shared_state['input'] = input_text
                context = RunContext(
                    run_id=run_id,
                    workdir=Path.cwd(),
                    node_id=None,
                    shared_state=shared_state,
                    session_id=session_id,
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
                output = self._build_output(run_id, result.payload, session_id=session_id)
            else:
                raise RuntimeError(f"Unsupported checkpoint kind: {checkpoint['kind']}")
            output = self._apply_final_output_guardrails(output, run_id)
        except Exception as exc:
            failure = {'error': str(exc)}
            self.store.finish_run(run_id, 'failed', failure)
            self.store.record_event(
                run_id,
                'run_failed',
                failure,
                scope='run',
                span_id=f'run:{run_id}',
            )
            raise RuntimeError(f'Run {run_id} resume failed: {exc}') from exc
        self.store.finish_run(run_id, 'succeeded', output)
        self.store.record_event(
            run_id,
            'run_succeeded',
            {'result': output},
            scope='run',
            span_id=f'run:{run_id}',
        )
        return output

    async def _run_internal(self, run_id: str, input_text: str, session_id: str | None) -> dict[str, Any]:
        if self.config.graph.entrypoint in self.config.agent_map and not self.config.graph.nodes:
            return await self._run_direct_agent(run_id, input_text, session_id)
        if self.config.graph.entrypoint in self.config.team_map and not self.config.graph.nodes:
            return await self._run_direct_team(run_id, input_text, session_id)
        shared_state = self.store.load_session_state(session_id) if session_id is not None else {}
        shared_state = dict(shared_state)
        shared_state['input'] = input_text
        output = await self._run_graph_flow(run_id, input_text, shared_state, session_id)
        if session_id is not None:
            self.store.save_session_state(session_id, self.config.graph.name, shared_state)
        return self._apply_final_output_guardrails(output, run_id)

    async def _run_direct_agent(self, run_id: str, input_text: str, session_id: str | None) -> dict[str, Any]:
        shared_messages = []
        if session_id is not None:
            shared_messages.extend(self.store.load_session_messages(session_id))
        shared_messages.append(ChatMessage(role='user', content=input_text))
        context = RunContext(run_id=run_id, workdir=Path.cwd(), node_id=None, shared_state={'input': input_text}, session_id=session_id)
        result = await self.orchestrator.run_agent_with_messages(self.config.graph.entrypoint, shared_messages, context)
        if session_id is not None:
            self.store.save_session_messages(session_id, self.config.graph.name, result.shared_messages)
        return self._apply_final_output_guardrails(self._build_output(run_id, result.text, session_id=session_id), run_id)

    async def _run_direct_team(self, run_id: str, input_text: str, session_id: str | None) -> dict[str, Any]:
        shared_messages = []
        if session_id is not None:
            shared_messages.extend(self.store.load_session_messages(session_id))
        shared_messages.append(ChatMessage(role='user', content=input_text))
        context = RunContext(run_id=run_id, workdir=Path.cwd(), node_id=None, shared_state={'input': input_text}, session_id=session_id)
        result = await self.orchestrator.run_team_stateful(
            self.config.graph.entrypoint,
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
        )
        if checkpoint_initial:
            self.store.create_checkpoint(
                run_id,
                'graph',
                {
                    'results': graph_results,
                    'remaining': sorted(graph_remaining),
                    'shared_state': context.shared_state,
                },
            )
        while graph_remaining:
            ready = [nodes[node_id] for node_id in graph_remaining if all(dep in graph_results for dep in nodes[node_id].deps)]
            if not ready:
                raise RuntimeError('Graph contains unresolved dependencies or a cycle')
            for node in ready:
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

    @staticmethod
    def _build_output(
        run_id: str,
        result: Any,
        nodes: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {'run_id': run_id, 'result': result}
        if nodes is not None:
            payload['nodes'] = nodes
        if session_id is not None:
            payload['session_id'] = session_id
        return payload
