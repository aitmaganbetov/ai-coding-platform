"""The project's GitHub repository: Actions workflows and runs (ADR-0043).

Settings section ``github``: ``enabled`` (off: the server does not call GitHub for the
project and refuses GitHub Actions deployments), ``repository`` (``owner/name``),
``api_url`` (GitHub or GitHub Enterprise, https, a public address) and ``default_ref``.
The token is the project secret ``github.token`` (a fine-grained personal access token:
Actions read/write, Contents read), encrypted at rest and never returned (ADR-0043).
The server calls GitHub with
it for the project's members: who may read runs (``run.view``) and who may start a
workflow (``run.create_changes``) is the platform's RBAC, not the token's scope.

GitHub's errors become clear messages without the token (the client redacts it). The
outcome of the latest call (a check or a poll) is kept in the ``github_status`` section,
written by the server only, for the overview across projects (``GET /github/status``).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ai_coding.api.core import ApiError, ConflictError, InvalidError, NotFoundError, Principal
from ai_coding.api.project_settings import ProjectSettingsService
from ai_coding.api.rbac import Permission, allowed
from ai_coding.domain.enums import Outcome
from ai_coding.domain.errors import TransientError
from ai_coding.integrations.github.auth import GitHubAuthError, TokenAuth
from ai_coding.integrations.github.client import (
    DEFAULT_API_URL,
    GitHubAPIError,
    GitHubClient,
    RateLimitError,
    parse_repo_slug,
)
from ai_coding.integrations.github.host import GitHubHost
from ai_coding.persistence.models import Project, ProjectSecret, ProjectSetting
from ai_coding.workflows.deploy import PublicOnlySyncTransport, check_public_url

T = TypeVar("T")
SECTION = "github"
STATUS_SECTION = "github_status"  # the latest call's outcome (server-written)
TOKEN_SECRET = "github.token"  # noqa: S105 - the secret's name
WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")
REF = re.compile(r"^[A-Za-z0-9._/@^~-]{1,255}$")
INPUT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


class UpstreamError(ApiError):
    """GitHub refused or could not be reached; the message says why (no secrets).
    ``reason``: bad_token, forbidden, not_found, refused, rate_limited, unreachable,
    upstream."""

    status = 502
    code = "upstream"

    def __init__(self, message: str, reason: str = "upstream") -> None:
        super().__init__(message)
        self.reason = reason


class GitHubSetupError(ConflictError):
    """The project is not ready to call GitHub (``reason``: no_repository, no_token,
    invalid_settings, disabled)."""

    code = "github_setup"

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class GitHubDisabledError(GitHubSetupError):
    code = "github_disabled"

    def __init__(self, message: str) -> None:
        super().__init__(message, "disabled")


class GitHubSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    repository: str = Field(min_length=3, max_length=200)
    api_url: str = Field(default=DEFAULT_API_URL, max_length=300)
    default_ref: str = Field(default="main", max_length=255)

    @field_validator("repository")
    @classmethod
    def _repository(cls, value: str) -> str:
        return parse_repo_slug(value)

    @field_validator("api_url")
    @classmethod
    def _api_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith("https://"):
            raise ValueError("the GitHub API URL must use https")
        # GitHub Enterprise Server too must be at a public address: the server sends the
        # token there and shows what it answers (no probing of the internal network).
        return check_public_url(value)

    @field_validator("default_ref")
    @classmethod
    def _ref(cls, value: str) -> str:
        if not REF.fullmatch(value) or ".." in value:
            raise ValueError("not a valid git ref")
        return value


def _iso(value: Any) -> str | None:
    return str(value) if value else None


def _seconds(start: Any, end: Any) -> int | None:
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((b - a).total_seconds()))


def run_dict(run: dict[str, Any]) -> dict[str, Any]:
    """A workflow run as the Web UI shows it."""
    started = run.get("run_started_at") or run.get("created_at")
    done = run.get("status") == "completed"
    return {
        "id": run.get("id"),
        "name": run.get("name"),
        "title": run.get("display_title"),
        "workflow_id": run.get("workflow_id"),
        "path": run.get("path"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "branch": run.get("head_branch"),
        "sha": run.get("head_sha"),
        "event": run.get("event"),
        "actor": (run.get("actor") or {}).get("login"),
        "run_number": run.get("run_number"),
        "started_at": _iso(started),
        "updated_at": _iso(run.get("updated_at")),
        "duration_seconds": _seconds(started, run.get("updated_at")) if done else None,
        "html_url": run.get("html_url"),
    }


class GitHubService(ProjectSettingsService):
    """GitHub Actions of a project's repository, read and started through the server."""

    # Tests replace the HTTP transport (no network); production uses httpx's own.
    github_transport: httpx.BaseTransport | None = None

    # ------------------------------------------------------------ settings --
    def github_settings(self, principal: Principal, project_id: uuid.UUID) -> dict[str, Any]:
        """The section, whether a token is set, and (for those who manage it) its hint."""
        section = self.read_section(principal, project_id, SECTION)
        with self.session() as s:
            role = self.require(s, principal, project_id, Permission.PROJECT_VIEW)
            manage = allowed(role, Permission.PROJECT_SETTINGS)
            row = s.get(ProjectSecret, (project_id, TOKEN_SECRET))
            hint = row.hint if row is not None else None
        return {
            **section,
            "enabled": bool(section["data"].get("enabled")),
            "token_set": hint is not None,
            "token_hint": hint if manage else None,
            "can_manage": manage,
            "last": self.section(project_id, STATUS_SECTION) or None,
        }

    def put_github_settings(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        data: dict[str, Any],
        version: int | None = None,
    ) -> dict[str, Any]:
        """Replace the section; a ``version`` in ``data`` (the one read) guards against
        overwriting a change made meanwhile."""
        with self.session() as s:  # access first
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
        data = dict(data)
        if "version" in data:
            version = data.pop("version")
            if version is not None and (not isinstance(version, int) or isinstance(version, bool)):
                raise InvalidError("version: the version that was read (a number)")
        try:
            settings = GitHubSettings.model_validate(data)
        except (ValidationError, ValueError) as exc:
            raise InvalidError(_first_error(exc)) from exc
        return self.write_section(
            principal, project_id, SECTION, settings.model_dump(), version=version
        )

    # ------------------------------------------------------------- GitHub --
    def github_enabled(self, project_id: uuid.UUID) -> bool:
        return bool(self.section(project_id, SECTION).get("enabled"))

    def github_host(self, project_id: uuid.UUID, *, require_enabled: bool = True) -> GitHubHost:
        """A client of the project's repository with its token (server use only); refused
        while GitHub Actions are turned off for the project (an explicit connection check
        is allowed)."""
        data = self.section(project_id, SECTION)
        if require_enabled and data.get("repository") and not data.get("enabled"):
            raise GitHubDisabledError(
                "GitHub Actions are turned off for this project: "
                "turn them on in the project's integrations"
            )
        if not data.get("repository"):
            raise GitHubSetupError(
                "the project has no GitHub repository: set it in the project's integrations",
                "no_repository",
            )
        token = self.secret_value(project_id, TOKEN_SECRET)
        if not token:
            raise GitHubSetupError(
                "the project has no GitHub token (secret github.token): "
                "set it in the project's integrations",
                "no_token",
            )
        try:
            client = GitHubClient(
                TokenAuth(token),
                api_url=str(data.get("api_url") or DEFAULT_API_URL),
                # Only public addresses: the API URL is the project's setting (ADR-0043).
                transport=self.github_transport or PublicOnlySyncTransport(),
                timeout=15.0,
                max_retries=1,
                max_wait_seconds=5.0,
            )
        except (GitHubAuthError, ValueError) as exc:
            raise GitHubSetupError(
                f"the GitHub settings are not usable: {exc}", "invalid_settings"
            ) from exc
        return GitHubHost(client, str(data["repository"]))

    def github_check(self, principal: Principal, project_id: uuid.UUID) -> dict[str, Any]:
        """Whether the repository answers with the saved token, and what the token may do
        there; a failure is an answer too (``ok: false`` and why). Works while GitHub
        Actions are off, so a connection can be tested before it is turned on."""
        with self.session() as s:
            self.require(s, principal, project_id, Permission.PROJECT_VIEW)
        checked = _now()
        try:
            repo = self._github(
                project_id,
                lambda h: h.client.json("GET", f"/repos/{h.repo}"),
                kind="check",
                require_enabled=False,
            )
        except (GitHubSetupError, UpstreamError) as exc:
            return {"ok": False, "error": exc.message, "reason": exc.reason, "checked_at": checked}
        return {
            "ok": True,
            "repository": repo.get("full_name"),
            "private": repo.get("private"),
            "default_branch": repo.get("default_branch"),
            "html_url": repo.get("html_url"),
            "permissions": repo.get("permissions") or {},
            "checked_at": checked,
        }

    def github_status(self, principal: Principal) -> dict[str, Any]:
        """The GitHub connection of every project the caller can see: configured, on,
        and the latest call's outcome — for the infrastructure overview."""
        rows: list[dict[str, Any]] = []
        with self.session() as s:
            projects = s.scalars(
                select(Project).where(Project.deleted_at.is_(None)).order_by(Project.name)
            ).all()
            for project in projects:
                role = self.role_in(s, principal, project.id)
                if role is None or not allowed(role, Permission.PROJECT_VIEW):
                    continue
                sections = {
                    r.section: dict(r.data)
                    for r in s.scalars(
                        select(ProjectSetting).where(
                            ProjectSetting.project_id == project.id,
                            ProjectSetting.section.in_((SECTION, STATUS_SECTION)),
                        )
                    )
                }
                data = sections.get(SECTION, {})
                token = s.get(ProjectSecret, (project.id, TOKEN_SECRET)) is not None
                rows.append(
                    {
                        "project_id": str(project.id),
                        "name": project.name,
                        "repository": data.get("repository"),
                        "enabled": bool(data.get("enabled")),
                        "token_set": token,
                        "connected": bool(data.get("repository")) and token,
                        "last": sections.get(STATUS_SECTION),
                        "can_manage": allowed(role, Permission.PROJECT_SETTINGS),
                    }
                )
        return {
            "projects": rows,
            "connected": sum(1 for r in rows if r["connected"]),
            "enabled": sum(1 for r in rows if r["connected"] and r["enabled"]),
            "failing": sum(1 for r in rows if r["enabled"] and r["last"] and not r["last"]["ok"]),
        }

    def github_workflows(self, principal: Principal, project_id: uuid.UUID) -> list[dict[str, Any]]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.RUN_VIEW)
        items = self._github(
            project_id,
            lambda h: list(
                h.client.paginate(f"/repos/{h.repo}/actions/workflows", key="workflows", limit=100)
            ),
        )
        return [
            {
                "id": w.get("id"),
                "name": w.get("name"),
                "path": w.get("path"),
                "file": str(w.get("path") or "").rsplit("/", 1)[-1],
                "state": w.get("state"),
                "html_url": w.get("html_url"),
            }
            for w in items
        ]

    def github_runs(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        *,
        workflow: str | None = None,
        branch: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.RUN_VIEW)
        if workflow is not None and not WORKFLOW_FILE.fullmatch(workflow):
            raise InvalidError("workflow: a workflow file name such as ci.yml")
        if branch is not None and not REF.fullmatch(branch):
            raise InvalidError("branch: not a valid git ref")
        params = {"branch": branch} if branch else {}
        limit = max(1, min(limit, 100))

        def runs_of(h: GitHubHost) -> list[dict[str, Any]]:
            path = f"/repos/{h.repo}/actions/" + (
                f"workflows/{workflow}/runs" if workflow else "runs"
            )
            return list(h.client.paginate(path, params=params, key="workflow_runs", limit=limit))

        runs = self._github(project_id, runs_of)
        return [run_dict(r) for r in runs]

    def github_run_jobs(
        self, principal: Principal, project_id: uuid.UUID, gh_run_id: int | str
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.RUN_VIEW)
        if not str(gh_run_id).isdigit():
            raise NotFoundError("no such workflow run")
        run_id = int(gh_run_id)
        jobs = self._github(project_id, lambda h: h.workflow_jobs(run_id))
        return [
            {
                "id": j.get("id"),
                "name": j.get("name"),
                "status": j.get("status"),
                "conclusion": j.get("conclusion"),
                "started_at": _iso(j.get("started_at")),
                "completed_at": _iso(j.get("completed_at")),
                "html_url": j.get("html_url"),
                "steps": [
                    {
                        "number": st.get("number"),
                        "name": st.get("name"),
                        "status": st.get("status"),
                        "conclusion": st.get("conclusion"),
                    }
                    for st in j.get("steps") or []
                ],
            }
            for j in jobs
        ]

    def github_dispatch(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        workflow: str,
        ref: str,
        inputs: dict[str, str],
    ) -> dict[str, Any]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.RUN_CREATE_CHANGES)
        if not WORKFLOW_FILE.fullmatch(workflow):
            raise InvalidError("workflow: a workflow file name such as deploy.yml")
        if not REF.fullmatch(ref) or ".." in ref:
            raise InvalidError("ref: not a valid git ref")
        if len(inputs) > 10 or any(
            not INPUT_NAME.fullmatch(k) or len(v) > 1000 for k, v in inputs.items()
        ):
            raise InvalidError(
                "inputs: at most 10 names (letters, digits, '_', '-') with short values"
            )
        self._github(
            project_id, lambda h: h.dispatch_workflow(workflow, ref=ref, inputs=dict(inputs))
        )
        with self.session() as s:
            self.record(
                s,
                principal.username,
                "github.dispatch",
                Outcome.SUCCESS,
                project_id=project_id,
                target=workflow,
                details={"ref": ref, "inputs": sorted(inputs)},
            )
        return {"ok": True, "workflow": workflow, "ref": ref}

    # -------------------------------------------------------------- helpers --
    def _github(
        self,
        project_id: uuid.UUID,
        fn: Callable[[GitHubHost], T],
        *,
        kind: str = "poll",
        require_enabled: bool = True,
    ) -> T:
        """One use of the project's repository (the client is closed afterwards); GitHub's
        answer — success or why not — is noted for the overview."""
        host = self.github_host(project_id, require_enabled=require_enabled)
        try:
            result = self._call(lambda: fn(host))
        except UpstreamError as exc:
            self._note(project_id, kind, exc)
            raise
        finally:
            host.client.close()
        self._note(project_id, kind, None)
        return result  # type: ignore[no-any-return]

    def _note(self, project_id: uuid.UUID, kind: str, error: UpstreamError | None) -> None:
        data = {
            "ok": error is None,
            "error": error.message if error else None,
            "reason": error.reason if error else None,
            "kind": kind,
            "at": _now(),
        }
        try:
            with self.session() as s:
                row = s.get(ProjectSetting, (project_id, STATUS_SECTION))
                if row is None:
                    s.add(
                        ProjectSetting(
                            project_id=project_id,
                            section=STATUS_SECTION,
                            data=data,
                            version=1,
                            updated_by="server",
                        )
                    )
                elif row.data != data:
                    row.data = data
        except IntegrityError:  # noted by a concurrent call at the same moment
            pass

    @staticmethod
    def _call(fn: Any) -> Any:
        """Run a GitHub call; its failure becomes a message the user can act on."""
        try:
            return fn()
        except GitHubAPIError as exc:
            reason = _REASONS.get(exc.status, "upstream")
            raise UpstreamError(github_message(exc.status, str(exc)), reason) from None
        except RateLimitError:
            raise UpstreamError(
                "GitHub rate limit exceeded: try again later", "rate_limited"
            ) from None
        except TransientError:
            raise UpstreamError(
                "GitHub is unreachable from the server: try again later", "unreachable"
            ) from None
        except ValueError as exc:
            raise InvalidError(str(exc)) from None


_REASONS = {401: "bad_token", 403: "forbidden", 404: "not_found", 422: "refused"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def github_message(status: int, detail: str) -> str:
    """GitHub's answer in words: what is wrong and what to check."""
    if status == 401:
        return "GitHub rejected the token (401): it is invalid, expired or revoked — set a new one"
    if status == 403:
        return (
            "the token may not do this (403): give it access to the repository (Actions, Contents)"
        )
    if status == 404:
        return (
            "not found on GitHub (404): check the repository, the workflow and the token's access"
        )
    if status == 422:
        return f"GitHub refused the request (422): {detail.split(': ', 1)[-1][:200]}"
    return f"GitHub answered {status}: {detail.split(': ', 1)[-1][:200]}"


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        err = exc.errors()[0]
        field = ".".join(str(p) for p in err.get("loc", ()))
        return f"{field}: {str(err.get('msg', 'invalid')).removeprefix('Value error, ')}"
    return str(exc)


__all__ = [
    "SECTION",
    "TOKEN_SECRET",
    "GitHubDisabledError",
    "GitHubService",
    "GitHubSettings",
    "GitHubSetupError",
    "UpstreamError",
    "github_message",
    "run_dict",
]
