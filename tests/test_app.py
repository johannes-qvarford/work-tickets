import asyncio
import atexit
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

# Tests must never drop or populate the database used by a running development
# server. Select an isolated database before importing the application modules.
_test_db_fd, _test_db_name = tempfile.mkstemp(prefix="work-tickets-test-", suffix=".db")
os.close(_test_db_fd)
_test_db_path = Path(_test_db_name)
os.environ["WORK_TICKETS_DATABASE_URL"] = f"sqlite:///{_test_db_path}"
atexit.register(_test_db_path.unlink, missing_ok=True)

from work_tickets import refine  # noqa: E402
from work_tickets.app import app, parse_jira_issue_reference  # noqa: E402
from work_tickets.db_migrations import apply_migrations  # noqa: E402
from work_tickets.gitlab import (  # noqa: E402
    GitLabClient,
    GitLabError,
    GitLabMergeRequest,
    GitLabMergeRequestApprovalState,
    GitLabMergeRequestDiscussion,
    GitLabMergeRequestDiscussionNote,
)
from work_tickets.jira import (  # noqa: E402
    JiraApiConventions,
    JiraClient,
    JiraError,
    JiraIssue,
    JiraIssueWithSubtasks,
)
from work_tickets.jira_service import (  # noqa: E402
    MergeRequestReference,
    MergeRequestSelection,
    _add_jira_comment_if_missing,
    _merge_selected_merge_request,
    _merged_commit_link,
    _resolve_selected_merge_request_discussions,
    _transition_review_status,
    _wait_for_merge,
    canonicalize_jira_key,
    detect_merge_requests,
    parse_gitlab_base_url,
    ready_to_merge_review,
    save_jira_issue,
    select_merge_request,
    transition_jira_issue,
)
from work_tickets.models import (  # noqa: E402
    Category,
    CategoryComponent,
    Component,
    JiraConfig,
    SessionLocal,
    Ticket,
    engine,
)

apply_migrations(engine)
client = TestClient(app)


def _seed_jira_config(db, **overrides: object) -> JiraConfig:
    defaults = {
        "base_url": "https://jira.example.test",
        "browser_base_url": "",
        "email": "person@example.test",
        "api_token": "test-token",
        "project_key": "WORK",
        "issue_type": "Task",
        "completed_statuses": "Done",
        "in_review_status": "Awaiting Review",
        "ready_to_merge_status": "Ready to Merge",
        "ready_to_deploy_status": "Ready to Deploy",
        "gitlab_base_url": "https://gitlab.example",
        "gitlab_token": "gitlab-token",
    }
    defaults.update(overrides)
    config = db.get(JiraConfig, 1)
    if config is None:
        config = JiraConfig(id=1, **defaults)
        db.add(config)
    else:
        for name, value in defaults.items():
            setattr(config, name, value)
    db.flush()
    return config


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
        ticket = Ticket(
            summary="API state parent", notes="Parent notes", position=0, category=category
        )
        Ticket(summary="API state child", position=0, parent=ticket)
        db.add_all([category, ticket])
        db.commit()

    response = client.get("/api/state")

    assert response.status_code == 200
    result = response.json()
    assert {item["name"] for item in result["categories"]} >= {"API state category"}
    parent = next(item for item in result["tickets"] if item["summary"] == "API state parent")
    assert parent["category_name"] == "API state category"
    assert parent["notes"] == "Parent notes"
    assert parent["subtasks"][0]["summary"] == "API state child"
    assert "notes" not in parent["subtasks"][0]


def test_spa_state_api_serializes_local_projects_directory(tmp_path) -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        if config is None:
            config = JiraConfig(
                id=1,
                base_url="https://api.example.test",
                browser_base_url="",
                email="person@example.test",
                api_token="test-token",
                project_key="WORK",
                issue_type="Task",
                completed_statuses="Done",
            )
            db.add(config)
        config.local_projects_directory = str(tmp_path)
        db.commit()

    response = client.get("/api/state")

    assert response.status_code == 200
    assert response.json()["jira_config"]["local_projects_directory"] == str(tmp_path)


def test_packaged_spa_assets_are_served() -> None:
    page = client.get("/")
    asset_dir = Path(__file__).parents[1] / "work_tickets" / "static" / "assets"

    assert page.status_code == 200
    assert "/src/main.ts" not in page.text
    assert asset_dir.exists()
    assert list(asset_dir.glob("index-*.js"))


def test_ticket_and_subtask_api_mutations_persist_state() -> None:
    with SessionLocal() as db:
        category = Category(name="API update category")
        db.add(category)
        db.commit()
        category_id = category.id

    create_response = client.post(
        "/api/tickets",
        json={
            "summary": "API created ticket",
            "description": "Ticket details",
            "notes": "Initial local notes",
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
            "notes": "Updated local notes",
            "planned_date": "2026-08-25",
            "category_id": category_id,
        },
    )
    subtask_response = client.post(
        f"/api/tickets/{ticket_id}/subtasks",
        json={
            "summary": "API created subtask",
            "description": "Subtask details",
            "notes": "Must not be stored on a subtask",
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
            "notes": "Must still not be stored on a subtask",
            "planned_date": "2026-08-27",
        },
    )
    wrong_route_response = client.put(
        f"/api/tickets/{subtask_id}",
        json={
            "summary": "Must remain unchanged",
            "notes": "Must not be stored on a subtask",
        },
    )

    assert subtask_update_response.status_code == 200
    assert wrong_route_response.status_code == 400
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        subtask = db.get(Ticket, subtask_id)
        assert ticket is not None
        assert subtask is not None
        assert ticket.summary == "API updated ticket"
        assert ticket.notes == "Updated local notes"
        assert ticket.planned_date == date(2026, 8, 25)
        assert ticket.category_id == category_id
        assert subtask.summary == "API updated subtask"
        assert subtask.notes == ""
        assert subtask.planned_date == date(2026, 8, 27)
    assert "notes" in update_response.json()["state"]["tickets"][0]
    assert "notes" not in subtask_response.json()["state"]["tickets"][0]["subtasks"][0]


def test_subtask_creation_works_for_new_and_existing_tickets_with_migrated_schema() -> None:
    new_ticket_response = client.post(
        "/api/tickets",
        json={"summary": "Ticket with creation-flow subtask"},
    )
    assert new_ticket_response.status_code == 200
    new_ticket_id = new_ticket_response.json()["created_id"]

    creation_flow_response = client.post(
        f"/api/tickets/{new_ticket_id}/subtasks",
        json={"summary": "Creation-flow subtask"},
    )

    with SessionLocal() as db:
        existing_ticket = Ticket(summary="Existing ticket", position=0)
        db.add(existing_ticket)
        db.commit()
        existing_ticket_id = existing_ticket.id

    edit_flow_response = client.post(
        f"/api/tickets/{existing_ticket_id}/subtasks",
        json={"summary": "Edit-flow subtask"},
    )

    assert creation_flow_response.status_code == 200
    assert edit_flow_response.status_code == 200
    for response, summary in (
        (creation_flow_response, "Creation-flow subtask"),
        (edit_flow_response, "Edit-flow subtask"),
    ):
        ticket = next(
            ticket
            for ticket in response.json()["state"]["tickets"]
            if ticket["id"] in {new_ticket_id, existing_ticket_id}
            and any(subtask["summary"] == summary for subtask in ticket["subtasks"])
        )
        subtask = next(subtask for subtask in ticket["subtasks"] if subtask["summary"] == summary)
        assert "notes" not in subtask

    with SessionLocal() as db:
        subtasks = list(
            db.scalars(
                select(Ticket).where(Ticket.parent_id.in_([new_ticket_id, existing_ticket_id]))
            )
        )
        assert {subtask.notes for subtask in subtasks} == {""}


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


def test_synced_ticket_saves_local_fields_without_jira_and_rejects_owned_fields(
    monkeypatch,
) -> None:
    class UnavailableJiraClient:
        def __init__(self, config) -> None:
            del config
            raise AssertionError("Jira must not be contacted for local-only edits")

    monkeypatch.setattr("work_tickets.app.JiraClient", UnavailableJiraClient)
    with SessionLocal() as db:
        category = Category(name="Synced local category")
        component = Component(name="synced-local-component")
        ticket = Ticket(
            summary="Jira-owned summary",
            description="Jira-owned description",
            notes="Old notes",
            planned_date=date(2026, 8, 25),
            category=category,
            component=component.name,
            position=0,
            jira_issue_key="WORK-200",
        )
        subtask = Ticket(
            summary="Jira-owned subtask summary",
            description="Jira-owned subtask description",
            planned_date=date(2026, 8, 26),
            position=0,
            parent=ticket,
            jira_issue_key="WORK-201",
        )
        db.add_all([category, component, ticket, subtask])
        db.commit()
        ticket_id = ticket.id
        subtask_id = subtask.id
        category_id = category.id

    local_update = client.put(
        f"/api/tickets/{ticket_id}",
        json={
            "summary": "Jira-owned summary",
            "description": "Jira-owned description",
            "notes": "New local notes",
            "planned_date": "2026-08-30",
            "category_id": category_id,
            "component": "synced-local-component",
        },
    )
    changed_summary = client.put(
        f"/api/tickets/{ticket_id}",
        json={
            "summary": "Attempted Jira summary change",
            "description": "Jira-owned description",
            "notes": "Another local update",
            "planned_date": "2026-08-31",
            "category_id": category_id,
            "component": "synced-local-component",
        },
    )
    changed_description = client.put(
        f"/api/tickets/{ticket_id}",
        json={
            "summary": "Jira-owned summary",
            "description": "Attempted Jira description change",
            "notes": "Another local update",
            "planned_date": "2026-09-01",
            "category_id": category_id,
            "component": "synced-local-component",
        },
    )
    changed_subtask_summary = client.put(
        f"/api/subtasks/{subtask_id}",
        json={
            "summary": "Attempted Jira subtask summary change",
            "description": "Jira-owned subtask description",
            "planned_date": "2026-08-29",
        },
    )
    changed_subtask_description = client.put(
        f"/api/subtasks/{subtask_id}",
        json={
            "summary": "Jira-owned subtask summary",
            "description": "Attempted Jira subtask description change",
            "planned_date": "2026-08-29",
        },
    )

    assert local_update.status_code == 200
    subtask_update = client.put(
        f"/api/subtasks/{subtask_id}",
        json={"planned_date": "2026-08-29"},
    )
    assert changed_summary.status_code == 422
    assert changed_summary.json() == {
        "ok": False,
        "message": "Ticket summary is owned by Jira and cannot be changed after sync.",
    }
    assert changed_description.status_code == 422
    assert changed_description.json() == {
        "ok": False,
        "message": "Ticket description is owned by Jira and cannot be changed after sync.",
    }
    assert changed_subtask_summary.status_code == 422
    assert changed_subtask_summary.json() == {
        "ok": False,
        "message": "Subtask summary is owned by Jira and cannot be changed after sync.",
    }
    assert changed_subtask_description.status_code == 422
    assert changed_subtask_description.json() == {
        "ok": False,
        "message": "Subtask description is owned by Jira and cannot be changed after sync.",
    }
    assert subtask_update.status_code == 200
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        assert ticket is not None
        assert ticket.summary == "Jira-owned summary"
        assert ticket.description == "Jira-owned description"
        assert ticket.notes == "New local notes"
        assert ticket.planned_date == date(2026, 8, 30)
        assert ticket.category_id == category_id
        assert ticket.component == "synced-local-component"
        subtask = db.get(Ticket, subtask_id)
        assert subtask is not None
        assert subtask.summary == "Jira-owned subtask summary"
        assert subtask.description == "Jira-owned subtask description"
        assert subtask.planned_date == date(2026, 8, 29)


def test_ticket_sync_surfaces_the_actual_jira_error(monkeypatch) -> None:
    class FailingJiraClient:
        def __init__(self, config) -> None:
            del config

        def create_issue(self, summary: str, description: str) -> JiraIssue:
            del summary, description
            raise JiraError("Jira returned HTTP 503: unavailable.")

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FailingJiraClient)
    with SessionLocal() as db:
        _seed_jira_config(db)
        ticket = Ticket(summary="Needs Jira", position=0)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(f"/api/tickets/{ticket_id}/sync")

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "Jira returned HTTP 503: unavailable.",
    }


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


def test_component_api_assigns_orders_and_deletes_without_changing_ticket_values() -> None:
    category_response = client.post("/api/categories", json={"name": "Component category"})
    category_id = next(
        item["id"]
        for item in category_response.json()["state"]["categories"]
        if item["name"] == "Component category"
    )
    second_category_response = client.post(
        "/api/categories", json={"name": "Second component category"}
    )
    second_category_id = next(
        item["id"]
        for item in second_category_response.json()["state"]["categories"]
        if item["name"] == "Second component category"
    )
    component_ids = {}
    for name in ("payment-integration-app", "payment-provider-app"):
        response = client.post("/api/components", json={"name": name})
        component_ids[name] = next(
            item["id"] for item in response.json()["state"]["components"] if item["name"] == name
        )

    first_id = component_ids["payment-integration-app"]
    second_id = component_ids["payment-provider-app"]
    assert (
        client.post(
            f"/api/categories/{category_id}/components", json={"component_id": first_id}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/categories/{category_id}/components", json={"component_id": second_id}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/categories/{second_category_id}/components", json={"component_id": first_id}
        ).status_code
        == 200
    )
    move_response = client.post(
        f"/api/categories/{category_id}/components/{first_id}/move",
        params={"target_index": 1},
    )

    assert move_response.status_code == 200
    category = next(
        item for item in move_response.json()["state"]["categories"] if item["id"] == category_id
    )
    assert [item["id"] for item in category["components"]] == [second_id, first_id]
    second_category = next(
        item
        for item in move_response.json()["state"]["categories"]
        if item["id"] == second_category_id
    )
    assert [item["id"] for item in second_category["components"]] == [first_id]

    ticket_response = client.post(
        "/api/tickets",
        json={
            "summary": "Retain deleted component",
            "category_id": category_id,
            "component": "payment-integration-app",
        },
    )
    ticket_id = ticket_response.json()["created_id"]
    delete_response = client.delete(f"/api/components/{first_id}")

    assert delete_response.status_code == 200
    assert all(
        component["id"] != first_id for component in delete_response.json()["state"]["components"]
    )
    assert all(
        first_id not in {component["id"] for component in category["components"]}
        for category in delete_response.json()["state"]["categories"]
    )
    ticket = next(
        item for item in delete_response.json()["state"]["tickets"] if item["id"] == ticket_id
    )
    assert ticket["component"] == "payment-integration-app"

    edit_response = client.put(
        f"/api/tickets/{ticket_id}",
        json={"summary": "Edited while component deleted", "component": "payment-integration-app"},
    )
    create_deleted_response = client.post(
        "/api/tickets",
        json={"summary": "Cannot select deleted", "component": "payment-integration-app"},
    )

    assert edit_response.status_code == 200
    assert create_deleted_response.status_code == 422
    with SessionLocal() as db:
        stored_ticket = db.get(Ticket, ticket_id)
        assert stored_ticket is not None
        assert stored_ticket.component == "payment-integration-app"
        assert (
            db.scalar(select(CategoryComponent).where(CategoryComponent.component_id == first_id))
            is None
        )
        assert db.get(Component, first_id) is None


def test_component_api_rejects_unsafe_directory_names() -> None:
    for name in (
        ".",
        "..",
        "../outside",
        r"..\outside",
        "bad/name",
        "bad\x00name",
        "bad\x7fname",
        "bad\x80name",
        "bad\x9fname",
        "bad:name",
    ):
        response = client.post("/api/components", json={"name": name})

        assert response.status_code == 422
        assert response.json() == {
            "ok": False,
            "message": "Component name must be a safe local directory name.",
        }

    with SessionLocal() as db:
        category = Category(name="Unsafe component assignment category")
        component = Component(name="unsafe:legacy-component")
        db.add_all([category, component])
        db.commit()
        category_id = category.id
        component_id = component.id

    response = client.post(
        f"/api/categories/{category_id}/components", json={"component_id": component_id}
    )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "Component name must be a safe local directory name.",
    }


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


