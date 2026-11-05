"""Content-addressed artifact store."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from ai_coding.persistence.artifacts import ArtifactIntegrityError, LocalArtifactStore


def test_put_get_dedup(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    ref = store.put(b"hello")
    assert ref.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert ref.uri == f"sha256:{ref.sha256}" and ref.size_bytes == 5
    assert store.get(ref.sha256) == b"hello"
    assert store.put(b"hello") == ref
    assert len(list((tmp_path / "sha256").rglob("*"))) == 2  # one dir + one blob
    assert store.exists(ref.sha256)


def test_integrity_check(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    ref = store.put(b"data")
    path = tmp_path / "sha256" / ref.sha256[:2] / ref.sha256
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        store.get(ref.sha256)


@pytest.mark.parametrize("digest", ["", "../../etc/passwd", "A" * 64, "g" * 64, "a" * 63])
def test_invalid_digest_rejected(tmp_path: Path, digest: str) -> None:
    with pytest.raises(ValueError, match="invalid sha256"):
        LocalArtifactStore(tmp_path).get(digest)


def test_delete_and_purge(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    assert store.purge_older_than(1) == []
    old, new, kept = store.put(b"old"), store.put(b"new"), store.put(b"kept")
    past = time.time() - 10 * 86400
    for ref in (old, kept):
        p = tmp_path / "sha256" / ref.sha256[:2] / ref.sha256
        os.utime(p, (past, past))
    assert store.purge_older_than(5, keep={kept.sha256}) == [old.sha256]
    assert store.exists(new.sha256) and store.exists(kept.sha256)
    assert store.delete(new.sha256) is True
    assert store.delete(new.sha256) is False
