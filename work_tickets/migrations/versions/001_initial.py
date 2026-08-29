"""Create the initial Work Tickets schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())

    if "categories" not in tables:
        op.create_table(
            "categories",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )

    if "jira_config" not in tables:
        op.create_table(
            "jira_config",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("base_url", sa.String(length=300), nullable=False),
            sa.Column(
                "browser_base_url",
                sa.String(length=300),
                server_default="",
                nullable=False,
            ),
            sa.Column("email", sa.String(length=320), nullable=False),
            sa.Column("api_token", sa.String(length=300), nullable=False),
            sa.Column("project_key", sa.String(length=40), nullable=False),
            sa.Column("issue_type", sa.String(length=80), nullable=False),
            sa.Column("completed_statuses", sa.String(length=500), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    elif "browser_base_url" not in {
        column["name"] for column in inspect(bind).get_columns("jira_config")
    }:
        if "base_url" not in {
            column["name"] for column in inspect(bind).get_columns("jira_config")
        }:
            raise RuntimeError("jira_config is missing the required base_url column")
        op.add_column(
            "jira_config",
            sa.Column(
                "browser_base_url",
                sa.String(length=300),
                server_default="",
                nullable=False,
            ),
        )

    if "tickets" not in tables:
        op.create_table(
            "tickets",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("parent_id", sa.Integer(), nullable=True),
            sa.Column("summary", sa.String(length=240), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("planned_date", sa.Date(), nullable=True),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("local_completed", sa.Boolean(), nullable=False),
            sa.Column("jira_issue_key", sa.String(length=40), nullable=True),
            sa.Column("jira_status_name", sa.String(length=80), nullable=True),
            sa.Column("synced_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("category_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
            sa.ForeignKeyConstraint(["parent_id"], ["tickets.id"]),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    op.drop_table("tickets")
    op.drop_table("jira_config")
    op.drop_table("categories")
