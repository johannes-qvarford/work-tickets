"""Add the configurable Implement prompt template."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "009_add_implement_prompt_template"
down_revision = "008_add_opencode_sessions"
branch_labels = None
depends_on = None

DEFAULT_IMPLEMENT_PROMPT_TEMPLATE = (
    "Please implement the work described at <TICKET_URL> and run the relevant tests."
)


def upgrade() -> None:
    if "implement_prompt_template" not in {
        column["name"] for column in inspect(op.get_bind()).get_columns("jira_config")
    }:
        op.add_column(
            "jira_config",
            sa.Column(
                "implement_prompt_template",
                sa.Text(),
                server_default=DEFAULT_IMPLEMENT_PROMPT_TEMPLATE,
                nullable=False,
            ),
        )


def downgrade() -> None:
    op.drop_column("jira_config", "implement_prompt_template")
