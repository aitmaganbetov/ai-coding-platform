"""``ai-coding server`` — the Self-Hosted profile (ТЗ §15): database, users, tokens,
API server and workers. Needs the ``server`` extra; settings come from
``AI_CODING_SERVER_*`` environment variables (``docs/server.md``).

Passwords are read from a hidden prompt or stdin, never from command-line arguments
(they would end up in shell history and process listings).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from ai_coding.cli.console import error, info, stderr
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.observability import configure_logging

if TYPE_CHECKING:
    from ai_coding.api.services import Server

app = typer.Typer(
    help="Self-Hosted server: database, users, tokens, API, workers.", no_args_is_help=True
)
users = typer.Typer(help="Users of the server.", no_args_is_help=True)
tokens = typer.Typer(help="API tokens.", no_args_is_help=True)
app.add_typer(users, name="user")
app.add_typer(tokens, name="token")


def _server(*, level: str = "WARNING") -> Server:
    """Server from the environment; logs go to stderr (JSON), never into command output."""
    configure_logging(json=True, level=level)  # stderr, resolved at write time
    try:
        from ai_coding.api.services import Server  # noqa: PLC0415 - optional extra
        from ai_coding.api.settings import ServerSettings  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the installation
        error(
            f"the server needs the 'server' extra: pip install 'ai-coding-platform[server]' "
            f"({exc.name} missing)"
        )
        raise typer.Exit(ExitCode.NOT_FOUND) from exc
    try:
        settings = ServerSettings.from_env()
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.INVALID) from exc
    return Server(settings)


def _password(stdin: bool) -> str:
    if stdin:
        return sys.stdin.readline().rstrip("\n")
    return str(typer.prompt("Password", hide_input=True, confirmation_prompt=True))


def _fail(exc: Exception) -> typer.Exit:
    error(str(getattr(exc, "message", exc)))
    status = getattr(exc, "status", 400)
    return typer.Exit(ExitCode.NOT_FOUND if status == 404 else ExitCode.INVALID)


@app.command("migrate")
def migrate() -> None:
    """Create or upgrade the server database schema."""
    server = _server()
    server.migrate()
    info(f"database ready: {server.db.engine.url.render_as_string(hide_password=True)}")
    server.close()


@users.command("add")
def add_user(
    username: str,
    admin: Annotated[bool, typer.Option("--admin", help="Instance administrator.")] = False,
    password_stdin: Annotated[bool, typer.Option("--password-stdin")] = False,
) -> None:
    """Create a user (the password is prompted for or read from stdin)."""
    from ai_coding.api.security import WeakPasswordError  # noqa: PLC0415
    from ai_coding.api.services import ApiError  # noqa: PLC0415

    server = _server()
    try:
        server.create_user(username, _password(password_stdin), is_admin=admin, actor="cli")
    except (ApiError, WeakPasswordError) as exc:
        raise _fail(exc) from exc
    finally:
        server.close()
    info(f"user {username} created" + (" (administrator)" if admin else ""))


@users.command("password")
def set_password(
    username: str, password_stdin: Annotated[bool, typer.Option("--password-stdin")] = False
) -> None:
    """Replace a user's password."""
    from ai_coding.api.security import WeakPasswordError  # noqa: PLC0415
    from ai_coding.api.services import ApiError  # noqa: PLC0415

    server = _server()
    try:
        server.set_password(username, _password(password_stdin), actor="cli")
    except (ApiError, WeakPasswordError) as exc:
        raise _fail(exc) from exc
    finally:
        server.close()
    info(f"password of {username} updated")


@tokens.command("create")
def create_token(
    username: str,
    name: Annotated[str, typer.Option("--name", help="Label, e.g. ci or laptop.")] = "cli",
    project: Annotated[str | None, typer.Option("--project", help="Limit to a project.")] = None,
    role: Annotated[str | None, typer.Option("--role", help="Maximum role.")] = None,
    ttl_hours: Annotated[int | None, typer.Option("--ttl-hours", min=1)] = None,
) -> None:
    """Issue a token for USERNAME (bootstrap, automation); printed once."""
    from sqlalchemy import select  # noqa: PLC0415

    from ai_coding.api.services import ApiError, Principal  # noqa: PLC0415
    from ai_coding.domain.enums import Role  # noqa: PLC0415
    from ai_coding.persistence.models import Project, User  # noqa: PLC0415

    server = _server()
    try:
        with server.session() as s:
            user = s.scalar(select(User).where(User.username == username))
            if user is None:
                error(f"user '{username}' not found")
                raise typer.Exit(ExitCode.NOT_FOUND)
            principal = Principal(user.id, user.username, user.is_admin)
            project_id = None
            if project is not None:
                row = s.scalar(select(Project).where(Project.name == project))
                if row is None:
                    error(f"project '{project}' not found")
                    raise typer.Exit(ExitCode.NOT_FOUND)
                project_id = row.id
        token, token_id = server.issue_token(
            principal,
            name,
            project_id=project_id,
            max_role=Role(role) if role else None,
            ttl_hours=ttl_hours,
        )
    except (ApiError, ValueError) as exc:
        raise _fail(exc) from exc
    finally:
        server.close()
    typer.echo(token)
    info(f"token {token_id} — shown once; store it in a secret manager")


