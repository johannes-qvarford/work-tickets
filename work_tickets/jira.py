from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .models import JiraConfig


class JiraError(Exception):
    """An expected, user-facing Jira integration error."""


@dataclass(frozen=True)
class JiraValidation:
    project_name: str
    issue_type: str


@dataclass(frozen=True)
class JiraIssue:
    key: str
    status_name: str | None


class JiraClient:
    """Small Jira Cloud REST client with no dependency on Jira credentials at import time."""

    def __init__(self, config: JiraConfig, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            auth=httpx.BasicAuth(config.email, config.api_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=20.0,
            transport=transport,
        )
        self._project_key = config.project_key
        self._issue_type = config.issue_type

    def close(self) -> None:
        self._client.close()

    def validate(self) -> JiraValidation:
        """Check authentication, project access, and the configured issue type."""
        myself = self._request_dict("GET", "/rest/api/3/myself")
        project = self._request_dict(
            "GET", f"/rest/api/3/project/{quote(self._project_key, safe='')}"
        )
        issue_types = self._request_list("GET", "/rest/api/3/issuetype")
        available_types = {
            str(item.get("name"))
            for item in issue_types
            if isinstance(item, dict) and item.get("name")
        }
        if self._issue_type not in available_types:
            raise JiraError(f"Jira issue type '{self._issue_type}' was not found for this account.")
        user_name = myself.get("displayName") or myself.get("emailAddress") or "account"
        project_name = str(project.get("name") or self._project_key)
        return JiraValidation(
            project_name=f"{project_name} (connected as {user_name})",
            issue_type=self._issue_type,
        )

    def create_issue(self, summary: str, description: str) -> JiraIssue:
        response = self._request_dict(
            "POST",
            "/rest/api/3/issue",
            json={"fields": self._issue_fields(summary, description)},
        )
        key = response.get("key")
        if not isinstance(key, str) or not key:
            raise JiraError("Jira created the issue but did not return an issue key.")
        return self.get_issue(key)

    def update_issue(self, key: str, summary: str, description: str) -> JiraIssue:
        self._request_dict(
            "PUT",
            f"/rest/api/3/issue/{quote(key, safe='')}",
            json={"fields": {"summary": summary, "description": self._adf(description)}},
        )
        return self.get_issue(key)

    def get_issue(self, key: str) -> JiraIssue:
        response = self._request_dict(
            "GET",
            f"/rest/api/3/issue/{quote(key, safe='')}",
            params={"fields": "status"},
        )
        fields = response.get("fields")
        status_name: str | None = None
        if isinstance(fields, dict):
            status = fields.get("status")
            if isinstance(status, dict) and isinstance(status.get("name"), str):
                status_name = status["name"]
        return JiraIssue(key=key, status_name=status_name)

    def _issue_fields(self, summary: str, description: str) -> dict[str, Any]:
        return {
            "project": {"key": self._project_key},
            "issuetype": {"name": self._issue_type},
            "summary": summary,
            "description": self._adf(description),
        }

    @staticmethod
    def _adf(description: str) -> dict[str, Any]:
        # Jira Cloud API v3 expects Atlassian Document Format for descriptions.
        paragraphs = description.splitlines() or [""]
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": line}],
                }
                for line in paragraphs
            ],
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                body = exc.response.json()
                if isinstance(body, dict):
                    errors = body.get("errorMessages")
                    if isinstance(errors, list) and errors:
                        detail = f": {', '.join(str(error) for error in errors)}"
            except ValueError:
                pass
            raise JiraError(f"Jira returned HTTP {exc.response.status_code}{detail}.") from exc
        except httpx.RequestError as exc:
            raise JiraError(f"Could not reach Jira: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise JiraError("Jira returned an invalid JSON response.") from exc
        if not isinstance(payload, (dict, list)):
            raise JiraError("Jira returned an unexpected response.")
        return payload

    def _request_dict(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        payload = self._request(method, path, **kwargs)
        if not isinstance(payload, dict):
            raise JiraError("Jira returned an unexpected response object.")
        return payload

    def _request_list(self, method: str, path: str, **kwargs: Any) -> list[Any]:
        payload = self._request(method, path, **kwargs)
        if not isinstance(payload, list):
            raise JiraError("Jira returned an unexpected response list.")
        return payload
