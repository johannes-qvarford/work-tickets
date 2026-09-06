"""Persist OpenCode sessions by Jira key and session kind."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "008_add_opencode_sessions"
down_revision = "007_repair_ticket_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "opencode_sessions" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "opencode_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("jira_key", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("session_id", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("jira_key", "kind"),
    )


def downgrade() -> None:
    op.drop_table("opencode_sessions")
