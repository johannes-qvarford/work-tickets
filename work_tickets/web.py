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

from .models import JiraConfig, Ticket

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def ticket_list_context(db: Session) -> dict[str, object]:
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


def render_fragment(request: Request, template_name: str, context: Mapping[str, object]) -> str:
    response = templates.TemplateResponse(request, template_name, dict(context))
    return bytes(response.body).decode()


def render_ticket(request: Request, ticket: Ticket, db: Session) -> str:
    return render_fragment(
        request,
        "ticket.html",
        {"ticket": ticket, "jira_config": db.get(JiraConfig, 1)},
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
) -> Response:
    if "application/json" in request.headers.get("accept", "").lower():
        payload: dict[str, object] = {"ok": kind == "success", "message": message}
        if tickets_html is not None:
            payload.update({"target": "ticket-lists", "html": tickets_html})
        elif ticket_html is not None:
            payload.update({"target": ticket_target, "html": ticket_html})
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
    if "application/json" in request.headers.get("accept", "").lower():
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
    if "application/json" in request.headers.get("accept", "").lower():
        payload: dict[str, object] = {"ok": kind == "success", "message": message}
        if order is not None:
            payload["order"] = order
        if db is not None and kind == "success":
            payload.update({"target": "ticket-lists", "html": render_ticket_lists(request, db)})
        return JSONResponse(payload, status_code=status_code)
    return redirect_with_message(kind, message)
