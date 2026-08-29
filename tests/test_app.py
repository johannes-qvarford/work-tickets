import atexit
import json
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

from work_tickets.app import app, parse_jira_issue_reference  # noqa: E402
from work_tickets.jira import (  # noqa: E402
    JiraApiConventions,
    JiraClient,
    JiraError,
    JiraIssue,
    JiraIssueWithSubtasks,
)
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


def test_homepage_is_available_and_legacy_frontend_is_removed() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert '<div id="app"></div>' in response.text
    assert 'type="module"' in response.text
    assert client.get("/legacy").status_code == 404
    assert not list((Path(__file__).parents[1] / "work_tickets" / "templates").glob("*.html"))


def test_spa_state_api_serializes_tickets_categories_and_jira_config() -> None:
    with SessionLocal() as db:
        category = Category(name="API state category")
        ticket = Ticket(summary="API state parent", position=0, category=category)
        Ticket(summary="API state child", position=0, parent=ticket)
        db.add_all([category, ticket])
        db.commit()

    response = client.get("/api/state")

    assert response.status_code == 200
    result = response.json()
    assert {item["name"] for item in result["categories"]} >= {"API state category"}
    parent = next(item for item in result["tickets"] if item["summary"] == "API state parent")
    assert parent["category_name"] == "API state category"
    assert parent["subtasks"][0]["summary"] == "API state child"


def test_packaged_spa_assets_are_served() -> None:
    page = client.get("/")
    asset_dir = Path(__file__).parents[1] / "work_tickets" / "static" / "assets"

    assert page.status_code == 200
    assert "/src/main.ts" not in page.text
    assert asset_dir.exists()
    assert list(asset_dir.glob("index-*.js"))


def test_ticket_and_subtask_api_mutations_persist_state() -> None:
    create_response = client.post(
        "/api/tickets",
        json={
            "summary": "API created ticket",
            "description": "Ticket details",
            "planned_date": "2026-08-24",
        },
    )

    assert create_response.status_code == 200
    create_result = create_response.json()
    assert create_result["ok"] is True
    ticket_id = create_result["created_id"]

    update_response = client.put(
        f"/api/tickets/{ticket_id}",
        json={
            "summary": "API updated ticket",
            "description": "Updated details",
            "planned_date": "2026-08-25",
        },
    )
    subtask_response = client.post(
        f"/api/tickets/{ticket_id}/subtasks",
        json={
            "summary": "API created subtask",
            "description": "Subtask details",
            "planned_date": "2026-08-26",
        },
    )

    assert update_response.status_code == 200
    assert subtask_response.status_code == 200
    subtask_id = subtask_response.json()["state"]["tickets"][0]["subtasks"][0]["id"]

    subtask_update_response = client.put(
        f"/api/subtasks/{subtask_id}",
        json={
            "summary": "API updated subtask",
            "description": "Updated subtask details",
            "planned_date": "2026-08-27",
        },
    )

    assert subtask_update_response.status_code == 200
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        subtask = db.get(Ticket, subtask_id)
        assert ticket is not None
        assert subtask is not None
        assert ticket.summary == "API updated ticket"
        assert ticket.planned_date == date(2026, 8, 25)
        assert subtask.summary == "API updated subtask"
        assert subtask.planned_date == date(2026, 8, 27)


def test_ticket_api_validation_does_not_persist_invalid_values() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="Validation ticket", position=0)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    missing_summary = client.put(
        f"/api/tickets/{ticket_id}",
        json={"summary": " ", "description": "changed", "planned_date": None},
    )
    invalid_date = client.put(
        f"/api/tickets/{ticket_id}",
        json={"summary": "Changed", "description": "changed", "planned_date": "invalid"},
    )

    assert missing_summary.status_code == 422
    assert missing_summary.json() == {"ok": False, "message": "Ticket summary is required."}
    assert invalid_date.status_code == 422
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        assert ticket is not None
        assert ticket.summary == "Validation ticket"


def test_category_api_creates_and_deletes_without_deleting_tickets() -> None:
    create_response = client.post("/api/categories", json={"name": "API category lifecycle"})
    category_id = next(
        item["id"]
        for item in create_response.json()["state"]["categories"]
        if item["name"] == "API category lifecycle"
    )
    with SessionLocal() as db:
        ticket = Ticket(summary="Category ticket", position=0, category_id=category_id)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    delete_response = client.delete(f"/api/categories/{category_id}")

    assert create_response.status_code == 200
    assert delete_response.status_code == 200
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        assert ticket is not None
        assert ticket.category_id is None
        assert db.get(Category, category_id) is None


def test_completion_api_updates_local_priority_and_state() -> None:
    with SessionLocal() as db:
        first = Ticket(summary="Completion first", position=0)
        second = Ticket(summary="Completion second", position=1)
        child = Ticket(summary="Completion child", position=0, parent=second)
        db.add_all([first, second, child])
        db.commit()
        first_id = first.id
        second_id = second.id
        child_id = child.id

    complete_response = client.post(f"/api/tickets/{second_id}/complete")
    child_response = client.post(f"/api/subtasks/{child_id}/complete")

    assert complete_response.status_code == 200
    assert child_response.status_code == 200
    state_ids = [item["id"] for item in complete_response.json()["state"]["tickets"]]
    assert state_ids.index(first_id) < state_ids.index(second_id)
    with SessionLocal() as db:
        second = db.get(Ticket, second_id)
        child = db.get(Ticket, child_id)
        assert second is not None and second.local_completed is True
        assert child is not None and child.local_completed is True


def test_completion_api_rejects_missing_and_wrong_level_items() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Completion validation parent", position=0)
        child = Ticket(summary="Completion validation child", position=0, parent=parent)
        db.add_all([parent, child])
        db.commit()
        child_id = child.id
        parent_id = parent.id

    missing = client.post("/api/tickets/999999/complete")
    child_as_ticket = client.post(f"/api/tickets/{child_id}/complete")
    parent_as_subtask = client.post(f"/api/subtasks/{parent_id}/complete")

    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "message": "Ticket was not found."}
    assert child_as_ticket.status_code == 400
    assert parent_as_subtask.status_code == 400


def test_api_reordering_uses_active_items_and_persists_positions() -> None:
    with SessionLocal() as db:
        first = Ticket(summary="Move first", position=0)
        middle = Ticket(summary="Move middle", position=1)
        done = Ticket(summary="Move done", position=2, local_completed=True)
        parent = Ticket(summary="Move parent", position=3)
        child_first = Ticket(summary="Child first", position=0, parent=parent)
        child_middle = Ticket(summary="Child middle", position=1, parent=parent)
        db.add_all([first, middle, done, parent, child_first, child_middle])
        db.commit()
        first_id = first.id
        middle_id = middle.id
        parent_id = parent.id
        child_first_id = child_first.id
        child_middle_id = child_middle.id

    ticket_response = client.post(f"/api/tickets/{middle_id}/move", params={"target_index": 0})
    subtask_response = client.post(
        f"/api/subtasks/{child_middle_id}/move", params={"target_index": 0}
    )

    assert ticket_response.status_code == 200
    assert subtask_response.status_code == 200
    with SessionLocal() as db:
        tickets = list(
            db.scalars(
                select(Ticket)
                .where(Ticket.parent_id.is_(None), Ticket.id.in_([first_id, middle_id]))
                .order_by(Ticket.position)
            )
        )
        parent = db.get(Ticket, parent_id)
        assert [ticket.id for ticket in tickets] == [middle_id, first_id]
        assert parent is not None
        assert [child.id for child in parent.subtasks] == [child_middle_id, child_first_id]


