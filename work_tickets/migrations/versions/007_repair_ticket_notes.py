"""Make ticket notes non-null after the original nullable migration."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "007_repair_ticket_notes"
down_revision = "006_add_gitlab_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    notes_column = next(
        (column for column in inspect(bind).get_columns("tickets") if column["name"] == "notes"),
        None,
    )
    if notes_column is None:
        return

    bind.execute(sa.text("UPDATE tickets SET notes = '' WHERE notes IS NULL"))
    if notes_column["nullable"] or notes_column["default"] != "''":
        with op.batch_alter_table("tickets", recreate="always") as batch_op:
            batch_op.alter_column(
                "notes",
                existing_type=sa.Text(),
                existing_nullable=notes_column["nullable"],
                existing_server_default=notes_column["default"],
                nullable=False,
                server_default="",
            )


def downgrade() -> None:
    with op.batch_alter_table("tickets", recreate="always") as batch_op:
        batch_op.alter_column(
            "notes",
            existing_type=sa.Text(),
            existing_nullable=False,
            existing_server_default="",
            nullable=True,
            server_default=None,
        )
