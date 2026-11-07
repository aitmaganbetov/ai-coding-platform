"""Settings and secrets of a project made in the Web UI (ADR-0043, ADR-0046, ADR-0048).

* **Sections** (``ai``, ``github``…) are JSON documents owned and validated by the module
  that uses them; they never hold a secret.
* **Secrets** (API keys, tokens, webhook signing keys) are encrypted at rest with
  AES-256-GCM under the server's key (``AI_CODING_SERVER_SECRET_KEY``), bound to their
  project and name (associated data). They are write-only: the API returns a hint (the
  last characters) and never the value; only the server reads them, to call the service
  they are for. Without a server key, secrets cannot be stored at all.

Reading a section needs ``project.view``; changing settings or secrets
``project.settings`` (owners and maintainers). Every change is in the audit trail —
the secret's name, never its value.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import uuid
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ai_coding.api.core import ConflictError, InvalidError, NotFoundError, Principal, iso_or_none
from ai_coding.api.projects import ProjectService
from ai_coding.api.rbac import Permission
from ai_coding.domain.enums import Outcome
from ai_coding.persistence.models import ProjectSecret, ProjectSetting
from ai_coding.persistence.types import utcnow

SECRET_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
SECTION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SECRET_LIMIT = 16 * 1024


class SecretStoreUnavailableError(ConflictError):
    """The server has no (valid) secret key: secrets cannot be stored or read."""


def parse_key(value: str | None) -> bytes | None:
    """The server key: 32 bytes as 64 hex digits or base64; anything else is refused."""
    if not value:
        return None
    text = value.strip()
    try:
        raw = (
            bytes.fromhex(text)
            if re.fullmatch(r"[0-9a-fA-F]{64}", text)
            else base64.b64decode(text, validate=True)
        )
    except (ValueError, binascii.Error):
        return None
    return raw if len(raw) == 32 else None


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"ai-coding secret store" + key).hexdigest()[:16]


def _hint(value: str) -> str:
    return f"…{value[-4:]}" if len(value) >= 12 else "••••"


class ProjectSettingsService(ProjectService):
    """Sections and encrypted secrets of projects."""

    # ------------------------------------------------------------- secrets --
    def _key(self) -> bytes:
        key = parse_key(self.settings.secret_key)
        if key is None:
            raise SecretStoreUnavailableError(
                "the server has no secret key (AI_CODING_SERVER_SECRET_KEY, 32 bytes as hex or "
                "base64): secrets such as API keys and tokens cannot be stored"
            )
        return key

    @property
    def secrets_available(self) -> bool:
        return parse_key(self.settings.secret_key) is not None

    def list_secrets(self, principal: Principal, project_id: uuid.UUID) -> dict[str, Any]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
            rows = (
                s.query(ProjectSecret)
                .filter(ProjectSecret.project_id == project_id)
                .order_by(ProjectSecret.name)
            )
            secrets = [{"name": r.name, "hint": r.hint, "updated_by": r.updated_by,
                        "updated_at": iso_or_none(r.updated_at)} for r in rows]  # fmt: skip
        return {"available": self.secrets_available, "secrets": secrets}

    def set_secret(
        self, principal: Principal, project_id: uuid.UUID, name: str, value: str
    ) -> dict[str, Any]:
        with self.session() as s:  # access first: others learn nothing, not even validity
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
        if not SECRET_NAME.fullmatch(name):
            raise InvalidError(
                "a secret name is lowercase letters, digits, '.', '_' or '-' (64 at most)"
            )
        value = value.strip("\r\n")
        if not value or len(value.encode()) > SECRET_LIMIT:
            raise InvalidError("a secret value of 1 byte to 16 KB is required")
        key = self._key()
        nonce = os.urandom(12)
        sealed = AESGCM(key).encrypt(nonce, value.encode(), f"{project_id}:{name}".encode())
        with self.session() as s:
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
            row = s.get(ProjectSecret, (project_id, name))
            if row is None:
                row = ProjectSecret(project_id=project_id, name=name)
                s.add(row)
            row.key_id, row.nonce, row.ciphertext = key_id(key), nonce, sealed
            row.hint, row.updated_by, row.updated_at = _hint(value), principal.username, utcnow()
            self.record(s, principal.username, "project.secret.set", Outcome.SUCCESS,
                        project_id=project_id, target=name)  # fmt: skip
            return {"name": name, "hint": row.hint, "updated_by": principal.username,
                    "updated_at": iso_or_none(row.updated_at)}  # fmt: skip

    def delete_secret(self, principal: Principal, project_id: uuid.UUID, name: str) -> None:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
            row = s.get(ProjectSecret, (project_id, name))
            if row is None:
                raise NotFoundError("no such secret")
            s.delete(row)
            self.record(s, principal.username, "project.secret.delete", Outcome.SUCCESS,
                        project_id=project_id, target=name)  # fmt: skip

    def secret_value(self, project_id: uuid.UUID, name: str) -> str | None:
        """The value, for the server's own use (calling the service it is for); ``None``
        when there is no such secret."""
        with self.session() as s:
            row = s.get(ProjectSecret, (project_id, name))
            if row is None:
                return None
            key_ref, nonce, sealed = row.key_id, row.nonce, row.ciphertext
        key = self._key()
        if key_ref != key_id(key):
            raise SecretStoreUnavailableError(
                f"the secret '{name}' was stored under another server key: set it again"
            )
        try:
            return AESGCM(key).decrypt(nonce, sealed, f"{project_id}:{name}".encode()).decode()
        except InvalidTag:
            raise SecretStoreUnavailableError(
                f"the secret '{name}' cannot be decrypted: set it again"
            ) from None

    # ------------------------------------------------------------ sections --
    def section(self, project_id: uuid.UUID, name: str) -> dict[str, Any]:
        """A section's data for the server's own use (``{}`` when never set)."""
        with self.session() as s:
            row = s.get(ProjectSetting, (project_id, name))
            return dict(row.data) if row is not None else {}

    def read_section(
        self, principal: Principal, project_id: uuid.UUID, name: str
    ) -> dict[str, Any]:
        with self.session() as s:
            self.require(s, principal, project_id, Permission.PROJECT_VIEW)
            row = s.get(ProjectSetting, (project_id, name))
            if row is None:
                return {"data": {}, "version": 0, "updated_by": None, "updated_at": None}
            return {"data": dict(row.data), "version": row.version, "updated_by": row.updated_by,
                    "updated_at": iso_or_none(row.updated_at)}  # fmt: skip

    def write_section(
        self,
        principal: Principal,
        project_id: uuid.UUID,
        name: str,
        data: dict[str, Any],
        *,
        version: int | None = None,
    ) -> dict[str, Any]:
        """Replace a section (validated by its owner before); ``version`` guards against
        overwriting someone else's change made meanwhile."""
        if not SECTION_NAME.fullmatch(name):
            raise InvalidError("unknown settings section")
        with self.session() as s:
            self.require(s, principal, project_id, Permission.PROJECT_SETTINGS)
            row = s.get(ProjectSetting, (project_id, name))
            current = row.version if row is not None else 0
            if version is not None and version != current:
                raise ConflictError("the settings were changed meanwhile: reload and try again")
            if row is None:
                row = ProjectSetting(project_id=project_id, section=name, version=0)
                s.add(row)
            row.data, row.version = dict(data), current + 1
            row.updated_by, row.updated_at = principal.username, utcnow()
            self.record(s, principal.username, "project.settings", Outcome.SUCCESS,
                        project_id=project_id, target=name)  # fmt: skip
            return {"data": dict(row.data), "version": row.version, "updated_by": row.updated_by,
                    "updated_at": iso_or_none(row.updated_at)}  # fmt: skip


__all__ = ["ProjectSettingsService", "SecretStoreUnavailableError", "key_id", "parse_key"]
