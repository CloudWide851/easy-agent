from __future__ import annotations

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
