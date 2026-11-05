"""Content-addressed local artifact store (ADR-0005, NFR-03, NFR-08).

Blobs are stored once under ``<root>/sha256/<aa>/<digest>`` and written atomically
(temp file + ``os.replace``), so a crash never leaves a partial artifact visible.
Integrity is re-checked on every read.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_DIGEST_LEN = 64


class ArtifactIntegrityError(Exception):
    """Stored content does not match its digest."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Reference to a stored blob."""

    sha256: str
    size_bytes: int
    uri: str


class LocalArtifactStore:
    """Artifact store on the local filesystem."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, digest: str) -> Path:
        if len(digest) != _DIGEST_LEN or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"invalid sha256 digest: {digest!r}")
        return self.root / "sha256" / digest[:2] / digest

    def put(self, data: bytes) -> ArtifactRef:
        """Store ``data`` (idempotent: identical content is stored once)."""
        digest = hashlib.sha256(data).hexdigest()
        target = self._path(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                Path(tmp).replace(target)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return ArtifactRef(sha256=digest, size_bytes=len(data), uri=f"sha256:{digest}")

    def get(self, digest: str) -> bytes:
        """Read a blob and verify its digest."""
        data = self._path(digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ArtifactIntegrityError(f"artifact {digest} is corrupted")
        return data

    def exists(self, digest: str) -> bool:
        """Whether a blob is stored."""
        return self._path(digest).is_file()

    def delete(self, digest: str) -> bool:
        """Delete a blob; returns whether it existed."""
        path = self._path(digest)
        if path.is_file():
            path.unlink()
            return True
        return False

    def purge_older_than(self, days: int, *, keep: set[str] | None = None) -> list[str]:
        """Delete blobs older than the retention period (NFR-08); returns deleted digests."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        keep = keep or set()
        deleted: list[str] = []
        base = self.root / "sha256"
        if not base.is_dir():
            return deleted
        for path in sorted(base.glob("*/*")):
            if path.name.startswith(".") or path.name in keep:
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if mtime < cutoff:
                path.unlink()
                deleted.append(path.name)
        return deleted
