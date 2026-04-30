from __future__ import annotations

import asyncio
import json
from html import escape
from pathlib import Path
from typing import Any, cast

from agent_runtime.connectors import connector_checks, connector_summary
from agent_runtime.reports import build_report_trend, latest_report_payload
from agent_runtime.runtime import build_runtime


def dashboard_payload(
    config: Path,
    *,
    history: Path = Path('.easy-agent'),
    run_limit: int = 30,
) -> dict[str, Any]:
    latest = latest_report_payload(config, run_limit=run_limit)
    trend = build_report_trend(history)
    checks = connector_checks(config)
    runtime = build_runtime(config)
    try:
        runs = runtime.store.list_runs(limit=run_limit)
        approvals = runtime.list_human_requests(status=None, run_id=None)
    finally:
        asyncio.run(runtime.aclose())
    pending = [item for item in approvals if str(item.get('status')) == 'pending']
    return {
        'config': str(config),
        'history': str(history),
        'latest': latest,
        'trend': trend,
        'connectors': {
            'summary': connector_summary(checks),
            'checks': [check.__dict__ for check in checks],
        },
        'runs': runs,
        'approvals': {
            'pending': pending,
            'total': len(approvals),
        },
    }


def dashboard_html(payload: dict[str, Any]) -> str:
    latest = cast(dict[str, Any], payload.get('latest') if isinstance(payload.get('latest'), dict) else {})
    reports = cast(dict[str, Any], latest.get('reports') if isinstance(latest.get('reports'), dict) else {})
    connectors = cast(dict[str, Any], payload.get('connectors') if isinstance(payload.get('connectors'), dict) else {})
    raw_connector_checks = connectors.get('checks')
    connector_check_rows: list[Any] = raw_connector_checks if isinstance(raw_connector_checks, list) else []
    raw_runs = payload.get('runs')
    runs: list[Any] = raw_runs if isinstance(raw_runs, list) else []
    approvals = cast(dict[str, Any], payload.get('approvals') if isinstance(payload.get('approvals'), dict) else {})
    trend = cast(dict[str, Any], payload.get('trend') if isinstance(payload.get('trend'), dict) else {})
    raw_json = escape(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    report_cards = ''.join(_report_card(name, item if isinstance(item, dict) else {}) for name, item in reports.items())
    connector_rows = ''.join(_connector_row(item if isinstance(item, dict) else {}) for item in connector_check_rows)
    run_rows = ''.join(_run_row(item if isinstance(item, dict) else {}) for item in runs[:12])
    raw_pending = approvals.get('pending')
    pending_items: list[Any] = raw_pending if isinstance(raw_pending, list) else []
    pending_count = len(pending_items)
    trend_cards = ''.join(_trend_card(name, item if isinstance(item, dict) else {}) for name, item in cast(dict[str, Any], trend.get('surfaces') if isinstance(trend.get('surfaces'), dict) else {}).items())

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>easy-agent dashboard</title>
  <style>
    :root {{ color-scheme: light dark; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f4f1ea; color: #20242b; }}
    header {{ border-bottom: 1px solid #d7cfbf; background: #faf8f2; }}
    .wrap {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; }}
    .hero {{ display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(260px, .8fr); gap: 28px; padding: 34px 0 28px; align-items: end; }}
    h1 {{ margin: 0; font-size: 32px; line-height: 1.1; letter-spacing: 0; }}
    .lead {{ margin: 10px 0 0; max-width: 780px; color: #5d6470; }}
    .status-strip {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }}
    .stat {{ border: 1px solid #d7cfbf; border-radius: 8px; padding: 12px; background: #ffffff; }}
    .stat strong {{ display: block; font-size: 22px; }}
    .stat span {{ color: #6b7280; font-size: 12px; }}
    main {{ padding: 26px 0 42px; }}
    section {{ margin-top: 24px; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
    .card, table {{ background: #fffdfa; border: 1px solid #d7cfbf; border-radius: 8px; }}
    .card {{ padding: 14px; }}
    .card h3 {{ margin: 0 0 8px; font-size: 15px; }}
    .score {{ font-size: 28px; font-weight: 750; margin: 10px 0 4px; }}
    .muted {{ color: #6b7280; font-size: 13px; }}
    .pill {{ display: inline-block; padding: 3px 8px; border-radius: 999px; border: 1px solid #c9c0b0; font-size: 12px; }}
    .ok {{ color: #12613a; background: #eaf6ee; }}
    .warn {{ color: #8a5a00; background: #fff3d7; }}
    .error, .failed {{ color: #9f1d1d; background: #ffe4e4; }}
    table {{ width: 100%; border-collapse: collapse; overflow: hidden; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #eee4d5; text-align: left; vertical-align: top; font-size: 13px; }}
    tr:last-child td {{ border-bottom: 0; }}
    pre {{ overflow: auto; padding: 12px; border-radius: 8px; background: #20242b; color: #f4f1ea; }}
    @media (max-width: 780px) {{ .hero {{ grid-template-columns: 1fr; }} .status-strip {{ grid-template-columns: 1fr; }} }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #171717; color: #f3efe5; }}
      header {{ background: #20201f; border-color: #38332a; }}
      .lead, .muted {{ color: #b9b0a2; }}
      .card, .stat, table {{ background: #20201f; border-color: #38332a; }}
      th, td {{ border-color: #38332a; }}
      pre {{ background: #0f1115; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap hero">
      <div>
        <h1>easy-agent dashboard</h1>
        <p class="lead">Read-only local evidence for configuration, connectors, recent runs, approvals, and report movement.</p>
      </div>
      <div class="status-strip">
        <div class="stat"><strong>{escape(str(len(runs)))}</strong><span>recent runs</span></div>
        <div class="stat"><strong>{escape(str(pending_count))}</strong><span>pending approvals</span></div>
        <div class="stat"><strong>{escape(str(connectors.get('summary', {}).get('warn', 0)))}</strong><span>connector warnings</span></div>
      </div>
    </div>
  </header>
  <main class="wrap">
    <section>
      <h2>Reports</h2>
      <div class="grid">{report_cards or '<p class="muted">No report summaries available.</p>'}</div>
    </section>
    <section>
      <h2>Trend</h2>
      <div class="grid">{trend_cards or '<p class="muted">No trend points available.</p>'}</div>
    </section>
    <section>
      <h2>Connectors</h2>
      <table><thead><tr><th>Name</th><th>Status</th><th>Message</th><th>Action</th></tr></thead><tbody>{connector_rows}</tbody></table>
    </section>
    <section>
      <h2>Recent Runs</h2>
      <table><thead><tr><th>Run</th><th>Kind</th><th>Status</th><th>Created</th></tr></thead><tbody>{run_rows or '<tr><td colspan="4" class="muted">No runs recorded.</td></tr>'}</tbody></table>
    </section>
    <section>
      <details>
        <summary>Raw dashboard JSON</summary>
        <pre>{raw_json}</pre>
      </details>
    </section>
  </main>
</body>
</html>
"""


def _report_card(name: str, item: dict[str, Any]) -> str:
    score = item.get('score')
    return (
        '<article class="card">'
        f'<h3>{escape(str(name))}</h3>'
        f'<span class="pill {escape(str(item.get("status", "")))}">{escape(str(item.get("status", "unknown")))}</span>'
        f'<div class="score">{escape(str(score if score is not None else "-"))}</div>'
        f'<p class="muted">{escape(str(item.get("summary") or "-"))}</p>'
        '</article>'
    )


def _trend_card(name: str, item: dict[str, Any]) -> str:
    raw_latest = item.get('latest')
    latest: dict[str, Any] = raw_latest if isinstance(raw_latest, dict) else {}
    return (
        '<article class="card">'
        f'<h3>{escape(str(name))}</h3>'
        f'<span class="pill">delta {escape(str(item.get("score_delta") if item.get("score_delta") is not None else "-"))}</span>'
        f'<div class="score">{escape(str(latest.get("score") if latest.get("score") is not None else "-"))}</div>'
        f'<p class="muted">{escape(str(latest.get("summary") or latest.get("status") or "-"))}</p>'
        '</article>'
    )


def _connector_row(item: dict[str, Any]) -> str:
    status = str(item.get('status') or 'unknown')
    return (
        '<tr>'
        f'<td>{escape(str(item.get("name") or "-"))}</td>'
        f'<td><span class="pill {escape(status)}">{escape(status)}</span></td>'
        f'<td>{escape(str(item.get("message") or "-"))}</td>'
        f'<td>{escape(str(item.get("action") or "-"))}</td>'
        '</tr>'
    )


def _run_row(item: dict[str, Any]) -> str:
    status = str(item.get('status') or 'unknown')
    return (
        '<tr>'
        f'<td>{escape(str(item.get("run_id") or "-"))}</td>'
        f'<td>{escape(str(item.get("run_kind") or "-"))}</td>'
        f'<td><span class="pill {escape(status)}">{escape(status)}</span></td>'
        f'<td>{escape(str(item.get("created_at") or "-"))}</td>'
        '</tr>'
    )
