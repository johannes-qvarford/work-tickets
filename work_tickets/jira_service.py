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
    "canonicalize_jira_key",
    "delete_linked_jira_issue",
    "fetch_reviews",
    "import_ticket_from_jira",
    "is_jira_issue_reference_candidate",
    "parse_jira_issue_reference",
    "reconcile_jira_subtasks",
    "save_jira_issue",
    "sync_subtask",
    "sync_ticket",
    "sync_ticket_from_jira",
    "transition_jira_issue",
]


type JiraClientFactory = Callable[[JiraConfig], JiraClient]


def canonicalize_jira_key(key: str) -> str:
    """Return the stable representation used for stored and runtime Jira keys."""
    return key.strip().upper()


def fetch_reviews(
    db: Session,
    *,
    jira_client_factory: JiraClientFactory = JiraClient,
) -> dict[str, object]:
    """Fetch the current user's in-review Jira issues and match local tickets."""
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before viewing reviews.")

    local_tickets_by_key = {
        canonicalize_jira_key(ticket.jira_issue_key): ticket
        for ticket in db.scalars(select(Ticket)).all()
        if ticket.jira_issue_key
    }
    jql = (
        f"project = {_jql_value(config.project_key)} "
        f"AND issuetype = {_jql_value(config.issue_type)} "
        f"AND status = {_jql_value(config.in_review_status)} "
        "AND assignee = currentUser() ORDER BY key"
    )
    jira = jira_client_factory(config)
    try:
        search_results = jira.search_issues(jql)
        reviews: list[dict[str, object]] = []
        for search_result in search_results:
            review = _review_data(search_result, local_tickets_by_key)
            try:
                issue = jira.get_issue(search_result.key)
            except JiraError as exc:
                review["error"] = str(exc)
            else:
                review.update(_review_data(issue, local_tickets_by_key))
            reviews.append(review)
    finally:
        jira.close()
    return {"reviews": reviews}


def transition_jira_issue(
    issue_key: str,
    target_status: str,
    db: Session,
    *,
    jira_client_factory: JiraClientFactory = JiraClient,
) -> JiraIssue:
    """Transition a Jira issue to a configured destination status if needed."""
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before changing status.")

    canonical_key = canonicalize_jira_key(issue_key)
    jira = jira_client_factory(config)
    try:
        current = jira.get_issue(canonical_key)
        if current.status_name == target_status:
            return current
        return jira.transition_issue(
            canonical_key,
            target_status,
            current_status=current.status_name,
        )
    finally:
        jira.close()


def _jql_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _review_data(
    issue: JiraIssue,
    local_tickets_by_key: dict[str, Ticket],
) -> dict[str, object]:
    local_ticket = local_tickets_by_key.get(canonicalize_jira_key(issue.key))
    return {
        "key": issue.key,
        "summary": issue.summary or issue.key,
        "description": issue.description or "",
        "issue_type_name": issue.issue_type_name,
        "status_name": issue.status_name,
        "local_ticket": (
            {
                "id": local_ticket.id,
                "summary": local_ticket.summary,
                "parent_id": local_ticket.parent_id,
            }
            if local_ticket is not None
            else None
        ),
        "error": None,
    }


def parse_jira_issue_reference(reference: str, browser_base_url: str) -> str:
    """Extract a normalized Jira issue key from a key or configured browser URL."""
    value = reference.strip()
    if _JIRA_ISSUE_KEY.fullmatch(value):
        return canonicalize_jira_key(value)

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
    return canonicalize_jira_key(key)


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
    issue_key = canonicalize_jira_key(ticket.jira_issue_key)
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
            issue = jira.update_issue(
                canonicalize_jira_key(ticket.jira_issue_key), ticket.summary, ticket.description
            )
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
                        canonicalize_jira_key(subtask.jira_issue_key),
                        subtask.summary,
                        subtask.description,
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
        issue = jira.update_issue(
            canonicalize_jira_key(subtask.jira_issue_key), subtask.summary, subtask.description
        )
        save_jira_issue(subtask, issue, datetime.utcnow())
        db.commit()
    finally:
        jira.close()