def test_spa_ticket_edit_layout_constrains_narrow_content() -> None:
    frontend_source = Path(__file__).parents[1] / "frontend" / "src"
    style_source = (frontend_source / "style.css").read_text()

    assert "grid-template-columns: minmax(0, 1fr)" in style_source
    assert ".ticket-card { min-width: 0;" in style_source
    assert ".details-form > * { min-width: 0; max-width: 100%; }" in style_source
    assert ".details-form > .p-inputtext, .details-form > .p-textarea" in style_source
    assert "{ width: 100%; }" in style_source
    assert ".edit-toggle .p-button-label { white-space: normal;" in style_source
    assert ".draft-row, .subtask-row { min-width: 0; flex-wrap: wrap; }" in style_source
    assert ".draft-row > *, .subtask-row > * { min-width: 0; max-width: 100%; }" in style_source
    assert "grid-template-columns: minmax(0, 1fr); display: grid" in style_source
    date_control = ".date-control { display: flex; align-items: center; gap: 8px; min-width: 0;"
    assert date_control in style_source
    assert "max-width: 100%; flex-wrap: wrap; }" in style_source


def test_spa_disables_and_grays_jira_owned_ticket_fields() -> None:
    frontend_source = Path(__file__).parents[1] / "frontend" / "src"
    ticket_card_source = (frontend_source / "components" / "TicketCard.vue").read_text()
    style_source = (frontend_source / "style.css").read_text()

    assert (
        '<InputText v-model="ticket.summary" aria-label="Ticket summary" '
        ':disabled="!!ticket.jira_issue_key" '
        ":class=\"{ 'jira-owned-field': ticket.jira_issue_key }\" />"
    ) in ticket_card_source
    assert (
        '<Textarea v-model="ticket.description" rows="3" autoResize '
        'aria-label="Ticket description" :disabled="!!ticket.jira_issue_key" '
        ":class=\"{ 'jira-owned-field': ticket.jira_issue_key }\" />"
    ) in ticket_card_source
    assert (
        '<InputText v-if="!subtask.local_completed" v-model="subtask.summary" '
        ':aria-label="`Subtask ${subtask.summary}`" '
        ':disabled="!!subtask.jira_issue_key" '
        ":class=\"{ 'jira-owned-field': subtask.jira_issue_key }\" />"
    ) in ticket_card_source
    assert (
        '<Textarea v-if="!subtask.local_completed" v-model="subtask.description" '
        'rows="2" autoResize :aria-label="`Description for subtask ${subtask.summary}`" '
        ':disabled="!!subtask.jira_issue_key" '
        ":class=\"{ 'jira-owned-field': subtask.jira_issue_key }\" />"
    ) in ticket_card_source
    assert ".details-form .jira-owned-field { color:" in style_source
    assert 'v-model="ticket.notes"' in ticket_card_source
    assert (
        'v-model="ticket.planned_date" type="date" aria-label="Planned date"' in ticket_card_source
    )
    assert '<CategoryButtons v-model="ticket.category_id"' in ticket_card_source
    assert '<ComponentSelect v-model="ticket.component"' in ticket_card_source
    assert (
        'v-model="subtask.planned_date" type="date" aria-label="Subtask planned date"'
        in ticket_card_source
    )


def test_spa_uses_a_category_first_component_dropdown() -> None:
    frontend_source = Path(__file__).parents[1] / "frontend" / "src"
    component_select_source = (frontend_source / "components" / "ComponentSelect.vue").read_text()
    app_source = (frontend_source / "App.vue").read_text()

    assert "<Select" in component_select_source
    assert (
        "const ordered = [...(category?.components || []), ...props.components]"
        in component_select_source
    )
    assert "const seen = new Set<number>()" in component_select_source
    assert "disabled: true" in component_select_source
    assert "<ComponentSelect" in app_source


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
        assert config.in_review_status == "In Review"
        assert config.ready_to_merge_status == "Ready to Merge"
        assert config.ready_to_deploy_status == "Ready to Deploy"


def test_api_saves_gitlab_settings_without_exposing_or_overwriting_token() -> None:
    response = client.put(
        "/api/settings/jira",
        json={
            "base_url": "https://jira.example.test",
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "WORK",
            "issue_type": "Task",
            "gitlab_base_url": "https://gitlab.example.test/",
            "gitlab_token": "gitlab-secret",
        },
    )

    assert response.status_code == 200
    assert response.json()["state"]["jira_config"]["gitlab_base_url"] == (
        "https://gitlab.example.test"
    )
    assert "gitlab_token" not in response.json()["state"]["jira_config"]
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        assert config.gitlab_base_url == "https://gitlab.example.test"
        assert config.gitlab_token == "gitlab-secret"

    update_response = client.put(
        "/api/settings/jira",
        json={
            "base_url": "https://jira.example.test",
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "WORK",
            "issue_type": "Task",
            "gitlab_base_url": "https://gitlab.example.test",
            "gitlab_token": "",
        },
    )

    assert update_response.status_code == 200
    assert "gitlab_token" not in update_response.json()["state"]["jira_config"]
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        assert config.gitlab_token == "gitlab-secret"


def test_api_rejects_invalid_gitlab_base_url_without_persisting_it() -> None:
    response = client.put(
        "/api/settings/jira",
        json={
            "base_url": "https://jira.example.test",
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "WORK",
            "issue_type": "Task",
            "gitlab_base_url": "gitlab.example.test",
            "gitlab_token": "gitlab-secret",
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "GitLab base URL must start with http:// or https://.",
    }


def test_api_saves_configured_jira_workflow_statuses() -> None:
    response = client.put(
        "/api/settings/jira",
        json={
            "base_url": "https://jira.example.test",
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "WORK",
            "issue_type": "Task",
            "in_review_status": "Awaiting Review",
            "ready_to_merge_status": "Merge Queue",
            "ready_to_deploy_status": "Deployment Queue",
        },
    )

    assert response.status_code == 200
    assert response.json()["state"]["jira_config"] == {
        "base_url": "https://jira.example.test",
        "browser_base_url": "",
        "local_projects_directory": "",
        "gitlab_base_url": "",
        "email": "person@example.test",
        "project_key": "WORK",
        "issue_type": "Task",
        "completed_statuses": "Done",
        "in_review_status": "Awaiting Review",
        "ready_to_merge_status": "Merge Queue",
        "ready_to_deploy_status": "Deployment Queue",
    }

    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        assert config.in_review_status == "Awaiting Review"
        assert config.ready_to_merge_status == "Merge Queue"
        assert config.ready_to_deploy_status == "Deployment Queue"
        config.in_review_status = "In Review"
        config.ready_to_merge_status = "Ready to Merge"
        config.ready_to_deploy_status = "Ready to Deploy"
        db.commit()


def test_api_saves_only_existing_local_projects_directory_and_rejects_missing_path(
    tmp_path,
) -> None:
    existing_directory = tmp_path / "projects"
    existing_directory.mkdir()
    missing_directory = tmp_path / "missing"
    response = client.put(
        "/api/settings/jira",
        json={
            "base_url": "https://jira.example.test",
            "browser_base_url": "",
            "local_projects_directory": str(missing_directory),
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "WORK",
            "issue_type": "Task",
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "Local projects directory must exist and be a directory.",
    }

    valid_response = client.put(
        "/api/settings/jira",
        json={
            "base_url": "https://jira.example.test",
            "browser_base_url": "",
            "local_projects_directory": str(existing_directory),
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "WORK",
            "issue_type": "Task",
        },
    )

    assert valid_response.status_code == 200
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        assert config.local_projects_directory == str(existing_directory)


def test_api_rejects_invalid_local_projects_directory_without_server_error(
    monkeypatch, tmp_path
) -> None:
    payload = {
        "base_url": "https://jira.example.test",
        "browser_base_url": "",
        "email": "person@example.test",
        "api_token": "test-token",
        "project_key": "WORK",
        "issue_type": "Task",
    }

    invalid_response = client.put(
        "/api/settings/jira",
        json={**payload, "local_projects_directory": str(tmp_path / "invalid\x00path")},
    )

    assert invalid_response.status_code == 422
    assert invalid_response.json() == {
        "ok": False,
        "message": "Local projects directory must exist and be a directory.",
    }

    def raise_os_error(self) -> bool:
        del self
        raise OSError("path is too long")

    monkeypatch.setattr(Path, "is_dir", raise_os_error)
    overlong_response = client.put(
        "/api/settings/jira",
        json={**payload, "local_projects_directory": str(tmp_path / ("x" * 300))},
    )

    assert overlong_response.status_code == 422
    assert overlong_response.json() == {
        "ok": False,
        "message": "Local projects directory must exist and be a directory.",
    }


def test_api_rejects_local_projects_directory_when_expanduser_fails(monkeypatch, tmp_path) -> None:
    payload = {
        "base_url": "https://jira.example.test",
        "browser_base_url": "",
        "email": "person@example.test",
        "api_token": "test-token",
        "project_key": "WORK",
        "issue_type": "Task",
        "local_projects_directory": str(tmp_path),
    }

    def raise_runtime_error(self) -> Path:
        del self
        raise RuntimeError("home directory could not be determined")

    monkeypatch.setattr(Path, "expanduser", raise_runtime_error)
    response = client.put("/api/settings/jira", json=payload)

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "Local projects directory must exist and be a directory.",
    }


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


def test_api_reviews_filters_jira_issues_and_isolates_item_failures(monkeypatch) -> None:
    calls: list[str] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.email == "person@example.test"
            assert config.project_key == "WORK"
            assert config.issue_type == "Story"

        def search_issues(self, jql: str) -> list[JiraIssue]:
            calls.append(jql)
            return [
                JiraIssue(key="WORK-501", summary="Local review", status_name="Awaiting Review"),
                JiraIssue(
                    key="WORK-502", summary="Remote-only review", status_name="Awaiting Review"
                ),
            ]

        def get_issue(self, key: str) -> JiraIssue:
            if key == "WORK-502":
                raise JiraError("Jira returned HTTP 503.")
            return JiraIssue(
                key=key,
                summary="Updated local review",
                description="Review details: https://gitlab.example/group/repository/-/merge_requests/1234.",
                issue_type_name="Story",
                status_name="Awaiting Review",
            )

        def close(self) -> None:
            pass

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            assert config.gitlab_base_url == "https://gitlab.example"

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert project_path == "group/repository"
            assert number == 1234
            return GitLabMergeRequest(state="opened", updated_at="2026-08-30T10:00:00Z")

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    monkeypatch.setattr("work_tickets.app.GitLabClient", FakeGitLabClient)
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        if config is None:
            config = JiraConfig(
                id=1,
                base_url="https://jira.example.test",
                email="person@example.test",
                api_token="test-token",
                project_key="WORK",
                issue_type="Task",
                completed_statuses="Done",
            )
            db.add(config)
        config.project_key = "WORK"
        config.issue_type = "Story"
        config.in_review_status = "Awaiting Review"
        config.gitlab_base_url = "https://gitlab.example"
        local_ticket = Ticket(summary="Local ticket", jira_issue_key="work-501", position=0)
        db.add(local_ticket)
        db.commit()

    response = client.get("/api/reviews")

    assert response.status_code == 200
    assert calls == [
        'project = "WORK" AND issuetype = "Story" AND status = "Awaiting Review" '
        "AND assignee = currentUser() ORDER BY key"
    ]
    reviews = response.json()["reviews"]
    assert reviews[0]["summary"] == "Updated local review"
    assert reviews[0]["local_ticket"]["summary"] == "Local ticket"
    assert reviews[0]["error"] is None
    assert reviews[0]["merge_requests"] == [
        {
            "repository": "repository",
            "number": 1234,
            "url": "https://gitlab.example/group/repository/-/merge_requests/1234",
            "state": "opened",
            "updated_at": "2026-08-30T10:00:00Z",
            "draft": False,
        }
    ]
    assert reviews[0]["selected_merge_request"]["number"] == 1234
    assert reviews[0]["ready_to_merge_enabled"] is True
    assert reviews[0]["merge_request_selection_reason"] == (
        "Selected the only open MR; closed MRs were ignored."
    )
    assert reviews[1]["key"] == "WORK-502"
    assert reviews[1]["summary"] == "Remote-only review"
    assert reviews[1]["local_ticket"] is None
    assert reviews[1]["error"] == "Jira returned HTTP 503."


def test_api_reviews_exposes_selection_and_disables_ambiguous_or_failed_items(monkeypatch) -> None:
    descriptions = {
        "WORK-601": "https://gitlab.example/team/repository/-/merge_requests/1",
        "WORK-602": " ".join(
            (
                "https://gitlab.example/team/repository/-/merge_requests/2",
                "https://gitlab.example/team/repository/-/merge_requests/3",
            )
        ),
        "WORK-603": " ".join(
            (
                "https://gitlab.example/team/repository/-/merge_requests/4",
                "https://gitlab.example/team/repository/-/merge_requests/5",
            )
        ),
        "WORK-604": "https://gitlab.example/team/repository/-/merge_requests/6",
        "WORK-605": "No review link",
    }

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def search_issues(self, jql: str) -> list[JiraIssue]:
            del jql
            return [JiraIssue(key=key, summary=key) for key in descriptions]

        def get_issue(self, key: str) -> JiraIssue:
            return JiraIssue(key=key, summary=key, description=descriptions[key])

        def close(self) -> None:
            pass

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert project_path == "team/repository"
            if number == 1:
                return GitLabMergeRequest("opened", "2026-08-30T10:00:00Z")
            if number in {2, 3}:
                return GitLabMergeRequest("opened", f"2026-08-30T{number:02d}:00:00Z")
            if number == 4:
                return GitLabMergeRequest("closed", "2026-08-30T10:00:00Z")
            if number == 5:
                return GitLabMergeRequest("closed", "2026-08-30T11:00:00Z")
            if number == 6:
                raise GitLabError("GitLab returned HTTP 503: unavailable.")
            raise AssertionError(f"unexpected merge request {number}")

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    monkeypatch.setattr("work_tickets.app.GitLabClient", FakeGitLabClient)
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.gitlab_base_url = "https://gitlab.example"
        db.commit()

    response = client.get("/api/reviews")

    assert response.status_code == 200
    reviews = {review["key"]: review for review in response.json()["reviews"]}
    assert reviews["WORK-601"]["ready_to_merge_enabled"] is True
    assert reviews["WORK-602"]["ready_to_merge_enabled"] is False
    assert "multiple open MRs" in reviews["WORK-602"]["merge_request_selection_reason"]
    assert reviews["WORK-603"]["selected_merge_request"]["number"] == 5
    assert reviews["WORK-604"]["ready_to_merge_enabled"] is False
    assert reviews["WORK-604"]["error"] == "GitLab returned HTTP 503: unavailable."
    assert reviews["WORK-604"]["merge_requests"][0]["number"] == 6
    assert reviews["WORK-604"]["merge_request_selection_reason"] == (
        "Merge request state has not been retrieved."
    )
    assert reviews["WORK-605"]["selected_merge_request"] is None
    assert reviews["WORK-605"]["ready_to_merge_enabled"] is False


def test_detect_merge_requests_extracts_repository_and_number() -> None:
    assert detect_merge_requests(
        "See https://gitlab.example/group1/group2/repository/-/merge_requests/1234.",
        "https://gitlab.example",
    ) == [
        MergeRequestReference(
            repository="repository",
            number=1234,
            url="https://gitlab.example/group1/group2/repository/-/merge_requests/1234",
        )
    ]


def test_detect_merge_requests_accepts_adf_link_attributes_and_multiple_links() -> None:
    description = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "Review this MR",
                        "marks": [
                            {
                                "type": "link",
                                "attrs": {
                                    "href": "https://gitlab.example/team/service/-/merge_requests/12"
                                },
                            }
                        ],
                    },
                    {
                        "type": "text",
                        "text": (" and https://gitlab.example/team/other/-/merge_requests/34"),
                    },
                ],
            }
        ],
    }

    assert detect_merge_requests(description, "https://gitlab.example/") == [
        MergeRequestReference(
            repository="service",
            number=12,
            url="https://gitlab.example/team/service/-/merge_requests/12",
        ),
        MergeRequestReference(
            repository="other",
            number=34,
            url="https://gitlab.example/team/other/-/merge_requests/34",
        ),
    ]


