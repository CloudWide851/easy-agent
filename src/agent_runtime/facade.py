from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any

import yaml

from agent_common.models import HumanLoopMode
from agent_runtime.bundles import write_run_bundle
from agent_runtime.connectors import browser_artifacts, browser_doctor, connector_checks
from agent_runtime.reports import latest_report_payload
from agent_runtime.runtime import EasyAgentRuntime, build_runtime
from agent_runtime.tasks import get_task_pack, render_task_prompt, task_pack_payload


class AgentApp:
    """Small Python facade over EasyAgentRuntime for product-style embedding."""

    def __init__(self, runtime: EasyAgentRuntime, *, config_path: str | Path | None = None) -> None:
        self.runtime = runtime
        self.config_path = Path(config_path) if config_path is not None else None

    @classmethod
    def from_config(cls, config: str | Path = 'easy-agent.yml') -> AgentApp:
        return cls(build_runtime(config), config_path=config)

    @classmethod
    def from_runtime(cls, runtime: EasyAgentRuntime) -> AgentApp:
        return cls(runtime)

    async def arun(
        self,
        input_text: str,
        *,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        return await self.runtime.run(input_text, session_id=session_id, approval_mode=approval_mode)

    def run(
        self,
        input_text: str,
        *,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        return asyncio.run(self.arun(input_text, session_id=session_id, approval_mode=approval_mode))

    async def arun_task(
        self,
        pack: str,
        *,
        context: str | None = None,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        return await self.arun(
            render_task_prompt(pack, context),
            session_id=session_id,
            approval_mode=approval_mode,
        )

    def run_task(
        self,
        pack: str,
        *,
        context: str | None = None,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        return asyncio.run(
            self.arun_task(pack, context=context, session_id=session_id, approval_mode=approval_mode)
        )

    def workflow_plan(
        self,
        workflow_path: str | Path,
        *,
        config: str | Path | None = None,
        context: str | None = None,
    ) -> dict[str, Any]:
        workflow = self._load_workflow(workflow_path)
        pack = str(workflow.get('pack') or '')
        pack_info = task_pack_payload(get_task_pack(pack))
        selected_context = context if context is not None else str(workflow.get('context') or '')
        config_path = self._config_path(config)
        return {
            'pack': pack,
            'workflow': workflow,
            'description': pack_info['description'],
            'recommended_scenario': pack_info['recommended_scenario'],
            'acceptance_criteria': pack_info['acceptance_criteria'],
            'prompt': render_task_prompt(pack, selected_context),
            'approval_mode': str(workflow.get('approval_mode') or HumanLoopMode.HYBRID.value),
            'preflight': [check.__dict__ for check in connector_checks(config_path)],
            'next_commands': [
                f'easy-agent workflow plan {workflow_path} -c easy-agent.yml',
                f'easy-agent workflow run {workflow_path} -c easy-agent.yml --dry-run',
            ],
        }

    async def arun_workflow(
        self,
        workflow_path: str | Path,
        *,
        context: str | None = None,
        session_id: str | None = None,
        approval_mode: HumanLoopMode | None = None,
    ) -> dict[str, Any]:
        plan = self.workflow_plan(workflow_path, context=context)
        workflow_approval = HumanLoopMode(str(plan['approval_mode']))
        return await self.arun(
            str(plan['prompt']),
            session_id=session_id,
            approval_mode=approval_mode or workflow_approval,
        )

    def run_workflow(
        self,
        workflow_path: str | Path,
        *,
        context: str | None = None,
        session_id: str | None = None,
        approval_mode: HumanLoopMode | None = None,
    ) -> dict[str, Any]:
        return asyncio.run(
            self.arun_workflow(
                workflow_path,
                context=context,
                session_id=session_id,
                approval_mode=approval_mode,
            )
        )

    def run_bundle(
        self,
        run_id: str,
        *,
        output_dir: str | Path | None = None,
        artifact_limit: int = 50,
        copy_browser_artifacts: bool = True,
        force: bool = False,
        config: str | Path | None = None,
    ) -> dict[str, Any]:
        target = Path(output_dir) if output_dir is not None else Path(f'run-bundle-{_safe_token(run_id)}')
        return write_run_bundle(
            self.runtime.store,
            run_id,
            target,
            browser_payload=browser_artifacts(self._config_path(config), limit=artifact_limit),
            artifact_limit=artifact_limit,
            copy_browser_artifacts=copy_browser_artifacts,
            force=force,
        )

    async def abrowser_audit(
        self,
        url: str,
        *,
        kind: str = 'audit',
        context: str | None = None,
        run: bool = False,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
        config: str | Path | None = None,
    ) -> dict[str, Any]:
        payload = self.browser_audit(url, kind=kind, context=context, run=False, config=config)
        if not run:
            return payload
        result = await self.arun(str(payload['prompt']), approval_mode=approval_mode)
        payload['mode'] = 'run'
        payload['result'] = result
        return payload

    def browser_audit(
        self,
        url: str,
        *,
        kind: str = 'audit',
        context: str | None = None,
        run: bool = False,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
        config: str | Path | None = None,
    ) -> dict[str, Any]:
        if kind not in {'audit', 'seo', 'a11y', 'links', 'smoke', 'snapshot'}:
            raise ValueError('kind must be audit, seo, a11y, links, smoke, or snapshot')
        task_pack = 'browser-audit' if kind in {'audit', 'seo', 'a11y', 'links'} else 'browser-qa'
        prompt = render_task_prompt(task_pack, _browser_context(kind, url, context))
        payload = {
            'kind': kind,
            'url': url,
            'pack': task_pack,
            'mode': 'plan_only',
            'doctor': browser_doctor(self._config_path(config)),
            'prompt': prompt,
            'next_commands': [
                'easy-agent browser doctor -c easy-agent.yml',
                'easy-agent connectors test browser -c easy-agent.yml',
                'easy-agent browser artifacts -c easy-agent.yml',
            ],
        }
        if not run:
            return payload
        result = self.run(str(payload['prompt']), approval_mode=approval_mode)
        payload['mode'] = 'run'
        payload['result'] = result
        return payload

    async def astream(
        self,
        input_text: str,
        *,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in self.runtime.stream(
            input_text,
            session_id=session_id,
            approval_mode=approval_mode,
        ):
            yield event

    def stream(
        self,
        input_text: str,
        *,
        session_id: str | None = None,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> list[dict[str, Any]]:
        async def _collect() -> list[dict[str, Any]]:
            return [
                event
                async for event in self.astream(
                    input_text,
                    session_id=session_id,
                    approval_mode=approval_mode,
                )
            ]

        return asyncio.run(_collect())

    async def aresume(
        self,
        run_id: str,
        checkpoint_id: int | None = None,
        *,
        fork: bool = False,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        return await self.runtime.resume(run_id, checkpoint_id, fork=fork, approval_mode=approval_mode)

    def resume(
        self,
        run_id: str,
        checkpoint_id: int | None = None,
        *,
        fork: bool = False,
        approval_mode: HumanLoopMode = HumanLoopMode.HYBRID,
    ) -> dict[str, Any]:
        return asyncio.run(
            self.aresume(run_id, checkpoint_id, fork=fork, approval_mode=approval_mode)
        )

    def report(
        self,
        *,
        config: str | Path = 'easy-agent.yml',
        benchmark_report: str | Path = '.easy-agent/benchmark-report.json',
        public_eval_report: str | Path = '.easy-agent/public-eval-report.json',
        real_network_report: str | Path = '.easy-agent/real-network-report.json',
        run_limit: int = 50,
    ) -> dict[str, Any]:
        return latest_report_payload(
            Path(config),
            benchmark_report=Path(benchmark_report),
            public_eval_report=Path(public_eval_report),
            real_network_report=Path(real_network_report),
            run_limit=run_limit,
        )

    def trace(self, run_id: str, *, tree: bool = True) -> dict[str, Any]:
        return self.runtime.store.load_trace_tree(run_id) if tree else self.runtime.store.load_trace(run_id)

    def _config_path(self, config: str | Path | None = None) -> Path:
        if config is not None:
            return Path(config)
        if self.config_path is not None:
            return self.config_path
        return Path('easy-agent.yml')

    @staticmethod
    def _load_workflow(workflow_path: str | Path) -> dict[str, Any]:
        path = Path(workflow_path)
        loaded = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        if not isinstance(loaded, dict):
            raise ValueError('workflow file must contain a YAML mapping')
        if loaded.get('version') != 1:
            raise ValueError('workflow file version must be 1')
        if not loaded.get('pack'):
            raise ValueError('workflow file must define pack')
        get_task_pack(str(loaded['pack']))
        return loaded

    async def aclose(self) -> None:
        await self.runtime.aclose()

    def close(self) -> None:
        asyncio.run(self.aclose())

    async def __aenter__(self) -> AgentApp:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


def _browser_context(kind: str, url: str, context: str | None) -> str:
    objectives = {
        'smoke': 'Open the page, collect snapshot/accessibility-tree evidence, verify visible readiness, and list blocked follow-up actions.',
        'snapshot': 'Collect snapshot/accessibility-tree evidence before screenshots, then summarize notable page structure.',
        'audit': 'Audit title, metadata, canonical signals, headings, visible content, links, accessibility signals, and page-quality gaps.',
        'seo': 'Check title, meta description, canonical URL, indexability signals, headings, content relevance, internal links, and prioritized SEO fixes.',
        'a11y': 'Check landmarks, heading order, names and labels, interactive controls, keyboard risks, dialog focus risks, and prioritized accessibility fixes.',
        'links': 'Map internal links, external links, navigation links, missing or suspicious hrefs, repeated calls to action, and link-quality follow-up checks.',
    }
    return (
        f'URL: {url}\n'
        f'Objective: {objectives.get(kind, objectives["audit"])}\n'
        f'Additional context: {context or "No additional context provided."}'
    )


def _safe_token(value: str) -> str:
    return ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '-' for ch in value.lower()) or 'unknown'