def import_ticket_from_jira(
    reference: str,
    planned_date: date | None,
    category_id: int | None,
    component: str | None,
    notes: str,
    db: Session,
    *,
    jira_client_factory: JiraClientFactory = JiraClient,
) -> Ticket:
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before importing.")
    issue_key = parse_jira_issue_reference(reference, config.browser_base_url or config.base_url)
    if any(
        stored_key is not None and canonicalize_jira_key(stored_key) == issue_key
        for stored_key in db.scalars(
            select(Ticket.jira_issue_key).where(Ticket.jira_issue_key.is_not(None))
        )
    ):
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
        subtask_key = canonicalize_jira_key(subtask.key)
        if subtask_key == issue_key:
            raise JiraError(f"Jira returned the parent issue {issue_key} as its own subtask.")
        if subtask_key in remote_subtask_keys:
            raise JiraError(f"Jira returned duplicate subtask key {subtask_key}.")
        if not subtask.summary:
            raise JiraError(f"Jira returned subtask {subtask_key} without a summary.")
        remote_subtask_keys.add(subtask_key)
    imported_issue_keys = {issue_key, *remote_subtask_keys}
    existing_issue_key = next(
        (
            stored_key
            for stored_key in db.scalars(
                select(Ticket.jira_issue_key).where(Ticket.jira_issue_key.is_not(None))
            )
            if stored_key is not None and canonicalize_jira_key(stored_key) in imported_issue_keys
        ),
        None,
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
        component=component,
        position=0,
        jira_issue_key=canonicalize_jira_key(synced.issue.key),
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
                jira_issue_key=canonicalize_jira_key(subtask.key),
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
        synced = jira.get_issue_with_subtasks(canonicalize_jira_key(ticket.jira_issue_key))
    finally:
        jira.close()
    if not synced.issue.summary:
        raise JiraError("Jira returned an issue without a summary.")

    remote_subtasks_by_key: dict[str, JiraIssue] = {}
    for issue in synced.subtasks:
        issue_key = canonicalize_jira_key(issue.key)
        parent_key = canonicalize_jira_key(synced.issue.key)
        if issue_key == parent_key:
            raise JiraError(f"Jira returned the parent issue {parent_key} as its own subtask.")
        if issue_key in remote_subtasks_by_key:
            raise JiraError(f"Jira returned duplicate subtask key {issue_key}.")
        if not issue.summary:
            raise JiraError(f"Jira returned subtask {issue_key} without a summary.")
        remote_subtasks_by_key[issue_key] = issue

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
    normalized_remote_subtasks: dict[str, JiraIssue] = {}
    for key, issue in remote_subtasks_by_key.items():
        canonical_key = canonicalize_jira_key(key)
        if canonical_key in normalized_remote_subtasks:
            raise JiraError(f"Jira returned duplicate subtask key {canonical_key}.")
        normalized_remote_subtasks[canonical_key] = issue
    local_subtasks = list(ticket.subtasks)
    local_subtasks_by_key: dict[str, Ticket] = {}
    for subtask in local_subtasks:
        if not subtask.jira_issue_key:
            continue
        key = canonicalize_jira_key(subtask.jira_issue_key)
        if key in local_subtasks_by_key:
            raise JiraError(f"Local subtasks have duplicate Jira key {subtask.jira_issue_key}.")
        local_subtasks_by_key[key] = subtask

    retained_subtasks: list[Ticket] = []
    for subtask in local_subtasks:
        if (
            subtask.jira_issue_key
            and canonicalize_jira_key(subtask.jira_issue_key) not in normalized_remote_subtasks
        ):
            if subtask.local_completed:
                retained_subtasks.append(subtask)
            else:
                db.delete(subtask)
        else:
            retained_subtasks.append(subtask)

    active_subtasks = [subtask for subtask in retained_subtasks if not subtask.local_completed]
    for key, issue in normalized_remote_subtasks.items():
        matching_subtask = local_subtasks_by_key.get(key)
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
    ticket.jira_issue_key = canonicalize_jira_key(issue.key)
    if issue.summary is not None:
        ticket.summary = issue.summary
    elif clear_missing_fields:
        raise JiraError(f"Jira returned issue {issue.key} without a summary.")
    if issue.description is not None or clear_missing_fields:
        ticket.description = issue.description or ""
    ticket.jira_status_name = issue.status_name
    ticket.synced_at = synced_at