def test_detect_merge_requests_requires_configured_origin_and_base_path() -> None:
    description = " ".join(
        (
            "https://gitlab.example/gitlab/team/repository/-/merge_requests/1,",
            "https://gitlab.example/gitlab-other/team/repository/-/merge_requests/2",
            "https://gitlab.example.evil/gitlab/team/repository/-/merge_requests/3",
            "https://gitlab.example/gitlab/../team/repository/-/merge_requests/7",
            "https://gitlab.example/gitlab/team/repository/-/merge_requests/4extra",
            "https://gitlab.example/gitlab/team/repository/-/merge_requests/5/notes",
            "https://gitlab.example/gitlab/team/repository/-/merge_requests/6",
        )
    )

    assert detect_merge_requests(description, "https://gitlab.example/gitlab/") == [
        MergeRequestReference(
            repository="repository",
            number=1,
            url="https://gitlab.example/gitlab/team/repository/-/merge_requests/1",
        ),
        MergeRequestReference(
            repository="repository",
            number=6,
            url="https://gitlab.example/gitlab/team/repository/-/merge_requests/6",
        ),
    ]


def test_detect_merge_requests_ignores_malformed_base_and_duplicate_links() -> None:
    link = "https://gitlab.example/team/repository/-/merge_requests/42"

    assert detect_merge_requests(f"{link} {link}", "https://gitlab.example") == [
        MergeRequestReference(repository="repository", number=42, url=link)
    ]
    assert detect_merge_requests(link, "not a url") == []


def test_select_merge_request_disables_without_an_mr() -> None:
    selection = select_merge_request([])

    assert selection.selected is None
    assert selection.enabled is False
    assert selection.reason == "No merge requests were found in the Jira description."


def test_select_merge_request_disables_when_multiple_mrs_are_open() -> None:
    selection = select_merge_request(
        [
            (
                MergeRequestReference("one", 1, "https://gitlab.example/one/-/merge_requests/1"),
                GitLabMergeRequest("opened", "2026-08-30T10:00:00Z"),
            ),
            (
                MergeRequestReference("two", 2, "https://gitlab.example/two/-/merge_requests/2"),
                GitLabMergeRequest("opened", "2026-08-30T11:00:00Z"),
            ),
        ]
    )

    assert selection.selected is None
    assert selection.enabled is False
    assert "multiple open MRs" in selection.reason


def test_select_merge_request_prefers_one_open_mr_over_closed_mrs() -> None:
    selected = MergeRequestReference("open", 2, "https://gitlab.example/open/-/merge_requests/2")
    selection = select_merge_request(
        [
            (
                MergeRequestReference(
                    "closed", 1, "https://gitlab.example/closed/-/merge_requests/1"
                ),
                GitLabMergeRequest("closed", "2026-08-30T12:00:00Z"),
            ),
            (selected, GitLabMergeRequest("opened", "2026-08-30T10:00:00Z")),
        ]
    )

    assert selection.enabled is True
    assert selection.selected is not None
    assert selection.selected["url"] == selected.url
    assert selection.reason == "Selected the only open MR; closed MRs were ignored."


def test_select_merge_request_uses_most_recent_closed_mr() -> None:
    selected = MergeRequestReference("newer", 2, "https://gitlab.example/newer/-/merge_requests/2")
    selection = select_merge_request(
        [
            (
                MergeRequestReference(
                    "older", 1, "https://gitlab.example/older/-/merge_requests/1"
                ),
                GitLabMergeRequest("closed", "2026-08-30T10:00:00Z"),
            ),
            (selected, GitLabMergeRequest("merged", "2026-08-30T11:00:00+00:00")),
        ]
    )

    assert selection.enabled is True
    assert selection.selected is not None
    assert selection.selected["url"] == selected.url
    assert selection.reason == "All MRs are closed; selected the most recently updated MR."


def test_select_merge_request_rejects_unknown_state_and_invalid_updated_at() -> None:
    reference = MergeRequestReference("repo", 1, "https://gitlab.example/repo/-/merge_requests/1")

    for merge_request in (
        GitLabMergeRequest("unknown", "2026-08-30T10:00:00Z"),
        GitLabMergeRequest("closed", "not-a-timestamp"),
    ):
        try:
            select_merge_request([(reference, merge_request)])
        except JiraError as exc:
            assert "merge request repo!1" in str(exc)
        else:
            raise AssertionError("Expected malformed merge request data to fail")


def test_select_merge_request_disables_tied_closed_mrs() -> None:
    selection = select_merge_request(
        [
            (
                MergeRequestReference("one", 1, "https://gitlab.example/one/-/merge_requests/1"),
                GitLabMergeRequest("closed", "2026-08-30T10:00:00Z"),
            ),
            (
                MergeRequestReference("two", 2, "https://gitlab.example/two/-/merge_requests/2"),
                GitLabMergeRequest("closed", "2026-08-30T10:00:00Z"),
            ),
        ]
    )

    assert selection.enabled is False
    assert "tied" in selection.reason


def test_gitlab_client_retrieves_merge_request_state_and_updated_at() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "state": "opened",
                "updated_at": "2026-08-30T10:00:00Z",
                "draft": False,
                "web_url": "https://gitlab.example/group/repository/-/merge_requests/42",
                "merge_commit_sha": None,
            },
        )

    config = JiraConfig(
        gitlab_base_url="https://gitlab.example/gitlab",
        gitlab_token="gitlab-secret",
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
    )
    gitlab = GitLabClient(config, transport=httpx.MockTransport(handler))
    result = gitlab.get_merge_request("group/repository", 42)
    gitlab.close()

    assert result == GitLabMergeRequest(
        "opened",
        "2026-08-30T10:00:00Z",
        draft=False,
        web_url="https://gitlab.example/group/repository/-/merge_requests/42",
    )
    assert requests[0].url.raw_path == (
        b"/gitlab/api/v4/projects/group%2Frepository/merge_requests/42"
    )
    assert requests[0].headers["PRIVATE-TOKEN"] == "gitlab-secret"


def test_gitlab_client_reports_missing_fields_and_api_errors() -> None:
    responses = [
        httpx.Response(200, json={"updated_at": "2026-08-30T10:00:00Z"}),
        httpx.Response(200, json={"state": "closed"}),
        httpx.Response(503, json={"message": "GitLab unavailable"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return responses.pop(0)

    config = JiraConfig(
        gitlab_base_url="https://gitlab.example",
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
    )
    gitlab = GitLabClient(config, transport=httpx.MockTransport(handler))
    for expected in ("without a valid state", "without a valid updated_at", "HTTP 503"):
        try:
            gitlab.get_merge_request("group/repository", 42)
        except GitLabError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("Expected GitLab response failure")
    gitlab.close()


def test_gitlab_client_retrieves_and_changes_merge_request_approval() -> None:
    requests: list[httpx.Request] = []
    responses = [
        httpx.Response(200, json={"approved": False}),
        httpx.Response(201),
        httpx.Response(200, json={"approved": True}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    config = JiraConfig(
        gitlab_base_url="https://gitlab.example/gitlab",
        gitlab_token="gitlab-secret",
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
    )
    gitlab = GitLabClient(config, transport=httpx.MockTransport(handler))
    assert gitlab.get_merge_request_approval_state("group/repository", 42) == (
        GitLabMergeRequestApprovalState(approved=False)
    )
    gitlab.approve_merge_request("group/repository", 42)
    assert gitlab.get_merge_request_approval_state("group/repository", 42).approved is True
    gitlab.close()

    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert requests[0].url.raw_path == (
        b"/gitlab/api/v4/projects/group%2Frepository/merge_requests/42/approvals"
    )
    assert requests[1].url.raw_path == (
        b"/gitlab/api/v4/projects/group%2Frepository/merge_requests/42/approve"
    )


def test_gitlab_client_marks_draft_merge_request_ready_and_rechecks_it() -> None:
    requests: list[httpx.Request] = []
    responses = [
        httpx.Response(
            200,
            json={"state": "opened", "updated_at": "2026-08-30T10:00:00Z", "draft": True},
        ),
        httpx.Response(201),
        httpx.Response(
            200,
            json={"state": "opened", "updated_at": "2026-08-30T10:01:00Z", "draft": False},
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    config = JiraConfig(
        gitlab_base_url="https://gitlab.example/gitlab",
        gitlab_token="gitlab-secret",
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
    )
    gitlab = GitLabClient(config, transport=httpx.MockTransport(handler))
    assert gitlab.get_merge_request("group/repository", 42).draft is True
    gitlab.mark_merge_request_ready("group/repository", 42)
    assert gitlab.get_merge_request("group/repository", 42).draft is False
    gitlab.close()

    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert requests[1].url.raw_path == (
        b"/gitlab/api/v4/projects/group%2Frepository/merge_requests/42/notes"
    )
    assert json.loads(requests[1].content) == {"body": "/ready"}


def test_gitlab_client_squash_merges_and_validates_merge_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "state": "merged",
                "updated_at": "2026-08-30T10:02:00Z",
                "draft": False,
                "squash_commit_sha": "abcdef0123456789abcdef0123456789abcdef01",
                "web_url": "https://gitlab.example/group/repository/-/merge_requests/42",
            },
        )

    config = JiraConfig(
        gitlab_base_url="https://gitlab.example",
        gitlab_token="gitlab-secret",
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
    )
    gitlab = GitLabClient(config, transport=httpx.MockTransport(handler))
    result = gitlab.merge_merge_request("group/repository", 42)
    gitlab.close()

    assert result == GitLabMergeRequest(
        "merged",
        "2026-08-30T10:02:00Z",
        draft=False,
        merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
        web_url="https://gitlab.example/group/repository/-/merge_requests/42",
    )
    assert requests[0].method == "POST"
    assert requests[0].url.raw_path == (
        b"/api/v4/projects/group%2Frepository/merge_requests/42/merge"
    )
    assert json.loads(requests[0].content) == {"squash": True}


def test_gitlab_client_uses_squash_sha_only_when_present_and_non_null() -> None:
    base_payload = {
        "state": "merged",
        "updated_at": "2026-08-30T10:02:00Z",
        "draft": False,
        "merge_commit_sha": "abcdef0123456789abcdef0123456789abcdef01",
    }

    for squash_commit_sha in ("absent", None):
        payload = base_payload.copy()
        if squash_commit_sha != "absent":
            payload["squash_commit_sha"] = squash_commit_sha
        result = GitLabClient._merge_request_from_payload(payload, "group/repository", 42)
        assert result.merge_commit_sha == base_payload["merge_commit_sha"]

    for squash_commit_sha in ("", "not-a-sha"):
        payload = {**base_payload, "squash_commit_sha": squash_commit_sha}
        with pytest.raises(GitLabError, match="invalid squash commit SHA"):
            GitLabClient._merge_request_from_payload(payload, "group/repository", 42)


def test_gitlab_client_rejects_invalid_squash_merge_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"state": "merged", "draft": False})

    config = JiraConfig(
        gitlab_base_url="https://gitlab.example",
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
    )
    gitlab = GitLabClient(config, transport=httpx.MockTransport(handler))
    try:
        gitlab.merge_merge_request("group/repository", 42)
    except GitLabError as exc:
        assert "without a valid updated_at" in str(exc)
    else:
        raise AssertionError("Expected malformed squash merge response")
    finally:
        gitlab.close()


def test_gitlab_client_retrieves_paginated_discussions_and_mutates_threads() -> None:
    requests: list[httpx.Request] = []
    responses = [
        httpx.Response(
            200,
            headers={"X-Next-Page": "2"},
            json=[
                {
                    "id": "discussion-1",
                    "notes": [
                        {
                            "id": 11,
                            "resolvable": True,
                            "resolved": False,
                            "body": "Needs review",
                        },
                    ],
                }
            ],
        ),
        httpx.Response(
            200,
            json=[
                {
                    "id": "discussion-2",
                    "notes": [
                        {"id": 21, "resolvable": True, "resolved": True},
                        {"id": 22, "resolvable": False, "resolved": False},
                    ],
                }
            ],
        ),
        httpx.Response(201, json={"id": 31}),
        httpx.Response(
            200,
            json={
                "id": "discussion-1",
                "notes": [{"id": 11, "resolvable": True, "resolved": True}],
            },
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    config = JiraConfig(
        gitlab_base_url="https://gitlab.example/gitlab",
        gitlab_token="gitlab-secret",
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
    )
    gitlab = GitLabClient(config, transport=httpx.MockTransport(handler))
    discussions = gitlab.get_merge_request_discussions("group/repository", 42)
    gitlab.add_merge_request_discussion_note("group/repository", 42, "discussion-1", "Approved 👑")
    gitlab.resolve_merge_request_discussion("group/repository", 42, "discussion-1")
    gitlab.close()

    assert discussions == [
        GitLabMergeRequestDiscussion(
            id="discussion-1",
            notes=(
                GitLabMergeRequestDiscussionNote(
                    id=11,
                    resolvable=True,
                    resolved=False,
                    body="Needs review",
                ),
            ),
        ),
        GitLabMergeRequestDiscussion(
            id="discussion-2",
            notes=(
                GitLabMergeRequestDiscussionNote(id=21, resolvable=True, resolved=True),
                GitLabMergeRequestDiscussionNote(id=22, resolvable=False, resolved=False),
            ),
        ),
    ]
    assert [request.method for request in requests] == ["GET", "GET", "POST", "PUT"]
    assert requests[0].url.raw_path.startswith(
        b"/gitlab/api/v4/projects/group%2Frepository/merge_requests/42/discussions?"
    )
    assert str(requests[0].url.params) == "page=1&per_page=100"
    assert str(requests[1].url.params) == "page=2&per_page=100"
    assert requests[2].url.raw_path.endswith(b"/discussions/discussion-1/notes")
    assert json.loads(requests[2].content) == {"body": "Approved 👑"}
    assert requests[3].url.raw_path.endswith(b"/discussions/discussion-1")
    assert json.loads(requests[3].content) == {"resolved": True}


def test_gitlab_client_validates_discussion_and_mutation_responses() -> None:
    responses = [
        httpx.Response(200, json=[{"id": "discussion-1", "notes": [{"id": 1}]}]),
        httpx.Response(201, json={}),
        httpx.Response(
            200,
            json={
                "id": "discussion-1",
                "notes": [{"id": 1, "resolvable": True, "resolved": False}],
            },
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return responses.pop(0)

    config = JiraConfig(
        gitlab_base_url="https://gitlab.example",
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
    )
    gitlab = GitLabClient(config, transport=httpx.MockTransport(handler))
    for operation, expected in (
        (
            lambda: gitlab.get_merge_request_discussions("group/repository", 42),
            "invalid note",
        ),
        (
            lambda: gitlab.add_merge_request_discussion_note(
                "group/repository", 42, "discussion-1", "Approved 👑"
            ),
            "invalid merge request discussion comment",
        ),
        (
            lambda: gitlab.resolve_merge_request_discussion("group/repository", 42, "discussion-1"),
            "did not resolve",
        ),
    ):
        try:
            operation()
        except GitLabError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("Expected malformed GitLab discussion response")
    gitlab.close()


def test_parse_gitlab_base_url_rejects_boundary_whitespace() -> None:
    for value in (
        " https://gitlab.example/group",
        "https://gitlab.example/group ",
        "\thttps://gitlab.example/group",
        "https://gitlab.example/group\n",
    ):
        assert parse_gitlab_base_url(value) is None


def test_detect_merge_requests_rejects_encoded_whitespace_and_control_path_values() -> None:
    description = " ".join(
        (
            "https://gitlab.example/team/%20repository/-/merge_requests/1",
            "https://gitlab.example/team/%00repository/-/merge_requests/2",
            "https://gitlab.example/team/%0Arepository/-/merge_requests/3",
        )
    )

    assert detect_merge_requests(description, "https://gitlab.example") == []


def test_parse_gitlab_base_url_accepts_valid_urls() -> None:
    assert parse_gitlab_base_url("https://gitlab.example/group/") == (
        "https",
        "gitlab.example",
        443,
        "/group",
    )
    assert parse_gitlab_base_url("http://gitlab.example:8080/group") == (
        "http",
        "gitlab.example",
        8080,
        "/group",
    )


def test_detect_merge_requests_distinguishes_same_basename_in_different_groups() -> None:
    assert detect_merge_requests(
        " ".join(
            (
                "https://gitlab.example/team-a/repository/-/merge_requests/42",
                "https://gitlab.example/team-b/repository/-/merge_requests/42",
                "https://gitlab.example/team-a/repository/-/merge_requests/42",
            )
        ),
        "https://gitlab.example",
    ) == [
        MergeRequestReference(
            repository="repository",
            number=42,
            url="https://gitlab.example/team-a/repository/-/merge_requests/42",
        ),
        MergeRequestReference(
            repository="repository",
            number=42,
            url="https://gitlab.example/team-b/repository/-/merge_requests/42",
        ),
    ]


def test_api_rejects_unsafe_gitlab_base_urls_without_persisting_or_exposing_them() -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        if config is None:
            config = JiraConfig(
                id=1,
                base_url="https://jira.example.test",
                email="person@example.test",
                api_token="test-token",
                project_key="WORK",
                issue_type="Task",
            )
            db.add(config)
        config.gitlab_base_url = "https://previous.gitlab.example/group"
        config.gitlab_token = "previous-gitlab-secret"
        db.commit()

    invalid_urls = (
        "https://user:password@gitlab.example.test",
        "https://gitlab.example.test:not-a-port",
        "https://gitlab.example.test:",
        "https://gitlab.example.test/group?project=work",
        "https://gitlab.example.test/group#merge-requests",
        "https://gitlab.example.test/group/../other",
        "https://gitlab.example.test/group with-space",
        " https://gitlab.example.test/group",
        "https://gitlab.example.test/group ",
        "https://gitlab.example.test/group/%20path",
        "https://gitlab.example.test/group/%00path",
    )
    for invalid_url in invalid_urls:
        response = client.put(
            "/api/settings/jira",
            json={
                "base_url": "https://jira.example.test",
                "email": "person@example.test",
                "api_token": "test-token",
                "project_key": "WORK",
                "issue_type": "Task",
                "gitlab_base_url": invalid_url,
                "gitlab_token": "new-gitlab-secret",
            },
        )

        assert response.status_code == 422
        assert "state" not in response.json()
        with SessionLocal() as db:
            config = db.get(JiraConfig, 1)
            assert config is not None
            assert config.gitlab_base_url == "https://previous.gitlab.example/group"
            assert config.gitlab_token == "previous-gitlab-secret"

    state = client.get("/api/state").json()
    assert state["jira_config"]["gitlab_base_url"] == "https://previous.gitlab.example/group"
    assert "gitlab_token" not in state["jira_config"]


def test_jira_client_searches_issues_with_paging_fields() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "key": "WORK-503",
                        "fields": {
                            "summary": "Search result",
                            "issuetype": {"name": "Story"},
                            "status": {"name": "In Review"},
                        },
                    }
                ],
                "total": 1,
            },
        )

    config = JiraConfig(
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Story",
    )
    jira = JiraClient(config, transport=httpx.MockTransport(handler))
    issues = jira.search_issues('project = "WORK"')
    jira.close()

    assert issues == [
        JiraIssue(
            key="WORK-503",
            summary="Search result",
            issue_type_name="Story",
            status_name="In Review",
        )
    ]
    assert requests[0].url.path == "/rest/api/3/search"
    assert requests[0].url.params["jql"] == 'project = "WORK"'
    assert requests[0].url.params["startAt"] == "0"
    assert requests[0].url.params["maxResults"] == "100"
    assert requests[0].url.params["fields"] == "summary,description,issuetype,status"


def test_jira_client_adds_plain_text_comment() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": "comment-1"})

    config = JiraConfig(
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Task",
    )
    jira = JiraClient(config, transport=httpx.MockTransport(handler))
    jira.add_comment("work-508", "Tested and reviewed.")
    jira.close()

    assert requests[0].url.path == "/rest/api/3/issue/work-508/comment"
    assert json.loads(requests[0].content) == {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Tested and reviewed."}],
                }
            ],
        }
    }


