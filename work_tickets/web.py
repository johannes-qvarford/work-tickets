from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, JiraConfig, Ticket

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def jira_issue_url(config: JiraConfig | None, issue_key: str | None) -> str | None:
    if config is None or not issue_key or not config.browser_base_url.strip():
        return None
    browser_base_url = config.browser_base_url.strip().rstrip("/")
    return f"{browser_base_url}/browse/{quote(issue_key, safe='')}"


def ticket_list_context(db: Session) -> dict[str, object]:
    tickets = list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id.is_(None))
            .order_by(Ticket.local_completed, Ticket.position, Ticket.created_at, Ticket.id)
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
    config = db.get(JiraConfig, 1)
    return {
        "tickets": tickets,
        "today_tickets": today_tickets,
        "jira_config": config,
        "jira_issue_url": lambda issue_key: jira_issue_url(config, issue_key),
        "today": today,
    }


def ticket_list_data(db: Session) -> dict[str, object]:
    """Return the complete client state used by the Vue application."""
    context = ticket_list_context(db)
    tickets = context["tickets"]
    assert isinstance(tickets, list)
    categories = list(db.scalars(select(Category).order_by(Category.name)))

    def serialize_ticket(ticket: Ticket) -> dict[str, object]:
        return {
            "id": ticket.id,
            "parent_id": ticket.parent_id,
            "summary": ticket.summary,
            "description": ticket.description,
            "planned_date": ticket.planned_date.isoformat() if ticket.planned_date else None,
            "position": ticket.position,
            "local_completed": ticket.local_completed,
            "jira_issue_key": ticket.jira_issue_key,
            "jira_status_name": ticket.jira_status_name,
            "category_id": ticket.category_id,
            "category_name": ticket.category.name if ticket.category else None,
            "subtasks": [serialize_ticket(subtask) for subtask in ticket.subtasks],
        }

    return {
        "tickets": [serialize_ticket(ticket) for ticket in tickets],
        "categories": [{"id": category.id, "name": category.name} for category in categories],
        "jira_config": (
            {
                "base_url": context["jira_config"].base_url,
                "browser_base_url": context["jira_config"].browser_base_url,
                "email": context["jira_config"].email,
                "project_key": context["jira_config"].project_key,
                "issue_type": context["jira_config"].issue_type,
                "completed_statuses": context["jira_config"].completed_statuses,
            }
            if isinstance(context["jira_config"], JiraConfig)
            else None
        ),
    }


def render_fragment(request: Request, template_name: str, context: Mapping[str, object]) -> str:
    response = templates.TemplateResponse(request, template_name, dict(context))
    return bytes(response.body).decode()


def render_ticket(request: Request, ticket: Ticket, db: Session) -> str:
    config = db.get(JiraConfig, 1)
    return render_fragment(
        request,
        "ticket.html",
        {
            "ticket": ticket,
            "jira_config": config,
            "jira_issue_url": lambda issue_key: jira_issue_url(config, issue_key),
        },
    )


def render_ticket_lists(request: Request, db: Session) -> str:
    return render_fragment(request, "ticket_lists.html", ticket_list_context(db))


def redirect_with_message(kind: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"/?{kind}={quote(message)}", status_code=303)


def mutation_response(
    request: Request,
    kind: str,
    message: str,
    status_code: int,
    *,
    tickets_html: str | None = None,
    ticket_html: str | None = None,
    ticket_target: str | None = None,
    created_id: int | None = None,
) -> Response:
    if (
        request.url.path.startswith("/api/")
        or "application/json" in request.headers.get("accept", "").lower()
    ):
        payload: dict[str, object] = {"ok": kind == "success", "message": message}
        if tickets_html is not None:
            payload.update({"target": "ticket-lists", "html": tickets_html})
        elif ticket_html is not None:
            payload.update({"target": ticket_target, "html": ticket_html})
        if created_id is not None:
            payload["created_id"] = created_id
        return JSONResponse(payload, status_code=status_code)
    return redirect_with_message(kind, message)


def move_response(
    request: Request,
    kind: str,
    message: str,
    status_code: int,
    parent_id: int | None = None,
    order: list[int] | None = None,
) -> Response:
    if (
        request.url.path.startswith("/api/")
        or "application/json" in request.headers.get("accept", "").lower()
    ):
        payload: dict[str, object] = {"ok": kind == "success", "message": message}
        if parent_id is not None and order is not None:
            payload["parent_id"] = parent_id
            payload["order"] = order
        return JSONResponse(payload, status_code=status_code)
    return redirect_with_message(kind, message)


def ticket_move_response(
    request: Request,
    kind: str,
    message: str,
    status_code: int,
    *,
    db: Session | None = None,
    order: list[int] | None = None,
) -> Response:
    if (
        request.url.path.startswith("/api/")
        or "application/json" in request.headers.get("accept", "").lower()
    ):
        payload: dict[str, object] = {"ok": kind == "success", "message": message}
        if order is not None:
            payload["order"] = order
        if db is not None and kind == "success":
            payload.update({"target": "ticket-lists", "html": render_ticket_lists(request, db)})
        return JSONResponse(payload, status_code=status_code)
    return redirect_with_message(kind, message)
