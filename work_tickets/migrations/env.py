from __future__ import annotations

from alembic import context
from sqlalchemy import Connection


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if not isinstance(connection, Connection):
        raise RuntimeError("An active SQLAlchemy connection is required to run migrations")

    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
