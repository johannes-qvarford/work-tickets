"""Add local components and ordered category assignments."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "003_add_local_components"
down_revision = "002_add_ticket_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "components" not in tables:
        op.create_table(
            "components",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )
    if "category_components" not in tables:
        op.create_table(
            "category_components",
            sa.Column("category_id", sa.Integer(), nullable=False),
            sa.Column("component_id", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),
            sa.ForeignKeyConstraint(["component_id"], ["components.id"]),
            sa.PrimaryKeyConstraint("category_id", "component_id"),
        )
    if "component" not in {column["name"] for column in inspect(bind).get_columns("tickets")}:
        op.add_column("tickets", sa.Column("component", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "component")
    op.drop_table("category_components")
    op.drop_table("components")
