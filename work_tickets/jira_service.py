from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from unicodedata import category
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .gitlab import GitLabClient, GitLabError, GitLabMergeRequest
from .jira import JiraClient, JiraError, JiraIssue
from .models import JiraConfig, Ticket

_JIRA_ISSUE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[A-Za-z0-9]+")
_URL_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_])https?://[^\s<>\"']+", re.IGNORECASE)
_MERGE_REQUEST_PATH = re.compile(
    r"(?P<repository_path>.+)/-/merge_requests/(?P<number>[1-9][0-9]*)/?$"
)


@dataclass(frozen=True)
class MergeRequestReference:
    repository: str
    number: int
    url: str


@dataclass(frozen=True)
class MergeRequestSelection:
    selected: dict[str, object] | None
    enabled: bool
    reason: str


__all__ = [
    "JiraClientFactory",
    "GitLabClientFactory",
    "MergeRequestSelection",
    "JiraError",
    "MergeRequestReference",
    "select_merge_request",
    "canonicalize_jira_key",
    "detect_merge_requests",
    "delete_linked_jira_issue",
    "fetch_reviews",
    "import_ticket_from_jira",
    "is_jira_issue_reference_candidate",
    "parse_jira_issue_reference",
    "parse_gitlab_base_url",
    "reconcile_jira_subtasks",
    "save_jira_issue",
    "sync_subtask",
    "sync_ticket",
    "sync_ticket_from_jira",
    "transition_jira_issue",
    "ready_to_merge_review",
]


type JiraClientFactory = Callable[[JiraConfig], JiraClient]
type GitLabClientFactory = Callable[[JiraConfig], GitLabClient]

_OPEN_MERGE_REQUEST_STATE = "opened"
_CLOSED_MERGE_REQUEST_STATES = {"closed", "locked", "merged"}
_KNOWN_MERGE_REQUEST_STATES = {
    _OPEN_MERGE_REQUEST_STATE,
    *_CLOSED_MERGE_REQUEST_STATES,
}
_MERGE_POLL_TIMEOUT_SECONDS = 60.0
_MERGE_POLL_INITIAL_DELAY_SECONDS = 0.5
_MERGE_POLL_MAX_DELAY_SECONDS = 5.0
_GIT_SHA = re.compile(r"[0-9a-fA-F]{7,40}\Z")
_REVIEW_COMMENT = "Tested and reviewed."
_DISCUSSION_APPROVAL_COMMENT = "Approved 👑"
_ready_to_merge_locks: dict[str, threading.Lock] = {}
_ready_to_merge_locks_guard = threading.Lock()


def canonicalize_jira_key(key: str) -> str:
    """Return the stable representation used for stored and runtime Jira keys."""
    return key.strip().upper()


def detect_merge_requests(
    description: str | Mapping[str, object] | None,
    gitlab_base_url: str,
) -> list[MergeRequestReference]:
    """Find GitLab merge request links in a Jira description.

    Jira Cloud descriptions are converted to plain text by ``JiraClient``, but
    accepting the raw ADF shape here also preserves links whose visible text is
    not the URL. Only links with the same HTTP origin and a path beneath the
    configured GitLab base path are considered.
    """
    base = parse_gitlab_base_url(gitlab_base_url)
    if base is None:
        return []

    found: list[MergeRequestReference] = []
    seen: set[tuple[str, int]] = set()
    for value in _description_values(description):
        for match in _URL_CANDIDATE.finditer(value):
            candidate = match.group(0).rstrip(".,;:!?)]}")
            reference = _parse_merge_request_url(candidate, base)
            if reference is None:
                continue
            reference_value, repository_path = reference
            identity = (repository_path, reference_value.number)
            if identity not in seen:
                seen.add(identity)
                found.append(reference_value)
    return found