def test_jira_client_reads_paginated_comment_bodies() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params["startAt"] == "0":
            return httpx.Response(
                200,
                json={
                    "startAt": 0,
                    "total": 2,
                    "comments": [
                        {
                            "body": {
                                "type": "doc",
                                "version": 1,
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {"type": "text", "text": "Tested and reviewed."}
                                        ],
                                    }
                                ],
                            }
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={"startAt": 1, "total": 2, "comments": [{"body": "A second comment"}]},
        )

    config = JiraConfig(
        base_url="https://work.atlassian.net",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Task",
    )
    jira = JiraClient(config, transport=httpx.MockTransport(handler))
    comments = jira.get_comments("WORK-508")
    jira.close()

    assert comments == ["Tested and reviewed.", "A second comment"]
    assert [request.method for request in requests] == ["GET", "GET"]
    assert requests[0].url.path == "/rest/api/3/issue/WORK-508/comment"
    assert requests[0].url.params["maxResults"] == "100"
    assert requests[1].url.params["startAt"] == "1"


def test_jira_client_preserves_linked_comment_for_adf_and_plain_jira() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": "comment-1"})

    cloud_config = JiraConfig(
        base_url="https://work.atlassian.net",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Task",
    )
    cloud = JiraClient(cloud_config, transport=httpx.MockTransport(handler))
    cloud.add_comment(
        "WORK-509",
        "Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef01]",
    )
    cloud.close()

    server_config = JiraConfig(
        base_url="https://jira.example.test/jira",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Task",
    )
    server = JiraClient(server_config, transport=httpx.MockTransport(handler))
    server.add_comment(
        "WORK-509",
        "Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef01]",
    )
    server.close()

    assert json.loads(requests[0].content)["body"] == {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Merged with "},
                    {
                        "type": "text",
                        "text": "abcdef01",
                        "marks": [
                            {
                                "type": "link",
                                "attrs": {
                                    "href": "https://gitlab.example/group/repository/-/commit/abcdef01"
                                },
                            }
                        ],
                    },
                ],
            }
        ],
    }
    assert json.loads(requests[1].content)["body"] == (
        "Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef01]"
    )


def test_jira_comment_idempotency_matches_cloud_adf_link_after_successful_post() -> None:
    comment = "Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef01]"
    requests: list[httpx.Request] = []
    remote_body: dict[str, object] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal remote_body
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "comments": ([{"body": remote_body}] if remote_body is not None else []),
                    "total": 1 if remote_body is not None else 0,
                },
            )
        assert request.method == "POST"
        payload = json.loads(request.content)
        remote_body = payload["body"]
        return httpx.Response(201, json={"id": "comment-1"})

    config = JiraConfig(
        base_url="https://work.atlassian.net",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Task",
    )
    jira = JiraClient(config, transport=httpx.MockTransport(handler))
    _add_jira_comment_if_missing(jira, "WORK-536", comment)
    _add_jira_comment_if_missing(jira, "WORK-536", comment)
    jira.close()

    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert sum(request.method == "POST" for request in requests) == 1


def test_jira_comment_idempotency_confirms_cloud_adf_link_after_ambiguous_post() -> None:
    comment = "Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef01]"
    requests: list[httpx.Request] = []
    remote_body: dict[str, object] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal remote_body
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "comments": ([{"body": remote_body}] if remote_body is not None else []),
                    "total": 1 if remote_body is not None else 0,
                },
            )
        assert request.method == "POST"
        payload = json.loads(request.content)
        remote_body = payload["body"]
        return httpx.Response(503, json={"errorMessages": ["gateway timeout"]})

    config = JiraConfig(
        base_url="https://work.atlassian.net",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Task",
    )
    jira = JiraClient(config, transport=httpx.MockTransport(handler))
    _add_jira_comment_if_missing(jira, "WORK-537", comment)
    _add_jira_comment_if_missing(jira, "WORK-537", comment)
    jira.close()

    assert [request.method for request in requests] == ["GET", "POST", "GET", "GET"]
    assert sum(request.method == "POST" for request in requests) == 1


def test_jira_client_transitions_by_destination_status() -> None:
    requests: list[httpx.Request] = []
    transitioned = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transitioned
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/transitions"):
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "11", "name": "Unrelated", "to": {"name": "Done"}},
                        {"id": "42", "name": "Advance", "to": {"name": "Ready to Merge"}},
                    ]
                },
            )
        if request.method == "POST":
            transitioned = True
            assert json.loads(request.content) == {"transition": {"id": "42"}}
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "key": "WORK-504",
                "fields": {
                    "summary": "Review item",
                    "status": {"name": "Ready to Merge" if transitioned else "In Progress"},
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
    issue = jira.transition_issue("WORK-504", "Ready to Merge")
    jira.close()

    assert issue == JiraIssue(key="WORK-504", summary="Review item", status_name="Ready to Merge")
    assert [request.method for request in requests] == ["GET", "GET", "POST", "GET"]
    assert all(request.method != "PUT" for request in requests)


def test_jira_client_confirms_ambiguous_transition_without_repeat() -> None:
    requests: list[httpx.Request] = []
    status = "In Progress"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/transitions"):
            return httpx.Response(
                200,
                json={"transitions": [{"id": "42", "to": {"name": "Ready to Merge"}}]},
            )
        if request.method == "POST":
            assert json.loads(request.content) == {"transition": {"id": "42"}}
            status = "Ready to Merge"
            return httpx.Response(503, json={"errorMessages": ["gateway timeout"]})
        return httpx.Response(
            200,
            json={"key": "WORK-535", "fields": {"status": {"name": status}}},
        )

    config = JiraConfig(
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Task",
    )
    jira = JiraClient(config, transport=httpx.MockTransport(handler))
    issue = jira.transition_issue("WORK-535", "Ready to Merge")
    jira.close()

    assert issue == JiraIssue(key="WORK-535", status_name="Ready to Merge")
    assert [request.method for request in requests] == ["GET", "GET", "POST", "GET"]
    assert [request.url.path for request in requests] == [
        "/rest/api/3/issue/WORK-535",
        "/rest/api/3/issue/WORK-535/transitions",
        "/rest/api/3/issue/WORK-535/transitions",
        "/rest/api/3/issue/WORK-535",
    ]
    assert sum(request.method == "POST" for request in requests) == 1


def test_jira_client_treats_current_target_status_as_idempotent() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "key": "WORK-505",
                "fields": {"summary": "Already ready", "status": {"name": "Ready to Deploy"}},
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
    issue = jira.transition_issue("WORK-505", "Ready to Deploy")
    jira.close()

    assert issue.status_name == "Ready to Deploy"
    assert [request.method for request in requests] == ["GET"]


def test_jira_client_reports_missing_transition_destination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"transitions": []})

    config = JiraConfig(
        base_url="https://jira.example.test",
        email="person@example.test",
        api_token="test-token",
        project_key="WORK",
        issue_type="Task",
    )
    jira = JiraClient(config, transport=httpx.MockTransport(handler))
    try:
        jira.transition_issue("WORK-506", "Ready to Deploy", current_status="In Progress")
    except JiraError as exc:
        assert str(exc) == "Jira issue WORK-506 has no transition to status 'Ready to Deploy'."
    else:
        raise AssertionError("Expected missing transition destination to fail")
    finally:
        jira.close()


def test_transition_service_treats_current_target_status_as_idempotent() -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.ready_to_merge_status == "Ready to Merge"

        def get_issue(self, key: str) -> JiraIssue:
            return JiraIssue(key=key, status_name="Ready to Merge")

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            raise AssertionError("an already-target issue must not be transitioned")

        def close(self) -> None:
            pass

    with SessionLocal() as db:
        config = _seed_jira_config(db)
        db.commit()
        result = transition_jira_issue(
            "work-507",
            config.ready_to_merge_status,
            db,
            jira_client_factory=FakeJiraClient,
        )

    assert result == JiraIssue(key="WORK-507", status_name="Ready to Merge")


def test_merge_selected_merge_request_treats_already_merged_as_success() -> None:
    class FakeGitLabClient:
        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 518)
            return GitLabMergeRequest(
                "merged",
                "2026-08-30T10:00:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/518",
            )

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            del project_path, number
            raise AssertionError("an already merged MR must not be merged again")

    selection = MergeRequestSelection(
        selected={
            "repository": "repository",
            "number": 518,
            "url": "https://gitlab.example/group/repository/-/merge_requests/518",
            "state": "merged",
        },
        enabled=True,
        reason="All MRs are closed; selected the most recently updated MR.",
    )

    _merge_selected_merge_request("https://gitlab.example", selection, FakeGitLabClient())


@pytest.mark.parametrize(
    "merge_request, expected",
    (
        (
            GitLabMergeRequest(
                "merged",
                "2026-08-30T10:00:00Z",
                merge_commit_sha="not-a-sha",
                web_url="https://gitlab.example/group/repository/-/merge_requests/518",
            ),
            "valid merge commit SHA",
        ),
        (
            GitLabMergeRequest(
                "merged",
                "2026-08-30T10:00:00Z",
                merge_commit_sha=None,
                web_url="https://gitlab.example/group/repository/-/merge_requests/518",
            ),
            "valid merge commit SHA",
        ),
        (
            GitLabMergeRequest(
                "merged",
                "2026-08-30T10:00:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/merge_requests/518",
            ),
            "valid merge request URL",
        ),
        (
            GitLabMergeRequest(
                "merged",
                "2026-08-30T10:00:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example:invalid/group/repository/-/merge_requests/518",
            ),
            "valid merge request URL",
        ),
    ),
)
def test_merged_commit_link_rejects_missing_or_malformed_merge_metadata(
    merge_request: GitLabMergeRequest, expected: str
) -> None:
    with pytest.raises(JiraError, match=expected):
        _merged_commit_link(merge_request)


