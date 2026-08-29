import atexit
import json
import os
import tempfile
from datetime import date
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

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


def test_homepage_is_available() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert '<div id="app"></div>' in response.text
    assert 'type="module"' in response.text


def test_spa_state_api_serializes_tickets_categories_and_jira_config() -> None:
    with SessionLocal() as db:
        category = Category(name="API category")
        ticket = Ticket(summary="API parent", position=0, category=category)
        Ticket(summary="API child", position=0, parent=ticket)
        db.add_all([category, ticket])
        db.commit()

    response = client.get("/api/state")

    assert response.status_code == 200
    result = response.json()
    assert {item["name"] for item in result["categories"]} >= {"API category"}
    parent = next(item for item in result["tickets"] if item["summary"] == "API parent")
    assert parent["category_name"] == "API category"
    assert parent["subtasks"][0]["summary"] == "API child"


def test_spa_has_distinct_hash_navigation_pages() -> None:
    page = client.get("/")

    assert page.status_code == 200
    assert "/src/main.ts" not in page.text
    assert "Tickets" not in page.text
    assert (Path(__file__).parents[1] / "work_tickets" / "static" / "assets").exists()


def test_ticket_pages_remove_overview_and_keep_focus_and_queue() -> None:
    spa_page = client.get("/")
    assert spa_page.status_code == 200
    asset_dir = Path(__file__).parents[1] / "work_tickets" / "static" / "assets"
    spa_bundle = next(asset_dir.glob("index-*.js")).read_text()
    assert "OVERVIEW" not in spa_bundle
    assert "Ticket command center" not in spa_bundle
    assert "FOCUS" in spa_bundle
    assert "QUEUE" in spa_bundle
    assert "All tickets" in spa_bundle

    legacy_page = client.get("/legacy")
    assert legacy_page.status_code == 200
    assert "Overview" not in legacy_page.text
    assert "<h2>Today</h2>" in legacy_page.text
    assert "<h2>All tickets</h2>" in legacy_page.text


def test_homepage_uses_wider_responsive_layout() -> None:
    page = client.get("/legacy")

    assert page.status_code == 200
    assert "body { max-width: 1400px;" in page.text
    assert "grid-template-columns:minmax(0, 1.7fr) minmax(360px, 1fr);" in page.text
    assert ".grid { grid-template-columns:1fr; }" in page.text


def test_category_filter_renders_category_metadata_for_both_ticket_sections() -> None:
    with SessionLocal() as db:
        category = Category(name="Filter category")
        categorized = Ticket(
            summary="Filter categorized ticket",
            planned_date=date.today(),
            position=0,
            category=category,
        )
        uncategorized = Ticket(summary="Filter uncategorized ticket", position=1)
        db.add_all([category, categorized, uncategorized])
        db.commit()
        category_id = category.id
        categorized_id = categorized.id
        uncategorized_id = uncategorized.id

    page = client.get("/legacy")

    assert page.status_code == 200
    assert 'id="category-filter"' in page.text
    assert 'aria-controls="ticket-lists"' in page.text
    assert f'<option value="{category_id}">Filter category</option>' in page.text
    assert page.text.count(f'data-category-id="{category_id}"') == 2
    assert page.text.count('data-category-id=""') == 1
    assert page.text.count("data-filterable-ticket\n") >= 3
    assert 'data-ticket-section="today"' in page.text
    assert 'data-ticket-section="all"' in page.text
    assert "data-filter-empty hidden" in page.text
    assert 'categoryFilter?.addEventListener("change", applyCategoryFilter)' in page.text
    assert "Showing ${visibleTickets} of ${totalTickets} tickets" in page.text
    assert page.text.index(f'data-ticket-id="{categorized_id}"') < page.text.index(
        f'data-ticket-id="{uncategorized_id}"'
    )


def test_ticket_controls_use_compact_accessible_actions() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="Compact controls", planned_date=date.today(), position=0)
        subtask = Ticket(summary="Compact subtask", position=0, parent=ticket)
        db.add_all([ticket, subtask])
        db.commit()
        ticket_id = ticket.id
        subtask_id = subtask.id

    page = client.get("/legacy")

    assert page.status_code == 200
    assert f'action="/tickets/{ticket_id}/complete"' in page.text
    assert f'action="/tickets/{ticket_id}/sync"' in page.text
    assert f'action="/tickets/{ticket_id}/delete"' in page.text
    assert f'action="/subtasks/{subtask_id}/complete"' in page.text
    assert f'action="/subtasks/{subtask_id}/move-up"' in page.text
    assert f'action="/subtasks/{subtask_id}/move-down"' in page.text
    assert f'action="/subtasks/{subtask_id}/delete"' in page.text
    assert 'class="compact-control completion-control"' in page.text
    assert 'aria-label="Sync Compact controls and its subtasks to Jira"' in page.text
    assert 'aria-label="Mark Compact subtask as done"' in page.text
    assert 'aria-label="Move up Compact subtask"' in page.text
    assert 'aria-label="Delete Compact subtask"' in page.text
    assert 'aria-label="Delete Compact controls"' in page.text


def test_subtask_move_controls_have_no_refresh_hooks() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Hook parent", position=0)
        first = Ticket(summary="Hook first", position=0, parent=parent)
        second = Ticket(summary="Hook second", position=1, parent=parent)
        db.add_all([parent, first, second])
        db.commit()

    page = client.get("/legacy")

    assert page.status_code == 200
    assert 'class="subtasks" data-subtasks' in page.text
    assert f'data-subtask-id="{first.id}"' in page.text
    assert f'data-subtask-id="{second.id}"' in page.text
    assert page.text.count('class="move-subtask-form"') >= 4
    assert 'data-move-direction="up"' in page.text
    assert 'data-move-direction="down"' in page.text
    assert 'headers: { Accept: "application/json" }' in page.text
    assert 'id="move-status"' in page.text


def test_subtask_move_returns_json_order_for_enhanced_requests() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="JSON ordering parent", position=1)
        first = Ticket(summary="JSON first", position=0, parent=parent)
        second = Ticket(summary="JSON second", position=1, parent=parent)
        db.add_all([parent, first, second])
        db.commit()
        second_id = second.id
        first_id = first.id
        parent_id = parent.id

    response = client.post(
        f"/subtasks/{second_id}/move-up",
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "ok": True,
        "message": "Subtask JSON second moved up.",
        "parent_id": parent_id,
        "order": [second_id, first_id],
    }
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        assert parent is not None
        assert [subtask.id for subtask in parent.subtasks] == [second_id, first_id]


def test_subtask_drag_hooks_keep_keyboard_move_fallback() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Drag parent", position=0)
        first = Ticket(summary="Drag first", position=0, parent=parent)
        second = Ticket(summary="Drag second", position=1, parent=parent)
        db.add_all([parent, first, second])
        db.commit()

    page = client.get("/legacy")

    assert page.status_code == 200
    assert 'class="subtask" data-subtask-id=' in page.text
    assert 'draggable="true"' in page.text
    assert 'title="Drag to reorder"' in page.text
    assert "Drag rows to reorder; use arrows with a keyboard" in page.text
    assert 'data-move-direction="up"' in page.text
    assert 'data-move-direction="down"' in page.text
    assert "/move-to" in page.text
    assert "new URLSearchParams({ target_index: String(targetIndex) })" in page.text
    assert "container.insertBefore(subtask, subtaskForm)" in page.text


