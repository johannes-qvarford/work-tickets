import atexit
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

from work_tickets.app import app  # noqa: E402
from work_tickets.jira import JiraClient, JiraIssue, JiraIssueWithSubtasks  # noqa: E402
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


def test_homepage_uses_wider_responsive_layout() -> None:
    page = client.get("/")

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

    page = client.get("/")

    assert page.status_code == 200
    assert 'id="category-filter"' in page.text
    assert 'aria-controls="ticket-lists"' in page.text
    assert f'<option value="{category_id}">Filter category</option>' in page.text
    assert page.text.count(f'data-category-id="{category_id}"') == 2
    assert page.text.count('data-category-id=""') == 1
    assert page.text.count("data-filterable-ticket\n") == 3
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

    page = client.get("/")

    assert page.status_code == 200
    assert f'action="/tickets/{ticket_id}/complete"' in page.text
    assert f'action="/tickets/{ticket_id}/sync"' in page.text
    assert f'action="/subtasks/{subtask_id}/complete"' in page.text
    assert f'action="/subtasks/{subtask_id}/move-up"' in page.text
    assert f'action="/subtasks/{subtask_id}/move-down"' in page.text
    assert f'action="/subtasks/{subtask_id}/delete"' in page.text
    assert 'class="compact-control completion-control"' in page.text
    assert 'aria-label="Sync Compact controls and its subtasks to Jira"' in page.text
    assert 'aria-label="Mark Compact subtask as done"' in page.text
    assert 'aria-label="Move up Compact subtask"' in page.text
    assert 'aria-label="Delete Compact subtask"' in page.text


def test_subtask_move_controls_have_no_refresh_hooks() -> None:
    with SessionLocal() as db:
        parent = Ticket(summary="Hook parent", position=0)
        first = Ticket(summary="Hook first", position=0, parent=parent)
        second = Ticket(summary="Hook second", position=1, parent=parent)
        db.add_all([parent, first, second])
        db.commit()

    page = client.get("/")

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

    page = client.get("/")

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

    page = client.get("/")

    assert page.status_code == 200
    assert page.text.count(f'id="ticket-{first_id}"') == 1
    assert page.text.count(f'id="ticket-{second_id}"') == 1
    assert page.text.count('class="ticket"') >= 2
    assert f'action="/tickets/{first_id}/move-up"' in page.text
    assert f'action="/tickets/{second_id}/move-down"' in page.text
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

    page = client.get("/")

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
    assert "Prepare agenda" in client.get("/").text


def test_ticket_forms_group_summary_date_and_category_responsively() -> None:
    with SessionLocal() as db:
        category = Category(name="Form layout category")
        ticket = Ticket(summary="Form layout ticket", position=0, category=category)
        db.add_all([category, ticket])
        db.commit()
        ticket_id = ticket.id

    page = client.get("/")

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

    page = client.get("/")
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

    page = client.get("/")
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

    page = client.get("/")
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
            local_completed=True,
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
        assert parent.local_completed is True
        assert existing is not None and existing.summary == "Remote existing child"
        assert existing.description == "Remote child details"
        assert existing.jira_status_name == "Done"
        assert existing.position == 7
        new = db.scalar(select(Ticket).where(Ticket.jira_issue_key == "WORK-42"))
        assert new is not None
        assert new.parent_id == parent_id
        assert new.summary == "Remote new child"
        assert new.position == 8


def test_jira_client_creates_subtask_and_fetches_parent_subtasks() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
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
    assert requests[0].method == "POST"
    post_payload = requests[0].read().decode()
    assert '"issuetype":{"name":"Sub-task"}' in post_payload
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

    page = client.get("/")

    assert page.status_code == 200
    assert 'href="https://jira.example.test/browse/WORK-42"' in page.text
    assert 'target="_blank"' in page.text
    assert 'rel="noopener noreferrer"' in page.text
    assert ">WORK-42</a>" in page.text


def test_synced_ticket_label_uses_configured_browser_base_url() -> None:
    with SessionLocal() as db:
        config = db.get(JiraConfig, 1)
        if config is None:
            db.add(
                JiraConfig(
                    id=1,
                    base_url="https://api.atlassian.com/ex/jira/cloud-id",
                    browser_base_url="https://johannesqvarford.atlassian.net",
                    email="person@example.test",
                    api_token="test-token",
                    project_key="SCRUM",
                    issue_type="Task",
                )
            )
        else:
            config.base_url = "https://api.atlassian.com/ex/jira/cloud-id"
            config.browser_base_url = "https://johannesqvarford.atlassian.net"
            config.project_key = "SCRUM"
        ticket = Ticket(summary="Browser linked ticket", position=104, jira_issue_key="SCRUM-5")
        db.add(ticket)
        db.commit()

    page = client.get("/")

    assert page.status_code == 200
    assert 'href="https://johannesqvarford.atlassian.net/browse/SCRUM-5"' in page.text
    assert page.text.count('href="https://johannesqvarford.atlassian.net/browse/SCRUM-5"') == 1
    assert 'href="https://api.atlassian.com/ex/jira/cloud-id/browse/SCRUM-5"' not in page.text


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


def test_init_db_migrates_existing_jira_config_to_browser_base_url(tmp_path, monkeypatch) -> None:
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

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("jira_config")}
    with legacy_engine.connect() as connection:
        browser_base_url = connection.scalar(
            text("SELECT browser_base_url FROM jira_config WHERE id = 1")
        )
    assert "browser_base_url" in columns
    assert browser_base_url == "https://jira.example.test"


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


def test_edit_subtask_persists_fields_without_changing_relationship_or_order() -> None:
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
    assert "Subtask%20Updated%20subtask%20updated" in response.headers["location"]
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        subtask = db.get(Ticket, subtask_id)
        assert parent is not None
        assert subtask is not None
        assert subtask.summary == "Updated subtask"
        assert subtask.description == "Updated details"
        assert subtask.planned_date == date(2026, 9, 3)
        assert subtask.parent_id == parent_id
        assert subtask.position == 7
        assert subtask.local_completed is True

    page = client.get("/")
    assert f'action="/subtasks/{subtask_id}"' in page.text
    assert 'name="summary" value="Updated subtask"' in page.text
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


def test_edit_synced_subtask_updates_only_that_jira_issue(monkeypatch) -> None:
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
            local_completed=True,
            jira_issue_key="WORK-61",
            jira_status_name="To Do",
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
    assert calls == [("WORK-61", "Edited locally", "Edited details")]
    with SessionLocal() as db:
        parent = db.get(Ticket, parent_id)
        subtask = db.get(Ticket, subtask_id)
        assert parent is not None
        assert subtask is not None
        assert parent.summary == "Synced edit parent"
        assert parent.jira_issue_key == "WORK-60"
        assert subtask.summary == "Remote updated subtask"
        assert subtask.description == "Remote updated details"
        assert subtask.planned_date == date(2026, 9, 5)
        assert subtask.parent_id == parent_id
        assert subtask.position == 3
        assert subtask.local_completed is True
        assert subtask.jira_issue_key == "WORK-61"
        assert subtask.jira_status_name == "In Progress"


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
