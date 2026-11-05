"""Repository host adapter for GitHub (ТЗ §4, FR-10, FR-17; ADR-0021).

Guarantees:

* pull requests are idempotent by head branch (an existing open PR is reused);
* the platform never enables auto-merge and never merges (FR-17);
* PR comments are upserted by a hidden marker, so re-runs do not duplicate (FR-10);
* check runs carry at most 50 annotations per request (GitHub limit), batched.
"""

from __future__ import annotations

import io
import re
import stat
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_coding.integrations.github.client import GitHubAPIError, GitHubClient, parse_repo_slug
from ai_coding.security import redact

MAX_COMMENT_CHARS = 65_000
MAX_ANNOTATIONS_PER_REQUEST = 50
MAX_CHECK_TEXT = 65_000
_MARKER_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_WORKFLOW = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9._ -]{1,128}$")
MAX_ARTIFACT_BYTES = 200 * 1024 * 1024
MAX_ARTIFACT_FILES = 10_000


@dataclass(frozen=True, slots=True)
class PullRequest:
    """Subset of a GitHub pull request."""

    number: int
    url: str
    head: str
    base: str
    state: str
    draft: bool
    created: bool = False  # True when this call created it
    head_sha: str = ""
    auto_merge: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any], *, created: bool = False) -> PullRequest:
        return cls(
            number=int(data["number"]),
            url=str(data["html_url"]),
            head=str(data["head"]["ref"]),
            base=str(data["base"]["ref"]),
            state=str(data["state"]),
            draft=bool(data.get("draft", False)),
            head_sha=str(data["head"].get("sha", "")),
            auto_merge=bool(data.get("auto_merge")),
            created=created,
        )


@dataclass(frozen=True, slots=True)
class Annotation:
    """A check-run annotation (file/line + message)."""

    path: str
    start_line: int
    level: str  # notice | warning | failure
    message: str
    title: str = ""

    def to_api(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.start_line,
            "annotation_level": self.level,
            "message": redact(self.message)[:64_000],
            "title": redact(self.title)[:255],
        }


@dataclass(frozen=True, slots=True)
class ProtectionReport:
    """What protects a branch against direct pushes (AC-03, ТЗ §15 Rulesets)."""

    branch: str
    rules: tuple[str, ...]
    requires_pull_request: bool
    requires_status_checks: bool
    blocks_force_push: bool
    gaps: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.gaps