def test_subtask_drag_move_returns_json_order_and_persists_positions() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Drag ordering parent", position=0)
        first = Ticket(summary="Drag first", position=0, parent=parent)
        middle = Ticket(summary="Drag middle", position=1, parent=parent)
        last = Ticket(summary="Drag last", position=2, parent=parent)
        db.add_all([parent, first, middle, last])
        db.commit()
        middle_id = middle.id
        first_id = first.id
        last_id = last.id
        parent_id = parent.id

    response = client.post(
        f"/subtasks/{middle_id}/move-to",
        data={"target_index": "2"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "message": "Subtask Drag middle reordered.",
        "parent_id": parent_id,
        "order": [first_id, last_id, middle_id],
    }
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        assert parent is not None
        assert [(subtask.id, subtask.position) for subtask in parent.subtasks] == [
            (first_id, 0),
            (last_id, 1),
            (middle_id, 2),
        ]


def test_subtask_drag_move_rejects_invalid_position_without_reordering() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Invalid drag parent", position=0)
        first = Ticket(summary="Invalid drag first", position=0, parent=parent)
        second = Ticket(summary="Invalid drag second", position=1, parent=parent)
        db.add_all([parent, first, second])
        db.commit()
        second_id = second.id
        parent_id = parent.id

    response = client.post(
        f"/subtasks/{second_id}/move-to",
        data={"target_index": "-1"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "Subtask target position is invalid.",
    }
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        assert parent is not None
        assert [subtask.id for subtask in parent.subtasks] == [first.id, second.id]


def test_top_level_ticket_reorder_controls_are_only_in_all_tickets() -> None:
    with SessionLocal() as db:
        first = Ticket(summary="Priority first", position=0, planned_date=date.today())
        second = Ticket(summary="Priority second", position=1, planned_date=date.today())
        db.add_all([first, second])
        db.commit()
        first_id = first.id
        second_id = second.id

    page = client.get("/legacy")

    assert page.status_code == 200
    assert page.text.count(f'id="ticket-{first_id}"') == 1
    assert page.text.count(f'id="ticket-{second_id}"') == 1
    assert page.text.count('class="ticket"') >= 2
    assert f'action="/tickets/{first_id}/move-up"' in page.text
    assert f'action="/tickets/{second_id}/move-down"' in page.text
    assert 'class="ticket-drag-handle"' in page.text
    assert 'title="Drag to reorder"' in page.text
    ticket_start = page.text.index(f'<article\n  id="ticket-{first_id}"')
    ticket_opening_tag = page.text[ticket_start : page.text.index(">", ticket_start) + 1]
    assert 'draggable="true"' not in ticket_opening_tag
    assert 'class="ticket-drag-handle"' in page.text[ticket_start:]
    assert 'title="Drag to reorder"\n        draggable="true"' in page.text[ticket_start:]
    today_section = page.text.split("<h2>Today</h2>", 1)[1].split("</section>", 1)[0]
    assert "data-top-level-ticket" not in today_section
    assert "Drag tickets to reorder; use arrows with a keyboard" in page.text


def test_top_level_ticket_drag_move_returns_fragment_and_persists_priority() -> None:
    with SessionLocal() as db:
        first = Ticket(summary="Priority one", position=0)
        middle = Ticket(summary="Priority two", position=1)
        last = Ticket(summary="Priority three", position=2)
        parent = Ticket(summary="Nested parent", position=3)
        child = Ticket(summary="Nested child", position=0, parent=parent)
        db.add_all([first, middle, last, parent, child])
        db.commit()
        first_id = first.id
        middle_id = middle.id
        last_id = last.id
        parent_id = parent.id
        child_id = child.id

    with SessionLocal() as db:
        initial_tickets = list(
            db.scalars(
                select(Ticket)
                .where(Ticket.parent_id.is_(None))
                .order_by(Ticket.position, Ticket.id)
            )
        )
        initial_order = [ticket.id for ticket in initial_tickets]
    current_index = initial_order.index(middle_id)
    target_index = current_index + 1

    response = client.post(
        f"/tickets/{middle_id}/move-to",
        data={"target_index": str(target_index)},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["message"] == "Ticket Priority two reordered."
    assert result["target"] == "ticket-lists"
    expected_order = initial_order.copy()
    expected_order.pop(current_index)
    expected_order.insert(target_index, middle_id)
    assert result["order"] == expected_order
    assert "Priority three" in result["html"]
    with SessionLocal() as db:
        tickets = list(
            db.scalars(
                select(Ticket)
                .where(Ticket.parent_id.is_(None))
                .order_by(Ticket.position, Ticket.id)
            )
        )
        relevant_tickets = [
            ticket for ticket in tickets if ticket.id in {first_id, middle_id, last_id, parent_id}
        ]
        assert [ticket.id for ticket in relevant_tickets] == [
            first_id,
            last_id,
            middle_id,
            parent_id,
        ]
        assert [ticket.position for ticket in relevant_tickets] == sorted(
            ticket.position for ticket in relevant_tickets
        )
        nested = db.get(Ticket, child_id)
        assert nested is not None
        assert nested.parent_id == parent_id
        assert nested.position == 0


def test_top_level_ticket_reorder_rejects_invalid_and_subtask_targets() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Move parent", position=0)
        child = Ticket(summary="Move child", position=0, parent=parent)
        db.add_all([parent, child])
        db.commit()
        parent_id = parent.id
        child_id = child.id

    invalid = client.post(
        f"/tickets/{parent_id}/move-to",
        data={"target_index": "-1"},
        headers={"Accept": "application/json"},
    )
    child_response = client.post(
        f"/tickets/{child_id}/move-down",
        headers={"Accept": "application/json"},
    )

    assert invalid.status_code == 422
    assert invalid.json() == {"ok": False, "message": "Ticket target position is invalid."}
    assert child_response.status_code == 400
    assert child_response.json() == {
        "ok": False,
        "message": "Subtasks cannot be reordered with top-level tickets.",
    }


def test_ticket_and_subtask_forms_have_no_refresh_hooks() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="AJAX hooks", position=0)
        subtask = Ticket(summary="AJAX subtask hooks", position=0, parent=ticket)
        db.add_all([ticket, subtask])
        db.commit()
        ticket_id = ticket.id
        subtask_id = subtask.id

    page = client.get("/legacy")

    assert page.status_code == 200
    assert page.text.count("data-ajax-form") >= 3
    assert 'data-response-target="ticket-lists"' in page.text
    assert f'data-response-target="ticket-{ticket_id}"' in page.text
    assert f'action="/tickets/{ticket_id}"' in page.text
    assert f'action="/tickets/{ticket_id}/subtasks"' in page.text
    assert f'action="/subtasks/{subtask_id}"' in page.text
    assert 'headers: { Accept: "application/json" }' in page.text
    assert "body: new FormData(form)" in page.text
    assert "targetElement.outerHTML = result.html" in page.text


def test_ticket_and_subtask_mutations_return_fragments_and_persist() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="Original parent", position=0)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    enhanced_headers = {"Accept": "application/json"}
    create_response = client.post(
        "/tickets",
        data={
            "summary": "<script>unsafe</script> parent",
            "description": "Parent details",
            "planned_date": "2026-08-24",
        },
        headers=enhanced_headers,
    )

    assert create_response.status_code == 200
    create_result = create_response.json()
    assert create_result["ok"] is True
    assert create_result["target"] == "ticket-lists"
    assert "&lt;script&gt;unsafe&lt;/script&gt; parent" in create_result["html"]
    with SessionLocal() as db:
        created = db.scalar(
            select(Ticket).where(Ticket.summary == "<script>unsafe</script> parent")
        )
        assert created is not None
        assert created.planned_date == date(2026, 8, 24)

    update_response = client.post(
        f"/tickets/{ticket_id}",
        data={
            "summary": "Updated parent",
            "description": "Updated parent details",
            "planned_date": "2026-08-25",
        },
        headers=enhanced_headers,
    )
    assert update_response.status_code == 200
    assert update_response.json()["target"] == "ticket-lists"
    assert "Updated parent" in update_response.json()["html"]

    create_subtask_response = client.post(
        f"/tickets/{ticket_id}/subtasks",
        data={
            "summary": "New child",
            "description": "Child details",
            "planned_date": "2026-08-26",
        },
        headers=enhanced_headers,
    )
    assert create_subtask_response.status_code == 200
    create_subtask_result = create_subtask_response.json()
    assert create_subtask_result["target"] == f"ticket-{ticket_id}"
    assert "New child" in create_subtask_result["html"]
    with SessionLocal() as db:
        subtask = db.scalar(select(Ticket).where(Ticket.summary == "New child"))
        assert subtask is not None
        subtask_id = subtask.id

    update_subtask_response = client.post(
        f"/subtasks/{subtask_id}",
        data={
            "summary": "Updated child",
            "description": "Updated child details",
            "planned_date": "2026-08-27",
        },
        headers=enhanced_headers,
    )
    assert update_subtask_response.status_code == 200
    assert update_subtask_response.json()["target"] == f"ticket-{ticket_id}"
    assert "Updated child" in update_subtask_response.json()["html"]
    with SessionLocal() as db:
        updated_parent = db.get(Ticket, ticket_id)
        updated_subtask = db.get(Ticket, subtask_id)
        assert updated_parent is not None
        assert updated_subtask is not None
        assert updated_parent.summary == "Updated parent"
        assert updated_parent.description == "Updated parent details"
        assert updated_parent.planned_date == date(2026, 8, 25)
        assert updated_subtask.summary == "Updated child"
        assert updated_subtask.description == "Updated child details"
        assert updated_subtask.planned_date == date(2026, 8, 27)
        assert updated_subtask.parent_id == ticket_id


def test_enhanced_mutations_return_validation_errors_without_persisting() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="Validation AJAX parent", position=1)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    response = client.post(
        f"/tickets/{ticket_id}/subtasks",
        data={"summary": " ", "planned_date": "not-a-date"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "message": "Subtask summary is required."}
    with SessionLocal() as db:
        assert db.scalar(select(Ticket).where(Ticket.parent_id == ticket_id)) is None


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
    assert "Prepare agenda" in client.get("/legacy").text


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


def test_create_ticket_imports_jira_issue_and_subtasks_with_local_fields(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.browser_base_url == "https://jira.example.test"

        def get_issue_with_subtasks(self, key: str) -> JiraIssueWithSubtasks:
            assert key == "SCRUM-505"
            return JiraIssueWithSubtasks(
                issue=JiraIssue(
                    key=key,
                    summary="Imported parent",
                    description="Imported details",
                    status_name="In Progress",
                ),
                subtasks=(
                    JiraIssue(
                        key="SCRUM-506",
                        summary="Imported child",
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
        if config is None:
            db.add(
                JiraConfig(
                    id=1,
                    base_url="https://jira.example.test",
                    browser_base_url="https://jira.example.test",
                    email="person@example.test",
                    api_token="test-token",
                    project_key="SCRUM",
                    issue_type="Task",
                )
            )
        else:
            config.base_url = "https://jira.example.test"
            config.browser_base_url = "https://jira.example.test"
            config.project_key = "SCRUM"
        category = Category(name="Import category")
        db.add(category)
        db.commit()
        category_id = category.id

    response = client.post(
        "/tickets",
        data={
            "summary": "https://jira.example.test/browse/scrum-505",
            "planned_date": "2026-08-30",
            "category_id": str(category_id),
            "description": "Ignored local description",
        },
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["target"] == "ticket-lists"
    assert "Imported parent" in result["html"]
    with SessionLocal() as db:
        parent = db.scalar(select(Ticket).where(Ticket.jira_issue_key == "SCRUM-505"))
        child = db.scalar(select(Ticket).where(Ticket.jira_issue_key == "SCRUM-506"))
        assert parent is not None
        assert child is not None
        assert parent.summary == "Imported parent"
        assert parent.description == "Imported details"
        assert parent.planned_date == date(2026, 8, 30)
        assert parent.category_id == category_id
        assert parent.jira_status_name == "In Progress"
        assert child.parent_id == parent.id
        assert child.summary == "Imported child"
        assert child.description == "Child details"
        assert child.position == 0
        assert child.jira_status_name == "To Do"


def test_create_ticket_requires_jira_configuration_for_import() -> None:
    with SessionLocal() as db:
        db.query(JiraConfig).delete()
        db.commit()

    response = client.post(
        "/tickets",
        data={"jira_reference": "SCRUM-404"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "message": "Jira is not configured. Configure Jira before importing.",
    }


def test_ticket_forms_group_summary_date_and_category_responsively() -> None:
    with SessionLocal() as db:
        category = Category(name="Form layout category")
        ticket = Ticket(summary="Form layout ticket", position=0, category=category)
        db.add_all([category, ticket])
        db.commit()
        ticket_id = ticket.id

    page = client.get("/legacy")

    create_form = page.text.split('<form method="post" action="/tickets" data-ajax-form>', 1)[
        1
    ].split("</form>", 1)[0]
    create_row = create_form.split('<div class="form-field-row create-fields">', 1)[1].split(
        "</div>", 1
    )[0]
    assert create_form.count('class="form-field-row create-fields"') == 1
    assert create_row.index('name="summary"') < create_row.index('name="planned_date"')
    assert create_row.index('name="planned_date"') < create_row.index('name="category_id"')
    assert 'name="description"' not in create_row

    edit_form = page.text.split(f'action="/tickets/{ticket_id}"', 1)[1].split("</form>", 1)[0]
    edit_row = edit_form.split('<div class="form-field-row edit-fields">', 1)[1].split("</div>", 1)[
        0
    ]
    assert edit_form.count('class="form-field-row edit-fields"') == 1
    assert edit_row.index('name="summary"') < edit_row.index('name="planned_date"')
    assert 'name="description"' not in edit_row
    assert "Category: Form layout category" in edit_form


def test_ticket_date_fields_offer_today_quick_actions() -> None:
    with SessionLocal() as db:
        parent = Ticket(
            summary="Today action parent",
            planned_date=date(2026, 8, 28),
            position=0,
        )
        subtask = Ticket(
            summary="Today action subtask",
            planned_date=date(2026, 8, 27),
            position=0,
            parent=parent,
        )
        db.add(parent)
        db.commit()
        parent_id = parent.id
        subtask_id = subtask.id

    page = client.get("/legacy")

    assert page.status_code == 200
    create_form = page.text.split('<form method="post" action="/tickets" data-ajax-form>', 1)[
        1
    ].split("</form>", 1)[0]
    edit_form = page.text.split(f'action="/tickets/{parent_id}"', 1)[1].split("</form>", 1)[0]
    subtask_edit_form = page.text.split(f'action="/subtasks/{subtask_id}"', 1)[1].split(
        "</form>", 1
    )[0]
    new_subtask_form = page.text.split(f'action="/tickets/{parent_id}/subtasks"', 1)[1].split(
        "</form>", 1
    )[0]

    for form in (create_form, edit_form, subtask_edit_form, new_subtask_form):
        assert form.count("data-today-date") == 1
        assert form.count("data-clear-date") == 1
        assert form.index('name="planned_date"') < form.index("data-today-date")
        assert form.index("data-today-date") < form.index("data-clear-date")
        assert 'type="button"' in form
        assert 'aria-label="Set planned date to today"' in form
        assert 'aria-label="Remove ' in form
    assert 'value="2026-08-28"' in edit_form
    assert 'value="2026-08-27"' in subtask_edit_form
    assert 'data-clear-date aria-label="Remove planned date"' in edit_form
    assert 'data-clear-date aria-label="Remove subtask planned date"' in subtask_edit_form
    assert 'data-clear-date aria-label="Remove planned date" disabled' in create_form
    clear_new_subtask_date = 'data-clear-date aria-label="Remove new subtask planned date" disabled'
    assert clear_new_subtask_date in new_subtask_form
    assert "function localDateValue()" in page.text
    assert "input.value = localDateValue();" in page.text
    assert 'if (button.matches("[data-today-date]")) input.value = localDateValue();' in page.text
    assert 'else input.value = "";' in page.text
    assert 'input.dispatchEvent(new Event("input", { bubbles: true }));' in page.text
    assert 'input.dispatchEvent(new Event("change", { bubbles: true }));' in page.text
    assert "clearButton.disabled = !input?.value;" in page.text
    reset_state = (
        'form.reset();\n             updateDateControl(form.querySelector(".date-control"));'
    )
    assert reset_state in page.text


def test_edit_cannot_change_a_ticket_category() -> None:
    with SessionLocal() as db:
        original = Category(name="Original edit category")
        replacement = Category(name="Replacement edit category")
        db.add_all([original, replacement])
        db.flush()
        ticket = Ticket(summary="Categorized ticket", position=1, category=original)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id
        original_id = original.id
        replacement_id = replacement.id

    page = client.get("/legacy")
    edit_form = page.text.split(f'action="/tickets/{ticket_id}"', 1)[1].split("</form>", 1)[0]
    assert 'name="category_id"' not in edit_form
    assert "Category: Original edit category" in edit_form

    response = client.post(
        f"/tickets/{ticket_id}",
        data={
            "summary": "Categorized ticket updated",
            "description": "Updated details",
            "category_id": str(replacement_id),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with SessionLocal() as db:
        updated = db.get(Ticket, ticket_id)
        assert updated is not None
        assert updated.category_id == original_id
        assert updated.category_id != replacement_id


def test_edit_keeps_an_uncategorized_ticket_uncategorized() -> None:
    with SessionLocal() as db:
        category = Category(name="Category for uncategorized edit")
        ticket = Ticket(summary="Uncategorized ticket", position=2)
        db.add_all([category, ticket])
        db.commit()
        ticket_id = ticket.id
        category_id = category.id

    page = client.get("/legacy")
    edit_form = page.text.split(f'action="/tickets/{ticket_id}"', 1)[1].split("</form>", 1)[0]
    assert 'name="category_id"' not in edit_form
    assert "Category: Uncategorized" in edit_form

    response = client.post(
        f"/tickets/{ticket_id}",
        data={
            "summary": "Uncategorized ticket updated",
            "category_id": str(category_id),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with SessionLocal() as db:
        updated = db.get(Ticket, ticket_id)
        assert updated is not None
        assert updated.category_id is None


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

    page = client.get("/legacy")
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
    page = client.get("/legacy")
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

    state = client.get("/api/state").json()
    relevant_ids = {
        first_id,
        second_id,
        done_id,
    }
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


def test_done_tickets_cannot_be_edited_or_reordered() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="Locked completed ticket", position=0, local_completed=True)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    update_response = client.put(
        f"/api/tickets/{ticket_id}",
        json={"summary": "Changed", "description": "Changed", "planned_date": None},
    )
    move_response = client.post(
        f"/api/tickets/{ticket_id}/move",
        params={"target_index": 0},
    )

    assert update_response.status_code == 400
    assert update_response.json() == {
        "ok": False,
        "message": "Done tickets can only be marked active.",
    }
    assert move_response.status_code == 400
    assert move_response.json() == {
        "ok": False,
        "message": "Done tickets cannot be reordered.",
    }
    with SessionLocal() as db:
        unchanged = db.get(Ticket, ticket_id)
        assert unchanged is not None
        assert unchanged.summary == "Locked completed ticket"


def test_completed_items_have_no_edit_or_reorder_controls_in_legacy_view() -> None:
    with SessionLocal() as db:
        ticket = Ticket(summary="Locked view ticket", position=0, local_completed=True)
        subtask = Ticket(
            summary="Locked view subtask",
            position=0,
            parent=ticket,
            local_completed=True,
        )
        db.add_all([ticket, subtask])
        db.commit()
        ticket_id = ticket.id
        subtask_id = subtask.id

    page = client.get("/legacy")
    ticket_start = page.text.index(f'id="ticket-{ticket_id}"')
    ticket_block = page.text[ticket_start : page.text.index("</article>", ticket_start)]

    assert f'action="/tickets/{ticket_id}/complete"' in ticket_block
    assert f'action="/tickets/{ticket_id}"' not in ticket_block
    assert f'action="/tickets/{ticket_id}/move-up"' not in ticket_block
    assert 'title="Drag to reorder"' not in ticket_block
    assert f'action="/subtasks/{subtask_id}/complete"' in ticket_block
    assert f'action="/subtasks/{subtask_id}"' not in ticket_block
    assert f'action="/subtasks/{subtask_id}/move-up"' not in ticket_block
    assert f'action="/subtasks/{subtask_id}/delete"' not in ticket_block
    assert f'action="/tickets/{ticket_id}/sync"' not in ticket_block
    assert f'action="/tickets/{ticket_id}/sync-from-jira"' not in ticket_block


def test_completed_items_reject_delete_and_sync_in_api_and_legacy_paths(monkeypatch) -> None:
    class UnexpectedJiraClient:
        def __init__(self, config) -> None:
            raise AssertionError("completed items must not construct a Jira client")

    monkeypatch.setattr("work_tickets.app.JiraClient", UnexpectedJiraClient)
    with SessionLocal() as db:
        parent = Ticket(
            summary="Protected completed ticket",
            position=0,
            local_completed=True,
            jira_issue_key="WORK-300",
        )
        subtask = Ticket(
            summary="Protected completed subtask",
            position=0,
            parent=parent,
            local_completed=True,
            jira_issue_key="WORK-301",
        )
        db.add_all([parent, subtask])
        db.commit()
        parent_id = parent.id
        subtask_id = subtask.id

    for path in (
        f"/api/tickets/{parent_id}/sync",
        f"/api/tickets/{parent_id}/sync-from-jira",
    ):
        response = client.post(path)
        assert response.status_code in {400, 422}
        assert response.json()["ok"] is False

    response = client.delete(f"/api/tickets/{parent_id}")
    assert response.status_code == 400
    assert response.json()["ok"] is False

    for path in (
        f"/api/subtasks/{subtask_id}",
        f"/tickets/{parent_id}/sync",
        f"/tickets/{parent_id}/sync-from-jira",
        f"/tickets/{parent_id}/delete",
        f"/subtasks/{subtask_id}/delete",
    ):
        response = (
            client.delete(path)
            if path.startswith("/api/")
            else client.post(path, follow_redirects=False)
        )
        assert response.status_code in {303, 400, 422}

    with SessionLocal() as db:
        unchanged_parent = db.get(Ticket, parent_id)
        unchanged_subtask = db.get(Ticket, subtask_id)
        assert (
            unchanged_parent is not None
            and unchanged_parent.summary == "Protected completed ticket"
        )
        assert unchanged_parent.jira_issue_key == "WORK-300"
        assert (
            unchanged_subtask is not None
            and unchanged_subtask.summary == "Protected completed subtask"
        )
        assert unchanged_subtask.jira_issue_key == "WORK-301"


def test_completed_parent_rejects_subtask_creation_in_api_and_legacy() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="No children after done", position=0, local_completed=True)
        db.add(parent)
        db.commit()
        parent_id = parent.id

    api_response = client.post(
        f"/api/tickets/{parent_id}/subtasks",
        json={"summary": "Must be rejected", "description": ""},
    )
    legacy_response = client.post(
        f"/tickets/{parent_id}/subtasks",
        data={"summary": "Must also be rejected"},
        follow_redirects=False,
    )

    assert api_response.status_code == 400
    assert api_response.json() == {
        "ok": False,
        "message": "Done tickets cannot have subtasks added.",
    }
    assert legacy_response.status_code == 303
    assert (
        "Done%20tickets%20cannot%20have%20subtasks%20added" in legacy_response.headers["location"]
    )
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

    api_response = client.delete(f"/api/tickets/{parent_id}")
    legacy_response = client.post(f"/tickets/{parent_id}/delete", follow_redirects=False)

    assert api_response.status_code == 400
    assert api_response.json() == {
        "ok": False,
        "message": "Tickets with done subtasks can only be deleted after they are active.",
    }
    assert legacy_response.status_code == 303
    assert "done%20subtasks%20can%20only%20be%20deleted" in legacy_response.headers["location"]
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

    ticket_response = client.post(
        "/api/tickets",
        json={"summary": "New active ticket", "description": ""},
    )
    subtask_response = client.post(
        f"/api/tickets/{parent_id}/subtasks",
        json={"summary": "New active child", "description": ""},
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


def test_api_move_indices_use_only_unfinished_siblings() -> None:
    with SessionLocal() as db:
        first = Ticket(summary="Move first", position=10)
        target = Ticket(summary="Move target", position=50)
        done = Ticket(summary="Move done", position=1, local_completed=True)
        parent = Ticket(summary="Move child parent", position=10)
        child_first = Ticket(summary="Move child first", position=10, parent=parent)
        child_target = Ticket(summary="Move child target", position=50, parent=parent)
        child_done = Ticket(
            summary="Move child done", position=1, parent=parent, local_completed=True
        )
        db.add_all([first, target, done, parent, child_first, child_target, child_done])
        db.commit()
        target_id = target.id
        child_target_id = child_target.id
        parent_id = parent.id

    ticket_response = client.post(f"/api/tickets/{target_id}/move", params={"target_index": 0})
    child_response = client.post(
        f"/api/subtasks/{child_target_id}/move", params={"target_index": 0}
    )

    assert ticket_response.status_code == 200
    assert child_response.status_code == 200
    with SessionLocal() as db:
        top_level = list(
            db.scalars(
                select(Ticket)
                .where(Ticket.parent_id.is_(None), Ticket.id.in_([first.id, target.id, done.id]))
                .order_by(Ticket.local_completed, Ticket.position, Ticket.id)
            )
        )
        assert [ticket.id for ticket in top_level] == [target_id, first.id, done.id]
        active_tickets = list(
            db.scalars(
                select(Ticket)
                .where(Ticket.parent_id.is_(None), Ticket.local_completed.is_(False))
                .order_by(Ticket.position, Ticket.id)
            )
        )
        assert [ticket.position for ticket in active_tickets] == list(range(len(active_tickets)))
        assert db.get(Ticket, done.id).position == 1

        parent = db.get(Ticket, parent_id)
        assert parent is not None
        assert [subtask.id for subtask in parent.subtasks] == [
            child_target_id,
            child_first.id,
            child_done.id,
        ]
        assert [subtask.position for subtask in parent.subtasks[:2]] == [0, 1]
        assert db.get(Ticket, child_done.id).position == 1


def test_subtask_completion_and_locking_use_the_same_priority_rules() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Subtask completion parent", position=0)
        first = Ticket(summary="Subtask active first", position=0, parent=parent)
        second = Ticket(summary="Subtask active second", position=1, parent=parent)
        done = Ticket(summary="Subtask done", position=2, parent=parent, local_completed=True)
        db.add_all([parent, first, second, done])
        db.commit()
        first_id = first.id
        second_id = second.id
        done_id = done.id
        relevant_ids = {first_id, second_id, done_id}

    response = client.post(f"/api/subtasks/{second_id}/complete")
    assert response.status_code == 200
    state = response.json()["state"]
    subtasks = next(ticket["subtasks"] for ticket in state["tickets"] if ticket["id"] == parent.id)
    assert [subtask["id"] for subtask in subtasks if subtask["id"] in relevant_ids] == [
        first_id,
        done_id,
        second_id,
    ]

    locked_update = client.put(
        f"/api/subtasks/{second_id}",
        json={"summary": "Changed", "description": "", "planned_date": None},
    )
    locked_move = client.post(
        f"/api/subtasks/{second_id}/move",
        params={"target_index": 0},
    )
    assert locked_update.status_code == 400
    assert locked_move.status_code == 400

    response = client.post(f"/api/subtasks/{second_id}/complete")
    assert response.status_code == 200
    subtasks = next(
        ticket["subtasks"]
        for ticket in response.json()["state"]["tickets"]
        if ticket["id"] == parent.id
    )
    assert [subtask["id"] for subtask in subtasks if subtask["id"] in relevant_ids] == [
        second_id,
        first_id,
        done_id,
    ]


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
    assert requests[0].read().decode().find('"project":{"key":"WORK"') >= 0


def test_jira_api_conventions_identify_cloud_and_server_urls() -> None:
    cloud_urls = (
        "https://work.example.atlassian.net",
        "https://api.atlassian.com/ex/jira/cloud-id",
    )
    for url in cloud_urls:
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
    create_payload = json.loads(requests[0].content)
    assert create_payload["fields"]["description"] == "Server details"


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
            return JiraIssue(key=f"WORK-{31 + len(calls)}", summary=summary, status_name="To Do")

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
            config.base_url = "https://jira.example.test"
            config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        existing_id = existing.id
        new_id = new.id

    response = client.post(f"/tickets/{parent_id}/sync", follow_redirects=False)

    assert response.status_code == 303
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
            config.base_url = "https://jira.example.test"
            config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        completed_id = completed.id
        failing_id = failing.id

    response = client.post(f"/tickets/{parent_id}/sync", follow_redirects=False)

    assert response.status_code == 303
    assert "Parent%20WORK-70%20synced%2C%20but%20subtask" in response.headers["location"]
    assert "Retry%20the%20parent%20sync%20to%20continue" in response.headers["location"]
    assert calls == [
        ("create-parent", "Partial parent"),
        ("create-child", "Fails remotely"),
    ]
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        completed = db.get(Ticket, completed_id)
        failing = db.get(Ticket, failing_id)
        assert parent is not None and parent.jira_issue_key == "WORK-70"
        assert completed is not None and completed.jira_issue_key == "WORK-72"
        assert completed.local_completed is True
        assert completed.jira_status_name == "Done"
        assert failing is not None and failing.jira_issue_key is None


def test_sync_from_jira_reconciles_children_without_touching_local_fields(monkeypatch) -> None:
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
            local_completed=False,
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
            config.base_url = "https://jira.example.test"
            config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        existing_id = existing.id
        stale_id = stale.id
        local_only_id = local_only.id
        category_id = category.id

    response = client.post(f"/tickets/{parent_id}/sync-from-jira", follow_redirects=False)

    assert response.status_code == 303
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
        assert parent.local_completed is False
        assert existing is not None
        assert existing.summary == "Old existing child"
        assert existing.description == "Old child details"
        assert existing.position == 4
        assert existing.local_completed is True
        assert stale is not None
        assert stale.summary == "Stale linked child"
        assert stale.jira_issue_key == "WORK-83"
        assert local_only is not None
        assert local_only.jira_issue_key is None
        assert local_only.position == 6
        created = db.scalar(select(Ticket).where(Ticket.jira_issue_key == "WORK-82"))
        assert created is not None
        assert created.parent_id == parent_id
        assert created.position == 0
        assert created.planned_date is None
        assert created.local_completed is False


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
        local_only_child = Ticket(
            summary="Keep for outbound sync",
            parent=parent,
            position=20,
        )
        db.add_all([parent, linked_child, local_only_child])
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
            config.base_url = "https://jira.example.test"
            config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        linked_child_id = linked_child.id
        local_only_child_id = local_only_child.id

    response = client.post(f"/tickets/{parent_id}/sync-from-jira", follow_redirects=False)

    assert response.status_code == 303
    with SessionLocal() as db:
        assert db.get(Ticket, linked_child_id) is None
        retained = db.get(Ticket, local_only_child_id)
        assert retained is not None
        assert retained.parent_id == parent_id
        assert retained.jira_issue_key is None
        assert retained.position == 0


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
            config.base_url = "https://jira.example.test"
            config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        child_id = child.id

    response = client.post(f"/tickets/{parent_id}/sync-from-jira", follow_redirects=False)

    assert response.status_code == 303
    assert "duplicate%20subtask%20key%20WORK-91" in response.headers["location"]
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        child = db.get(Ticket, child_id)
        assert parent is not None and parent.summary == "Local parent that must survive"
        assert child is not None and child.summary == "Local child that must survive"


def test_sync_parent_creates_local_subtask_with_project_subtask_type(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/createmeta/WORK/issuetypes"):
            return httpx.Response(
                200,
                json={
                    "issueTypes": [
                        {"id": "10004", "name": "Subtask", "subtask": True},
                    ]
                },
            )
        if request.method == "POST":
            payload = json.loads(request.content)
            fields = payload["fields"]
            if fields.get("parent") is not None:
                if fields["issuetype"] != {"id": "10004"}:
                    error_message = "Issue type is not available in this project."
                    return httpx.Response(
                        400,
                        json={"errors": {"issuetype": error_message}},
                    )
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
        parent = Ticket(summary="Local parent", description="Parent details", position=303)
        child = Ticket(
            summary="Local child", description="Child details", position=0, parent=parent
        )
        db.add_all([parent, child])
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
            config.base_url = "https://jira.example.test"
            config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        child_id = child.id

    response = client.post(f"/tickets/{parent_id}/sync", follow_redirects=False)

    assert response.status_code == 303
    assert "synced%20to%20Jira" in response.headers["location"]
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        child = db.get(Ticket, child_id)
        assert parent is not None and parent.jira_issue_key == "WORK-100"
        assert child is not None and child.jira_issue_key == "WORK-101"
    assert any(
        request.method == "GET" and request.url.path.endswith("/createmeta/WORK/issuetypes")
        for request in requests
    )


def test_direct_subtask_sync_is_rejected_without_touching_jira(monkeypatch) -> None:
    class UnexpectedJiraClient:
        def __init__(self, config) -> None:
            raise AssertionError("direct subtask sync must not construct a Jira client")

    monkeypatch.setattr("work_tickets.app.JiraClient", UnexpectedJiraClient)
    with SessionLocal() as db:
        parent = Ticket(summary="Sync parent", position=301)
        subtask = Ticket(summary="Sync child", position=0, parent=parent)
        db.add_all([parent, subtask])
        db.commit()
        subtask_id = subtask.id

    for suffix, expected in (
        ("sync", "Only%20top-level%20tickets%20can%20sync%20to%20Jira"),
        ("sync-from-jira", "Only%20top-level%20tickets%20can%20sync%20from%20Jira"),
    ):
        response = client.post(f"/tickets/{subtask_id}/{suffix}", follow_redirects=False)
        assert response.status_code == 303
        assert expected in response.headers["location"]

    page = client.get("/legacy")
    assert f'action="/tickets/{subtask_id}/sync"' not in page.text
    assert f'action="/tickets/{subtask_id}/sync-from-jira"' not in page.text


def test_sync_from_jira_updates_parent_and_existing_or_new_subtasks(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            pass

        def get_issue_with_subtasks(self, key: str) -> JiraIssueWithSubtasks:
            assert key == "WORK-40"
            return JiraIssueWithSubtasks(
                issue=JiraIssue(
                    key=key,
                    summary="Remote parent",
                    description="Remote parent details",
                    status_name="In Progress",
                ),
                subtasks=(
                    JiraIssue(
                        key="WORK-41",
                        summary="Remote existing child",
                        description="Remote child details",
                        status_name="Done",
                    ),
                    JiraIssue(
                        key="WORK-42",
                        summary="Remote new child",
                        description="New child details",
                        status_name="To Do",
                    ),
                ),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    with SessionLocal() as db:
        category = Category(name="Inbound subtask category")
        parent = Ticket(
            summary="Local parent",
            description="Local details",
            planned_date=date(2026, 9, 2),
            position=302,
            local_completed=False,
            jira_issue_key="WORK-40",
            category=category,
        )
        existing = Ticket(
            summary="Local child",
            position=7,
            parent=parent,
            jira_issue_key="WORK-41",
            local_completed=True,
        )
        db.add_all([category, parent, existing])
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
            config.base_url = "https://jira.example.test"
            config.project_key = "WORK"
        db.commit()
        parent_id = parent.id
        existing_id = existing.id

    response = client.post(f"/tickets/{parent_id}/sync-from-jira", follow_redirects=False)

    assert response.status_code == 303
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        existing = db.get(Ticket, existing_id)
        assert parent is not None and parent.summary == "Remote parent"
        assert parent.category_id == category.id
        assert parent.planned_date == date(2026, 9, 2)
        assert parent.local_completed is False
        assert existing is not None and existing.summary == "Local child"
        assert existing.description == ""
        assert existing.jira_status_name is None
        assert existing.position == 7
        new = db.scalar(select(Ticket).where(Ticket.jira_issue_key == "WORK-42"))
        assert new is not None
        assert new.parent_id == parent_id
        assert new.summary == "Remote new child"
        assert new.position == 0


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


def test_synced_ticket_label_links_to_jira_issue() -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        if config is None:
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
        else:
            config.base_url = "https://jira.example.test"
            config.browser_base_url = "https://jira.example.test"
        ticket = Ticket(summary="Linked ticket", position=103, jira_issue_key="WORK-42")
        db.add(ticket)
        db.commit()

    page = client.get("/legacy")

    assert page.status_code == 200
    assert 'href="https://jira.example.test/browse/WORK-42"' in page.text
    assert 'target="_blank"' in page.text
    assert 'rel="noopener noreferrer"' in page.text
    assert ">(WORK-42)</a>" in page.text


def test_synced_ticket_label_uses_configured_browser_base_url() -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        if config is None:
            db.add(
                JiraConfig(
                    id=1,
                    base_url="https://api.atlassian.com/ex/jira/cloud-id",
                    browser_base_url="https://johannesqvarford.atlassian.net/",
                    email="person@example.test",
                    api_token="test-token",
                    project_key="SCRUM",
                    issue_type="Task",
                )
            )
        else:
            config.base_url = "https://api.atlassian.com/ex/jira/cloud-id"
            config.browser_base_url = "https://johannesqvarford.atlassian.net/"
            config.project_key = "SCRUM"
        ticket = Ticket(summary="Browser linked ticket", position=104, jira_issue_key="SCRUM-5")
        db.add(ticket)
        db.commit()

    page = client.get("/legacy")

    assert page.status_code == 200
    assert 'href="https://johannesqvarford.atlassian.net/browse/SCRUM-5"' in page.text
    assert page.text.count('href="https://johannesqvarford.atlassian.net/browse/SCRUM-5"') == 1
    assert 'href="https://api.atlassian.com/ex/jira/cloud-id/browse/SCRUM-5"' not in page.text


def test_synced_subtask_labels_link_and_fall_back_without_browser_url() -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        if config is None:
            db.add(
                JiraConfig(
                    id=1,
                    base_url="https://api.atlassian.com/ex/jira/cloud-id",
                    browser_base_url="https://jira.example.test/",
                    email="person@example.test",
                    api_token="test-token",
                    project_key="SCRUM",
                    issue_type="Task",
                )
            )
        else:
            config.base_url = "https://api.atlassian.com/ex/jira/cloud-id"
            config.browser_base_url = "https://jira.example.test/"
        parent = Ticket(summary="Subtask label parent", position=105, jira_issue_key="SCRUM-700")
        child = Ticket(
            summary="Subtask label child",
            position=0,
            parent=parent,
            jira_issue_key="SCRUM-701",
        )
        db.add_all([parent, child])
        db.commit()

    page = client.get("/legacy")

    assert page.status_code == 200
    assert 'href="https://jira.example.test/browse/SCRUM-700"' in page.text
    assert 'href="https://jira.example.test/browse/SCRUM-701"' in page.text
    assert page.text.count('target="_blank"') >= 2
    assert page.text.count('rel="noopener noreferrer"') >= 2
    assert ">(SCRUM-700)</a>" in page.text
    assert ">(SCRUM-701)</a>" in page.text

    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        config.browser_base_url = ""
        db.commit()

    page = client.get("/legacy")

    assert page.status_code == 200
    assert 'class="badge">(SCRUM-700)</span>' in page.text
    assert 'class="badge">(SCRUM-701)</span>' in page.text
    assert 'href="https://api.atlassian.com/ex/jira/cloud-id/browse/SCRUM-700"' not in page.text
    assert 'href="https://api.atlassian.com/ex/jira/cloud-id/browse/SCRUM-701"' not in page.text


def test_saving_jira_config_persists_separate_browser_base_url() -> None:
    response = client.post(
        "/jira/config",
        data={
            "base_url": "https://api.atlassian.com/ex/jira/cloud-id/",
            "browser_base_url": "https://johannesqvarford.atlassian.net/",
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "scrum",
            "issue_type": "Task",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        assert config.base_url == "https://api.atlassian.com/ex/jira/cloud-id"
        assert config.browser_base_url == "https://johannesqvarford.atlassian.net"


def test_api_saving_jira_config_preserves_blank_browser_base_url_and_hides_api_url() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="API blank browser URL", position=106, jira_issue_key="BLANK-API-1")
        child = Ticket(
            summary="API blank browser URL subtask",
            position=0,
            parent=parent,
            jira_issue_key="BLANK-API-2",
        )
        db.add_all([parent, child])
        db.commit()

    response = client.put(
        "/api/settings/jira",
        json={
            "base_url": "https://api.atlassian.com/ex/jira/cloud-id",
            "browser_base_url": "",
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "blank",
            "issue_type": "Task",
        },
    )

    assert response.status_code == 200
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        assert config.browser_base_url == ""

    page = client.get("/legacy")

    assert 'class="badge">(BLANK-API-1)</span>' in page.text
    assert 'class="badge">(BLANK-API-2)</span>' in page.text
    assert 'href="https://api.atlassian.com/ex/jira/cloud-id/browse/BLANK-API-1"' not in page.text
    assert 'href="https://api.atlassian.com/ex/jira/cloud-id/browse/BLANK-API-2"' not in page.text


def test_legacy_saving_jira_config_preserves_blank_browser_base_url_and_hides_api_url() -> None:
    with SessionLocal() as db:
        parent = Ticket(
            summary="Legacy blank browser URL", position=107, jira_issue_key="BLANK-LEGACY-1"
        )
        child = Ticket(
            summary="Legacy blank browser URL subtask",
            position=0,
            parent=parent,
            jira_issue_key="BLANK-LEGACY-2",
        )
        db.add_all([parent, child])
        db.commit()

    response = client.post(
        "/jira/config",
        data={
            "base_url": "https://api.atlassian.com/ex/jira/cloud-id",
            "browser_base_url": "",
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "legacy",
            "issue_type": "Task",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        assert config is not None
        assert config.browser_base_url == ""

    page = client.get("/legacy")

    assert 'class="badge">(BLANK-LEGACY-1)</span>' in page.text
    assert 'class="badge">(BLANK-LEGACY-2)</span>' in page.text
    assert (
        'href="https://api.atlassian.com/ex/jira/cloud-id/browse/BLANK-LEGACY-1"' not in page.text
    )
    assert (
        'href="https://api.atlassian.com/ex/jira/cloud-id/browse/BLANK-LEGACY-2"' not in page.text
    )


def test_saving_jira_config_keeps_validate_form_field_compatible(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.project_key == "WORK"

        def validate(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("work_tickets.app.JiraClient", FakeJiraClient)
    response = client.post(
        "/jira/config",
        data={
            "base_url": "https://jira.example.test",
            "email": "person@example.test",
            "api_token": "test-token",
            "project_key": "work",
            "issue_type": "Task",
            "validate": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Jira%20connection%20validated%20and%20saved" in response.headers["location"]


def test_init_db_migrates_existing_jira_config_with_blank_browser_base_url(
    tmp_path, monkeypatch
) -> None:
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE jira_config ("
                "id INTEGER NOT NULL PRIMARY KEY, "
                "base_url VARCHAR(300) NOT NULL, "
                "email VARCHAR(320) NOT NULL, "
                "api_token VARCHAR(300) NOT NULL, "
                "project_key VARCHAR(40) NOT NULL, "
                "issue_type VARCHAR(80) NOT NULL, "
                "completed_statuses VARCHAR(500) NOT NULL, "
                "updated_at DATETIME NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO jira_config "
                "(id, base_url, email, api_token, project_key, issue_type, "
                "completed_statuses, updated_at) VALUES "
                "(1, 'https://jira.example.test', 'person@example.test', 'test-token', "
                "'WORK', 'Task', 'Done', '2026-08-23 00:00:00')"
            )
        )

    monkeypatch.setattr("work_tickets.models.engine", legacy_engine)
    from work_tickets.models import init_db

    init_db()
    init_db()

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("jira_config")}
    with legacy_engine.connect() as connection:
        browser_base_url = connection.scalar(
            text("SELECT browser_base_url FROM jira_config WHERE id = 1")
        )
    assert "browser_base_url" in columns
    assert browser_base_url == ""


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


def test_sync_from_jira_rejects_completed_ticket_without_mutating_local_data(monkeypatch) -> None:
    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.project_key == "WORK"

        def get_issue_with_subtasks(self, key: str) -> JiraIssueWithSubtasks:
            assert key == "WORK-10"
            return JiraIssueWithSubtasks(
                issue=JiraIssue(
                    key=key,
                    summary="Changed in Jira",
                    description="Remote description",
                    issue_type_name="Bug",
                    status_name="Done",
                ),
                subtasks=(),
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
    assert "Done%20tickets%20can%20only%20be%20marked%20active" in response.headers["location"]
    with SessionLocal() as db:
        synced = db.get(Ticket, ticket_id)
        assert synced is not None
        assert synced.summary == "Old local summary"
        assert synced.description == "Old local description"
        assert synced.jira_status_name == "Open"
        assert synced.jira_issue_key == "WORK-10"
        assert synced.category_id == category.id
        assert synced.planned_date == date(2026, 9, 1)
        assert synced.local_completed is True
        assert synced.position == 42


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

    page = client.get("/legacy")
    assert page.status_code == 200
    assert "Subtasks" in page.text
    assert "First subtask" in page.text
    assert "Second subtask" in page.text
    for subtask_id in subtask_ids:
        assert f'action="/subtasks/{subtask_id}/delete"' in page.text
    assert "Delete this subtask?" in page.text


def test_edit_completed_subtask_is_rejected_without_mutation() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Edit parent", position=211)
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

    response = client.post(
        f"/subtasks/{subtask_id}",
        data={
            "summary": "  Updated subtask  ",
            "description": "Updated details",
            "planned_date": "2026-09-03",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Done%20subtasks%20can%20only%20be%20marked%20active" in response.headers["location"]
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        subtask = db.get(Ticket, subtask_id)
        assert parent is not None
        assert subtask is not None
        assert subtask.summary == "Original subtask"
        assert subtask.description == "Original details"
        assert subtask.planned_date == date(2026, 8, 24)
        assert subtask.parent_id == parent_id
        assert subtask.position == 7
        assert subtask.local_completed is True

    page = client.get("/legacy")
    assert f'action="/subtasks/{subtask_id}"' not in page.text
    assert f'action="/subtasks/{subtask_id}/sync"' not in page.text


def test_edit_subtask_validates_summary_and_planned_date() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Edit validation parent", position=212)
        subtask = Ticket(summary="Keep this subtask", position=0, parent=parent)
        db.add_all([parent, subtask])
        db.commit()
        subtask_id = subtask.id

    missing_summary = client.post(
        f"/subtasks/{subtask_id}",
        data={"summary": "   ", "planned_date": "2026-08-25"},
        follow_redirects=False,
    )
    invalid_date = client.post(
        f"/subtasks/{subtask_id}",
        data={"summary": "Updated subtask", "planned_date": "not-a-date"},
        follow_redirects=False,
    )

    assert missing_summary.status_code == 303
    assert "Subtask%20summary%20is%20required" in missing_summary.headers["location"]
    assert invalid_date.status_code == 303
    assert "Subtask%20planned%20date%20is%20invalid" in invalid_date.headers["location"]
    with SessionLocal() as db:
        subtask = db.get(Ticket, subtask_id)
        assert subtask is not None
        assert subtask.summary == "Keep this subtask"
        assert subtask.planned_date is None


def test_edit_subtask_rejects_missing_ids_and_top_level_tickets() -> None:
    with SessionLocal() as db:
        top_level = Ticket(summary="Not a subtask to edit", position=213)
        db.add(top_level)
        db.commit()
        top_level_id = top_level.id

    missing = client.post(
        "/subtasks/999999",
        data={"summary": "Missing"},
        follow_redirects=False,
    )
    top_level_response = client.post(
        f"/subtasks/{top_level_id}",
        data={"summary": "Should not edit"},
        follow_redirects=False,
    )

    assert missing.status_code == 303
    assert "Subtask%20was%20not%20found" in missing.headers["location"]
    assert top_level_response.status_code == 303
    assert (
        "Top-level%20tickets%20cannot%20be%20edited%20here"
        in top_level_response.headers["location"]
    )


def test_edit_completed_synced_subtask_is_rejected_without_touching_jira(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    class FakeJiraClient:
        def __init__(self, config) -> None:
            assert config.project_key == "WORK"

        def update_issue(self, key: str, summary: str, description: str) -> JiraIssue:
            calls.append((key, summary, description))
            return JiraIssue(
                key=key,
                summary="Remote updated subtask",
                description="Remote updated details",
                status_name="In Progress",
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
        parent = Ticket(summary="Synced edit parent", position=214, jira_issue_key="WORK-60")
        subtask = Ticket(
            summary="Local subtask",
            description="Local details",
            planned_date=date(2026, 9, 4),
            position=3,
            parent=parent,
            jira_issue_key="WORK-61",
            jira_status_name="To Do",
            local_completed=True,
        )
        db.add_all([parent, subtask])
        db.commit()
        parent_id = parent.id
        subtask_id = subtask.id

    response = client.post(
        f"/subtasks/{subtask_id}",
        data={
            "summary": "Edited locally",
            "description": "Edited details",
            "planned_date": "2026-09-05",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Done%20subtasks%20can%20only%20be%20marked%20active" in response.headers["location"]
    assert calls == []
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        subtask = db.get(Ticket, subtask_id)
        assert parent is not None
        assert subtask is not None
        assert parent.summary == "Synced edit parent"
        assert parent.jira_issue_key == "WORK-60"
        assert subtask.summary == "Local subtask"
        assert subtask.description == "Local details"
        assert subtask.planned_date == date(2026, 9, 4)
        assert subtask.parent_id == parent_id
        assert subtask.position == 3
        assert subtask.local_completed is True
        assert subtask.jira_issue_key == "WORK-61"
        assert subtask.jira_status_name == "To Do"


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

    response = client.post(f"/tickets/{parent_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert "Ticket%20Delete%20linked%20parent%20deleted" in response.headers["location"]
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
        parent = Ticket(
            summary="Delete despite Jira failure", position=216, jira_issue_key="WORK-92"
        )
        child = Ticket(summary="Cascaded child", position=0, parent=parent)
        db.add_all([parent, child])
        db.commit()
        parent_id = parent.id
        child_id = child.id

    response = client.post(f"/tickets/{parent_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    assert "error=" in location
    assert "Delete%20despite%20Jira%20failure" in location
    assert "linked%20Jira%20issue%20WORK-92%20could%20not%20be%20deleted" in location
    assert "Jira%20returned%20HTTP%20403%3A%20Forbidden" in location
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

    response = client.post(f"/subtasks/{subtask_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert (
        "Subtask%20Delete%20despite%20remote%20missing%20deleted%20locally"
        in response.headers["location"]
    )
    assert (
        "linked%20Jira%20issue%20WORK-93%20could%20not%20be%20deleted"
        in response.headers["location"]
    )
    with SessionLocal() as db:
        assert db.get(Ticket, subtask_id) is None
        assert db.get(Ticket, parent_id) is not None


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

    page = client.get("/legacy")
    assert page.status_code == 200
    assert "Move up" in page.text
    assert "Move down" in page.text
    assert f'action="/subtasks/{first_id}/move-up"' in page.text
    assert f'action="/subtasks/{last_id}/move-down"' in page.text

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

    page = client.get("/legacy")
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


def test_spa_subtask_dates_are_created_and_updated() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="SPA date parent", position=211)
        db.add(parent)
        db.commit()
        parent_id = parent.id

    create_response = client.post(
        f"/api/tickets/{parent_id}/subtasks",
        json={
            "summary": "SPA date child",
            "description": "",
            "planned_date": "2026-08-29",
        },
    )

    assert create_response.status_code == 200
    with SessionLocal() as db:
        created = db.scalar(
            select(Ticket).where(
                Ticket.parent_id == parent_id,
                Ticket.summary == "SPA date child",
            )
        )
        assert created is not None
        subtask_id = created.id
        assert created.planned_date == date(2026, 8, 29)

    parent_state = next(
        ticket for ticket in create_response.json()["state"]["tickets"] if ticket["id"] == parent_id
    )
    assert parent_state["subtasks"][0]["planned_date"] == "2026-08-29"

    update_response = client.put(
        f"/api/subtasks/{subtask_id}",
        json={
            "summary": "SPA date child",
            "description": "Updated",
            "planned_date": "2026-09-01",
        },
    )

    assert update_response.status_code == 200
    updated_subtask = next(
        subtask
        for ticket in update_response.json()["state"]["tickets"]
        for subtask in ticket["subtasks"]
        if subtask["id"] == subtask_id
    )
    assert updated_subtask["planned_date"] == "2026-09-01"
    with SessionLocal() as db:
        persisted = db.get(Ticket, subtask_id)
        assert persisted is not None
        assert persisted.planned_date == date(2026, 9, 1)


def test_spa_new_subtask_without_planned_date_is_persisted_unset() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="SPA unset date parent", position=212)
        db.add(parent)
        db.commit()
        parent_id = parent.id

    response = client.post(
        f"/api/tickets/{parent_id}/subtasks",
        json={
            "summary": "SPA unset date child",
            "description": "",
            "planned_date": None,
        },
    )

    assert response.status_code == 200
    with SessionLocal() as db:
        created = db.scalar(
            select(Ticket).where(
                Ticket.parent_id == parent_id,
                Ticket.summary == "SPA unset date child",
            )
        )
        assert created is not None
        assert created.planned_date is None
