"""Add configurable Jira workflow status settings."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "005_add_jira_workflow_statuses"
down_revision = "004_add_local_projects_directory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing_columns = {
        column["name"] for column in inspect(op.get_bind()).get_columns("jira_config")
    }
    for name, default in (
        ("in_review_status", "In Review"),
        ("ready_to_merge_status", "Ready to Merge"),
        ("ready_to_deploy_status", "Ready to Deploy"),
    ):
        if name not in existing_columns:
            op.add_column(
                "jira_config",
                sa.Column(name, sa.String(length=80), server_default=default, nullable=False),
            )


def downgrade() -> None:
    op.drop_column("jira_config", "ready_to_deploy_status")
    op.drop_column("jira_config", "ready_to_merge_status")
    op.drop_column("jira_config", "in_review_status")
