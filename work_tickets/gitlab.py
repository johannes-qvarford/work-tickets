from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .models import JiraConfig

_GIT_SHA = re.compile(r"[0-9a-fA-F]{7,40}\Z")


class GitLabError(Exception):
    """An expected, user-facing GitLab integration error."""


@dataclass(frozen=True)
class GitLabMergeRequest:
    state: str
    updated_at: str
    draft: bool = False
    merge_commit_sha: str | None = None
    web_url: str | None = None


@dataclass(frozen=True)
class GitLabMergeRequestApprovalState:
    approved: bool


@dataclass(frozen=True)
class GitLabMergeRequestDiscussionNote:
    id: int
    resolvable: bool
    resolved: bool
    body: str | None = None


@dataclass(frozen=True)
class GitLabMergeRequestDiscussion:
    id: str
    notes: tuple[GitLabMergeRequestDiscussionNote, ...]


class GitLabClient:
    """Small GitLab REST client for merge request review operations."""

    def __init__(self, config: JiraConfig, transport: httpx.BaseTransport | None = None) -> None:
        parsed = urlsplit(config.gitlab_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise GitLabError("GitLab is not configured with a valid base URL.")
        base_path = parsed.path.rstrip("/")
        self._api_prefix = f"{base_path}/api/v4" if base_path else "/api/v4"
        self._client = httpx.Client(
            base_url=f"{parsed.scheme}://{parsed.netloc}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "PRIVATE-TOKEN": config.gitlab_token or "",
            },
            timeout=20.0,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def validate(self) -> None:
        """Check authenticated access without changing GitLab state."""
        self._request("GET", f"{self._api_prefix}/user")

    def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
        response = self._request(
            "GET",
            f"{self._api_prefix}/projects/{quote(project_path, safe='')}/merge_requests/{number}",
        )
        return self._merge_request_from_payload(response, project_path, number)

    def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
        """Merge an MR with squash enabled and validate GitLab's response."""
        response = self._request(
            "POST",
            f"{self._api_prefix}/projects/{quote(project_path, safe='')}/merge_requests/"
            f"{number}/merge",
            json={"squash": True},
        )
        return self._merge_request_from_payload(response, project_path, number)

    @staticmethod
    def _merge_request_from_payload(
        response: dict[str, Any], project_path: str, number: int
    ) -> GitLabMergeRequest:
        state = response.get("state")
        updated_at = response.get("updated_at")
        if not isinstance(state, str) or not state:
            raise GitLabError(
                f"GitLab returned merge request {project_path}!{number} without a valid state."
            )
        if not isinstance(updated_at, str) or not updated_at:
            raise GitLabError(
                f"GitLab returned merge request {project_path}!{number} without a valid updated_at."
            )
        draft = response.get("draft")
        if not isinstance(draft, bool):
            raise GitLabError(
                f"GitLab returned merge request {project_path}!{number} without a valid "
                "draft state."
            )
        squash_commit_sha = response.get("squash_commit_sha")
        merge_commit_sha = response.get("merge_commit_sha")
        if squash_commit_sha is not None and (
            not isinstance(squash_commit_sha, str) or _GIT_SHA.fullmatch(squash_commit_sha) is None
        ):
            raise GitLabError(
                f"GitLab returned merge request {project_path}!{number} with an invalid "
                "squash commit SHA."
            )
        if merge_commit_sha is not None and not isinstance(merge_commit_sha, str):
            raise GitLabError(
                f"GitLab returned merge request {project_path}!{number} with an invalid "
                "merge commit SHA."
            )
        web_url = response.get("web_url")
        if web_url is not None and not isinstance(web_url, str):
            raise GitLabError(
                f"GitLab returned merge request {project_path}!{number} with an invalid web URL."
            )
        return GitLabMergeRequest(
            state=state,
            updated_at=updated_at,
            draft=draft,
            merge_commit_sha=(
                squash_commit_sha if squash_commit_sha is not None else merge_commit_sha
            ),
            web_url=web_url,
        )

    def get_merge_request_approval_state(
        self, project_path: str, number: int
    ) -> GitLabMergeRequestApprovalState:
        response = self._request(
            "GET",
            f"{self._api_prefix}/projects/{quote(project_path, safe='')}/merge_requests/"
            f"{number}/approvals",
        )
        approved = response.get("approved")
        if not isinstance(approved, bool):
            raise GitLabError(
                f"GitLab returned merge request {project_path}!{number} without a valid "
                "approval state."
            )
        return GitLabMergeRequestApprovalState(approved=approved)

    def approve_merge_request(self, project_path: str, number: int) -> None:
        self._request(
            "POST",
            f"{self._api_prefix}/projects/{quote(project_path, safe='')}/merge_requests/"
            f"{number}/approve",
            expect_json=False,
        )

    def mark_merge_request_ready(self, project_path: str, number: int) -> None:
        """Remove the draft flag using GitLab's ``/ready`` quick action."""
        self._request(
            "POST",
            f"{self._api_prefix}/projects/{quote(project_path, safe='')}/merge_requests/"
            f"{number}/notes",
            json={"body": "/ready"},
            expect_json=False,
        )

    def get_merge_request_discussions(
        self, project_path: str, number: int
    ) -> list[GitLabMergeRequestDiscussion]:
        """Return all discussions on a merge request, following GitLab pagination."""
        discussions: list[GitLabMergeRequestDiscussion] = []
        page = 1
        while True:
            payload, headers = self._request_list(
                "GET",
                f"{self._api_prefix}/projects/{quote(project_path, safe='')}/merge_requests/"
                f"{number}/discussions",
                params={"page": page, "per_page": 100},
            )
            for item in payload:
                if not isinstance(item, dict):
                    raise GitLabError("GitLab returned an invalid merge request discussion.")
                discussions.append(self._discussion_from_payload(item))

            next_page = headers.get("X-Next-Page", "").strip()
            if not next_page:
                return discussions
            try:
                next_page_number = int(next_page)
            except ValueError as exc:
                raise GitLabError(
                    "GitLab returned an invalid merge request discussion pagination response."
                ) from exc
            if next_page_number <= page:
                raise GitLabError(
                    "GitLab returned an invalid merge request discussion pagination response."
                )
            page = next_page_number

    def add_merge_request_discussion_note(
        self, project_path: str, number: int, discussion_id: str, body: str
    ) -> None:
        """Add a note to an existing merge request discussion."""
        response = self._request(
            "POST",
            f"{self._api_prefix}/projects/{quote(project_path, safe='')}/merge_requests/"
            f"{number}/discussions/{quote(discussion_id, safe='')}/notes",
            json={"body": body},
        )
        if not _is_positive_integer(response.get("id")):
            raise GitLabError("GitLab returned an invalid merge request discussion comment.")

    def resolve_merge_request_discussion(
        self, project_path: str, number: int, discussion_id: str
    ) -> None:
        """Resolve an existing merge request discussion."""
        response = self._request(
            "PUT",
            f"{self._api_prefix}/projects/{quote(project_path, safe='')}/merge_requests/"
            f"{number}/discussions/{quote(discussion_id, safe='')}",
            json={"resolved": True},
        )
        resolved_discussion = self._discussion_from_payload(response)
        if resolved_discussion.id != discussion_id or any(
            note.resolvable and not note.resolved for note in resolved_discussion.notes
        ):
            raise GitLabError("GitLab did not resolve the merge request discussion.")

    @staticmethod
    def _discussion_from_payload(payload: dict[str, Any]) -> GitLabMergeRequestDiscussion:
        discussion_id = payload.get("id")
        raw_notes = payload.get("notes")
        if not isinstance(discussion_id, str) or not discussion_id:
            raise GitLabError("GitLab returned a discussion without a valid id.")
        if not isinstance(raw_notes, list):
            raise GitLabError(
                f"GitLab returned discussion {discussion_id} without a valid note list."
            )

        notes: list[GitLabMergeRequestDiscussionNote] = []
        for raw_note in raw_notes:
            if not isinstance(raw_note, dict):
                raise GitLabError(f"GitLab returned an invalid note in discussion {discussion_id}.")
            note_id = raw_note.get("id")
            resolvable = raw_note.get("resolvable")
            resolved = raw_note.get("resolved")
            body = raw_note.get("body")
            if (
                not _is_positive_integer(note_id)
                or not isinstance(resolvable, bool)
                or not isinstance(resolved, bool)
                or (body is not None and not isinstance(body, str))
            ):
                raise GitLabError(f"GitLab returned an invalid note in discussion {discussion_id}.")
            assert isinstance(note_id, int)
            notes.append(
                GitLabMergeRequestDiscussionNote(
                    id=note_id,
                    resolvable=resolvable,
                    resolved=resolved,
                    body=body,
                )
            )
        return GitLabMergeRequestDiscussion(id=discussion_id, notes=tuple(notes))

    def _request_list(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[list[Any], httpx.Headers]:
        response = self._request_response(method, path, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitLabError("GitLab returned an invalid JSON response.") from exc
        if not isinstance(payload, list):
            raise GitLabError("GitLab returned an unexpected merge request discussions response.")
        return payload, response.headers

    def _request(
        self, method: str, path: str, *, expect_json: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        response = self._request_response(method, path, **kwargs)
        if not expect_json:
            return {}

        try:
            payload = response.json()
        except ValueError as exc:
            raise GitLabError("GitLab returned an invalid JSON response.") from exc
        if not isinstance(payload, dict):
            raise GitLabError("GitLab returned an unexpected merge request response.")
        return payload

    def _request_response(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                body = exc.response.json()
                if isinstance(body, dict):
                    message = body.get("message")
                    if isinstance(message, str) and message:
                        detail = f": {message}"
            except ValueError:
                pass
            raise GitLabError(f"GitLab returned HTTP {exc.response.status_code}{detail}.") from exc
        except httpx.RequestError as exc:
            raise GitLabError(f"Could not reach GitLab: {exc}") from exc
        return response


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
