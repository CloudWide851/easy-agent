from __future__ import annotations

import typer

from agent_cli.commands.approvals import approvals_app
from agent_cli.commands.catalog import (
    federation_app,
    mcp_app,
    plugins_app,
    skills_app,
    teams_app,
    workbench_app,
)
from agent_cli.commands.general import register as register_general
from agent_cli.commands.general import runs_app, traces_app
from agent_cli.commands.harness import harness_app
from agent_cli.commands.integration import integration_app
from agent_cli.commands.onboarding import (
    config_app,
    template_app,
)
from agent_cli.commands.onboarding import (
    register as register_onboarding,
)

app = typer.Typer(help='Engineered CLI for the easy-agent foundation.')
app.add_typer(skills_app, name='skills')
app.add_typer(mcp_app, name='mcp')
app.add_typer(plugins_app, name='plugins')
app.add_typer(teams_app, name='teams')
app.add_typer(federation_app, name='federation')
app.add_typer(workbench_app, name='workbench')
app.add_typer(approvals_app, name='approvals')
app.add_typer(harness_app, name='harness')
app.add_typer(integration_app, name='integration')
app.add_typer(runs_app, name='runs')
app.add_typer(traces_app, name='traces')
app.add_typer(template_app, name='template')
app.add_typer(config_app, name='config')
register_general(app)
register_onboarding(app)
