# Work Tickets

A local-first personal ticket workflow for prioritizing work and syncing selected tickets to Jira Cloud.

## Product rules

- Local tickets are the source of truth until their first Jira sync.
- After sync, Jira owns Jira-mapped fields such as summary, description, issue type, and status.
- Local-only fields remain local: category, planned date, and priority ordering.
- Editing a synced ticket is supported; local workflow fields remain editable, while Jira-owned summary and description fields are read-only after sync.
- Jira setup is configured once and can be revalidated when needed.
- OpenCode integration is intentionally deferred.

## Development

```sh
uv sync
uv run uvicorn work_tickets.app:app --reload
```

Then open <http://127.0.0.1:8000>. The root route serves the Vue/PrimeVue single-page
application. It provides separate hash-routed pages for tickets, ticket creation,
categories, and application settings.

The frontend source is in `frontend/`. Install Node dependencies and build the packaged
static assets after frontend changes:

```sh
npm install --prefix frontend
npm run check --prefix frontend
npm run build --prefix frontend
```

The build writes to `work_tickets/static/`, which is included in the Python wheel. The
SPA talks to the FastAPI JSON endpoints under `/api`; Jira services remain available for
ticket synchronization and imports.

Checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy work_tickets
uv run pytest
npm run check --prefix frontend
npm run build --prefix frontend
```

Install the repository's verification hook once per checkout. The command can be run
from the repository root or any of its subdirectories:

```sh
git -C "$(git rev-parse --show-toplevel)" config core.hooksPath .githooks
```

The hook runs the complete check suite above before each commit and stops at the first
failing command. Verify the installation with:

```sh
git config --get core.hooksPath
test -x "$(git rev-parse --show-toplevel)/.githooks/pre-commit"
```
