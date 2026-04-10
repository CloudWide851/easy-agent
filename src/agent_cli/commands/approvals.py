from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

from agent_common.models import HumanRequestStatus
from agent_runtime import build_runtime

console = Console()
approvals_app = typer.Typer(help='Inspect and resolve human approval requests.')


@approvals_app.command('list')
def list_approvals(
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    status: str | None = typer.Option(None, '--status', help='pending, approved, rejected, or cancelled'),
    run_id: str | None = typer.Option(None, '--run-id'),
) -> None:
    runtime = build_runtime(config)
    try:
        resolved_status = HumanRequestStatus(status) if status else None
        rows = runtime.list_human_requests(resolved_status, run_id)
        table = Table(title='approval requests')
        table.add_column('Request ID', style='cyan')
        table.add_column('Run ID', style='green')
        table.add_column('Kind', style='yellow')
        table.add_column('Status', style='magenta')
        table.add_column('Title', style='white')
        for row in rows:
            table.add_row(row['request_id'], row['run_id'], row['kind'], row['status'], row['title'])
        console.print(table)
    finally:
        asyncio.run(runtime.aclose())


@approvals_app.command('show')
def show_approval(
    request_id: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
) -> None:
    runtime = build_runtime(config)
    try:
        console.print_json(json.dumps(runtime.load_human_request(request_id), ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())


@approvals_app.command('approve')
def approve_approval(
    request_id: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    content_json: str | None = typer.Option(None, '--content-json', help='Optional JSON response payload.'),
) -> None:
    runtime = build_runtime(config)
    try:
        payload = json.loads(content_json) if content_json else None
        console.print_json(json.dumps(runtime.approve_human_request(request_id, payload), ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())


@approvals_app.command('reject')
def reject_approval(
    request_id: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    content_json: str | None = typer.Option(None, '--content-json', help='Optional JSON response payload.'),
) -> None:
    runtime = build_runtime(config)
    try:
        payload = json.loads(content_json) if content_json else None
        console.print_json(json.dumps(runtime.reject_human_request(request_id, payload), ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())


@approvals_app.command('cancel')
def cancel_approval(
    request_id: str = typer.Argument(...),
    config: str = typer.Option('easy-agent.yml', '-c', '--config'),
    content_json: str | None = typer.Option(None, '--content-json', help='Optional JSON response payload.'),
) -> None:
    runtime = build_runtime(config)
    try:
        payload = json.loads(content_json) if content_json else None
        console.print_json(json.dumps(runtime.cancel_human_request(request_id, payload), ensure_ascii=False))
    finally:
        asyncio.run(runtime.aclose())
