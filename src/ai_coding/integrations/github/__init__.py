"""GitHub integration: REST client, repository host adapter, Actions executor (S4)."""

from ai_coding.integrations.github.auth import (
    AppAuth,
    GitHubAuth,
    TokenAuth,
    auth_from_env,
)
from ai_coding.integrations.github.client import (
    GitHubAPIError,
    GitHubClient,
    RateLimitError,
    parse_repo_slug,
)
from ai_coding.integrations.github.host import GitHubHost, PullRequest

__all__ = [
    "AppAuth",
    "GitHubAPIError",
    "GitHubAuth",
    "GitHubClient",
    "GitHubHost",
    "PullRequest",
    "RateLimitError",
    "TokenAuth",
    "auth_from_env",
    "parse_repo_slug",
]
