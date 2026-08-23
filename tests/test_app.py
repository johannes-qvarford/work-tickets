import atexit
import os
import tempfile
from datetime import date
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

# Tests must never drop or populate the database used by a running development
# server. Select an isolated database before importing the application modules.
_test_db_fd, _test_db_name = tempfile.mkstemp(prefix="work-tickets-test-", suffix=".db")
os.close(_test_db_fd)
_test_db_path = Path(_test_db_name)
os.environ["WORK_TICKETS_DATABASE_URL"] = f"sqlite:///{_test_db_path}"
atexit.register(_test_db_path.unlink, missing_ok=True)

from work_tickets.app import app  # noqa: E402
from work_tickets.jira import JiraClient, JiraIssue  # noqa: E402
from work_tickets.models import (  # noqa: E402
    Base,
    Category,
    JiraConfig,
    SessionLocal,
    Ticket,
    engine,
)

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


def test_toggle_top_level_completion_persists_and_excludes_ticket_from_today() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="Complete locally", planned_date=date.today(), position=2)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(f"/tickets/{ticket_id}/complete", follow_redirects=False)
    assert response.status_code == 303
    assert "Ticket%20Complete%20locally%20marked%20done" in response.headers["location"]
    with SessionLocal() as db:
        completed = db.get(Ticket, ticket_id)
        assert completed is not None
        assert completed.local_completed is True

    page = client.get("/")
    today_section = page.text.split("<h2>Today</h2>", 1)[1].split("</section>", 1)[0]
    assert "Complete locally" not in today_section
    assert f'action="/tickets/{ticket_id}/complete"' in page.text
    assert "Mark as active" in page.text

    response = client.post(f"/tickets/{ticket_id}/complete", follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        active = db.get(Ticket, ticket_id)
        assert active is not None
        assert active.local_completed is False
    page = client.get("/")
    today_section = page.text.split("<h2>Today</h2>", 1)[1].split("</section>", 1)[0]
    assert "Complete locally" in today_section


def test_completion_rejects_missing_and_subtask_targets() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Completion parent", position=3)
        subtask = Ticket(summary="Cannot complete alone", position=0, parent=parent)
        db.add_all([parent, subtask])
        db.commit()
        parent_id = parent.id
        subtask_id = subtask.id

    missing = client.post("/tickets/999999/complete", follow_redirects=False)
    subtask_response = client.post(f"/tickets/{subtask_id}/complete", follow_redirects=False)

    assert missing.status_code == 303
    assert "Ticket%20was%20not%20found" in missing.headers["location"]
    assert subtask_response.status_code == 303
    assert (
        "Only%20top-level%20tickets%20can%20be%20completed%20here"
        in subtask_response.headers["location"]
    )
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        subtask = db.get(Ticket, subtask_id)
        assert parent is not None and parent.local_completed is False
        assert subtask is not None and subtask.local_completed is False


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


def test_jira_validation_does_not_require_myself_scope() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/project/WORK"):
            return httpx.Response(200, json={"name": "Work project"})
        return httpx.Response(200, json=[{"name": "Story"}])

    config = JiraConfig(
        base_url="https://api.atlassian.com/ex/jira/cloud-id",
        email="person@example.test",
        api_token="scoped-token",
        project_key="WORK",
        issue_type="Story",
    )
    jira = JiraClient(config, transport=httpx.MockTransport(handler))
    validation = jira.validate()
    jira.close()

    assert validation.project_name == "Work project"
    assert [request.url.path for request in requests] == [
        "/ex/jira/cloud-id/rest/api/3/project/WORK",
        "/ex/jira/cloud-id/rest/api/3/issuetype",
    ]


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


def test_create_subtasks_in_edit_section_persists_fields_and_orders_them() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Parent ticket", position=200)
        db.add(parent)
        db.commit()
        parent_id = parent.id

    first_response = client.post(
        f"/tickets/{parent_id}/subtasks",
        data={
            "summary": "First subtask",
            "description": "First details",
            "planned_date": "2026-08-24",
        },
        follow_redirects=False,
    )
    second_response = client.post(
        f"/tickets/{parent_id}/subtasks",
        data={"summary": "Second subtask"},
        follow_redirects=False,
    )

    assert first_response.status_code == 303
    assert second_response.status_code == 303
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        assert parent is not None
        assert [(subtask.summary, subtask.position) for subtask in parent.subtasks] == [
            ("First subtask", 0),
            ("Second subtask", 1),
        ]
        assert parent.subtasks[0].parent_id == parent_id
        assert parent.subtasks[0].description == "First details"
        assert parent.subtasks[0].planned_date == date(2026, 8, 24)
        subtask_ids = [subtask.id for subtask in parent.subtasks]

    page = client.get("/")
    assert page.status_code == 200
    assert "Subtasks" in page.text
    assert "First subtask" in page.text
    assert "Second subtask" in page.text
    for subtask_id in subtask_ids:
        assert f'action="/subtasks/{subtask_id}/delete"' in page.text
    assert "Delete this subtask?" in page.text


def test_create_subtask_validates_summary_and_planned_date() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Validation parent", position=201)
        db.add(parent)
        db.commit()
        parent_id = parent.id

    missing_summary = client.post(
        f"/tickets/{parent_id}/subtasks",
        data={"summary": "   "},
        follow_redirects=False,
    )
    invalid_date = client.post(
        f"/tickets/{parent_id}/subtasks",
        data={"summary": "Bad date", "planned_date": "not-a-date"},
        follow_redirects=False,
    )

    assert missing_summary.status_code == 303
    assert "Subtask%20summary%20is%20required" in missing_summary.headers["location"]
    assert invalid_date.status_code == 303
    assert "Subtask%20planned%20date%20is%20invalid" in invalid_date.headers["location"]
    with SessionLocal() as db:
        assert db.scalar(select(Ticket).where(Ticket.parent_id == parent_id)) is None


def test_create_subtask_rejects_missing_and_nested_parents() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Top-level parent", position=202)
        nested_parent = Ticket(summary="Nested parent", position=0, parent=parent)
        db.add_all([parent, nested_parent])
        db.commit()
        parent_id = parent.id
        nested_parent_id = nested_parent.id

    missing_parent = client.post(
        "/tickets/999999/subtasks",
        data={"summary": "Orphan subtask"},
        follow_redirects=False,
    )
    nested = client.post(
        f"/tickets/{nested_parent_id}/subtasks",
        data={"summary": "Too deeply nested"},
        follow_redirects=False,
    )

    assert missing_parent.status_code == 303
    assert "Parent%20ticket%20was%20not%20found" in missing_parent.headers["location"]
    assert nested.status_code == 303
    assert (
        "Subtasks%20can%20only%20be%20added%20to%20top-level%20tickets"
        in nested.headers["location"]
    )
    with SessionLocal() as db:
        assert db.scalar(select(Ticket).where(Ticket.parent_id == nested_parent_id)) is None
        assert db.get(Ticket, parent_id) is not None


def test_delete_subtask_removes_only_the_requested_subtask_and_preserves_order() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Delete parent", position=203)
        first = Ticket(summary="First subtask", position=0, parent=parent)
        target = Ticket(summary="Target subtask", position=1, parent=parent)
        remaining = Ticket(summary="Remaining subtask", position=2, parent=parent)
        other_parent = Ticket(summary="Other parent", position=204)
        other_subtask = Ticket(summary="Other subtask", position=0, parent=other_parent)
        db.add_all([parent, first, target, remaining, other_parent, other_subtask])
        db.commit()
        target_id = target.id
        parent_id = parent.id
        other_parent_id = other_parent.id
        other_subtask_id = other_subtask.id

    response = client.post(f"/subtasks/{target_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert "Subtask%20Target%20subtask%20deleted" in response.headers["location"]
    with SessionLocal() as db:
        assert db.get(Ticket, target_id) is None
        parent = db.get(Ticket, parent_id)
        other_parent = db.get(Ticket, other_parent_id)
        assert parent is not None
        assert other_parent is not None
        assert [(subtask.summary, subtask.position) for subtask in parent.subtasks] == [
            ("First subtask", 0),
            ("Remaining subtask", 2),
        ]
        assert db.get(Ticket, other_subtask_id) is not None


def test_delete_subtask_rejects_missing_ids_and_top_level_tickets() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Protected parent", position=205)
        subtask = Ticket(summary="Protected subtask", position=0, parent=parent)
        db.add_all([parent, subtask])
        db.commit()
        parent_id = parent.id
        subtask_id = subtask.id

    missing = client.post("/subtasks/999999/delete", follow_redirects=False)
    top_level = client.post(f"/subtasks/{parent_id}/delete", follow_redirects=False)

    assert missing.status_code == 303
    assert "Subtask%20was%20not%20found" in missing.headers["location"]
    assert top_level.status_code == 303
    assert "Top-level%20tickets%20cannot%20be%20deleted%20here" in top_level.headers["location"]
    with SessionLocal() as db:
        assert db.get(Ticket, parent_id) is not None
        assert db.get(Ticket, subtask_id) is not None


def test_move_subtasks_reorders_only_siblings_and_normalizes_positions() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Ordering parent", position=206)
        first = Ticket(summary="First", position=20, parent=parent)
        middle = Ticket(summary="Middle", position=20, parent=parent)
        last = Ticket(summary="Last", position=80, parent=parent)
        other_parent = Ticket(summary="Other ordering parent", position=207)
        other = Ticket(summary="Other child", position=0, parent=other_parent)
        db.add_all([parent, first, middle, last, other_parent, other])
        db.commit()
        parent_id = parent.id
        first_id = first.id
        middle_id = middle.id
        last_id = last.id
        other_parent_id = other_parent.id
        other_id = other.id

    page = client.get("/")
    assert page.status_code == 200
    assert "Move up" in page.text
    assert "Move down" in page.text
    assert "/subtasks/${match[1]}/move-up" in page.text
    assert "/subtasks/${match[1]}/move-down" in page.text

    move_middle_up = client.post(f"/subtasks/{middle_id}/move-up", follow_redirects=False)
    assert move_middle_up.status_code == 303
    assert "Subtask%20Middle%20moved%20up" in move_middle_up.headers["location"]
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        assert parent is not None
        assert [(subtask.id, subtask.position) for subtask in parent.subtasks] == [
            (middle_id, 0),
            (first_id, 1),
            (last_id, 2),
        ]

    move_middle_down = client.post(f"/subtasks/{middle_id}/move-down", follow_redirects=False)
    assert move_middle_down.status_code == 303
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        other_parent = db.get(Ticket, other_parent_id)
        assert parent is not None
        assert other_parent is not None
        assert [(subtask.id, subtask.position) for subtask in parent.subtasks] == [
            (first_id, 0),
            (middle_id, 1),
            (last_id, 2),
        ]
        assert [(subtask.id, subtask.position) for subtask in other_parent.subtasks] == [
            (other_id, 0)
        ]


def test_move_subtask_handles_boundaries_and_rejects_invalid_targets() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Boundary parent", position=208)
        only = Ticket(summary="Only child", position=5, parent=parent)
        db.add_all([parent, only])
        db.commit()
        parent_id = parent.id
        only_id = only.id

    at_top = client.post(f"/subtasks/{only_id}/move-up", follow_redirects=False)
    assert at_top.status_code == 303
    assert "already%20at%20the%20top" in at_top.headers["location"]
    at_bottom = client.post(f"/subtasks/{only_id}/move-down", follow_redirects=False)
    assert at_bottom.status_code == 303
    assert "already%20at%20the%20bottom" in at_bottom.headers["location"]

    missing = client.post("/subtasks/999999/move-up", follow_redirects=False)
    top_level = client.post(f"/subtasks/{parent_id}/move-down", follow_redirects=False)
    assert "Subtask%20was%20not%20found" in missing.headers["location"]
    assert "Top-level%20tickets%20cannot%20be%20reordered" in top_level.headers["location"]
    with SessionLocal() as db:
        child = db.get(Ticket, only_id)
        assert child is not None
        assert child.position == 0


def test_toggle_subtask_completion_persists_without_changing_parent_or_jira_fields() -> None:
    with SessionLocal() as db:
        parent = Ticket(
            summary="Completion parent",
            description="Parent details",
            position=209,
            local_completed=False,
            jira_issue_key="WORK-20",
            jira_status_name="In Progress",
        )
        subtask = Ticket(
            summary="Complete this subtask",
            description="Subtask details",
            position=0,
            parent=parent,
            local_completed=False,
            jira_issue_key="WORK-21",
            jira_status_name="To Do",
        )
        db.add_all([parent, subtask])
        db.commit()
        parent_id = parent.id
        subtask_id = subtask.id

    response = client.post(f"/subtasks/{subtask_id}/complete", follow_redirects=False)

    assert response.status_code == 303
    assert "Subtask%20Complete%20this%20subtask%20marked%20done" in response.headers["location"]
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        subtask = db.get(Ticket, subtask_id)
        assert parent is not None
        assert subtask is not None
        assert parent.local_completed is False
        assert parent.jira_issue_key == "WORK-20"
        assert parent.jira_status_name == "In Progress"
        assert subtask.local_completed is True
        assert subtask.jira_issue_key == "WORK-21"
        assert subtask.jira_status_name == "To Do"

    page = client.get("/")
    assert f'action="/subtasks/{subtask_id}/complete"' in page.text
    assert "Complete this subtask" in page.text
    assert "Subtask marked done" not in page.text
    assert "Mark as active" in page.text

    response = client.post(f"/subtasks/{subtask_id}/complete", follow_redirects=False)

    assert response.status_code == 303
    assert "Subtask%20Complete%20this%20subtask%20marked%20active" in response.headers["location"]
    with SessionLocal() as db:
        subtask = db.get(Ticket, subtask_id)
        assert subtask is not None
        assert subtask.local_completed is False


def test_subtask_completion_rejects_missing_ids_and_top_level_tickets() -> None:
    with SessionLocal() as db:
        top_level = Ticket(summary="Not a subtask", position=210)
        db.add(top_level)
        db.commit()
        top_level_id = top_level.id

    missing = client.post("/subtasks/999999/complete", follow_redirects=False)
    top_level_response = client.post(f"/subtasks/{top_level_id}/complete", follow_redirects=False)

    assert missing.status_code == 303
    assert "Subtask%20was%20not%20found" in missing.headers["location"]
    assert top_level_response.status_code == 303
    assert (
        "Top-level%20tickets%20cannot%20be%20completed%20here"
        in top_level_response.headers["location"]
    )
    with SessionLocal() as db:
        top_level = db.get(Ticket, top_level_id)
        assert top_level is not None
        assert top_level.local_completed is False
