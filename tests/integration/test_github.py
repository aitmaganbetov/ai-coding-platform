"""GitHub integration over real HTTP against an in-memory fake API (S4)."""

from __future__ import annotations

import io
import json
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_coding.cli.app import app
from ai_coding.cli.exit_codes import ExitCode
from ai_coding.domain.enums import RunState
from ai_coding.executors import CommandSpec, ExecutorError
from ai_coding.git import GitCommandError, LocalGitRepository, ProtectedBranchError
from ai_coding.integrations import RepositoryHost
from ai_coding.integrations.github import GitHubClient, GitHubHost, TokenAuth
from ai_coding.integrations.github.executor import GitHubActionsExecutor
from ai_coding.integrations.github.host import Annotation
from ai_coding.orchestrator import TaskPlanner
from ai_coding.workflows.pull_request import PullRequestRequest, build_pull_request
from tests.fake_github import FakeGitHub
from tests.helpers import make_config, make_orchestrator

pytestmark = pytest.mark.integration
runner = CliRunner()
SHA = "a" * 40


def host(fake: FakeGitHub) -> GitHubHost:
    return GitHubHost(GitHubClient(TokenAuth(fake.token), api_url=fake.url), fake.repo)


# ------------------------------------------------------------------- host --
def test_pull_requests_are_idempotent_and_never_auto_merge(fake_github: FakeGitHub) -> None:
    h = host(fake_github)
    first = h.create_pull_request(head="ai/feature", base="main", title="Add", body="body")
    second = h.create_pull_request(head="ai/feature", base="main", title="Add", body="body")
    assert first.created and not second.created and first.number == second.number
    assert len(fake_github.pulls) == 1
    assert fake_github.pulls[1]["maintainer_can_modify"] is False
    assert not any("merge" in path for _, path in fake_github.requests)
    with pytest.raises(ValueError, match="differ"):
        h.create_pull_request(head="main", base="main", title="x", body="")


def test_comment_upsert_has_no_duplicates(fake_github: FakeGitHub) -> None:
    h = host(fake_github)
    cid, created = h.upsert_comment(7, "audit", "first")
    cid2, created2 = h.upsert_comment(7, "audit", "second " + "x" * 70_000)
    other, _ = h.upsert_comment(7, "security", "other")
    assert created and not created2 and cid == cid2 and other != cid
    body = fake_github.comments[cid]["body"]
    assert body.startswith("<!-- ai-coding:audit -->") and body.endswith("(truncated)")
    assert len(body) <= 65_000
    with pytest.raises(ValueError, match="marker"):
        h.upsert_comment(7, "Bad Key", "x")


def test_check_run_batches_annotations(fake_github: FakeGitHub) -> None:
    annotations = [
        Annotation(path="a.py", start_line=i + 1, level="warning", message="m") for i in range(120)
    ]
    cid = host(fake_github).create_check_run(
        name="ai-coding / audit",
        head_sha=SHA,
        conclusion="failure",
        title="t",
        summary="s",
        annotations=annotations,
    )
    assert fake_github.check_runs[cid]["annotation_batches"] == [50, 50, 20]
    with pytest.raises(ValueError, match="40-character"):
        host(fake_github).create_check_run(
            name="n", head_sha="abc", conclusion="success", title="t", summary="s"
        )
    with pytest.raises(ValueError, match="conclusion"):
        host(fake_github).create_check_run(
            name="n", head_sha=SHA, conclusion="great", title="t", summary="s"
        )


def test_commit_status(fake_github: FakeGitHub) -> None:
    host(fake_github).create_commit_status(
        sha=SHA, state="success", context="ai-coding", description="ok", target_url="https://x"
    )
    assert fake_github.statuses[0]["state"] == "success"
    with pytest.raises(ValueError):
        host(fake_github).create_commit_status(sha=SHA, state="great", context="c", description="d")


