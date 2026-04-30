from __future__ import annotations

from pathlib import Path

from agent_runtime import AgentApp


def test_agent_app_runs_mock_config(tmp_path: Path) -> None:
    config_path = tmp_path / 'easy-agent.yml'
    storage_path = str(tmp_path / 'state').replace('\\', '/')
    config_path.write_text(
        f"""
model:
  provider: mock
  protocol: mock
  model: mock-agent
  base_url: mock://local
  api_key_env: EASY_AGENT_MOCK_API_KEY
graph:
  entrypoint: assistant
  agents:
    - name: assistant
      system_prompt: Reply concisely.
      max_iterations: 2
storage:
  path: {storage_path}
  database: state.db
""",
        encoding='utf-8',
    )

    app = AgentApp.from_config(config_path)
    try:
        result = app.run('hello from facade')
    finally:
        app.close()

    assert result['status'] == 'succeeded'
    assert 'hello from facade' in str(result['result'])


def test_agent_app_runs_task_and_loads_trace(tmp_path: Path) -> None:
    config_path = tmp_path / 'easy-agent.yml'
    storage_path = str(tmp_path / 'state').replace('\\', '/')
    config_path.write_text(
        f"""
model:
  provider: mock
  protocol: mock
  model: mock-agent
  base_url: mock://local
  api_key_env: EASY_AGENT_MOCK_API_KEY
graph:
  entrypoint: assistant
  agents:
    - name: assistant
      system_prompt: Use python_echo once.
      tools:
        - python_echo
      max_iterations: 3
skills:
  - path: skills/examples
storage:
  path: {storage_path}
  database: state.db
""",
        encoding='utf-8',
    )

    app = AgentApp.from_config(config_path)
    try:
        result = app.run_task('repo-review', context='focus facade tests')
        trace = app.trace(str(result['run_id']))
    finally:
        app.close()

    assert result['status'] == 'succeeded'
    assert trace['run']['run_id'] == result['run_id']


def test_agent_app_workflow_browser_and_bundle_helpers(tmp_path: Path) -> None:
    config_path = tmp_path / 'easy-agent.yml'
    storage_path = str(tmp_path / 'state').replace('\\', '/')
    config_path.write_text(
        f"""
model:
  provider: mock
  protocol: mock
  model: mock-agent
  base_url: mock://local
  api_key_env: EASY_AGENT_MOCK_API_KEY
graph:
  entrypoint: assistant
  agents:
    - name: assistant
      system_prompt: Use python_echo once.
      tools:
        - python_echo
      max_iterations: 3
skills:
  - path: skills/examples
storage:
  path: {storage_path}
  database: state.db
""",
        encoding='utf-8',
    )
    workflow_path = tmp_path / 'workflow.yml'
    workflow_path.write_text(
        """
version: 1
name: facade
pack: repo-review
context: focus facade workflow
approval_mode: hybrid
bundle_on_completion: false
""",
        encoding='utf-8',
    )

    app = AgentApp.from_config(config_path)
    try:
        plan = app.workflow_plan(workflow_path)
        result = app.run_workflow(workflow_path)
        browser = app.browser_audit('https://example.com', kind='seo')
        bundle = app.run_bundle(str(result['run_id']), output_dir=tmp_path / 'bundle', force=True)
    finally:
        app.close()

    assert plan['pack'] == 'repo-review'
    assert 'focus facade workflow' in str(plan['prompt'])
    assert result['status'] == 'succeeded'
    assert browser['pack'] == 'browser-audit'
    assert 'SEO fixes' in str(browser['prompt'])
    assert bundle['mode'] == 'advice_only'
    assert (tmp_path / 'bundle' / 'README.md').exists()
