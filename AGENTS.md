# AGENTS.md

## Project overview

`work-tickets` is a local-only, single-user personal ticket workflow application.
It uses FastAPI, SQLite, SQLAlchemy, Jinja2, and uv.

## Development commands

Install dependencies:

```sh
uv sync
```

Run the server:

```sh
uv run uvicorn work_tickets.app:app --reload
```

Run all checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy work_tickets
uv run pytest
```

## Conventions

- Use Python 3.12+ and keep code strictly typed for mypy.
- Use Ruff for formatting and linting.
- Use pytest for tests.
- Use SQLAlchemy models for persistence; do not commit `work-tickets.db`.
- Keep local-only workflow fields separate from future Jira-owned fields.
- OpenCode integration is intentionally deferred.
- Run the complete local checks before committing changes.

## Product rules

- Tickets are local source of truth until first Jira sync.
- After first sync, Jira owns Jira-mapped fields such as summary, description, issue type, and status.
- Category, planned date, and local priority remain local after synchronization.
- Jira setup will be configured through a first-time wizard and can be revalidated.