def test_branch_protection_report(fake_github: FakeGitHub) -> None:
    fake_github.rules["main"] = [{"type": "pull_request"}, {"type": "non_fast_forward"}]
    report = host(fake_github).branch_protection("main")
    assert report.requires_pull_request and report.blocks_force_push
    assert not report.ok and report.gaps == (
        "no required status checks (Quality Gates can be bypassed)",
    )
    fake_github.rules["main"].append({"type": "required_status_checks"})
    assert host(fake_github).branch_protection("main").ok
    assert len(host(fake_github).branch_protection("dev").gaps) == 3


def test_bad_credentials(fake_github: FakeGitHub) -> None:
    bad = GitHubHost(
        GitHubClient(TokenAuth("wrong-token-000"), api_url=fake_github.url), fake_github.repo
    )
    with pytest.raises(Exception, match="401"):
        bad.find_pull_request("x")


# --------------------------------------------------------------- executor --
def executor(fake: FakeGitHub, **kw: object) -> GitHubActionsExecutor:
    async def no_sleep(_: float) -> None:
        return None

    return GitHubActionsExecutor(
        host(fake),
        ref="main",
        allowed_workflows={"ai-coding-exec.yml"},
        sleep=no_sleep,
        poll_interval=0,
        **kw,
    )  # type: ignore[arg-type]


async def test_actions_executor_success(fake_github: FakeGitHub, tmp_path: Path) -> None:
    ex = executor(fake_github)
    assert ex.available()
    result = await ex.run(CommandSpec(argv=("ai-coding-exec.yml", "suite=fast"), cwd=tmp_path))
    assert result.ok and result.exit_code == 0
    summary = json.loads(result.stdout)
    assert summary["conclusion"] == "success" and summary["jobs"] == [
        {"name": "test", "conclusion": "success"}
    ]
    dispatch = fake_github.dispatches[0]
    assert dispatch["ref"] == "main" and dispatch["inputs"]["suite"] == "fast"
    assert len(dispatch["inputs"]["correlation_id"]) == 32


async def test_actions_executor_failure_and_timeout(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    original = fake_github.handle

    def failing(method: str, path: str, q: dict, body: object):  # type: ignore[no-untyped-def]
        status, data = original(method, path, q, body)
        if method == "POST" and path.endswith("/dispatches"):
            run = max(fake_github.runs.values(), key=lambda r: r["id"])
            run["final"] = "failure"
        return status, data

    fake_github.handle = failing  # type: ignore[method-assign]
    result = await executor(fake_github).run(
        CommandSpec(argv=("ai-coding-exec.yml",), cwd=tmp_path)
    )
    assert result.exit_code == 1 and "failed jobs: test" in result.stderr

    def hanging(method: str, path: str, q: dict, body: object):  # type: ignore[no-untyped-def]
        status, data = original(method, path, q, body)
        if method == "POST" and path.endswith("/dispatches"):
            max(fake_github.runs.values(), key=lambda r: r["id"])["hang"] = True
        return status, data

    fake_github.handle = hanging  # type: ignore[method-assign]
    ticks = iter(range(0, 10_000, 50))
    timed = executor(fake_github, clock=lambda: float(next(ticks)))
    result = await timed.run(
        CommandSpec(argv=("ai-coding-exec.yml",), cwd=tmp_path, timeout_seconds=100)
    )
    assert result.timed_out and result.exit_code == 1
    assert any(path.endswith("/cancel") for _, path in fake_github.requests)


def _zip(entries: dict[str, bytes], symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


def test_github_host_implements_repository_host_port(fake_github: FakeGitHub) -> None:
    assert isinstance(host(fake_github), RepositoryHost)


async def test_actions_executor_downloads_artifacts(
    fake_github: FakeGitHub, tmp_path: Path
) -> None:
    fake_github.blobs[7] = _zip({"junit.xml": b"<testsuites/>", "sub/report.json": b"{}"})
    fake_github.artifacts_on_dispatch = [
        {"id": 7, "name": "test-results", "size_in_bytes": 300, "expired": False},
        {"id": 8, "name": "old", "size_in_bytes": 1, "expired": True},
    ]
    ex = executor(fake_github, artifacts_dir=tmp_path / "artifacts")
    result = await ex.run(CommandSpec(argv=("ai-coding-exec.yml",), cwd=tmp_path))
    assert result.ok, result.stderr
    arts = json.loads(result.stdout)["artifacts"]
    assert [a["name"] for a in arts] == ["test-results", "old"]
    assert arts[0]["files"] == 2 and "path" not in arts[1]
    extracted = Path(arts[0]["path"])
    assert (extracted / "junit.xml").read_bytes() == b"<testsuites/>"
    assert (extracted / "sub" / "report.json").exists()
    # The signed blob URL is fetched without the GitHub token.
    assert fake_github.blob_requests == [None]


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (_zip({"../escape.txt": b"x"}), "escapes"),
        (_zip({"ok.txt": b"x"}, symlink="link"), "symlink"),
        (_zip({"big.bin": b"0" * 4096}), "beyond|limit"),
    ],
)
def test_artifact_extraction_is_safe(
    fake_github: FakeGitHub, tmp_path: Path, payload: bytes, match: str
) -> None:
    fake_github.blobs[9] = payload
    with pytest.raises(Exception, match=match):
        host(fake_github).download_artifact(
            {"id": 9, "name": "a", "expired": False}, tmp_path, max_bytes=1024
        )
    assert not (tmp_path / "escape.txt").exists()


