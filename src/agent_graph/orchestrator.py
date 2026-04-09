from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent_common.models import ChatMessage, RunContext, ToolCall, ToolSpec
from agent_common.tools import ToolHandler, ToolRegistry
from agent_config.app import AgentConfig, AppConfig, TeamConfig
from agent_integrations.guardrails import GuardrailEngine
from agent_integrations.human_loop import HumanLoopManager
from agent_integrations.storage import SQLiteRunStore
from agent_integrations.tool_validation import normalize_and_validate_tool_arguments


@dataclass(slots=True)
class TeamTurnResult:
    speaker: str
    text: str
    shared_messages: list[ChatMessage]
    handoff_target: str | None = None
    handoff_message: str | None = None


@dataclass(slots=True)
class AgentRunResult:
    text: str
    shared_messages: list[ChatMessage]


@dataclass(slots=True)
class TeamRunResult:
    payload: dict[str, Any]
    shared_messages: list[ChatMessage]


class AgentOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        model_client: Any,
        registry: ToolRegistry,
        store: SQLiteRunStore,
        guardrail_engine: GuardrailEngine,
        human_loop: HumanLoopManager | None = None,
    ) -> None:
        self.config = config
        self.model_client = model_client
        self.registry = registry
        self.store = store
        self.guardrail_engine = guardrail_engine
        self.human_loop = human_loop or HumanLoopManager(store, config.security.human_loop)
        self.agents: dict[str, AgentConfig] = config.agent_map
        self.teams: dict[str, TeamConfig] = config.team_map

    def register_subagent_tools(self) -> None:
        for agent in self.config.graph.agents:
            for sub_agent_name in agent.sub_agents:
                tool_name = f'subagent__{sub_agent_name}'
                if self.registry.has(tool_name):
                    continue
                self.registry.register(self._subagent_spec(tool_name, sub_agent_name), self._subagent_runner(sub_agent_name))

    def _subagent_runner(self, target_name: str) -> ToolHandler:
        async def _run(arguments: dict[str, Any], context: RunContext) -> Any:
            prompt = str(arguments.get('prompt', ''))
            next_context = RunContext(
                run_id=context.run_id,
                workdir=context.workdir,
                node_id=context.node_id,
                shared_state=context.shared_state,
                depth=context.depth + 1,
                session_id=context.session_id,
                approval_mode=context.approval_mode,
            )
            return await self.run_agent(target_name, prompt, next_context)

        return _run

    @staticmethod
    def _subagent_spec(tool_name: str, agent_name: str) -> ToolSpec:
        return ToolSpec(
            name=tool_name,
            description=f"Delegate work to sub-agent '{agent_name}'.",
            input_schema={
                'type': 'object',
                'properties': {'prompt': {'type': 'string'}},
                'required': ['prompt'],
            },
        )

    @staticmethod
    def _handoff_spec(agent_name: str) -> ToolSpec:
        return ToolSpec(
            name=f'handoff__{agent_name}',
            description=f"Transfer the active turn to team member '{agent_name}'.",
            input_schema={
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'prompt': {'type': 'string'},
                },
            },
        )

    async def run_agent(self, name: str, prompt: str, context: RunContext) -> Any:
        result = await self.run_agent_with_messages(name, [ChatMessage(role='user', content=prompt)], context)
        return result.text

    async def run_agent_with_messages(
        self,
        name: str,
        shared_messages: list[ChatMessage],
        context: RunContext,
    ) -> AgentRunResult:
        if context.depth > 6:
            raise RuntimeError('Maximum sub-agent depth exceeded')
        result = await self._run_agent_turn(name, list(shared_messages), context)
        return AgentRunResult(text=result.text, shared_messages=result.shared_messages)

    async def run_team(self, name: str, prompt: str, context: RunContext) -> dict[str, Any]:
        result = await self.run_team_stateful(name, prompt, context)
        return result.payload

    async def run_team_stateful(
        self,
        name: str,
        prompt: str,
        context: RunContext,
        initial_messages: list[ChatMessage] | None = None,
        restored_state: dict[str, Any] | None = None,
        checkpointing: bool = False,
    ) -> TeamRunResult:
        team = self.teams[name]
        checkpointing = checkpointing and context.node_id is None
        turns: list[dict[str, Any]]
        if restored_state is not None:
            shared_messages = self._restore_messages(restored_state.get('shared_messages', []))
            current_speaker = str(restored_state.get('current_speaker') or team.members[0])
            turns = [dict(turn) for turn in restored_state.get('turns', [])]
            start_turn = int(restored_state.get('next_turn_index', 1))
        else:
            shared_messages = list(initial_messages) if initial_messages is not None else [ChatMessage(role='user', content=prompt)]
            current_speaker = team.members[0]
            turns = []
            start_turn = 1
            self.store.record_event(
                context.run_id,
                'team_start',
                {'team': name, 'mode': team.mode.value, 'members': team.members, 'prompt': prompt},
                scope='team',
                node_id=context.node_id,
                span_id=f'team:{name}',
            )
            self._checkpoint_team(name, prompt, context, shared_messages, turns, current_speaker, start_turn, checkpointing)
        for turn_index in range(start_turn, team.max_turns + 1):
            await self.human_loop.check_interrupt(context, f'team_turn:{name}:{turn_index}')
            if team.mode.value == 'round_robin':
                speaker = team.members[(turn_index - 1) % len(team.members)]
            elif team.mode.value == 'selector':
                speaker = await self._select_speaker(team, shared_messages, context, turns)
            else:
                speaker = current_speaker
            self.store.record_event(
                context.run_id,
                'team_turn',
                {'team': name, 'mode': team.mode.value, 'turn': turn_index, 'speaker': speaker},
                scope='team',
                node_id=context.node_id,
                span_id=f'team:{name}:turn:{turn_index}',
                parent_span_id=f'team:{name}',
            )
            handoff_targets = [member for member in team.members if member != speaker] if team.mode.value == 'swarm' else []
            result = await self._run_agent_turn(speaker, shared_messages, context, handoff_targets)
            shared_messages = result.shared_messages
            turn_payload = {
                'turn': turn_index,
                'speaker': speaker,
                'text': result.text,
                'handoff_target': result.handoff_target,
            }
            turns.append(turn_payload)
            if result.handoff_target and team.mode.value == 'swarm':
                handoff_message = result.handoff_message or f'Handoff from {speaker} to {result.handoff_target}.'
                if self.config.security.human_loop.approve_handoffs:
                    await self.human_loop.require_approval(
                        context,
                        request_key=f'handoff:{name}:{turn_index}:{self.human_loop.stable_key(speaker, result.handoff_target, handoff_message)}',
                        kind='handoff',
                        title=f'Approve handoff from {speaker} to {result.handoff_target}',
                        payload={
                            'team': name,
                            'turn': turn_index,
                            'from': speaker,
                            'to': result.handoff_target,
                            'message': handoff_message,
                        },
                    )
                await self.human_loop.check_interrupt(context, f'team_handoff:{name}:{turn_index}')
                current_speaker = result.handoff_target
                shared_messages.append(
                    ChatMessage(
                        role='user',
                        content=(
                            f'Team handoff from {speaker} to {result.handoff_target}. '
                            f'Continue the task using this context: {handoff_message}'
                        ),
                    )
                )
                self.store.record_event(
                    context.run_id,
                    'team_handoff',
                    {
                        'team': name,
                        'from': speaker,
                        'to': result.handoff_target,
                        'message': handoff_message,
                    },
                    scope='team',
                    node_id=context.node_id,
                    span_id=f'team:{name}:handoff:{turn_index}',
                    parent_span_id=f'team:{name}',
                )
            if team.termination_text and team.termination_text in result.text:
                payload = {
                    'team': name,
                    'mode': team.mode.value,
                    'turns': turns,
                    'result': result.text,
                    'terminated_by': speaker,
                }
                self.store.record_event(
                    context.run_id,
                    'team_finish',
                    payload,
                    scope='team',
                    node_id=context.node_id,
                    span_id=f'team:{name}:finish',
                    parent_span_id=f'team:{name}',
                )
                return TeamRunResult(payload=payload, shared_messages=shared_messages)
            self._checkpoint_team(name, prompt, context, shared_messages, turns, current_speaker, turn_index + 1, checkpointing)
        raise RuntimeError(f"Team '{name}' exceeded max_turns")

    async def _select_speaker(
        self,
        team: TeamConfig,
        shared_messages: list[ChatMessage],
        context: RunContext,
        turns: list[dict[str, Any]],
    ) -> str:
        transcript = '\n'.join(
            f"{message.role}: {message.content}" for message in shared_messages[-12:] if message.content
        )
        default_prompt = (
            'Select exactly one next speaker from the provided candidates. '
            'Reply with only the agent name and no extra text.'
        )
        member_lines = []
        for member in team.members:
            description = self.agents[member].description
            member_lines.append(f'- {member}: {description}')
        selector_messages = [
            ChatMessage(role='system', content=team.selector_prompt or default_prompt),
            ChatMessage(
                role='user',
                content=(
                    f'Team: {team.name}\n'
                    f"Members:\n" + '\n'.join(member_lines) + '\n\n'
                    f'Recent transcript:\n{transcript}\n\n'
                    f"Last speaker: {turns[-1]['speaker'] if turns else 'none'}"
                ),
            ),
        ]
        response = await self.model_client.complete(selector_messages, [])
        selected = self._match_team_member(response.text, team.members, turns[-1]['speaker'] if turns else None, team.allow_repeated_speaker)
        self.store.record_event(
            context.run_id,
            'team_select_speaker',
            {'team': team.name, 'response': response.text, 'speaker': selected},
            scope='team',
            node_id=context.node_id,
            span_id=f'team:{team.name}:selector',
            parent_span_id=f'team:{team.name}',
        )
        return selected

    @staticmethod
    def _match_team_member(
        raw_text: str,
        members: list[str],
        last_speaker: str | None,
        allow_repeated_speaker: bool,
    ) -> str:
        candidate = raw_text.strip().splitlines()[0].strip() if raw_text.strip() else ''
        if candidate in members and (allow_repeated_speaker or candidate != last_speaker):
            return candidate
        for member in members:
            if member in raw_text and (allow_repeated_speaker or member != last_speaker):
                return member
        if last_speaker is not None:
            for member in members:
                if member != last_speaker:
                    return member
        if members:
            return members[0]
        raise RuntimeError('No valid team member available for selection')

    async def _run_agent_turn(
        self,
        name: str,
        shared_messages: list[ChatMessage],
        context: RunContext,
        handoff_targets: list[str] | None = None,
    ) -> TeamTurnResult:
        if context.depth > 6:
            raise RuntimeError('Maximum sub-agent depth exceeded')
        agent = self.agents[name]
        tool_names = agent.tools + [f'subagent__{item}' for item in agent.sub_agents]
        tool_specs = self.registry.list_specs(tool_names)
        handoff_targets = handoff_targets or []
        tool_specs.extend(self._handoff_spec(target) for target in handoff_targets)
        messages = [ChatMessage(role='system', content=agent.system_prompt), *shared_messages]
        for iteration in range(agent.max_iterations):
            await self.human_loop.check_interrupt(context, f'agent_iteration:{name}:{iteration + 1}')
            agent_span = f'agent:{name}:iter:{iteration + 1}'
            self.store.record_event(
                context.run_id,
                'agent_iteration_started',
                {'agent': name, 'iteration': iteration + 1},
                scope='agent',
                node_id=context.node_id,
                span_id=agent_span,
            )
            self.store.record_event(
                context.run_id,
                'agent_request',
                {'agent': name, 'iteration': iteration + 1, 'prompt': messages[-1].content if messages else ''},
                scope='agent',
                node_id=context.node_id,
                span_id=agent_span,
            )
            response = await self.model_client.complete(messages, tool_specs)
            self.store.record_event(
                context.run_id,
                'agent_response',
                {
                    'agent': name,
                    'text': response.text,
                    'tool_calls': [item.model_dump() for item in response.tool_calls],
                },
                scope='agent',
                node_id=context.node_id,
                span_id=agent_span,
            )
            if not response.tool_calls:
                if response.text:
                    messages.append(ChatMessage(role='assistant', content=response.text))
                self.store.record_event(
                    context.run_id,
                    'agent_final_answer',
                    {'agent': name, 'text': response.text},
                    scope='agent',
                    node_id=context.node_id,
                    span_id=agent_span,
                )
                return TeamTurnResult(
                    speaker=name,
                    text=response.text,
                    shared_messages=messages[1:],
                )
            messages.append(ChatMessage(role='assistant', content=response.text, tool_calls=response.tool_calls))
            handoff_call = next((call for call in response.tool_calls if call.name.startswith('handoff__')), None)
            if handoff_call is not None:
                target_name = handoff_call.name.replace('handoff__', '', 1)
                handoff_message = str(handoff_call.arguments.get('message') or handoff_call.arguments.get('prompt') or response.text)
                messages.append(
                    ChatMessage(
                        role='tool',
                        content=str({'handoff_to': target_name, 'message': handoff_message}),
                        name=handoff_call.name,
                        tool_call_id=handoff_call.id,
                    )
                )
                return TeamTurnResult(
                    speaker=name,
                    text=response.text,
                    shared_messages=messages[1:],
                    handoff_target=target_name,
                    handoff_message=handoff_message,
                )
            validation_repaired = False
            for tool_call in response.tool_calls:
                executed = await self._execute_tool_call(tool_call, context, agent_span, actor=name, iteration=iteration + 1)
                if isinstance(executed, ChatMessage):
                    validation_repaired = True
                    messages.append(executed)
                    break
                messages.append(
                    ChatMessage(
                        role='tool',
                        content=str(executed),
                        name=tool_call.name,
                        tool_call_id=tool_call.id,
                    )
                )
            if validation_repaired:
                continue
        raise RuntimeError(f"Agent '{name}' exceeded max_iterations")

    async def _execute_tool_call(
        self,
        tool_call: ToolCall,
        context: RunContext,
        parent_span_id: str,
        *,
        actor: str,
        iteration: int,
    ) -> Any | ChatMessage:
        tool_spec = self.registry.get_spec(tool_call.name)
        validation = normalize_and_validate_tool_arguments(tool_spec.input_schema, tool_call.arguments)
        if validation.errors:
            error_message = '; '.join(validation.errors)
            self.store.record_event(
                context.run_id,
                'tool_validation_failed',
                {
                    'tool_name': tool_call.name,
                    'tool_call_id': tool_call.id,
                    'arguments': tool_call.arguments,
                    'normalized_arguments': validation.normalized,
                    'errors': validation.errors,
                },
                scope='tool',
                node_id=context.node_id,
                span_id=f'tool:{tool_call.name}',
                parent_span_id=parent_span_id,
            )
            self.store.record_event(
                context.run_id,
                'tool_validation_repair_requested',
                {'tool_name': tool_call.name, 'tool_call_id': tool_call.id, 'errors': validation.errors},
                scope='agent',
                node_id=context.node_id,
                span_id=f'tool:{tool_call.name}',
                parent_span_id=parent_span_id,
            )
            return ChatMessage(
                role='tool',
                content=(
                    f"ValidationError for tool '{tool_call.name}': {error_message}. "
                    f'Call the same tool again with corrected arguments only.'
                ),
                name=tool_call.name,
                tool_call_id=tool_call.id,
            )
        duplicate_hit, cached_output = self._lookup_successful_tool_result(
            tool_spec,
            tool_call.name,
            validation.normalized,
            context,
        )
        if duplicate_hit:
            self.store.record_event(
                context.run_id,
                'tool_call_duplicate_blocked',
                {
                    'tool_name': tool_call.name,
                    'tool_call_id': tool_call.id,
                    'arguments': validation.normalized,
                    'result': cached_output,
                },
                scope='tool',
                node_id=context.node_id,
                span_id=f'tool:{tool_call.name}',
                parent_span_id=parent_span_id,
            )
            return cached_output
        if self.human_loop.is_sensitive_tool(tool_call.name):
            await self.human_loop.require_approval(
                context,
                request_key=(
                    f'tool:{actor}:{iteration}:{tool_call.name}:'
                    f'{self.human_loop.stable_key(validation.normalized, context.node_id)}'
                ),
                kind='tool',
                title=f'Approve sensitive tool {tool_call.name}',
                payload={
                    'tool_name': tool_call.name,
                    'arguments': validation.normalized,
                    'actor': actor,
                    'iteration': iteration,
                    'node_id': context.node_id,
                },
            )
        await self.human_loop.check_interrupt(context, f'tool_call:{tool_call.name}')
        decisions = self.guardrail_engine.check_tool_input(tool_call.name, validation.normalized, context)
        for decision in decisions:
            self.store.record_event(
                context.run_id,
                'tool_guardrail_result',
                {
                    'tool_name': tool_call.name,
                    'tool_call_id': tool_call.id,
                    'guardrail': decision.guardrail,
                    'outcome': decision.outcome,
                    'reason': decision.reason,
                    'payload': decision.payload,
                },
                scope='guardrail',
                node_id=context.node_id,
                span_id=f'guardrail:{decision.guardrail}',
                parent_span_id=parent_span_id,
            )
        self.guardrail_engine.ensure_allowed('tool_input', decisions)
        self.store.record_event(
            context.run_id,
            'tool_call_started',
            {'tool_name': tool_call.name, 'tool_call_id': tool_call.id, 'arguments': validation.normalized},
            scope='tool',
            node_id=context.node_id,
            span_id=f'tool:{tool_call.name}',
            parent_span_id=parent_span_id,
        )
        try:
            output = await self.registry.call(tool_call.name, validation.normalized, context)
        except Exception as exc:
            self.store.record_event(
                context.run_id,
                'tool_call_failed',
                {
                    'tool_name': tool_call.name,
                    'tool_call_id': tool_call.id,
                    'arguments': validation.normalized,
                    'error': str(exc),
                },
                scope='tool',
                node_id=context.node_id,
                span_id=f'tool:{tool_call.name}',
                parent_span_id=parent_span_id,
            )
            raise
        self.store.record_event(
            context.run_id,
            'tool_call_succeeded',
            {
                'tool_name': tool_call.name,
                'tool_call_id': tool_call.id,
                'arguments': validation.normalized,
                'result': output,
            },
            scope='tool',
            node_id=context.node_id,
            span_id=f'tool:{tool_call.name}',
            parent_span_id=parent_span_id,
        )
        self._remember_successful_tool_result(tool_call.name, validation.normalized, output, context)
        return output

    def _lookup_successful_tool_result(
        self,
        tool_spec: ToolSpec,
        tool_name: str,
        arguments: dict[str, Any],
        context: RunContext,
    ) -> tuple[bool, Any]:
        cache = self._successful_tool_cache(context)
        cached = cache.get(self._tool_cache_key(tool_name, arguments))
        if isinstance(cached, dict) and 'result' in cached:
            return True, cached['result']
        for cache_key, payload in cache.items():
            if not isinstance(payload, dict) or 'result' not in payload:
                continue
            cached_tool_name = str(payload.get('tool_name') or '')
            if cached_tool_name and cached_tool_name != tool_name:
                continue
            if not cached_tool_name and not str(cache_key).startswith(f'{tool_name}:'):
                continue
            previous_arguments = payload.get('arguments')
            if self._is_optional_argument_superset_duplicate(tool_spec, previous_arguments, arguments):
                return True, payload['result']
        return False, None

    def _remember_successful_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        output: Any,
        context: RunContext,
    ) -> None:
        cache = self._successful_tool_cache(context)
        cache[self._tool_cache_key(tool_name, arguments)] = {
            'tool_name': tool_name,
            'arguments': dict(arguments),
            'result': output,
        }

    @staticmethod
    def _is_optional_argument_superset_duplicate(
        tool_spec: ToolSpec,
        previous_arguments: Any,
        current_arguments: dict[str, Any],
    ) -> bool:
        if not isinstance(previous_arguments, dict):
            return False
        if previous_arguments == current_arguments:
            return True
        schema = tool_spec.input_schema if isinstance(tool_spec.input_schema, dict) else {}
        schema_type = str(schema.get('type', 'object'))
        if schema_type not in {'object', 'dict'}:
            return False
        properties = schema.get('properties', {})
        if not isinstance(properties, dict):
            properties = {}
        required = {str(item) for item in schema.get('required', [])}
        if not required:
            return False
        for key in required:
            if key not in previous_arguments or key not in current_arguments:
                return False
            if previous_arguments[key] != current_arguments[key]:
                return False
        for key, value in previous_arguments.items():
            if key not in current_arguments or current_arguments[key] != value:
                return AgentOrchestrator._is_declared_optional_subset(
                    smaller=current_arguments,
                    larger=previous_arguments,
                    required=required,
                    properties=properties,
                )
        return AgentOrchestrator._is_declared_optional_subset(
            smaller=previous_arguments,
            larger=current_arguments,
            required=required,
            properties=properties,
        )

    @staticmethod
    def _is_declared_optional_subset(
        *,
        smaller: dict[str, Any],
        larger: dict[str, Any],
        required: set[str],
        properties: dict[str, Any],
    ) -> bool:
        if smaller == larger:
            return True
        for key in required:
            if key not in smaller or key not in larger or smaller[key] != larger[key]:
                return False
        for key, value in smaller.items():
            if key not in larger or larger[key] != value:
                return False
        extra_keys = set(larger) - set(smaller)
        if not extra_keys:
            return False
        if any(key in required for key in extra_keys):
            return False
        if any(key not in properties for key in extra_keys):
            return False
        return True

    @staticmethod
    def _successful_tool_cache(context: RunContext) -> dict[str, Any]:
        cache = context.shared_state.setdefault('_successful_tool_cache', {})
        if isinstance(cache, dict):
            return cache
        fresh_cache: dict[str, Any] = {}
        context.shared_state['_successful_tool_cache'] = fresh_cache
        return fresh_cache

    @staticmethod
    def _tool_cache_key(tool_name: str, arguments: dict[str, Any]) -> str:
        return f"{tool_name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)}"

    def _checkpoint_team(
        self,
        name: str,
        prompt: str,
        context: RunContext,
        shared_messages: list[ChatMessage],
        turns: list[dict[str, Any]],
        current_speaker: str,
        next_turn_index: int,
        checkpointing: bool,
    ) -> None:
        if not checkpointing:
            return
        self.store.create_checkpoint(
            context.run_id,
            'team',
            {
                'team': name,
                'prompt': prompt,
                'shared_messages': [message.model_dump() for message in shared_messages],
                'turns': turns,
                'current_speaker': current_speaker,
                'next_turn_index': next_turn_index,
                'shared_state': context.shared_state,
            },
        )

    @staticmethod
    def _restore_messages(payloads: list[dict[str, Any]]) -> list[ChatMessage]:
        return [ChatMessage.model_validate(item) for item in payloads]






