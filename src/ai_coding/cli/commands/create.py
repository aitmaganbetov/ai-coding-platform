"""``ai-coding create`` — CREATE mode: idea → approved requirements → ADR → backlog →
bootstrap → PR (FR-03, AC-01).

The requirements dialogue: each round produces a new version of the specification
with open questions. The user approves the version (design work starts), or answers
the questions (a new round with the answers), or stops. Interactively this happens in
the terminal; otherwise the run waits and the same choices are made with
``ai-coding run approve`` / ``ai-coding create --resume`` / ``ai-coding create --revise``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.markup import escape

from ai_coding.cli.commands.github import make_host, resolve_repo
from ai_coding.cli.commands.run import _finish, _resolve
from ai_coding.cli.common import close_all, guard, open_orchestrator
from ai_coding.cli.console import error, info
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.config.loader import STATE_CONFIG, find_config
from ai_coding.config.models import AICodingConfig
from ai_coding.config.validator import validate_file
from ai_coding.domain.enums import ApprovalOperation, ApprovalStatus, Mode, RunState
from ai_coding.git import LocalGitRepository
from ai_coding.integrations.github.auth import TokenAuth, auth_from_env
from ai_coding.integrations.github.client import DEFAULT_API_URL
from ai_coding.orchestrator import Orchestrator, RunOptions, RunStatus, TaskPlanner
from ai_coding.workflows.create import DEFAULT_BRANCH, CreateRequest, build_create

MAX_ROUNDS = 5


def _load_config(path: Path | None, dest: Path) -> AICodingConfig:
    """Platform configuration for the CREATE run (AI provider, policy, budgets).

    Order: ``--config``, the configuration stored by an earlier ``create`` in the
    project's state directory, ``.ai-coding.yml`` of the current directory.
    """
    stored = dest / STATE_CONFIG
    config_path = path or (stored if stored.is_file() else None) or find_config(Path.cwd())
    if config_path is None:
        error(
            "no configuration: pass --config with an `ai` section "
            "(see docs/create.md), or run from a directory with .ai-coding.yml"
        )
        raise typer.Exit(ExitCode.NOT_FOUND)
    report = validate_file(config_path)
    if not report.ok or report.config is None:
        error(f"{config_path} is invalid; run `ai-coding config validate {config_path}`")
        raise typer.Exit(ExitCode.INVALID)
    return report.config.model_copy(update={"mode": Mode.CREATE})


def _store_config(dest: Path, config: AICodingConfig) -> None:
    """Keep the run's configuration with its state (no secrets: keys are env var names)."""
    path = dest / STATE_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json", exclude_none=True)
    path.write_text(
        "# Configuration of `ai-coding create` runs in this project (generated).\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _prepare_repository(dest: Path, base: str) -> LocalGitRepository:
    """A git repository at ``dest`` whose ``base`` branch has at least one commit."""
    dest.mkdir(parents=True, exist_ok=True)
    if LocalGitRepository.is_repository(dest):
        repo = LocalGitRepository.open(dest)
        if repo.root != dest.resolve():
            error(f"{dest} is inside another repository ({repo.root}); use its root or a new dir")
            raise typer.Exit(ExitCode.USAGE)
    else:
        if any(p.name != ".ai-coding" for p in dest.iterdir()):
            error(f"{dest} is not empty and not a git repository")
            raise typer.Exit(ExitCode.USAGE)
        repo = LocalGitRepository.init(dest, initial_branch=base)
    if repo.head_commit() is None:
        repo.create_root_commit("chore: initialize repository")
    if not repo.branch_exists(base):
        error(f"base branch '{base}' does not exist in {dest}")
        raise typer.Exit(ExitCode.USAGE)
    return repo


def _pull_request(repo: str | None, root: Path, title: str) -> Any:
    host = make_host(resolve_repo(repo, root))
    auth = auth_from_env()

    def token() -> str | None:
        if isinstance(auth, TokenAuth):
            return auth.token
        return auth.authorization(host.client._exchange).removeprefix("Bearer ")

    api_host = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL).split("://", 1)[-1].split("/")[0]
    return (host, token, api_host, title)