class GitHubHost:
    """Operations on one repository."""

    def __init__(self, client: GitHubClient, repo: str) -> None:
        self.client = client
        self.repo = parse_repo_slug(repo)
        self._base = f"/repos/{self.repo}"

    # ------------------------------------------------------------ pull requests --
    def find_pull_request(self, head: str, base: str | None = None) -> PullRequest | None:
        """Open PR whose head is ``head`` (same repository)."""
        owner = self.repo.split("/", 1)[0]
        params: dict[str, Any] = {"state": "open", "head": f"{owner}:{head}"}
        if base:
            params["base"] = base
        for item in self.client.paginate(f"{self._base}/pulls", params=params, limit=10):
            return PullRequest.from_api(item)
        return None

    def get_pull_request(self, number: int) -> PullRequest:
        """One pull request by number."""
        data = self.client.json("GET", f"{self._base}/pulls/{int(number)}")
        return PullRequest.from_api(data)

    def create_pull_request(
        self, *, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> PullRequest:
        """Open a PR, or return the existing open PR for ``head`` (idempotent)."""
        if head == base:
            raise ValueError("head and base must differ")
        existing = self.find_pull_request(head, base)
        if existing is not None:
            return existing
        data = self.client.json(
            "POST",
            f"{self._base}/pulls",
            json={
                "head": head,
                "base": base,
                "title": redact(title)[:250],
                "body": redact(body)[:MAX_COMMENT_CHARS],
                "draft": draft,
                "maintainer_can_modify": False,
            },
        )
        return PullRequest.from_api(data, created=True)

    # ----------------------------------------------------------------- comments --
    @staticmethod
    def marker(key: str) -> str:
        """Hidden HTML marker identifying a platform comment."""
        if not _MARKER_KEY.match(key):
            raise ValueError(f"invalid marker key {key!r}")
        return f"<!-- ai-coding:{key} -->"

    def upsert_comment(self, number: int, key: str, body: str) -> tuple[int, bool]:
        """Create or update the comment carrying ``key``; returns ``(id, created)``."""
        marker = self.marker(key)
        text = redact(body)
        if len(text) > MAX_COMMENT_CHARS - len(marker) - 100:
            text = text[: MAX_COMMENT_CHARS - len(marker) - 100] + "\n\n… (truncated)"
        content = f"{marker}\n{text}"
        for comment in self.client.paginate(f"{self._base}/issues/{number}/comments", limit=500):
            if str(comment.get("body", "")).startswith(marker):
                self.client.json(
                    "PATCH", f"{self._base}/issues/comments/{comment['id']}", json={"body": content}
                )
                return int(comment["id"]), False
        created = self.client.json(
            "POST", f"{self._base}/issues/{number}/comments", json={"body": content}
        )
        return int(created["id"]), True

    # --------------------------------------------------------------- check runs --
    def create_check_run(
        self,
        *,
        name: str,
        head_sha: str,
        conclusion: str,
        title: str,
        summary: str,
        text: str = "",
        annotations: Sequence[Annotation] = (),
    ) -> int:
        """Completed check run with annotations (batched by 50)."""
        if not _SHA.match(head_sha):
            raise ValueError("head_sha must be a full 40-character commit SHA")
        if conclusion not in {
            "success",
            "failure",
            "neutral",
            "cancelled",
            "skipped",
            "timed_out",
            "action_required",
        }:
            raise ValueError(f"invalid conclusion {conclusion!r}")
        batches = [
            list(annotations[i : i + MAX_ANNOTATIONS_PER_REQUEST])
            for i in range(0, len(annotations), MAX_ANNOTATIONS_PER_REQUEST)
        ] or [[]]
        output = {
            "title": redact(title)[:255],
            "summary": redact(summary)[:MAX_CHECK_TEXT],
            "text": redact(text)[:MAX_CHECK_TEXT],
        }
        created = self.client.json(
            "POST",
            f"{self._base}/check-runs",
            json={
                "name": name,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {**output, "annotations": [a.to_api() for a in batches[0]]},
            },
        )
        check_id = int(created["id"])
        for batch in batches[1:]:
            self.client.json(
                "PATCH",
                f"{self._base}/check-runs/{check_id}",
                json={"output": {**output, "annotations": [a.to_api() for a in batch]}},
            )
        return check_id

    def create_commit_status(
        self, *, sha: str, state: str, context: str, description: str, target_url: str | None = None
    ) -> None:
        """Commit status (works with tokens that cannot create check runs)."""
        if not _SHA.match(sha):
            raise ValueError("sha must be a full 40-character commit SHA")
        if state not in {"error", "failure", "pending", "success"}:
            raise ValueError(f"invalid state {state!r}")
        payload: dict[str, Any] = {
            "state": state,
            "context": context,
            "description": redact(description)[:140],
        }
        if target_url:
            payload["target_url"] = target_url
        self.client.json("POST", f"{self._base}/statuses/{sha}", json=payload)

    # ---------------------------------------------------------------- workflows --
    def dispatch_workflow(self, workflow: str, *, ref: str, inputs: dict[str, str]) -> None:
        """Trigger a ``workflow_dispatch`` workflow."""
        if not _WORKFLOW.match(workflow):
            raise ValueError(f"invalid workflow file name {workflow!r}")
        self.client.json(
            "POST",
            f"{self._base}/actions/workflows/{workflow}/dispatches",
            json={"ref": ref, "inputs": inputs},
        )

    def workflow_runs(
        self, workflow: str, *, branch: str, event: str = "workflow_dispatch", limit: int = 30
    ) -> list[dict[str, Any]]:
        """Recent runs of ``workflow`` on ``branch``."""
        if not _WORKFLOW.match(workflow):
            raise ValueError(f"invalid workflow file name {workflow!r}")
        return list(
            self.client.paginate(
                f"{self._base}/actions/workflows/{workflow}/runs",
                params={"branch": branch, "event": event},
                key="workflow_runs",
                limit=limit,
            )
        )

    def workflow_run(self, run_id: int) -> dict[str, Any]:
        """One workflow run."""
        data: dict[str, Any] = self.client.json("GET", f"{self._base}/actions/runs/{int(run_id)}")
        return data

    def cancel_workflow_run(self, run_id: int) -> None:
        """Cancel a run (used when the executor times out)."""
        self.client.json("POST", f"{self._base}/actions/runs/{int(run_id)}/cancel")

    def workflow_jobs(self, run_id: int) -> list[dict[str, Any]]:
        """Jobs of a workflow run."""
        return list(
            self.client.paginate(f"{self._base}/actions/runs/{int(run_id)}/jobs", key="jobs")
        )

    def workflow_artifacts(self, run_id: int) -> list[dict[str, Any]]:
        """Artifacts uploaded by a workflow run (name, id, size, expired)."""
        return list(
            self.client.paginate(
                f"{self._base}/actions/runs/{int(run_id)}/artifacts", key="artifacts"
            )
        )

    def download_artifact(
        self, artifact: dict[str, Any], dest: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES
    ) -> list[Path]:
        """Download and safely extract one artifact into ``dest/<name>/``.

        Untrusted archive (ТЗ §12): names are validated, entries escaping the target
        directory (zip-slip), absolute paths and symlinks are refused, and both the
        compressed and the uncompressed size are capped.
        """
        name = str(artifact.get("name", ""))
        if not _ARTIFACT_NAME.match(name) or name in {".", ".."}:
            raise ValueError(f"refusing artifact with unsafe name {name!r}")
        if artifact.get("expired"):
            raise ValueError(f"artifact {name!r} has expired")
        data = self.client.download(
            f"{self._base}/actions/artifacts/{int(artifact['id'])}/zip", max_bytes=max_bytes
        )
        target = (dest / name).resolve()
        target.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARTIFACT_FILES:
                raise ValueError(f"artifact {name!r} has too many files")
            if sum(e.file_size for e in entries) > max_bytes:
                raise ValueError(f"artifact {name!r} expands beyond {max_bytes} bytes")
            for entry in entries:
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError(f"artifact {name!r} contains a symlink: {entry.filename}")
                path = (target / entry.filename).resolve()
                if not path.is_relative_to(target) or Path(entry.filename).is_absolute():
                    raise ValueError(f"artifact {name!r} escapes its directory: {entry.filename}")
                if entry.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as src, path.open("wb") as out:
                    out.write(src.read(entry.file_size + 1)[: entry.file_size])
                written.append(path)
        return written

    # --------------------------------------------------------------- protection --
    def branch_protection(self, branch: str) -> ProtectionReport:
        """Effective rules on ``branch`` (rulesets API; readable with read access)."""
        try:
            rules = list(self.client.paginate(f"{self._base}/rules/branches/{branch}"))
        except GitHubAPIError as exc:
            if exc.status != 404:
                raise
            rules = []
        types = tuple(sorted({str(r.get("type")) for r in rules}))
        requires_pr = "pull_request" in types
        requires_checks = "required_status_checks" in types
        blocks_force = "non_fast_forward" in types
        gaps = []
        if not requires_pr:
            gaps.append("pull requests are not required (direct pushes possible)")
        if not requires_checks:
            gaps.append("no required status checks (Quality Gates can be bypassed)")
        if not blocks_force:
            gaps.append("force pushes are not blocked")
        return ProtectionReport(
            branch, types, requires_pr, requires_checks, blocks_force, tuple(gaps)
        )
