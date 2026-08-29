from __future__ import annotations

from datetime import datetime

from sqlalchemy import Connection, create_engine, inspect, text

from work_tickets.db_migrations import apply_migrations


def test_initial_migration_creates_current_schema_on_a_fresh_database(tmp_path) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")

    apply_migrations(database_engine)

    assert set(inspect(database_engine).get_table_names()) == {
        "alembic_version",
        "category_components",
        "categories",
        "components",
        "jira_config",
        "tickets",
    }
    assert {column["name"] for column in inspect(database_engine).get_columns("jira_config")} == {
        "id",
        "base_url",
        "browser_base_url",
        "email",
        "api_token",
        "project_key",
        "issue_type",
        "completed_statuses",
        "updated_at",
    }
    assert {column["name"] for column in inspect(database_engine).get_columns("tickets")} >= {
        "notes",
        "component",
    }
    with database_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar_one() == 1
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "003_add_local_components"
        )


def test_reapplying_migrations_preserves_an_initialized_database(tmp_path) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'initialized.db'}")
    apply_migrations(database_engine)
    with database_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jira_config "
                "(id, base_url, browser_base_url, email, api_token, project_key, issue_type, "
                "completed_statuses, updated_at) VALUES "
                "(1, 'https://api.example.test', 'https://jira.example.test', "
                "'person@example.test', 'secret', 'WORK', 'Task', 'Done', :updated_at)"
            ),
            {"updated_at": datetime(2026, 8, 23)},
        )

    apply_migrations(database_engine)

    with database_engine.connect() as connection:
        config = connection.execute(
            text("SELECT base_url, browser_base_url, project_key FROM jira_config WHERE id = 1")
        ).one()
        assert tuple(config) == (
            "https://api.example.test",
            "https://jira.example.test",
            "WORK",
        )
        assert connection.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar_one() == 1


def test_existing_homegrown_tracking_is_converted_without_losing_data(tmp_path) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-tracking.db'}")
    with database_engine.begin() as connection:
        _create_pre_migration_schema(connection, tracked=True)
        connection.exec_driver_sql(
            "INSERT INTO categories (id, name) VALUES (1, 'Legacy category')"
        )
        connection.exec_driver_sql(
            "INSERT INTO tickets "
            "(id, summary, description, position, local_completed, created_at, updated_at, "
            "category_id) VALUES "
            "(10, 'Legacy parent', 'Parent description', 0, 0, '2026-08-23 00:00:00', "
            "'2026-08-23 00:00:00', 1)"
        )
        connection.exec_driver_sql(
            "INSERT INTO tickets "
            "(id, parent_id, summary, description, position, local_completed, created_at, "
            "updated_at, category_id) VALUES "
            "(11, 10, 'Legacy subtask', 'Subtask description', 0, 0, '2026-08-23 00:00:00', "
            "'2026-08-23 00:00:00', 1)"
        )

    apply_migrations(database_engine)

    with database_engine.connect() as connection:
        assert "schema_migrations" not in inspect(database_engine).get_table_names()
        assert connection.execute(
            text("SELECT base_url, browser_base_url, project_key FROM jira_config WHERE id = 1")
        ).one() == (
            "https://api.example.test",
            "",
            "WORK",
        )
        assert connection.execute(
            text("SELECT name FROM categories WHERE id = 1")
        ).scalar_one() == ("Legacy category")
        assert connection.execute(
            text("SELECT parent_id, summary, category_id FROM tickets ORDER BY id")
        ).all() == [(None, "Legacy parent", 1), (10, "Legacy subtask", 1)]
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "003_add_local_components"
        )


def test_untracked_pre_migration_schema_is_upgraded_without_losing_data(tmp_path) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'untracked-pre-migration.db'}")
    with database_engine.begin() as connection:
        _create_pre_migration_schema(connection)
        connection.exec_driver_sql(
            "INSERT INTO categories (id, name) VALUES (1, 'Pre-migration category')"
        )
        connection.exec_driver_sql(
            "INSERT INTO tickets "
            "(id, summary, description, position, local_completed, created_at, updated_at, "
            "category_id) VALUES "
            "(20, 'Pre-migration parent', 'Parent description', 0, 0, '2026-08-23 00:00:00', "
            "'2026-08-23 00:00:00', 1)"
        )
        connection.exec_driver_sql(
            "INSERT INTO tickets "
            "(id, parent_id, summary, description, position, local_completed, created_at, "
            "updated_at, category_id) VALUES "
            "(21, 20, 'Pre-migration subtask', 'Subtask description', 0, 0, "
            "'2026-08-23 00:00:00', '2026-08-23 00:00:00', 1)"
        )

    apply_migrations(database_engine)

    with database_engine.connect() as connection:
        assert "schema_migrations" not in inspect(database_engine).get_table_names()
        assert connection.execute(
            text("SELECT base_url, browser_base_url, project_key FROM jira_config WHERE id = 1")
        ).one() == (
            "https://api.example.test",
            "",
            "WORK",
        )
        assert connection.execute(
            text("SELECT parent_id, summary, category_id FROM tickets ORDER BY id")
        ).all() == [(None, "Pre-migration parent", 1), (20, "Pre-migration subtask", 1)]
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "003_add_local_components"
        )


def _create_pre_migration_schema(connection: Connection, *, tracked: bool = False) -> None:
    if tracked:
        connection.exec_driver_sql(
            "CREATE TABLE schema_migrations ("
            "version INTEGER NOT NULL PRIMARY KEY, "
            "name VARCHAR(255) NOT NULL, "
            "applied_at DATETIME NOT NULL"
            ")"
        )
        connection.exec_driver_sql(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, 'initial', '2026-08-23 00:00:00')"
        )
    connection.exec_driver_sql(
        "CREATE TABLE categories ("
        "id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(80) NOT NULL UNIQUE"
        ")"
    )
    connection.exec_driver_sql(
        "CREATE TABLE jira_config ("
        "id INTEGER NOT NULL PRIMARY KEY, base_url VARCHAR(300) NOT NULL, "
        "email VARCHAR(320) NOT NULL, api_token VARCHAR(300) NOT NULL, "
        "project_key VARCHAR(40) NOT NULL, issue_type VARCHAR(80) NOT NULL, "
        "completed_statuses VARCHAR(500) NOT NULL, updated_at DATETIME NOT NULL"
        ")"
    )
    connection.exec_driver_sql(
        "INSERT INTO jira_config "
        "(id, base_url, email, api_token, project_key, issue_type, completed_statuses, "
        "updated_at) VALUES "
        "(1, 'https://api.example.test', 'person@example.test', 'secret', 'WORK', 'Task', "
        "'Done', '2026-08-23 00:00:00')"
    )
    connection.exec_driver_sql(
        "CREATE TABLE tickets ("
        "id INTEGER NOT NULL PRIMARY KEY, parent_id INTEGER, summary VARCHAR(240) NOT NULL, "
        "description TEXT NOT NULL, planned_date DATE, position INTEGER NOT NULL, "
        "local_completed BOOLEAN NOT NULL, jira_issue_key VARCHAR(40), "
        "jira_status_name VARCHAR(80), synced_at DATETIME, created_at DATETIME NOT NULL, "
        "updated_at DATETIME NOT NULL, category_id INTEGER"
        ")"
    )