@tokens.command("revoke")
def revoke(token_id: str) -> None:
    """Revoke a token by id (administrative)."""
    import uuid  # noqa: PLC0415

    from ai_coding.api.services import ApiError, Principal  # noqa: PLC0415

    server = _server()
    try:
        admin = Principal(uuid.UUID(int=0), "cli", is_admin=True)
        server.revoke_token(admin, uuid.UUID(token_id))
    except (ApiError, ValueError) as exc:
        raise _fail(exc) from exc
    finally:
        server.close()
    info(f"token {token_id} revoked")


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
    with_worker: Annotated[
        bool, typer.Option("--with-worker", help="Also run a worker in this process.")
    ] = False,
    ui: Annotated[Path | None, typer.Option("--ui", help="Built Web UI directory.")] = None,
    workers: Annotated[
        int,
        typer.Option(
            "--workers", min=1, max=64, help="API processes (one per CPU core is a good start)."
        ),
    ] = 1,
) -> None:
    """Run the API (behind a TLS reverse proxy in production)."""
    import uvicorn  # noqa: PLC0415

    from ai_coding.api.app import UI_DIR_ENV, create_app  # noqa: PLC0415

    if workers > 1 and with_worker:
        error("--with-worker runs jobs inside the API process; use it with --workers 1 "
              "and start `ai-coding server worker` separately for more API processes")  # fmt: skip
        raise typer.Exit(ExitCode.USAGE)
    server = _server(level="INFO")
    server.migrate()
    proxies = ",".join(server.settings.trusted_proxies) or None
    common: dict[str, Any] = {"host": host, "port": port, "proxy_headers": bool(proxies),
                              "forwarded_allow_ips": proxies, "server_header": False,
                              "log_level": "info"}  # fmt: skip
    if workers > 1:
        server.close()
        if ui is not None:
            os.environ[UI_DIR_ENV] = str(ui.resolve())
        uvicorn.run("ai_coding.api.app:app_from_env", factory=True, workers=workers, **common)
        return
    uvicorn.run(create_app(server, ui_dir=ui, run_worker=with_worker), **common)
    server.close()


@app.command("workspaces")
def workspaces(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8001,
) -> None:
    """Run the workspace service: browser terminals and git of the users' workspaces.

    For deployments whose API has no Docker access (docker-compose): the API reaches this
    service with ``AI_CODING_SERVER_WORKSPACES=http://<host>:<port>`` and tickets signed
    by ``AI_CODING_SERVER_WORKSPACE_SECRET``. Never expose it outside the backend network.
    """
    import uvicorn  # noqa: PLC0415

    from ai_coding.api.terminal import create_workspace_app  # noqa: PLC0415
    from ai_coding.api.workspace_broker import LocalBroker  # noqa: PLC0415

    server = _server(level="INFO")
    if not server.settings.workspace_secret:
        error("set AI_CODING_SERVER_WORKSPACE_SECRET (the same value as for the API)")
        raise typer.Exit(ExitCode.USAGE)
    app_ = create_workspace_app(server, LocalBroker(server.settings))
    uvicorn.run(app_, host=host, port=port, server_header=False, log_level="info")
    server.close()


@app.command("worker")
def worker(
    concurrency: Annotated[int | None, typer.Option("--concurrency", min=1, max=256)] = None,
) -> None:
    """Execute queued runs until interrupted (SIGTERM/SIGINT finish running jobs first)."""
    from ai_coding.api.worker import Worker  # noqa: PLC0415

    server = _server(level="INFO")
    server.migrate()
    pool = Worker(server, concurrency=concurrency)
    info(f"worker {pool.owner}: up to {pool.concurrency} concurrent runs")

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):  # Windows: Ctrl+C only
                loop.add_signal_handler(sig, stop.set)
        await pool.run(stop)

    asyncio.run(main())
    server.close()


