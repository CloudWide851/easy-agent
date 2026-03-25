from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import anyio

from agent_common.models import NodeStatus, NodeType, RunContext
from agent_common.tools import ToolRegistry
from agent_config.app import AppConfig, GraphNodeConfig
from agent_graph.orchestrator import AgentOrchestrator
from agent_integrations.storage import SQLiteRunStore


class GraphScheduler:
    def __init__(
        self,
        config: AppConfig,
        registry: ToolRegistry,
        orchestrator: AgentOrchestrator,
        store: SQLiteRunStore,
        mcp_manager: Any,
    ) -> None:
        self.config = config
        self.registry = registry
        self.orchestrator = orchestrator
        self.store = store
        self.mcp_manager = mcp_manager

    async def run(self, input_text: str) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        self.store.create_run(run_id, self.config.graph.name, {'input': input_text})
        shared_state: dict[str, Any] = {'input': input_text}
        context = RunContext(run_id=run_id, workdir=Path.cwd(), node_id=None, shared_state=shared_state)

        if self.config.graph.entrypoint in self.config.agent_map and not self.config.graph.nodes:
            output = await self.orchestrator.run_agent(self.config.graph.entrypoint, input_text, context)
            self.store.finish_run(run_id, 'succeeded', {'result': output})
            return {'run_id': run_id, 'result': output}

        if self.config.graph.entrypoint in self.config.team_map and not self.config.graph.nodes:
            output = await self.orchestrator.run_team(self.config.graph.entrypoint, input_text, context)
            self.store.finish_run(run_id, 'succeeded', {'result': output})
            return {'run_id': run_id, 'result': output}

        nodes = {node.id: node for node in self.config.graph.nodes}
        results: dict[str, Any] = {}
        remaining = set(nodes)
        while remaining:
            ready = [nodes[node_id] for node_id in remaining if all(dep in results for dep in nodes[node_id].deps)]
            if not ready:
                self.store.finish_run(run_id, 'failed', {'error': 'cycle_or_missing_dependency'})
                raise RuntimeError('Graph contains unresolved dependencies or a cycle')
            for node in ready:
                output = await self._execute_node(node, results, context)
                results[node.id] = output
                shared_state[node.id] = output
                remaining.remove(node.id)

        final_output = results[self.config.graph.entrypoint]
        self.store.finish_run(run_id, 'succeeded', {'result': final_output, 'nodes': results})
        return {'run_id': run_id, 'result': final_output, 'nodes': results}

    async def _execute_node(
        self,
        node: GraphNodeConfig,
        results: dict[str, Any],
        parent_context: RunContext,
    ) -> Any:
        template_values = {'input': parent_context.shared_state['input'], **results}
        prompt = node.input_template.format(**template_values)
        node_context = RunContext(
            run_id=parent_context.run_id,
            workdir=parent_context.workdir,
            node_id=node.id,
            shared_state=parent_context.shared_state,
            depth=parent_context.depth,
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
            return await self.registry.call(node.target, payload, context)
        if node.type is NodeType.MCP_TOOL:
            if node.target is None or '/' not in node.target:
                raise ValueError("mcp_tool target must be in the format 'server/tool'")
            server_name, tool_name = node.target.split('/', 1)
            payload = {'prompt': prompt, **node.arguments}
            return await self.mcp_manager.call_tool(server_name, tool_name, payload)
        if node.type is NodeType.JOIN:
            return {dep: context.shared_state[dep] for dep in node.deps}
        raise ValueError(f'Unsupported node type: {node.type}')
