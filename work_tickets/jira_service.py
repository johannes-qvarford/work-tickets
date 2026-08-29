from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .jira import JiraClient, JiraError, JiraIssue
from .models import JiraConfig, Ticket

_JIRA_ISSUE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[A-Za-z0-9]+")
__all__ = [
    "JiraClientFactory",
    "JiraError",
    "delete_linked_jira_issue",
    "import_ticket_from_jira",
    "is_jira_issue_reference_candidate",
    "parse_jira_issue_reference",
    "reconcile_jira_subtasks",
    "save_jira_issue",
    "sync_subtask",
    "sync_ticket",
    "sync_ticket_from_jira",
]


type JiraClientFactory = Callable[[JiraConfig], JiraClient]


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


def is_jira_issue_reference_candidate(value: str) -> bool:
    return bool(
        _JIRA_ISSUE_KEY.fullmatch(value) or value.lower().startswith(("http://", "https://"))
    )


def delete_linked_jira_issue(
    ticket: Ticket,
    db: Session,
    *,
    jira_client_factory: JiraClientFactory = JiraClient,
) -> str | None:
    """Try to remove a linked Jira issue and return a user-facing failure, if any."""
    if ticket.local_completed:
        return "done items can only be marked active"
    if not ticket.jira_issue_key:
        return None
    issue_key = ticket.jira_issue_key
    config = db.get(JiraConfig, 1)
    if config is None:
        return f"linked Jira issue {issue_key} could not be deleted: Jira is not configured."

    jira = None
    try:
        jira = jira_client_factory(config)
        jira.delete_issue(issue_key)
    except JiraError as exc:
        return f"linked Jira issue {issue_key} could not be deleted: {exc}"
    finally:
        if jira is not None:
            jira.close()
    return None


def sync_ticket(
    ticket: Ticket,
    db: Session,
    *,
    jira_client_factory: JiraClientFactory = JiraClient,
) -> None:
    if ticket.parent_id is not None:
        raise JiraError(
            "Only top-level tickets can sync to Jira; sync the parent to include all subtasks."
        )
    if ticket.local_completed:
        raise JiraError("Done tickets can only be marked active.")
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before syncing.")
    jira = jira_client_factory(config)
    try:
        if ticket.jira_issue_key:
            issue = jira.update_issue(ticket.jira_issue_key, ticket.summary, ticket.description)
        else:
            issue = jira.create_issue(ticket.summary, ticket.description)
        synced_at = datetime.utcnow()
        save_jira_issue(ticket, issue, synced_at)
        db.commit()
        for subtask in list(ticket.subtasks):
            if subtask.local_completed:
                continue
            try:
                if subtask.jira_issue_key:
                    subtask_issue = jira.update_issue(
                        subtask.jira_issue_key, subtask.summary, subtask.description
                    )
                else:
                    subtask_issue = jira.create_subtask(
                        issue.key, subtask.summary, subtask.description
                    )
                save_jira_issue(subtask, subtask_issue, synced_at)
                db.commit()
            except JiraError as exc:
                raise JiraError(
                    f"Parent {issue.key} synced, but subtask '{subtask.summary}' failed: {exc} "
                    "Retry the parent sync to continue."
                ) from exc
    finally:
        jira.close()


def sync_subtask(
    subtask: Ticket,
    db: Session,
    *,
    jira_client_factory: JiraClientFactory = JiraClient,
) -> None:
    if subtask.parent_id is None:
        raise JiraError("Only subtasks can use the subtask edit sync path.")
    if subtask.local_completed:
        raise JiraError("Done subtasks can only be marked active.")
    if not subtask.jira_issue_key:
        raise JiraError("Subtask has not been synced to Jira yet.")
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before syncing.")
    jira = jira_client_factory(config)
    try:
        issue = jira.update_issue(subtask.jira_issue_key, subtask.summary, subtask.description)
        save_jira_issue(subtask, issue, datetime.utcnow())
        db.commit()
    finally:
        jira.close()


