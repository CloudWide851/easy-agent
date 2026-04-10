from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_config.app import McpRootConfig


def root_to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def root_payload(root: McpRootConfig) -> dict[str, Any]:
    return {'path': root.path, 'name': root.name, 'uri': root_to_uri(root.path)}


def infer_stdio_filesystem_roots(transport: str, command: list[str]) -> list[McpRootConfig]:
    if transport != 'stdio' or not command:
        return []
    package_index = next((index for index, item in enumerate(command) if 'server-filesystem' in item), None)
    if package_index is None:
        return []
    roots: list[McpRootConfig] = []
    for raw in command[package_index + 1 :]:
        if raw.startswith('-'):
            continue
        roots.append(McpRootConfig(path=raw, name=Path(raw).name or None))
    return roots


def normalize_root_entries(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for item in roots:
        uri = str(item.get('uri') or '').strip()
        if not uri:
            continue
        deduped[uri] = {
            'path': str(item.get('path') or ''),
            'name': str(item.get('name') or '').strip() or None,
            'uri': uri,
        }
    return [deduped[key] for key in sorted(deduped)]


def diff_root_entries(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    previous_by_uri = {str(item['uri']): item for item in normalize_root_entries(previous)}
    current_by_uri = {str(item['uri']): item for item in normalize_root_entries(current)}
    added = [current_by_uri[uri] for uri in current_by_uri.keys() - previous_by_uri.keys()]
    removed = [previous_by_uri[uri] for uri in previous_by_uri.keys() - current_by_uri.keys()]
    changed = [
        {'previous': previous_by_uri[uri], 'current': current_by_uri[uri]}
        for uri in previous_by_uri.keys() & current_by_uri.keys()
        if previous_by_uri[uri] != current_by_uri[uri]
    ]
    return {'added': added, 'removed': removed, 'changed': changed}
