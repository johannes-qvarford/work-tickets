from __future__ import annotations

from collections.abc import Callable
from datetime import date

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .jira import JiraClient, JiraError
from .local_projects import is_safe_local_component_name
from .models import Category, Component, JiraConfig, Ticket
from .web import (
    move_response,
    mutation_response,
    ticket_move_response,
)


def create_ticket(
    request: Request,
    summary: str,
    description: str,
    notes: str,
    planned_date: str,
    category_id: str,
    component: str | None,
    db: Session,
    *,
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
    component_value = _validate_component(component, db)
    if component and component_value is None:
        return mutation_response(request, "error", "Ticket component was not found.", 422)

    if jira_reference_value:
        from .jira_service import import_ticket_from_jira

        try:
            ticket = import_ticket_from_jira(
                jira_reference_value,
                planned_date_value,
                category_id_value,
                component_value,
                notes,
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
            created_id=ticket.id,
        )

    ticket = Ticket(
        summary=summary_value,
        description=description,
        notes=notes,
        planned_date=planned_date_value,
        category_id=category_id_value,
        component=component_value,
        position=0,
    )
    _append_unfinished(_top_level_tickets(db), ticket)
    db.add(ticket)
    db.commit()
    return mutation_response(
        request,
        "success",
        f"Ticket {ticket.summary} added.",
        200,
        created_id=ticket.id,
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
    if parent.local_completed:
        return mutation_response(
            request,
            "error",
            "Done tickets cannot have subtasks added.",
            400,
        )

    summary_value = summary.strip()
    if not summary_value:
        return mutation_response(request, "error", "Subtask summary is required.", 422)
    planned_date_value = _parse_date(planned_date)
    if planned_date and planned_date_value is None:
        return mutation_response(request, "error", "Subtask planned date is invalid.", 422)

    subtask = Ticket(
        parent_id=parent.id,
        summary=summary_value,
        description=description,
        planned_date=planned_date_value,
        position=0,
    )
    _append_unfinished(_subtasks(db, parent.id), subtask)
    db.add(subtask)
    db.commit()
    return mutation_response(
        request,
        "success",
        f"Subtask {subtask.summary} added.",
        200,
    )


def update_ticket(
    ticket_id: int,
    request: Request,
    summary: str,
    description: str,
    notes: str,
    planned_date: str,
    category_id: int | None,
    component: str | None,
    db: Session,
    *,
    component_provided: bool = True,
    jira_client_factory: Callable[[JiraConfig], JiraClient] = JiraClient,
) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return mutation_response(request, "error", "Ticket was not found.", 404)
    if ticket.parent_id is not None:
        return mutation_response(request, "error", "Subtasks cannot be edited here.", 400)
    if ticket.local_completed:
        return mutation_response(
            request,
            "error",
            "Done tickets can only be marked active.",
            400,
        )
    summary_value = summary.strip()
    if not summary_value:
        return mutation_response(request, "error", "Ticket summary is required.", 422)
    planned_date_value = _parse_date(planned_date)
    if planned_date and planned_date_value is None:
        return mutation_response(request, "error", "Ticket planned date is invalid.", 422)
    if category_id is not None and db.get(Category, category_id) is None:
        return mutation_response(request, "error", "Ticket category was not found.", 422)
    component_value = _validate_component(component, db)
    if component_provided and component and component_value is None:
        if component.strip() != (ticket.component or ""):
            return mutation_response(request, "error", "Ticket component was not found.", 422)

    ticket.summary = summary_value
    ticket.description = description
    ticket.notes = notes
    ticket.planned_date = planned_date_value
    ticket.category_id = category_id
    if component_provided:
        if component_value is not None or not component:
            ticket.component = component_value
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
    if subtask.local_completed:
        return mutation_response(
            request,
            "error",
            "Done subtasks can only be marked active.",
            400,
        )

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
    )


def delete_subtask(
    subtask_id: int,
    db: Session,
    jira_client_factory: Callable[[JiraConfig], JiraClient] = JiraClient,
) -> Response:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return _error_response("Subtask was not found.", 404)
    if subtask.parent_id is None:
        return _error_response("Top-level tickets cannot be deleted here.", 400)
    if subtask.local_completed:
        return _error_response("Done subtasks can only be marked active.", 400)
    summary = subtask.summary
    from .jira_service import delete_linked_jira_issue

    jira_error = delete_linked_jira_issue(subtask, db, jira_client_factory=jira_client_factory)
    db.delete(subtask)
    db.commit()
    if jira_error is not None:
        return _error_response(f"Subtask {summary} deleted locally, but {jira_error}", 200)
    return Response(status_code=200)


def delete_ticket(
    ticket_id: int,
    db: Session,
    jira_client_factory: Callable[[JiraConfig], JiraClient] = JiraClient,
) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _error_response("Ticket was not found.", 404)
    if ticket.parent_id is not None:
        return _error_response("Subtasks cannot be deleted here.", 400)
    if ticket.local_completed:
        return _error_response("Done tickets can only be marked active.", 400)
    if any(subtask.local_completed for subtask in ticket.subtasks):
        return _error_response(
            "Tickets with done subtasks can only be deleted after they are active.", 400
        )
    summary = ticket.summary
    from .jira_service import delete_linked_jira_issue

    jira_error = delete_linked_jira_issue(ticket, db, jira_client_factory=jira_client_factory)
    db.delete(ticket)
    db.commit()
    if jira_error is not None:
        return _error_response(f"Ticket {summary} deleted locally, but {jira_error}", 200)
    return Response(status_code=200)


def complete_ticket(ticket_id: int, db: Session) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return _error_response("Ticket was not found.", 404)
    if ticket.parent_id is not None:
        return _error_response("Only top-level tickets can be completed here.", 400)
    ticket.local_completed = not ticket.local_completed
    _prioritize_completion(ticket, db)
    db.commit()
    return Response(status_code=200)


def complete_subtask(subtask_id: int, db: Session) -> Response:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return _error_response("Subtask was not found.", 404)
    if subtask.parent_id is None:
        return _error_response("Top-level tickets cannot be completed here.", 400)
    subtask.local_completed = not subtask.local_completed
    _prioritize_completion(subtask, db)
    db.commit()
    return Response(status_code=200)


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
    if subtask.local_completed:
        return move_response(request, "error", "Done subtasks cannot be reordered.", 400)
    siblings = _subtasks(db, subtask.parent_id)
    active_siblings = [sibling for sibling in siblings if not sibling.local_completed]
    if target_index < 0 or target_index >= len(active_siblings):
        return move_response(request, "error", "Subtask target position is invalid.", 422)
    current_index = next(
        index for index, sibling in enumerate(active_siblings) if sibling.id == subtask.id
    )
    active_siblings.pop(current_index)
    active_siblings.insert(target_index, subtask)
    ordered_siblings = active_siblings + [
        sibling for sibling in siblings if sibling.local_completed
    ]
    _normalize_positions(active_siblings)
    db.commit()
    return move_response(
        request,
        "success",
        message or f"Subtask {subtask.summary} reordered.",
        200,
        subtask.parent_id,
        [sibling.id for sibling in ordered_siblings],
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
    if ticket.local_completed:
        return ticket_move_response(request, "error", "Done tickets cannot be reordered.", 400)
    tickets = _top_level_tickets(db)
    active_tickets = [candidate for candidate in tickets if not candidate.local_completed]
    if target_index < 0 or target_index >= len(active_tickets):
        return ticket_move_response(request, "error", "Ticket target position is invalid.", 422)
    current_index = next(
        index for index, candidate in enumerate(active_tickets) if candidate.id == ticket.id
    )
    active_tickets.pop(current_index)
    active_tickets.insert(target_index, ticket)
    ordered_tickets = active_tickets + [
        candidate for candidate in tickets if candidate.local_completed
    ]
    _normalize_positions(active_tickets)
    db.commit()
    return ticket_move_response(
        request,
        "success",
        message or f"Ticket {ticket.summary} reordered.",
        200,
        order=[candidate.id for candidate in ordered_tickets],
    )


def _top_level_tickets(db: Session) -> list[Ticket]:
    return list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id.is_(None))
            .order_by(Ticket.local_completed, Ticket.position, Ticket.created_at, Ticket.id)
        )
    )


