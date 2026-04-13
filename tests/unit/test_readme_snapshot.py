from pathlib import Path

from scripts.readme_snapshot import comparison_rows


def test_comparison_rows_include_docs_mapped_projects() -> None:
    rows = comparison_rows()
    projects = {row['project'] for row in rows}

    assert {'easy-agent', 'OpenHands', 'Skyvern', 'AutoGPT Platform'}.issubset(projects)


def test_readmes_reference_logo_and_split_reference_docs() -> None:
    english = Path('README.md').read_text(encoding='utf-8')
    chinese = Path('README.zh-CN.md').read_text(encoding='utf-8')
    english_reference = Path('reference/en/test-results.md').read_text(encoding='utf-8')
    chinese_reference = Path('reference/zh/test-results.md').read_text(encoding='utf-8')

    assert './logo.svg' in english
    assert './logo.svg' in chinese
    assert './reference/en/test-results.md' in english
    assert './reference/en/usage-guide.md' in english
    assert './reference/en/next-reinforcement.md' in english
    assert './reference/zh/test-results.md' in chinese
    assert './reference/zh/usage-guide.md' in chinese
    assert './reference/zh/next-reinforcement.md' in chinese
    assert 'Warm-Start Telemetry Snapshot' not in english
    assert 'Warm-Start Telemetry 快照' not in chinese
    assert 'https://linux.do/logo-128.svg' not in english
    assert 'https://linux.do/logo-128.svg' not in chinese
    assert '[Linux.do](https://linux.do/)' in english
    assert '[Linux.do](https://linux.do/)' in chinese
    assert '.easy-agent/' not in english
    assert '.easy-agent/' not in chinese
    assert 'MEMORY.md' not in english
    assert 'MEMORY.md' not in chinese
    assert 'AGENTS.md' not in english
    assert 'AGENTS.md' not in chinese
    assert '.easy-agent/' not in english_reference
    assert '.easy-agent/' not in chinese_reference
    assert 'MEMORY.md' not in english_reference
    assert 'MEMORY.md' not in chinese_reference
    assert 'AGENTS.md' not in english_reference
    assert 'AGENTS.md' not in chinese_reference


def test_readmes_keep_required_section_order() -> None:
    english = Path('README.md').read_text(encoding='utf-8')
    chinese = Path('README.zh-CN.md').read_text(encoding='utf-8')

    english_sections = [
        '## What This Project Is',
        '## Who It Is For',
        '## Tech Stack',
        '## Features',
        '## Human Loop, Replay, and MCP',
        '## A2A Remote Agent Federation',
        '## Executor / Workbench Isolation',
        '## Architecture',
        '## Long-Running Harness Design',
        '## Protocol and Tool Model',
        '## Project Layout',
        '## Quick Start',
        '## What a Harness Run Produces',
        '## Verification',
        '## Real Network Test Set Results',
        '## Next Reinforcement',
        '## Design References',
        '## Acknowledgements',
        '## License',
    ]
    chinese_sections = [
        '## 这个项目到底是什么',
        '## 适合谁用',
        '## 技术栈',
        '## 能力一览',
        '## Human Loop、Replay 与 MCP',
        '## A2A Remote Agent Federation',
        '## Executor / Workbench Isolation',
        '## 架构说明',
        '## 长任务 Harness 设计',
        '## 协议与工具模型',
        '## 项目结构',
        '## 快速开始',
        '## 一次 Harness 运行会留下什么',
        '## 验证方式',
        '## 真实网络测试集结果',
        '## 下一步补强',
        '## 设计参考',
        '## 致谢',
        '## License',
    ]

    english_positions = [english.index(section) for section in english_sections]
    chinese_positions = [chinese.index(section) for section in chinese_sections]

    assert english_positions == sorted(english_positions)
    assert chinese_positions == sorted(chinese_positions)
