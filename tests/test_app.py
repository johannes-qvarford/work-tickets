from fastapi.testclient import TestClient

from work_tickets.app import app
from work_tickets.models import Base, engine

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
