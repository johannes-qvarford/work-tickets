from __future__ import annotations

from collections.abc import Callable
from datetime import date

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .jira import JiraClient, JiraError
from .models import Category, JiraConfig, Ticket
from .web import (
    move_response,
    mutation_response,
    render_ticket,
    render_ticket_lists,
    ticket_move_response,
)


def create_ticket(
    request: Request,
    summary: str,
    description: str,
    planned_date: str,
    category_id: str,
    db: Session,
    jira_reference: str = "",
    jira_client_factory: Callable[[JiraConfig], JiraClient] = JiraClient,
) -> Response:
    summary_value = summary.strip()
    jira_reference_value = jira_reference.strip() or (
        summary_value if _is_jira_issue_reference_candidate(summary_value) else ""
    )
    if not summary_value and not jira_reference_value:
        return mutation_response(request, "error", "Ticket summary is required.", 422)

    planned_date_value = _parse_date(planned_date)
    if planned_date and planned_date_value is None:
        return mutation_response(request, "error", "Ticket planned date is invalid.", 422)

    try:
        category_id_value = int(category_id) if category_id else None
    except ValueError:
        return mutation_response(request, "error", "Ticket category is invalid.", 422)

    if category_id_value is not None and db.get(Category, category_id_value) is None:
        return mutation_response(request, "error", "Ticket category was not found.", 422)

    if jira_reference_value:
        from .jira_service import import_ticket_from_jira

        try:
            ticket = import_ticket_from_jira(
                jira_reference_value,
                planned_date_value,
                category_id_value,
                db,
                jira_client_factory=jira_client_factory,
            )
        except JiraError as exc:
            db.rollback()
            return mutation_response(request, "error", str(exc), 422)
        return mutation_response(
            request,
            "success",
            f"Ticket {ticket.summary} imported from Jira.",
            200,
            tickets_html=render_ticket_lists(request, db),
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
    return mutation_response(
        request,
        "success",
        f"Ticket {ticket.summary} added.",
        200,
        tickets_html=render_ticket_lists(request, db),
    )


def create_subtask(
    ticket_id: int,
    request: Request,
    summary: str,
    description: str,
    planned_date: str,
    db: Session,
) -> Response:
    parent = db.get(Ticket, ticket_id)
    if parent is None:
        return mutation_response(request, "error", "Parent ticket was not found.", 404)
    if parent.parent_id is not None:
        return mutation_response(
            request,
            "error",
            "Subtasks can only be added to top-level tickets.",
            400,
        )

    summary_value = summary.strip()
    if not summary_value:
        return mutation_response(request, "error", "Subtask summary is required.", 422)
    planned_date_value = _parse_date(planned_date)
    if planned_date and planned_date_value is None:
        return mutation_response(request, "error", "Subtask planned date is invalid.", 422)

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
    return mutation_response(
        request,
        "success",
        f"Subtask {subtask.summary} added.",
        200,
        ticket_html=render_ticket(request, parent, db),
        ticket_target=f"ticket-{parent.id}",
    )


def update_ticket(
    ticket_id: int,
    request: Request,
    summary: str,
    description: str,
    planned_date: str,
    db: Session,
    jira_client_factory: Callable[[JiraConfig], JiraClient] = JiraClient,
) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return mutation_response(request, "error", "Ticket was not found.", 404)
    summary_value = summary.strip()
    if not summary_value:
        return mutation_response(request, "error", "Ticket summary is required.", 422)
    planned_date_value = _parse_date(planned_date)
    if planned_date and planned_date_value is None:
        return mutation_response(request, "error", "Ticket planned date is invalid.", 422)

    ticket.summary = summary_value
    ticket.description = description
    ticket.planned_date = planned_date_value
    if ticket.jira_issue_key:
        from .jira_service import sync_ticket

        try:
            sync_ticket(ticket, db, jira_client_factory=jira_client_factory)
        except JiraError as exc:
            db.rollback()
            return mutation_response(request, "error", str(exc), 422)
    else:
        db.commit()
    return mutation_response(
        request,
        "success",
        f"Ticket {ticket.summary} updated.",
        200,
        tickets_html=render_ticket_lists(request, db),
    )


def update_subtask(
    subtask_id: int,
    request: Request,
    summary: str,
    description: str,
    planned_date: str,
    db: Session,
    jira_client_factory: Callable[[JiraConfig], JiraClient] = JiraClient,
) -> Response:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return mutation_response(request, "error", "Subtask was not found.", 404)
    if subtask.parent_id is None:
        return mutation_response(request, "error", "Top-level tickets cannot be edited here.", 400)

    summary_value = summary.strip()
    if not summary_value:
        return mutation_response(request, "error", "Subtask summary is required.", 422)
    planned_date_value = _parse_date(planned_date)
    if planned_date and planned_date_value is None:
        return mutation_response(request, "error", "Subtask planned date is invalid.", 422)

    parent_id = subtask.parent_id
    subtask.summary = summary_value
    subtask.description = description
    subtask.planned_date = planned_date_value
    if subtask.jira_issue_key:
        from .jira_service import sync_subtask

        try:
            sync_subtask(subtask, db, jira_client_factory=jira_client_factory)
        except JiraError as exc:
            db.rollback()
            return mutation_response(request, "error", str(exc), 422)
    else:
        db.commit()
    parent = db.get(Ticket, parent_id)
    if parent is None:
        return mutation_response(request, "error", "Parent ticket was not found.", 404)
    db.expire(parent, ["subtasks"])
    return mutation_response(
        request,
        "success",
        f"Subtask {subtask.summary} updated.",
        200,
        ticket_html=render_ticket(request, parent, db),
        ticket_target=f"ticket-{parent.id}",
    )


def delete_subtask(
    subtask_id: int,
    db: Session,
    jira_client_factory: Callable[[JiraConfig], JiraClient] = JiraClient,
) -> RedirectResponse:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return _redirect_error("Subtask was not found.")
    if subtask.parent_id is None:
        return _redirect_error("Top-level tickets cannot be deleted here.")
    summary = subtask.summary
    from .jira_service import delete_linked_jira_issue

    jira_error = delete_linked_jira_issue(subtask, db, jira_client_factory=jira_client_factory)
    db.delete(subtask)
    db.commit()
    if jira_error is not None:
        return _redirect_error(f"Subtask {summary} deleted locally, but {jira_error}")
    return _redirect_success(f"Subtask {summary} deleted.")


def delete_ticket(
    ticket_id: int,
    db: Session,
    jira_client_factory: Callable[[JiraConfig], JiraClient] = JiraClient,
) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _redirect_error("Ticket was not found.")
    if ticket.parent_id is not None:
        return _redirect_error("Subtasks cannot be deleted here.")
    summary = ticket.summary
    from .jira_service import delete_linked_jira_issue

    jira_error = delete_linked_jira_issue(ticket, db, jira_client_factory=jira_client_factory)
    db.delete(ticket)
    db.commit()
    if jira_error is not None:
        return _redirect_error(f"Ticket {summary} deleted locally, but {jira_error}")
    return _redirect_success(f"Ticket {summary} deleted.")


def complete_ticket(ticket_id: int, db: Session) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _redirect_error("Ticket was not found.")
    if ticket.parent_id is not None:
        return _redirect_error("Only top-level tickets can be completed here.")
    ticket.local_completed = not ticket.local_completed
    db.commit()
    state = "done" if ticket.local_completed else "active"
    return _redirect_success(f"Ticket {ticket.summary} marked {state}.")


def complete_subtask(subtask_id: int, db: Session) -> RedirectResponse:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return _redirect_error("Subtask was not found.")
    if subtask.parent_id is None:
        return _redirect_error("Top-level tickets cannot be completed here.")
    subtask.local_completed = not subtask.local_completed
    db.commit()
    state = "done" if subtask.local_completed else "active"
    return _redirect_success(f"Subtask {subtask.summary} marked {state}.")


def move_subtask(subtask_id: int, offset: int, request: Request, db: Session) -> Response:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return move_response(request, "error", "Subtask was not found.", 404)
    if subtask.parent_id is None:
        return move_response(request, "error", "Top-level tickets cannot be reordered here.", 400)
    siblings = _subtasks(db, subtask.parent_id)
    current_index = next(
        index for index, sibling in enumerate(siblings) if sibling.id == subtask.id
    )
    target_index = current_index + offset
    direction = "up" if offset < 0 else "down"
    if target_index < 0 or target_index >= len(siblings):
        _normalize_positions(siblings)
        db.commit()
        boundary = "top" if offset < 0 else "bottom"
        return move_response(
            request,
            "success",
            f"Subtask {subtask.summary} is already at the {boundary}.",
            200,
            subtask.parent_id,
            [sibling.id for sibling in siblings],
        )
    return move_subtask_to_index(
        subtask_id,
        target_index,
        request,
        db,
        message=f"Subtask {subtask.summary} moved {direction}.",
    )


def move_subtask_to_index(
    subtask_id: int,
    target_index: int,
    request: Request,
    db: Session,
    *,
    message: str | None = None,
) -> Response:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return move_response(request, "error", "Subtask was not found.", 404)
    if subtask.parent_id is None:
        return move_response(request, "error", "Top-level tickets cannot be reordered here.", 400)
    siblings = _subtasks(db, subtask.parent_id)
    if target_index < 0 or target_index >= len(siblings):
        return move_response(request, "error", "Subtask target position is invalid.", 422)
    current_index = next(
        index for index, sibling in enumerate(siblings) if sibling.id == subtask.id
    )
    siblings.pop(current_index)
    siblings.insert(target_index, subtask)
    _normalize_positions(siblings)
    db.commit()
    return move_response(
        request,
        "success",
        message or f"Subtask {subtask.summary} reordered.",
        200,
        subtask.parent_id,
        [sibling.id for sibling in siblings],
    )


def move_ticket(ticket_id: int, offset: int, request: Request, db: Session) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return ticket_move_response(request, "error", "Ticket was not found.", 404)
    if ticket.parent_id is not None:
        return ticket_move_response(
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
        return ticket_move_response(
            request,
            "success",
            f"Ticket {ticket.summary} is already at the {boundary}.",
            200,
            db=db,
            order=[candidate.id for candidate in tickets],
        )
    return move_ticket_to_index(
        ticket_id,
        target_index,
        request,
        db,
        message=f"Ticket {ticket.summary} moved {direction}.",
    )


def move_ticket_to_index(
    ticket_id: int,
    target_index: int,
    request: Request,
    db: Session,
    *,
    message: str | None = None,
) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return ticket_move_response(request, "error", "Ticket was not found.", 404)
    if ticket.parent_id is not None:
        return ticket_move_response(
            request, "error", "Subtasks cannot be reordered with top-level tickets.", 400
        )
    tickets = _top_level_tickets(db)
    if target_index < 0 or target_index >= len(tickets):
        return ticket_move_response(request, "error", "Ticket target position is invalid.", 422)
    current_index = next(
        index for index, candidate in enumerate(tickets) if candidate.id == ticket.id
    )
    tickets.pop(current_index)
    tickets.insert(target_index, ticket)
    _normalize_positions(tickets)
    db.commit()
    return ticket_move_response(
        request,
        "success",
        message or f"Ticket {ticket.summary} reordered.",
        200,
        db=db,
        order=[candidate.id for candidate in tickets],
    )


def _top_level_tickets(db: Session) -> list[Ticket]:
    return list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id.is_(None))
            .order_by(Ticket.position, Ticket.created_at, Ticket.id)
        )
    )


def _subtasks(db: Session, parent_id: int) -> list[Ticket]:
    return list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id == parent_id)
            .order_by(Ticket.position, Ticket.created_at, Ticket.id)
        )
    )


def _normalize_positions(siblings: list[Ticket]) -> None:
    for index, sibling in enumerate(siblings):
        sibling.position = index


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _is_jira_issue_reference_candidate(value: str) -> bool:
    from .jira_service import is_jira_issue_reference_candidate

    return is_jira_issue_reference_candidate(value)


def _redirect_error(message: str) -> RedirectResponse:
    from .web import redirect_with_message

    return redirect_with_message("error", message)


def _redirect_success(message: str) -> RedirectResponse:
    from .web import redirect_with_message

    return redirect_with_message("success", message)
