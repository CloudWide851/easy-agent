from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from agent_common.models import RunContext


class GitHubAutomationError(RuntimeError):
    pass


_GH_ISSUE_FIELDS = 'number,title,state,labels,assignees,url,updatedAt,author'
_GH_ISSUE_VIEW_FIELDS = 'number,title,state,body,labels,assignees,url,updatedAt,author,comments'


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float = 60.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = (result.stderr or '').strip()
        stdout = (result.stdout or '').strip()
        message = stderr or stdout or f'command failed: {command}'
        raise GitHubAutomationError(message)
    return result


def _ensure_gh_installed() -> str:
    executable = shutil.which('gh')
    if executable is None:
        raise GitHubAutomationError('GitHub CLI `gh` is not installed. Install it first and run `gh auth login`.')
    return executable


def _ensure_gh_auth(repo_root: Path) -> None:
    _ensure_gh_installed()
    _run_command(['gh', 'auth', 'status'], cwd=repo_root, timeout_seconds=20.0)


def _ensure_repo_root(start_path: Path) -> Path:
    resolved = start_path.resolve()
    probe = resolved if resolved.is_dir() else resolved.parent
    result = _run_command(['git', 'rev-parse', '--show-toplevel'], cwd=probe, timeout_seconds=20.0)
    repo_root = Path((result.stdout or '').strip())
    if not repo_root:
        raise GitHubAutomationError('Failed to resolve git repository root.')
    return repo_root.resolve()


def _slugify(value: str, *, max_length: int = 48) -> str:
    lowered = value.strip().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', lowered).strip('-')
    slug = re.sub(r'-{2,}', '-', slug)
    return (slug or 'issue')[:max_length].rstrip('-') or 'issue'


def _repo_relative_path(repo_root: Path, raw_path: str | Path) -> str:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (repo_root / candidate).resolve()
    try:
        relative = resolved.relative_to(repo_root)
    except ValueError as exc:
        raise GitHubAutomationError(f'Path escapes repository root: {raw_path}') from exc
    return str(relative).replace('\\', '/')


def _json_output(command: list[str], *, cwd: Path, timeout_seconds: float = 60.0) -> Any:
    result = _run_command(command, cwd=cwd, timeout_seconds=timeout_seconds)
    try:
        return json.loads(result.stdout or 'null')
    except json.JSONDecodeError as exc:
        raise GitHubAutomationError(f'Invalid JSON from command: {command}') from exc


def _normalize_labels(arguments: dict[str, Any]) -> list[str]:
    labels = arguments.get('labels')
    if labels is None and arguments.get('label') is not None:
        labels = [arguments['label']]
    if labels is None:
        return []
    if isinstance(labels, str):
        return [labels]
    if isinstance(labels, list):
        return [str(item) for item in labels if str(item).strip()]
    raise GitHubAutomationError('`labels` must be a string or a list of strings.')


def _normalize_issue(issue: dict[str, Any]) -> dict[str, Any]:
    labels = [str(item.get('name', '')) for item in issue.get('labels', []) if str(item.get('name', '')).strip()]
    assignees = [str(item.get('login', '')) for item in issue.get('assignees', []) if str(item.get('login', '')).strip()]
    author = issue.get('author') or {}
    comments = issue.get('comments', [])
    return {
        'number': int(issue['number']),
        'title': str(issue.get('title', '')),
        'state': str(issue.get('state', '')),
        'body': str(issue.get('body', '')),
        'labels': labels,
        'assignees': assignees,
        'url': str(issue.get('url', '')),
        'updated_at': str(issue.get('updatedAt', '')),
        'author': str(author.get('login', '')),
        'comment_count': len(comments),
        'comments': [
            {
                'author': str((comment.get('author') or {}).get('login', '')),
                'body': str(comment.get('body', '')),
                'created_at': str(comment.get('createdAt', '')),
                'updated_at': str(comment.get('updatedAt', '')),
            }
            for comment in comments
        ],
    }


def _issue_branch_name(number: int, title: str) -> str:
    return f'issue/{number}-{_slugify(title)}'


def _switch_issue_branch(repo_root: Path, branch_name: str) -> dict[str, Any]:
    current_branch = (_run_command(['git', 'branch', '--show-current'], cwd=repo_root).stdout or '').strip()
    exists = _run_command(['git', 'show-ref', '--verify', '--quiet', f'refs/heads/{branch_name}'], cwd=repo_root, check=False)
    created = exists.returncode != 0
    if created:
        result = _run_command(['git', 'switch', '-c', branch_name], cwd=repo_root, check=False)
        if result.returncode != 0:
            _run_command(['git', 'checkout', '-b', branch_name], cwd=repo_root)
    else:
        result = _run_command(['git', 'switch', branch_name], cwd=repo_root, check=False)
        if result.returncode != 0:
            _run_command(['git', 'checkout', branch_name], cwd=repo_root)
    return {'current_branch': current_branch, 'branch_name': branch_name, 'created': created}