@app.command("export")
def export(
    project: str,
    user: Annotated[str, typer.Option("--user", help="Act as this project owner.")],
    output: Annotated[Path, typer.Option("--output", "-o")],
) -> None:
    """Export a project's data as a ZIP (NFR-08; RBAC of --user applies)."""
    from sqlalchemy import select  # noqa: PLC0415

    from ai_coding.api.lifecycle import export_project  # noqa: PLC0415
    from ai_coding.api.services import ApiError, Principal  # noqa: PLC0415
    from ai_coding.persistence.models import Project, User  # noqa: PLC0415

    server = _server()
    try:
        with server.session() as s:
            row = s.scalar(select(User).where(User.username == user))
            pid = s.scalar(select(Project.id).where(Project.name == project))
            if row is None or pid is None:
                error("user or project not found")
                raise typer.Exit(ExitCode.NOT_FOUND)
            principal = Principal(row.id, row.username, row.is_admin)
        output.write_bytes(export_project(server, principal, pid))
    except ApiError as exc:
        raise _fail(exc) from exc
    finally:
        server.close()
    info(f"exported {project} to {output}")


@app.command("purge")
def purge() -> None:
    """Apply the retention policy now (cron-friendly)."""
    from ai_coding.api.lifecycle import purge_expired  # noqa: PLC0415

    server = _server()
    try:
        counts = purge_expired(server)
    finally:
        server.close()
    typer.echo(json.dumps(counts))


@app.command("settings")
def show_settings() -> None:
    """Effective settings (secrets hidden)."""
    server = _server()
    s = server.settings
    typer.echo(
        json.dumps(
            {
                "data_dir": str(s.data_dir),
                "database_url": server.db.engine.url.render_as_string(hide_password=True),
                "workers": s.workers,
                "allowed_roots": [str(r) for r in s.allowed_roots],
                "rate_limit_per_minute": s.rate_limit_per_minute,
                "login_rate_limit_per_minute": s.login_rate_limit_per_minute,
                "trusted_proxies": sorted(s.trusted_proxies),
                "metrics_token": "set" if s.metrics_token else None,
                "retention_days": s.retention_days,
                "project_creators": s.project_creators,
                "max_upload_mb": s.max_upload_bytes // (1024 * 1024),
                "workspaces": s.workspaces,
                "workspace_secret": "set" if s.workspace_secret else None,
                "workspace_image": s.workspace_image,
                "workspace_egress": list(s.workspace_egress),
                "workspace_runtime": s.workspace_runtime,
            },
            indent=2,
        )
    )
    server.close()


# --------------------------------------------- this machine → a server (ADR-0047) --
# The commands above run a server; these connect the CLI of a developer's machine to one,
# so that its local runs show on the server (Infrastructure → Local (CLI)).


def _remote_fail(exc: Exception) -> typer.Exit:
    error(str(exc))
    return typer.Exit(ExitCode.NOT_FOUND)


def _signed_in() -> Any:
    from ai_coding.cli import remote  # noqa: PLC0415

    creds = remote.load_credentials()
    if creds is None:
        error("not signed in to a server: ai-coding server login --url URL")
        raise typer.Exit(ExitCode.NOT_FOUND)
    return creds


@app.command("login")
def login(
    url: Annotated[str, typer.Option("--url", help="The server, e.g. http://10.0.1.115:8090.")],
    token_stdin: Annotated[
        bool, typer.Option("--token-stdin", help="Read the token from stdin (scripts, CI).")
    ] = False,
) -> None:
    """Sign this machine's CLI in to a server with your API token (Аккаунт → Токены)."""
    from ai_coding.cli import remote  # noqa: PLC0415

    try:
        base = remote.normalize_url(url)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc
    token = sys.stdin.readline() if token_stdin else typer.prompt("API token", hide_input=True)
    token = str(token).strip()
    if not token:
        error("an API token is required (Web UI → Аккаунт → Токены)")
        raise typer.Exit(ExitCode.USAGE)
    if remote.plain_http(base):
        stderr.print("[yellow]warning:[/] plain HTTP: the token travels unencrypted; "
                     "prefer the server's HTTPS address")  # fmt: skip
    try:
        with remote.Client(remote.Credentials(base, token)) as client:
            me = client.call("GET", "/me")
    except remote.RemoteError as exc:
        raise _remote_fail(exc) from exc
    user = str(me.get("username")) if isinstance(me, dict) else None
    path = remote.save_credentials(remote.Credentials(base, token, user))
    info(f"signed in to {base} as {user} (saved in {path}, readable by you only)")
    info("next, in a project directory: ai-coding server link")


@app.command("logout")
def logout() -> None:
    """Forget the server sign-in of this machine (the token itself stays valid until
    revoked in the Web UI)."""
    from ai_coding.cli import remote  # noqa: PLC0415

    info("signed out" if remote.forget_credentials() else "not signed in")