def import_ticket_from_jira(
    reference: str,
    planned_date: date | None,
    category_id: int | None,
    notes: str,
    db: Session,
    *,
    jira_client_factory: JiraClientFactory = JiraClient,
) -> Ticket:
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before importing.")
    issue_key = parse_jira_issue_reference(reference, config.browser_base_url or config.base_url)
    if db.scalar(select(Ticket.id).where(Ticket.jira_issue_key == issue_key)) is not None:
        raise JiraError(f"A local ticket is already linked to Jira issue {issue_key}.")

    jira = jira_client_factory(config)
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
    existing_tickets = list(
        db.scalars(
            select(Ticket)
            .where(Ticket.parent_id.is_(None))
            .order_by(Ticket.local_completed, Ticket.position, Ticket.created_at, Ticket.id)
        )
    )
    ticket = Ticket(
        summary=synced.issue.summary,
        description=synced.issue.description or "",
        planned_date=planned_date,
        notes=notes,
        category_id=category_id,
        position=0,
        jira_issue_key=synced.issue.key,
        jira_status_name=synced.issue.status_name,
        synced_at=synced_at,
    )
    active_tickets = [candidate for candidate in existing_tickets if not candidate.local_completed]
    active_tickets.append(ticket)
    for position, active_ticket in enumerate(active_tickets):
        active_ticket.position = position
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


def sync_ticket_from_jira(
    ticket: Ticket,
    db: Session,
    *,
    jira_client_factory: JiraClientFactory = JiraClient,
) -> None:
    if ticket.parent_id is not None:
        raise JiraError(
            "Only top-level tickets can sync from Jira; sync the parent to include all subtasks."
        )
    if ticket.local_completed:
        raise JiraError("Done tickets can only be marked active.")
    if not ticket.jira_issue_key:
        raise JiraError("Ticket has not been synced to Jira yet.")
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before syncing.")
    jira = jira_client_factory(config)
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

    synced_at = datetime.utcnow()
    save_jira_issue(ticket, synced.issue, synced_at, clear_missing_fields=True)
    reconcile_jira_subtasks(ticket, remote_subtasks_by_key, synced_at, db)
    db.flush()
    db.commit()


def reconcile_jira_subtasks(
    ticket: Ticket,
    remote_subtasks_by_key: dict[str, JiraIssue],
    synced_at: datetime,
    db: Session,
) -> None:
    """Make Jira-linked children match Jira while retaining local-only fields."""
    local_subtasks = list(ticket.subtasks)
    local_subtasks_by_key: dict[str, Ticket] = {}
    for subtask in local_subtasks:
        if not subtask.jira_issue_key:
            continue
        if subtask.jira_issue_key in local_subtasks_by_key:
            raise JiraError(f"Local subtasks have duplicate Jira key {subtask.jira_issue_key}.")
        local_subtasks_by_key[subtask.jira_issue_key] = subtask

    retained_subtasks: list[Ticket] = []
    for subtask in local_subtasks:
        if subtask.jira_issue_key and subtask.jira_issue_key not in remote_subtasks_by_key:
            if subtask.local_completed:
                retained_subtasks.append(subtask)
            else:
                db.delete(subtask)
        else:
            retained_subtasks.append(subtask)

    active_subtasks = [subtask for subtask in retained_subtasks if not subtask.local_completed]
    for issue in remote_subtasks_by_key.values():
        matching_subtask = local_subtasks_by_key.get(issue.key)
        if matching_subtask is None:
            matching_subtask = Ticket(parent=ticket, position=len(active_subtasks))
            active_subtasks.append(matching_subtask)
            db.add(matching_subtask)
        elif matching_subtask.local_completed:
            continue
        save_jira_issue(matching_subtask, issue, synced_at, clear_missing_fields=True)
    for position, subtask in enumerate(active_subtasks):
        subtask.position = position
    # Completed subtasks are intentionally excluded: parent sync must not mutate them.


def save_jira_issue(
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
