"""Server services (S8, B-701): authentication, tokens, RBAC, project isolation (AC-08)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from ai_coding.api.rbac import MATRIX, Permission, allowed, capped
from ai_coding.api.security import WeakPasswordError, token_hash
from ai_coding.api.services import (
    ConflictError,
    ForbiddenError,
    InvalidError,
    NotFoundError,
    Principal,
    Server,
)
from ai_coding.api.settings import ServerSettings
from ai_coding.domain.enums import Role
from ai_coding.persistence.models import ApiToken, AuditEvent, RunJob
from ai_coding.persistence.types import utcnow

PASSWORD = "correct horse battery staple"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    roots = tmp_path / "repos"
    roots.mkdir()
    settings = ServerSettings(data_dir=tmp_path / "data",
                              database_url=f"sqlite:///{tmp_path / 'server.db'}",
                              allowed_roots=(roots.resolve(),))  # fmt: skip
    srv = Server(settings)
    srv.migrate()
    yield srv
    srv.close()


def make_repo(server: Server, name: str) -> str:
    root = server.settings.allowed_roots[0] / name
    root.mkdir()
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return str(root)


def user(server: Server, name: str, *, admin: bool = False) -> Principal:
    server.create_user(name, PASSWORD, is_admin=admin)
    principal = server.login(name, PASSWORD)
    assert principal is not None
    return principal


@pytest.fixture
def world(server: Server) -> dict[str, object]:
    """Two projects, an admin and users with different roles (AC-08 shape)."""
    admin = user(server, "admin", admin=True)
    alpha = server.create_project(admin, "alpha", path=make_repo(server, "alpha"))
    beta = server.create_project(admin, "beta", path=make_repo(server, "beta"))
    people = {n: user(server, n) for n in ("dev", "auditor", "maint", "outsider")}
    server.set_member(admin, alpha, "dev", Role.DEVELOPER)
    server.set_member(admin, alpha, "auditor", Role.AUDITOR)
    server.set_member(admin, alpha, "maint", Role.MAINTAINER)
    server.set_member(admin, beta, "outsider", Role.DEVELOPER)
    return {"admin": admin, "alpha": alpha, "beta": beta, **people}


# --------------------------------------------------------------------- rbac --
def test_permission_matrix() -> None:
    assert MATRIX[Role.OWNER] == frozenset(Permission)
    assert allowed(Role.MAINTAINER, Permission.RUN_APPROVE)
    assert not allowed(Role.DEVELOPER, Permission.RUN_APPROVE)
    assert not allowed(Role.AUDITOR, Permission.RUN_CREATE)
    assert allowed(Role.AUDITOR, Permission.AUDIT_VIEW)
    assert not allowed(Role.AGENT, Permission.RUN_CREATE)
    assert allowed(Role.AGENT, Permission.RUN_VIEW)
    assert not allowed(None, Permission.PROJECT_VIEW)
    assert capped(Role.OWNER, Role.DEVELOPER) is Role.DEVELOPER
    assert capped(Role.DEVELOPER, Role.OWNER) is Role.DEVELOPER
    assert capped(Role.DEVELOPER, Role.AUDITOR) is Role.AUDITOR
    assert capped(None, Role.OWNER) is None and capped(Role.AUDITOR, None) is Role.AUDITOR


# ---------------------------------------------------------------- auth --
def test_users_and_login(server: Server) -> None:
    server.create_user("alice", PASSWORD)
    with pytest.raises(ConflictError):
        server.create_user("alice", PASSWORD)
    with pytest.raises(WeakPasswordError):
        server.create_user("bob", "short")
    with pytest.raises(InvalidError):
        server.create_user("bad name", PASSWORD)
    assert server.login("alice", PASSWORD) is not None
    assert server.login("alice", "wrong password!!") is None
    assert server.login("nobody", PASSWORD) is None
    server.set_password("alice", "another long password")
    assert server.login("alice", PASSWORD) is None
    with server.session() as s:
        outcomes = [e.outcome for e in s.scalars(select(AuditEvent).where(
            AuditEvent.action == "auth.login"))]  # fmt: skip
    assert outcomes.count("denied") == 3 and outcomes.count("success") == 1


def test_tokens_issue_authenticate_revoke_rotate(server: Server) -> None:
    alice = user(server, "alice")
    token, token_id = server.issue_token(alice, "laptop")
    principal = server.authenticate(token)
    assert principal is not None and principal.username == "alice" and principal.token_id
    assert server.authenticate(token + "x") is None and server.authenticate("nope") is None
    with server.session() as s:
        row = s.get(ApiToken, token_id)
        assert row is not None and row.token_hash == token_hash(token)  # only the hash
        assert token not in str(row.__dict__)
    new, new_id = server.rotate_token(alice, token_id)
    assert server.authenticate(token) is None and server.authenticate(new) is not None
    assert {t["id"]: t["revoked"] for t in server.list_tokens(alice)} == {
        str(token_id): True, str(new_id): False}  # fmt: skip
    server.revoke_token(alice, new_id)
    assert server.authenticate(new) is None
    bob = user(server, "bob")
    with pytest.raises(NotFoundError):
        server.revoke_token(bob, token_id)  # not bob's token


def test_expired_token_and_inactive_user(server: Server) -> None:
    alice = user(server, "alice")
    token, token_id = server.issue_token(alice, "t", ttl_hours=1)
    with server.session() as s:
        row = s.get(ApiToken, token_id)
        assert row is not None
        row.expires_at = utcnow() - timedelta(seconds=1)
    assert server.authenticate(token) is None
    with pytest.raises(InvalidError):
        server.issue_token(alice, "t", ttl_hours=24 * 366)


def test_agent_tokens_are_project_scoped_and_short_lived(
    server: Server, world: dict[str, object]
) -> None:
    dev, alpha = world["dev"], world["alpha"]
    assert isinstance(dev, Principal) and isinstance(alpha, uuid.UUID)
    with pytest.raises(InvalidError, match="one project"):
        server.issue_token(dev, "bot", max_role=Role.AGENT)
    token, token_id = server.issue_token(dev, "bot", project_id=alpha, max_role=Role.AGENT,
                                         ttl_hours=24 * 30)  # fmt: skip
    with server.session() as s:
        row = s.get(ApiToken, token_id)
        assert row is not None and row.expires_at is not None
        assert row.expires_at - row.created_at <= timedelta(hours=24, seconds=5)
    agent = server.authenticate(token)
    assert agent is not None and agent.token_max_role is Role.AGENT
    with pytest.raises(ForbiddenError):  # capped to agent: read-only
        server.create_run(agent, alpha, "discover", {})
    with pytest.raises(ForbiddenError, match="scoped token"):
        server.issue_token(agent, "more")
    beta = world["beta"]
    assert isinstance(beta, uuid.UUID)
    with pytest.raises(NotFoundError):
        server.issue_token(dev, "x", project_id=beta)  # not a member of beta


# ------------------------------------------------------------ projects --
def test_project_creation_rules(server: Server, tmp_path: Path) -> None:
    admin = user(server, "admin", admin=True)
    alice = user(server, "alice")
    with pytest.raises(ForbiddenError):
        server.create_project(alice, "p1", path=make_repo(server, "p1"))
    with pytest.raises(ForbiddenError, match="ALLOWED_ROOTS"):
        server.create_project(admin, "p2", path=str(tmp_path))
    with pytest.raises(InvalidError):
        server.create_project(admin, "bad name!", path=make_repo(server, "p3"))
    with pytest.raises(InvalidError, match="exactly one"):
        server.create_project(admin, "p4")
    pid = server.create_project(admin, "p5", path=make_repo(server, "p5"))
    with pytest.raises(ConflictError):
        server.create_project(admin, "p5", path=make_repo(server, "p6"))
    root = server.settings.allowed_roots[0] / "p5"
    assert (root / ".ai-coding" / "config.yml").is_file()  # server-side config
    assert not (root / ".ai-coding.yml").exists()  # the repository is not edited
    info = server.get_project(admin, pid)
    assert info["role"] == "owner" and info["members"] == [{"username": "admin", "role": "owner"}]


def test_isolation_between_projects(server: Server, world: dict[str, object]) -> None:
    admin, dev, outsider = world["admin"], world["dev"], world["outsider"]
    alpha, beta = world["alpha"], world["beta"]
    assert isinstance(dev, Principal) and isinstance(outsider, Principal)
    assert isinstance(admin, Principal)
    assert isinstance(alpha, uuid.UUID) and isinstance(beta, uuid.UUID)
    assert [p["name"] for p in server.list_projects(dev)] == ["alpha"]
    assert [p["name"] for p in server.list_projects(outsider)] == ["beta"]
    assert {p["name"]: p["role"] for p in server.list_projects(admin)} == {
        "alpha": "owner", "beta": "owner"}  # fmt: skip
    for call in (lambda: server.get_project(outsider, alpha),
                 lambda: server.list_runs(outsider, alpha),
                 lambda: server.findings(outsider, alpha),
                 lambda: server.create_run(outsider, alpha, "discover", {})):  # fmt: skip
        with pytest.raises(NotFoundError):
            call()
    run = server.create_run(dev, alpha, "discover", {})
    with pytest.raises(NotFoundError, match="run not found"):
        server.run_status(outsider, run)  # same answer as for a missing run
    with pytest.raises(NotFoundError, match="run not found"):
        server.run_status(outsider, uuid.uuid4())
    token, _ = server.issue_token(admin, "alpha-only", project_id=alpha)
    scoped = server.authenticate(token)
    assert scoped is not None
    assert [p["name"] for p in server.list_projects(scoped)] == ["alpha"]
    with pytest.raises(NotFoundError):
        server.get_project(scoped, beta)


def test_members_management(server: Server, world: dict[str, object]) -> None:
    admin, dev, alpha = world["admin"], world["dev"], world["alpha"]
    assert isinstance(admin, Principal) and isinstance(dev, Principal)
    assert isinstance(alpha, uuid.UUID)
    with pytest.raises(ForbiddenError):
        server.set_member(dev, alpha, "outsider", Role.DEVELOPER)
    with pytest.raises(ConflictError, match="at least one owner"):
        server.set_member(admin, alpha, "admin", Role.MAINTAINER)
    server.set_member(admin, alpha, "dev", None)
    with pytest.raises(NotFoundError):
        server.get_project(dev, alpha)
    with pytest.raises(NotFoundError):
        server.set_member(admin, alpha, "ghost", Role.DEVELOPER)


# --------------------------------------------------------------------- runs --
def test_runs_rbac_queue_and_audit(server: Server, world: dict[str, object]) -> None:
    dev, auditor, maint = world["dev"], world["auditor"], world["maint"]
    alpha = world["alpha"]
    assert isinstance(dev, Principal) and isinstance(auditor, Principal)
    assert isinstance(maint, Principal) and isinstance(alpha, uuid.UUID)
    with pytest.raises(ForbiddenError):
        server.create_run(auditor, alpha, "discover", {})  # read-only role
    with pytest.raises(InvalidError, match="unknown run kind"):
        server.create_run(dev, alpha, "rm-rf", {})
    with pytest.raises(InvalidError, match="invalid parameters"):
        server.create_run(dev, alpha, "develop", {"title": ""})
    run = server.create_run(dev, alpha, "discover", {}, idempotency_key="k1")
    assert server.create_run(dev, alpha, "discover", {}, idempotency_key="k1") == run
    with server.session() as s:
        jobs = s.scalars(select(RunJob).where(RunJob.run_id == run)).all()
    assert [(j.kind, j.state) for j in jobs] == [("execute", "queued")]
    status = server.run_status(auditor, run)
    assert status["state"] == "CREATED" and status["kind"] == "discover"
    assert [r["id"] for r in server.list_runs(auditor, alpha)] == [str(run)]
    other = server.create_run(maint, alpha, "discover", {})
    with pytest.raises(ForbiddenError, match="own runs"):
        server.cancel(dev, other, "nope")
    assert server.cancel(dev, run, "changed my mind")["state"] == "CANCELLED"
    assert server.cancel(maint, other, "cleanup")["state"] == "CANCELLED"
    with pytest.raises(ForbiddenError):
        server.audit_events(dev, alpha)
    events = server.audit_events(auditor, alpha)
    actions = {(e["action"], e["outcome"]) for e in events}
    assert ("run.create", "success") in actions and ("run.cancel", "denied") in actions
    assert ("access.run.create", "denied") in actions
    assert all(e["project_id"] == str(alpha) for e in events)
    assert server.audit_events(dev) == []  # developers do not read the audit trail
