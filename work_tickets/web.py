from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, JiraConfig, Ticket


def ticket_list_data(db: Session) -> dict[str, object]:
    """Return the complete client state used by the Vue application."""
    tickets = list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id.is_(None))
            .order_by(Ticket.local_completed, Ticket.position, Ticket.created_at, Ticket.id)
        )
    )
    categories = list(db.scalars(select(Category).order_by(Category.name)))
    config = db.get(JiraConfig, 1)

    def serialize_ticket(ticket: Ticket, *, include_notes: bool = False) -> dict[str, object]:
        serialized: dict[str, object] = {
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
        if include_notes:
            serialized["notes"] = ticket.notes or ""
        return serialized

    return {
        "tickets": [serialize_ticket(ticket, include_notes=True) for ticket in tickets],
        "categories": [{"id": category.id, "name": category.name} for category in categories],
        "jira_config": (
            {
                "base_url": config.base_url,
                "browser_base_url": config.browser_base_url,
                "email": config.email,
                "project_key": config.project_key,
                "issue_type": config.issue_type,
                "completed_statuses": config.completed_statuses,
            }
            if config is not None
            else None
        ),
    }


def mutation_response(
    request: Request,
    kind: str,
    message: str,
    status_code: int,
    *,
    created_id: int | None = None,
) -> Response:
    del request
    payload: dict[str, object] = {"ok": kind == "success", "message": message}
    if created_id is not None:
        payload["created_id"] = created_id
    return JSONResponse(payload, status_code=status_code)


def move_response(
    request: Request,
    kind: str,
    message: str,
    status_code: int,
    parent_id: int | None = None,
    order: list[int] | None = None,
) -> Response:
    del request
    payload: dict[str, object] = {"ok": kind == "success", "message": message}
    if parent_id is not None and order is not None:
        payload["parent_id"] = parent_id
        payload["order"] = order
    return JSONResponse(payload, status_code=status_code)


def ticket_move_response(
    request: Request,
    kind: str,
    message: str,
    status_code: int,
    *,
    order: list[int] | None = None,
) -> Response:
    del request
    payload: dict[str, object] = {"ok": kind == "success", "message": message}
    if order is not None:
        payload["order"] = order
    return JSONResponse(payload, status_code=status_code)