def _description_values(value: object) -> list[str]:
    """Return searchable text and link attributes from plain text or ADF."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        values: list[str] = []
        for child in value.values():
            values.extend(_description_values(child))
        return values
    if isinstance(value, list):
        values = []
        for child in value:
            values.extend(_description_values(child))
        return values
    return []


def parse_gitlab_base_url(value: str) -> tuple[str, str, int, str] | None:
    """Parse a GitLab base URL using the same safe rules as link detection."""
    parsed_url = _parse_gitlab_url(value)
    if parsed_url is None:
        return None
    scheme, hostname, port, path = parsed_url
    return scheme, hostname, port, path.rstrip("/") or "/"


def _parse_gitlab_url(value: str) -> tuple[str, str, int, str] | None:
    """Parse an absolute GitLab URL without accepting unsafe URL components."""
    if not value or _contains_whitespace_or_control(value):
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (AttributeError, ValueError):
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
        or parsed.netloc.endswith(":")
    ):
        return None
    scheme = parsed.scheme.lower()
    path = unquote(parsed.path)
    if _contains_whitespace_or_control(path):
        return None
    if any(segment in {".", ".."} for segment in path.split("/")):
        return None
    return scheme, hostname.lower().rstrip("."), _effective_port(scheme, port), path


def _contains_whitespace_or_control(value: str) -> bool:
    return any(character.isspace() or category(character) == "Cc" for character in value)


def _parse_merge_request_url(
    value: str,
    base: tuple[str, str, int, str],
) -> tuple[MergeRequestReference, str] | None:
    parsed_url = _parse_gitlab_url(value)
    if parsed_url is None:
        return None
    if parsed_url[:3] != base[:3]:
        return None

    base_path = base[3]
    path = parsed_url[3]
    if base_path == "/":
        relative_path = path.removeprefix("/")
    elif path.startswith(f"{base_path}/"):
        relative_path = path[len(base_path) + 1 :]
    else:
        return None
    match = _MERGE_REQUEST_PATH.fullmatch(relative_path)
    if match is None:
        return None
    repository_path = match.group("repository_path")
    if any(not segment for segment in repository_path.split("/")):
        return None
    repository = repository_path.rsplit("/", 1)[-1]
    if not repository:
        return None
    return MergeRequestReference(
        repository=repository,
        number=int(match.group("number")),
        url=value,
    ), repository_path


def _effective_port(scheme: str, port: int | None) -> int:
    return port if port is not None else {"http": 80, "https": 443}[scheme]


def select_merge_request(
    merge_requests: list[tuple[MergeRequestReference, GitLabMergeRequest]],
) -> MergeRequestSelection:
    """Select one MR without guessing when the GitLab state is ambiguous."""
    if not merge_requests:
        return MergeRequestSelection(
            selected=None,
            enabled=False,
            reason="No merge requests were found in the Jira description.",
        )

    candidates: list[tuple[MergeRequestReference, GitLabMergeRequest, datetime]] = []
    for reference, merge_request in merge_requests:
        if (
            not isinstance(merge_request.state, str)
            or merge_request.state not in _KNOWN_MERGE_REQUEST_STATES
        ):
            raise JiraError(
                f"GitLab returned an unsupported state '{merge_request.state}' for "
                f"merge request {reference.repository}!{reference.number}."
            )
        candidates.append(
            (reference, merge_request, _parse_merge_request_updated_at(reference, merge_request))
        )

    open_candidates = [
        candidate for candidate in candidates if candidate[1].state == _OPEN_MERGE_REQUEST_STATE
    ]
    if len(open_candidates) > 1:
        return MergeRequestSelection(
            selected=None,
            enabled=False,
            reason=("Ready to Merge requires one unambiguous MR; multiple open MRs were found."),
        )
    if len(open_candidates) == 1:
        return MergeRequestSelection(
            selected=_merge_request_dict(open_candidates[0]),
            enabled=True,
            reason="Selected the only open MR; closed MRs were ignored.",
        )

    closed_candidates = [
        candidate for candidate in candidates if candidate[1].state in _CLOSED_MERGE_REQUEST_STATES
    ]
    if not closed_candidates:
        return MergeRequestSelection(
            selected=None,
            enabled=False,
            reason="No merge requests have a usable open or closed state.",
        )
    most_recent = max(closed_candidates, key=lambda candidate: candidate[2])
    if sum(candidate[2] == most_recent[2] for candidate in closed_candidates) > 1:
        return MergeRequestSelection(
            selected=None,
            enabled=False,
            reason="The most recently updated closed MRs are tied; one unambiguous MR is required.",
        )
    return MergeRequestSelection(
        selected=_merge_request_dict(most_recent),
        enabled=True,
        reason="All MRs are closed; selected the most recently updated MR.",
    )


def _parse_merge_request_updated_at(
    reference: MergeRequestReference, merge_request: GitLabMergeRequest
) -> datetime:
    value = merge_request.updated_at
    if not isinstance(value, str) or not value:
        raise JiraError(
            f"GitLab returned an invalid updated_at for merge request "
            f"{reference.repository}!{reference.number}."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JiraError(
            f"GitLab returned an invalid updated_at for merge request "
            f"{reference.repository}!{reference.number}."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _merge_request_dict(
    candidate: tuple[MergeRequestReference, GitLabMergeRequest, datetime],
) -> dict[str, object]:
    reference, merge_request, _ = candidate
    payload: dict[str, object] = {
        **asdict(reference),
        "state": merge_request.state,
        "updated_at": merge_request.updated_at,
        "draft": merge_request.draft,
    }
    if merge_request.merge_commit_sha is not None:
        payload["merge_commit_sha"] = merge_request.merge_commit_sha
    if merge_request.web_url is not None:
        payload["web_url"] = merge_request.web_url
    return payload


def _retrieve_merge_requests(
    description: str | Mapping[str, object] | None,
    gitlab_base_url: str,
    gitlab_client: GitLabClient | None,
) -> tuple[list[dict[str, object]], MergeRequestSelection]:
    references = detect_merge_requests(description, gitlab_base_url)
    if not references:
        return [], select_merge_request([])
    if gitlab_client is None:
        raise JiraError("GitLab merge request details could not be retrieved.")

    base = parse_gitlab_base_url(gitlab_base_url)
    if base is None:
        raise JiraError("GitLab merge request links use an invalid configured base URL.")
    details: list[tuple[MergeRequestReference, GitLabMergeRequest]] = []
    for reference in references:
        parsed = _parse_merge_request_url(reference.url, base)
        if parsed is None:
            raise JiraError(
                f"Could not determine the GitLab project for merge request "
                f"{reference.repository}!{reference.number}."
            )
        _, project_path = parsed
        try:
            merge_request = gitlab_client.get_merge_request(project_path, reference.number)
        except GitLabError as exc:
            raise JiraError(str(exc)) from exc
        details.append((reference, merge_request))

    selection = select_merge_request(details)
    payload = [
        _merge_request_dict(
            (reference, merge_request, _parse_merge_request_updated_at(reference, merge_request))
        )
        for reference, merge_request in details
    ]
    return payload, selection


def fetch_reviews(
    db: Session,
    *,
    jira_client_factory: JiraClientFactory = JiraClient,
    gitlab_client_factory: GitLabClientFactory = GitLabClient,
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
        f"AND status = {_jql_value(config.in_review_status)} "
        "AND assignee = currentUser() ORDER BY key"
    )
    jira = jira_client_factory(config)
    gitlab: GitLabClient | None = None
    gitlab_error: str | None = None
    if parse_gitlab_base_url(config.gitlab_base_url) is not None:
        try:
            gitlab = gitlab_client_factory(config)
        except GitLabError as exc:
            gitlab_error = str(exc)
    try:
        search_results = jira.search_issues(jql)
        reviews: list[dict[str, object]] = []
        for search_result in search_results:
            review = _review_data(
                search_result,
                local_tickets_by_key,
                config.gitlab_base_url,
                gitlab_client=None,
                resolve_merge_requests=False,
            )
            try:
                issue = jira.get_issue(search_result.key)
            except JiraError as exc:
                review["error"] = str(exc)
            else:
                try:
                    review.update(
                        _review_data(
                            issue,
                            local_tickets_by_key,
                            config.gitlab_base_url,
                            gitlab_client=gitlab,
                            gitlab_error=gitlab_error,
                        )
                    )
                except JiraError as exc:
                    review.update(
                        _review_data(
                            issue,
                            local_tickets_by_key,
                            config.gitlab_base_url,
                            gitlab_client=None,
                            resolve_merge_requests=False,
                        )
                    )
                    review["error"] = str(exc)
            reviews.append(review)
    finally:
        if gitlab is not None:
            gitlab.close()
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
        return _transition_review_status(
            jira,
            canonical_key,
            target_status,
            config,
            allowed_sources=None,
        )
    finally:
        jira.close()


def ready_to_merge_review(
    issue_key: str,
    db: Session,
    *,
    jira_client_factory: JiraClientFactory = JiraClient,
    gitlab_client_factory: GitLabClientFactory = GitLabClient,
) -> JiraIssue:
    """Complete review preparation, merge the selected MR, and update Jira."""
    canonical_key = canonicalize_jira_key(issue_key)
    with _ready_to_merge_locks_guard:
        lock = _ready_to_merge_locks.setdefault(canonical_key, threading.Lock())
    with lock:
        return _ready_to_merge_review_locked(
            canonical_key,
            db,
            jira_client_factory=jira_client_factory,
            gitlab_client_factory=gitlab_client_factory,
        )


def _ready_to_merge_review_locked(
    canonical_key: str,
    db: Session,
    *,
    jira_client_factory: JiraClientFactory,
    gitlab_client_factory: GitLabClientFactory,
) -> JiraIssue:
    """Run one serialized attempt; progress is read from remote state."""
    config = db.get(JiraConfig, 1)
    if config is None:
        raise JiraError("Jira is not configured. Configure Jira before completing a review.")

    jira = jira_client_factory(config)
    gitlab: GitLabClient | None = None
    try:
        current = jira.get_issue(canonical_key)
        if parse_gitlab_base_url(config.gitlab_base_url) is not None:
            try:
                gitlab = gitlab_client_factory(config)
            except GitLabError as exc:
                raise JiraError(str(exc)) from exc
        description = (
            current.description_adf if current.description_adf is not None else current.description
        )
        _, selection = _retrieve_merge_requests(
            description,
            config.gitlab_base_url,
            gitlab,
        )
        if not selection.enabled:
            raise JiraError(selection.reason)
        merged = _approve_selected_merge_request(
            config.gitlab_base_url,
            selection,
            gitlab,
        )
        if merged is None:
            merged = _mark_selected_merge_request_ready(
                config.gitlab_base_url,
                selection,
                gitlab,
            )
        if merged is None:
            merged = _resolve_selected_merge_request_discussions(
                config.gitlab_base_url,
                selection,
                gitlab,
            )
        if merged is None:
            merged = _merge_selected_merge_request(config.gitlab_base_url, selection, gitlab)
        short_sha, commit_url = _merged_commit_link(merged)
        current = _transition_review_status(
            jira,
            canonical_key,
            config.ready_to_merge_status,
            config,
            allowed_sources={config.in_review_status},
            later_statuses={config.ready_to_deploy_status},
        )
        _add_jira_comment_if_missing(jira, canonical_key, _REVIEW_COMMENT)
        _add_jira_comment_if_missing(jira, canonical_key, f"Merged with [{short_sha}|{commit_url}]")
        current = _transition_review_status(
            jira,
            canonical_key,
            config.ready_to_deploy_status,
            config,
            allowed_sources={config.ready_to_merge_status},
        )
        return current
    finally:
        if gitlab is not None:
            gitlab.close()
        jira.close()


def _transition_review_status(
    jira: JiraClient,
    issue_key: str,
    target_status: str,
    config: JiraConfig,
    *,
    allowed_sources: set[str] | None,
    later_statuses: set[str] | None = None,
) -> JiraIssue:
    """Transition only from a freshly confirmed safe workflow state."""
    # GitLab preparation can take long enough for Jira to change. Never use the
    # issue snapshot fetched before that work as the transition source.
    current = jira.get_issue(issue_key)
    if current.status_name == target_status:
        return current
    if current.status_name in _completed_statuses(config.completed_statuses):
        raise JiraError(
            f"Jira issue {issue_key} is already in terminal status '{current.status_name}' "
            f"and cannot transition to '{target_status}'."
        )
    if later_statuses is not None and current.status_name in later_statuses:
        # A later workflow state is already further along. Do not reverse it;
        # the caller may still need to reconcile comments or the final target.
        return current
    if allowed_sources is not None and current.status_name not in allowed_sources:
        expected = ", ".join(sorted(allowed_sources))
        raise JiraError(
            f"Jira issue {issue_key} is in status '{current.status_name}', not a safe source "
            f"for transition to '{target_status}' (expected {expected})."
        )

    try:
        jira.transition_issue(
            issue_key,
            target_status,
            current_status=current.status_name,
        )
    except JiraError as exc:
        try:
            confirmed = jira.get_issue(issue_key)
        except JiraError as confirm_error:
            raise exc from confirm_error
        if confirmed.status_name == target_status:
            return confirmed
        raise
    try:
        confirmed = jira.get_issue(issue_key)
    except JiraError as exc:
        raise JiraError(
            f"Jira transition to '{target_status}' did not return a verifiable target state."
        ) from exc
    if confirmed.status_name == target_status:
        return confirmed
    raise JiraError(
        f"Jira transition to '{target_status}' was not confirmed; current status is "
        f"'{confirmed.status_name}'."
    )


def _completed_statuses(value: str) -> set[str]:
    return {status.strip() for status in value.split(",") if status.strip()}


def _jira_comment_reader(jira: JiraClient) -> Callable[[str], list[str]] | None:
    reader = getattr(jira, "get_comments", None)
    return reader if callable(reader) else None


def _add_jira_comment_if_missing(jira: JiraClient, issue_key: str, comment: str) -> None:
    """Post an exact Jira comment only when it is not already present."""
    reader = _jira_comment_reader(jira)
    if reader is not None and comment in reader(issue_key):
        return
    try:
        jira.add_comment(issue_key, comment)
    except JiraError:
        if reader is not None:
            try:
                if comment in reader(issue_key):
                    return
            except JiraError:
                pass
        raise


def _selected_merge_request_target(
    gitlab_base_url: str,
    selection: MergeRequestSelection,
    gitlab_client: GitLabClient | None,
) -> tuple[MergeRequestReference, str]:
    if selection.selected is None or gitlab_client is None:
        raise JiraError("GitLab merge request details could not be retrieved.")

    selected_url = selection.selected.get("url")
    if not isinstance(selected_url, str):
        raise JiraError("The selected GitLab merge request has an invalid URL.")
    base = parse_gitlab_base_url(gitlab_base_url)
    parsed = _parse_merge_request_url(selected_url, base) if base is not None else None
    if parsed is None:
        raise JiraError("Could not determine the GitLab project for the selected merge request.")
    return parsed


def _refresh_selected_merge_request(
    gitlab_base_url: str,
    selection: MergeRequestSelection,
    gitlab_client: GitLabClient | None,
) -> tuple[MergeRequestReference, str, GitLabMergeRequest]:
    reference, project_path = _selected_merge_request_target(
        gitlab_base_url,
        selection,
        gitlab_client,
    )
    assert gitlab_client is not None
    try:
        current = gitlab_client.get_merge_request(project_path, reference.number)
    except GitLabError as exc:
        raise JiraError(str(exc)) from exc
    if current.state not in _KNOWN_MERGE_REQUEST_STATES:
        raise JiraError(
            f"GitLab returned an unsupported state '{current.state}' for merge request "
            f"{reference.repository}!{reference.number}."
        )
    if current.state != "opened" and current.state != "merged":
        raise JiraError(
            f"GitLab merge request {reference.repository}!{reference.number} is already "
            f"{current.state} and cannot be prepared for merge."
        )
    return reference, project_path, current


def _merged_after_gitlab_failure(
    gitlab_client: GitLabClient,
    project_path: str,
    number: int,
) -> GitLabMergeRequest | None:
    try:
        current = gitlab_client.get_merge_request(project_path, number)
        return current if current.state == "merged" else None
    except GitLabError:
        return None


def _merge_selected_merge_request(
    gitlab_base_url: str,
    selection: MergeRequestSelection,
    gitlab_client: GitLabClient | None,
) -> GitLabMergeRequest:
    """Squash-merge the selected MR and return GitLab's confirmed merge payload."""
    if selection.selected is None or gitlab_client is None:
        raise JiraError("GitLab merge request details could not be retrieved.")

    # Revalidate immediately before the POST. The selection payload may have
    # been fetched before another actor merged or closed this MR.
    reference, project_path, current = _refresh_selected_merge_request(
        gitlab_base_url,
        selection,
        gitlab_client,
    )
    if current.state == "merged":
        return current
    if current.state != "opened":
        raise JiraError(
            f"GitLab merge request {reference.repository}!{reference.number} is already "
            f"{current.state} and cannot be merged."
        )

    try:
        merge_response = gitlab_client.merge_merge_request(project_path, reference.number)
        if merge_response.state not in _KNOWN_MERGE_REQUEST_STATES:
            raise JiraError(
                f"GitLab returned an unsupported state '{merge_response.state}' for merge request "
                f"{reference.repository}!{reference.number}."
            )
        return _wait_for_merge(
            gitlab_client,
            project_path,
            reference,
            merge_response,
        )
    except GitLabError as exc:
        # A concurrent merge can make GitLab reject the mutation; confirm that it
        # did not succeed before reporting the mutation error.
        try:
            current = gitlab_client.get_merge_request(project_path, reference.number)
        except GitLabError:
            raise JiraError(str(exc)) from exc
        if current.state != "merged":
            raise JiraError(str(exc)) from exc
        return current
    except JiraError:
        raise


