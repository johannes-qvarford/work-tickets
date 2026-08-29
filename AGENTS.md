# AGENTS.md

## Project overview

`work-tickets` is a local-only, single-user personal ticket workflow application.
The backend uses FastAPI, SQLite, SQLAlchemy, and uv. The main interface is a Vue 3,
PrimeVue, and Vite single-page application in `frontend/`. The previous Jinja2 interface
is retained at `/legacy` for compatibility.

## Development commands

Install dependencies:

```sh
uv sync
```

Run the server:

```sh
uv run uvicorn work_tickets.app:app --reload
```

After frontend changes, install the Node dependencies, type-check the frontend, and rebuild
the packaged assets:

```sh
npm install --prefix frontend
npm run check --prefix frontend
npm run build --prefix frontend
```

Run all checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run djlint --check work_tickets/templates/index.html
uv run mypy work_tickets
uv run pytest
npm run check --prefix frontend
npm run build --prefix frontend
```

## Conventions

- Use Python 3.12+ and keep code strictly typed for mypy.
- Use Ruff for formatting and linting.
- Use pytest for tests.
- Use SQLAlchemy models for persistence; do not commit `work-tickets.db`.
- Keep Vue and TypeScript frontend code under `frontend/` and rebuild `work_tickets/static/`
  after frontend changes; the generated static assets are packaged with the Python wheel.
- Use the FastAPI JSON endpoints under `/api` for SPA data and mutations. Keep the existing
  form endpoints and `/legacy` Jinja templates available for compatibility.
- Keep local-only workflow fields separate from future Jira-owned fields.
- OpenCode integration is intentionally deferred.
- Run the complete local checks before committing changes.

## Product rules

- Tickets are local source of truth until first Jira sync.
- After first sync, Jira owns Jira-mapped fields such as summary, description, issue type, and status.
- Category, planned date, and local priority remain local after synchronization.
- Jira setup will be configured through a first-time wizard and can be revalidated.
