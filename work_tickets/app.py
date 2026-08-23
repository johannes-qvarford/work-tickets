from __future__ import annotations

from collections.abc import Generator
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Category, SessionLocal, Ticket, init_db

app = FastAPI(title="Work Tickets")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.on_event("startup")
def startup() -> None:
    init_db()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Annotated[Session, Depends(get_db)]) -> HTMLResponse:
    tickets = list(db.scalars(select(Ticket).order_by(Ticket.position, Ticket.created_at)))
    categories = list(db.scalars(select(Category).order_by(Category.name)))
    today = date.today()
    today_tickets = [
        ticket
        for ticket in tickets
        if (
            not ticket.local_completed
            and ticket.planned_date is not None
            and ticket.planned_date <= today
        )
    ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "tickets": tickets,
            "today_tickets": today_tickets,
            "categories": categories,
            "today": today,
        },
    )


@app.post("/tickets")
def create_ticket(
    summary: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    planned_date: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    count = db.scalar(select(func.count()).select_from(Ticket)) or 0
    ticket = Ticket(summary=summary.strip(), description=description, position=count)
    ticket.planned_date = date.fromisoformat(planned_date) if planned_date else None
    ticket.category_id = int(category_id) if category_id else None
    db.add(ticket)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/tickets/{ticket_id}")
def update_ticket(
    ticket_id: int,
    summary: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    planned_date: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is not None and summary.strip():
        ticket.summary = summary.strip()
        ticket.description = description
        ticket.planned_date = date.fromisoformat(planned_date) if planned_date else None
        ticket.category_id = int(category_id) if category_id else None
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/tickets/{ticket_id}/complete")
def complete_ticket(ticket_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is not None:
        ticket.local_completed = not ticket.local_completed
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/categories")
def create_category(
    name: Annotated[str, Form()], db: Session = Depends(get_db)
) -> RedirectResponse:
    if name.strip() and db.scalar(select(Category).where(Category.name == name.strip())) is None:
        db.add(Category(name=name.strip()))
        db.commit()
    return RedirectResponse("/", status_code=303)
