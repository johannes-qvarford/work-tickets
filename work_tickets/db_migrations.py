from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, inspect, text

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LEGACY_MIGRATIONS_TABLE = "schema_migrations"
_INITIAL_REVISION = "001_initial"
_REQUIRED_COLUMNS = {
    "categories": {"id", "name"},
    "jira_config": {
        "id",
        "base_url",
        "browser_base_url",
        "email",
        "api_token",
        "project_key",
        "issue_type",
        "completed_statuses",
        "updated_at",
    },
    "tickets": {
        "id",
        "parent_id",
        "summary",
        "description",
        "planned_date",
        "position",
        "local_completed",
        "jira_issue_key",
        "jira_status_name",
        "synced_at",
        "created_at",
        "updated_at",
        "category_id",
    },
}


def apply_migrations(engine: Engine, migrations_dir: Path = _MIGRATIONS_DIR) -> None:
    """Apply migrations with Alembic and keep old databases compatible."""
    config = _alembic_config(migrations_dir)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        legacy_version = _legacy_version(connection)
        if legacy_version is not None:
            if legacy_version != 1:
                raise ValueError(f"Unsupported legacy migration version: {legacy_version}")
            _prepare_legacy_schema(connection)
            # Version 1 is the only migration written by the old runner. Stamping
            # it tells Alembic that the existing schema already contains that
            # migration, without re-running DDL against a live database.
            command.stamp(config, _INITIAL_REVISION)

        command.upgrade(config, "head")
        _validate_current_schema(connection)

        if legacy_version is not None:
            connection.exec_driver_sql(f"DROP TABLE {_LEGACY_MIGRATIONS_TABLE}")


def _alembic_config(migrations_dir: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(migrations_dir))
    return config


def _legacy_version(connection: Connection) -> int | None:
    if _LEGACY_MIGRATIONS_TABLE not in inspect(connection).get_table_names():
        return None

    versions = list(
        connection.scalars(text(f"SELECT version FROM {_LEGACY_MIGRATIONS_TABLE} ORDER BY version"))
    )
    if len(versions) != 1:
        raise ValueError("Legacy migration tracking must contain exactly one migration")
    return int(versions[0])


def _prepare_legacy_schema(connection: Connection) -> None:
    """Bring a version-one database up to the schema Alembic is about to stamp.

    The old runner recorded version one before the Jira browser URL column was
    introduced.  That database cannot be stamped as current until the column is
    present, because Alembic quite correctly skips the initial revision after it
    has been stamped.
    """
    tables = set(inspect(connection).get_table_names())
    if "jira_config" in tables:
        columns = {column["name"] for column in inspect(connection).get_columns("jira_config")}
        if "browser_base_url" not in columns:
            if "base_url" not in columns:
                raise ValueError("Legacy jira_config is missing the required base_url column")
            connection.exec_driver_sql(
                "ALTER TABLE jira_config "
                "ADD COLUMN browser_base_url VARCHAR(300) NOT NULL DEFAULT ''"
            )

    _validate_current_schema(connection)


def _validate_current_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    missing = {
        table: sorted(columns - {column["name"] for column in inspector.get_columns(table)})
        for table, columns in _REQUIRED_COLUMNS.items()
        if table not in tables
        or columns - {column["name"] for column in inspector.get_columns(table)}
    }
    if missing:
        details = ", ".join(f"{table}: {', '.join(columns)}" for table, columns in missing.items())
        raise ValueError(f"Database is missing required current-schema elements: {details}")
