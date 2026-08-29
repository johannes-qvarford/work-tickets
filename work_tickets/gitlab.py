from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .models import JiraConfig


class GitLabError(Exception):
    """An expected, user-facing GitLab integration error."""


@dataclass(frozen=True)
class GitLabMergeRequest:
    state: str
    updated_at: str


class GitLabClient:
    """Small GitLab REST client for retrieving merge request state."""

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

    def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
        response = self._request(
            "GET",
            f"{self._api_prefix}/projects/{quote(project_path, safe='')}/merge_requests/{number}",
        )
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
        return GitLabMergeRequest(state=state, updated_at=updated_at)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
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

        try:
            payload = response.json()
        except ValueError as exc:
            raise GitLabError("GitLab returned an invalid JSON response.") from exc
        if not isinstance(payload, dict):
            raise GitLabError("GitLab returned an unexpected merge request response.")
        return payload
