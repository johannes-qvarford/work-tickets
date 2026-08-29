"""Add local notes to tickets."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "002_add_ticket_notes"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "notes" not in {column["name"] for column in inspect(bind).get_columns("tickets")}:
        op.add_column(
            "tickets",
            sa.Column("notes", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("tickets", "notes")
