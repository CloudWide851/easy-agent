from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_common.models import ChatMessage, RunContext, ToolSpec
from agent_common.tools import ToolHandler, ToolRegistry
from agent_config.app import AgentConfig, AppConfig, TeamConfig
from agent_integrations.storage import SQLiteRunStore


@dataclass(slots=True)
class TeamTurnResult:
    speaker: str
    text: str
    shared_messages: list[ChatMessage]
    handoff_target: str | None = None
    handoff_message: str | None = None


class AgentOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        model_client: Any,
        registry: ToolRegistry,
        store: SQLiteRunStore,
    ) -> None:
        self.config = config
        self.model_client = model_client
        self.registry = registry
        self.store = store
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
        if context.depth > 6:
            raise RuntimeError('Maximum sub-agent depth exceeded')
        shared_messages = [ChatMessage(role='user', content=prompt)]
        result = await self._run_agent_turn(name, shared_messages, context)
        return result.text

    async def run_team(self, name: str, prompt: str, context: RunContext) -> dict[str, Any]:
        team = self.teams[name]
        shared_messages = [ChatMessage(role='user', content=prompt)]
        current_speaker = team.members[0]
        turns: list[dict[str, Any]] = []
        self.store.record_event(
            context.run_id,
            'team_start',
            {'team': name, 'mode': team.mode.value, 'members': team.members, 'prompt': prompt},
        )
        for turn_index in range(1, team.max_turns + 1):
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
                current_speaker = result.handoff_target
                handoff_message = result.handoff_message or f'Handoff from {speaker} to {result.handoff_target}.'
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
                )
            if team.termination_text and team.termination_text in result.text:
                payload = {
                    'team': name,
                    'mode': team.mode.value,
                    'turns': turns,
                    'result': result.text,
                    'terminated_by': speaker,
                }
                self.store.record_event(context.run_id, 'team_finish', payload)
                return payload
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
                    f"Team: {team.name}\n"
                    f"Members:\n" + '\n'.join(member_lines) + '\n\n'
                    f"Recent transcript:\n{transcript}\n\n"
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
            self.store.record_event(
                context.run_id,
                'agent_request',
                {'agent': name, 'iteration': iteration + 1, 'prompt': messages[-1].content if messages else ''},
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
            )
            if not response.tool_calls:
                if response.text:
                    messages.append(ChatMessage(role='assistant', content=response.text))
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
            for tool_call in response.tool_calls:
                output = await self.registry.call(tool_call.name, tool_call.arguments, context)
                messages.append(
                    ChatMessage(
                        role='tool',
                        content=str(output),
                        name=tool_call.name,
                        tool_call_id=tool_call.id,
                    )
                )
        raise RuntimeError(f"Agent '{name}' exceeded max_iterations")
