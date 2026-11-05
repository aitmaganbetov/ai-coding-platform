"""Base error classes shared across layers (retry classification, ADR-0006)."""


class TransientError(Exception):
    """Temporary failure; the operation may be retried with backoff."""


class PermanentError(Exception):
    """Deterministic failure; retrying cannot help."""
