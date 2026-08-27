from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import jira_service, tickets, web
from .jira import JiraClient
from .models import Category, JiraConfig, SessionLocal, Ticket, init_db

app = FastAPI(title="Work Tickets")
parse_jira_issue_reference = jira_service.parse_jira_issue_reference


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
    categories = list(db.scalars(select(Category).order_by(Category.name)))
    return web.templates.TemplateResponse(
        request,
        "index.html",
        {
            **web.ticket_list_context(db),
            "categories": categories,
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
        },
    )


@app.post("/tickets")
def create_ticket(
    request: Request,
    summary: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    planned_date: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    jira_reference: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> Response:
    return tickets.create_ticket(
        request,
        summary,
        description,
        planned_date,
        category_id,
        db,
        jira_reference,
        jira_client_factory=JiraClient,
    )


@app.post("/tickets/{ticket_id}/subtasks")
def create_subtask(
    ticket_id: int,
    request: Request,
    summary: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    planned_date: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> Response:
    return tickets.create_subtask(ticket_id, request, summary, description, planned_date, db)


@app.post("/subtasks/{subtask_id}/delete")
def delete_subtask(subtask_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    return tickets.delete_subtask(subtask_id, db, jira_client_factory=JiraClient)


@app.post("/tickets/{ticket_id}/delete")
def delete_ticket(ticket_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    return tickets.delete_ticket(ticket_id, db, jira_client_factory=JiraClient)


@app.post("/subtasks/{subtask_id}/move-up")
def move_subtask_up(subtask_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    return tickets.move_subtask(subtask_id, -1, request, db)


@app.post("/subtasks/{subtask_id}/move-down")
def move_subtask_down(subtask_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    return tickets.move_subtask(subtask_id, 1, request, db)


@app.post("/subtasks/{subtask_id}/move-to")
def move_subtask_to(
    subtask_id: int,
    request: Request,
    target_index: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> Response:
    try:
        target_index_value = int(target_index)
    except ValueError:
        return web.move_response(request, "error", "Subtask target position is invalid.", 422)
    return tickets.move_subtask_to_index(subtask_id, target_index_value, request, db)


@app.post("/tickets/{ticket_id}/move-up")
def move_ticket_up(ticket_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    return tickets.move_ticket(ticket_id, -1, request, db)


@app.post("/tickets/{ticket_id}/move-down")
def move_ticket_down(ticket_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    return tickets.move_ticket(ticket_id, 1, request, db)


@app.post("/tickets/{ticket_id}/move-to")
def move_ticket_to(
    ticket_id: int,
    request: Request,
    target_index: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> Response:
    try:
        target_index_value = int(target_index)
    except ValueError:
        return web.ticket_move_response(request, "error", "Ticket target position is invalid.", 422)
    return tickets.move_ticket_to_index(ticket_id, target_index_value, request, db)


@app.post("/tickets/{ticket_id}")
def update_ticket(
    ticket_id: int,
    request: Request,
    summary: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    planned_date: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> Response:
    return tickets.update_ticket(
        ticket_id,
        request,
        summary,
        description,
        planned_date,
        db,
        jira_client_factory=JiraClient,
    )


@app.post("/subtasks/{subtask_id}")
def update_subtask(
    subtask_id: int,
    request: Request,
    summary: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    planned_date: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> Response:
    return tickets.update_subtask(
        subtask_id,
        request,
        summary,
        description,
        planned_date,
        db,
        jira_client_factory=JiraClient,
    )


@app.post("/tickets/{ticket_id}/sync")
def sync_ticket(ticket_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return web.redirect_with_message("error", "Ticket was not found.")
    if ticket.parent_id is not None:
        return web.redirect_with_message(
            "error",
            "Only top-level tickets can sync to Jira; sync the parent to include all subtasks.",
        )
    try:
        jira_service.sync_ticket(ticket, db, jira_client_factory=JiraClient)
    except jira_service.JiraError as exc:
        db.rollback()
        return web.redirect_with_message("error", str(exc))
    return web.redirect_with_message("success", f"{ticket.summary} synced to Jira.")


@app.post("/tickets/{ticket_id}/sync-from-jira")
def sync_ticket_from_jira(ticket_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return web.redirect_with_message("error", "Ticket was not found.")
    if ticket.parent_id is not None:
        return web.redirect_with_message(
            "error",
            "Only top-level tickets can sync from Jira; sync the parent to include all subtasks.",
        )
    try:
        jira_service.sync_ticket_from_jira(ticket, db, jira_client_factory=JiraClient)
    except jira_service.JiraError as exc:
        db.rollback()
        return web.redirect_with_message("error", str(exc))
    return web.redirect_with_message("success", f"{ticket.summary} synced from Jira.")


@app.post("/jira/config")
def save_jira_config(
    base_url: Annotated[str, Form()],
    email: Annotated[str, Form()],
    browser_base_url: Annotated[str, Form()] = "",
    api_token: Annotated[str, Form()] = "",
    project_key: Annotated[str, Form()] = "",
    issue_type: Annotated[str, Form()] = "Task",
    completed_statuses: Annotated[str, Form()] = "Done",
    validate_connection: Annotated[str, Form(alias="validate")] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    existing = db.get(JiraConfig, 1)
    token = api_token.strip() or (existing.api_token if existing is not None else "")
    normalized_base_url = base_url.strip().rstrip("/")
    values = {
        "base_url": normalized_base_url,
        "browser_base_url": browser_base_url.strip().rstrip("/") or normalized_base_url,
        "email": email.strip(),
        "api_token": token,
        "project_key": project_key.strip().upper(),
        "issue_type": issue_type.strip(),
        "completed_statuses": completed_statuses.strip() or "Done",
    }
    required_keys = ("base_url", "email", "api_token", "project_key", "issue_type")
    if not all(values[key] for key in required_keys):
        return web.redirect_with_message("error", "All Jira connection fields are required.")
    for url_key, label in (("base_url", "Jira API URL"), ("browser_base_url", "Jira browser URL")):
        if not values[url_key].startswith(("https://", "http://")):
            return web.redirect_with_message(
                "error", f"{label} must start with http:// or https://."
            )

    candidate = JiraConfig(id=1, **values)
    try:
        if validate_connection:
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
    except jira_service.JiraError as exc:
        db.rollback()
        return web.redirect_with_message("error", f"Jira setup failed: {exc}")
    message = (
        "Jira connection validated and saved."
        if validate_connection
        else "Jira configuration saved."
    )
    return web.redirect_with_message("success", message)


@app.post("/tickets/{ticket_id}/complete")
def complete_ticket(ticket_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    return tickets.complete_ticket(ticket_id, db)


@app.post("/subtasks/{subtask_id}/complete")
def complete_subtask(subtask_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    return tickets.complete_subtask(subtask_id, db)


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
        return web.redirect_with_message("error", "Category was not found.")

    # Categories are local labels. Removing one must not remove any tickets that
    # use it; those tickets become uncategorized instead.
    db.execute(update(Ticket).where(Ticket.category_id == category_id).values(category_id=None))
    db.delete(category)
    db.commit()
    return web.redirect_with_message("success", f"Category {category.name} deleted.")