def test_wait_for_merge_polls_with_backoff_until_merged() -> None:
    responses = iter(
        (
            GitLabMergeRequest("opened", "2026-08-30T10:01:00Z"),
            GitLabMergeRequest("merged", "2026-08-30T10:02:00Z"),
        )
    )
    delays: list[float] = []
    now = [0.0]

    class FakeGitLabClient:
        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 519)
            return next(responses)

    def sleep(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    _wait_for_merge(
        FakeGitLabClient(),
        "group/repository",
        MergeRequestReference(
            "repository", 519, "https://gitlab.example/group/repository/-/merge_requests/519"
        ),
        GitLabMergeRequest("opened", "2026-08-30T10:00:00Z"),
        timeout_seconds=10,
        sleep=sleep,
        monotonic=lambda: now[0],
    )

    assert delays == [0.5, 1.0]


def test_wait_for_merge_times_out_with_bounded_backoff() -> None:
    delays: list[float] = []
    now = [0.0]

    class FakeGitLabClient:
        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            del project_path, number
            return GitLabMergeRequest("opened", "2026-08-30T10:01:00Z")

    def sleep(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    try:
        _wait_for_merge(
            FakeGitLabClient(),
            "group/repository",
            MergeRequestReference(
                "repository", 520, "https://gitlab.example/group/repository/-/merge_requests/520"
            ),
            GitLabMergeRequest("opened", "2026-08-30T10:00:00Z"),
            timeout_seconds=1,
            sleep=sleep,
            monotonic=lambda: now[0],
        )
    except JiraError as exc:
        assert str(exc) == (
            "Timed out waiting for GitLab merge request repository!520 to reach merged state."
        )
    else:
        raise AssertionError("Expected merge polling to time out")

    assert delays == [0.5, 0.5]


def test_wait_for_merge_reports_terminal_failure_state() -> None:
    class FakeGitLabClient:
        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            del project_path, number
            return GitLabMergeRequest("closed", "2026-08-30T10:01:00Z")

    try:
        _wait_for_merge(
            FakeGitLabClient(),
            "group/repository",
            MergeRequestReference(
                "repository", 521, "https://gitlab.example/group/repository/-/merge_requests/521"
            ),
            GitLabMergeRequest("opened", "2026-08-30T10:00:00Z"),
            timeout_seconds=10,
            sleep=lambda delay: None,
            monotonic=lambda: 0.0,
        )
    except JiraError as exc:
        assert str(exc) == (
            "GitLab merge request repository!521 reached terminal state 'closed' "
            "while waiting to merge."
        )
    else:
        raise AssertionError("Expected terminal merge failure")


@pytest.mark.parametrize(
    "race",
    (
        "initially-merged",
        "concurrent-before-approval",
        "concurrent-before-draft",
        "concurrent-before-discussions",
        "concurrent-before-merge",
    ),
)
def test_ready_to_merge_review_handles_merged_mr_without_later_mutations(race: str) -> None:
    calls: list[str] = []
    jira_status = ["Awaiting Review"]

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def get_issue(self, key: str) -> JiraIssue:
            calls.append(f"jira-get:{key}")
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/522",
                status_name=jira_status[0],
            )

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            calls.append(f"transition:{key}:{target_status}:{current_status}")
            jira_status[0] = target_status
            return JiraIssue(key=key, status_name=target_status)

        def add_comment(self, key: str, comment: str) -> None:
            calls.append(f"jira-comment:{key}:{comment}")

        def close(self) -> None:
            calls.append("jira-close")

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config
            self.state = "merged" if race == "initially-merged" else "opened"
            self.draft = race == "concurrent-before-draft"

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 522)
            calls.append("mr-get")
            return GitLabMergeRequest(
                self.state,
                "2026-08-30T10:00:00Z",
                draft=self.draft,
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/522",
            )

        def merge_before_mutation(self) -> None:
            self.state = "merged"
            raise GitLabError("GitLab returned HTTP 405: merge request is already merged.")

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            del project_path, number
            calls.append("approval-get")
            return GitLabMergeRequestApprovalState(approved=race != "concurrent-before-approval")

        def approve_merge_request(self, project_path: str, number: int) -> None:
            del project_path, number
            calls.append("approve")
            if race == "concurrent-before-approval":
                self.merge_before_mutation()

        def mark_merge_request_ready(self, project_path: str, number: int) -> None:
            del project_path, number
            calls.append("mark-ready")
            if race == "concurrent-before-draft":
                self.merge_before_mutation()

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            del project_path, number
            calls.append("discussions-get")
            if race == "concurrent-before-discussions":
                return [
                    GitLabMergeRequestDiscussion(
                        "thread-1",
                        (GitLabMergeRequestDiscussionNote(1, resolvable=True, resolved=False),),
                    )
                ]
            return []

        def add_merge_request_discussion_note(
            self, project_path: str, number: int, discussion_id: str, body: str
        ) -> None:
            del project_path, number, discussion_id, body
            calls.append("discussion-comment")
            if race == "concurrent-before-discussions":
                self.merge_before_mutation()

        def resolve_merge_request_discussion(
            self, project_path: str, number: int, discussion_id: str
        ) -> None:
            del project_path, number, discussion_id
            calls.append("discussion-resolve")

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            del project_path, number
            calls.append("merge")
            if race == "concurrent-before-merge":
                self.merge_before_mutation()
            return GitLabMergeRequest(
                "merged",
                "2026-08-30T10:01:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/522",
            )

        def close(self) -> None:
            calls.append("gitlab-close")

    with SessionLocal() as db:
        config = _seed_jira_config(
            db,
            ready_to_merge_status="Merge Queue",
        )
        db.commit()
        result = ready_to_merge_review(
            "WORK-522",
            db,
            jira_client_factory=FakeJiraClient,
            gitlab_client_factory=FakeGitLabClient,
        )
        config.ready_to_merge_status = "Ready to Merge"
        db.commit()

    assert result.status_name == "Ready to Deploy"
    expected_calls = {
        "initially-merged": [
            "jira-get:WORK-522",
            "mr-get",
            "mr-get",
        ],
        "concurrent-before-approval": [
            "jira-get:WORK-522",
            "mr-get",
            "mr-get",
            "approval-get",
            "approve",
            "mr-get",
        ],
        "concurrent-before-draft": [
            "jira-get:WORK-522",
            "mr-get",
            "mr-get",
            "approval-get",
            "mr-get",
            "mark-ready",
            "mr-get",
        ],
        "concurrent-before-discussions": [
            "jira-get:WORK-522",
            "mr-get",
            "mr-get",
            "approval-get",
            "mr-get",
            "mr-get",
            "discussions-get",
            "discussion-comment",
            "mr-get",
        ],
        "concurrent-before-merge": [
            "jira-get:WORK-522",
            "mr-get",
            "mr-get",
            "approval-get",
            "mr-get",
            "mr-get",
            "discussions-get",
            "mr-get",
            "merge",
            "mr-get",
        ],
    }
    common_calls = [
        "jira-get:WORK-522",
        "transition:WORK-522:Merge Queue:Awaiting Review",
        "jira-get:WORK-522",
        "jira-comment:WORK-522:Tested and reviewed.",
        "jira-comment:WORK-522:Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef0123456789abcdef0123456789abcdef01]",
        "jira-get:WORK-522",
        "transition:WORK-522:Ready to Deploy:Merge Queue",
        "jira-get:WORK-522",
        "gitlab-close",
        "jira-close",
    ]
    assert calls == expected_calls[race] + common_calls
    assert "discussion-resolve" not in calls
    if race in {
        "initially-merged",
        "concurrent-before-approval",
        "concurrent-before-draft",
        "concurrent-before-discussions",
    }:
        assert "merge" not in calls


def test_ready_to_merge_review_transitions_and_comments_once() -> None:
    calls: list[tuple[str, ...]] = []
    jira_status = ["Awaiting Review"]

    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.ready_to_merge_status == "Merge Queue"

        def get_issue(self, key: str) -> JiraIssue:
            calls.append(("get", key))
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/509",
                status_name=jira_status[0],
            )

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            calls.append(("transition", key, target_status, current_status))
            jira_status[0] = target_status
            return JiraIssue(key=key, status_name=target_status)

        def add_comment(self, key: str, comment: str) -> None:
            calls.append(("comment", key, comment))

        def close(self) -> None:
            calls.append(("close",))

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 509)
            return GitLabMergeRequest(state="opened", updated_at="2026-08-30T10:00:00Z")

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            assert (project_path, number) == ("group/repository", 509)
            return GitLabMergeRequestApprovalState(approved=True)

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 509)
            return GitLabMergeRequest(
                state="merged",
                updated_at="2026-08-30T10:01:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/509",
            )

        def approve_merge_request(self, project_path: str, number: int) -> None:
            raise AssertionError("an approved MR must not be approved again")

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            del project_path, number
            return []

        def close(self) -> None:
            pass

    with SessionLocal() as db:
        _seed_jira_config(db, ready_to_merge_status="Merge Queue")
        db.commit()
        result = ready_to_merge_review(
            "work-509",
            db,
            jira_client_factory=FakeJiraClient,
            gitlab_client_factory=FakeGitLabClient,
        )

    assert result == JiraIssue(
        key="WORK-509",
        description="https://gitlab.example/group/repository/-/merge_requests/509",
        status_name="Ready to Deploy",
    )
    assert calls == [
        ("get", "WORK-509"),
        ("get", "WORK-509"),
        ("transition", "WORK-509", "Merge Queue", "Awaiting Review"),
        ("get", "WORK-509"),
        ("comment", "WORK-509", "Tested and reviewed."),
        (
            "comment",
            "WORK-509",
            "Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef0123456789abcdef0123456789abcdef01]",
        ),
        ("get", "WORK-509"),
        ("transition", "WORK-509", "Ready to Deploy", "Merge Queue"),
        ("get", "WORK-509"),
        ("close",),
    ]


def test_ready_to_merge_review_skips_transition_when_already_ready_and_no_discussions() -> None:
    calls: list[str] = []
    jira_status = ["Ready to Merge"]

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def get_issue(self, key: str) -> JiraIssue:
            calls.append(f"get:{key}")
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/510",
                status_name=jira_status[0],
            )

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            assert target_status == "Ready to Deploy"
            calls.append(f"transition:{key}:{target_status}:{current_status}")
            jira_status[0] = target_status
            return JiraIssue(key=key, status_name=target_status)

        def add_comment(self, key: str, comment: str) -> None:
            calls.append(f"comment:{key}:{comment}")

        def close(self) -> None:
            calls.append("close")

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 510)
            return GitLabMergeRequest(
                state="opened", updated_at="2026-08-30T10:00:00Z", draft=False
            )

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            assert (project_path, number) == ("group/repository", 510)
            return GitLabMergeRequestApprovalState(approved=True)

        def approve_merge_request(self, project_path: str, number: int) -> None:
            raise AssertionError("an approved MR must not be approved again")

        def mark_merge_request_ready(self, project_path: str, number: int) -> None:
            raise AssertionError("a non-draft MR must not be marked ready again")

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 510)
            return GitLabMergeRequest(
                state="merged",
                updated_at="2026-08-30T10:01:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/510",
            )

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            del project_path, number
            calls.append("discussions-get")
            return []

        def close(self) -> None:
            pass

    with SessionLocal() as db:
        _seed_jira_config(db)
        result = ready_to_merge_review(
            "WORK-510",
            db,
            jira_client_factory=FakeJiraClient,
            gitlab_client_factory=FakeGitLabClient,
        )

    assert result.status_name == "Ready to Deploy"
    assert calls == [
        "get:WORK-510",
        "discussions-get",
        "get:WORK-510",
        "comment:WORK-510:Tested and reviewed.",
        "comment:WORK-510:Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef0123456789abcdef0123456789abcdef01]",
        "get:WORK-510",
        "transition:WORK-510:Ready to Deploy:Ready to Merge",
        "get:WORK-510",
        "close",
    ]


def test_ready_to_merge_review_does_not_regress_an_already_deployable_issue() -> None:
    calls: list[str] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.ready_to_merge_status == "Ready to Merge"
            assert config.ready_to_deploy_status == "Ready to Deploy"

        def get_issue(self, key: str) -> JiraIssue:
            calls.append(f"get:{key}")
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/523",
                status_name="Ready to Deploy",
            )

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            del key, target_status, current_status
            raise AssertionError("an already-deployable issue must not be transitioned")

        def add_comment(self, key: str, comment: str) -> None:
            calls.append(f"comment:{key}:{comment}")

        def close(self) -> None:
            calls.append("close")

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 523)
            return GitLabMergeRequest("opened", "2026-08-30T10:00:00Z")

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            return GitLabMergeRequestApprovalState(approved=True)

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            return []

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            return GitLabMergeRequest(
                "merged",
                "2026-08-30T10:01:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/523",
            )

        def close(self) -> None:
            pass

    with SessionLocal() as db:
        _seed_jira_config(db)
        result = ready_to_merge_review(
            "WORK-523",
            db,
            jira_client_factory=FakeJiraClient,
            gitlab_client_factory=FakeGitLabClient,
        )

    assert result == JiraIssue(
        key="WORK-523",
        description="https://gitlab.example/group/repository/-/merge_requests/523",
        status_name="Ready to Deploy",
    )
    assert calls == [
        "get:WORK-523",
        "get:WORK-523",
        "comment:WORK-523:Tested and reviewed.",
        "comment:WORK-523:Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef0123456789abcdef0123456789abcdef01]",
        "get:WORK-523",
        "close",
    ]


def test_ready_to_merge_review_refuses_without_an_unambiguous_mr() -> None:
    calls: list[str] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def get_issue(self, key: str) -> JiraIssue:
            calls.append(f"get:{key}")
            return JiraIssue(key=key, description="No merge request", status_name="In Review")

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            del key, target_status, current_status
            raise AssertionError("an issue without a selected MR must not be transitioned")

        def add_comment(self, key: str, comment: str) -> None:
            del key, comment
            raise AssertionError("an issue without a selected MR must not be commented")

        def close(self) -> None:
            calls.append("close")

    with SessionLocal() as db:
        _seed_jira_config(db, gitlab_base_url="")
        db.commit()
        try:
            ready_to_merge_review("WORK-512", db, jira_client_factory=FakeJiraClient)
        except JiraError as exc:
            assert str(exc) == "No merge requests were found in the Jira description."
        else:
            raise AssertionError("Expected Ready to Merge to require a selected MR")

    assert calls == ["get:WORK-512", "close"]


def test_api_ready_to_merge_review_uses_configured_status_and_reports_success(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    jira_status = ["Awaiting Review"]

    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.ready_to_merge_status == "Merge Queue"

        def get_issue(self, key: str) -> JiraIssue:
            calls.append(("get", key))
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/511",
                status_name=jira_status[0],
            )

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            calls.append(("transition", key, target_status, current_status))
            jira_status[0] = target_status
            return JiraIssue(key=key, status_name=target_status)

        def add_comment(self, key: str, comment: str) -> None:
            calls.append(("comment", key, comment))

        def close(self) -> None:
            calls.append(("close",))

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 511)
            return GitLabMergeRequest(state="opened", updated_at="2026-08-30T10:00:00Z")

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            assert (project_path, number) == ("group/repository", 511)
            return GitLabMergeRequestApprovalState(approved=True)

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 511)
            return GitLabMergeRequest(
                state="merged",
                updated_at="2026-08-30T10:01:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/511",
            )

        def approve_merge_request(self, project_path: str, number: int) -> None:
            raise AssertionError("an approved MR must not be approved again")

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            del project_path, number
            return []

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    monkeypatch.setattr("work_tickets.app.GitLabClient", FakeGitLabClient)
    with SessionLocal() as db:
        _seed_jira_config(db, ready_to_merge_status="Merge Queue")
        db.commit()

    response = client.post("/api/reviews/work-511/ready-to-merge")

    with SessionLocal() as db:
        _seed_jira_config(db)
        db.commit()

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "message": "Review completed and merge request merged successfully.",
        "review": {"key": "WORK-511", "status_name": "Ready to Deploy"},
    }
    assert calls == [
        ("get", "WORK-511"),
        ("get", "WORK-511"),
        ("transition", "WORK-511", "Merge Queue", "Awaiting Review"),
        ("get", "WORK-511"),
        ("comment", "WORK-511", "Tested and reviewed."),
        (
            "comment",
            "WORK-511",
            "Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef0123456789abcdef0123456789abcdef01]",
        ),
        ("get", "WORK-511"),
        ("transition", "WORK-511", "Ready to Deploy", "Merge Queue"),
        ("get", "WORK-511"),
        ("close",),
    ]


