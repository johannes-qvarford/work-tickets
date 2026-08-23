from __future__ import annotations

import os
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    tickets: Mapped[list[Ticket]] = relationship(back_populates="category")


class JiraConfig(Base):
    __tablename__ = "jira_config"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    base_url: Mapped[str] = mapped_column(String(300))
    browser_base_url: Mapped[str] = mapped_column(String(300), default="", server_default="")
    email: Mapped[str] = mapped_column(String(320))
    api_token: Mapped[str] = mapped_column(String(300))
    project_key: Mapped[str] = mapped_column(String(40))
    issue_type: Mapped[str] = mapped_column(String(80), default="Task")
    completed_statuses: Mapped[str] = mapped_column(String(500), default="Done")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    summary: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    planned_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    local_completed: Mapped[bool] = mapped_column(default=False)
    jira_issue_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    jira_status_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    category: Mapped[Category | None] = relationship(back_populates="tickets")
    parent: Mapped[Ticket | None] = relationship(remote_side=[id], back_populates="subtasks")
    subtasks: Mapped[list[Ticket]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by=lambda: (Ticket.position, Ticket.created_at, Ticket.id),
    )


engine = create_engine(
    os.environ.get("WORK_TICKETS_DATABASE_URL", "sqlite:///work-tickets.db"),
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    jira_columns = {column["name"] for column in inspect(engine).get_columns("jira_config")}
    if "browser_base_url" not in jira_columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE jira_config "
                    "ADD COLUMN browser_base_url VARCHAR(300) NOT NULL DEFAULT ''"
                )
            )
            connection.execute(
                text(
                    "UPDATE jira_config SET browser_base_url = base_url WHERE browser_base_url = ''"
                )
            )