def _wait_for_merge(
    gitlab_client: GitLabClient,
    project_path: str,
    reference: MergeRequestReference,
    initial_response: GitLabMergeRequest,
    *,
    timeout_seconds: float | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> GitLabMergeRequest:
    """Poll a merge response with bounded exponential backoff."""
    if initial_response.state not in _KNOWN_MERGE_REQUEST_STATES:
        raise JiraError(
            f"GitLab returned an unsupported state '{initial_response.state}' for merge request "
            f"{reference.repository}!{reference.number}."
        )
    if initial_response.state == "merged":
        return initial_response
    if initial_response.state in _CLOSED_MERGE_REQUEST_STATES:
        raise JiraError(
            f"GitLab merge request {reference.repository}!{reference.number} reached terminal "
            f"state '{initial_response.state}' while waiting to merge."
        )

    sleep_fn = time.sleep if sleep is None else sleep
    monotonic_fn = time.monotonic if monotonic is None else monotonic
    timeout = _MERGE_POLL_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    deadline = monotonic_fn() + timeout
    delay = _MERGE_POLL_INITIAL_DELAY_SECONDS
    while True:
        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            raise JiraError(
                f"Timed out waiting for GitLab merge request {reference.repository}!"
                f"{reference.number} to reach merged state."
            )
        sleep_fn(min(delay, remaining))
        try:
            current = gitlab_client.get_merge_request(project_path, reference.number)
        except GitLabError as exc:
            merged = _merged_after_gitlab_failure(gitlab_client, project_path, reference.number)
            if merged is not None:
                return merged
            raise JiraError(str(exc)) from exc
        if current.state == "merged":
            return current
        if current.state not in _KNOWN_MERGE_REQUEST_STATES:
            raise JiraError(
                f"GitLab returned an unsupported state '{current.state}' for merge request "
                f"{reference.repository}!{reference.number}."
            )
        if current.state in _CLOSED_MERGE_REQUEST_STATES:
            raise JiraError(
                f"GitLab merge request {reference.repository}!{reference.number} reached terminal "
                f"state '{current.state}' while waiting to merge."
            )
        delay = min(delay * 2, _MERGE_POLL_MAX_DELAY_SECONDS)


def _approve_selected_merge_request(
    gitlab_base_url: str,
    selection: MergeRequestSelection,
    gitlab_client: GitLabClient | None,
) -> GitLabMergeRequest | None:
    """Approve only the MR selected after resolving all links in the description."""
    reference, project_path, current = _refresh_selected_merge_request(
        gitlab_base_url,
        selection,
        gitlab_client,
    )
    assert gitlab_client is not None
    if current.state == "merged":
        return current
    try:
        approval = gitlab_client.get_merge_request_approval_state(project_path, reference.number)
        if approval.approved:
            return None
        gitlab_client.approve_merge_request(project_path, reference.number)
        if not gitlab_client.get_merge_request_approval_state(
            project_path, reference.number
        ).approved:
            raise JiraError(
                f"GitLab did not approve merge request {reference.repository}!{reference.number}."
            )
    except GitLabError as exc:
        merged = _merged_after_gitlab_failure(gitlab_client, project_path, reference.number)
        if merged is not None:
            return merged
        raise JiraError(str(exc)) from exc
    return None


def _mark_selected_merge_request_ready(
    gitlab_base_url: str,
    selection: MergeRequestSelection,
    gitlab_client: GitLabClient | None,
) -> GitLabMergeRequest | None:
    """Remove the draft flag from the MR selected after resolving all links."""
    reference, project_path, current = _refresh_selected_merge_request(
        gitlab_base_url,
        selection,
        gitlab_client,
    )
    assert gitlab_client is not None
    if current.state == "merged":
        return current
    if not current.draft:
        return None
    try:
        gitlab_client.mark_merge_request_ready(project_path, reference.number)
        refreshed = gitlab_client.get_merge_request(project_path, reference.number)
        if refreshed.state == "merged":
            return refreshed
        if refreshed.state not in _KNOWN_MERGE_REQUEST_STATES:
            raise JiraError(
                f"GitLab returned an unsupported state '{refreshed.state}' for merge request "
                f"{reference.repository}!{reference.number}."
            )
        if refreshed.state != "opened":
            raise JiraError(
                f"GitLab merge request {reference.repository}!{reference.number} is already "
                f"{refreshed.state} and cannot be prepared for merge."
            )
        if refreshed.draft:
            raise JiraError(
                f"GitLab did not mark merge request {reference.repository}!{reference.number} "
                "as ready."
            )
    except GitLabError as exc:
        merged = _merged_after_gitlab_failure(gitlab_client, project_path, reference.number)
        if merged is not None:
            return merged
        raise JiraError(str(exc)) from exc
    return None


def _resolve_selected_merge_request_discussions(
    gitlab_base_url: str,
    selection: MergeRequestSelection,
    gitlab_client: GitLabClient | None,
) -> GitLabMergeRequest | None:
    """Comment on and resolve every currently unresolved discussion in the selected MR."""
    reference, project_path, current = _refresh_selected_merge_request(
        gitlab_base_url,
        selection,
        gitlab_client,
    )
    assert gitlab_client is not None
    if current.state == "merged":
        return current

    try:
        discussions = gitlab_client.get_merge_request_discussions(project_path, reference.number)
        for discussion in discussions:
            unresolved_notes = tuple(
                note for note in discussion.notes if note.resolvable and not note.resolved
            )
            if not unresolved_notes:
                continue
            try:
                if not any(note.body == _DISCUSSION_APPROVAL_COMMENT for note in discussion.notes):
                    try:
                        gitlab_client.add_merge_request_discussion_note(
                            project_path,
                            reference.number,
                            discussion.id,
                            _DISCUSSION_APPROVAL_COMMENT,
                        )
                    except GitLabError as exc:
                        merged = _merged_after_gitlab_failure(
                            gitlab_client, project_path, reference.number
                        )
                        if merged is not None:
                            return merged
                        # A note POST can be applied remotely before its response
                        # is lost. Re-read the thread before deciding to fail so a
                        # retry cannot create a duplicate approval note.
                        try:
                            refreshed_discussions = gitlab_client.get_merge_request_discussions(
                                project_path, reference.number
                            )
                        except GitLabError as refresh_error:
                            raise exc from refresh_error
                        refreshed = next(
                            (
                                candidate
                                for candidate in refreshed_discussions
                                if candidate.id == discussion.id
                            ),
                            None,
                        )
                        if refreshed is None:
                            raise exc
                        if not any(
                            note.resolvable and not note.resolved for note in refreshed.notes
                        ):
                            continue
                        if not any(
                            note.body == _DISCUSSION_APPROVAL_COMMENT for note in refreshed.notes
                        ):
                            raise exc
                try:
                    gitlab_client.resolve_merge_request_discussion(
                        project_path,
                        reference.number,
                        discussion.id,
                    )
                except GitLabError as exc:
                    merged = _merged_after_gitlab_failure(
                        gitlab_client, project_path, reference.number
                    )
                    if merged is not None:
                        return merged
                    # The PUT may have succeeded even when its response was
                    # lost. Confirm this discussion before allowing a retry.
                    try:
                        refreshed_discussions = gitlab_client.get_merge_request_discussions(
                            project_path, reference.number
                        )
                    except GitLabError as refresh_error:
                        raise exc from refresh_error
                    refreshed = next(
                        (
                            candidate
                            for candidate in refreshed_discussions
                            if candidate.id == discussion.id
                        ),
                        None,
                    )
                    if refreshed is not None and not any(
                        note.resolvable and not note.resolved for note in refreshed.notes
                    ):
                        continue
                    raise
            except GitLabError as exc:
                merged = _merged_after_gitlab_failure(gitlab_client, project_path, reference.number)
                if merged is not None:
                    return merged
                raise JiraError(
                    f"Could not resolve GitLab discussion {discussion.id} on "
                    f"merge request {reference.repository}!{reference.number}: {exc}"
                ) from exc
    except GitLabError as exc:
        merged = _merged_after_gitlab_failure(gitlab_client, project_path, reference.number)
        if merged is not None:
            return merged
        raise JiraError(str(exc)) from exc
    return None


def _merged_commit_link(merge_request: GitLabMergeRequest) -> tuple[str, str]:
    """Validate the confirmed merge data and build the GitLab commit link."""
    if merge_request.state != "merged":
        raise JiraError("GitLab did not confirm that the merge request was merged.")
    sha = merge_request.merge_commit_sha
    if not isinstance(sha, str) or _GIT_SHA.fullmatch(sha) is None:
        raise JiraError("GitLab confirmed the merge but did not return a valid merge commit SHA.")
    web_url = merge_request.web_url
    if not isinstance(web_url, str) or not web_url.startswith(("http://", "https://")):
        raise JiraError("GitLab confirmed the merge but did not return a valid merge request URL.")
    parsed = _parse_gitlab_url(web_url)
    if parsed is None or re.fullmatch(r".+/-/merge_requests/[1-9][0-9]*/?", parsed[3]) is None:
        raise JiraError("GitLab confirmed the merge but did not return a valid merge request URL.")
    commit_url = f"{web_url.rstrip('/').rsplit('/-/merge_requests/', 1)[0]}/-/commit/{sha}"
    return sha[:8], commit_url


def _jql_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _review_data(
    issue: JiraIssue,
    local_tickets_by_key: dict[str, Ticket],
    gitlab_base_url: str,
    *,
    gitlab_client: GitLabClient | None,
    gitlab_error: str | None = None,
    resolve_merge_requests: bool = True,
) -> dict[str, object]:
    local_ticket = local_tickets_by_key.get(canonicalize_jira_key(issue.key))
    description = issue.description_adf if issue.description_adf is not None else issue.description
    merge_requests = detect_merge_requests(description, gitlab_base_url)
    selection = select_merge_request([])
    if merge_requests and resolve_merge_requests:
        if gitlab_error is not None:
            raise JiraError(gitlab_error)
        merge_request_data, selection = _retrieve_merge_requests(
            description, gitlab_base_url, gitlab_client
        )
    elif merge_requests:
        merge_request_data = [asdict(reference) for reference in merge_requests]
        selection = MergeRequestSelection(
            selected=None,
            enabled=False,
            reason="Merge request state has not been retrieved.",
        )
    else:
        merge_request_data = []
    return {
        "key": issue.key,
        "summary": issue.summary or issue.key,
        "description": issue.description or "",
        "merge_requests": merge_request_data,
        "selected_merge_request": selection.selected,
        "ready_to_merge_enabled": selection.enabled,
        "merge_request_selection_reason": selection.reason,
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
