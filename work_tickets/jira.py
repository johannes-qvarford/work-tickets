from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

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


@dataclass(frozen=True)
class JiraIssueWithSubtasks:
    issue: JiraIssue
    subtasks: tuple[JiraIssue, ...]


@dataclass(frozen=True)
class JiraApiConventions:
    """The REST conventions that differ between Jira Cloud and Jira Server."""

    deployment: str
    api_version: int
    uses_adf_descriptions: bool

    @classmethod
    def from_base_url(cls, base_url: str) -> JiraApiConventions:
        """Select conventions from the common Jira Cloud and Server URL formats.

        Atlassian Cloud site URLs and the ``api.atlassian.com/ex/jira`` gateway
        are unambiguous. A non-root context path is the usual Server/Data
        Center URL format and selects REST v2. Root URLs retain the existing
        v3 behavior for compatibility with installations that do not expose
        enough information in their hostname to identify the deployment.
        """
        parsed = urlsplit(base_url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path.rstrip("/").lower()
        is_cloud_site = hostname.endswith(".atlassian.net")
        is_cloud_gateway = hostname == "api.atlassian.com" and path.startswith("/ex/jira/")
        if is_cloud_site or is_cloud_gateway:
            return cls(deployment="cloud", api_version=3, uses_adf_descriptions=True)
        if path and path != "/rest":
            return cls(deployment="server", api_version=2, uses_adf_descriptions=False)
        return cls(deployment="cloud-compatible", api_version=3, uses_adf_descriptions=True)

    def path(self, resource: str) -> str:
        return f"/rest/api/{self.api_version}/{resource}"

    def description_payload(self, description: str) -> str | dict[str, Any]:
        return self._adf(description) if self.uses_adf_descriptions else description

    @staticmethod
    def _adf(description: str) -> dict[str, Any]:
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


class JiraClient:
    """Small Jira REST client with no dependency on Jira credentials at import time."""

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
        self._conventions = JiraApiConventions.from_base_url(config.base_url)

    def close(self) -> None:
        self._client.close()

    def validate(self) -> JiraValidation:
        """Check project access and the configured issue type."""
        project = self._request_dict(
            "GET", self._api_path(f"project/{quote(self._project_key, safe='')}")
        )
        issue_types = self._request_list("GET", self._api_path("issuetype"))
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
            self._api_path("issue"),
            json={"fields": self._issue_fields(summary, description)},
        )
        key = response.get("key")
        if not isinstance(key, str) or not key:
            raise JiraError("Jira created the issue but did not return an issue key.")
        return self.get_issue(key)

    def create_subtask(self, parent_key: str, summary: str, description: str) -> JiraIssue:
        """Create a Jira subtask beneath an already-created parent issue."""
        fields = self._issue_fields(summary, description)
        # Jira requires the ID of a subtask issue type that is available in the
        # configured project. The display name is not stable (for example,
        # projects may use "Subtask" instead of "Sub-task").
        fields["issuetype"] = {"id": self._get_subtask_issue_type_id()}
        fields["parent"] = {"key": parent_key}
        response = self._request_dict(
            "POST",
            self._api_path("issue"),
            json={"fields": fields},
        )
        key = response.get("key")
        if not isinstance(key, str) or not key:
            raise JiraError("Jira created the subtask but did not return an issue key.")
        return self.get_issue(key)

    def _get_subtask_issue_type_id(self) -> str:
        metadata = self._request_dict(
            "GET",
            self._api_path(f"issue/createmeta/{quote(self._project_key, safe='')}/issuetypes"),
            params={"maxResults": 100},
        )
        issue_types = metadata.get("issueTypes")
        if not isinstance(issue_types, list):
            raise JiraError("Jira returned unexpected issue type metadata for subtasks.")
        for issue_type in issue_types:
            if not isinstance(issue_type, dict) or issue_type.get("subtask") is not True:
                continue
            issue_type_id = issue_type.get("id")
            if isinstance(issue_type_id, str) and issue_type_id:
                return issue_type_id
        raise JiraError(f"Jira has no usable subtask issue type for project {self._project_key}.")

    def update_issue(self, key: str, summary: str, description: str) -> JiraIssue:
        self._request_dict(
            "PUT",
            self._api_path(f"issue/{quote(key, safe='')}"),
            json={
                "fields": {
                    "summary": summary,
                    "description": self._conventions.description_payload(description),
                }
            },
        )
        return self.get_issue(key)

    def delete_issue(self, key: str) -> None:
        """Delete an issue from Jira."""
        self._request(
            "DELETE",
            self._api_path(f"issue/{quote(key, safe='')}"),
            expect_json=False,
        )

    def get_issue(self, key: str) -> JiraIssue:
        response = self._request_dict(
            "GET",
            self._api_path(f"issue/{quote(key, safe='')}"),
            params={"fields": "summary,description,issuetype,status"},
        )
        return self._issue_from_payload(key, response)

    def search_issues(self, jql: str) -> list[JiraIssue]:
        """Return all issues matching a JQL query."""
        issues: list[JiraIssue] = []
        start_at = 0
        while True:
            response = self._request_dict(
                "GET",
                self._api_path("search"),
                params={
                    "jql": jql,
                    "startAt": start_at,
                    "maxResults": 100,
                    "fields": "summary,description,issuetype,status",
                },
            )
            raw_issues = response.get("issues")
            if not isinstance(raw_issues, list):
                raise JiraError("Jira returned an unexpected issue search response.")

            page: list[JiraIssue] = []
            for raw_issue in raw_issues:
                if not isinstance(raw_issue, dict):
                    raise JiraError("Jira returned an invalid issue search result.")
                key = raw_issue.get("key")
                if not isinstance(key, str) or not key:
                    raise JiraError("Jira returned an issue search result without a key.")
                page.append(self._issue_from_payload(key, raw_issue))
            issues.extend(page)

            total = response.get("total")
            if not page or (isinstance(total, int) and len(issues) >= total):
                return issues
            if not isinstance(total, int) and len(page) < 100:
                return issues
            start_at += len(page)

    def get_issue_with_subtasks(self, key: str) -> JiraIssueWithSubtasks:
        """Fetch an issue and the subtasks Jira currently links beneath it."""
        response = self._request_dict(
            "GET",
            self._api_path(f"issue/{quote(key, safe='')}"),
            params={"fields": "summary,description,issuetype,status,subtasks"},
        )
        issue = self._issue_from_payload(key, response)
        fields = response.get("fields")
        if not isinstance(fields, dict) or not isinstance(fields.get("subtasks"), list):
            raise JiraError(f"Jira returned issue {key} without a usable subtask list.")
        raw_subtasks = fields["subtasks"]
        subtasks: list[JiraIssue] = []
        for raw_subtask in raw_subtasks:
            if not isinstance(raw_subtask, dict):
                raise JiraError(f"Jira returned an invalid subtask entry for issue {key}.")
            subtask_key = raw_subtask.get("key")
            if not isinstance(subtask_key, str) or not subtask_key:
                raise JiraError(f"Jira returned a subtask without a key for issue {key}.")
            # The parent response commonly contains only a compact subtask
            # representation. Fetch each issue so inbound sync also gets its
            # description and the current status reliably.
            subtasks.append(self.get_issue(subtask_key))
        return JiraIssueWithSubtasks(issue=issue, subtasks=tuple(subtasks))

    def _issue_from_payload(self, key: str, response: dict[str, Any]) -> JiraIssue:
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
            "description": self._conventions.description_payload(description),
        }

    def _api_path(self, resource: str) -> str:
        return self._conventions.path(resource)

    @classmethod
    def _description_text(cls, description: Any) -> str | None:
        """Convert Jira's description representation into local plain text."""
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

    def _request(
        self,
        method: str,
        path: str,
        *,
        expect_json: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any] | list[Any]:
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

        if not expect_json:
            return {}

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
