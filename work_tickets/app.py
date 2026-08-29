from __future__ import annotations

from collections.abc import Generator
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import jira_service, refine, tickets, web
from .jira import JiraClient
from .local_projects import is_safe_local_component_name
from .models import (
    Category,
    CategoryComponent,
    Component,
    JiraConfig,
    SessionLocal,
    Ticket,
    init_db,
)

app = FastAPI(title="Work Tickets")
parse_jira_issue_reference = jira_service.parse_jira_issue_reference
app.mount(
    "/assets",
    StaticFiles(directory=Path(__file__).parent / "static" / "assets"),
    name="assets",
)


class TicketPayload(BaseModel):
    summary: str = ""
    description: str = ""
    notes: str = ""
    planned_date: date | None = None
    category_id: int | None = None
    component: str | None = None
    jira_reference: str = ""


class SubtaskPayload(BaseModel):
    summary: str = ""
    description: str = ""
    planned_date: date | None = None


class CategoryPayload(BaseModel):
    name: str


class ComponentPayload(BaseModel):
    name: str


class CategoryComponentPayload(BaseModel):
    component_id: int


class JiraConfigPayload(BaseModel):
    base_url: str
    browser_base_url: str = ""
    local_projects_directory: str = ""
    email: str
    api_token: str = ""
    project_key: str = ""
    issue_type: str = "Task"
    completed_statuses: str = "Done"
    validate_connection: bool = Field(default=False, alias="validate")

    model_config = {"populate_by_name": True}


@app.on_event("startup")
def startup() -> None:
    init_db()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _api_state(db: Session) -> dict[str, object]:
    return web.ticket_list_data(db)


def _service_json_response(response: Response, db: Session) -> Response:
    if response.status_code >= 400:
        return response
    message = "Saved."
    ok = True
    created_id: int | None = None
    if isinstance(response, JSONResponse):
        try:
            import json

            content = bytes(response.body).decode()
            result = json.loads(content)
            message = str(result.get("message", message))
            ok = result.get("ok", True) is True
            if isinstance(result.get("created_id"), int):
                created_id = result["created_id"]
        except (UnicodeDecodeError, ValueError):
            pass
    payload: dict[str, object] = {"ok": ok, "message": message, "state": _api_state(db)}
    if created_id is not None:
        payload["created_id"] = created_id
    return JSONResponse(payload)


@app.get("/api/state")
def api_state(db: Session = Depends(get_db)) -> dict[str, object]:
    return _api_state(db)


@app.websocket("/api/tickets/{ticket_id}/refine")
async def api_refine_ticket(websocket: WebSocket, ticket_id: int) -> None:
    await websocket.accept()
    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        config = db.get(JiraConfig, 1)
        try:
            if ticket is None:
                raise refine.RefineError("Ticket was not found.")
            prompt = refine.refine_prompt(ticket, config)
            working_directory = refine.refine_working_directory(ticket, config)
        except refine.RefineError as exc:
            await refine.send_error(websocket, str(exc))
            return
    await refine.run_refine(websocket, prompt, working_directory)


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.post("/api/tickets")
def api_create_ticket(
    payload: TicketPayload, request: Request, db: Session = Depends(get_db)
) -> Response:
    response = tickets.create_ticket(
        request,
        payload.summary,
        payload.description,
        payload.notes,
        payload.planned_date.isoformat() if payload.planned_date else "",
        str(payload.category_id) if payload.category_id is not None else "",
        payload.component,
        db,
        jira_reference=payload.jira_reference,
        jira_client_factory=JiraClient,
    )
    return _service_json_response(response, db)


@app.post("/api/tickets/{ticket_id}/subtasks")
def api_create_subtask(
    ticket_id: int, payload: SubtaskPayload, request: Request, db: Session = Depends(get_db)
) -> Response:
    response = tickets.create_subtask(
        ticket_id,
        request,
        payload.summary,
        payload.description,
        payload.planned_date.isoformat() if payload.planned_date else "",
        db,
    )
    return _service_json_response(response, db)


