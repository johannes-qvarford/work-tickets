from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, inspect, text

from work_tickets.db_migrations import apply_migrations


def test_initial_migration_creates_current_schema_on_a_fresh_database(tmp_path) -> None:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")

    apply_migrations(database_engine)

    assert set(inspect(database_engine).get_table_names()) == {
        "categories",
        "jira_config",
        "schema_migrations",
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
    with database_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one() == 1
        assert connection.execute(text("SELECT version FROM schema_migrations")).scalar_one() == 1


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
        assert connection.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one() == 1