def test_ready_to_merge_review_approves_selected_mr_before_jira_updates() -> None:
    calls: list[tuple[object, ...]] = []
    jira_status = ["Awaiting Review"]
    approval_states = iter((False, True))
    draft_states = iter((True, True, True, False, False, False))

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def get_issue(self, key: str) -> JiraIssue:
            calls.append(("jira-get", key))
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/513",
                status_name=jira_status[0],
            )

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            calls.append(("transition", key, target_status, current_status))
            jira_status[0] = target_status
            return JiraIssue(key=key, status_name=target_status)

        def add_comment(self, key: str, comment: str) -> None:
            calls.append(("comment", key, comment))

        def close(self) -> None:
            calls.append(("jira-close",))

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            calls.append(("mr-get", project_path, number))
            return GitLabMergeRequest(
                state="opened",
                updated_at="2026-08-30T10:00:00Z",
                draft=next(draft_states),
            )

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            calls.append(("approval-get", project_path, number))
            return GitLabMergeRequestApprovalState(approved=next(approval_states))

        def approve_merge_request(self, project_path: str, number: int) -> None:
            calls.append(("approve", project_path, number))

        def mark_merge_request_ready(self, project_path: str, number: int) -> None:
            calls.append(("mark-ready", project_path, number))

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            calls.append(("merge", project_path, number))
            return GitLabMergeRequest(
                state="merged",
                updated_at="2026-08-30T10:01:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/513",
            )

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            del project_path, number
            return []

        def close(self) -> None:
            calls.append(("gitlab-close",))

    with SessionLocal() as db:
        _seed_jira_config(db, ready_to_merge_status="Merge Queue")
        db.commit()
        result = ready_to_merge_review(
            "WORK-513",
            db,
            jira_client_factory=FakeJiraClient,
            gitlab_client_factory=FakeGitLabClient,
        )

    assert result.status_name == "Ready to Deploy"
    assert calls == [
        ("jira-get", "WORK-513"),
        ("mr-get", "group/repository", 513),
        ("mr-get", "group/repository", 513),
        ("approval-get", "group/repository", 513),
        ("approve", "group/repository", 513),
        ("approval-get", "group/repository", 513),
        ("mr-get", "group/repository", 513),
        ("mark-ready", "group/repository", 513),
        ("mr-get", "group/repository", 513),
        ("mr-get", "group/repository", 513),
        ("mr-get", "group/repository", 513),
        ("merge", "group/repository", 513),
        ("jira-get", "WORK-513"),
        ("transition", "WORK-513", "Merge Queue", "Awaiting Review"),
        ("jira-get", "WORK-513"),
        ("comment", "WORK-513", "Tested and reviewed."),
        (
            "comment",
            "WORK-513",
            "Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef0123456789abcdef0123456789abcdef01]",
        ),
        ("jira-get", "WORK-513"),
        ("transition", "WORK-513", "Ready to Deploy", "Merge Queue"),
        ("jira-get", "WORK-513"),
        ("gitlab-close",),
        ("jira-close",),
    ]


def test_ready_to_merge_review_reports_draft_failure_without_jira_side_effects() -> None:
    calls: list[str] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def get_issue(self, key: str) -> JiraIssue:
            calls.append(f"get:{key}")
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/515",
                status_name="Awaiting Review",
            )

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            del key, target_status, current_status
            raise AssertionError("Jira must not transition after draft failure")

        def add_comment(self, key: str, comment: str) -> None:
            del key, comment
            raise AssertionError("Jira must not be commented after draft failure")

        def close(self) -> None:
            calls.append("jira-close")

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 515)
            calls.append("mr-get")
            return GitLabMergeRequest("opened", "2026-08-30T10:00:00Z", draft=True)

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            assert (project_path, number) == ("group/repository", 515)
            return GitLabMergeRequestApprovalState(approved=True)

        def approve_merge_request(self, project_path: str, number: int) -> None:
            del project_path, number
            raise AssertionError("an approved MR must not be approved again")

        def mark_merge_request_ready(self, project_path: str, number: int) -> None:
            assert (project_path, number) == ("group/repository", 515)
            raise GitLabError("GitLab returned HTTP 503: draft update unavailable.")

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            del project_path, number
            raise AssertionError("discussion retrieval must not run after draft failure")

        def close(self) -> None:
            calls.append("gitlab-close")

    with SessionLocal() as db:
        _seed_jira_config(db)
        try:
            ready_to_merge_review(
                "WORK-515",
                db,
                jira_client_factory=FakeJiraClient,
                gitlab_client_factory=FakeGitLabClient,
            )
        except JiraError as exc:
            assert str(exc) == "GitLab returned HTTP 503: draft update unavailable."
        else:
            raise AssertionError("Expected draft update failure")

    assert calls == [
        "get:WORK-515",
        "mr-get",
        "mr-get",
        "mr-get",
        "mr-get",
        "gitlab-close",
        "jira-close",
    ]


def test_api_ready_to_merge_review_reports_gitlab_approval_failure_without_jira_side_effects(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def get_issue(self, key: str) -> JiraIssue:
            calls.append(f"get:{key}")
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/514",
                status_name="Awaiting Review",
            )

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            del key, target_status, current_status
            raise AssertionError("Jira must not transition after approval failure")

        def add_comment(self, key: str, comment: str) -> None:
            del key, comment
            raise AssertionError("Jira must not be commented after approval failure")

        def close(self) -> None:
            calls.append("close")

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 514)
            return GitLabMergeRequest(state="opened", updated_at="2026-08-30T10:00:00Z")

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            assert (project_path, number) == ("group/repository", 514)
            raise GitLabError("GitLab returned HTTP 503: approvals unavailable.")

        def approve_merge_request(self, project_path: str, number: int) -> None:
            del project_path, number
            raise AssertionError("approval mutation must not run after state failure")

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            del project_path, number
            raise AssertionError("discussion retrieval must not run after approval failure")

        def close(self) -> None:
            calls.append("gitlab-close")

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    monkeypatch.setattr("work_tickets.app.GitLabClient", FakeGitLabClient)
    with SessionLocal() as db:
        _seed_jira_config(db)
        db.commit()

    response = client.post("/api/reviews/work-514/ready-to-merge")

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "GitLab returned HTTP 503: approvals unavailable.",
    }
    assert calls == ["get:WORK-514", "gitlab-close", "close"]


def test_ready_to_merge_review_resolves_all_currently_unresolved_discussions_in_order() -> None:
    calls: list[tuple[object, ...]] = []
    jira_status = ["Awaiting Review"]

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def get_issue(self, key: str) -> JiraIssue:
            calls.append(("jira-get", key))
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/516",
                status_name=jira_status[0],
            )

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            calls.append(("transition", key, target_status, current_status))
            jira_status[0] = target_status
            return JiraIssue(key=key, status_name=target_status)

        def add_comment(self, key: str, comment: str) -> None:
            calls.append(("jira-comment", key, comment))

        def close(self) -> None:
            calls.append(("jira-close",))

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            calls.append(("mr-get", project_path, number))
            return GitLabMergeRequest("opened", "2026-08-30T10:00:00Z")

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            calls.append(("approval-get", project_path, number))
            return GitLabMergeRequestApprovalState(approved=True)

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            calls.append(("discussions-get", project_path, number))
            return [
                GitLabMergeRequestDiscussion(
                    "resolved",
                    (GitLabMergeRequestDiscussionNote(1, resolvable=True, resolved=True),),
                ),
                GitLabMergeRequestDiscussion(
                    "thread-1",
                    (GitLabMergeRequestDiscussionNote(11, resolvable=True, resolved=False),),
                ),
                GitLabMergeRequestDiscussion(
                    "ordinary-comment",
                    (GitLabMergeRequestDiscussionNote(12, resolvable=False, resolved=False),),
                ),
                GitLabMergeRequestDiscussion(
                    "thread-2",
                    (GitLabMergeRequestDiscussionNote(21, resolvable=True, resolved=False),),
                ),
            ]

        def add_merge_request_discussion_note(
            self, project_path: str, number: int, discussion_id: str, body: str
        ) -> None:
            calls.append(("discussion-comment", project_path, number, discussion_id, body))

        def resolve_merge_request_discussion(
            self, project_path: str, number: int, discussion_id: str
        ) -> None:
            calls.append(("discussion-resolve", project_path, number, discussion_id))

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            calls.append(("merge", project_path, number))
            return GitLabMergeRequest(
                state="merged",
                updated_at="2026-08-30T10:01:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/516",
            )

        def close(self) -> None:
            calls.append(("gitlab-close",))

    with SessionLocal() as db:
        _seed_jira_config(db, ready_to_merge_status="Merge Queue")
        db.commit()
        result = ready_to_merge_review(
            "WORK-516",
            db,
            jira_client_factory=FakeJiraClient,
            gitlab_client_factory=FakeGitLabClient,
        )

    assert result.status_name == "Ready to Deploy"
    assert calls == [
        ("jira-get", "WORK-516"),
        ("mr-get", "group/repository", 516),
        ("mr-get", "group/repository", 516),
        ("approval-get", "group/repository", 516),
        ("mr-get", "group/repository", 516),
        ("mr-get", "group/repository", 516),
        ("discussions-get", "group/repository", 516),
        ("discussion-comment", "group/repository", 516, "thread-1", "Approved 👑"),
        ("discussion-resolve", "group/repository", 516, "thread-1"),
        ("discussion-comment", "group/repository", 516, "thread-2", "Approved 👑"),
        ("discussion-resolve", "group/repository", 516, "thread-2"),
        ("mr-get", "group/repository", 516),
        ("merge", "group/repository", 516),
        ("jira-get", "WORK-516"),
        ("transition", "WORK-516", "Merge Queue", "Awaiting Review"),
        ("jira-get", "WORK-516"),
        ("jira-comment", "WORK-516", "Tested and reviewed."),
        (
            "jira-comment",
            "WORK-516",
            "Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef0123456789abcdef0123456789abcdef01]",
        ),
        ("jira-get", "WORK-516"),
        ("transition", "WORK-516", "Ready to Deploy", "Merge Queue"),
        ("jira-get", "WORK-516"),
        ("gitlab-close",),
        ("jira-close",),
    ]


def test_ready_to_merge_review_preserves_discussion_failure_and_skips_later_steps() -> None:
    calls: list[str] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def get_issue(self, key: str) -> JiraIssue:
            calls.append(f"jira-get:{key}")
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/517",
                status_name="Awaiting Review",
            )

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            del key, target_status, current_status
            raise AssertionError("Jira must not be changed after discussion failure")

        def add_comment(self, key: str, comment: str) -> None:
            del key, comment
            raise AssertionError("Jira must not be commented after discussion failure")

        def close(self) -> None:
            calls.append("jira-close")

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            del project_path, number
            return GitLabMergeRequest("opened", "2026-08-30T10:00:00Z")

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            del project_path, number
            return GitLabMergeRequestApprovalState(approved=True)

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            del project_path, number
            calls.append("discussions-get")
            return [
                GitLabMergeRequestDiscussion(
                    "thread-failed",
                    (GitLabMergeRequestDiscussionNote(31, resolvable=True, resolved=False),),
                )
            ]

        def add_merge_request_discussion_note(
            self, project_path: str, number: int, discussion_id: str, body: str
        ) -> None:
            del project_path, number, discussion_id, body
            calls.append("discussion-comment")

        def resolve_merge_request_discussion(
            self, project_path: str, number: int, discussion_id: str
        ) -> None:
            del project_path, number, discussion_id
            calls.append("discussion-resolve")
            raise GitLabError("GitLab returned HTTP 503: resolution unavailable.")

        def close(self) -> None:
            calls.append("gitlab-close")

    with SessionLocal() as db:
        _seed_jira_config(db)
        db.commit()
        try:
            ready_to_merge_review(
                "WORK-517",
                db,
                jira_client_factory=FakeJiraClient,
                gitlab_client_factory=FakeGitLabClient,
            )
        except JiraError as exc:
            assert str(exc) == (
                "Could not resolve GitLab discussion thread-failed on merge request "
                "repository!517: GitLab returned HTTP 503: resolution unavailable."
            )
        else:
            raise AssertionError("Expected discussion failure")

    assert calls == [
        "jira-get:WORK-517",
        "discussions-get",
        "discussion-comment",
        "discussion-resolve",
        "discussions-get",
        "gitlab-close",
        "jira-close",
    ]


@pytest.mark.parametrize(
    "failure_stage",
    (
        "approval",
        "draft",
        "discussion-comment",
        "discussion-resolve",
        "merge",
        "jira-ready-transition",
        "jira-review-comment",
        "jira-commit-comment",
        "jira-deploy-transition",
    ),
)
def test_ready_to_merge_review_retries_each_stage_from_current_state(failure_stage: str) -> None:
    state = {
        "approved": True,
        "draft": False,
        "discussion_body": None,
        "discussion_resolved": False,
        "mr_state": "merged",
        "jira_status": "Ready to Deploy",
        "jira_comments": [],
        "failed": False,
        "jira_gets": 0,
        "mr_gets": 0,
        "approval_gets": 0,
        "approval_mutations": 0,
        "draft_mutations": 0,
        "discussion_comments": 0,
        "discussion_resolutions": 0,
        "merges": 0,
        "transitions": [],
        "comments": [],
    }
    state.update(
        approved=failure_stage != "approval",
        draft=failure_stage not in {"discussion-comment", "discussion-resolve"},
        mr_state="opened",
        jira_status="In Review",
    )

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def get_issue(self, key: str) -> JiraIssue:
            assert key == "WORK-518"
            state["jira_gets"] += 1
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/518",
                status_name=state["jira_status"],
            )

        def get_comments(self, key: str) -> list[str]:
            assert key == "WORK-518"
            return list(state["jira_comments"])

        def add_comment(self, key: str, comment: str) -> None:
            assert key == "WORK-518"
            state["comments"].append(comment)
            if (
                failure_stage in {"jira-review-comment", "jira-commit-comment"}
                and not state["failed"]
            ):
                state["failed"] = True
                raise JiraError(f"forced failure at {failure_stage}")
            state["jira_comments"].append(comment)

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            assert key == "WORK-518"
            assert current_status == state["jira_status"]
            state["transitions"].append(target_status)
            if target_status == "Ready to Merge" and failure_stage == "jira-ready-transition":
                if not state["failed"]:
                    state["failed"] = True
                    raise JiraError("forced failure at jira-ready-transition")
            if target_status == "Ready to Deploy" and failure_stage == "jira-deploy-transition":
                if not state["failed"]:
                    state["failed"] = True
                    raise JiraError("forced failure at jira-deploy-transition")
            state["jira_status"] = target_status
            return JiraIssue(key=key, status_name=target_status)

        def close(self) -> None:
            pass

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 518)
            state["mr_gets"] += 1
            return GitLabMergeRequest(
                state["mr_state"],
                "2026-08-30T10:00:00Z",
                draft=state["draft"],
                merge_commit_sha=(
                    "abcdef0123456789abcdef0123456789abcdef01"
                    if state["mr_state"] == "merged"
                    else None
                ),
                web_url=(
                    "https://gitlab.example/group/repository/-/merge_requests/518"
                    if state["mr_state"] == "merged"
                    else None
                ),
            )

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            assert (project_path, number) == ("group/repository", 518)
            state["approval_gets"] += 1
            return GitLabMergeRequestApprovalState(approved=state["approved"])

        def approve_merge_request(self, project_path: str, number: int) -> None:
            assert (project_path, number) == ("group/repository", 518)
            state["approval_mutations"] += 1
            state["approved"] = True
            if failure_stage == "approval" and not state["failed"]:
                state["failed"] = True
                raise GitLabError("forced failure at approval")

        def mark_merge_request_ready(self, project_path: str, number: int) -> None:
            assert (project_path, number) == ("group/repository", 518)
            state["draft_mutations"] += 1
            state["draft"] = False
            if failure_stage == "draft" and not state["failed"]:
                state["failed"] = True
                raise GitLabError("forced failure at draft")

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            assert (project_path, number) == ("group/repository", 518)
            return [
                GitLabMergeRequestDiscussion(
                    "thread-1",
                    (
                        GitLabMergeRequestDiscussionNote(
                            1,
                            resolvable=True,
                            resolved=state["discussion_resolved"],
                            body=state["discussion_body"],
                        ),
                    ),
                )
            ]

        def add_merge_request_discussion_note(
            self, project_path: str, number: int, discussion_id: str, body: str
        ) -> None:
            assert (project_path, number, discussion_id, body) == (
                "group/repository",
                518,
                "thread-1",
                "Approved 👑",
            )
            state["discussion_comments"] += 1
            if failure_stage == "discussion-comment" and not state["failed"]:
                state["failed"] = True
                raise GitLabError("forced failure at discussion-comment")
            state["discussion_body"] = body

        def resolve_merge_request_discussion(
            self, project_path: str, number: int, discussion_id: str
        ) -> None:
            assert (project_path, number, discussion_id) == ("group/repository", 518, "thread-1")
            state["discussion_resolutions"] += 1
            if failure_stage == "discussion-resolve" and not state["failed"]:
                state["failed"] = True
                raise GitLabError("forced failure at discussion-resolve")
            state["discussion_resolved"] = True

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 518)
            state["merges"] += 1
            if failure_stage == "merge" and not state["failed"]:
                state["failed"] = True
                raise GitLabError("forced failure at merge")
            state["mr_state"] = "merged"
            return GitLabMergeRequest(
                "merged",
                "2026-08-30T10:01:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/518",
            )

        def close(self) -> None:
            pass

    with SessionLocal() as db:
        _seed_jira_config(db, in_review_status="In Review")
        db.commit()
        with pytest.raises(JiraError, match=failure_stage):
            ready_to_merge_review(
                "WORK-518",
                db,
                jira_client_factory=FakeJiraClient,
                gitlab_client_factory=FakeGitLabClient,
            )
        result = ready_to_merge_review(
            "WORK-518",
            db,
            jira_client_factory=FakeJiraClient,
            gitlab_client_factory=FakeGitLabClient,
        )

    assert result.status_name == "Ready to Deploy"
    assert state["jira_gets"] >= 2
    assert state["mr_gets"] >= 2
    assert state["jira_status"] == "Ready to Deploy"
    assert state["discussion_resolved"] is True
    assert state["mr_state"] == "merged"
    assert state["jira_comments"] == [
        "Tested and reviewed.",
        "Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef0123456789abcdef0123456789abcdef01]",
    ]
    assert state["discussion_comments"] == (2 if failure_stage == "discussion-comment" else 1)
    assert state["discussion_resolutions"] == (2 if failure_stage == "discussion-resolve" else 1)
    assert state["approval_mutations"] <= 1
    assert state["draft_mutations"] <= 1
    assert state["merges"] == (2 if failure_stage == "merge" else 1)


