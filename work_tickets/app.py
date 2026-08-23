from __future__ import annotations

import re
from collections.abc import Generator
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, unquote, urlsplit

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .jira import JiraClient, JiraError, JiraIssue
from .models import Category, JiraConfig, SessionLocal, Ticket, init_db

app = FastAPI(title="Work Tickets")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
_JIRA_ISSUE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[A-Za-z0-9]+")


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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            **_ticket_list_context(db),
            "categories": categories,
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
        },
    )


def _ticket_list_context(db: Session) -> dict[str, object]:
    tickets = list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id.is_(None))
            .order_by(Ticket.position, Ticket.created_at)
        )
    )
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
    return {
        "tickets": tickets,
        "today_tickets": today_tickets,
        "jira_config": db.get(JiraConfig, 1),
        "today": today,
    }


def _render_fragment(request: Request, template_name: str, context: dict[str, object]) -> str:
    response = templates.TemplateResponse(request, template_name, context)
    return bytes(response.body).decode()


def _render_ticket(request: Request, ticket: Ticket, db: Session) -> str:
    return _render_fragment(
        request,
        "ticket.html",
        {"ticket": ticket, "jira_config": db.get(JiraConfig, 1)},
    )


def _render_ticket_lists(request: Request, db: Session) -> str:
    return _render_fragment(request, "ticket_lists.html", _ticket_list_context(db))


def parse_jira_issue_reference(reference: str, browser_base_url: str) -> str:
    """Extract a normalized Jira issue key from a key or configured browser URL."""
    value = reference.strip()
    if _JIRA_ISSUE_KEY.fullmatch(value):
        return value.upper()

    try:
        parsed = urlsplit(value)
        browser_base = urlsplit(browser_base_url.rstrip("/"))
    except ValueError as exc:
        raise JiraError(
            "Enter a Jira issue key or a Jira browser URL ending in /browse/KEY."
        ) from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.scheme.lower() != browser_base.scheme.lower()
        or parsed.netloc.lower() != browser_base.netloc.lower()
    ):
        raise JiraError("Enter a Jira issue key or a Jira browser URL ending in /browse/KEY.")

    base_path = browser_base.path.rstrip("/")
    browse_prefix = f"{base_path}/browse/" if base_path else "/browse/"
    path = unquote(parsed.path)
    if not path.lower().startswith(browse_prefix.lower()):
        raise JiraError("Enter a Jira issue key or a Jira browser URL ending in /browse/KEY.")
    key = path[len(browse_prefix) :].strip("/")
    if not _JIRA_ISSUE_KEY.fullmatch(key):
        raise JiraError("Enter a Jira issue key or a Jira browser URL ending in /browse/KEY.")
    return key.upper()