def _write_issue_task_package(repo_root: Path, issue: dict[str, Any], branch_name: str) -> dict[str, str]:
    issue_root = repo_root / '.easy-agent' / 'github-automation' / 'issues' / str(issue['number'])
    issue_root.mkdir(parents=True, exist_ok=True)
    payload_path = issue_root / 'issue.json'
    task_path = issue_root / 'task-package.md'
    payload_path.write_text(json.dumps(issue, ensure_ascii=False, indent=2), encoding='utf-8')
    comment_lines: list[str] = []
    for comment in issue['comments'][:5]:
        author = comment['author'] or 'unknown'
        body = comment['body'].strip().replace('\r\n', '\n')
        preview = body[:240] + ('...' if len(body) > 240 else '')
        comment_lines.append(f'- {author}: {preview}')
    if not comment_lines:
        comment_lines.append('- No issue comments yet.')
    task_path.write_text(
        textwrap.dedent(
            f'''\
            # Issue #{issue['number']} Repair Package

            - Title: {issue['title']}
            - State: {issue['state']}
            - Branch: {branch_name}
            - URL: {issue['url']}
            - Labels: {', '.join(issue['labels']) if issue['labels'] else 'none'}
            - Assignees: {', '.join(issue['assignees']) if issue['assignees'] else 'none'}
            - Updated At: {issue['updated_at']}
            - Author: {issue['author'] or 'unknown'}

            ## Problem Statement

            {issue['body'].strip() or 'No issue body provided.'}

            ## Recent Comments

            {chr(10).join(comment_lines)}

            ## Repair Checklist

            - Reproduce the reported issue locally.
            - Identify the smallest safe fix.
            - Add or update tests before final verification.
            - Run Python lint, type-check, and the relevant pytest scope.
            - Update README or changelog only if the user-facing contract changed.
            '''
        ).strip()
        + '\n',
        encoding='utf-8',
    )
    return {
        'issue_root': str(issue_root),
        'issue_json': str(payload_path),
        'task_package': str(task_path),
    }


def github_issue_list(arguments: dict[str, Any], context: RunContext) -> dict[str, Any]:
    _ensure_gh_installed()
    repo_root = _ensure_repo_root(context.workdir)
    _ensure_gh_auth(repo_root)
    limit = int(arguments.get('limit', 20) or 20)
    state = str(arguments.get('state', 'open') or 'open')
    labels = _normalize_labels(arguments)
    command = ['gh', 'issue', 'list', '--limit', str(limit), '--state', state, '--json', _GH_ISSUE_FIELDS]
    for label in labels:
        command.extend(['--label', label])
    if arguments.get('assignee'):
        command.extend(['--assignee', str(arguments['assignee'])])
    if arguments.get('search'):
        command.extend(['--search', str(arguments['search'])])
    payload = _json_output(command, cwd=repo_root, timeout_seconds=60.0)
    issues = [_normalize_issue(dict(item)) for item in payload]
    for issue in issues:
        issue.pop('body', None)
        issue.pop('comments', None)
        issue.pop('comment_count', None)
    return {
        'repo_root': str(repo_root),
        'count': len(issues),
        'filters': {
            'limit': limit,
            'state': state,
            'labels': labels,
            'assignee': str(arguments.get('assignee') or ''),
            'search': str(arguments.get('search') or ''),
        },
        'issues': issues,
    }


