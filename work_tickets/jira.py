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
    summary: str | None = None
    description: str | None = None
    issue_type_name: str | None = None
    status_name: str | None = None


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
        """Check project access and the configured issue type."""
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
        project_name = str(project.get("name") or self._project_key)
        return JiraValidation(
            project_name=project_name,
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
            params={"fields": "summary,description,issuetype,status"},
        )
        fields = response.get("fields")
        summary: str | None = None
        description: str | None = None
        issue_type_name: str | None = None
        status_name: str | None = None
        if isinstance(fields, dict):
            if isinstance(fields.get("summary"), str):
                summary = fields["summary"]
            description = self._description_text(fields.get("description"))
            issue_type = fields.get("issuetype")
            if isinstance(issue_type, dict) and isinstance(issue_type.get("name"), str):
                issue_type_name = issue_type["name"]
            status = fields.get("status")
            if isinstance(status, dict) and isinstance(status.get("name"), str):
                status_name = status["name"]
        return JiraIssue(
            key=key,
            summary=summary,
            description=description,
            issue_type_name=issue_type_name,
            status_name=status_name,
        )

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

    @classmethod
    def _description_text(cls, description: Any) -> str | None:
        """Convert Jira Cloud's ADF description into the local plain-text form."""
        if description is None:
            return None
        if isinstance(description, str):
            return description
        if not isinstance(description, dict):
            return None

        description_type = description.get("type")
        if description_type == "text":
            text = description.get("text")
            return text if isinstance(text, str) else ""
        if description_type == "hardBreak":
            return "\n"

        content = description.get("content")
        if not isinstance(content, list):
            return ""
        children = [cls._description_text(item) or "" for item in content]
        if description_type in {
            "blockquote",
            "bulletList",
            "codeBlock",
            "doc",
            "heading",
            "listItem",
            "orderedList",
            "paragraph",
        }:
            return "\n".join(children)
        return "".join(children)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = ""
            login_reason = exc.response.headers.get("X-Seraph-LoginReason")
            if login_reason == "AUTHENTICATION_DENIED":
                detail = (
                    ": Jira denied authentication (CAPTCHA may be active; sign in to Jira "
                    "in a browser first)"
                )
            try:
                body = exc.response.json()
                if isinstance(body, dict):
                    errors = body.get("errorMessages")
                    if isinstance(errors, list) and errors and not detail:
                        detail = f": {', '.join(str(error) for error in errors)}"
                    message = body.get("message")
                    if isinstance(message, str) and message and not detail:
                        detail = f": {message}"
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