def test_spa_reordering_adjusts_target_index_after_source_removal() -> None:
    helper_source = (Path(__file__).parents[1] / "frontend" / "src" / "reordering.ts").read_text()

    assert (
        "const sourceIndex = activeItems.findIndex((item) => item.id === sourceId)" in helper_source
    )
    assert (
        "const adjustedTargetIndex = targetIndex > sourceIndex ? targetIndex - 1 : targetIndex"
        in helper_source
    )
    assert "return adjustedTargetIndex + (afterTarget ? 1 : 0)" in helper_source


def test_spa_reordering_uses_drag_handles_without_arrow_controls() -> None:
    frontend_source = Path(__file__).parents[1] / "frontend" / "src"
    app_source = (frontend_source / "App.vue").read_text()
    ticket_card_source = (frontend_source / "components" / "TicketCard.vue").read_text()

    assert 'class="drag-handle ticket-drag-handle"' in ticket_card_source
    assert 'class="drag-handle subtask-drag-handle"' in ticket_card_source
    assert 'icon="pi pi-arrow-up"' not in ticket_card_source
    assert 'icon="pi pi-arrow-down"' not in ticket_card_source
    assert "canMoveUp" not in ticket_card_source
    assert "canMoveDown" not in ticket_card_source
    assert "moveTicketBy" not in ticket_card_source
    assert "moveSubtaskBy" not in ticket_card_source
    assert "@move-ticket=" not in app_source
    assert "canMoveTicket" not in app_source
    assert "Drag active tickets to reorder." in app_source
    assert "arrow controls" not in app_source
    assert "use arrows" not in ticket_card_source


def test_api_reordering_swaps_adjacent_items_in_both_directions() -> None:
    with SessionLocal() as db:
        first = Ticket(summary="Swap first", position=0)
        second = Ticket(summary="Swap second", position=1)
        db.add_all([first, second])
        db.commit()
        first_id = first.id
        second_id = second.id

    move_second_before_first = client.post(
        f"/api/tickets/{second_id}/move", params={"target_index": 0}
    )
    move_first_before_second = client.post(
        f"/api/tickets/{first_id}/move", params={"target_index": 0}
    )
    move_first_after_second = client.post(
        f"/api/tickets/{first_id}/move", params={"target_index": 1}
    )

    assert move_second_before_first.status_code == 200
    assert move_first_before_second.status_code == 200
    assert move_first_after_second.status_code == 200
    assert [
        ticket["id"]
        for ticket in move_second_before_first.json()["state"]["tickets"]
        if ticket["id"] in {first_id, second_id}
    ] == [second_id, first_id]
    assert [
        ticket["id"]
        for ticket in move_first_before_second.json()["state"]["tickets"]
        if ticket["id"] in {first_id, second_id}
    ] == [first_id, second_id]
    assert [
        ticket["id"]
        for ticket in move_first_after_second.json()["state"]["tickets"]
        if ticket["id"] in {first_id, second_id}
    ] == [second_id, first_id]


def test_api_rejects_invalid_reordering_targets() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="Invalid move ticket", position=0)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(f"/api/tickets/{ticket_id}/move", params={"target_index": 99})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "message": "Ticket target position is invalid."}


def test_api_delete_removes_ticket_and_subtasks() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Delete API parent", position=0)
        child = Ticket(summary="Delete API child", position=0, parent=parent)
        db.add_all([parent, child])
        db.commit()
        parent_id = parent.id
        child_id = child.id

    response = client.delete(f"/api/tickets/{parent_id}")

    assert response.status_code == 200
    with SessionLocal() as db:
        assert db.get(Ticket, parent_id) is None
        assert db.get(Ticket, child_id) is None


def test_api_delete_rejects_completed_items() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="Protected API ticket", position=0, local_completed=True)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.delete(f"/api/tickets/{ticket_id}")

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "message": "Done tickets can only be marked active.",
    }


def test_api_saves_jira_config_and_preserves_blank_browser_url() -> None:
    response = client.put(
        "/api/settings/jira",
        json={
            "base_url": "https://api.atlassian.com/ex/jira/cloud-id/",
            "browser_base_url": "",
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "work",
            "issue_type": "Task",
        },
    )

    assert response.status_code == 200
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        assert config.base_url == "https://api.atlassian.com/ex/jira/cloud-id"
        assert config.browser_base_url == ""
        assert config.project_key == "WORK"


