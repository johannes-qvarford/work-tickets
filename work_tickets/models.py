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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .db_migrations import apply_migrations


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    tickets: Mapped[list[Ticket]] = relationship(back_populates="category")
    component_links: Mapped[list[CategoryComponent]] = relationship(
        back_populates="category",
        cascade="all, delete-orphan",
        order_by=lambda: CategoryComponent.position,
    )


class Component(Base):
    __tablename__ = "components"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    category_links: Mapped[list[CategoryComponent]] = relationship(
        back_populates="component",
        cascade="all, delete-orphan",
    )


class CategoryComponent(Base):
    __tablename__ = "category_components"
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), primary_key=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("components.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[Category] = relationship(back_populates="component_links")
    component: Mapped[Component] = relationship(back_populates="category_links")


class JiraConfig(Base):
    __tablename__ = "jira_config"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    base_url: Mapped[str] = mapped_column(String(300))
    browser_base_url: Mapped[str] = mapped_column(String(300), default="", server_default="")
    local_projects_directory: Mapped[str] = mapped_column(
        String(1000), default="", server_default=""
    )
    email: Mapped[str] = mapped_column(String(320))
    api_token: Mapped[str] = mapped_column(String(300))
    project_key: Mapped[str] = mapped_column(String(40))
    issue_type: Mapped[str] = mapped_column(String(80), default="Task")
    completed_statuses: Mapped[str] = mapped_column(String(500), default="Done")
    in_review_status: Mapped[str] = mapped_column(
        String(80), default="In Review", server_default="In Review"
    )
    ready_to_merge_status: Mapped[str] = mapped_column(
        String(80), default="Ready to Merge", server_default="Ready to Merge"
    )
    ready_to_deploy_status: Mapped[str] = mapped_column(
        String(80), default="Ready to Deploy", server_default="Ready to Deploy"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    summary: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    component: Mapped[str | None] = mapped_column(String(120), nullable=True)
    category: Mapped[Category | None] = relationship(back_populates="tickets")
    parent: Mapped[Ticket | None] = relationship(remote_side=[id], back_populates="subtasks")
    subtasks: Mapped[list[Ticket]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by=lambda: (
            Ticket.local_completed,
            Ticket.position,
            Ticket.created_at,
            Ticket.id,
        ),
    )


engine = create_engine(
    os.environ.get("WORK_TICKETS_DATABASE_URL", "sqlite:///work-tickets.db"),
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    apply_migrations(engine)