@app.put("/api/tickets/{ticket_id}")
def api_update_ticket(
    ticket_id: int, payload: TicketPayload, request: Request, db: Session = Depends(get_db)
) -> Response:
    response = tickets.update_ticket(
        ticket_id,
        request,
        payload.summary,
        payload.description,
        payload.notes,
        payload.planned_date.isoformat() if payload.planned_date else "",
        payload.category_id,
        payload.component,
        db,
        component_provided="component" in payload.model_fields_set,
        jira_client_factory=JiraClient,
    )
    return _service_json_response(response, db)


@app.put("/api/subtasks/{subtask_id}")
def api_update_subtask(
    subtask_id: int, payload: SubtaskPayload, request: Request, db: Session = Depends(get_db)
) -> Response:
    response = tickets.update_subtask(
        subtask_id,
        request,
        payload.summary,
        payload.description,
        payload.planned_date.isoformat() if payload.planned_date else "",
        db,
        jira_client_factory=JiraClient,
    )
    return _service_json_response(response, db)


@app.post("/api/tickets/{ticket_id}/complete")
def api_complete_ticket(ticket_id: int, db: Session = Depends(get_db)) -> Response:
    response = tickets.complete_ticket(ticket_id, db)
    if response.status_code >= 400:
        return response
    return JSONResponse({"ok": response.status_code < 400, "state": _api_state(db)})


@app.post("/api/subtasks/{subtask_id}/complete")
def api_complete_subtask(subtask_id: int, db: Session = Depends(get_db)) -> Response:
    response = tickets.complete_subtask(subtask_id, db)
    if response.status_code >= 400:
        return response
    return JSONResponse({"ok": response.status_code < 400, "state": _api_state(db)})


@app.delete("/api/tickets/{ticket_id}")
def api_delete_ticket(ticket_id: int, db: Session = Depends(get_db)) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return JSONResponse({"ok": False, "message": "Ticket was not found."}, status_code=404)
    if ticket.local_completed:
        return JSONResponse(
            {"ok": False, "message": "Done tickets can only be marked active."}, status_code=400
        )
    if any(subtask.local_completed for subtask in ticket.subtasks):
        return JSONResponse(
            {
                "ok": False,
                "message": "Tickets with done subtasks can only be deleted after they are active.",
            },
            status_code=400,
        )
    response = tickets.delete_ticket(ticket_id, db, jira_client_factory=JiraClient)
    return _service_json_response(response, db)


@app.delete("/api/subtasks/{subtask_id}")
def api_delete_subtask(subtask_id: int, db: Session = Depends(get_db)) -> Response:
    subtask = db.get(Ticket, subtask_id)
    if subtask is None:
        return JSONResponse({"ok": False, "message": "Subtask was not found."}, status_code=404)
    if subtask.local_completed:
        return JSONResponse(
            {"ok": False, "message": "Done subtasks can only be marked active."}, status_code=400
        )
    response = tickets.delete_subtask(subtask_id, db, jira_client_factory=JiraClient)
    return _service_json_response(response, db)


@app.post("/api/tickets/{ticket_id}/move")
def api_move_ticket(
    ticket_id: int, target_index: int, request: Request, db: Session = Depends(get_db)
) -> Response:
    response = tickets.move_ticket_to_index(ticket_id, target_index, request, db)
    if response.status_code >= 400:
        return response
    return JSONResponse({"ok": True, "state": _api_state(db)})


@app.post("/api/subtasks/{subtask_id}/move")
def api_move_subtask(
    subtask_id: int, target_index: int, request: Request, db: Session = Depends(get_db)
) -> Response:
    response = tickets.move_subtask_to_index(subtask_id, target_index, request, db)
    if response.status_code >= 400:
        return response
    return JSONResponse({"ok": True, "state": _api_state(db)})


@app.post("/api/tickets/{ticket_id}/sync")
def api_sync_ticket(ticket_id: int, db: Session = Depends(get_db)) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return JSONResponse({"ok": False, "message": "Ticket was not found."}, status_code=404)
    try:
        jira_service.sync_ticket(ticket, db, jira_client_factory=JiraClient)
    except jira_service.JiraError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)
    return JSONResponse(
        {"ok": True, "message": f"{ticket.summary} synced to Jira.", "state": _api_state(db)}
    )


