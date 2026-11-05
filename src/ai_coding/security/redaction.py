"""Redaction of secret-looking substrings.

The same :class:`Redactor` is meant to be applied to logs, prompts, PR comments and
artifacts (ТЗ §12). Patterns are deliberately conservative: a false positive only
hides a few characters, a false negative leaks a credential.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import partial

REDACTED = "***REDACTED***"


@dataclass(frozen=True, slots=True)
class SecretPattern:
    """A named regular expression that matches a secret value."""

    name: str
    regex: re.Pattern[str]
    group: int = 0  # which group holds the secret itself


def _p(name: str, pattern: str, group: int = 0, flags: int = 0) -> SecretPattern:
    return SecretPattern(name=name, regex=re.compile(pattern, flags), group=group)


DEFAULT_PATTERNS: tuple[SecretPattern, ...] = (
    _p(
        "private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),
    _p("anthropic-key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    _p("openai-key", r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"),
    _p("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    _p("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    _p("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    _p("google-api-key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    _p("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    _p("url-credentials", r"(?<=://)[^/\s:@]+:([^/\s@]+)(?=@)", group=1),
    _p(
        "signed-url-param",
        r"(?i)[?&](?:sig|signature|x-amz-signature|x-goog-signature|hmac|token|access_token|"
        r"x-amz-security-token|sas|sv-signature)=([^&\s\"']{6,})",
        group=1,
    ),
    _p(
        "assignment",
        r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key|access[_-]?key)"
        r"\b\s*[:=]\s*['\"]?([^\s'\"]{6,})",
        group=1,
    ),
)


@dataclass(slots=True)
class Redactor:
    """Replaces secret matches with :data:`REDACTED`."""

    patterns: tuple[SecretPattern, ...] = DEFAULT_PATTERNS
    literals: set[str] = field(default_factory=set)

    def add_literals(self, values: Iterable[str]) -> None:
        """Register exact values (e.g. resolved environment secrets) to always hide."""
        self.literals.update(v for v in values if v and len(v) >= 4)

    def find(self, text: str) -> list[str]:
        """Return names of patterns that match ``text`` (literal matches report ``literal``)."""
        hits = [p.name for p in self.patterns if p.regex.search(text)]
        if any(lit in text for lit in self.literals):
            hits.append("literal")
        return hits

    def redact(self, text: str) -> str:
        """Return ``text`` with every secret occurrence replaced."""
        for lit in sorted(self.literals, key=len, reverse=True):
            text = text.replace(lit, REDACTED)
        for pattern in self.patterns:
            text = pattern.regex.sub(partial(_replace, group=pattern.group), text)
        return text


def _replace(match: re.Match[str], group: int) -> str:
    if group == 0:
        return REDACTED
    whole = match.group(0)
    start = match.start(group) - match.start(0)
    end = match.end(group) - match.start(0)
    return whole[:start] + REDACTED + whole[end:]


default_redactor = Redactor()


def redact(text: str) -> str:
    """Redact ``text`` with the default pattern set."""
    return default_redactor.redact(text)
