from scripts.readme_snapshot import comparison_rows


def test_comparison_rows_include_docs_mapped_projects() -> None:
    rows = comparison_rows()
    projects = {row['project'] for row in rows}

    assert {'easy-agent', 'OpenAI Agents SDK', 'AutoGen', 'LangGraph'}.issubset(projects)
