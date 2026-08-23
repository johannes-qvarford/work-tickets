from datetime import date

import httpx
from fastapi.testclient import TestClient

from work_tickets.app import app
from work_tickets.jira import JiraClient, JiraIssue
from work_tickets.models import Base, Category, JiraConfig, SessionLocal, Ticket, engine

Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
client = TestClient(app)


def test_homepage_is_available() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Work tickets" in response.text


def test_create_category_and_ticket() -> None:
    assert (
        client.post("/categories", data={"name": "Planning"}, follow_redirects=False).status_code
        == 303
    )
    response = client.post(
        "/tickets",
        data={"summary": "Prepare agenda", "category_id": "1", "planned_date": "2026-08-23"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Prepare agenda" in client.get("/").text


def test_toggle_completion() -> None:
    response = client.post("/tickets/1/complete", follow_redirects=False)
    assert response.status_code == 303
    assert "Done" in client.get("/").text


def test_jira_client_creates_issue_and_refreshes_status() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"key": "WORK-7"})
        return httpx.Response(200, json={"key": "WORK-7", "fields": {"status": {"name": "To Do"}}})

    config = JiraConfig(
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Task",
    )
    jira = JiraClient(config, transport=httpx.MockTransport(handler))
    issue = jira.create_issue("Prepare agenda", "Bring notes")
    jira.close()

    assert issue == JiraIssue(key="WORK-7", status_name="To Do")
    assert [request.url.path for request in requests] == [
        "/rest/api/3/issue",
        "/rest/api/3/issue/WORK-7",
    ]
    assert requests[0].headers["Authorization"].startswith("Basic ")
    assert requests[0].read().decode().find('"project":{"key":"WORK"') >= 0


def test_jira_client_fetches_remote_fields_and_converts_adf_description() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "key": "WORK-9",
                "fields": {
                    "summary": "Remote summary",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "First paragraph"}],
                            },
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Second paragraph"}],
                            },
                        ],
                    },
                    "issuetype": {"name": "Bug"},
                    "status": {"name": "In Progress"},
                },
            },
        )

    config = JiraConfig(
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Task",
    )
    jira = JiraClient(config, transport=httpx.MockTransport(handler))
    issue = jira.get_issue("WORK-9")
    jira.close()

    assert issue == JiraIssue(
        key="WORK-9",
        summary="Remote summary",
        description="First paragraph\nSecond paragraph",
        issue_type_name="Bug",
        status_name="In Progress",
    )
    assert requests[0].url.params["fields"] == "summary,description,issuetype,status"


def test_sync_ticket_persists_jira_key_and_status(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.project_key == "WORK"

        def create_issue(self, summary: str, description: str) -> JiraIssue:
            assert summary == "Sync this ticket"
            assert description == "Remote description"
            return JiraIssue(key="WORK-8", status_name="Open")

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        db.add(
            JiraConfig(
                id=1,
                base_url="https://jira.example.test",
                email="person@example.test",
                api_token="test-token",
                project_key="WORK",
                issue_type="Task",
            )
        )
        ticket = Ticket(summary="Sync this ticket", description="Remote description", position=99)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(f"/tickets/{ticket_id}/sync", follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        synced = db.get(Ticket, ticket_id)
        assert synced is not None
        assert synced.jira_issue_key == "WORK-8"
        assert synced.jira_status_name == "Open"
        assert synced.synced_at is not None


def test_sync_without_configuration_shows_error() -> None:
    with SessionLocal() as db:
        db.query(JiraConfig).delete()
        ticket = Ticket(summary="Needs Jira", position=100)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(f"/tickets/{ticket_id}/sync", follow_redirects=False)
    assert response.status_code == 303
    assert "Jira%20is%20not%20configured" in response.headers["location"]


def test_sync_from_jira_updates_owned_fields_and_preserves_local_fields(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.project_key == "WORK"

        def get_issue(self, key: str) -> JiraIssue:
            assert key == "WORK-10"
            return JiraIssue(
                key=key,
                summary="Changed in Jira",
                description="Remote description",
                issue_type_name="Bug",
                status_name="Done",
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        if config is None:
            db.add(
                JiraConfig(
                    id=1,
                    base_url="https://jira.example.test",
                    email="person@example.test",
                    api_token="test-token",
                    project_key="WORK",
                    issue_type="Task",
                )
            )
        else:
            config.project_key = "WORK"
        category = Category(name="Inbound local category")
        db.add(category)
        db.flush()
        ticket = Ticket(
            summary="Old local summary",
            description="Old local description",
            planned_date=date(2026, 9, 1),
            position=42,
            local_completed=True,
            jira_issue_key="WORK-10",
            jira_status_name="Open",
            category_id=category.id,
        )
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(f"/tickets/{ticket_id}/sync-from-jira", follow_redirects=False)
    assert response.status_code == 303
    assert "synced%20from%20Jira" in response.headers["location"]
    with SessionLocal() as db:
        synced = db.get(Ticket, ticket_id)
        assert synced is not None
        assert synced.summary == "Changed in Jira"
        assert synced.description == "Remote description"
        assert synced.jira_status_name == "Done"
        assert synced.jira_issue_key == "WORK-10"
        assert synced.category_id == category.id
        assert synced.planned_date == date(2026, 9, 1)
        assert synced.local_completed is True
        assert synced.position == 42
        assert synced.synced_at is not None


def test_sync_from_jira_requires_a_linked_ticket() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="Local only", position=101)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(f"/tickets/{ticket_id}/sync-from-jira", follow_redirects=False)
    assert response.status_code == 303
    assert "Ticket%20has%20not%20been%20synced" in response.headers["location"]


def test_delete_category_uncategorizes_tickets() -> None:
    with SessionLocal() as db:
        category = Category(name="Category to remove")
        db.add(category)
        db.flush()
        ticket = Ticket(summary="Keep this ticket", position=102, category_id=category.id)
        db.add(ticket)
        db.commit()
        category_id = category.id
        ticket_id = ticket.id

    response = client.post(f"/categories/{category_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert "Category%20Category%20to%20remove%20deleted" in response.headers["location"]
    with SessionLocal() as db:
        assert db.get(Category, category_id) is None
        remaining_ticket = db.get(Ticket, ticket_id)
        assert remaining_ticket is not None
        assert remaining_ticket.category_id is None


def test_delete_missing_category_shows_error() -> None:
    response = client.post("/categories/999999/delete", follow_redirects=False)

    assert response.status_code == 303
    assert "Category%20was%20not%20found" in response.headers["location"]