def test_ready_to_merge_review_serializes_concurrent_attempts_for_one_issue() -> None:
    state = {
        "active": 0,
        "maximum_active": 0,
        "first_started": threading.Event(),
        "release_first": threading.Event(),
        "second_started": threading.Event(),
    }

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def get_issue(self, key: str) -> JiraIssue:
            assert key == "WORK-519"
            state["active"] += 1
            state["maximum_active"] = max(state["maximum_active"], state["active"])
            if not state["first_started"].is_set():
                state["first_started"].set()
                assert state["release_first"].wait(2)
            else:
                state["second_started"].set()
            state["active"] -= 1
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/519",
                status_name="Ready to Deploy",
            )

        def get_comments(self, key: str) -> list[str]:
            assert key == "WORK-519"
            return [
                "Tested and reviewed.",
                "Merged with [abcdef01|https://gitlab.example/group/repository/-/commit/abcdef0123456789abcdef0123456789abcdef01]",
            ]

        def add_comment(self, key: str, comment: str) -> None:
            raise AssertionError(f"already-commented issue must not be changed: {key} {comment}")

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            raise AssertionError(
                f"already-deployable issue must not be transitioned: "
                f"{key} {target_status} {current_status}"
            )

        def close(self) -> None:
            pass

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 519)
            return GitLabMergeRequest(
                "merged",
                "2026-08-30T10:00:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/519",
            )

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            raise AssertionError("merged issue must skip approval state")

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            assert (project_path, number) == ("group/repository", 519)
            return []

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            raise AssertionError("already-merged MR must not be merged")

        def close(self) -> None:
            pass

    def run() -> JiraIssue:
        with SessionLocal() as db:
            _seed_jira_config(db)
            db.commit()
            return ready_to_merge_review(
                "WORK-519",
                db,
                jira_client_factory=FakeJiraClient,
                gitlab_client_factory=FakeGitLabClient,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run)
        assert state["first_started"].wait(2)
        second = executor.submit(run)
        assert not state["second_started"].wait(0.1)
        state["release_first"].set()
        assert first.result().status_name == "Ready to Deploy"
        assert second.result().status_name == "Ready to Deploy"

    assert state["maximum_active"] == 1


def test_ready_to_merge_review_refuses_concurrent_terminal_jira_change() -> None:
    statuses = iter(("In Review", "Done"))
    transitions: list[str] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            del config

        def get_issue(self, key: str) -> JiraIssue:
            return JiraIssue(
                key=key,
                description="https://gitlab.example/group/repository/-/merge_requests/530",
                status_name=next(statuses),
            )

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            del key, current_status
            transitions.append(target_status)
            raise AssertionError("a terminal Jira status must not be changed")

        def add_comment(self, key: str, comment: str) -> None:
            raise AssertionError(f"Jira must not be commented: {key} {comment}")

        def close(self) -> None:
            pass

    class FakeGitLabClient:
        def __init__(self, config) -> None:
            del config

        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 530)
            return GitLabMergeRequest("opened", "2026-08-30T10:00:00Z")

        def get_merge_request_approval_state(
            self, project_path: str, number: int
        ) -> GitLabMergeRequestApprovalState:
            return GitLabMergeRequestApprovalState(approved=True)

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            return []

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            return GitLabMergeRequest(
                "merged",
                "2026-08-30T10:01:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/530",
            )

        def close(self) -> None:
            pass

    with SessionLocal() as db:
        _seed_jira_config(db, in_review_status="In Review", completed_statuses="Done")
        db.commit()
        with pytest.raises(JiraError, match="terminal status"):
            ready_to_merge_review(
                "WORK-530",
                db,
                jira_client_factory=FakeJiraClient,
                gitlab_client_factory=FakeGitLabClient,
            )

    assert transitions == []


def test_merge_revalidates_opened_selection_before_post() -> None:
    selection = MergeRequestSelection(
        selected={
            "repository": "repository",
            "number": 531,
            "url": "https://gitlab.example/group/repository/-/merge_requests/531",
            "state": "opened",
        },
        enabled=True,
        reason="Selected the only open MR; closed MRs were ignored.",
    )
    calls = {"gets": 0, "merges": 0}

    class FakeGitLabClient:
        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 531)
            calls["gets"] += 1
            return GitLabMergeRequest(
                "merged",
                "2026-08-30T10:00:00Z",
                merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                web_url="https://gitlab.example/group/repository/-/merge_requests/531",
            )

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            del project_path, number
            calls["merges"] += 1
            raise AssertionError("a freshly merged MR must not receive a merge POST")

    result = _merge_selected_merge_request("https://gitlab.example", selection, FakeGitLabClient())

    assert result.state == "merged"
    assert calls == {"gets": 1, "merges": 0}


def test_merge_timeout_then_retry_skips_post_after_remote_merge(monkeypatch) -> None:
    selection = MergeRequestSelection(
        selected={
            "repository": "repository",
            "number": 532,
            "url": "https://gitlab.example/group/repository/-/merge_requests/532",
            "state": "opened",
        },
        enabled=True,
        reason="Selected the only open MR; closed MRs were ignored.",
    )
    state = {"merged": False, "gets": 0, "merges": 0}

    class FakeGitLabClient:
        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 532)
            state["gets"] += 1
            if state["merged"]:
                return GitLabMergeRequest(
                    "merged",
                    "2026-08-30T10:01:00Z",
                    merge_commit_sha="abcdef0123456789abcdef0123456789abcdef01",
                    web_url="https://gitlab.example/group/repository/-/merge_requests/532",
                )
            return GitLabMergeRequest("opened", "2026-08-30T10:00:00Z")

        def merge_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            del project_path, number
            state["merges"] += 1
            return GitLabMergeRequest("opened", "2026-08-30T10:00:00Z")

    gitlab = FakeGitLabClient()
    monkeypatch.setattr("work_tickets.jira_service._MERGE_POLL_TIMEOUT_SECONDS", 0.0)
    with pytest.raises(JiraError, match="Timed out waiting"):
        _merge_selected_merge_request("https://gitlab.example", selection, gitlab)

    state["merged"] = True
    result = _merge_selected_merge_request("https://gitlab.example", selection, gitlab)

    assert result.state == "merged"
    assert state["merges"] == 1


def test_ambiguous_jira_comment_is_confirmed_without_second_post() -> None:
    comment = "Tested and reviewed."
    remote_comments: list[str] = []
    calls: list[str] = []

    class FakeJiraClient:
        def get_comments(self, key: str) -> list[str]:
            assert key == "WORK-535"
            calls.append("GET comments")
            return list(remote_comments)

        def add_comment(self, key: str, body: str) -> None:
            assert (key, body) == ("WORK-535", comment)
            calls.append("POST comment")
            remote_comments.append(body)
            raise JiraError("comment response was lost")

    jira = FakeJiraClient()
    _add_jira_comment_if_missing(jira, "WORK-535", comment)
    _add_jira_comment_if_missing(jira, "WORK-535", comment)

    assert remote_comments == [comment]
    assert calls == ["GET comments", "POST comment", "GET comments", "GET comments"]
    assert calls.count("POST comment") == 1


def test_jira_transition_ambiguous_mutation_is_confirmed_without_repeat() -> None:
    status = ["In Review"]
    calls: list[str] = []

    class FakeJiraClient:
        def get_issue(self, key: str) -> JiraIssue:
            calls.append("get")
            return JiraIssue(key=key, status_name=status[0])

        def transition_issue(self, key: str, target_status: str, *, current_status: str):
            del key, current_status
            calls.append("transition")
            status[0] = target_status
            raise JiraError("Jira transition response was lost")

    with SessionLocal() as db:
        config = _seed_jira_config(db, in_review_status="In Review")
        result = _transition_review_status(
            FakeJiraClient(),
            "WORK-533",
            config.ready_to_merge_status,
            config,
            allowed_sources={config.in_review_status},
        )

    assert result.status_name == "Ready to Merge"
    assert calls == ["get", "transition", "get"]


def test_ambiguous_discussion_mutations_are_confirmed_without_duplicates() -> None:
    selection = MergeRequestSelection(
        selected={
            "repository": "repository",
            "number": 534,
            "url": "https://gitlab.example/group/repository/-/merge_requests/534",
            "state": "opened",
        },
        enabled=True,
        reason="Selected the only open MR; closed MRs were ignored.",
    )
    state = {
        "body": None,
        "resolved": False,
        "note_posts": 0,
        "resolution_puts": 0,
        "calls": [],
    }

    class FakeGitLabClient:
        def get_merge_request(self, project_path: str, number: int) -> GitLabMergeRequest:
            assert (project_path, number) == ("group/repository", 534)
            state["calls"].append("GET merge request")
            return GitLabMergeRequest("opened", "2026-08-30T10:00:00Z")

        def get_merge_request_discussions(
            self, project_path: str, number: int
        ) -> list[GitLabMergeRequestDiscussion]:
            assert (project_path, number) == ("group/repository", 534)
            state["calls"].append("GET discussions")
            return [
                GitLabMergeRequestDiscussion(
                    "thread-1",
                    (
                        GitLabMergeRequestDiscussionNote(
                            1,
                            resolvable=True,
                            resolved=state["resolved"],
                            body=state["body"],
                        ),
                    ),
                )
            ]

        def add_merge_request_discussion_note(
            self, project_path: str, number: int, discussion_id: str, body: str
        ) -> None:
            assert (project_path, number, discussion_id, body) == (
                "group/repository",
                534,
                "thread-1",
                "Approved 👑",
            )
            state["calls"].append("POST note")
            state["note_posts"] += 1
            state["body"] = body
            raise GitLabError("note response was lost")

        def resolve_merge_request_discussion(
            self, project_path: str, number: int, discussion_id: str
        ) -> None:
            assert (project_path, number, discussion_id) == (
                "group/repository",
                534,
                "thread-1",
            )
            state["calls"].append("PUT resolution")
            state["resolution_puts"] += 1
            state["resolved"] = True
            raise GitLabError("resolution response was lost")

    result = _resolve_selected_merge_request_discussions(
        "https://gitlab.example", selection, FakeGitLabClient()
    )
    retry_result = _resolve_selected_merge_request_discussions(
        "https://gitlab.example", selection, FakeGitLabClient()
    )

    assert result is None
    assert retry_result is None
    assert state["body"] == "Approved 👑"
    assert state["resolved"] is True
    assert state["note_posts"] == 1
    assert state["resolution_puts"] == 1
    assert state["calls"] == [
        "GET merge request",
        "GET discussions",
        "POST note",
        "GET merge request",
        "GET discussions",
        "PUT resolution",
        "GET merge request",
        "GET discussions",
        "GET merge request",
        "GET discussions",
    ]


def test_reviews_frontend_has_navigation_refresh_and_item_error_state() -> None:
    app_source = (Path(__file__).parents[1] / "frontend" / "src" / "App.vue").read_text()

    assert 'label="Reviews"' in app_source
    assert 'fetch("/api/reviews")' in app_source
    assert 'label="Refresh"' in app_source
    assert "review.error" in app_source
    assert "Not in local tickets" in app_source
    assert "ready-to-merge" in app_source
    assert "reviewActionState" in app_source
    assert "reviewActionErrors" in app_source
    assert "review.merge_requests" in app_source
    assert "review.selected_merge_request" in app_source
    assert "review.ready_to_merge_enabled" in app_source
    assert "merge_request_selection_reason" in app_source
    assert "Detected merge requests" in app_source
    assert ':key="mergeRequest.url"' in app_source


def test_workflow_status_settings_are_present_in_frontend() -> None:
    app_source = (Path(__file__).parents[1] / "frontend" / "src" / "App.vue").read_text()

    assert 'v-model="settings.in_review_status"' in app_source
    assert 'v-model="settings.ready_to_merge_status"' in app_source
    assert 'v-model="settings.ready_to_deploy_status"' in app_source


def test_gitlab_settings_are_present_in_frontend() -> None:
    app_source = (Path(__file__).parents[1] / "frontend" / "src" / "App.vue").read_text()

    assert 'v-model="settings.gitlab_base_url"' in app_source
    assert 'v-model="settings.gitlab_token"' in app_source
    assert 'settings.value.gitlab_token = ""' in app_source


def test_refine_registry_reuses_key_replays_output_and_expires_idle_sessions(
    monkeypatch, tmp_path
) -> None:
    class FakeStream:
        def __init__(self) -> None:
            self.chunks: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def read(self, size: int) -> bytes:
            del size
            chunk = await self.chunks.get()
            return b"" if chunk is None else chunk

        def emit(self, chunk: bytes) -> None:
            self.chunks.put_nowait(chunk)

        def close(self) -> None:
            self.chunks.put_nowait(None)

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = FakeStdin()
            self.stdout = FakeStream()
            self.terminated = False
            self.finished = asyncio.Event()

        async def wait(self) -> int:
            await self.finished.wait()
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15
            self.stdout.close()
            self.finished.set()

        def kill(self) -> None:
            self.returncode = -9
            self.stdout.close()
            self.finished.set()

    class FakeStdin:
        def __init__(self) -> None:
            self.data: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.data.append(data)

        async def drain(self) -> None:
            pass

    class FakeWebSocket:
        def __init__(self) -> None:
            self.output: list[str] = []

        async def send_text(self, output: str) -> None:
            self.output.append(output)

        async def close(self, code: int = 1000) -> None:
            del code

    processes: list[FakeProcess] = []

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(
        "work_tickets.refine.asyncio.create_subprocess_exec", fake_create_subprocess_exec
    )

    async def exercise() -> None:
        registry = refine.RefineSessionRegistry(stale_after=0.01)
        first_client = FakeWebSocket()
        first_session, error = await registry.attach(
            "WORK-706", "Refine prompt", tmp_path, first_client
        )
        assert first_session is not None
        assert error is None
        assert len(processes) == 1

        processes[0].stdout.emit(b"buffered output")
        for _ in range(10):
            if first_client.output:
                break
            await asyncio.sleep(0)
        assert first_client.output == ["buffered output"]
        await first_session.send_input("user input")
        assert processes[0].stdin.data == [b"user input"]

        await registry.detach(first_session, first_client)
        second_client = FakeWebSocket()
        second_session, error = await registry.attach(
            "work-706", "A different prompt must not launch", tmp_path, second_client
        )
        assert second_session is first_session
        assert error is None
        assert second_client.output == ["buffered output"]
        assert len(processes) == 1

        await registry.detach(second_session, second_client)
        await asyncio.wait_for(first_session.finished.wait(), timeout=1)
        assert processes[0].terminated is True

    asyncio.run(exercise())


