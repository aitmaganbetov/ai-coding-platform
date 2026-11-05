"""``RepositoryHost`` port (ТЗ §4 "Git adapters"; ADR-0021).

Workflows depend on this protocol, not on GitHub: another forge (GitLab, Gitea, …)
can be added as a separate adapter without touching the orchestrator or workflows.
The GitHub adapter is :class:`ai_coding.integrations.github.GitHubHost`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ai_coding.integrations.github.host import (
        Annotation,
        Issue,
        ProtectionReport,
        PullRequest,
    )


@runtime_checkable
class RepositoryHost(Protocol):
    """Operations the platform performs on a hosted repository.

    Deliberately absent: merge, auto-merge, force-push, branch deletion — the
    platform never performs them (FR-17, AC-03).
    """

    repo: str

    def get_issue(self, number: int) -> Issue:
        """Issue by number."""
        ...

    def find_pull_request(self, head: str, base: str | None = None) -> PullRequest | None:
        """Open PR from ``head`` (into ``base``), if any."""
        ...

    def get_pull_request(self, number: int) -> PullRequest:
        """PR by number."""
        ...

    def create_pull_request(
        self, *, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> PullRequest:
        """Open a PR; idempotent — returns the existing open PR for the same head/base."""
        ...

    def upsert_comment(self, number: int, key: str, body: str) -> tuple[int, bool]:
        """Create or update the single platform comment identified by ``key``."""
        ...

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
        """Publish a completed check with annotations."""
        ...

    def create_commit_status(
        self, *, sha: str, state: str, context: str, description: str, target_url: str | None = None
    ) -> None:
        """Publish a commit status (fallback when checks are unavailable)."""
        ...

    def branch_protection(self, branch: str) -> ProtectionReport:
        """Effective protection of ``branch`` and the gaps against ТЗ §15."""
        ...
