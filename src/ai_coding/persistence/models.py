"""ORM models for the entities of ТЗ §13 plus the run event log and effect ledger.

Conventions (ADR-0005): UUIDv7 primary keys, UTC timestamps, ``version`` columns
for optimistic concurrency on mutable state (runs, tasks), JSON documents that map
to JSONB on PostgreSQL, append-only ``audit_events``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column, relationship

from ai_coding.persistence.types import UTCDateTime, utcnow, uuid7

JSONType = JSON().with_variant(JSONB(), "postgresql")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base with deterministic constraint names (stable migrations)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy API
        dict[str, Any]: JSONType,
        list[Any]: JSONType,
        datetime: UTCDateTime(),
        uuid.UUID: Uuid(),
    }


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid7)


def _created() -> Mapped[datetime]:
    return mapped_column(default=utcnow, nullable=False)


# --------------------------------------------------------------- identity ---
class User(Base):
    """A human or service principal."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    username: Mapped[str] = mapped_column(String(150), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # instance admin (S8)
    created_at: Mapped[datetime] = _created()


class Credential(Base):
    """Password hash of a user (argon2id; Self-Hosted server, ADR-0030)."""

    __tablename__ = "user_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    password_hash: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ApiToken(Base):
    """API token: only its SHA-256 is stored; optionally limited to one project/role."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    max_role: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime | None] = mapped_column()
    last_used_at: Mapped[datetime | None] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column()


class IdempotencyRecord(Base):
    """Stored response of a create request, replayed for the same Idempotency-Key."""

    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_idempotency_user_key"),)

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(500))
    body_sha256: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()


class Project(Base):
    """A managed software project (multi-project isolation boundary)."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(100), unique=True)
    root_path: Mapped[str | None] = mapped_column(Text)
    config_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    deleted_at: Mapped[datetime | None] = mapped_column()

    members: Mapped[list[ProjectMember]] = relationship(back_populates="project")


class ProjectMember(Base):
    """RBAC role of a user inside one project (ТЗ §3)."""

    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = _created()

    project: Mapped[Project] = relationship(back_populates="members")


class Repository(Base):
    """A source repository attached to a project (FR-01)."""

    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))  # local | git_url | github
    location: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    created_at: Mapped[datetime] = _created()


# -------------------------------------------------------------------- runs ---
class Run(Base):
    """One execution of a lifecycle mode; the unit of the run FSM (ТЗ §5)."""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_runs_project_idempotency"),
        Index("ix_runs_project_state", "project_id", "state"),
    )

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(20))
    goal: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    requested_by: Mapped[str] = mapped_column(String(150))
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    budget: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()

    tasks: Mapped[list[Task]] = relationship(
        back_populates="run", order_by="Task.created_at", cascade="all, delete-orphan"
    )

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012 - SQLAlchemy API


class RunJob(Base):
    """Server work queue entry: execute or resume a run (ADR-0013; leased by workers)."""

    __tablename__ = "run_jobs"
    __table_args__ = (Index("ix_run_jobs_state_available", "state", "available_at"),)

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # execute | resume
    state: Mapped[str] = mapped_column(String(16))  # queued | leased | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    available_at: Mapped[datetime] = mapped_column(default=utcnow)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column()


class RateLimitCounter(Base):
    """Requests per key and one-minute window, shared by every API process (B-807).

    ``key`` is a SHA-256 of the limited identity (user or client address), so neither
    addresses nor user ids are stored; old windows are purged.
    """

    __tablename__ = "rate_limit_counters"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    minute: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # unix time // 60
    count: Mapped[int] = mapped_column(Integer, default=0)


