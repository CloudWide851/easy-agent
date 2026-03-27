from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_common.models import HumanLoopMode, RunContext, RunStatus
from agent_config.app import AppConfig, HarnessConfig
from agent_graph.orchestrator import AgentOrchestrator
from agent_integrations.guardrails import GuardrailEngine
from agent_integrations.human_loop import ApprovalRequired, HumanLoopManager, RunInterrupted
from agent_integrations.storage import SQLiteRunStore
from agent_integrations.workbench import WorkbenchManager


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, indent=2)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


class HarnessRuntime:
    def __init__(
        self,
        config: AppConfig,
        orchestrator: AgentOrchestrator,
        store: SQLiteRunStore,
        guardrail_engine: GuardrailEngine,
        human_loop: HumanLoopManager | None = None,
        workbench_manager: WorkbenchManager | None = None,
    ) -> None:
        self.config = config
        self.orchestrator = orchestrator
        self.store = store
        self.guardrail_engine = guardrail_engine
        self.human_loop = human_loop or HumanLoopManager(store, config.security.human_loop)
        self.workbench_manager = workbench_manager

    def list_harnesses(self) -> list[HarnessConfig]:
        return list(self.config.harnesses)

    async def run(
        self,
        name: str,
        input_text: str,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        harness = self._get_harness(name)
        run_id = uuid.uuid4().hex
        self.store.create_run(
            run_id,
            harness.name,
            {'input': input_text, 'harness': harness.name},
            session_id=session_id,
            run_kind='harness',
        )
        self.store.record_event(
            run_id,
            'run_started',
            {'graph_name': harness.name, 'input': input_text, 'session_id': session_id, 'run_kind': 'harness'},
            scope='run',
            span_id=f'run:{run_id}',
        )
        self.store.record_event(
            run_id,
            'harness_started',
            {'harness': harness.name, 'input': input_text, 'session_id': session_id},
            scope='harness',
            span_id=f'harness:{harness.name}',
            parent_span_id=f'run:{run_id}',
        )
        return await self._execute_run(
            run_id,
            harness,
            lambda: self._run_internal(harness, run_id, input_text, session_id, restored_state=None, approval_mode=approval_mode),
        )

    async def resume(
        self,
        run_id: str,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        run_payload = self.store.load_run(run_id)
        if run_payload['run_kind'] != 'harness':
            raise RuntimeError(f"Run '{run_id}' is not a harness run")
        if run_payload['status'] == RunStatus.SUCCEEDED.value:
            raise RuntimeError(f"Run '{run_id}' has already succeeded")
        checkpoint = self.store.load_latest_checkpoint(run_id)
        if checkpoint is None or checkpoint['kind'] != 'harness':
            raise RuntimeError(f"Run '{run_id}' does not have a resumable harness checkpoint")
        harness_name = str(checkpoint['payload'].get('harness'))
        harness = self._get_harness(harness_name)
        return await self._execute_run(
            run_id,
            harness,
            lambda: self._resume_internal(run_id, harness, checkpoint, run_payload, approval_mode),
        )

    async def _resume_internal(
        self,
        run_id: str,
        harness: HarnessConfig,
        checkpoint: dict[str, Any],
        run_payload: dict[str, Any],
        approval_mode: HumanLoopMode,
    ) -> dict[str, Any]:
        if self.config.security.human_loop.approve_harness_resume:
            context = RunContext(
                run_id=run_id,
                workdir=Path(checkpoint['payload'].get('state', {}).get('artifact_root', Path.cwd())),
                node_id=None,
                shared_state={},
                session_id=run_payload['session_id'],
                approval_mode=approval_mode,
            )
            await self.human_loop.require_approval(
                context,
                request_key=f'harness_resume:{run_id}:{checkpoint["checkpoint_id"]}',
                kind='harness_resume',
                title=f'Approve harness resume for {harness.name}',
                payload={'run_id': run_id, 'harness': harness.name, 'checkpoint_id': checkpoint['checkpoint_id']},
            )
        self.store.mark_run_running(run_id)
        self.store.record_event(
            run_id,
            'run_resumed',
            {'checkpoint_kind': 'harness', 'harness': harness.name, 'checkpoint_id': checkpoint['checkpoint_id']},
            scope='run',
            span_id=f'run:{run_id}',
        )
        return await self._run_internal(
            harness,
            run_id,
            str(checkpoint['payload'].get('input', run_payload['input_payload'].get('input', ''))),
            run_payload['session_id'],
            restored_state=dict(checkpoint['payload'].get('state', {})),
            approval_mode=approval_mode,
        )

    async def _execute_run(self, run_id: str, harness: HarnessConfig, runner: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        try:
            output: dict[str, Any] = await runner()
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
            failure = {'error': str(exc), 'harness': harness.name}
            self.store.finish_run(run_id, RunStatus.FAILED.value, failure)
            self.store.record_event(
                run_id,
                'harness_failed',
                failure,
                scope='harness',
                span_id=f'harness:{harness.name}',
                parent_span_id=f'run:{run_id}',
            )
            self.store.record_event(
                run_id,
                'run_failed',
                failure,
                scope='run',
                span_id=f'run:{run_id}',
            )
            raise RuntimeError(f'Harness run {run_id} failed: {exc}') from exc
        self.store.finish_run(run_id, RunStatus.SUCCEEDED.value, output)
        self.store.record_event(
            run_id,
            'harness_succeeded',
            {'harness': harness.name, 'result': output['result']},
            scope='harness',
            span_id=f'harness:{harness.name}',
            parent_span_id=f'run:{run_id}',
        )
        self.store.record_event(
            run_id,
            'run_succeeded',
            {'result': output},
            scope='run',
            span_id=f'run:{run_id}',
        )
        return output

    async def _run_internal(
        self,
        harness: HarnessConfig,
        run_id: str,
        input_text: str,
        session_id: str | None,
        restored_state: dict[str, Any] | None,
        approval_mode: HumanLoopMode,
    ) -> dict[str, Any]:
        state = dict(restored_state or {})
        if not state and session_id is not None:
            state = self.store.load_harness_state(session_id, harness.name)
        if not state:
            state = self._build_state(harness, input_text, session_id, run_id)
        state['input'] = input_text
        state['session_id'] = session_id
        if not state.get('initialized'):
            state = await self._initialize(harness, run_id, input_text, session_id, state, approval_mode)
        return await self._run_cycles(harness, run_id, input_text, session_id, state, approval_mode)

    async def _initialize(
        self,
        harness: HarnessConfig,
        run_id: str,
        input_text: str,
        session_id: str | None,
        state: dict[str, Any],
        approval_mode: HumanLoopMode,
    ) -> dict[str, Any]:
        self._ensure_artifacts(state)
        prompt = self._initializer_prompt(harness, input_text, state)
        context = self._context(run_id, session_id, state, phase='initializer', cycle=0, approval_mode=approval_mode)
        summary = await self.orchestrator.run_agent(harness.initializer_agent, prompt, context)
        state['initialized'] = True
        state['status'] = 'running'
        state['initializer_summary'] = _stringify(summary)
        state['updated_at'] = _now()
        self._write_artifacts(harness, state)
        self._persist_state(run_id, harness, input_text, session_id, state)
        self.store.record_event(
            run_id,
            'harness_initialized',
            {
                'harness': harness.name,
                'artifact_root': state['artifact_root'],
                'initializer_agent': harness.initializer_agent,
            },
            scope='harness',
            span_id=f'harness:{harness.name}:initialize',
            parent_span_id=f'harness:{harness.name}',
        )
        return state

    async def _run_cycles(
        self,
        harness: HarnessConfig,
        run_id: str,
        input_text: str,
        session_id: str | None,
        state: dict[str, Any],
        approval_mode: HumanLoopMode,
    ) -> dict[str, Any]:
        while int(state.get('cycle_index', 1)) <= harness.max_cycles:
            cycle = int(state.get('cycle_index', 1))
            cycle_context = self._context(run_id, session_id, state, phase='cycle', cycle=cycle, approval_mode=approval_mode)
            await self.human_loop.check_interrupt(cycle_context, f'harness_cycle:{harness.name}:{cycle}')
            self.store.record_event(
                run_id,
                'harness_cycle_started',
                {'harness': harness.name, 'cycle': cycle, 'worker_target': harness.worker_target},
                scope='harness',
                span_id=f'harness:{harness.name}:cycle:{cycle}',
                parent_span_id=f'harness:{harness.name}',
            )
            worker_result = await self._run_worker(harness, run_id, input_text, session_id, state, cycle, approval_mode)
            worker_text = _stringify(worker_result)
            evaluation_text = await self.orchestrator.run_agent(
                harness.evaluator_agent,
                self._evaluator_prompt(harness, input_text, state, cycle, worker_text),
                self._context(run_id, session_id, state, phase='evaluator', cycle=cycle, approval_mode=approval_mode),
            )
            evaluation = self._parse_evaluation(_stringify(evaluation_text))
            history_entry = {
                'cycle': cycle,
                'worker_target': harness.worker_target,
                'worker_result': worker_text,
                'evaluator_text': _stringify(evaluation_text),
                'decision': evaluation['decision'],
                'summary': evaluation['summary'],
                'next': evaluation['next'],
                'timestamp': _now(),
            }
            state.setdefault('history', []).append(history_entry)
            state['last_decision'] = evaluation['decision']
            state['updated_at'] = _now()
            self.store.record_event(
                run_id,
                'harness_evaluated',
                {
                    'harness': harness.name,
                    'cycle': cycle,
                    'decision': evaluation['decision'],
                    'summary': evaluation['summary'],
                    'next': evaluation['next'],
                },
                scope='harness',
                span_id=f'harness:{harness.name}:evaluation:{cycle}',
                parent_span_id=f'harness:{harness.name}:cycle:{cycle}',
            )
            if evaluation['decision'] == 'REPLAN':
                if int(state.get('replan_count', 0)) >= harness.max_replans:
                    raise RuntimeError(f"Harness '{harness.name}' exceeded max_replans")
                state['replan_count'] = int(state.get('replan_count', 0)) + 1
                refreshed = await self.orchestrator.run_agent(
                    harness.initializer_agent,
                    self._replan_prompt(harness, input_text, state, cycle, history_entry),
                    self._context(run_id, session_id, state, phase='replan', cycle=cycle, approval_mode=approval_mode),
                )
                state['initializer_summary'] = _stringify(refreshed)
                self.store.record_event(
                    run_id,
                    'harness_replanned',
                    {'harness': harness.name, 'cycle': cycle, 'replan_count': state['replan_count']},
                    scope='harness',
                    span_id=f'harness:{harness.name}:replan:{cycle}',
                    parent_span_id=f'harness:{harness.name}:cycle:{cycle}',
                )
            self._write_artifacts(harness, state)
            if evaluation['decision'] == 'COMPLETE':
                state['status'] = 'succeeded'
                state['completed_at'] = _now()
                self._persist_state(run_id, harness, input_text, session_id, state)
                output = self._build_output(run_id, harness, session_id, state)
                return self._apply_final_output_guardrails(output, run_id)
            state['cycle_index'] = cycle + 1
            self._persist_state(run_id, harness, input_text, session_id, state)
        raise RuntimeError(f"Harness '{harness.name}' exceeded max_cycles")

    async def _run_worker(
        self,
        harness: HarnessConfig,
        run_id: str,
        input_text: str,
        session_id: str | None,
        state: dict[str, Any],
        cycle: int,
        approval_mode: HumanLoopMode,
    ) -> Any:
        prompt = self._worker_prompt(harness, input_text, state, cycle)
        context = self._context(run_id, session_id, state, phase='worker', cycle=cycle, approval_mode=approval_mode)
        if harness.worker_target in self.config.agent_map:
            return await self.orchestrator.run_agent(harness.worker_target, prompt, context)
        return await self.orchestrator.run_team(harness.worker_target, prompt, context)

    def _persist_state(
        self,
        run_id: str,
        harness: HarnessConfig,
        input_text: str,
        session_id: str | None,
        state: dict[str, Any],
    ) -> None:
        if session_id is not None:
            self.store.save_harness_state(session_id, harness.name, state)
        self.store.create_checkpoint(
            run_id,
            'harness',
            {
                'harness': harness.name,
                'input': input_text,
                'state': state,
                'workbench': self._workbench_manifest(run_id),
            },
        )

    def _build_state(
        self,
        harness: HarnessConfig,
        input_text: str,
        session_id: str | None,
        run_id: str,
    ) -> dict[str, Any]:
        session_key = self._safe_key(session_id or run_id)
        artifact_root = (Path(harness.artifacts_dir) / session_key).resolve()
        return {
            'harness': harness.name,
            'input': input_text,
            'session_id': session_id,
            'artifact_root': str(artifact_root),
            'bootstrap_path': str(artifact_root / 'bootstrap.md'),
            'progress_path': str(artifact_root / 'progress.md'),
            'features_path': str(artifact_root / 'features.json'),
            'initialized': False,
            'status': 'pending',
            'cycle_index': 1,
            'replan_count': 0,
            'history': [],
            'initializer_summary': '',
            'last_decision': None,
            'updated_at': _now(),
        }

    @staticmethod
    def _safe_key(value: str) -> str:
        return re.sub(r'[^A-Za-z0-9._-]+', '-', value)

    @staticmethod
    def _parse_evaluation(text: str) -> dict[str, str]:
        decision_match = re.search(r'^DECISION:\s*(COMPLETE|CONTINUE|REPLAN)\s*$', text, flags=re.MULTILINE)
        decision = decision_match.group(1) if decision_match else 'CONTINUE'
        summary_match = re.search(r'^SUMMARY:\s*(.+)$', text, flags=re.MULTILINE)
        next_match = re.search(r'^NEXT:\s*(.+)$', text, flags=re.MULTILINE)
        summary = summary_match.group(1).strip() if summary_match else text.strip().splitlines()[0] if text.strip() else ''
        next_step = next_match.group(1).strip() if next_match else ''
        if not summary:
            summary = decision.lower()
        return {'decision': decision, 'summary': summary, 'next': next_step}

    def _ensure_artifacts(self, state: dict[str, Any]) -> None:
        artifact_root = Path(state['artifact_root'])
        artifact_root.mkdir(parents=True, exist_ok=True)
        for key in ('bootstrap_path', 'progress_path', 'features_path'):
            path = Path(state[key])
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                default = '{}' if path.suffix == '.json' else ''
                path.write_text(default, encoding='utf-8')

    def _write_artifacts(self, harness: HarnessConfig, state: dict[str, Any]) -> None:
        self._ensure_artifacts(state)
        bootstrap_path = Path(state['bootstrap_path'])
        progress_path = Path(state['progress_path'])
        features_path = Path(state['features_path'])

        bootstrap_text = '\n'.join(
            [
                f'# {harness.name} bootstrap',
                '',
                '## Goal',
                str(state.get('input', '')),
                '',
                '## Completion Contract',
                harness.completion_contract,
                '',
                '## Initializer Summary',
                str(state.get('initializer_summary', '')).strip() or 'Not initialized yet.',
                '',
                '## Resume Instructions',
                'Read features.json for machine state, progress.md for prior cycles, then continue from the latest checkpoint.',
            ]
        )
        progress_lines = [f'# {harness.name} progress', '']
        for item in state.get('history', []):
            progress_lines.extend(
                [
                    f"## Cycle {item['cycle']}",
                    f"- Worker: {item['worker_target']}",
                    f"- Decision: {item['decision']}",
                    f"- Summary: {item['summary']}",
                    f"- Next: {item['next'] or 'n/a'}",
                    '',
                    '### Worker Result',
                    item['worker_result'],
                    '',
                    '### Evaluator Notes',
                    item['evaluator_text'],
                    '',
                ]
            )
        features_payload = {
            'harness': harness.name,
            'status': state.get('status'),
            'session_id': state.get('session_id'),
            'artifact_root': state.get('artifact_root'),
            'completion_contract': harness.completion_contract,
            'initializer_summary': state.get('initializer_summary'),
            'cycles_completed': len(state.get('history', [])),
            'next_cycle': state.get('cycle_index'),
            'replan_count': state.get('replan_count'),
            'last_decision': state.get('last_decision'),
            'updated_at': state.get('updated_at'),
            'history': state.get('history', []),
        }
        bootstrap_path.write_text(bootstrap_text, encoding='utf-8')
        progress_path.write_text('\n'.join(progress_lines).strip() + '\n', encoding='utf-8')
        features_path.write_text(json.dumps(features_payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def _context(
        self,
        run_id: str,
        session_id: str | None,
        state: dict[str, Any],
        *,
        phase: str,
        cycle: int,
        approval_mode: HumanLoopMode,
    ) -> RunContext:
        return RunContext(
            run_id=run_id,
            workdir=Path(state['artifact_root']),
            node_id=None,
            shared_state={
                'harness': state['harness'],
                'phase': phase,
                'cycle': cycle,
                'artifact_root': state['artifact_root'],
                'bootstrap_path': state['bootstrap_path'],
                'progress_path': state['progress_path'],
                'features_path': state['features_path'],
            },
            session_id=session_id,
            approval_mode=approval_mode,
        )

    def _initializer_prompt(self, harness: HarnessConfig, input_text: str, state: dict[str, Any]) -> str:
        return (
            'You are preparing a long-running agent harness. Produce a compact white-box kickoff summary with: '
            'goal, acceptance criteria, initial plan, key risks, and first action. '
            'Do not claim completion.\n\n'
            f'Harness: {harness.name}\n'
            f'Goal: {input_text}\n'
            f'Completion contract: {harness.completion_contract}\n'
            f'Artifact root: {state["artifact_root"]}\n'
            f'Bootstrap file: {state["bootstrap_path"]}\n'
            f'Progress file: {state["progress_path"]}\n'
            f'Features file: {state["features_path"]}'
        )

    def _worker_prompt(self, harness: HarnessConfig, input_text: str, state: dict[str, Any], cycle: int) -> str:
        return (
            'You are the worker for a resumable long-running harness. Work on one meaningful increment only. '
            'Use tools when they materially help. End with what changed and what remains.\n\n'
            f'Harness: {harness.name}\n'
            f'Cycle: {cycle}\n'
            f'Goal: {input_text}\n'
            f'Completion contract: {harness.completion_contract}\n'
            f'Bootstrap summary:\n{state.get("initializer_summary", "")}\n\n'
            f'Current progress:\n{Path(state["progress_path"]).read_text(encoding="utf-8")}\n'
        )

    def _evaluator_prompt(
        self,
        harness: HarnessConfig,
        input_text: str,
        state: dict[str, Any],
        cycle: int,
        worker_text: str,
    ) -> str:
        return (
            'You are the evaluator for a long-running harness. Judge whether the work is complete against the contract. '
            'Reply in exactly this shape:\n'
            'DECISION: COMPLETE|CONTINUE|REPLAN\n'
            'SUMMARY: one sentence\n'
            'NEXT: one sentence\n\n'
            f'Harness: {harness.name}\n'
            f'Cycle: {cycle}\n'
            f'Goal: {input_text}\n'
            f'Completion contract: {harness.completion_contract}\n'
            f'Initializer summary:\n{state.get("initializer_summary", "")}\n\n'
            f'Worker result:\n{worker_text}\n\n'
            f'Progress so far:\n{Path(state["progress_path"]).read_text(encoding="utf-8")}\n'
        )

    def _replan_prompt(
        self,
        harness: HarnessConfig,
        input_text: str,
        state: dict[str, Any],
        cycle: int,
        history_entry: dict[str, Any],
    ) -> str:
        return (
            'Refresh the harness bootstrap after an evaluator-triggered replan. Produce a revised plan with: '
            'goal, acceptance criteria, updated work plan, and next action.\n\n'
            f'Harness: {harness.name}\n'
            f'Cycle: {cycle}\n'
            f'Goal: {input_text}\n'
            f'Completion contract: {harness.completion_contract}\n'
            f'Prior initializer summary:\n{state.get("initializer_summary", "")}\n\n'
            f'Latest worker result:\n{history_entry["worker_result"]}\n\n'
            f'Evaluator feedback:\n{history_entry["evaluator_text"]}'
        )

    def _apply_final_output_guardrails(self, output: dict[str, Any], run_id: str) -> dict[str, Any]:
        context = RunContext(run_id=run_id, workdir=Path(output['result']['artifact_root']), node_id=None, shared_state={})
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
        harness: HarnessConfig,
        session_id: str | None,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        result = {
            'harness': harness.name,
            'status': state.get('status'),
            'artifact_root': state['artifact_root'],
            'bootstrap_path': state['bootstrap_path'],
            'progress_path': state['progress_path'],
            'features_path': state['features_path'],
            'cycles_completed': len(state.get('history', [])),
            'replan_count': state.get('replan_count', 0),
            'last_decision': state.get('last_decision'),
            'initializer_summary': state.get('initializer_summary', ''),
            'history': state.get('history', []),
        }
        payload: dict[str, Any] = {'run_id': run_id, 'result': result, 'status': RunStatus.SUCCEEDED.value}
        if session_id is not None:
            payload['session_id'] = session_id
        return payload

    def _workbench_manifest(self, run_id: str) -> dict[str, Any]:
        if self.workbench_manager is None:
            return {'sessions': []}
        return self.workbench_manager.snapshot_manifest(run_id)

    def _get_harness(self, name: str) -> HarnessConfig:
        try:
            return self.config.harness_map[name]
        except KeyError as exc:
            raise RuntimeError(f"Unknown harness '{name}'") from exc