def _read_answers(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        error(f"{path}: expected a mapping of question id → answer")
        raise typer.Exit(ExitCode.INVALID)
    return {str(k): str(v) for k, v in data.items()}


def _show_spec(output: dict[str, Any]) -> None:
    spec = output["spec"]
    info(f"\n[bold]Требования {escape(output['version'])}[/]: {escape(spec['title'])}")
    info(escape(spec["summary"]))
    info(
        f"требований: {len(spec['functional_requirements'])}, "
        f"историй: {len(spec['user_stories'])}, "
        f"открытых вопросов: {len(spec.get('open_questions', []))}"
    )
    for r in spec["functional_requirements"]:
        info(escape(f"  {r['id']} [{r['priority']}] {r['text']}"))
    for q in spec.get("open_questions", []):
        info(f"  ? {escape(q['id'])} {escape(q['question'])} "
             f"(по умолчанию: {escape(q['default_assumption'])})")  # fmt: skip


def _pending(status: RunStatus) -> list[dict[str, Any]]:
    return [a for a in status.approvals if a["status"] == ApprovalStatus.PENDING.value]


class _Session:
    """One CLI session that may span several runs (dialogue rounds)."""

    def __init__(self, orch: Orchestrator, dest: Path, interactive: bool) -> None:
        self.orch = orch
        self.dest = dest
        self.interactive = interactive

    def hint(self, run_id: uuid.UUID, version: str | None) -> None:
        rid = str(run_id)
        info("\nRun is waiting for a decision:")
        info(f"  approve:  ai-coding run approve {rid} -C {self.dest}")
        info(f"  continue: ai-coding create --resume {rid} --dest {self.dest}")
        if version:
            info(f"  revise:   ai-coding create --revise {rid} --answers answers.yml "
                 f"--dest {self.dest}")  # fmt: skip

    def loop(self, run_id: uuid.UUID, status: RunStatus) -> tuple[uuid.UUID, RunStatus, Any]:
        """Handle approvals; returns ``(run, status, revision)``; revision = (spec, answers)."""
        while status.state is RunState.AWAITING_APPROVAL:
            pending = _pending(status)
            spec_out = self.orch.task_output(run_id, "create.requirements")
            is_spec = spec_out is not None and any(
                a["operation"] == ApprovalOperation.SPEC_APPROVAL.value for a in pending
            )
            if is_spec and spec_out is not None:
                _show_spec(spec_out)
            else:
                for a in pending:
                    info(f"\napproval needed: [bold]{escape(a['operation'])}[/] — "
                         f"{escape(a['reason'] or '')}")  # fmt: skip
            if not self.interactive:
                self.hint(run_id, spec_out["version"] if spec_out else None)
                return run_id, status, None
            choices = "approve/revise/stop" if is_spec else "approve/stop"
            choice = typer.prompt(f"Decision ({choices})", default="approve").strip().lower()
            if choice in {"approve", "a", "y", "yes"}:
                guard(lambda: self.orch.decide(run_id, approve=True, note="approved in CLI"))
                status = guard(lambda: asyncio.run(self.orch.resume(run_id)))
            elif is_spec and spec_out is not None and choice in {"revise", "r"}:
                answers: dict[str, str] = {}
                for q in spec_out["spec"].get("open_questions", []):
                    answers[q["id"]] = typer.prompt(q["question"], default=q["default_assumption"])
                extra = typer.prompt("Additional changes (empty = none)", default="")
                if extra.strip():
                    answers["changes"] = extra.strip()
                guard(lambda: self.orch.decide(run_id, approve=False, note="revision requested"))
                status = guard(lambda: asyncio.run(self.orch.resume(run_id)))
                return run_id, status, (spec_out, answers)
            else:
                self.hint(run_id, spec_out["version"] if spec_out else None)
                return run_id, status, None
        return run_id, status, None


def create(
    ctx: typer.Context,
    idea: Annotated[str | None, typer.Argument(help="Project description.")] = None,
    idea_file: Annotated[
        Path | None, typer.Option("--idea-file", help="Read the idea from a file.")
    ] = None,
    dest: Annotated[
        Path, typer.Option("--dest", "-d", help="Project directory (new or empty repo).")
    ] = Path(),
    config_file: Annotated[
        Path | None, typer.Option("--config", "-c", help="Platform config with the `ai` section.")
    ] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Project name.")] = None,
    constraint: Annotated[
        list[str] | None, typer.Option("--constraint", help="Hard constraint (repeatable).")
    ] = None,
    branch: Annotated[
        str, typer.Option("--branch", help="Work branch for the skeleton.")
    ] = DEFAULT_BRANCH,
    base: Annotated[str, typer.Option("--base")] = "main",
    resume: Annotated[str | None, typer.Option("--resume", help="Continue a waiting run.")] = None,
    revise: Annotated[
        str | None, typer.Option("--revise", help="New requirements round from a run.")
    ] = None,
    answers_file: Annotated[
        Path | None, typer.Option("--answers", help="YAML: question id → answer.")
    ] = None,
    pr: Annotated[bool, typer.Option("--pr", help="Push the work branch and open a PR.")] = False,
    repo: Annotated[str | None, typer.Option("--repo", "-R", help="owner/repo for --pr.")] = None,
    pr_title: Annotated[
        str, typer.Option("--pr-title")
    ] = "Bootstrap project skeleton (ai-coding create)",
    interactive: Annotated[
        bool | None,
        typer.Option("--interactive/--no-interactive", help="Default: when stdin is a terminal."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the plan; change nothing.")
    ] = False,
    require_approval: Annotated[
        bool, typer.Option("--require-approval", help="Every guarded operation needs approval.")
    ] = False,
) -> None:
    """Create a new project from a description (CREATE mode, FR-03)."""
    ctx.call_on_close(close_all)
    dest = dest.resolve()
    config = _load_config(config_file, dest)
    if resume and revise:
        error("--resume and --revise are mutually exclusive")
        raise typer.Exit(ExitCode.USAGE)
    text = idea_file.read_text(encoding="utf-8") if idea_file else idea
    if not (resume or revise) and not (text and text.strip()):
        error("describe the project: IDEA argument or --idea-file")
        raise typer.Exit(ExitCode.USAGE)
    interactive = sys.stdin.isatty() if interactive is None else interactive
    pull = _pull_request(repo, dest, pr_title) if pr and not dry_run else None
    request = CreateRequest(
        idea=(text or "").strip(),
        constraints=tuple(constraint or ()),
        project_name=name,
        branch=branch,
        base=base,
        answers=_read_answers(answers_file),
    )
    if dry_run:
        registry, steps = build_create(request)
        for s in steps:
            info(escape(f"{s.key}: {s.capability} (depends on: {', '.join(s.depends_on) or '—'})"))
        info("dry run: nothing was created; approvals: spec_approval (always), "
             "critical_file_change (CI/config files)" + (", pr_create" if pr else ""))  # fmt: skip
        return
    _prepare_repository(dest, base)
    _store_config(dest, config)
    registry, _ = build_create(request, pull_request=pull)
    orch = open_orchestrator(dest, config, registry)
    session = _Session(orch, dest, interactive)
    previous: dict[str, Any] | None = None
    if resume:
        run_id = _resolve(orch, resume)
        status = guard(lambda: asyncio.run(orch.resume(run_id)))
    else:
        if revise:
            prev_id = _resolve(orch, revise)
            previous = orch.task_output(prev_id, "create.requirements")
            if previous is None:
                error(f"run {revise} has no requirements version to revise")
                raise typer.Exit(ExitCode.NOT_FOUND)
            prev_goal = guard(lambda: orch.status(prev_id)).goal
            request = CreateRequest(
                idea=text.strip() if text else prev_goal.removeprefix("create: "),
                constraints=request.constraints, previous=previous["spec"],
                previous_version=previous["version"], answers=request.answers,
                project_name=name, branch=branch, base=base,
            )  # fmt: skip
        run_id, status = _start(orch, request, pull, require_approval)
    for _ in range(MAX_ROUNDS):
        run_id, status, revision = session.loop(run_id, status)
        if revision is None:
            break
        spec_out, answers = revision
        request = CreateRequest(
            idea=request.idea, constraints=request.constraints, previous=spec_out["spec"],
            previous_version=spec_out["version"], answers=answers, project_name=name,
            branch=branch, base=base,
        )  # fmt: skip
        info(f"\nrevising requirements {escape(spec_out['version'])} …")
        run_id, status = _start(orch, request, pull, require_approval)
    if status.state is RunState.COMPLETED:
        _report_completion(orch, run_id)
    _finish(status, as_json=False)


def _report_completion(orch: Orchestrator, run_id: uuid.UUID) -> None:
    boot = orch.task_output(run_id, "create.bootstrap") or {}
    info(f"\n[green]project skeleton committed[/] on branch "
         f"[bold]{escape(str(boot.get('branch')))}[/] ({str(boot.get('commit'))[:12]}), "
         f"{len(boot.get('files', []))} files")  # fmt: skip
    pr_out = orch.task_output(run_id, "github.pr")
    if pr_out:
        info(f"pull request: {pr_out.get('url')}")


def _start(
    orch: Orchestrator, request: CreateRequest, pull: Any, require_approval: bool = False
) -> tuple[uuid.UUID, RunStatus]:
    _, steps = build_create(request, pull_request=pull)
    goal = "create: " + request.idea[:200]
    options = RunOptions(require_approval=require_approval)
    plan = TaskPlanner().build(steps)
    run_id = guard(lambda: orch.create_run(plan, goal=goal, options=options))
    status = guard(lambda: asyncio.run(orch.execute(run_id)))
    return run_id, status