def test_artifact_name_and_expiry_are_checked(fake_github: FakeGitHub, tmp_path: Path) -> None:
    h = host(fake_github)
    with pytest.raises(ValueError, match="unsafe name"):
        h.download_artifact({"id": 1, "name": "../x"}, tmp_path)
    with pytest.raises(ValueError, match="expired"):
        h.download_artifact({"id": 1, "name": "x", "expired": True}, tmp_path)


async def test_actions_executor_guards(fake_github: FakeGitHub, tmp_path: Path) -> None:
    ex = executor(fake_github)
    with pytest.raises(ExecutorError, match="not allow-listed"):
        await ex.run(CommandSpec(argv=("deploy.yml",), cwd=tmp_path))
    with pytest.raises(ExecutorError, match="invalid workflow input"):
        await ex.run(CommandSpec(argv=("ai-coding-exec.yml", "noequals"), cwd=tmp_path))
    with pytest.raises(ExecutorError, match="invalid workflow input"):
        await ex.run(CommandSpec(argv=("ai-coding-exec.yml", "correlation_id=x"), cwd=tmp_path))
    fake_github.handle = lambda m, p, q, b: (
        (204, None)
        if p.endswith("/dispatches")
        else (  # type: ignore[method-assign]
            200,
            {"workflow_runs": []},
        )
        if p.endswith("/runs")
        else (404, {})
    )
    ticks = iter(range(0, 10_000, 100))
    lost = executor(fake_github, clock=lambda: float(next(ticks)))
    with pytest.raises(ExecutorError, match="run-name"):
        await lost.run(CommandSpec(argv=("ai-coding-exec.yml",), cwd=tmp_path))


# ---------------------------------------------------------------- git push --
@pytest.fixture
def remote_repo(git_repo: LocalGitRepository, tmp_path: Path) -> tuple[LocalGitRepository, Path]:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", bare.as_uri()], cwd=git_repo.root, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "ai/feature"], cwd=git_repo.root, check=True)
    (git_repo.root / "f.txt").write_text("x", encoding="utf-8")
    git_repo.commit("feat: f")
    return git_repo, bare