def test_refine_websocket_reports_opencode_start_failure(monkeypatch, tmp_path) -> None:
    project_directory = tmp_path / "refine-component-start-failure"
    project_directory.mkdir()

    async def fail_to_start(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise FileNotFoundError(2, "No such file or directory", "opencode")

    monkeypatch.setattr("work_tickets.refine.asyncio.create_subprocess_exec", fail_to_start)
    with SessionLocal() as db:
        config = _seed_jira_config(db)
        config.browser_base_url = "https://jira.example.test"
        config.local_projects_directory = str(tmp_path)
        component = Component(name=project_directory.name)
        ticket = Ticket(
            summary="Cannot start Refine",
            position=0,
            jira_issue_key="WORK-710",
            component=component.name,
        )
        db.add_all([component, ticket])
        db.commit()
        ticket_id = ticket.id

    with client.websocket_connect(f"/api/tickets/{ticket_id}/refine") as websocket:
        assert websocket.receive_text() == (
            "\r\n[Refine error] Could not start opencode: No such file or directory\r\n"
        )


def test_refine_registry_cancellation_releases_reservation_and_stops_startup(
    monkeypatch, tmp_path
) -> None:
    class FakeStream:
        def __init__(self) -> None:
            self.chunks: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def read(self, size: int) -> bytes:
            del size
            chunk = await self.chunks.get()
            return b"" if chunk is None else chunk

        def close(self) -> None:
            self.chunks.put_nowait(None)

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = None
            self.stdout = FakeStream()
            self.finished = asyncio.Event()

        async def wait(self) -> int:
            await self.finished.wait()
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15
            self.stdout.close()
            self.finished.set()

        def kill(self) -> None:
            self.returncode = -9
            self.stdout.close()
            self.finished.set()

    startup_started = asyncio.Event()
    processes: list[FakeProcess] = []

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        startup_started.set()
        await asyncio.Future[None]()
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(
        "work_tickets.refine.asyncio.create_subprocess_exec", fake_create_subprocess_exec
    )

    async def exercise() -> None:
        registry = refine.RefineSessionRegistry(stale_after=0.01)
        attach_task = asyncio.create_task(
            registry.attach("WORK-708", "Refine prompt", tmp_path, object())
        )
        await startup_started.wait()
        session = registry._sessions["WORK-708"]

        attach_task.cancel()
        try:
            await attach_task
        except asyncio.CancelledError:
            pass
        assert session._pending_clients == 0

        assert session._task is not None
        assert session._task.done()
        assert session.finished.is_set()
        assert registry._sessions == {}
        assert processes == []
        assert session._stale_task is None or session._stale_task.done()

    asyncio.run(exercise())


def test_refine_registry_cancellation_while_reserving_cleans_up_new_session(
    monkeypatch, tmp_path
) -> None:
    class FakeStream:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        async def read(self, size: int) -> bytes:
            del size
            await self.closed.wait()
            return b""

        def close(self) -> None:
            self.closed.set()

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = None
            self.stdout = FakeStream()
            self.terminated = False
            self.finished = asyncio.Event()

        async def wait(self) -> int:
            await self.finished.wait()
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15
            self.stdout.close()
            self.finished.set()

        def kill(self) -> None:
            self.returncode = -9
            self.stdout.close()
            self.finished.set()

    process = FakeProcess()

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(
        "work_tickets.refine.asyncio.create_subprocess_exec", fake_create_subprocess_exec
    )
    original_reserve_client = refine.RefineSession.reserve_client
    reservation_started = asyncio.Event()
    allow_reservation = asyncio.Event()

    async def blocked_reserve_client(session, reservation=None) -> bool:
        reservation_started.set()
        await allow_reservation.wait()
        return await original_reserve_client(session, reservation)

    monkeypatch.setattr(refine.RefineSession, "reserve_client", blocked_reserve_client)

    async def exercise() -> None:
        registry = refine.RefineSessionRegistry(stale_after=1)
        attach_task = asyncio.create_task(
            registry.attach("WORK-709", "Refine prompt", tmp_path, object())
        )
        await reservation_started.wait()
        session = registry._sessions["WORK-709"]
        await session._lock.acquire()
        allow_reservation.set()
        await asyncio.sleep(0)

        attach_task.cancel()
        session._lock.release()
        try:
            await attach_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("attach should be cancelled")

        await asyncio.wait_for(session.finished.wait(), timeout=1)
        assert process.terminated is True
        assert session._pending_clients == 0
        assert registry._sessions == {}

    asyncio.run(exercise())


def test_canonicalize_jira_key_is_used_for_storage_and_prompt() -> None:
    assert canonicalize_jira_key(" work-123 ") == "WORK-123"
    ticket = Ticket(summary="Canonical key", position=0)
    save_jira_issue(ticket, JiraIssue(key="work-123"), datetime.utcnow())
    assert ticket.jira_issue_key == "WORK-123"
    config = JiraConfig(browser_base_url="https://jira.example.test/context")
    assert refine.refine_prompt(ticket, config) == (
        "Refine https://jira.example.test/context/browse/WORK-123"
    )


def test_refine_registry_removes_sessions_after_process_exit(monkeypatch, tmp_path) -> None:
    class FakeStream:
        async def read(self, size: int) -> bytes:
            del size
            return b""

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = None
            self.stdout = FakeStream()
            self.finished = asyncio.Event()

        async def wait(self) -> int:
            await self.finished.wait()
            assert self.returncode is not None
            return self.returncode

        def finish(self) -> None:
            self.returncode = 0
            self.finished.set()

        def terminate(self) -> None:
            self.returncode = -15
            self.finished.set()

        def kill(self) -> None:
            self.returncode = -9
            self.finished.set()

    class FakeWebSocket:
        async def send_text(self, output: str) -> None:
            del output

        async def close(self, code: int = 1000) -> None:
            del code

    processes: list[FakeProcess] = []

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(
        "work_tickets.refine.asyncio.create_subprocess_exec", fake_create_subprocess_exec
    )

    async def exercise() -> None:
        registry = refine.RefineSessionRegistry(stale_after=1)
        second_client = FakeWebSocket()
        first_session, error = await registry.attach(
            "WORK-707", "Refine prompt", tmp_path, FakeWebSocket()
        )
        assert first_session is not None
        assert error is None
        processes[0].finish()
        await asyncio.wait_for(first_session.finished.wait(), timeout=1)

        second_session, error = await registry.attach(
            "WORK-707", "Refine prompt", tmp_path, second_client
        )
        assert second_session is not None
        assert second_session is not first_session
        assert error is None
        assert len(processes) == 2
        await registry.detach(second_session, second_client)
        await asyncio.wait_for(second_session.finished.wait(), timeout=2)

    asyncio.run(exercise())


def test_refine_websocket_launches_only_with_synced_jira_items(monkeypatch, tmp_path) -> None:
    class FakeStream:
        async def read(self, size: int) -> bytes:
            del size
            return b""

    class FakeStdin:
        def write(self, data: bytes) -> None:
            del data

        async def drain(self) -> None:
            pass

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = FakeStdin()
            self.stdout = FakeStream()

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    project_directory = tmp_path / "refine-component"
    project_directory.mkdir()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        calls.append((args, kwargs))
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        assert kwargs["stderr"] == asyncio.subprocess.STDOUT
        assert kwargs["cwd"] == str(project_directory)
        assert kwargs["env"] == os.environ
        return FakeProcess()

    monkeypatch.setattr(
        "work_tickets.refine.asyncio.create_subprocess_exec", fake_create_subprocess_exec
    )
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        if config is None:
            config = JiraConfig(
                id=1,
                base_url="https://api.example.test",
                browser_base_url="",
                email="person@example.test",
                api_token="test-token",
                project_key="WORK",
                issue_type="Task",
                completed_statuses="Done",
            )
            db.add(config)
        config.browser_base_url = "https://jira.example.test/context"
        config.local_projects_directory = str(tmp_path)
        component = Component(name="refine-component")
        parent = Ticket(
            summary="Refine parent",
            position=0,
            jira_issue_key="WORK-700",
            component=component.name,
        )
        child = Ticket(
            summary="Refine child",
            position=0,
            jira_issue_key="WORK-701",
            component=component.name,
            parent=parent,
        )
        unsynced = Ticket(summary="Not synced", position=1)
        db.add_all([component, parent, child, unsynced])
        db.commit()
        parent_id = parent.id
        child_id = child.id
        unsynced_id = unsynced.id

    with client.websocket_connect(f"/api/tickets/{parent_id}/refine") as websocket:
        assert websocket.receive_text() == "\r\n[Refine exited with code 0]\r\n"
    with client.websocket_connect(f"/api/tickets/{child_id}/refine") as websocket:
        assert websocket.receive_text() == "\r\n[Refine exited with code 0]\r\n"
    with client.websocket_connect(f"/api/tickets/{unsynced_id}/refine") as websocket:
        assert websocket.receive_text() == (
            "\r\n[Refine error] Refine is available only for tickets synced to Jira.\r\n"
        )

    assert [call[0] for call in calls] == [
        ("opencode", "--prompt", "Refine https://jira.example.test/context/browse/WORK-700"),
        ("opencode", "--prompt", "Refine https://jira.example.test/context/browse/WORK-701"),
    ]


def test_refine_websocket_reports_malformed_browser_url() -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        if config is None:
            config = JiraConfig(
                id=1,
                base_url="https://api.example.test",
                browser_base_url="https://[invalid",
                email="person@example.test",
                api_token="test-token",
                project_key="WORK",
                issue_type="Task",
                completed_statuses="Done",
            )
            db.add(config)
        else:
            config.browser_base_url = "https://[invalid"
        ticket = Ticket(summary="Malformed browser URL", position=0, jira_issue_key="WORK-702")
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    with client.websocket_connect(f"/api/tickets/{ticket_id}/refine") as websocket:
        assert websocket.receive_text() == (
            "\r\n[Refine error] The configured Jira browser URL is invalid.\r\n"
        )


def test_refine_websocket_reports_malformed_component_before_launch(tmp_path) -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.browser_base_url = "https://jira.example.test"
        config.local_projects_directory = str(tmp_path)
        ticket = Ticket(
            summary="Malformed local component",
            position=0,
            jira_issue_key="WORK-702A",
            component="../outside",
        )
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    with client.websocket_connect(f"/api/tickets/{ticket_id}/refine") as websocket:
        assert websocket.receive_text() == (
            "\r\n[Refine error] The ticket component is not a valid local project name.\r\n"
        )


def test_refine_websocket_rejects_component_symlink_outside_root(tmp_path) -> None:
    local_projects = tmp_path / "projects"
    local_projects.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (local_projects / "linked-component").symlink_to(outside, target_is_directory=True)

    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.browser_base_url = "https://jira.example.test"
        config.local_projects_directory = str(local_projects)
        ticket = Ticket(
            summary="Symlinked local component",
            position=0,
            jira_issue_key="WORK-702C",
            component="linked-component",
        )
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    with client.websocket_connect(f"/api/tickets/{ticket_id}/refine") as websocket:
        assert websocket.receive_text() == (
            "\r\n[Refine error] The ticket component is not a valid local project name.\r\n"
        )


def test_refine_websocket_reports_working_directory_resolution_errors(
    monkeypatch, tmp_path
) -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.browser_base_url = "https://jira.example.test"
        config.local_projects_directory = str(tmp_path)
        ticket = Ticket(
            summary="Unresolvable local component",
            position=0,
            jira_issue_key="WORK-702B",
            component="unresolvable-component",
        )
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    def raise_os_error(self, strict: bool = False) -> Path:
        del self, strict
        raise OSError("path cannot be resolved")

    monkeypatch.setattr(Path, "resolve", raise_os_error)
    with client.websocket_connect(f"/api/tickets/{ticket_id}/refine") as websocket:
        assert websocket.receive_text() == (
            "\r\n[Refine error] The local project directory could not be resolved.\r\n"
        )


def test_refine_websocket_reports_expanduser_runtime_errors(monkeypatch, tmp_path) -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.browser_base_url = "https://jira.example.test"
        config.local_projects_directory = str(tmp_path)
        ticket = Ticket(
            summary="Malformed stored local root",
            position=0,
            jira_issue_key="WORK-702D",
            component="runtime-error-component",
        )
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    def raise_runtime_error(self) -> Path:
        del self
        raise RuntimeError("home directory could not be determined")

    monkeypatch.setattr(Path, "expanduser", raise_runtime_error)
    with client.websocket_connect(f"/api/tickets/{ticket_id}/refine") as websocket:
        assert websocket.receive_text() == (
            "\r\n[Refine error] The local project directory could not be resolved.\r\n"
        )


def test_refine_websocket_reports_missing_local_project_directory(tmp_path) -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.browser_base_url = "https://jira.example.test"
        config.local_projects_directory = str(tmp_path)
        component = Component(name="missing-refine-component")
        ticket = Ticket(
            summary="Missing local project",
            position=0,
            jira_issue_key="WORK-703",
            component=component.name,
        )
        db.add_all([component, ticket])
        db.commit()
        ticket_id = ticket.id

    with client.websocket_connect(f"/api/tickets/{ticket_id}/refine") as websocket:
        assert websocket.receive_text() == (
            "\r\n[Refine error] The local project directory for component "
            "'missing-refine-component' does not exist.\r\n"
        )


def test_refine_websocket_reports_missing_local_projects_root(tmp_path) -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.browser_base_url = "https://jira.example.test"
        config.local_projects_directory = str(tmp_path / "missing-root")
        component = Component(name="missing-root-component")
        ticket = Ticket(
            summary="Missing local root",
            position=0,
            jira_issue_key="WORK-704",
            component=component.name,
        )
        db.add_all([component, ticket])
        db.commit()
        ticket_id = ticket.id

    with client.websocket_connect(f"/api/tickets/{ticket_id}/refine") as websocket:
        assert websocket.receive_text() == (
            "\r\n[Refine error] The configured local projects directory does not exist.\r\n"
        )


def test_refine_websocket_reports_missing_component_before_launch(tmp_path) -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.browser_base_url = "https://jira.example.test"
        config.local_projects_directory = str(tmp_path)
        ticket = Ticket(summary="Missing local component", position=0, jira_issue_key="WORK-705")
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    with client.websocket_connect(f"/api/tickets/{ticket_id}/refine") as websocket:
        assert websocket.receive_text() == (
            "\r\n[Refine error] Assign a local component before using Refine.\r\n"
        )


def test_refine_frontend_uses_xterm_and_only_renders_for_jira_keys() -> None:
    frontend_source = Path(__file__).parents[1] / "frontend" / "src"
    terminal_source = (frontend_source / "components" / "RefineTerminal.vue").read_text()
    ticket_card_source = (frontend_source / "components" / "TicketCard.vue").read_text()

    assert 'from "@xterm/xterm"' in terminal_source
    assert "acquireRefineSession(sessionIdentity(), socketUrl())" in terminal_source
    assert 'v-if="ticket.jira_issue_key"' in terminal_source
    assert ':disabled="!ticket.component || !browserBaseUrl"' in terminal_source
    assert '<RefineTerminal :ticket="subtask"' in ticket_card_source
    assert "onMounted(() =>" in terminal_source
    coordinator_source = (
        frontend_source / "components" / "RefineSessionCoordinator.vue"
    ).read_text()
    app_source = (frontend_source / "App.vue").read_text()
    assert "RefineSessionCoordinator" in coordinator_source
    assert '<RefineSessionCoordinator :tickets="state.tickets" />' in app_source


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
            "notes": "Keep this local note",
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
        assert parent.notes == "Keep this local note"
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
    create_payload = json.loads(requests[0].read())
    assert create_payload["fields"]["project"] == {"key": "WORK"}
    assert "notes" not in create_payload["fields"]


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
            assert "notes" not in payload["fields"]
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
            notes="Keep this local note",
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
        assert parent.notes == "Keep this local note"
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
