from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from viracase.domain.rules import utcnow


def new_id():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class AIConnection(Base):
    __tablename__ = "ai_connections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(16))
    auth_mode: Mapped[str] = mapped_column(String(24))
    credential: Mapped[str] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    takes_over: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("provider IN ('openai','gemini')"),
        CheckConstraint("auth_mode IN ('api_key','service_account')"),
    )


class AIModel(Base):
    __tablename__ = "ai_models"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    connection_id: Mapped[str] = mapped_column(ForeignKey("ai_connections.id"), index=True)
    model: Mapped[str] = mapped_column(String(160))
    name: Mapped[str] = mapped_column(String(100))
    policy: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    verified_connection_version: Mapped[int | None] = mapped_column(Integer)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("connection_id", "model"),)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    login: Mapped[str] = mapped_column(String(160), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="student")
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    lifecycle: Mapped[str] = mapped_column(String(24), default="active")
    entitlement_version: Mapped[int] = mapped_column(Integer, default=0)
    fence: Mapped[str | None] = mapped_column(String(36))
    current_period_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (CheckConstraint("role IN ('student','admin')"),)


class Session(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    audience: Mapped[str] = mapped_column(String(16))
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Period(Base):
    __tablename__ = "access_periods"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    purge_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer)
    offer_version: Mapped[str] = mapped_column(String(40), default="2026-10-v1")
    __table_args__ = (
        UniqueConstraint("id", "account_id"),
        CheckConstraint("starts_at < expires_at AND expires_at < purge_at"),
    )


class Grant(Base):
    __tablename__ = "budget_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    period_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(24))
    total: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    available: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    reserved: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal(0))
    consumed: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal(0))
    expired: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal(0))
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["period_id", "account_id"], ["access_periods.id", "access_periods.account_id"]),
        UniqueConstraint("id", "account_id"),
        CheckConstraint("total >= 0 AND available >= 0 AND reserved >= 0 AND consumed >= 0 AND expired >= 0"),
        CheckConstraint("total = available + reserved + consumed + expired"),
    )


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    objective: Mapped[str] = mapped_column(Text)
    stack: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, default=1)
    current_revision_id: Mapped[str | None] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(24), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("id", "account_id"),)


class Revision(Base):
    __tablename__ = "revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    origin: Mapped[str] = mapped_column(String(24))
    candidate: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "account_id"], ["projects.id", "projects.account_id"]),
        UniqueConstraint("id", "project_id", "account_id"),
    )


class Draft(Base):
    __tablename__ = "workspace_drafts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("account_id", "kind", "target"),)


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    progress_stage: Mapped[str] = mapped_column(String(60), default="queued")
    billing_status: Mapped[str] = mapped_column(String(32), default="not_applicable")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "account_id"], ["projects.id", "projects.account_id"]),
        UniqueConstraint("id", "account_id"),
    )


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    operation_id: Mapped[str] = mapped_column(String(36), index=True)
    grant_id: Mapped[str] = mapped_column(String(36))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    state: Mapped[str] = mapped_column(String(20), default="reserved")
    __table_args__ = (
        ForeignKeyConstraint(["operation_id", "account_id"], ["operations.id", "operations.account_id"]),
        ForeignKeyConstraint(["grant_id", "account_id"], ["budget_grants.id", "budget_grants.account_id"]),
        UniqueConstraint("operation_id", "grant_id"),
        CheckConstraint("amount > 0"),
    )


class Ledger(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    effect_key: Mapped[str] = mapped_column(String(180), unique=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    grant_id: Mapped[str] = mapped_column(ForeignKey("budget_grants.id"))
    operation_id: Mapped[str | None] = mapped_column(ForeignKey("operations.id"))
    kind: Mapped[str] = mapped_column(String(24))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    state: Mapped[str] = mapped_column(String(24), default="queued")
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_job_poll", "state", "available_at"),)


class Attempt(Base):
    __tablename__ = "provider_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(24))
    external_id: Mapped[str | None] = mapped_column(String(180))
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    actual_brl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Payment(Base):
    __tablename__ = "manual_payments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    operator_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    reference: Mapped[str] = mapped_column(String(160), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    credit: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    kind: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Idempotency(Base):
    __tablename__ = "idempotency"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    command: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(120))
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("actor_id", "command", "key"),)


class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    action: Mapped[str] = mapped_column(String(120))
    resource_id: Mapped[str] = mapped_column(String(60))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    resets_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PurgeInventory(Base):
    __tablename__ = "purge_inventory"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    fence: Mapped[str] = mapped_column(String(36))
    destination: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(24), default="pending")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("account_id", "fence", "destination"),)