def _is_jira_issue_reference_candidate(value: str) -> bool:
    return bool(
        _JIRA_ISSUE_KEY.fullmatch(value) or value.lower().startswith(("http://", "https://"))
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
    summary_value = summary.strip()
    jira_reference_value = jira_reference.strip() or (
        summary_value if _is_jira_issue_reference_candidate(summary_value) else ""
    )
    if not summary_value and not jira_reference_value:
        return _mutation_response(request, "error", "Ticket summary is required.", 422)

    try:
        planned_date_value = date.fromisoformat(planned_date) if planned_date else None
    except ValueError:
        return _mutation_response(request, "error", "Ticket planned date is invalid.", 422)

    try:
        category_id_value = int(category_id) if category_id else None
    except ValueError:
        return _mutation_response(request, "error", "Ticket category is invalid.", 422)

    if category_id_value is not None and db.get(Category, category_id_value) is None:
        return _mutation_response(request, "error", "Ticket category was not found.", 422)

    if jira_reference_value:
        try:
            browser_base_url = _browser_base_url(db)
            issue_key = parse_jira_issue_reference(jira_reference_value, browser_base_url)
            if db.scalar(select(Ticket.id).where(Ticket.jira_issue_key == issue_key)) is not None:
                raise JiraError(f"A local ticket is already linked to Jira issue {issue_key}.")
            ticket = _import_ticket_from_jira(
                issue_key,
                planned_date_value,
                category_id_value,
                db,
            )
        except JiraError as exc:
            db.rollback()
            return _mutation_response(request, "error", str(exc), 422)
        return _mutation_response(
            request,
            "success",
            f"Ticket {ticket.summary} imported from Jira.",
            200,
            tickets_html=_render_ticket_lists(request, db),
        )

    count = db.scalar(select(func.count()).select_from(Ticket)) or 0
    ticket = Ticket(
        summary=summary_value,
        description=description,
        planned_date=planned_date_value,
        category_id=category_id_value,
        position=count,
    )
    db.add(ticket)
    db.commit()
    return _mutation_response(
        request,
        "success",
        f"Ticket {ticket.summary} added.",
        200,
        tickets_html=_render_ticket_lists(request, db),
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
    parent = db.get(Ticket, ticket_id)
    if parent is None:
        return _mutation_response(request, "error", "Parent ticket was not found.", 404)
    if parent.parent_id is not None:
        return _mutation_response(
            request,
            "error",
            "Subtasks can only be added to top-level tickets.",
            400,
        )

    summary_value = summary.strip()
    if not summary_value:
        return _mutation_response(request, "error", "Subtask summary is required.", 422)

    try:
        planned_date_value = date.fromisoformat(planned_date) if planned_date else None
    except ValueError:
        return _mutation_response(request, "error", "Subtask planned date is invalid.", 422)

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
    return _mutation_response(
        request,
        "success",
        f"Subtask {subtask.summary} added.",
        200,
        ticket_html=_render_ticket(request, parent, db),
        ticket_target=f"ticket-{parent.id}",
    )


@app.post("/subtasks/{subtask_id}/delete")
def delete_subtask(subtask_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return _redirect_with_message("error", "Subtask was not found.")
    if subtask.parent_id is None:
        return _redirect_with_message("error", "Top-level tickets cannot be deleted here.")

    summary = subtask.summary
    jira_error = _delete_linked_jira_issue(subtask, db)
    db.delete(subtask)
    db.commit()
    if jira_error is not None:
        return _redirect_with_message(
            "error", f"Subtask {summary} deleted locally, but {jira_error}"
        )
    return _redirect_with_message("success", f"Subtask {summary} deleted.")


@app.post("/tickets/{ticket_id}/delete")
def delete_ticket(ticket_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _redirect_with_message("error", "Ticket was not found.")
    if ticket.parent_id is not None:
        return _redirect_with_message("error", "Subtasks cannot be deleted here.")

    summary = ticket.summary
    jira_error = _delete_linked_jira_issue(ticket, db)
    db.delete(ticket)
    db.commit()
    if jira_error is not None:
        return _redirect_with_message(
            "error", f"Ticket {summary} deleted locally, but {jira_error}"
        )
    return _redirect_with_message("success", f"Ticket {summary} deleted.")


@app.post("/subtasks/{subtask_id}/move-up")
def move_subtask_up(subtask_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    return _move_subtask(subtask_id, -1, request, db)


@app.post("/subtasks/{subtask_id}/move-down")
def move_subtask_down(subtask_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    return _move_subtask(subtask_id, 1, request, db)


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
        return _move_response(request, "error", "Subtask target position is invalid.", 422)
    return _move_subtask_to_index(subtask_id, target_index_value, request, db)


def _move_subtask(subtask_id: int, offset: int, request: Request, db: Session) -> Response:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return _move_response(request, "error", "Subtask was not found.", 404)
    if subtask.parent_id is None:
        return _move_response(request, "error", "Top-level tickets cannot be reordered here.", 400)

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

    target_index = current_index + offset
    direction = "up" if offset < 0 else "down"
    if target_index < 0 or target_index >= len(siblings):
        _normalize_positions(siblings)
        db.commit()
        boundary = "top" if offset < 0 else "bottom"
        return _move_response(
            request,
            "success",
            f"Subtask {subtask.summary} is already at the {boundary}.",
            200,
            subtask.parent_id,
            [sibling.id for sibling in siblings],
        )

    response = _move_subtask_to_index(
        subtask_id,
        target_index,
        request,
        db,
        message=f"Subtask {subtask.summary} moved {direction}.",
    )
    return response


def _move_subtask_to_index(
    subtask_id: int,
    target_index: int,
    request: Request,
    db: Session,
    *,
    message: str | None = None,
) -> Response:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return _move_response(request, "error", "Subtask was not found.", 404)
    if subtask.parent_id is None:
        return _move_response(request, "error", "Top-level tickets cannot be reordered here.", 400)

    siblings = list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id == subtask.parent_id)
            .order_by(Ticket.position, Ticket.created_at, Ticket.id)
        )
    )
    if target_index < 0 or target_index >= len(siblings):
        return _move_response(request, "error", "Subtask target position is invalid.", 422)

    current_index = next(
        index for index, sibling in enumerate(siblings) if sibling.id == subtask.id
    )
    siblings.pop(current_index)
    siblings.insert(target_index, subtask)
    _normalize_positions(siblings)
    db.commit()
    ordered_ids = [sibling.id for sibling in siblings]
    return _move_response(
        request,
        "success",
        message or f"Subtask {subtask.summary} reordered.",
        200,
        subtask.parent_id,
        ordered_ids,
    )


@app.post("/tickets/{ticket_id}/move-up")
def move_ticket_up(ticket_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    return _move_ticket(ticket_id, -1, request, db)


@app.post("/tickets/{ticket_id}/move-down")
def move_ticket_down(ticket_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    return _move_ticket(ticket_id, 1, request, db)


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
        return _ticket_move_response(request, "error", "Ticket target position is invalid.", 422)
    return _move_ticket_to_index(ticket_id, target_index_value, request, db)


def _top_level_tickets(db: Session) -> list[Ticket]:
    return list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id.is_(None))
            .order_by(Ticket.position, Ticket.created_at, Ticket.id)
        )
    )


def _move_ticket(ticket_id: int, offset: int, request: Request, db: Session) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _ticket_move_response(request, "error", "Ticket was not found.", 404)
    if ticket.parent_id is not None:
        return _ticket_move_response(
            request, "error", "Subtasks cannot be reordered with top-level tickets.", 400
        )

    tickets = _top_level_tickets(db)
    current_index = next(
        index for index, candidate in enumerate(tickets) if candidate.id == ticket.id
    )
    target_index = current_index + offset
    direction = "up" if offset < 0 else "down"
    if target_index < 0 or target_index >= len(tickets):
        _normalize_positions(tickets)
        db.commit()
        boundary = "top" if offset < 0 else "bottom"
        return _ticket_move_response(
            request,
            "success",
            f"Ticket {ticket.summary} is already at the {boundary}.",
            200,
            db=db,
            order=[candidate.id for candidate in tickets],
        )

    return _move_ticket_to_index(
        ticket_id,
        target_index,
        request,
        db,
        message=f"Ticket {ticket.summary} moved {direction}.",
    )


def _move_ticket_to_index(
    ticket_id: int,
    target_index: int,
    request: Request,
    db: Session,
    *,
    message: str | None = None,
) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _ticket_move_response(request, "error", "Ticket was not found.", 404)
    if ticket.parent_id is not None:
        return _ticket_move_response(
            request, "error", "Subtasks cannot be reordered with top-level tickets.", 400
        )

    tickets = _top_level_tickets(db)
    if target_index < 0 or target_index >= len(tickets):
        return _ticket_move_response(request, "error", "Ticket target position is invalid.", 422)

    current_index = next(
        index for index, candidate in enumerate(tickets) if candidate.id == ticket.id
    )
    tickets.pop(current_index)
    tickets.insert(target_index, ticket)
    _normalize_positions(tickets)
    db.commit()
    return _ticket_move_response(
        request,
        "success",
        message or f"Ticket {ticket.summary} reordered.",
        200,
        db=db,
        order=[candidate.id for candidate in tickets],
    )


def _normalize_positions(siblings: list[Ticket]) -> None:
    # Normalize so old or manually edited data cannot leave duplicate or sparse
    # positions. The explicit id tie-breaker in the query makes the order deterministic.
    for index, sibling in enumerate(siblings):
        sibling.position = index


@app.post("/tickets/{ticket_id}")
def update_ticket(
    ticket_id: int,
    request: Request,
    summary: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    planned_date: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _mutation_response(request, "error", "Ticket was not found.", 404)

    summary_value = summary.strip()
    if not summary_value:
        return _mutation_response(request, "error", "Ticket summary is required.", 422)
    try:
        planned_date_value = date.fromisoformat(planned_date) if planned_date else None
    except ValueError:
        return _mutation_response(request, "error", "Ticket planned date is invalid.", 422)

    ticket.summary = summary_value
    ticket.description = description
    ticket.planned_date = planned_date_value
    if ticket.jira_issue_key:
        try:
            _sync_ticket(ticket, db)
        except JiraError as exc:
            db.rollback()
            return _mutation_response(request, "error", str(exc), 422)
    else:
        db.commit()
    return _mutation_response(
        request,
        "success",
        f"Ticket {ticket.summary} updated.",
        200,
        tickets_html=_render_ticket_lists(request, db),
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
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return _mutation_response(request, "error", "Subtask was not found.", 404)
    if subtask.parent_id is None:
        return _mutation_response(request, "error", "Top-level tickets cannot be edited here.", 400)

    summary_value = summary.strip()
    if not summary_value:
        return _mutation_response(request, "error", "Subtask summary is required.", 422)

    try:
        planned_date_value = date.fromisoformat(planned_date) if planned_date else None
    except ValueError:
        return _mutation_response(request, "error", "Subtask planned date is invalid.", 422)

    parent_id = subtask.parent_id
    subtask.summary = summary_value
    subtask.description = description
    subtask.planned_date = planned_date_value
    if subtask.jira_issue_key:
        try:
            _sync_subtask(subtask, db)
        except JiraError as exc:
            db.rollback()
            return _mutation_response(request, "error", str(exc), 422)
    else:
        db.commit()
    parent = db.get(Ticket, parent_id)
    if parent is None:
        return _mutation_response(request, "error", "Parent ticket was not found.", 404)
    db.expire(parent, ["subtasks"])
    return _mutation_response(
        request,
        "success",
        f"Subtask {subtask.summary} updated.",
        200,
        ticket_html=_render_ticket(request, parent, db),
        ticket_target=f"ticket-{parent.id}",
    )


@app.post("/tickets/{ticket_id}/sync")
def sync_ticket(ticket_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _redirect_with_message("error", "Ticket was not found.")
    if ticket.parent_id is not None:
        return _redirect_with_message(
            "error",
            "Only top-level tickets can sync to Jira; sync the parent to include all subtasks.",
        )
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
    if ticket.parent_id is not None:
        return _redirect_with_message(
            "error",
            "Only top-level tickets can sync from Jira; sync the parent to include all subtasks.",
        )
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
        return _redirect_with_message("error", "All Jira connection fields are required.")
    for url_key, label in (("base_url", "Jira API URL"), ("browser_base_url", "Jira browser URL")):
        if not values[url_key].startswith(("https://", "http://")):
            return _redirect_with_message("error", f"{label} must start with http:// or https://.")

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
    except JiraError as exc:
        db.rollback()
        return _redirect_with_message("error", f"Jira setup failed: {exc}")
    message = (
        "Jira connection validated and saved."
        if validate_connection
        else "Jira configuration saved."
    )
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


def _delete_linked_jira_issue(ticket: Ticket, db: Session) -> str | None:
    """Try to remove a linked Jira issue and return a user-facing failure, if any."""
    if not ticket.jira_issue_key:
        return None

    issue_key = ticket.jira_issue_key
    config = db.get(JiraConfig, 1)
    if config is None:
        return f"linked Jira issue {issue_key} could not be deleted: Jira is not configured."

    jira = None
    try:
        jira = JiraClient(config)
        jira.delete_issue(issue_key)
    except JiraError as exc:
        return f"linked Jira issue {issue_key} could not be deleted: {exc}"
    finally:
        if jira is not None:
            jira.close()
    return None


def _sync_ticket(ticket: Ticket, db: Session) -> None:
    if ticket.parent_id is not None:
        raise JiraError(
            "Only top-level tickets can sync to Jira; sync the parent to include all subtasks."
        )
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before syncing.")
    jira = JiraClient(config)
    try:
        if ticket.jira_issue_key:
            issue = jira.update_issue(ticket.jira_issue_key, ticket.summary, ticket.description)
        else:
            issue = jira.create_issue(ticket.summary, ticket.description)
        synced_at = datetime.utcnow()
        _save_jira_issue(ticket, issue, synced_at)
        # Commit each remote success so a later failure does not lose already
        # established Jira links or statuses in the local database. If a later
        # child fails, the parent and any earlier children remain linked and a
        # retry will reconcile the remaining local children.
        db.commit()
        for subtask in list(ticket.subtasks):
            try:
                if subtask.jira_issue_key:
                    subtask_issue = jira.update_issue(
                        subtask.jira_issue_key, subtask.summary, subtask.description
                    )
                else:
                    subtask_issue = jira.create_subtask(
                        issue.key, subtask.summary, subtask.description
                    )
                _save_jira_issue(subtask, subtask_issue, synced_at)
                db.commit()
            except JiraError as exc:
                raise JiraError(
                    f"Parent {issue.key} synced, but subtask '{subtask.summary}' failed: {exc} "
                    "Retry the parent sync to continue."
                ) from exc
    finally:
        jira.close()


def _browser_base_url(db: Session) -> str:
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before importing.")
    return config.browser_base_url or config.base_url


def _import_ticket_from_jira(
    issue_key: str,
    planned_date: date | None,
    category_id: int | None,
    db: Session,
) -> Ticket:
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before importing.")

    jira = JiraClient(config)
    try:
        synced = jira.get_issue_with_subtasks(issue_key)
    finally:
        jira.close()

    if not synced.issue.summary:
        raise JiraError(f"Jira returned issue {issue_key} without a summary.")
    remote_subtask_keys: set[str] = set()
    for subtask in synced.subtasks:
        if subtask.key == synced.issue.key:
            raise JiraError(f"Jira returned the parent issue {issue_key} as its own subtask.")
        if subtask.key in remote_subtask_keys:
            raise JiraError(f"Jira returned duplicate subtask key {subtask.key}.")
        if not subtask.summary:
            raise JiraError(f"Jira returned subtask {subtask.key} without a summary.")
        remote_subtask_keys.add(subtask.key)
    imported_issue_keys = {synced.issue.key, *remote_subtask_keys}
    existing_issue_key = db.scalar(
        select(Ticket.jira_issue_key).where(Ticket.jira_issue_key.in_(imported_issue_keys))
    )
    if existing_issue_key is not None:
        raise JiraError(f"A local ticket is already linked to Jira issue {existing_issue_key}.")

    synced_at = datetime.utcnow()
    count = db.scalar(select(func.count()).select_from(Ticket)) or 0
    ticket = Ticket(
        summary=synced.issue.summary,
        description=synced.issue.description or "",
        planned_date=planned_date,
        category_id=category_id,
        position=count,
        jira_issue_key=synced.issue.key,
        jira_status_name=synced.issue.status_name,
        synced_at=synced_at,
    )
    db.add(ticket)
    for position, subtask in enumerate(synced.subtasks):
        db.add(
            Ticket(
                parent=ticket,
                summary=subtask.summary or "",
                description=subtask.description or "",
                position=position,
                jira_issue_key=subtask.key,
                jira_status_name=subtask.status_name,
                synced_at=synced_at,
            )
        )
    db.commit()
    return ticket


def _sync_subtask(subtask: Ticket, db: Session) -> None:
    if subtask.parent_id is None:
        raise JiraError("Only subtasks can use the subtask edit sync path.")
    if not subtask.jira_issue_key:
        raise JiraError("Subtask has not been synced to Jira yet.")
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before syncing.")

    jira = JiraClient(config)
    try:
        issue = jira.update_issue(subtask.jira_issue_key, subtask.summary, subtask.description)
        _save_jira_issue(subtask, issue, datetime.utcnow())
        db.commit()
    finally:
        jira.close()


def _sync_ticket_from_jira(ticket: Ticket, db: Session) -> None:
    if ticket.parent_id is not None:
        raise JiraError(
            "Only top-level tickets can sync from Jira; sync the parent to include all subtasks."
        )
    if not ticket.jira_issue_key:
        raise JiraError("Ticket has not been synced to Jira yet.")
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before syncing.")

    jira = JiraClient(config)
    try:
        synced = jira.get_issue_with_subtasks(ticket.jira_issue_key)
    finally:
        jira.close()

    if not synced.issue.summary:
        raise JiraError("Jira returned an issue without a summary.")
    remote_subtasks_by_key: dict[str, JiraIssue] = {}
    for issue in synced.subtasks:
        if issue.key == synced.issue.key:
            raise JiraError(f"Jira returned the parent issue {issue.key} as its own subtask.")
        if issue.key in remote_subtasks_by_key:
            raise JiraError(f"Jira returned duplicate subtask key {issue.key}.")
        if not issue.summary:
            raise JiraError(f"Jira returned subtask {issue.key} without a summary.")
        remote_subtasks_by_key[issue.key] = issue

    # Only Jira-owned fields are changed here. Category, date, completion, and position
    # are deliberately local workflow fields and must survive an inbound sync.
    synced_at = datetime.utcnow()
    _save_jira_issue(ticket, synced.issue, synced_at, clear_missing_fields=True)

    local_subtasks = list(ticket.subtasks)
    local_subtasks_by_key: dict[str, Ticket] = {}
    for subtask in local_subtasks:
        if not subtask.jira_issue_key:
            continue
        if subtask.jira_issue_key in local_subtasks_by_key:
            raise JiraError(f"Local subtasks have duplicate Jira key {subtask.jira_issue_key}.")
        local_subtasks_by_key[subtask.jira_issue_key] = subtask

    # A linked local child that Jira no longer reports is stale and is removed.
    # Local-only children without a Jira key are intentionally retained: they
    # have not been claimed by Jira and will be created by the next outbound sync.
    for subtask in local_subtasks:
        if subtask.jira_issue_key and subtask.jira_issue_key not in remote_subtasks_by_key:
            db.delete(subtask)

    next_position = max((subtask.position for subtask in local_subtasks), default=-1) + 1
    for issue in remote_subtasks_by_key.values():
        matching_subtask = local_subtasks_by_key.get(issue.key)
        if matching_subtask is None:
            # Position is local priority, so remote Jira ordering must not reorder
            # existing local subtasks. Newly discovered subtasks are appended.
            subtask = Ticket(parent=ticket, position=next_position)
            next_position += 1
            db.add(subtask)
        else:
            subtask = matching_subtask
        _save_jira_issue(subtask, issue, synced_at, clear_missing_fields=True)
    db.flush()
    db.commit()


def _save_jira_issue(
    ticket: Ticket,
    issue: JiraIssue,
    synced_at: datetime,
    *,
    clear_missing_fields: bool = False,
) -> None:
    ticket.jira_issue_key = issue.key
    if issue.summary is not None:
        ticket.summary = issue.summary
    elif clear_missing_fields:
        raise JiraError(f"Jira returned issue {issue.key} without a summary.")
    if issue.description is not None or clear_missing_fields:
        ticket.description = issue.description or ""
    ticket.jira_status_name = issue.status_name
    ticket.synced_at = synced_at


def _redirect_with_message(kind: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"/?{kind}={quote(message)}", status_code=303)


def _mutation_response(
    request: Request,
    kind: str,
    message: str,
    status_code: int,
    *,
    tickets_html: str | None = None,
    ticket_html: str | None = None,
    ticket_target: str | None = None,
) -> Response:
    if "application/json" in request.headers.get("accept", "").lower():
        payload: dict[str, object] = {"ok": kind == "success", "message": message}
        if tickets_html is not None:
            payload.update({"target": "ticket-lists", "html": tickets_html})
        elif ticket_html is not None:
            payload.update({"target": ticket_target, "html": ticket_html})
        return JSONResponse(payload, status_code=status_code)
    return _redirect_with_message(kind, message)


def _move_response(
    request: Request,
    kind: str,
    message: str,
    status_code: int,
    parent_id: int | None = None,
    order: list[int] | None = None,
) -> Response:
    if "application/json" in request.headers.get("accept", "").lower():
        payload: dict[str, object] = {"ok": kind == "success", "message": message}
        if parent_id is not None and order is not None:
            payload["parent_id"] = parent_id
            payload["order"] = order
        return JSONResponse(payload, status_code=status_code)
    return _redirect_with_message(kind, message)


def _ticket_move_response(
    request: Request,
    kind: str,
    message: str,
    status_code: int,
    *,
    db: Session | None = None,
    order: list[int] | None = None,
) -> Response:
    if "application/json" in request.headers.get("accept", "").lower():
        payload: dict[str, object] = {"ok": kind == "success", "message": message}
        if order is not None:
            payload["order"] = order
        if db is not None and kind == "success":
            payload.update({"target": "ticket-lists", "html": _render_ticket_lists(request, db)})
        return JSONResponse(payload, status_code=status_code)
    return _redirect_with_message(kind, message)