def test_api_jira_config_validation_uses_jira_client(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.project_key == "WORK"

        def validate(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    response = client.put(
        "/api/settings/jira",
        json={
            "base_url": "https://jira.example.test",
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "work",
            "issue_type": "Task",
            "validate": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["message"] == "Jira connection validated and saved."


def test_api_imports_jira_issue_and_subtasks_with_local_fields(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.browser_base_url == "https://jira.example.test"
            assert config.project_key == "SCRUM"

        def get_issue_with_subtasks(self, key: str) -> JiraIssueWithSubtasks:
            assert key == "SCRUM-505"
            return JiraIssueWithSubtasks(
                issue=JiraIssue(
                    key=key,
                    summary="Imported API parent",
                    description="Imported details",
                    status_name="In Progress",
                ),
                subtasks=(
                    JiraIssue(
                        key="SCRUM-506",
                        summary="Imported API child",
                        description="Child details",
                        status_name="To Do",
                    ),
                ),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.base_url = "https://jira.example.test"
        config.browser_base_url = "https://jira.example.test"
        config.project_key = "SCRUM"
        category = Category(name="API import category")
        db.add(category)
        db.flush()
        category_id = category.id
        db.commit()

    response = client.post(
        "/api/tickets",
        json={
            "summary": "https://jira.example.test/browse/scrum-505",
            "planned_date": "2026-08-30",
            "category_id": category_id,
            "description": "Ignored local description",
        },
    )

    assert response.status_code == 200
    with SessionLocal() as db:
        parent = db.scalar(select(Ticket).where(Ticket.jira_issue_key == "SCRUM-505"))
        child = db.scalar(select(Ticket).where(Ticket.jira_issue_key == "SCRUM-506"))
        assert parent is not None
        assert child is not None
        assert parent.summary == "Imported API parent"
        assert parent.description == "Imported details"
        assert parent.planned_date == date(2026, 8, 30)
        assert parent.category_id == category_id
        assert parent.jira_status_name == "In Progress"
        assert child.parent_id == parent.id
        assert child.summary == "Imported API child"
        assert child.description == "Child details"
        assert child.position == 0
        assert child.jira_status_name == "To Do"


def test_api_sync_updates_a_ticket_from_jira(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def get_issue_with_subtasks(self, key: str) -> JiraIssueWithSubtasks:
            assert key == "WORK-40"
            return JiraIssueWithSubtasks(
                issue=JiraIssue(
                    key=key,
                    summary="Remote API parent",
                    description="Remote API details",
                    status_name="In Progress",
                ),
                subtasks=(),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.project_key = "WORK"
        ticket = Ticket(summary="Local API parent", position=0, jira_issue_key="WORK-40")
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(f"/api/tickets/{ticket_id}/sync-from-jira")

    assert response.status_code == 200
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        assert ticket is not None
        assert ticket.summary == "Remote API parent"
        assert ticket.description == "Remote API details"


def test_parse_jira_issue_reference_accepts_key_and_configured_browser_url() -> None:
    browser_base_url = "https://jira.example.test"

    assert parse_jira_issue_reference(" scrum-xyz ", browser_base_url) == "SCRUM-XYZ"
    assert (
        parse_jira_issue_reference(
            "https://jira.example.test/browse/scrum-xyz?focusedCommentId=1",
            browser_base_url,
        )
        == "SCRUM-XYZ"
    )


def test_parse_jira_issue_reference_rejects_other_hosts_and_paths() -> None:
    browser_base_url = "https://jira.example.test"

    for reference in (
        "https://other.example.test/browse/WORK-1",
        "https://jira.example.test/issues/WORK-1",
        "not-an-issue-reference",
    ):
        try:
            parse_jira_issue_reference(reference, browser_base_url)
        except JiraError as exc:
            assert str(exc) == "Enter a Jira issue key or a Jira browser URL ending in /browse/KEY."
        else:
            raise AssertionError(f"Expected invalid Jira reference: {reference}")


def test_jira_client_creates_issue_and_refreshes_status() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"key": "WORK-7"})
        return httpx.Response(200, json={"key": "WORK-7", "fields": {"status": {"name": "To Do"}}})

    config = JiraConfig(
        base_url="https://work.atlassian.net",
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
    assert '"project":{"key":"WORK"' in requests[0].read().decode()


def test_jira_api_conventions_identify_cloud_and_server_urls() -> None:
    for url in (
        "https://work.example.atlassian.net",
        "https://api.atlassian.com/ex/jira/cloud-id",
    ):
        conventions = JiraApiConventions.from_base_url(url)
        assert conventions.deployment == "cloud"
        assert conventions.api_version == 3
        assert conventions.uses_adf_descriptions is True
        assert conventions.path("issue") == "/rest/api/3/issue"

    server = JiraApiConventions.from_base_url("https://jira.example.test/jira")
    assert server.deployment == "server"
    assert server.api_version == 2
    assert server.uses_adf_descriptions is False
    assert server.path("issue/WORK-1") == "/rest/api/2/issue/WORK-1"
    compatibility = JiraApiConventions.from_base_url("https://jira.example.test")
    assert compatibility.api_version == 3
    assert compatibility.uses_adf_descriptions is True


def test_jira_client_uses_server_api_v2_and_plain_text_descriptions() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"key": "WORK-10"})
        if request.method == "PUT":
            payload = json.loads(request.content)
            assert payload["fields"]["description"] == "Updated details"
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={
                "key": "WORK-10",
                "fields": {"summary": "Server issue", "description": "Server details"},
            },
        )

    config = JiraConfig(
        base_url="https://jira.example.test/jira",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Task",
    )
    jira = JiraClient(config, transport=httpx.MockTransport(handler))
    created = jira.create_issue("Server issue", "Server details")
    jira.update_issue(created.key, "Server issue", "Updated details")
    jira.close()

    assert [request.url.path for request in requests] == [
        "/jira/rest/api/2/issue",
        "/jira/rest/api/2/issue/WORK-10",
        "/jira/rest/api/2/issue/WORK-10",
        "/jira/rest/api/2/issue/WORK-10",
    ]
    assert json.loads(requests[0].content)["fields"]["description"] == "Server details"


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


def test_jira_client_creates_subtask_and_fetches_parent_subtasks() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/createmeta/WORK/issuetypes"):
            return httpx.Response(
                200,
                json={"issueTypes": [{"id": "10004", "name": "Subtask", "subtask": True}]},
            )
        if request.method == "POST":
            payload = json.loads(request.content)
            if payload["fields"].get("parent") is not None:
                assert payload["fields"]["issuetype"] == {"id": "10004"}
            return httpx.Response(201, json={"key": "WORK-51"})
        if request.url.path.endswith("/WORK-50"):
            return httpx.Response(
                200,
                json={
                    "key": "WORK-50",
                    "fields": {
                        "summary": "Parent",
                        "status": {"name": "Open"},
                        "subtasks": [{"key": "WORK-51"}],
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "key": "WORK-51",
                "fields": {
                    "summary": "Child",
                    "description": "Child details",
                    "status": {"name": "Done"},
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
    created = jira.create_subtask("WORK-50", "Child", "Child details")
    synced = jira.get_issue_with_subtasks("WORK-50")
    jira.close()

    assert created.key == "WORK-51"
    assert synced.issue.summary == "Parent"
    assert synced.subtasks == (
        JiraIssue(key="WORK-51", summary="Child", description="Child details", status_name="Done"),
    )
    assert requests[0].method == "GET"
    assert requests[1].method == "POST"
    post_payload = requests[1].read().decode()
    assert '"issuetype":{"id":"10004"}' in post_payload
    assert '"parent":{"key":"WORK-50"}' in post_payload


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
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.project_key = "WORK"
        ticket = Ticket(summary="Sync this ticket", description="Remote description", position=99)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(f"/api/tickets/{ticket_id}/sync")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    with SessionLocal() as db:
        synced = db.get(Ticket, ticket_id)
        assert synced is not None
        assert synced.jira_issue_key == "WORK-8"
        assert synced.jira_status_name == "Open"
        assert synced.synced_at is not None


def test_sync_ticket_updates_an_existing_jira_issue(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def update_issue(self, key: str, summary: str, description: str) -> JiraIssue:
            calls.append((key, summary, description))
            return JiraIssue(key=key, summary="Remote updated", status_name="In Progress")

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.project_key = "WORK"
        ticket = Ticket(
            summary="Local update",
            description="Local update details",
            position=0,
            jira_issue_key="WORK-8",
        )
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(f"/api/tickets/{ticket_id}/sync")

    assert response.status_code == 200
    assert calls == [("WORK-8", "Local update", "Local update details")]
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        assert ticket is not None
        assert ticket.jira_issue_key == "WORK-8"
        assert ticket.jira_status_name == "In Progress"


def test_sync_parent_creates_and_updates_all_subtasks(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def create_issue(self, summary: str, description: str) -> JiraIssue:
            calls.append(("create", summary, description))
            return JiraIssue(key="WORK-30", summary=summary, status_name="Open")

        def create_subtask(self, parent_key: str, summary: str, description: str) -> JiraIssue:
            assert parent_key == "WORK-30"
            calls.append(("create-subtask", summary, description))
            return JiraIssue(key="WORK-34", summary=summary, status_name="To Do")

        def update_issue(self, key: str, summary: str, description: str) -> JiraIssue:
            calls.append(("update", key, summary))
            return JiraIssue(key=key, summary=summary, status_name="In Progress")

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        parent = Ticket(summary="Parent", description="Parent details", position=300)
        existing = Ticket(
            summary="Existing subtask",
            description="Existing details",
            position=0,
            parent=parent,
            jira_issue_key="WORK-32",
        )
        new = Ticket(summary="New subtask", description="New details", position=1, parent=parent)
        db.add_all([parent, existing, new])
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        existing_id = existing.id
        new_id = new.id

    response = client.post(f"/api/tickets/{parent_id}/sync")

    assert response.status_code == 200
    assert calls == [
        ("create", "Parent", "Parent details"),
        ("update", "WORK-32", "Existing subtask"),
        ("create-subtask", "New subtask", "New details"),
    ]
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        existing = db.get(Ticket, existing_id)
        new = db.get(Ticket, new_id)
        assert parent is not None and parent.jira_issue_key == "WORK-30"
        assert existing is not None and existing.jira_issue_key == "WORK-32"
        assert new is not None and new.jira_issue_key == "WORK-34"
        assert existing.jira_status_name == "In Progress"
        assert new.jira_status_name == "To Do"


def test_sync_parent_partial_failure_keeps_completed_remote_links(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def create_issue(self, summary: str, description: str) -> JiraIssue:
            calls.append(("create-parent", summary))
            return JiraIssue(key="WORK-70", summary=summary, status_name="Open")

        def create_subtask(self, parent_key: str, summary: str, description: str) -> JiraIssue:
            assert parent_key == "WORK-70"
            calls.append(("create-child", summary))
            if summary == "Fails remotely":
                raise JiraError("Jira returned HTTP 503.")
            return JiraIssue(key="WORK-71", summary=summary, status_name="To Do")

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        parent = Ticket(summary="Partial parent", position=304)
        completed = Ticket(
            summary="Completed child",
            position=0,
            parent=parent,
            local_completed=True,
            jira_issue_key="WORK-72",
            jira_status_name="Done",
        )
        failing = Ticket(summary="Fails remotely", position=1, parent=parent)
        db.add_all([parent, completed, failing])
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        completed_id = completed.id
        failing_id = failing.id

    response = client.post(f"/api/tickets/{parent_id}/sync")

    assert response.status_code == 422
    assert "Parent WORK-70 synced, but subtask" in response.json()["message"]
    assert "Retry the parent sync to continue." in response.json()["message"]
    assert calls == [("create-parent", "Partial parent"), ("create-child", "Fails remotely")]
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        completed = db.get(Ticket, completed_id)
        failing = db.get(Ticket, failing_id)
        assert parent is not None and parent.jira_issue_key == "WORK-70"
        assert completed is not None and completed.jira_issue_key == "WORK-72"
        assert completed.local_completed is True
        assert completed.jira_status_name == "Done"
        assert failing is not None and failing.jira_issue_key is None


def test_sync_parent_uses_project_subtask_issue_type(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/createmeta/WORK/issuetypes"):
            return httpx.Response(
                200,
                json={"issueTypes": [{"id": "10004", "name": "Subtask", "subtask": True}]},
            )
        if request.method == "POST":
            fields = json.loads(request.content)["fields"]
            if fields.get("parent") is not None:
                assert fields["issuetype"] == {"id": "10004"}
                assert fields["parent"] == {"key": "WORK-100"}
                return httpx.Response(201, json={"key": "WORK-101"})
            return httpx.Response(201, json={"key": "WORK-100"})
        key = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "key": key,
                "fields": {
                    "summary": "Remote parent" if key == "WORK-100" else "Remote child",
                    "description": {"type": "doc", "version": 1, "content": []},
                    "status": {"name": "To Do"},
                },
            },
        )

    class MockJiraClient(JiraClient):
        def __init__(self, config) -> None:
            super().__init__(config, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("work_tickets.app.JiraClient", MockJiraClient)
    with SessionLocal() as db:
        parent = Ticket(summary="Local parent", description="Parent details", position=0)
        child = Ticket(
            summary="Local child", description="Child details", position=0, parent=parent
        )
        db.add_all([parent, child])
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        child_id = child.id

    response = client.post(f"/api/tickets/{parent_id}/sync")

    assert response.status_code == 200
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        child = db.get(Ticket, child_id)
        assert parent is not None and parent.jira_issue_key == "WORK-100"
        assert child is not None and child.jira_issue_key == "WORK-101"
    assert any(
        request.method == "GET" and request.url.path.endswith("/createmeta/WORK/issuetypes")
        for request in requests
    )


def test_sync_from_jira_updates_parent_and_existing_or_new_subtasks(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def get_issue_with_subtasks(self, key: str) -> JiraIssueWithSubtasks:
            assert key == "WORK-80"
            return JiraIssueWithSubtasks(
                issue=JiraIssue(
                    key=key,
                    summary="Remote parent",
                    description="Remote parent details",
                    status_name="In Progress",
                ),
                subtasks=(
                    JiraIssue(
                        key="WORK-81",
                        summary="Remote existing child",
                        description="Updated child details",
                        status_name="Done",
                    ),
                    JiraIssue(
                        key="WORK-82",
                        summary="Remote missing local child",
                        description="Created from Jira",
                        status_name="To Do",
                    ),
                ),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        category = Category(name="Reconciliation local category")
        parent = Ticket(
            summary="Old parent",
            description="Old parent details",
            planned_date=date(2026, 10, 1),
            position=305,
            jira_issue_key="WORK-80",
            category=category,
        )
        existing = Ticket(
            summary="Old existing child",
            description="Old child details",
            planned_date=date(2026, 10, 2),
            position=4,
            parent=parent,
            local_completed=True,
            jira_issue_key="WORK-81",
        )
        stale = Ticket(
            summary="Stale linked child",
            position=5,
            parent=parent,
            local_completed=True,
            jira_issue_key="WORK-83",
        )
        local_only = Ticket(
            summary="Unlinked local child",
            position=6,
            parent=parent,
            planned_date=date(2026, 10, 3),
            local_completed=True,
        )
        db.add_all([category, parent, existing, stale, local_only])
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        existing_id = existing.id
        stale_id = stale.id
        local_only_id = local_only.id
        category_id = category.id

    response = client.post(f"/api/tickets/{parent_id}/sync-from-jira")

    assert response.status_code == 200
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        existing = db.get(Ticket, existing_id)
        stale = db.get(Ticket, stale_id)
        local_only = db.get(Ticket, local_only_id)
        assert parent is not None
        assert parent.summary == "Remote parent"
        assert parent.description == "Remote parent details"
        assert parent.category_id == category_id
        assert parent.planned_date == date(2026, 10, 1)
        assert existing is not None
        assert existing.summary == "Old existing child"
        assert existing.description == "Old child details"
        assert existing.position == 4
        assert stale is not None and stale.jira_issue_key == "WORK-83"
        assert local_only is not None and local_only.jira_issue_key is None
        created = db.scalar(select(Ticket).where(Ticket.jira_issue_key == "WORK-82"))
        assert created is not None
        assert created.parent_id == parent_id
        assert created.position == 0
        assert created.planned_date is None


def test_sync_from_jira_deletes_linked_children_when_remote_has_none_and_keeps_local_only(
    monkeypatch,
) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def get_issue_with_subtasks(self, key: str) -> JiraIssueWithSubtasks:
            return JiraIssueWithSubtasks(
                issue=JiraIssue(key=key, summary="Remote parent", description="Remote details"),
                subtasks=(),
            )

        def delete_issue(self, key: str) -> None:
            raise AssertionError("Inbound sync must not delete anything from Jira")

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        parent = Ticket(summary="Local parent", jira_issue_key="WORK-85", position=307)
        linked_child = Ticket(
            summary="Removed from Jira",
            parent=parent,
            position=10,
            jira_issue_key="WORK-86",
        )
        local_only_child = Ticket(summary="Keep for outbound sync", parent=parent, position=20)
        db.add_all([parent, linked_child, local_only_child])
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        linked_child_id = linked_child.id
        local_only_child_id = local_only_child.id

    response = client.post(f"/api/tickets/{parent_id}/sync-from-jira")

    assert response.status_code == 200
    with SessionLocal() as db:
        assert db.get(Ticket, linked_child_id) is None
        retained = db.get(Ticket, local_only_child_id)
        assert retained is not None
        assert retained.parent_id == parent_id
        assert retained.jira_issue_key is None
        assert retained.position == 0


def test_sync_from_jira_preserves_local_fields_while_reconciling_active_children(
    monkeypatch,
) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def get_issue_with_subtasks(self, key: str) -> JiraIssueWithSubtasks:
            assert key == "WORK-180"
            return JiraIssueWithSubtasks(
                issue=JiraIssue(
                    key=key,
                    summary="Remote reconciliation parent",
                    description="Remote parent details",
                    status_name="In Progress",
                ),
                subtasks=(
                    JiraIssue(
                        key="WORK-181",
                        summary="Remote active child",
                        description="Remote child details",
                        status_name="To Do",
                    ),
                    JiraIssue(
                        key="WORK-182",
                        summary="Remote new child",
                        description="New child details",
                        status_name="Done",
                    ),
                ),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        category = Category(name="Reconciliation API category")
        parent = Ticket(
            summary="Local reconciliation parent",
            description="Local parent details",
            planned_date=date(2026, 10, 1),
            position=0,
            jira_issue_key="WORK-180",
            category=category,
        )
        existing = Ticket(
            summary="Local active child",
            description="Local child details",
            planned_date=date(2026, 10, 2),
            position=3,
            parent=parent,
            jira_issue_key="WORK-181",
        )
        stale = Ticket(
            summary="Stale active child",
            position=4,
            parent=parent,
            jira_issue_key="WORK-183",
        )
        local_only = Ticket(
            summary="Local-only active child",
            planned_date=date(2026, 10, 3),
            position=5,
            parent=parent,
        )
        db.add_all([category, parent, existing, stale, local_only])
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        existing_id = existing.id
        stale_id = stale.id
        local_only_id = local_only.id
        category_id = category.id

    response = client.post(f"/api/tickets/{parent_id}/sync-from-jira")

    assert response.status_code == 200
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        existing = db.get(Ticket, existing_id)
        stale = db.get(Ticket, stale_id)
        local_only = db.get(Ticket, local_only_id)
        assert parent is not None
        assert parent.summary == "Remote reconciliation parent"
        assert parent.description == "Remote parent details"
        assert parent.category_id == category_id
        assert parent.planned_date == date(2026, 10, 1)
        assert existing is not None
        assert existing.summary == "Remote active child"
        assert existing.description == "Remote child details"
        assert existing.planned_date == date(2026, 10, 2)
        assert existing.jira_status_name == "To Do"
        assert stale is None
        assert local_only is not None
        assert local_only.jira_issue_key is None
        assert local_only.position == 1
        created = db.scalar(select(Ticket).where(Ticket.jira_issue_key == "WORK-182"))
        assert created is not None
        assert created.parent_id == parent_id
        assert created.summary == "Remote new child"
        assert created.position == 2
        assert created.planned_date is None


def test_sync_from_jira_rejects_duplicate_remote_children_without_mutating_local_data(
    monkeypatch,
) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def get_issue_with_subtasks(self, key: str) -> JiraIssueWithSubtasks:
            return JiraIssueWithSubtasks(
                issue=JiraIssue(key=key, summary="Remote parent", description="Remote details"),
                subtasks=(
                    JiraIssue(key="WORK-91", summary="Duplicate one"),
                    JiraIssue(key="WORK-91", summary="Duplicate two"),
                ),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        parent = Ticket(
            summary="Local parent that must survive",
            description="Local details",
            jira_issue_key="WORK-90",
            position=306,
        )
        child = Ticket(summary="Local child that must survive", parent=parent, position=0)
        db.add_all([parent, child])
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        child_id = child.id

    response = client.post(f"/api/tickets/{parent_id}/sync-from-jira")

    assert response.status_code == 422
    assert "duplicate subtask key WORK-91" in response.json()["message"]
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        child = db.get(Ticket, child_id)
        assert parent is not None and parent.summary == "Local parent that must survive"
        assert child is not None and child.summary == "Local child that must survive"


def test_api_subtask_deletion_removes_only_requested_subtask_and_preserves_siblings() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Delete API parent", position=0)
        first = Ticket(summary="First subtask", position=0, parent=parent)
        target = Ticket(summary="Target subtask", position=1, parent=parent)
        remaining = Ticket(summary="Remaining subtask", position=2, parent=parent)
        other_parent = Ticket(summary="Other parent", position=1)
        other_subtask = Ticket(summary="Other subtask", position=0, parent=other_parent)
        db.add_all([parent, first, target, remaining, other_parent, other_subtask])
        db.commit()
        parent_id = parent.id
        target_id = target.id
        other_parent_id = other_parent.id
        other_subtask_id = other_subtask.id

    response = client.delete(f"/api/subtasks/{target_id}")

    assert response.status_code == 200
    assert response.json()["ok"] is True
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


def test_api_subtask_deletion_rejects_missing_and_top_level_items() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Protected parent", position=0)
        subtask = Ticket(summary="Protected subtask", position=0, parent=parent)
        db.add_all([parent, subtask])
        db.commit()
        parent_id = parent.id
        subtask_id = subtask.id

    missing = client.delete("/api/subtasks/999999")
    top_level = client.delete(f"/api/subtasks/{parent_id}")

    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "message": "Subtask was not found."}
    assert top_level.status_code == 400
    assert top_level.json() == {
        "ok": False,
        "message": "Top-level tickets cannot be deleted here.",
    }
    with SessionLocal() as db:
        assert db.get(Ticket, parent_id) is not None
        assert db.get(Ticket, subtask_id) is not None


def test_delete_top_level_ticket_cascades_locally_and_deletes_linked_jira_issue(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def delete_issue(self, key: str) -> None:
            calls.append(key)

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        parent = Ticket(summary="Delete linked parent", position=215, jira_issue_key="WORK-90")
        child = Ticket(
            summary="Delete local child",
            position=0,
            parent=parent,
            jira_issue_key="WORK-91",
        )
        db.add_all([parent, child])
        db.commit()
        parent_id = parent.id
        child_id = child.id

    response = client.delete(f"/api/tickets/{parent_id}")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert calls == ["WORK-90"]
    with SessionLocal() as db:
        assert db.get(Ticket, parent_id) is None
        assert db.get(Ticket, child_id) is None


def test_delete_ticket_keeps_local_cascade_when_jira_delete_fails(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def delete_issue(self, key: str) -> None:
            raise JiraError("Jira returned HTTP 403: Forbidden.")

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        parent = Ticket(
            summary="Delete despite Jira failure", position=216, jira_issue_key="WORK-92"
        )
        child = Ticket(summary="Cascaded child", position=0, parent=parent)
        db.add_all([parent, child])
        db.commit()
        parent_id = parent.id
        child_id = child.id

    response = client.delete(f"/api/tickets/{parent_id}")

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is False
    assert "Delete despite Jira failure" in result["message"]
    assert "linked Jira issue WORK-92 could not be deleted" in result["message"]
    assert "Jira returned HTTP 403: Forbidden" in result["message"]
    with SessionLocal() as db:
        assert db.get(Ticket, parent_id) is None
        assert db.get(Ticket, child_id) is None


def test_delete_linked_subtask_reports_jira_failure_but_removes_local_data(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def delete_issue(self, key: str) -> None:
            raise JiraError("Jira returned HTTP 404.")

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        parent = Ticket(summary="Subtask delete parent", position=217)
        subtask = Ticket(
            summary="Delete despite remote missing",
            position=0,
            parent=parent,
            jira_issue_key="WORK-93",
        )
        db.add_all([parent, subtask])
        db.commit()
        parent_id = parent.id
        subtask_id = subtask.id

    response = client.delete(f"/api/subtasks/{subtask_id}")

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is False
    assert "Subtask Delete despite remote missing deleted locally" in result["message"]
    assert "linked Jira issue WORK-93 could not be deleted" in result["message"]
    with SessionLocal() as db:
        assert db.get(Ticket, subtask_id) is None
        assert db.get(Ticket, parent_id) is not None


def test_api_subtask_validation_does_not_persist_invalid_values() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Validation parent", position=0)
        subtask = Ticket(summary="Keep this subtask", position=0, parent=parent)
        db.add_all([parent, subtask])
        db.commit()
        parent_id = parent.id
        subtask_id = subtask.id

    missing_summary = client.put(
        f"/api/subtasks/{subtask_id}",
        json={"summary": "   ", "description": "changed", "planned_date": "2026-08-25"},
    )
    invalid_date = client.put(
        f"/api/subtasks/{subtask_id}",
        json={"summary": "Updated subtask", "description": "changed", "planned_date": "invalid"},
    )

    assert missing_summary.status_code == 422
    assert missing_summary.json() == {"ok": False, "message": "Subtask summary is required."}
    assert invalid_date.status_code == 422
    with SessionLocal() as db:
        subtask = db.get(Ticket, subtask_id)
        assert subtask is not None
        assert subtask.summary == "Keep this subtask"
        assert subtask.planned_date is None
        assert db.get(Ticket, parent_id) is not None


def test_api_subtask_creation_rejects_missing_and_nested_parents() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Top-level parent", position=0)
        nested_parent = Ticket(summary="Nested parent", position=0, parent=parent)
        db.add_all([parent, nested_parent])
        db.commit()
        parent_id = parent.id
        nested_parent_id = nested_parent.id

    missing_parent = client.post("/api/tickets/999999/subtasks", json={"summary": "Orphan subtask"})
    nested = client.post(
        f"/api/tickets/{nested_parent_id}/subtasks", json={"summary": "Too deeply nested"}
    )

    assert missing_parent.status_code == 404
    assert missing_parent.json() == {"ok": False, "message": "Parent ticket was not found."}
    assert nested.status_code == 400
    assert nested.json() == {
        "ok": False,
        "message": "Subtasks can only be added to top-level tickets.",
    }
    with SessionLocal() as db:
        assert db.scalar(select(Ticket).where(Ticket.parent_id == nested_parent_id)) is None
        assert db.get(Ticket, parent_id) is not None


def test_completion_keeps_done_tickets_below_active_and_uncompletion_prioritizes() -> None:
    with SessionLocal() as db:
        first = Ticket(summary="Completion order first", position=0)
        second = Ticket(summary="Completion order second", position=1)
        already_done = Ticket(summary="Completion order done", position=2, local_completed=True)
        db.add_all([first, second, already_done])
        db.commit()
        first_id = first.id
        second_id = second.id
        done_id = already_done.id

    relevant_ids = {first_id, second_id, done_id}
    state = client.get("/api/state").json()
    assert [ticket["id"] for ticket in state["tickets"] if ticket["id"] in relevant_ids] == [
        first_id,
        second_id,
        done_id,
    ]

    response = client.post(f"/api/tickets/{second_id}/complete")
    assert response.status_code == 200
    assert [
        ticket["id"]
        for ticket in response.json()["state"]["tickets"]
        if ticket["id"] in relevant_ids
    ] == [first_id, done_id, second_id]

    response = client.post(f"/api/tickets/{second_id}/complete")
    assert response.status_code == 200
    assert [
        ticket["id"]
        for ticket in response.json()["state"]["tickets"]
        if ticket["id"] in relevant_ids
    ] == [second_id, first_id, done_id]
    with SessionLocal() as db:
        updated = db.get(Ticket, second_id)
        assert updated is not None
        assert updated.local_completed is False
        assert updated.position == 0


def test_api_move_indices_use_only_unfinished_siblings() -> None:
    with SessionLocal() as db:
        first = Ticket(summary="Move first isolated", position=10)
        target = Ticket(summary="Move target isolated", position=50)
        done = Ticket(summary="Move done isolated", position=1, local_completed=True)
        parent = Ticket(summary="Move child parent isolated", position=10)
        child_first = Ticket(summary="Move child first", position=10, parent=parent)
        child_target = Ticket(summary="Move child target", position=50, parent=parent)
        child_done = Ticket(
            summary="Move child done", position=1, parent=parent, local_completed=True
        )
        db.add_all([first, target, done, parent, child_first, child_target, child_done])
        db.commit()
        first_id = first.id
        target_id = target.id
        done_id = done.id
        parent_id = parent.id
        child_first_id = child_first.id
        child_target_id = child_target.id
        child_done_id = child_done.id

    ticket_response = client.post(f"/api/tickets/{target_id}/move", params={"target_index": 0})
    child_response = client.post(
        f"/api/subtasks/{child_target_id}/move", params={"target_index": 0}
    )

    assert ticket_response.status_code == 200
    assert child_response.status_code == 200
    ticket_state = ticket_response.json()["state"]["tickets"]
    assert [
        ticket["id"] for ticket in ticket_state if ticket["id"] in {first_id, target_id, done_id}
    ] == [target_id, first_id, done_id]
    child_state = next(
        ticket for ticket in child_response.json()["state"]["tickets"] if ticket["id"] == parent_id
    )
    assert [subtask["id"] for subtask in child_state["subtasks"]] == [
        child_target_id,
        child_first_id,
        child_done_id,
    ]
    with SessionLocal() as db:
        active_tickets = list(
            db.scalars(
                select(Ticket)
                .where(Ticket.parent_id.is_(None), Ticket.local_completed.is_(False))
                .order_by(Ticket.position, Ticket.id)
            )
        )
        assert [ticket.position for ticket in active_tickets] == list(range(len(active_tickets)))
        assert db.get(Ticket, done_id).position == 1
        parent = db.get(Ticket, parent_id)
        assert parent is not None
        assert [subtask.id for subtask in parent.subtasks] == [
            child_target_id,
            child_first_id,
            child_done_id,
        ]
        assert [subtask.position for subtask in parent.subtasks[:2]] == [0, 1]
        assert db.get(Ticket, child_done_id).position == 1


def test_api_move_rejects_targets_in_the_done_region() -> None:
    with SessionLocal() as db:
        active_ticket = Ticket(summary="Boundary active ticket", position=0)
        done_ticket = Ticket(summary="Boundary done ticket", position=1, local_completed=True)
        parent = Ticket(summary="Boundary parent", position=2)
        active_subtask = Ticket(summary="Boundary active subtask", position=0, parent=parent)
        done_subtask = Ticket(
            summary="Boundary done subtask", position=1, parent=parent, local_completed=True
        )
        db.add_all([active_ticket, done_ticket, parent, active_subtask, done_subtask])
        db.commit()
        active_ticket_id = active_ticket.id
        active_subtask_id = active_subtask.id

    ticket_response = client.post(
        f"/api/tickets/{active_ticket_id}/move", params={"target_index": 999999}
    )
    subtask_response = client.post(
        f"/api/subtasks/{active_subtask_id}/move", params={"target_index": 1}
    )

    assert ticket_response.status_code == 422
    assert ticket_response.json() == {"ok": False, "message": "Ticket target position is invalid."}
    assert subtask_response.status_code == 422
    assert subtask_response.json() == {
        "ok": False,
        "message": "Subtask target position is invalid.",
    }


def test_subtask_completion_and_locking_use_the_same_priority_rules() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Subtask completion parent", position=0)
        first = Ticket(summary="Subtask active first", position=0, parent=parent)
        second = Ticket(summary="Subtask active second", position=1, parent=parent)
        done = Ticket(summary="Subtask done", position=2, parent=parent, local_completed=True)
        db.add_all([parent, first, second, done])
        db.commit()
        parent_id = parent.id
        first_id = first.id
        second_id = second.id
        done_id = done.id

    response = client.post(f"/api/subtasks/{second_id}/complete")
    assert response.status_code == 200
    subtasks = next(
        ticket["subtasks"]
        for ticket in response.json()["state"]["tickets"]
        if ticket["id"] == parent_id
    )
    assert [
        subtask["id"] for subtask in subtasks if subtask["id"] in {first_id, second_id, done_id}
    ] == [
        first_id,
        done_id,
        second_id,
    ]

    locked_update = client.put(
        f"/api/subtasks/{second_id}",
        json={"summary": "Changed", "description": "", "planned_date": None},
    )
    locked_move = client.post(f"/api/subtasks/{second_id}/move", params={"target_index": 0})
    assert locked_update.status_code == 400
    assert locked_move.status_code == 400

    response = client.post(f"/api/subtasks/{second_id}/complete")
    assert response.status_code == 200
    subtasks = next(
        ticket["subtasks"]
        for ticket in response.json()["state"]["tickets"]
        if ticket["id"] == parent_id
    )
    assert [
        subtask["id"] for subtask in subtasks if subtask["id"] in {first_id, second_id, done_id}
    ] == [
        second_id,
        first_id,
        done_id,
    ]


def test_api_direct_subtask_sync_is_rejected_without_constructing_jira_client(monkeypatch) -> None:
    class UnexpectedJiraClient:
        def __init__(self, config) -> None:
            raise AssertionError("direct subtask sync must not construct a Jira client")

    monkeypatch.setattr("work_tickets.app.JiraClient", UnexpectedJiraClient)
    with SessionLocal() as db:
        parent = Ticket(summary="Sync parent", position=0)
        subtask = Ticket(summary="Sync child", position=0, parent=parent)
        db.add_all([parent, subtask])
        db.commit()
        subtask_id = subtask.id

    for suffix, expected in (
        ("sync", "Only top-level tickets can sync to Jira"),
        ("sync-from-jira", "Only top-level tickets can sync from Jira"),
    ):
        response = client.post(f"/api/tickets/{subtask_id}/{suffix}")
        assert response.status_code == 422
        assert response.json() == {
            "ok": False,
            "message": f"{expected}; sync the parent to include all subtasks.",
        }


def test_sync_from_jira_rejects_completed_ticket_without_mutating_local_data(monkeypatch) -> None:
    class UnexpectedJiraClient:
        def __init__(self, config) -> None:
            raise AssertionError("completed ticket must not construct a Jira client")

    monkeypatch.setattr("work_tickets.app.JiraClient", UnexpectedJiraClient)
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
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
        category_id = category.id

    response = client.post(f"/api/tickets/{ticket_id}/sync-from-jira")

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "Done tickets can only be marked active.",
    }
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        assert ticket is not None
        assert ticket.summary == "Old local summary"
        assert ticket.description == "Old local description"
        assert ticket.jira_status_name == "Open"
        assert ticket.jira_issue_key == "WORK-10"
        assert ticket.category_id == category_id
        assert ticket.planned_date == date(2026, 9, 1)
        assert ticket.local_completed is True
        assert ticket.position == 42


def test_sync_from_jira_requires_a_linked_ticket() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="Local only", position=0)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(f"/api/tickets/{ticket_id}/sync-from-jira")

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "Ticket has not been synced to Jira yet.",
    }


def test_create_ticket_requires_jira_configuration_for_import() -> None:
    with SessionLocal() as db:
        db.query(JiraConfig).delete()
        db.commit()

    response = client.post("/api/tickets", json={"jira_reference": "SCRUM-404"})

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "Jira is not configured. Configure Jira before importing.",
    }

    with SessionLocal() as db:
        db.add(
            JiraConfig(
                id=1,
                base_url="https://jira.example.test",
                browser_base_url="https://jira.example.test",
                email="person@example.test",
                api_token="test-token",
                project_key="WORK",
                issue_type="Task",
            )
        )
        db.commit()


def test_completed_parent_rejects_subtask_creation_in_api() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="No children after done", position=0, local_completed=True)
        db.add(parent)
        db.commit()
        parent_id = parent.id

    response = client.post(
        f"/api/tickets/{parent_id}/subtasks", json={"summary": "Must be rejected"}
    )

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "message": "Done tickets cannot have subtasks added.",
    }
    with SessionLocal() as db:
        assert db.scalar(select(Ticket).where(Ticket.parent_id == parent_id)) is None


def test_active_parent_cannot_delete_completed_subtasks_as_a_cascade() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Parent with protected child", position=0)
        child = Ticket(
            summary="Completed child that must survive",
            position=0,
            parent=parent,
            local_completed=True,
        )
        db.add_all([parent, child])
        db.commit()
        parent_id = parent.id
        child_id = child.id

    response = client.delete(f"/api/tickets/{parent_id}")

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "message": "Tickets with done subtasks can only be deleted after they are active.",
    }
    with SessionLocal() as db:
        assert db.get(Ticket, parent_id) is not None
        assert db.get(Ticket, child_id) is not None


def test_new_active_items_append_before_done_and_normalize_active_positions() -> None:
    with SessionLocal() as db:
        first = Ticket(summary="Existing first", position=10)
        second = Ticket(summary="Existing second", position=50)
        done = Ticket(summary="Existing done", position=2, local_completed=True)
        parent = Ticket(summary="Subtask parent", position=20)
        child_first = Ticket(summary="Child first", position=10, parent=parent)
        child_second = Ticket(summary="Child second", position=50, parent=parent)
        child_done = Ticket(summary="Child done", position=2, parent=parent, local_completed=True)
        db.add_all([first, second, done, parent, child_first, child_second, child_done])
        db.commit()
        parent_id = parent.id
        done_id = done.id
        child_done_id = child_done.id

    ticket_response = client.post("/api/tickets", json={"summary": "New active ticket"})
    subtask_response = client.post(
        f"/api/tickets/{parent_id}/subtasks", json={"summary": "New active child"}
    )

    assert ticket_response.status_code == 200
    assert isinstance(ticket_response.json()["created_id"], int)
    assert subtask_response.status_code == 200
    with SessionLocal() as db:
        tickets = list(
            db.scalars(
                select(Ticket)
                .where(
                    Ticket.parent_id.is_(None),
                    Ticket.summary.in_(
                        ["Existing first", "Existing second", "New active ticket", "Existing done"]
                    ),
                )
                .order_by(Ticket.local_completed, Ticket.position, Ticket.id)
            )
        )
        assert [ticket.summary for ticket in tickets] == [
            "Existing first",
            "Existing second",
            "New active ticket",
            "Existing done",
        ]
        active_tickets = list(
            db.scalars(
                select(Ticket)
                .where(Ticket.parent_id.is_(None), Ticket.local_completed.is_(False))
                .order_by(Ticket.position, Ticket.id)
            )
        )
        assert [ticket.position for ticket in active_tickets] == list(range(len(active_tickets)))
        assert db.get(Ticket, done_id).position == 2
        parent = db.get(Ticket, parent_id)
        assert parent is not None
        assert [subtask.summary for subtask in parent.subtasks] == [
            "Child first",
            "Child second",
            "New active child",
            "Child done",
        ]
        assert [subtask.position for subtask in parent.subtasks[:3]] == [0, 1, 2]
        assert db.get(Ticket, child_done_id).position == 2


def test_completed_items_cannot_sync_from_or_to_jira_in_api(monkeypatch) -> None:
    class UnexpectedJiraClient:
        def __init__(self, config) -> None:
            raise AssertionError("completed items must not construct a Jira client")

    monkeypatch.setattr("work_tickets.app.JiraClient", UnexpectedJiraClient)
    with SessionLocal() as db:
        ticket = Ticket(
            summary="Protected completed ticket",
            position=0,
            local_completed=True,
            jira_issue_key="WORK-300",
        )
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    for suffix in ("sync", "sync-from-jira"):
        response = client.post(f"/api/tickets/{ticket_id}/{suffix}")
        assert response.status_code == 422
        assert response.json()["ok"] is False

    with SessionLocal() as db:
        unchanged = db.get(Ticket, ticket_id)
        assert unchanged is not None
        assert unchanged.summary == "Protected completed ticket"
        assert unchanged.jira_issue_key == "WORK-300"


def test_api_delete_category_uncategorizes_tickets_and_reports_missing_category() -> None:
    with SessionLocal() as db:
        category = Category(name="Category to remove")
        db.add(category)
        db.flush()
        ticket = Ticket(summary="Keep this ticket", position=0, category_id=category.id)
        db.add(ticket)
        db.commit()
        category_id = category.id
        ticket_id = ticket.id

    response = client.delete(f"/api/categories/{category_id}")
    missing = client.delete("/api/categories/999999")

    assert response.status_code == 200
    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "message": "Category was not found."}
    with SessionLocal() as db:
        assert db.get(Category, category_id) is None
        remaining_ticket = db.get(Ticket, ticket_id)
        assert remaining_ticket is not None
        assert remaining_ticket.category_id is None


def test_api_create_subtasks_persists_fields_and_orders_them() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Parent ticket", position=0)
        db.add(parent)
        db.commit()
        parent_id = parent.id

    first_response = client.post(
        f"/api/tickets/{parent_id}/subtasks",
        json={
            "summary": "First subtask",
            "description": "First details",
            "planned_date": "2026-08-24",
        },
    )
    second_response = client.post(
        f"/api/tickets/{parent_id}/subtasks", json={"summary": "Second subtask"}
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        assert parent is not None
        assert [(subtask.summary, subtask.position) for subtask in parent.subtasks] == [
            ("First subtask", 0),
            ("Second subtask", 1),
        ]
        assert parent.subtasks[0].description == "First details"
        assert parent.subtasks[0].planned_date == date(2026, 8, 24)


def test_api_edit_completed_subtask_is_rejected_without_mutation() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Edit parent", position=0)
        subtask = Ticket(
            summary="Original subtask",
            description="Original details",
            planned_date=date(2026, 8, 24),
            position=7,
            parent=parent,
            local_completed=True,
        )
        db.add_all([parent, subtask])
        db.commit()
        parent_id = parent.id
        subtask_id = subtask.id

    response = client.put(
        f"/api/subtasks/{subtask_id}",
        json={
            "summary": "Updated subtask",
            "description": "Updated details",
            "planned_date": "2026-09-03",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "message": "Done subtasks can only be marked active.",
    }
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        subtask = db.get(Ticket, subtask_id)
        assert parent is not None and subtask is not None
        assert subtask.summary == "Original subtask"
        assert subtask.description == "Original details"
        assert subtask.planned_date == date(2026, 8, 24)
        assert subtask.position == 7


def test_api_edit_subtask_rejects_missing_ids_and_top_level_tickets() -> None:
    with SessionLocal() as db:
        top_level = Ticket(summary="Not a subtask to edit", position=0)
        db.add(top_level)
        db.commit()
        top_level_id = top_level.id

    missing = client.put("/api/subtasks/999999", json={"summary": "Missing"})
    top_level_response = client.put(
        f"/api/subtasks/{top_level_id}", json={"summary": "Should not edit"}
    )

    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "message": "Subtask was not found."}
    assert top_level_response.status_code == 400
    assert top_level_response.json() == {
        "ok": False,
        "message": "Top-level tickets cannot be edited here.",
    }


def test_api_create_subtask_validates_summary_and_planned_date() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Validation parent for create", position=0)
        db.add(parent)
        db.commit()
        parent_id = parent.id

    missing_summary = client.post(f"/api/tickets/{parent_id}/subtasks", json={"summary": "   "})
    invalid_date = client.post(
        f"/api/tickets/{parent_id}/subtasks",
        json={"summary": "Bad date", "planned_date": "not-a-date"},
    )

    assert missing_summary.status_code == 422
    assert missing_summary.json() == {"ok": False, "message": "Subtask summary is required."}
    assert invalid_date.status_code == 422
    assert invalid_date.json()["detail"][0]["loc"] == ["body", "planned_date"]
    with SessionLocal() as db:
        assert db.scalar(select(Ticket).where(Ticket.parent_id == parent_id)) is None


def test_api_reordering_rejects_missing_ids_and_wrong_level_items() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Boundary parent for reorder", position=0)
        child = Ticket(summary="Only child", position=5, parent=parent)
        db.add_all([parent, child])
        db.commit()
        parent_id = parent.id
        child_id = child.id

    missing = client.post("/api/subtasks/999999/move", params={"target_index": 0})
    top_level = client.post(f"/api/subtasks/{parent_id}/move", params={"target_index": 0})
    invalid = client.post(f"/api/tickets/{parent_id}/move", params={"target_index": 99})

    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "message": "Subtask was not found."}
    assert top_level.status_code == 400
    assert top_level.json() == {
        "ok": False,
        "message": "Top-level tickets cannot be reordered here.",
    }
    assert invalid.status_code == 422
    assert invalid.json() == {"ok": False, "message": "Ticket target position is invalid."}
    with SessionLocal() as db:
        child = db.get(Ticket, child_id)
        assert child is not None
        assert child.position == 5
