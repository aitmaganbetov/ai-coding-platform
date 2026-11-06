"""Deployments: environments, deploy runs, history (ADR-0048).

An environment says where and how a project deploys:

* ``github_actions`` — a ``workflow_dispatch`` workflow of the project's GitHub repository
  (ADR-0043), started with the ref and, as inputs, the environment's name and the ref;
* ``webhook`` — an https endpoint (a CD system, an internal deployer) called with a JSON
  body signed with the environment's secret (``deploy.<name>.webhook``).

``production`` environments, and those with ``requires_approval``, wait for a person's
approval (``run.approve``) before anything is started; ``require_gates`` refuses a
deployment unless the latest completed audit passed its blocking Quality Gates. A
deployment is a ``deploy`` run of the orchestrator (states, approval, audit trail) and a
``deployments`` row the Web UI lists; the row follows the run.

Rights: reading environments ``project.view``, changing them ``project.settings``,
deploying ``deploy.run`` (owners and maintainers), the history ``run.view``.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import delete, or_, select, update

from ai_coding.api.core import ConflictError, InvalidError, NotFoundError, Principal, iso_or_none
from ai_coding.api.github_service import (
    REF,
    WORKFLOW_FILE,
    GitHubDisabledError,
    GitHubService,
    _first_error,
)
from ai_coding.api.rbac import Permission, allowed
from ai_coding.domain.enums import Outcome
from ai_coding.persistence.models import DeployEnvironment, Deployment, Run, Task
from ai_coding.persistence.types import utcnow
from ai_coding.workflows import deploy as deploy_flow
from ai_coding.workflows.deploy import DeploymentSpec, EnvironmentSpec, check_public_url

ENV_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
INPUT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
ACTIVE = ("queued", "awaiting_approval", "running")
RUN_TO_STATE = {
    "AWAITING_APPROVAL": "awaiting_approval",
    "RUNNING": "running",
    "VERIFYING": "running",
    "COMPLETED": "succeeded",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
}


def secret_name(environment: str) -> str:
    """The project secret that signs the environment's webhook."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", environment.lower()).strip("-")[:48] or "env"
    return f"deploy.{slug}.webhook"


class EnvironmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    kind: Literal["github_actions", "webhook"]
    production: bool = False
    requires_approval: bool = False
    require_gates: bool = False
    position: int = Field(default=0, ge=0, le=1000)
    workflow: str | None = Field(default=None, max_length=200)
    environment_input: str | None = Field(default="environment", max_length=64)
    ref_input: str | None = Field(default=None, max_length=64)
    webhook_url: str | None = Field(default=None, max_length=2000)
    health_url: str | None = Field(default=None, max_length=2000)
    timeout_minutes: int = Field(default=30, ge=1, le=deploy_flow.MAX_TIMEOUT_MINUTES)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not ENV_NAME.fullmatch(value):
            raise ValueError("letters, digits, spaces, '.', '_' or '-' (64 at most)")
        return value

    @field_validator("environment_input", "ref_input")
    @classmethod
    def _input(cls, value: str | None) -> str | None:
        value = (value or "").strip() or None
        if value is not None and not INPUT_NAME.fullmatch(value):
            raise ValueError("an input name: letters, digits, '_' or '-'")
        return value

    @field_validator("webhook_url", "health_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        value = (value or "").strip() or None
        return check_public_url(value) if value is not None else None

    @model_validator(mode="after")
    def _kind(self) -> EnvironmentIn:
        if self.kind == "github_actions":
            if not self.workflow or not WORKFLOW_FILE.fullmatch(self.workflow):
                raise ValueError("workflow: the workflow file name, e.g. deploy.yml")
            self.webhook_url = None
        else:
            if not self.webhook_url:
                raise ValueError("webhook_url: the https URL that receives deployments")
            self.workflow = self.environment_input = self.ref_input = None
        return self

    def config(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "environment_input": self.environment_input,
            "ref_input": self.ref_input,
            "webhook_url": self.webhook_url,
            "health_url": self.health_url,
            "timeout_minutes": self.timeout_minutes,
            "secret_name": secret_name(self.name) if self.kind == "webhook" else None,
        }


def _validated(data: dict[str, Any]) -> EnvironmentIn:
    try:
        return EnvironmentIn.model_validate(data)
    except (ValidationError, ValueError) as exc:
        raise InvalidError(_first_error(exc)) from exc


def _id(value: uuid.UUID | str) -> uuid.UUID:
    """An id from a path or a body, parsed after the access checks (a malformed one is
    simply not found)."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise NotFoundError("no such environment in this project") from None


def _deployment_dict(d: Deployment, env_name: str | None = None) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "environment_id": str(d.environment_id),
        "environment": env_name,
        "ref": d.ref,
        "commit": d.commit,
        "state": d.state,
        "external_url": d.external_url,
        "detail": d.detail,
        "requested_by": d.requested_by,
        "run_id": str(d.run_id) if d.run_id else None,
        "created_at": iso_or_none(d.created_at),
        "finished_at": iso_or_none(d.finished_at),
    }


def _env_dict(e: DeployEnvironment) -> dict[str, Any]:
    cfg = dict(e.config or {})
    return {
        "id": str(e.id),
        "name": e.name,
        "kind": e.kind,
        "production": e.production,
        "requires_approval": e.requires_approval or e.production,
        "require_gates": e.require_gates,
        "position": e.position,
        "workflow": cfg.get("workflow"),
        "environment_input": cfg.get("environment_input"),
        "ref_input": cfg.get("ref_input"),
        "webhook_url": cfg.get("webhook_url"),
        "health_url": cfg.get("health_url"),
        "timeout_minutes": cfg.get("timeout_minutes", 30),
        "secret_name": cfg.get("secret_name"),
        "created_at": iso_or_none(e.created_at),
        "updated_at": iso_or_none(e.updated_at),
    }


class DeployService(GitHubService):
    """Environments and deployments of projects; serves the deploy run's steps."""

    # Tests replace the transport of webhooks and health checks; by default only public
    # https addresses are reachable (workflows/deploy.py: PublicOnlyTransport).
    deploy_test_transport: httpx.AsyncBaseTransport | None = None

    if TYPE_CHECKING:  # RunService, mixed into the same Server

        def create_run(
            self,
            principal: Principal,
            project_id: uuid.UUID,
            kind: str,
            params: dict[str, Any],
            *,
            idempotency_key: str | None = None,
            source: Path | None = None,
            goal: str | None = None,
        ) -> uuid.UUID: ...

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        deploy_flow.register_port(self)

    def close(self) -> None:
        if deploy_flow._PORT is self:  # this process registration
            deploy_flow.register_port(None)
        super().close()

    # -------------------------------------------------------- environments --
    def environments(self, principal: Principal, project_id: uuid.UUID) -> dict[str, Any]:
        """The project's environments with their latest deployment, and what the caller
        may do with them."""
        with self.session() as s:
            role = self.require(s, principal, project_id, Permission.PROJECT_VIEW)
        self._sync(project_id)
        with self.session() as s:
            envs = s.scalars(
                select(DeployEnvironment)
                .where(DeployEnvironment.project_id == project_id)
                .order_by(DeployEnvironment.position, DeployEnvironment.name)
            ).all()
            out = []
            for e in envs:
                last = s.scalars(
                    select(Deployment)
                    .where(Deployment.environment_id == e.id)
                    .order_by(Deployment.created_at.desc())
                    .limit(1)
                ).first()
                out.append(
                    {
                        **_env_dict(e),
                        "last_deployment": _deployment_dict(last, e.name) if last else None,
                    }
                )
        return {
            "environments": out,
            "can_manage": allowed(role, Permission.PROJECT_SETTINGS),
            "can_deploy": allowed(role, Permission.DEPLOY),
        }

    def create_environment(
        self, principal: Principal, project_id: uuid.UUID, data: dict[str, Any]
    ) -> dict[str, Any]:
        with self.session() as s:  # access first
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
        spec = _validated(data)
        with self.session() as s:
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
            self._unique(s, project_id, spec.name, None)
            env = DeployEnvironment(
                project_id=project_id,
                name=spec.name,
                kind=spec.kind,
                production=spec.production,
                requires_approval=spec.requires_approval,
                require_gates=spec.require_gates,
                position=spec.position,
                config=spec.config(),
            )
            s.add(env)
            s.flush()
            self.record(
                s,
                principal.username,
                "deploy.environment.create",
                Outcome.SUCCESS,
                project_id=project_id,
                target=spec.name,
            )
            return {**_env_dict(env), "last_deployment": None}

    def update_environment(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        environment_id: uuid.UUID | str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
        environment_id = _id(environment_id)
        spec = _validated(data)
        with self.session() as s:
            env = self._environment(s, project_id, environment_id)
            self._unique(s, project_id, spec.name, environment_id)
            old_secret = (env.config or {}).get("secret_name")
            env.name, env.kind, env.production = spec.name, spec.kind, spec.production
            env.requires_approval, env.require_gates, env.position = (
                spec.requires_approval,
                spec.require_gates,
                spec.position,
            )
            env.config, env.updated_at = spec.config(), utcnow()
            self.record(
                s,
                principal.username,
                "deploy.environment.update",
                Outcome.SUCCESS,
                project_id=project_id,
                target=spec.name,
            )
            out = {**_env_dict(env), "last_deployment": None}
        new_secret = out["secret_name"]
        if old_secret and old_secret != new_secret:  # renamed: the signing secret follows
            value = self.secret_value(project_id, str(old_secret))
            if value is not None and new_secret:
                self.set_secret(principal, project_id, new_secret, value)
            self.delete_secret(principal, project_id, str(old_secret))
        return out

    def delete_environment(
        self, principal: Principal, project_id: uuid.UUID, environment_id: uuid.UUID | str
    ) -> None:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
        environment_id = _id(environment_id)
        self._sync(project_id)
        with self.session() as s:
            env = self._environment(s, project_id, environment_id)
            secret = (env.config or {}).get("secret_name")
            busy = s.scalars(
                select(Deployment).where(
                    Deployment.environment_id == env.id, Deployment.state.in_(ACTIVE)
                )
            ).first()
            if busy is not None:
                raise ConflictError(
                    f"a deployment to '{env.name}' is in progress: wait or cancel it"
                )
            self.record(
                s,
                principal.username,
                "deploy.environment.delete",
                Outcome.SUCCESS,
                project_id=project_id,
                target=env.name,
            )
            s.execute(delete(Deployment).where(Deployment.environment_id == env.id))
            s.delete(env)
        if secret:
            self.delete_secret(principal, project_id, str(secret))

    # --------------------------------------------------------- deployments --
    def start_deployment(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        environment_id: uuid.UUID | str,
        ref: str,
    ) -> dict[str, Any]:
        """Deploy ``ref`` to the environment: a ``deploy`` run (approval if required)."""
        with self.session() as s:  # access first
            self.require(s, principal, project_id, Permission.DEPLOY)
        environment_id = _id(environment_id)
        ref = ref.strip()
        if not REF.fullmatch(ref) or ".." in ref:
            raise InvalidError("ref: a branch, tag or commit")
        self._sync(project_id)
        with self.session() as s:
            env = self._environment(s, project_id, environment_id)
            busy = s.scalars(
                select(Deployment).where(
                    Deployment.environment_id == env.id, Deployment.state.in_(ACTIVE)
                )
            ).first()
            if busy is not None:
                raise ConflictError(f"a deployment to '{env.name}' is already in progress")
            name, gates, kind = env.name, env.require_gates, env.kind
        if kind == "github_actions" and not self.section(project_id, "github").get("repository"):
            raise ConflictError(
                f"'{name}' deploys with GitHub Actions, but the project has no GitHub "
                "repository: set it in the project's integrations"
            )
        if kind == "github_actions" and not self.github_enabled(project_id):
            raise GitHubDisabledError(
                f"'{name}' deploys with GitHub Actions, which are turned off for this project: "
                "turn them on in the project's integrations"
            )
        if gates:  # fast feedback; the run checks again right before deploying
            latest = self.latest_gates(project_id)
            failed = [
                g.get("gate")
                for g in (latest[1] if latest else [])
                if g.get("result") == "FAIL" and g.get("blocking")
            ]
            if latest is None or failed:
                why = (
                    "no completed audit"
                    if latest is None
                    else "failed blocking gates: " + ", ".join(map(str, failed))
                )
                raise ConflictError(
                    f"'{name}' requires passing Quality Gates ({why}): "
                    "run an audit and fix them first"
                )
        with self.session() as s:
            row = Deployment(
                project_id=project_id,
                environment_id=environment_id,
                ref=ref,
                state="queued",
                requested_by=principal.username,
            )
            s.add(row)
            s.flush()
            deployment_id = row.id
        params = {
            "deployment_id": str(deployment_id),
            "environment_id": str(environment_id),
            "ref": ref,
        }
        try:
            run_id = self.create_run(
                principal, project_id, "deploy", params, goal=f"deploy {ref} → {name}"
            )
        except Exception:
            with self.session() as s:
                gone = s.get(Deployment, deployment_id)
                if gone is not None:
                    s.delete(gone)
            raise
        self.claim_deployment(deployment_id, run_id)
        with self.session() as s:
            self.record(
                s,
                principal.username,
                "deploy.start",
                Outcome.SUCCESS,
                project_id=project_id,
                run_id=run_id,
                target=name,
                details={"ref": ref},
            )
            saved = s.get(Deployment, deployment_id)
            if saved is None:  # deleted in between (the environment was removed)
                raise NotFoundError("the deployment no longer exists")
            return _deployment_dict(saved, name)

    def deployments(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        environment_id: uuid.UUID | str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.RUN_VIEW)
        environment_id = _id(environment_id) if environment_id else None
        self._sync(project_id)
        with self.session() as s:
            query = (
                select(Deployment, DeployEnvironment.name)
                .join(DeployEnvironment, DeployEnvironment.id == Deployment.environment_id)
                .where(Deployment.project_id == project_id)
            )
            if environment_id is not None:
                query = query.where(Deployment.environment_id == environment_id)
            rows = s.execute(
                query.order_by(Deployment.created_at.desc()).limit(max(1, min(limit, 200)))
            ).all()
            return [_deployment_dict(d, name) for d, name in rows]

    # --------------------------------------------------- the deploy run's port --
    def deployment_spec(self, deployment_id: uuid.UUID) -> DeploymentSpec | None:
        with self.session() as s:
            row = s.get(Deployment, deployment_id)
            env = s.get(DeployEnvironment, row.environment_id) if row is not None else None
            if row is None or env is None:
                return None
            return DeploymentSpec(
                id=row.id,
                project_id=row.project_id,
                run_id=row.run_id,
                ref=row.ref,
                requested_by=row.requested_by,
                environment=EnvironmentSpec(
                    id=env.id,
                    name=env.name,
                    kind=env.kind,
                    production=env.production,
                    requires_approval=env.requires_approval,
                    require_gates=env.require_gates,
                    config=dict(env.config or {}),
                ),
            )

    def claim_deployment(self, deployment_id: uuid.UUID, run_id: uuid.UUID) -> bool:
        """The run owns the deployment (the first to claim it; a reused id is refused)."""
        with self.session() as s:
            done = s.execute(
                update(Deployment)
                .where(
                    Deployment.id == deployment_id,
                    or_(Deployment.run_id.is_(None), Deployment.run_id == run_id),
                )
                .values(run_id=run_id)
            )
            return bool(done.rowcount)  # type: ignore[attr-defined]

    def update_deployment(self, deployment_id: uuid.UUID, **fields: Any) -> None:
        finished = bool(fields.pop("finished", False))
        allowed_fields = {"state", "commit", "external_url", "detail"}
        with self.session() as s:
            row = s.get(Deployment, deployment_id)
            if row is None:
                return
            for key, value in fields.items():
                if key in allowed_fields:
                    setattr(row, key, value)
            if finished:
                row.finished_at = utcnow()

    def webhook_secret(self, project_id: uuid.UUID, environment: EnvironmentSpec) -> str | None:
        name = environment.config.get("secret_name") or secret_name(environment.name)
        return self.secret_value(project_id, str(name))

    def latest_gates(self, project_id: uuid.UUID) -> tuple[uuid.UUID, list[dict[str, Any]]] | None:
        """The Quality Gates of the project's latest completed audit."""
        with self.session() as s:
            runs = s.scalars(
                select(Run)
                .where(Run.project_id == project_id, Run.state == "COMPLETED")
                .order_by(Run.created_at.desc())
                .limit(100)
            )
            for run in runs:
                if (run.options or {}).get("kind") != "audit":
                    continue
                task = s.scalars(
                    select(Task).where(Task.run_id == run.id, Task.key == "quality.gates")
                ).first()
                gates = (task.output or {}).get("gates") if task is not None else None
                if isinstance(gates, list):
                    return run.id, [dict(g) for g in gates if isinstance(g, dict)]
        return None

    def deploy_transport(self) -> httpx.AsyncBaseTransport | None:
        return self.deploy_test_transport

    # -------------------------------------------------------------- helpers --
    def _environment(
        self, s: Any, project_id: uuid.UUID, environment_id: uuid.UUID
    ) -> DeployEnvironment:
        env = s.get(DeployEnvironment, environment_id)
        if env is None or env.project_id != project_id:
            raise NotFoundError("no such environment in this project")
        return env  # type: ignore[no-any-return]

    @staticmethod
    def _unique(s: Any, project_id: uuid.UUID, name: str, own: uuid.UUID | None) -> None:
        others = s.scalars(
            select(DeployEnvironment).where(DeployEnvironment.project_id == project_id)
        ).all()
        for other in others:
            if other.id != own and secret_name(other.name) == secret_name(name):
                raise ConflictError(f"the project already has an environment '{other.name}'")

    def _sync(self, project_id: uuid.UUID) -> None:
        """Deployments follow their runs (a run can end without its last step: a failure,
        a rejected approval, a cancellation)."""
        with self.session() as s:
            rows = s.execute(
                select(Deployment, Run)
                .join(Run, Run.id == Deployment.run_id)
                .where(Deployment.project_id == project_id, Deployment.state.in_(ACTIVE))
            ).all()
            for row, run in rows:
                state = RUN_TO_STATE.get(run.state)
                if state is None or state == row.state:
                    continue
                if (
                    state == "running"
                    and row.state == "awaiting_approval"
                    and run.state == "RUNNING"
                ):
                    state = "running"
                row.state = state
                if state in ("succeeded", "failed", "cancelled"):
                    row.finished_at = row.finished_at or run.finished_at or utcnow()
                    if state != "succeeded" and not row.detail:
                        row.detail = (run.error or "")[:2000] or None


__all__ = ["DeployService", "EnvironmentIn", "secret_name"]
