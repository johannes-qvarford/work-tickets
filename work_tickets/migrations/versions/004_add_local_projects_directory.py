"""Add the local projects directory setting."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "004_add_local_projects_directory"
down_revision = "003_add_local_components"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "local_projects_directory" not in {
        column["name"] for column in inspect(op.get_bind()).get_columns("jira_config")
    }:
        op.add_column(
            "jira_config",
            sa.Column(
                "local_projects_directory",
                sa.String(length=1000),
                server_default="",
                nullable=False,
            ),
        )


def downgrade() -> None:
    op.drop_column("jira_config", "local_projects_directory")