@app.command("link")
def link(
    project_ref: Annotated[
        str | None, typer.Argument(metavar="[PROJECT]", help="Server project: name or id.")
    ] = None,
    project: Annotated[
        Path, typer.Option("--project", "-C", help="Project directory.", show_default=False)
    ] = Path(),
) -> None:
    """Link a project directory to a server project: its runs are then reported there."""
    from ai_coding.cli import remote  # noqa: PLC0415

    creds = _signed_in()
    try:
        with remote.Client(creds) as client:
            projects = client.call("GET", "/projects")
    except remote.RemoteError as exc:
        raise _remote_fail(exc) from exc
    items = [p for p in projects if isinstance(p, dict)] if isinstance(projects, list) else []
    if project_ref is None:
        chosen = items if len(items) == 1 else []
    else:
        wanted = project_ref.strip().lower()
        chosen = [p for p in items
                  if wanted in (str(p.get("id")).lower(), str(p.get("name")).lower())]  # fmt: skip
    if len(chosen) != 1:
        names = ", ".join(sorted(str(p.get("name")) for p in items)) or "none"
        what = "name a project" if project_ref is None else f"no project '{project_ref}'"
        error(f"{what}: ai-coding server link PROJECT (your projects on {creds.url}: {names})")
        raise typer.Exit(ExitCode.NOT_FOUND if project_ref else ExitCode.USAGE)
    target = chosen[0]
    root = project.resolve()
    remote.save_link(root, remote.Link(str(target["id"]), str(target.get("name")), creds.url))
    info(f"{root} → project {target.get('name')} on {creds.url}")
    info("the runs of ai-coding commands here are now reported to it (summaries only)")


@app.command("unlink")
def unlink(
    project: Annotated[
        Path, typer.Option("--project", "-C", help="Project directory.", show_default=False)
    ] = Path(),
) -> None:
    """Stop reporting this directory's runs to the server."""
    from ai_coding.cli import remote  # noqa: PLC0415

    info("unlinked" if remote.forget_link(project.resolve()) else "this directory is not linked")


@app.command("whoami")
def whoami(
    project: Annotated[
        Path, typer.Option("--project", "-C", help="Project directory.", show_default=False)
    ] = Path(),
) -> None:
    """The server this machine is signed in to and the project this directory reports to."""
    from ai_coding.cli import remote  # noqa: PLC0415

    creds = remote.load_credentials()
    if creds is None:
        info("not signed in to a server (ai-coding server login --url URL)")
    else:
        info(f"signed in to {creds.url} as {creds.user or '(token from the environment)'}")
    found = remote.load_link(project.resolve())
    if found is None:
        info("this directory is not linked (ai-coding server link PROJECT)")
    else:
        info(f"reports to project {found.project or found.project_id}"
             + (f" on {found.url}" if found.url else ""))  # fmt: skip


@app.command("report")
def report_run(
    run: Annotated[
        str | None, typer.Argument(help="Run id or prefix (default: the latest).")
    ] = None,
    project: Annotated[
        Path, typer.Option("--project", "-C", help="Project directory.", show_default=False)
    ] = Path(),
) -> None:
    """Send a run's summary to the linked server project again (e.g. after being offline)."""
    import uuid  # noqa: PLC0415

    from ai_coding.cli import remote  # noqa: PLC0415
    from ai_coding.cli.common import close_all, load_project, open_orchestrator, resolve_run  # noqa: PLC0415
    from ai_coding.orchestrator import HandlerRegistry  # noqa: PLC0415

    creds = _signed_in()
    root, config = load_project(project)
    found = remote.load_link(root)
    if found is None:
        error("this directory is not linked: ai-coding server link PROJECT")
        raise typer.Exit(ExitCode.NOT_FOUND)
    orch = open_orchestrator(root, config, HandlerRegistry([]))
    try:
        if run is None:
            latest = orch.list_runs(limit=1)
            if not latest:
                error("no runs in this directory yet")
                raise typer.Exit(ExitCode.NOT_FOUND)
            run_id = uuid.UUID(str(latest[0]["id"]))
        else:
            run_id = resolve_run(orch, run)
        summary = remote.run_summary(orch, run_id)
        with remote.Client(creds) as client:
            result = remote.send(client, found, summary)
    except remote.RemoteError as exc:
        raise _remote_fail(exc) from exc
    finally:
        close_all()  # this command changes no run: nothing is reported twice
    verb = "reported" if result.get("created") else "updated"
    info(f"run {str(run_id)[:8]} {verb} on {creds.url} "
         f"(project {found.project or found.project_id}): {summary['state']}"
         + (f", {summary['verdict']}" if summary.get("verdict") else ""))  # fmt: skip
