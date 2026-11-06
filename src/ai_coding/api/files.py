"""Files of a directory the caller controls (a user's workspace) read and written by the
server for the Web UI.

The user has a shell in that directory, so any name in it may be a symbolic link — or
become one while a request runs. Every path is therefore walked component by component
from a directory file descriptor with ``O_NOFOLLOW``: a link, planted or swapped in,
fails the request instead of leading the server out of the directory (to another
project, or to the server's own data). Files are opened ``O_NONBLOCK`` and must be
regular: a FIFO cannot hang a request. ``.git`` is neither listed nor accessible: git
state changes only through git, inside the user's own container.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from ai_coding.api.core import ConflictError, InvalidError, NotFoundError

MAX_READ_BYTES = 1_000_000  # larger files are listed but not shown in the editor
MAX_LIST_ENTRIES = 5_000
_HIDDEN = frozenset({".git"})
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def split_path(rel: str) -> list[str]:
    """Components of a path relative to the root; refuses ``..``, NUL and ``.git``."""
    if "\0" in rel or len(rel) > 4096:
        raise InvalidError("invalid path")
    parts = [p for p in PurePosixPath(rel.replace("\\", "/")).parts if p not in ("/", ".")]
    if any(p == ".." for p in parts):
        raise InvalidError("invalid path")
    if any(p in _HIDDEN for p in parts):
        raise InvalidError("git internals are not accessible here; use git in the terminal")
    return parts


def _os_error(exc: OSError) -> Exception:
    if isinstance(exc, FileNotFoundError):
        return NotFoundError("no such file or directory")
    if exc.errno in (errno.ELOOP, errno.EMLINK):  # O_NOFOLLOW met a symbolic link
        return InvalidError("symbolic links are not followed")
    if isinstance(exc, NotADirectoryError):
        return InvalidError("not a directory")
    if isinstance(exc, IsADirectoryError):
        return InvalidError("is a directory")
    return InvalidError(f"file system error ({exc.strerror or 'unknown'})")


@contextmanager
def _walk(root: Path, parts: list[str], *, create: bool = False) -> Iterator[int]:
    """A descriptor of ``root/parts…`` reached without following links."""
    fd = os.open(root, _DIR_FLAGS)
    try:
        for name in parts:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o755, dir_fd=fd)
            nested = os.open(name, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nested
        yield fd
    finally:
        os.close(fd)


def _version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


def list_dir(root: Path, rel: str) -> list[dict[str, Any]]:
    """Entries of a directory: directories first, then files, by name."""
    out: list[dict[str, Any]] = []
    try:
        with _walk(root, split_path(rel)) as fd, os.scandir(fd) as entries:
            for entry in entries:
                if entry.name in _HIDDEN:
                    continue
                if len(out) >= MAX_LIST_ENTRIES:
                    break
                info = entry.stat(follow_symlinks=False)
                out.append({"name": entry.name, "type": _kind(info.st_mode),
                            "size": info.st_size})  # fmt: skip
    except OSError as exc:
        raise _os_error(exc) from None
    out.sort(key=lambda e: (e["type"] != "dir", str(e["name"]).lower()))
    return out


def read_file(root: Path, rel: str) -> dict[str, Any]:
    """A text file for the editor; binary or large files come without content."""
    parts = split_path(rel)
    if not parts:
        raise InvalidError("is a directory")
    try:
        with _walk(root, parts[:-1]) as parent:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
            try:
                size = _regular(fd).st_size
                data = b"" if size > MAX_READ_BYTES else _read_all(fd)
            finally:
                os.close(fd)
    except OSError as exc:
        raise _os_error(exc) from None
    small = size <= MAX_READ_BYTES
    text = _text(data) if small else None
    return {"path": "/".join(parts), "size": size, "content": text,
            "binary": small and text is None, "too_large": not small,
            "version": _version(data) if small else None}  # fmt: skip


def _regular(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise InvalidError("not a regular file")
    return info


def _text(data: bytes) -> str | None:
    """UTF-8 text, or ``None`` for binary content."""
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_all(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def write_file(
    root: Path, rel: str, content: str, *, expected: str | None = None, create: bool = False
) -> str:
    """Write a text file (parents created); returns its new version.

    ``expected`` is the version the editor loaded: if the file changed since (in the
    terminal, by Claude Code…), nothing is written (409); ``create``: only a new file
    (409 if it exists). The write is atomic (temporary file + rename) and keeps the file's
    permission bits.
    """
    parts = split_path(rel)
    if not parts:
        raise InvalidError("a file name is required")
    data = content.encode("utf-8")
    try:
        with _walk(root, parts[:-1], create=True) as parent:
            mode = _current_mode(parent, parts[-1], expected, create=create)
            _replace(parent, parts[-1], data, mode)
    except OSError as exc:
        raise _os_error(exc) from None
    return _version(data)


def _current_mode(parent: int, name: str, expected: str | None, *, create: bool) -> int:
    """Permission bits of the file to replace (0o644 for a new one), after checking it
    is still the version the editor loaded."""
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if expected is not None:
            raise ConflictError("the file was deleted meanwhile") from None
        return 0o644
    if create:
        os.close(fd)
        raise ConflictError("the file already exists")
    try:
        mode = stat.S_IMODE(_regular(fd).st_mode)
        current = _version(_read_all(fd)) if expected is not None else None
    finally:
        os.close(fd)
    if current != expected:
        raise ConflictError("the file changed since it was opened")
    return mode


def _replace(parent: int, name: str, data: bytes, mode: int) -> None:
    """Atomic replacement: a temporary file in the same directory, then a rename."""
    temp = f".{name}.ai-coding-{os.getpid()}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)
    try:
        os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
    except OSError:
        os.unlink(temp, dir_fd=parent)
        raise


def delete_file(root: Path, rel: str) -> None:
    """Delete a file (or a link itself); directories are refused."""
    parts = split_path(rel)
    if not parts:
        raise InvalidError("is a directory")
    try:
        with _walk(root, parts[:-1]) as parent:
            info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                raise InvalidError("directories are deleted in the terminal")
            os.unlink(parts[-1], dir_fd=parent)
    except OSError as exc:
        raise _os_error(exc) from None
