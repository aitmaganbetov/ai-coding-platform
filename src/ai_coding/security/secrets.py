"""Detection of secrets embedded in configuration documents.

ТЗ §11: secrets are passed only through a secret store / environment and never in
YAML. Configuration may *reference* a secret (``api_key_env: OPENAI_API_KEY`` or
``"${env:OPENAI_API_KEY}"``) but must not contain its value.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from ai_coding.security.redaction import DEFAULT_PATTERNS

_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|apikey|secret|token|passw(?:or)?d|private[_-]?key|"
    r"access[_-]?key|client[_-]?secret|credentials?)$"
)
# Keys that *name* a secret rather than hold it.
_REFERENCE_KEY_RE = re.compile(r"(?i)(?:_env|_ref|_secret_name|_file)$")
_ENV_REFERENCE_RE = re.compile(r"^\$\{(?:env|secret):[A-Za-z_][A-Za-z0-9_./-]*\}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Value patterns from the redactor that are specific enough to flag on their own.
_VALUE_PATTERNS = tuple(p for p in DEFAULT_PATTERNS if p.name not in {"assignment"})


@dataclass(frozen=True, slots=True)
class SecretFinding:
    """A suspected secret value inside a configuration document."""

    path: str
    reason: str


def is_secret_key(key: str) -> bool:
    """Whether ``key`` looks like it would hold a secret value."""
    return bool(_SECRET_KEY_RE.search(key)) and not _REFERENCE_KEY_RE.search(key)


def is_secret_reference(value: str) -> bool:
    """Whether ``value`` references a secret instead of containing it."""
    return bool(_ENV_REFERENCE_RE.match(value))


def find_secrets_in_mapping(document: object, path: str = "") -> list[SecretFinding]:
    """Recursively scan a parsed YAML/JSON document for embedded secret values."""
    return list(_walk(document, path))


def _walk(node: object, path: str) -> Iterator[SecretFinding]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if (
                isinstance(key, str)
                and is_secret_key(key)
                and isinstance(value, str | int)
                and not isinstance(value, bool)
                and str(value).strip()
                and not is_secret_reference(str(value))
            ):
                yield SecretFinding(child, f"key '{key}' holds a literal value")
                continue
            if isinstance(key, str) and _REFERENCE_KEY_RE.search(key) and isinstance(value, str):
                if key.endswith("_env") and not _ENV_NAME_RE.match(value):
                    yield from _check_value(value, child)
                continue
            yield from _walk(value, child)
    elif isinstance(node, Sequence) and not isinstance(node, str | bytes):
        for index, item in enumerate(node):
            yield from _walk(item, f"{path}[{index}]")
    elif isinstance(node, str):
        yield from _check_value(node, path)


def _check_value(value: str, path: str) -> Iterator[SecretFinding]:
    for pattern in _VALUE_PATTERNS:
        if pattern.regex.search(value):
            yield SecretFinding(path, f"value matches secret pattern '{pattern.name}'")
            return
