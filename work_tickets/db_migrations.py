from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, Engine, inspect

_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]+)_(?P<name>[a-z0-9_]+)\.sql$")
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def apply_migrations(engine: Engine, migrations_dir: Path = _MIGRATIONS_DIR) -> None:
    """Apply each database migration once, while allowing interrupted runs to retry.

    Migration files contain idempotent SQL so a migration can safely be retried if
    the process stops before its row is recorded. The initial migration also has a
    small compatibility step for databases created before migrations were added.
    """
    migrations = _discover_migrations(migrations_dir)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER NOT NULL PRIMARY KEY, "
            "name VARCHAR(255) NOT NULL, "
            "applied_at DATETIME NOT NULL"
            ")"
        )
        applied_versions = {
            int(version)
            for (version,) in connection.exec_driver_sql("SELECT version FROM schema_migrations")
        }

        for version, name, path in migrations:
            if version in applied_versions:
                continue
            _apply_sql_file(connection, path)
            if version == 1:
                _upgrade_pre_migration_schema(connection)
            connection.exec_driver_sql(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, datetime.now(UTC).replace(tzinfo=None)),
            )


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, str, Path]]:
    migrations: list[tuple[int, str, Path]] = []
    seen_versions: set[int] = set()
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"Invalid migration filename: {path.name}")
        version = int(match.group("version"))
        if version in seen_versions:
            raise ValueError(f"Duplicate migration version: {version}")
        seen_versions.add(version)
        migrations.append((version, match.group("name"), path))
    return sorted(migrations)


def _apply_sql_file(connection: Connection, path: Path) -> None:
    # The migration files intentionally contain one uncomplicated SQL statement
    # per semicolon. SQLite's DB-API execute method does not accept a script with
    # multiple statements, so execute them individually in the engine transaction.
    for statement in path.read_text(encoding="utf-8").split(";"):
        statement = statement.strip()
        if statement:
            connection.exec_driver_sql(statement)


def _upgrade_pre_migration_schema(connection: Connection) -> None:
    """Complete the schema made by releases before the migration runner existed."""
    tables = set(inspect(connection).get_table_names())
    if "jira_config" not in tables:
        return

    columns = {column["name"] for column in inspect(connection).get_columns("jira_config")}
    if "browser_base_url" in columns:
        return

    connection.exec_driver_sql(
        "ALTER TABLE jira_config ADD COLUMN browser_base_url VARCHAR(300) NOT NULL DEFAULT ''"
    )
    connection.exec_driver_sql(
        "UPDATE jira_config SET browser_base_url = base_url WHERE browser_base_url = ''"
    )