def _subtasks(db: Session, parent_id: int) -> list[Ticket]:
    return list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id == parent_id)
            .order_by(Ticket.local_completed, Ticket.position, Ticket.created_at, Ticket.id)
        )
    )


def _prioritize_completion(ticket: Ticket, db: Session) -> None:
    if ticket.parent_id is None:
        siblings = _top_level_tickets(db)
    else:
        siblings = _subtasks(db, ticket.parent_id)

    active_siblings = [
        sibling for sibling in siblings if sibling.id != ticket.id and not sibling.local_completed
    ]
    if ticket.local_completed:
        done_siblings = [
            sibling for sibling in siblings if sibling.id != ticket.id and sibling.local_completed
        ]
        _normalize_positions(active_siblings)
        ticket.position = max((sibling.position for sibling in done_siblings), default=-1) + 1
    else:
        _normalize_positions([ticket, *active_siblings])


def _append_unfinished(siblings: list[Ticket], ticket: Ticket) -> None:
    active_siblings = [sibling for sibling in siblings if not sibling.local_completed]
    active_siblings.append(ticket)
    _normalize_positions(active_siblings)


def _normalize_positions(siblings: list[Ticket]) -> None:
    for index, sibling in enumerate(siblings):
        sibling.position = index


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _validate_component(value: str | None, db: Session) -> str | None:
    normalized = value.strip() if value else ""
    if not normalized or not is_safe_local_component_name(normalized):
        return None
    component = db.scalar(select(Component).where(Component.name == normalized))
    return component.name if component is not None else None


def _is_jira_issue_reference_candidate(value: str) -> bool:
    from .jira_service import is_jira_issue_reference_candidate

    return is_jira_issue_reference_candidate(value)


def _error_response(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=status_code)