class RunEvent(Base):
    """Journal of run state transitions; idempotent by ``(run_id, idempotency_key)``."""

    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_run_events_run_key"),
    )

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    actor: Mapped[str] = mapped_column(String(150))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class Task(Base):
    """A DAG step of a run."""

    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("run_id", "key", name="uq_tasks_run_key"),)

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(300))
    phase: Mapped[str] = mapped_column(String(20))
    capability: Mapped[str] = mapped_column(String(100))
    handler: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=1800.0)
    requires_approval: Mapped[str | None] = mapped_column(String(40))
    budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    success_criteria: Mapped[list[Any]] = mapped_column(default=list)
    input: Mapped[dict[str, Any]] = mapped_column(default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()

    run: Mapped[Run] = relationship(back_populates="tasks")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012 - SQLAlchemy API


class TaskDependency(Base):
    """Edge of the task DAG: ``task_id`` depends on ``depends_on_id``."""

    __tablename__ = "task_dependencies"

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )


# ------------------------------------------------------------------ agents ---
class Agent(Base):
    """Registered agent/handler and its capabilities (FR-08)."""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(100), unique=True)
    version: Mapped[str] = mapped_column(String(40), default="1")
    capabilities: Mapped[list[Any]] = mapped_column(default=list)
    cost_per_call_usd: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class AgentExecution(Base):
    """One attempt of a task by an agent/handler."""

    __tablename__ = "agent_executions"

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(100))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float | None] = mapped_column(Float)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    evidence_refs: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column()


# --------------------------------------------------------------- approvals ---
class Approval(Base):
    """Human-in-the-loop approval request (ТЗ §5, FR-17)."""

    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_run_status", "run_id", "status"),)

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    operation: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(150))
    requested_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime | None] = mapped_column()
    decided_by: Mapped[str | None] = mapped_column(String(150))
    decided_role: Mapped[str | None] = mapped_column(String(20))
    decided_at: Mapped[datetime | None] = mapped_column()
    decision_note: Mapped[str | None] = mapped_column(Text)


# ------------------------------------------------------- artifacts/findings ---
class Artifact(Base):
    """Content-addressed artifact produced by a run (NFR-03)."""

    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(300))
    media_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    uri: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class Finding(Base):
    """Normalised finding from a tool or agent (FR-06)."""

    __tablename__ = "findings"
    __table_args__ = (Index("ix_findings_project_severity", "project_id", "severity"),)

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(100))
    rule_id: Mapped[str | None] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(500))
    message: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str | None] = mapped_column(Text)
    line: Mapped[int | None] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = _created()


class Policy(Base):
    """Versioned policy-as-code document (FR-11)."""

    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("project_id", "name", "version", name="uq_policies_pnv"),)

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column()
    sha256: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class ModelUsage(Base):
    """Token and cost accounting per LLM call (FR-15)."""

    __tablename__ = "model_usage"

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(200))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _created()


class AuditEvent(Base):
    """Append-only audit trail (ТЗ §3: actor, project_id, run_id, timestamp, outcome)."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_project_created", "project_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    created_at: Mapped[datetime] = _created()
    actor: Mapped[str] = mapped_column(String(150))
    actor_role: Mapped[str | None] = mapped_column(String(20))
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(20))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Checkpoint(Base):
    """Confirmed recovery point of a run (ТЗ §5, NFR-03)."""

    __tablename__ = "checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_checkpoints_run_seq"),)

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    seq: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(200))
    snapshot: Mapped[dict[str, Any]] = mapped_column()
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class Effect(Base):
    """Ledger of side effects for exactly-once application across restarts (AC-04)."""

    __tablename__ = "effects"
    __table_args__ = (UniqueConstraint("run_id", "key", name="uq_effects_run_key"),)

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    key: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20))  # intent | applied
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = _created()
    applied_at: Mapped[datetime | None] = mapped_column()


class AppendOnlyViolationError(RuntimeError):
    """An attempt was made to modify or delete an audit event."""


def _forbid(mapper: Mapper[Any], connection: Any, target: AuditEvent) -> None:
    raise AppendOnlyViolationError("audit_events is append-only")


event.listen(AuditEvent, "before_update", _forbid)
event.listen(AuditEvent, "before_delete", _forbid)
