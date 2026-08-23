from __future__ import annotations

from collections.abc import Generator
from datetime import date
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .jira import JiraClient, JiraError
from .models import Category, JiraConfig, SessionLocal, Ticket, init_db

app = FastAPI(title="Work Tickets")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.on_event("startup")
def startup() -> None:
    init_db()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Annotated[Session, Depends(get_db)]) -> HTMLResponse:
    tickets = list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id.is_(None))
            .order_by(Ticket.position, Ticket.created_at)
        )
    )
    categories = list(db.scalars(select(Category).order_by(Category.name)))
    today = date.today()
    today_tickets = [
        ticket
        for ticket in tickets
        if (
            not ticket.local_completed
            and ticket.planned_date is not None
            and ticket.planned_date <= today
        )
    ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "tickets": tickets,
            "today_tickets": today_tickets,
            "categories": categories,
            "jira_config": db.get(JiraConfig, 1),
            "today": today,
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
        },
    )


@app.post("/tickets")
def create_ticket(
    summary: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    planned_date: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    count = db.scalar(select(func.count()).select_from(Ticket)) or 0
    ticket = Ticket(summary=summary.strip(), description=description, position=count)
    ticket.planned_date = date.fromisoformat(planned_date) if planned_date else None
    ticket.category_id = int(category_id) if category_id else None
    db.add(ticket)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/tickets/{ticket_id}/subtasks")
def create_subtask(
    ticket_id: int,
    summary: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    planned_date: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    parent = db.get(Ticket, ticket_id)
    if parent is None:
        return _redirect_with_message("error", "Parent ticket was not found.")
    if parent.parent_id is not None:
        return _redirect_with_message("error", "Subtasks can only be added to top-level tickets.")

    summary_value = summary.strip()
    if not summary_value:
        return _redirect_with_message("error", "Subtask summary is required.")

    try:
        planned_date_value = date.fromisoformat(planned_date) if planned_date else None
    except ValueError:
        return _redirect_with_message("error", "Subtask planned date is invalid.")

    max_position = db.scalar(select(func.max(Ticket.position)).where(Ticket.parent_id == parent.id))
    subtask = Ticket(
        parent_id=parent.id,
        summary=summary_value,
        description=description,
        planned_date=planned_date_value,
        position=(max_position if max_position is not None else -1) + 1,
    )
    db.add(subtask)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/subtasks/{subtask_id}/delete")
def delete_subtask(subtask_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return _redirect_with_message("error", "Subtask was not found.")
    if subtask.parent_id is None:
        return _redirect_with_message("error", "Top-level tickets cannot be deleted here.")

    summary = subtask.summary
    db.delete(subtask)
    db.commit()
    return _redirect_with_message("success", f"Subtask {summary} deleted.")


@app.post("/subtasks/{subtask_id}/move-up")
def move_subtask_up(subtask_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    return _move_subtask(subtask_id, -1, db)


@app.post("/subtasks/{subtask_id}/move-down")
def move_subtask_down(subtask_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    return _move_subtask(subtask_id, 1, db)


def _move_subtask(subtask_id: int, offset: int, db: Session) -> RedirectResponse:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return _redirect_with_message("error", "Subtask was not found.")
    if subtask.parent_id is None:
        return _redirect_with_message("error", "Top-level tickets cannot be reordered here.")

    siblings = list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id == subtask.parent_id)
            .order_by(Ticket.position, Ticket.created_at, Ticket.id)
        )
    )
    current_index = next(
        index for index, sibling in enumerate(siblings) if sibling.id == subtask.id
    )

    # Normalize first so old or manually edited data cannot leave duplicate or sparse
    # positions after a move. The explicit id tie-breaker makes the order deterministic.
    for index, sibling in enumerate(siblings):
        sibling.position = index

    target_index = current_index + offset
    if target_index < 0 or target_index >= len(siblings):
        db.commit()
        boundary = "top" if offset < 0 else "bottom"
        return _redirect_with_message(
            "success", f"Subtask {subtask.summary} is already at the {boundary}."
        )

    siblings[current_index].position, siblings[target_index].position = (
        siblings[target_index].position,
        siblings[current_index].position,
    )
    db.commit()
    direction = "up" if offset < 0 else "down"
    return _redirect_with_message("success", f"Subtask {subtask.summary} moved {direction}.")


@app.post("/tickets/{ticket_id}")
def update_ticket(
    ticket_id: int,
    summary: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    planned_date: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is not None and summary.strip():
        ticket.summary = summary.strip()
        ticket.description = description
        ticket.planned_date = date.fromisoformat(planned_date) if planned_date else None
        ticket.category_id = int(category_id) if category_id else None
        if ticket.jira_issue_key:
            try:
                _sync_ticket(ticket, db)
            except JiraError as exc:
                db.rollback()
                return _redirect_with_message("error", str(exc))
        else:
            db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/tickets/{ticket_id}/sync")
def sync_ticket(ticket_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _redirect_with_message("error", "Ticket was not found.")
    try:
        _sync_ticket(ticket, db)
    except JiraError as exc:
        db.rollback()
        return _redirect_with_message("error", str(exc))
    return _redirect_with_message("success", f"{ticket.summary} synced to Jira.")


@app.post("/tickets/{ticket_id}/sync-from-jira")
def sync_ticket_from_jira(ticket_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _redirect_with_message("error", "Ticket was not found.")
    try:
        _sync_ticket_from_jira(ticket, db)
    except JiraError as exc:
        db.rollback()
        return _redirect_with_message("error", str(exc))
    return _redirect_with_message("success", f"{ticket.summary} synced from Jira.")


@app.post("/jira/config")
def save_jira_config(
    base_url: Annotated[str, Form()],
    email: Annotated[str, Form()],
    api_token: Annotated[str, Form()] = "",
    project_key: Annotated[str, Form()] = "",
    issue_type: Annotated[str, Form()] = "Task",
    completed_statuses: Annotated[str, Form()] = "Done",
    validate: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    existing = db.get(JiraConfig, 1)
    token = api_token.strip() or (existing.api_token if existing is not None else "")
    values = {
        "base_url": base_url.strip().rstrip("/"),
        "email": email.strip(),
        "api_token": token,
        "project_key": project_key.strip().upper(),
        "issue_type": issue_type.strip(),
        "completed_statuses": completed_statuses.strip() or "Done",
    }
    required_keys = ("base_url", "email", "api_token", "project_key", "issue_type")
    if not all(values[key] for key in required_keys):
        return _redirect_with_message("error", "All Jira connection fields are required.")
    if not values["base_url"].startswith(("https://", "http://")):
        return _redirect_with_message("error", "Jira URL must start with http:// or https://.")

    candidate = JiraConfig(id=1, **values)
    try:
        if validate:
            jira = JiraClient(candidate)
            try:
                jira.validate()
            finally:
                jira.close()
        if existing is None:
            db.add(candidate)
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        db.commit()
    except JiraError as exc:
        db.rollback()
        return _redirect_with_message("error", f"Jira setup failed: {exc}")
    message = "Jira connection validated and saved." if validate else "Jira configuration saved."
    return _redirect_with_message("success", message)


@app.post("/tickets/{ticket_id}/complete")
def complete_ticket(ticket_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _redirect_with_message("error", "Ticket was not found.")
    if ticket.parent_id is not None:
        return _redirect_with_message("error", "Only top-level tickets can be completed here.")

    ticket.local_completed = not ticket.local_completed
    db.commit()
    state = "done" if ticket.local_completed else "active"
    return _redirect_with_message("success", f"Ticket {ticket.summary} marked {state}.")


@app.post("/subtasks/{subtask_id}/complete")
def complete_subtask(subtask_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return _redirect_with_message("error", "Subtask was not found.")
    if subtask.parent_id is None:
        return _redirect_with_message("error", "Top-level tickets cannot be completed here.")

    subtask.local_completed = not subtask.local_completed
    db.commit()
    state = "done" if subtask.local_completed else "active"
    return _redirect_with_message("success", f"Subtask {subtask.summary} marked {state}.")


@app.post("/categories")
def create_category(
    name: Annotated[str, Form()], db: Session = Depends(get_db)
) -> RedirectResponse:
    if name.strip() and db.scalar(select(Category).where(Category.name == name.strip())) is None:
        db.add(Category(name=name.strip()))
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/categories/{category_id}/delete")
def delete_category(category_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    category = db.get(Category, category_id)
    if category is None:
        return _redirect_with_message("error", "Category was not found.")

    # Categories are local labels. Removing one must not remove any tickets that
    # use it; those tickets become uncategorized instead.
    db.execute(update(Ticket).where(Ticket.category_id == category_id).values(category_id=None))
    db.delete(category)
    db.commit()
    return _redirect_with_message("success", f"Category {category.name} deleted.")


def _sync_ticket(ticket: Ticket, db: Session) -> None:
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before syncing.")
    jira = JiraClient(config)
    try:
        if ticket.jira_issue_key:
            issue = jira.update_issue(ticket.jira_issue_key, ticket.summary, ticket.description)
        else:
            issue = jira.create_issue(ticket.summary, ticket.description)
    finally:
        jira.close()
    ticket.jira_issue_key = issue.key
    ticket.jira_status_name = issue.status_name
    from datetime import datetime

    ticket.synced_at = datetime.utcnow()
    db.commit()


def _sync_ticket_from_jira(ticket: Ticket, db: Session) -> None:
    if not ticket.jira_issue_key:
        raise JiraError("Ticket has not been synced to Jira yet.")
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before syncing.")

    jira = JiraClient(config)
    try:
        issue = jira.get_issue(ticket.jira_issue_key)
    finally:
        jira.close()

    if not issue.summary:
        raise JiraError("Jira returned an issue without a summary.")

    # Only Jira-owned fields are changed here. Category, date, completion, and position
    # are deliberately local workflow fields and must survive an inbound sync.
    ticket.summary = issue.summary
    ticket.description = issue.description or ""
    ticket.jira_status_name = issue.status_name
    from datetime import datetime

    ticket.synced_at = datetime.utcnow()
    db.commit()


def _redirect_with_message(kind: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"/?{kind}={quote(message)}", status_code=303)