def github_issue_prepare_fix(arguments: dict[str, Any], context: RunContext) -> dict[str, Any]:
    _ensure_gh_installed()
    repo_root = _ensure_repo_root(context.workdir)
    _ensure_gh_auth(repo_root)
    number_raw = arguments.get('number', arguments.get('issue_number'))
    if number_raw in (None, ''):
        raise GitHubAutomationError('`number` is required.')
    number = int(str(number_raw))
    payload = _json_output(
        ['gh', 'issue', 'view', str(number), '--json', _GH_ISSUE_VIEW_FIELDS],
        cwd=repo_root,
        timeout_seconds=60.0,
    )
    issue = _normalize_issue(dict(payload))
    branch_name = str(arguments.get('branch_name') or _issue_branch_name(issue['number'], issue['title']))
    branch_state = _switch_issue_branch(repo_root, branch_name)
    artifacts = _write_issue_task_package(repo_root, issue, branch_name)
    suggested_tests = [
        '.\\.venv\\Scripts\\python.exe -m ruff check src tests scripts',
        '.\\.venv\\Scripts\\python.exe -m mypy src tests scripts',
        '.\\.venv\\Scripts\\python.exe -m pytest tests/unit -q --basetemp=%TEMP%\\easy-agent-pytest\\unit-issue-<timestamp>',
    ]
    return {
        'repo_root': str(repo_root),
        'issue': {key: value for key, value in issue.items() if key != 'comments'},
        'branch': branch_state,
        'artifacts': artifacts,
        'suggested_test_commands': suggested_tests,
    }


def git_commit_local(arguments: dict[str, Any], context: RunContext) -> dict[str, Any]:
    repo_root = _ensure_repo_root(context.workdir)
    message = str(arguments.get('message') or '').strip()
    if not message:
        raise GitHubAutomationError('`message` is required.')
    stage_all = bool(arguments.get('all', False))
    raw_paths = arguments.get('paths')
    if not stage_all and not raw_paths:
        raise GitHubAutomationError('Provide `paths` or set `all=true`.')
    if stage_all:
        _run_command(['git', 'add', '--all'], cwd=repo_root)
    else:
        if isinstance(raw_paths, str):
            normalized_paths = [_repo_relative_path(repo_root, raw_paths)]
        elif isinstance(raw_paths, list):
            normalized_paths = [_repo_relative_path(repo_root, item) for item in raw_paths]
        else:
            raise GitHubAutomationError('`paths` must be a string or a list of paths.')
        _run_command(['git', 'add', '--', *normalized_paths], cwd=repo_root)
    staged_output = _run_command(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR'], cwd=repo_root)
    staged_files = [line.strip() for line in (staged_output.stdout or '').splitlines() if line.strip()]
    if not staged_files:
        raise GitHubAutomationError('No staged changes to commit.')
    _run_command(['git', 'commit', '-m', message], cwd=repo_root, timeout_seconds=120.0)
    commit_sha = (_run_command(['git', 'rev-parse', 'HEAD'], cwd=repo_root).stdout or '').strip()
    return {
        'repo_root': str(repo_root),
        'commit_sha': commit_sha,
        'message': message,
        'staged_files': staged_files,
    }


def github_release_publish(arguments: dict[str, Any], context: RunContext) -> dict[str, Any]:
    _ensure_gh_installed()
    repo_root = _ensure_repo_root(context.workdir)
    _ensure_gh_auth(repo_root)
    tag_name = str(arguments.get('tag_name') or '').strip()
    title = str(arguments.get('title') or '').strip()
    if not tag_name:
        raise GitHubAutomationError('`tag_name` is required.')
    if not title:
        raise GitHubAutomationError('`title` is required.')
    dirty = _run_command(['git', 'status', '--porcelain'], cwd=repo_root)
    if (dirty.stdout or '').strip():
        raise GitHubAutomationError('Release publishing requires a clean git worktree.')
    command = ['gh', 'release', 'create', tag_name, '--title', title]
    notes_file = arguments.get('notes_file')
    if notes_file:
        normalized = _repo_relative_path(repo_root, str(notes_file))
        command.extend(['--notes-file', normalized])
    else:
        command.append('--generate-notes')
    if bool(arguments.get('draft', False)):
        command.append('--draft')
    if bool(arguments.get('prerelease', False)):
        command.append('--prerelease')
    result = _run_command(command, cwd=repo_root, timeout_seconds=180.0)
    return {
        'repo_root': str(repo_root),
        'tag_name': tag_name,
        'title': title,
        'draft': bool(arguments.get('draft', False)),
        'prerelease': bool(arguments.get('prerelease', False)),
        'release_url': (result.stdout or '').strip(),
    }


def github_issue_list_skill(arguments: dict[str, Any], context: RunContext) -> dict[str, Any]:
    return github_issue_list(arguments, context)


def github_issue_prepare_fix_skill(arguments: dict[str, Any], context: RunContext) -> dict[str, Any]:
    return github_issue_prepare_fix(arguments, context)


def git_commit_local_skill(arguments: dict[str, Any], context: RunContext) -> dict[str, Any]:
    return git_commit_local(arguments, context)


def github_release_publish_skill(arguments: dict[str, Any], context: RunContext) -> dict[str, Any]:
    return github_release_publish(arguments, context)

