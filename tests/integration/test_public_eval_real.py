from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from agent_runtime.public_eval import run_public_eval_suite

pytestmark = [pytest.mark.real]


@pytest.mark.skipif(not os.environ.get('DEEPSEEK_API_KEY'), reason='requires DEEPSEEK_API_KEY')
def test_public_eval_suite_runs_with_live_model(tmp_path: Path) -> None:
    config = yaml.safe_load(Path('easy-agent.yml').read_text(encoding='utf-8'))
    public_eval = config.setdefault('evaluation', {}).setdefault('public_eval', {})
    official_dataset = public_eval.setdefault('official_dataset', {})
    web_search = public_eval.setdefault('web_search', {})
    hidden_root = tmp_path / '.easy-agent'
    official_dataset['checkpoint_path'] = str(hidden_root / 'public-eval-progress.json')
    official_dataset['manifest_path'] = str(hidden_root / 'public-eval-cache' / 'bfcl_v4_manifest.json')
    official_dataset['cache_dir'] = str(hidden_root / 'public-eval-cache')
    official_dataset['resume'] = False
    web_search['usage_path'] = str(hidden_root / 'public-eval-web-search-usage.json')
    config_path = tmp_path / 'easy-agent.public-eval.real.yml'
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')

    report = run_public_eval_suite(config_path)

    expected_suites = {
        'bfcl_simple',
        'bfcl_multiple',
        'bfcl_parallel_multiple',
        'bfcl_irrelevance',
        'bfcl_web_search',
        'bfcl_memory',
        'bfcl_format_sensitivity',
        'tau2_mock',
        'overall',
    }
    assert expected_suites.issubset(report['summary'])
    assert report['profile'] == 'full_v4'
    assert report['records']
    assert all('suite' in record and 'case_id' in record for record in report['records'])
    assert all(record['duration_seconds'] >= 0 for record in report['records'])


@pytest.mark.skipif(not os.environ.get('DEEPSEEK_API_KEY'), reason='requires DEEPSEEK_API_KEY')
def test_public_eval_suite_supports_official_profile_slice_with_live_model(tmp_path: Path) -> None:
    config = yaml.safe_load(Path('easy-agent.yml').read_text(encoding='utf-8'))
    fixture_cases = json.loads(Path('public_evals/fixtures/bfcl_subset.json').read_text(encoding='utf-8'))['cases']
    official_case = next(case for case in fixture_cases if case['id'] == 'simple_0')
    public_eval = config.setdefault('evaluation', {}).setdefault('public_eval', {})
    public_eval['profile'] = 'official_full_v4'
    official_dataset = public_eval.setdefault('official_dataset', {})
    hidden_root = tmp_path / '.easy-agent'
    manifest_path = hidden_root / 'public-eval-cache' / 'bfcl_v4_manifest.json'
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    raw_official_case = {
        'id': official_case['id'],
        'suite': official_case['suite'],
        'question': official_case['messages'][0]['content'],
        'tools': [
            {
                'name': item['name'],
                'description': item.get('description', ''),
                'inputSchema': item['parameters'],
            }
            for item in official_case['functions']
        ],
        'expected_tool_calls': [
            {
                'name': next(iter(item.keys())),
                'arguments': {key: values[0] for key, values in next(iter(item.values())).items()},
            }
            for item in official_case['ground_truth']
        ],
    }
    manifest_path.write_text(json.dumps({'bfcl_cases': [raw_official_case]}, ensure_ascii=False), encoding='utf-8')
    official_dataset['checkpoint_path'] = str(hidden_root / 'public-eval-progress.json')
    official_dataset['manifest_path'] = str(manifest_path)
    official_dataset['cache_dir'] = str(hidden_root / 'public-eval-cache')
    official_dataset['resume'] = False
    official_dataset['suite_allowlist'] = ['simple']
    official_dataset['case_allowlist'] = ['simple_0']
    official_dataset['max_cases'] = 1
    config_path = tmp_path / 'easy-agent.public-eval.official.real.yml'
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')

    report = run_public_eval_suite(config_path, profile='official_full_v4')

    assert report['profile'] == 'official_full_v4'
    assert report['scope'] == 'official_manifest'
    assert report['case_counts']['bfcl'] == 1
    assert report['sources']['official_selection']['suite_allowlist'] == ['simple']
    assert report['sources']['official_selection']['case_allowlist'] == ['simple_0']
