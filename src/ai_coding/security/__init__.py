"""Security primitives: secret detection and redaction (ТЗ §12, FR-18, NFR-07)."""

from ai_coding.security.redaction import REDACTED, Redactor, default_redactor, redact
from ai_coding.security.secrets import (
    SecretFinding,
    find_secrets_in_mapping,
    is_secret_key,
    is_secret_reference,
)

__all__ = [
    "REDACTED",
    "Redactor",
    "SecretFinding",
    "default_redactor",
    "find_secrets_in_mapping",
    "is_secret_key",
    "is_secret_reference",
    "redact",
]