@app.post("/api/tickets/{ticket_id}/sync-from-jira")
def api_sync_ticket_from_jira(ticket_id: int, db: Session = Depends(get_db)) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return JSONResponse({"ok": False, "message": "Ticket was not found."}, status_code=404)
    try:
        jira_service.sync_ticket_from_jira(ticket, db, jira_client_factory=JiraClient)
    except jira_service.JiraError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)
    return JSONResponse(
        {
            "ok": True,
            "message": f"{ticket.summary} synced from Jira.",
            "state": _api_state(db),
        }
    )


@app.post("/api/categories")
def api_create_category(
    payload: CategoryPayload, db: Session = Depends(get_db)
) -> dict[str, object]:
    name = payload.name.strip()
    if name and db.scalar(select(Category).where(Category.name == name)) is None:
        db.add(Category(name=name))
        db.commit()
    return {"ok": True, "state": _api_state(db)}


@app.post("/api/components")
def api_create_component(payload: ComponentPayload, db: Session = Depends(get_db)) -> Response:
    name = payload.name.strip()
    if not is_safe_local_component_name(name):
        return JSONResponse(
            {"ok": False, "message": "Component name must be a safe local directory name."},
            status_code=422,
        )
    if name and db.scalar(select(Component).where(Component.name == name)) is None:
        db.add(Component(name=name))
        db.commit()
    return JSONResponse({"ok": True, "state": _api_state(db)})


@app.delete("/api/components/{component_id}")
def api_delete_component(component_id: int, db: Session = Depends(get_db)) -> Response:
    component = db.get(Component, component_id)
    if component is None:
        return JSONResponse({"ok": False, "message": "Component was not found."}, status_code=404)
    db.delete(component)
    db.commit()
    return JSONResponse({"ok": True, "state": _api_state(db)})


@app.post("/api/categories/{category_id}/components")
def api_assign_component(
    category_id: int,
    payload: CategoryComponentPayload,
    db: Session = Depends(get_db),
) -> Response:
    category = db.get(Category, category_id)
    component = db.get(Component, payload.component_id)
    if category is None:
        return JSONResponse({"ok": False, "message": "Category was not found."}, status_code=404)
    if component is None:
        return JSONResponse({"ok": False, "message": "Component was not found."}, status_code=404)
    if not is_safe_local_component_name(component.name):
        return JSONResponse(
            {"ok": False, "message": "Component name must be a safe local directory name."},
            status_code=422,
        )
    existing = db.get(CategoryComponent, (category_id, payload.component_id))
    if existing is None:
        position = db.scalar(
            select(CategoryComponent.position)
            .where(CategoryComponent.category_id == category_id)
            .order_by(CategoryComponent.position.desc())
        )
        db.add(
            CategoryComponent(
                category_id=category_id,
                component_id=payload.component_id,
                position=(position + 1 if position is not None else 0),
            )
        )
        db.commit()
    return JSONResponse({"ok": True, "state": _api_state(db)})


@app.delete("/api/categories/{category_id}/components/{component_id}")
def api_unassign_component(
    category_id: int, component_id: int, db: Session = Depends(get_db)
) -> Response:
    link = db.get(CategoryComponent, (category_id, component_id))
    if link is None:
        return JSONResponse(
            {"ok": False, "message": "Category component assignment was not found."},
            status_code=404,
        )
    db.delete(link)
    _normalize_category_components(category_id, db)
    db.commit()
    return JSONResponse({"ok": True, "state": _api_state(db)})


def _normalize_category_components(category_id: int, db: Session) -> None:
    links = list(
        db.scalars(
            select(CategoryComponent)
            .where(CategoryComponent.category_id == category_id)
            .order_by(CategoryComponent.position, CategoryComponent.component_id)
        )
    )
    for position, link in enumerate(links):
        link.position = position


