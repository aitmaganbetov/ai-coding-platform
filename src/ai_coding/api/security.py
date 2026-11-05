"""Passwords and API tokens.

* passwords: argon2id (``argon2-cffi`` defaults), never stored or logged in clear;
* API tokens: ``aic_`` + 43 random URL-safe characters (256 bits). Only the SHA-256
  of a token is stored, so a database leak does not reveal usable tokens. A token can
  be limited to one project and to a maximum role, and expires.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

TOKEN_PREFIX = "aic_"  # noqa: S105 - a public prefix, not a secret
MIN_PASSWORD_LENGTH = 12
_hasher = PasswordHasher()


class WeakPasswordError(ValueError):
    """The password does not meet the minimum policy."""


def hash_password(password: str) -> str:
    """argon2id hash of ``password`` (minimum length enforced)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"password must have at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time verification; ``False`` on mismatch or a malformed hash."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether the hash uses outdated parameters (rehash on next login)."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def new_token() -> str:
    """A new random API token (shown to the user once)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """SHA-256 hex digest stored instead of the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_token(value: str) -> bool:
    """Cheap format check before touching the database."""
    return value.startswith(TOKEN_PREFIX) and 40 <= len(value) <= 100


def same_digest(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    return hmac.compare_digest(a.encode(), b.encode())
