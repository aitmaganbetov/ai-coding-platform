"""``ai-coding github`` — PR comments, check runs and branch-protection checks (S4)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from ai_coding.cli.console import error, info, stdout
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.git import GitError, LocalGitRepository
from ai_coding.integrations.github import GitHubClient, GitHubHost, auth_from_env, parse_repo_slug
from ai_coding.integrations.github.auth import GitHubAuthError
from ai_coding.integrations.github.client import DEFAULT_API_URL, GitHubAPIError
from ai_coding.integrations.github.host import Annotation
from ai_coding.reports import AuditReport, render_markdown

app = typer.Typer(
    help="GitHub integration (needs GITHUB_TOKEN or a GitHub App).", no_args_is_help=True
)
RepoOpt = Annotated[
    str | None,
    typer.Option("--repo", "-R", help="owner/repo (default: $GITHUB_REPOSITORY or git remote)."),
]
_LEVEL = {
    "critical": "failure",
    "high": "failure",
    "medium": "warning",
    "low": "notice",
    "info": "notice",
}


@app.callback()
def _group() -> None:
    """GitHub integration (needs GITHUB_TOKEN or a GitHub App)."""


def resolve_repo(repo: str | None, project: Path = Path()) -> str:
    """Repository slug from the option, the Actions environment or the git remote."""
    candidate = repo or os.environ.get("GITHUB_REPOSITORY")
    if not candidate:
        try:
            candidate = LocalGitRepository.open(project).remote_url() or ""
        except GitError:
            candidate = ""
    try:
        return parse_repo_slug(candidate)
    except ValueError as exc:
        error("cannot determine the GitHub repository; pass --repo owner/repo")
        raise typer.Exit(ExitCode.USAGE) from exc


def make_host(repo: str) -> GitHubHost:
    """Authenticated host adapter (exits with a clear message without credentials)."""
    try:
        auth = auth_from_env()
    except GitHubAuthError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc
    api_url = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL)
    return GitHubHost(GitHubClient(auth, api_url=api_url), repo)


def _load(report: Path) -> dict[str, Any]:
    if not report.is_file():
        error(f"report not found: {report}")
        raise typer.Exit(ExitCode.NOT_FOUND)
    data: dict[str, Any] = json.loads(report.read_text(encoding="utf-8"))
    return data


@app.command("comment")
def comment(
    pr: Annotated[int, typer.Option("--pr", help="Pull request number.", min=1)],
    report: Annotated[Path, typer.Option("--report", help="audit-report.json")],
    key: Annotated[
        str, typer.Option("--key", help="Comment identity (one comment per key).")
    ] = "audit",
    repo: RepoOpt = None,
) -> None:
    """Create or update the audit summary comment on a PR (no duplicates, FR-10)."""
    data = _load(report)
    fields = AuditReport.__dataclass_fields__
    body = render_markdown(AuditReport(**{k: v for k, v in data.items() if k in fields}))
    host = make_host(resolve_repo(repo))
    try:
        comment_id, created = host.upsert_comment(pr, key, body)
    except GitHubAPIError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.INVALID) from exc
    info(f"{'created' if created else 'updated'} comment {comment_id} on PR #{pr}")


@app.command("check")
def check(
    sha: Annotated[str, typer.Option("--sha", help="Head commit SHA (40 hex).")],
    report: Annotated[Path, typer.Option("--report", help="audit-report.json")],
    name: Annotated[str, typer.Option("--name")] = "ai-coding / audit",
    repo: RepoOpt = None,
) -> None:
    """Publish the audit verdict as a check run with file/line annotations (AC-06)."""
    data = _load(report)
    annotations = [
        Annotation(
            path=f["path"],
            start_line=int(f.get("line") or 1),
            level=_LEVEL[f["severity"]],
            message=f.get("message") or f["title"],
            title=f"{f['tool']}: {f['rule_id']}",
        )
        for f in data.get("findings", [])
        if f.get("status") == "new" and f.get("path")
    ]
    fields = AuditReport.__dataclass_fields__
    summary = render_markdown(AuditReport(**{k: v for k, v in data.items() if k in fields}))
    host = make_host(resolve_repo(repo))
    try:
        check_id = host.create_check_run(
            name=name,
            head_sha=sha,
            conclusion="success" if data["verdict"] == "PASS" else "failure",
            title=f"Audit {data['verdict']}: {sum(data['summary'].values())} new finding(s)",
            summary=summary,
            annotations=annotations,
        )
    except (GitHubAPIError, ValueError) as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.INVALID) from exc
    info(f"check run {check_id} published ({len(annotations)} annotation(s))")


@app.command("protection")
def protection(
    branch: Annotated[str, typer.Option("--branch", "-b")] = "main",
    repo: RepoOpt = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check that the branch is protected by rulesets (PRs required, checks, no force push)."""
    host = make_host(resolve_repo(repo))
    try:
        result = host.branch_protection(branch)
    except GitHubAPIError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.INVALID) from exc
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "branch": result.branch,
                    "rules": list(result.rules),
                    "ok": result.ok,
                    "gaps": list(result.gaps),
                },
                indent=2,
            )
        )
    else:
        table = Table("requirement", "status")
        table.add_row("pull request required", "yes" if result.requires_pull_request else "NO")
        table.add_row("required status checks", "yes" if result.requires_status_checks else "NO")
        table.add_row("force push blocked", "yes" if result.blocks_force_push else "NO")
        stdout.print(table)
        for gap in result.gaps:
            error(gap)
    raise typer.Exit(ExitCode.OK if result.ok else ExitCode.INVALID)