def test_push_and_remote_sha(remote_repo: tuple[LocalGitRepository, Path]) -> None:
    repo, _ = remote_repo
    assert repo.remote_branch_sha("ai/feature") is None
    sha = repo.push("ai/feature", token="secret-token-123")
    assert sha == repo.rev_parse("ai/feature") == repo.remote_branch_sha("ai/feature")
    with pytest.raises(ProtectedBranchError):
        repo.push("main")
    with pytest.raises(GitCommandError, match="does not exist"):
        repo.push("ai/missing")
    with pytest.raises(GitCommandError, match="not configured"):
        repo.push("ai/feature", remote="upstream")


def test_token_travels_in_environment_not_argv() -> None:
    from ai_coding.git.local import _auth_env  # noqa: PLC0415
    from ai_coding.security import redact  # noqa: PLC0415

    env = _auth_env("tok-abcdef-123456")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")
    assert "tok-abcdef-123456" not in redact(f"x tok-abcdef-123456 {env['GIT_CONFIG_VALUE_0']}")


# ------------------------------------------------------- PR workflow (FR-17) --
async def test_pr_workflow_with_approval_and_idempotency(
    remote_repo: tuple[LocalGitRepository, Path], fake_github: FakeGitHub
) -> None:
    repo, bare = remote_repo
    fake_github.head_shas["ai/feature"] = repo.rev_parse("ai/feature")
    config = make_config(security__network="allowlist", security__egress_allowlist=["localhost"])
    request = PullRequestRequest(branch="ai/feature", base="main", title="Add f", body="Adds f")
    registry, steps = build_pull_request(
        request, host(fake_github), token=lambda: None, api_host="localhost"
    )
    orch = make_orchestrator(repo.root, list(registry.all()), config=config)
    run_id = orch.create_run(TaskPlanner().build(steps), goal="pr")
    waiting = await orch.execute(run_id)
    assert waiting.state is RunState.AWAITING_APPROVAL  # pr_create needs a human (FR-17)
    assert [a["operation"] for a in waiting.approvals] == ["pr_create"]
    assert fake_github.pulls == {}
    orch.decide(run_id, approve=True)
    final = await orch.resume(run_id)
    assert final.state is RunState.COMPLETED, final.error
    assert len(fake_github.pulls) == 1
    pr = fake_github.pulls[1]
    assert pr["head"]["ref"] == "ai/feature" and pr["base"]["ref"] == "main"
    remote = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "ai/feature"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert remote == repo.rev_parse("ai/feature")
    # main on the remote was never touched (AC-03)
    assert (
        subprocess.run(
            ["git", "--git-dir", str(bare), "rev-parse", "--verify", "main"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )
    # A second identical run reuses the PR instead of opening another one.
    second = orch.create_run(TaskPlanner().build(steps), goal="pr again")
    orch.decide(second, approve=True) if (
        await orch.execute(second)
    ).state is RunState.AWAITING_APPROVAL else None
    assert (await orch.resume(second)).state is RunState.COMPLETED
    assert len(fake_github.pulls) == 1


async def test_pr_workflow_denied_by_egress_policy(
    remote_repo: tuple[LocalGitRepository, Path], fake_github: FakeGitHub
) -> None:
    repo, _ = remote_repo
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/acme/demo.git"],
        cwd=repo.root,
        check=True,
    )
    registry, steps = build_pull_request(
        PullRequestRequest(branch="ai/feature", base="main", title="t"),
        host(fake_github),
        token=lambda: None,
        api_host="localhost",
    )
    orch = make_orchestrator(repo.root, list(registry.all()))  # default: local-only
    run_id = orch.create_run(TaskPlanner().build(steps))
    status = await orch.execute(run_id)
    # Plan-level approval for PR creation comes first; egress is still enforced afterwards.
    assert status.state is RunState.AWAITING_APPROVAL
    orch.decide(run_id, approve=True)
    status = await orch.resume(run_id)
    assert status.state is RunState.FAILED
    assert "local-only forbids egress" in (status.error or "")
    assert fake_github.pulls == {}


# --------------------------------------------------------------------- CLI --
@pytest.fixture
def report_file(tmp_path: Path) -> Path:
    report = {
        "schema_version": "1",
        "project": "demo",
        "run_id": "r",
        "generated_at": "t",
        "backend": "docker",
        "verdict": "FAIL",
        "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
        "gates": [
            {"gate": "security", "result": "FAIL", "blocking": True, "reason": "1", "evidence": []}
        ],
        "tools": [
            {
                "tool": "semgrep",
                "category": "sast",
                "version": "1",
                "succeeded": True,
                "error": None,
                "duration_seconds": 1,
                "findings": [],
            }
        ],
        "findings": [
            {
                "tool": "semgrep",
                "rule_id": "eval",
                "severity": "high",
                "title": "eval",
                "message": "m",
                "path": "a.py",
                "line": 2,
                "fingerprint": "f" * 64,
                "status": "new",
            }
        ],
        "baseline": {"suppressed": 0, "expired": 0},
        "integrity": {"unchanged": True, "method": "d"},
        "discovery": {},
        "notes": [],
    }
    path = tmp_path / "audit-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_cli_comment_check_protection(fake_github: FakeGitHub, report_file: Path) -> None:
    for _ in range(2):
        result = runner.invoke(
            app, ["github", "comment", "--pr", "3", "--report", str(report_file)]
        )
        assert result.exit_code == 0, result.output
    assert len(fake_github.comments) == 1
    check = runner.invoke(app, ["github", "check", "--sha", SHA, "--report", str(report_file)])
    assert check.exit_code == 0 and "1 annotation" in check.output
    created = next(iter(fake_github.check_runs.values()))
    assert created["conclusion"] == "failure" and created["name"] == "ai-coding / audit"
    unprotected = runner.invoke(app, ["github", "protection", "--json"])
    assert (
        unprotected.exit_code == ExitCode.INVALID and json.loads(unprotected.output)["ok"] is False
    )
    fake_github.rules["main"] = [
        {"type": t} for t in ("pull_request", "required_status_checks", "non_fast_forward")
    ]
    assert runner.invoke(app, ["github", "protection"]).exit_code == 0


def test_cli_github_errors(
    fake_github: FakeGitHub, report_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_sha = runner.invoke(app, ["github", "check", "--sha", "nope", "--report", str(report_file)])
    assert bad_sha.exit_code == ExitCode.INVALID
    missing = runner.invoke(app, ["github", "comment", "--pr", "1", "--report", "nope.json"])
    assert missing.exit_code == ExitCode.NOT_FOUND
    monkeypatch.delenv("GITHUB_TOKEN")
    no_auth = runner.invoke(app, ["github", "comment", "--pr", "1", "--report", str(report_file)])
    assert no_auth.exit_code == ExitCode.USAGE and "no GitHub credentials" in no_auth.output
    monkeypatch.delenv("GITHUB_REPOSITORY")
    no_repo = runner.invoke(app, ["github", "protection", "--repo", "not a repo"])
    assert no_repo.exit_code == ExitCode.USAGE


def test_cli_report_export(report_file: Path, tmp_path: Path) -> None:
    sarif = runner.invoke(app, ["report", "export", "-i", str(report_file), "-f", "sarif"])
    assert sarif.exit_code == 0 and json.loads(sarif.output)["version"] == "2.1.0"
    out = tmp_path / "junit.xml"
    assert (
        runner.invoke(
            app, ["report", "export", "-i", str(report_file), "-f", "junit", "-o", str(out)]
        ).exit_code
        == 0
    )
    assert out.read_text(encoding="utf-8").startswith("<?xml")
    md = runner.invoke(app, ["report", "export", "-i", str(report_file), "-f", "markdown"])
    assert "# Audit report — demo" in md.output
    js = runner.invoke(app, ["report", "export", "-i", str(report_file), "-f", "json"])
    assert json.loads(js.output)["verdict"] == "FAIL"
    assert runner.invoke(app, ["report", "export"]).exit_code == ExitCode.USAGE
    assert (
        runner.invoke(app, ["report", "export", "-i", "missing.json"]).exit_code
        == ExitCode.NOT_FOUND
    )
    bad = tmp_path / "bad.json"
    bad.write_text('{"tools": [{"x": 1}]}', encoding="utf-8")
    assert (
        runner.invoke(app, ["report", "export", "-i", str(bad), "-f", "sarif"]).exit_code
        == ExitCode.INVALID
    )


def test_cli_report_export_from_run(tmp_path: Path) -> None:
    import shutil  # noqa: PLC0415

    if shutil.which("ruff") is None:
        pytest.skip("ruff not on PATH")
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.py").write_text("import os\n", encoding="utf-8")
    runner.invoke(app, ["init", str(project)])
    runner.invoke(app, ["audit", "-C", str(project), "-b", "local", "-t", "ruff"])
    runs = json.loads(runner.invoke(app, ["run", "list", "-C", str(project), "--json"]).output)
    result = runner.invoke(
        app, ["report", "export", runs[0]["id"], "-C", str(project), "-f", "sarif"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["runs"][0]["tool"]["driver"]["name"] == "ruff"
    runner.invoke(app, ["run", "discover", "-C", str(project)])
    discover_id = json.loads(
        runner.invoke(app, ["run", "list", "-C", str(project), "--json"]).output
    )[0]["id"]
    no_report = runner.invoke(app, ["report", "export", discover_id, "-C", str(project)])
    assert no_report.exit_code == ExitCode.NOT_FOUND


def test_cli_pr_open(remote_repo: tuple[LocalGitRepository, Path], fake_github: FakeGitHub) -> None:
    repo, _ = remote_repo
    fake_github.head_shas["ai/feature"] = repo.rev_parse("ai/feature")
    runner.invoke(app, ["init", str(repo.root), "--mode", "develop"])
    cfg = repo.root / ".ai-coding.yml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "  network: local-only       # local-only | allowlist | open\n  egress_allowlist: []",
            "  network: allowlist\n  egress_allowlist: [localhost]",
        ),
        encoding="utf-8",
    )
    base = ["pr", "open", "-C", str(repo.root), "--branch", "ai/feature", "--title", "Add f"]
    dry = runner.invoke(app, [*base, "--dry-run"])
    assert dry.exit_code == 0 and "github.pr" in dry.output and "require_approval" in dry.output
    assert runner.invoke(app, [*base, "--branch", "main"]).exit_code == ExitCode.USAGE
    waiting = runner.invoke(app, base)
    assert waiting.exit_code == ExitCode.PENDING, waiting.output
    run_id = json.loads(runner.invoke(app, ["run", "list", "-C", str(repo.root), "--json"]).output)[
        0
    ]["id"]
    # The generic resume lacks the PR handlers: it refuses instead of failing the run.
    generic = runner.invoke(app, ["run", "approve", run_id, "-C", str(repo.root), "--resume"])
    assert generic.exit_code == ExitCode.USAGE
    assert "pr resume" in generic.output + (generic.stderr if generic.stderr_bytes else "")
    state = runner.invoke(app, ["run", "status", run_id, "-C", str(repo.root), "--json"])
    assert json.loads(state.output)["state"] == "AWAITING_APPROVAL"
    assert fake_github.pulls == {}
    done = runner.invoke(app, ["pr", "resume", run_id, "-C", str(repo.root)])
    assert done.exit_code == ExitCode.OK, done.output
    assert len(fake_github.pulls) == 1
    pr = next(iter(fake_github.pulls.values()))
    assert pr["head"]["ref"] == "ai/feature" and pr["base"]["ref"] == "main"
    assert repo.remote_branch_sha("ai/feature") == repo.rev_parse("ai/feature")
    # A second resume is a no-op: the effect ledger prevents a second push or PR.
    again = runner.invoke(app, ["pr", "resume", run_id, "-C", str(repo.root)])
    assert again.exit_code in {ExitCode.OK, ExitCode.INVALID}
    assert len(fake_github.pulls) == 1