@app.post("/api/categories/{category_id}/components/{component_id}/move")
def api_move_component(
    category_id: int,
    component_id: int,
    target_index: int,
    db: Session = Depends(get_db),
) -> Response:
    links = list(
        db.scalars(
            select(CategoryComponent)
            .where(CategoryComponent.category_id == category_id)
            .order_by(CategoryComponent.position, CategoryComponent.component_id)
        )
    )
    link = next((candidate for candidate in links if candidate.component_id == component_id), None)
    if link is None:
        return JSONResponse(
            {"ok": False, "message": "Category component assignment was not found."},
            status_code=404,
        )
    if target_index < 0 or target_index >= len(links):
        return JSONResponse(
            {"ok": False, "message": "Component target position is invalid."}, status_code=422
        )
    links.remove(link)
    links.insert(target_index, link)
    for position, candidate in enumerate(links):
        candidate.position = position
    db.commit()
    return JSONResponse({"ok": True, "state": _api_state(db)})


@app.delete("/api/categories/{category_id}")
def api_delete_category(category_id: int, db: Session = Depends(get_db)) -> Response:
    category = db.get(Category, category_id)
    if category is None:
        return JSONResponse({"ok": False, "message": "Category was not found."}, status_code=404)
    db.execute(update(Ticket).where(Ticket.category_id == category_id).values(category_id=None))
    db.delete(category)
    db.commit()
    return JSONResponse({"ok": True, "state": _api_state(db)})


@app.put("/api/settings/jira")
def api_save_jira_config(payload: JiraConfigPayload, db: Session = Depends(get_db)) -> Response:
    response = _save_jira_config(payload, db)
    if isinstance(response, JSONResponse):
        return response
    return JSONResponse({"ok": True, "message": response, "state": _api_state(db)})


def _save_jira_config(payload: JiraConfigPayload, db: Session) -> str | JSONResponse:
    existing = db.get(JiraConfig, 1)
    token = payload.api_token.strip() or (existing.api_token if existing is not None else "")
    normalized_base_url = payload.base_url.strip().rstrip("/")
    browser_base_url = payload.browser_base_url.strip().rstrip("/")
    local_projects_directory = payload.local_projects_directory.strip()
    if local_projects_directory:
        try:
            local_projects_path = Path(local_projects_directory).expanduser()
            local_projects_directory_is_valid = local_projects_path.is_dir()
        except (OSError, RuntimeError, ValueError):
            local_projects_directory_is_valid = False
        if not local_projects_directory_is_valid:
            return JSONResponse(
                {
                    "ok": False,
                    "message": "Local projects directory must exist and be a directory.",
                },
                status_code=422,
            )
        local_projects_directory = str(local_projects_path)
    values = {
        "base_url": normalized_base_url,
        "browser_base_url": browser_base_url,
        "local_projects_directory": local_projects_directory,
        "email": payload.email.strip(),
        "api_token": token,
        "project_key": payload.project_key.strip().upper(),
        "issue_type": payload.issue_type.strip(),
        "completed_statuses": payload.completed_statuses.strip() or "Done",
    }
    if not all(
        values[key] for key in ("base_url", "email", "api_token", "project_key", "issue_type")
    ):
        return JSONResponse(
            {"ok": False, "message": "All Jira connection fields are required."}, status_code=422
        )
    for url_key, label in (("base_url", "Jira API URL"), ("browser_base_url", "Jira browser URL")):
        if values[url_key] and not values[url_key].startswith(("https://", "http://")):
            return JSONResponse(
                {"ok": False, "message": f"{label} must start with http:// or https://."},
                status_code=422,
            )
    candidate = JiraConfig(id=1, **values)
    try:
        if payload.validate_connection:
            jira = JiraClient(candidate)
            try:
                jira.validate()
            finally:
                jira.close()
        if existing is None:
            db.add(candidate)
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        db.commit()
    except jira_service.JiraError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "message": f"Jira setup failed: {exc}"}, status_code=422)
    return (
        "Jira connection validated and saved."
        if payload.validate_connection
        else "Jira configuration saved."
    )
