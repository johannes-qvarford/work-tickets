from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

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
        "opencode_sessions",
        "tickets",
    }
    assert {column["name"] for column in inspect(database_engine).get_columns("jira_config")} == {
        "id",
        "base_url",
        "browser_base_url",
        "implement_prompt_template",
        "local_projects_directory",
        "gitlab_base_url",
        "email",
        "api_token",
        "gitlab_token",
        "project_key",
        "issue_type",
        "completed_statuses",
        "in_review_status",
        "ready_to_merge_status",
        "ready_to_deploy_status",
        "updated_at",
    }
    ticket_columns = inspect(database_engine).get_columns("tickets")
    assert {column["name"] for column in ticket_columns} >= {
        "notes",
        "component",
    }
    notes_column = next(column for column in ticket_columns if column["name"] == "notes")
    assert notes_column["nullable"] is False
    assert notes_column["default"] == "''"
    with database_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar_one() == 1
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "009_add_implement_prompt_template"
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
            text(
                "SELECT base_url, browser_base_url, project_key, in_review_status, "
                "ready_to_merge_status, ready_to_deploy_status, gitlab_base_url, gitlab_token "
                "FROM jira_config WHERE id = 1"
            )
        ).one()
        assert tuple(config) == (
            "https://api.example.test",
            "https://jira.example.test",
            "WORK",
            "In Review",
            "Ready to Merge",
            "Ready to Deploy",
            "",
            "",
        )
        assert connection.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar_one() == 1


def test_existing_opencode_schema_gets_default_implement_prompt_template(tmp_path) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'opencode-008.db'}")
    apply_migrations(database_engine)
    with database_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jira_config "
                "(id, base_url, browser_base_url, email, api_token, project_key, issue_type, "
                "completed_statuses, updated_at) VALUES "
                "(1, 'https://api.example.test', '', 'person@example.test', 'secret', 'WORK', "
                "'Task', 'Done', '2026-08-23 00:00:00')"
            )
        )
        connection.exec_driver_sql("ALTER TABLE jira_config DROP COLUMN implement_prompt_template")
        connection.execute(
            text("UPDATE alembic_version SET version_num = '008_add_opencode_sessions'")
        )

    apply_migrations(database_engine)

    with database_engine.connect() as connection:
        assert connection.scalar(text("SELECT implement_prompt_template FROM jira_config")) == (
            "Please implement the work described at <TICKET_URL> and run the relevant tests."
        )
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "009_add_implement_prompt_template"
        )


def test_nullable_notes_from_old_002_are_backfilled_and_rejected(tmp_path) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'old-002.db'}")
    with database_engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "CREATE TABLE categories ("
            "id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(80) NOT NULL UNIQUE"
            ")"
        )
        connection.exec_driver_sql(
            "CREATE TABLE jira_config ("
            "id INTEGER NOT NULL PRIMARY KEY, base_url VARCHAR(300) NOT NULL, "
            "browser_base_url VARCHAR(300) NOT NULL DEFAULT '', email VARCHAR(320) NOT NULL, "
            "api_token VARCHAR(300) NOT NULL, project_key VARCHAR(40) NOT NULL, "
            "issue_type VARCHAR(80) NOT NULL, completed_statuses VARCHAR(500) NOT NULL, "
            "updated_at DATETIME NOT NULL"
            ")"
        )
        connection.exec_driver_sql(
            "CREATE TABLE tickets ("
            "id INTEGER NOT NULL PRIMARY KEY, parent_id INTEGER, summary VARCHAR(240) NOT NULL, "
            "description TEXT NOT NULL, planned_date DATE, position INTEGER NOT NULL, "
            "local_completed BOOLEAN NOT NULL, jira_issue_key VARCHAR(40), "
            "jira_status_name VARCHAR(80), synced_at DATETIME, created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL, category_id INTEGER, notes TEXT, "
            "FOREIGN KEY(category_id) REFERENCES categories(id), "
            "FOREIGN KEY(parent_id) REFERENCES tickets(id)"
            ")"
        )
        connection.exec_driver_sql("CREATE INDEX ix_tickets_summary ON tickets (summary)")
        connection.exec_driver_sql(
            "INSERT INTO categories (id, name) VALUES (1, 'Migration category')"
        )
        connection.exec_driver_sql(
            "INSERT INTO tickets "
            "(id, summary, description, position, local_completed, created_at, updated_at, "
            "category_id, notes) VALUES "
            "(10, 'Old parent', 'Parent description', 0, 0, '2026-08-23 00:00:00', "
            "'2026-08-23 00:00:00', 1, NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO tickets "
            "(id, parent_id, summary, description, position, local_completed, created_at, "
            "updated_at, category_id, notes) VALUES "
            "(11, 10, 'Old subtask', 'Subtask description', 0, 0, '2026-08-23 00:00:00', "
            "'2026-08-23 00:00:00', 1, 'Keep this note')"
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES ('002_add_ticket_notes')"
        )

    apply_migrations(database_engine)

    ticket_columns = inspect(database_engine).get_columns("tickets")
    notes_column = next(column for column in ticket_columns if column["name"] == "notes")
    assert notes_column["nullable"] is False
    assert notes_column["default"] == "''"
    assert {index["name"] for index in inspect(database_engine).get_indexes("tickets")} >= {
        "ix_tickets_summary"
    }
    assert {
        (foreign_key["constrained_columns"][0], foreign_key["referred_table"])
        for foreign_key in inspect(database_engine).get_foreign_keys("tickets")
    } == {("category_id", "categories"), ("parent_id", "tickets")}
    with database_engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert connection.execute(text("SELECT id, notes FROM tickets ORDER BY id")).all() == [
            (10, ""),
            (11, "Keep this note"),
        ]
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "009_add_implement_prompt_template"
        )

    with pytest.raises(IntegrityError):
        with database_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tickets "
                    "(id, summary, description, position, local_completed, created_at, "
                    "updated_at, notes) VALUES "
                    "(12, 'Invalid ticket', '', 0, 0, '2026-08-23 00:00:00', "
                    "'2026-08-23 00:00:00', NULL)"
                )
            )


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
            "009_add_implement_prompt_template"
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
            "009_add_implement_prompt_template"
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
