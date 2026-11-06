"""Changes of a workspace for review (AI Changes Review, ADR-0040).

Git of a workspace runs in the user's container only (ADR-0036): the broker runs the
commands below there and the API parses their output. The review works on two layers:

* *pending* — the working tree against the index (``git diff``) plus untracked files:
  what the agent (or the user) changed and nobody has reviewed yet;
* *accepted* — the index against HEAD (``git diff --cached``): what the user accepted.

Accepting a hunk stages it (``git apply --cached``); rejecting one reverts it in the
working tree (``git apply -R``); "back to review" unstages it. New files are accepted
(``git add``) or rejected (``git clean`` — untracked files only) as a whole. "Commit
accepted" commits the index alone.
"""

from __future__ import annotations

import re
from typing import Any

# `git diff` that only reads: no colours, no external diff programs, no text conversion
# filters of the repository's configuration, paths unquoted where possible.
DIFF_ARGS = (
    "-c",
    "core.quotepath=off",
    "diff",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "-M",
)
# Untracked files as "new file" diffs (at most 200 files; binaries say so).
UNTRACKED_SCRIPT = (
    "git ls-files --others --exclude-standard -z | tr '\\0' '\\n' | head -n 200 | tr '\\n' '\\0' | "
    "xargs -0 -r -n1 git -c core.quotepath=off diff --no-index --no-color --no-ext-diff "
    "--no-textconv -- /dev/null; exit 0"
)
DIFF_LIMIT = 4 * 1024 * 1024  # per layer
PATCH_LIMIT = 1024 * 1024

# op → git arguments; a patch op reads the patch from stdin, a path op takes the path.
PATCH_OPS: dict[str, tuple[str, ...]] = {
    "accept": ("apply", "--cached", "--recount", "--whitespace=nowarn", "-"),
    "reject": ("apply", "-R", "--recount", "--whitespace=nowarn", "-"),
    "unstage": ("apply", "--cached", "-R", "--recount", "--whitespace=nowarn", "-"),
}
PATH_OPS: dict[str, tuple[str, ...]] = {
    "accept_file": ("add", "--"),
    "reject_file": ("clean", "--force", "--quiet", "--"),  # removes untracked files only
}
REVIEW_OPS = (*PATCH_OPS, *PATH_OPS)

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def validate_patch(patch: str | None) -> str:
    """A one-file patch from a diff of this module (the browser sends hunks back)."""
    if not patch or len(patch.encode()) > PATCH_LIMIT:
        raise ValueError("a patch of at most 1 MB is required")
    if not patch.startswith("diff --git ") or patch.count("\ndiff --git ") or "\n@@ " not in patch:
        raise ValueError("expected the hunks of one file, as the diff shows them")
    return patch if patch.endswith("\n") else patch + "\n"


def validate_path(path: str | None) -> str:
    """A path inside the workspace, relative, without ``..`` (git refuses others anyway)."""
    if not path or len(path) > 4096 or "\x00" in path or "\n" in path:
        raise ValueError("a file path is required")
    parts = path.split("/")
    if (
        path.startswith(("/", "-"))
        or any(p in ("", ".", "..") for p in parts)
        or parts[0] == ".git"
    ):
        raise ValueError("the path must be a file inside the workspace")
    return path


def _unquote(name: str) -> str:
    """Git quotes unusual paths in C style ("a\\tb", "\\320\\270" = UTF-8 bytes)."""
    if not (len(name) >= 2 and name[0] == name[-1] == '"'):
        return name
    out, body, i = bytearray(), name[1:-1], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            octal = body[i + 1 : i + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                out.append(int(octal, 8))
                i += 4
            else:
                out += _ESCAPES.get(body[i + 1], body[i + 1]).encode()
                i += 2
        else:
            out += body[i].encode()
            i += 1
    return out.decode(errors="replace")


def _side(value: str) -> str | None:
    value = _unquote(value.strip())
    if value == "/dev/null":
        return None
    return value[2:] if value[:2] in ("a/", "b/") else value


def _new_file(line: str) -> dict[str, Any]:
    f: dict[str, Any] = {"path": "", "old_path": None, "status": "modified", "binary": False,
                         "header": [line], "hunks": [], "additions": 0, "deletions": 0}  # fmt: skip
    names = line.rstrip("\n")[len("diff --git ") :]
    half = (len(names) - 1) // 2  # "a/X b/X": the same name on both sides
    if (
        names.startswith("a/")
        and names[half : half + 3] == " b/"
        and names[2:half] == names[half + 3 :]
    ):
        f["path"] = names[half + 3 :]
    return f


def _new_hunk(line: str) -> dict[str, Any] | None:
    match = _HUNK.match(line.rstrip("\n"))
    if match is None:
        return None
    o_start, o_len, n_start, n_len, section = match.groups()
    return {"header": line.rstrip("\n"), "section": section.strip(),
            "old_start": int(o_start), "old_lines": int(1 if o_len is None else o_len),
            "new_start": int(n_start), "new_lines": int(1 if n_len is None else n_len),
            "lines": [], "raw": [line]}  # fmt: skip


def _header_line(f: dict[str, Any], bare: str) -> None:
    if bare.startswith("--- "):
        f["old_path"] = _side(bare[4:])
        if f["old_path"] is None:
            f["status"] = "added"
    elif bare.startswith("+++ "):
        new = _side(bare[4:])
        if new is None:
            f["status"] = "deleted"
            f["path"] = f["old_path"] or f["path"]
        else:
            f["path"] = new
    elif bare.startswith("new file mode"):
        f["status"] = "added"
    elif bare.startswith("deleted file mode"):
        f["status"] = "deleted"
    elif bare.startswith("rename from "):
        f["old_path"], f["status"] = _unquote(bare[len("rename from ") :]), "renamed"
    elif bare.startswith("rename to "):
        f["path"] = _unquote(bare[len("rename to ") :])
    elif bare.startswith(("Binary files ", "GIT binary patch")):
        f["binary"] = True


def parse_diff(text: str) -> list[dict[str, Any]]:
    """Unified git diff → files with their hunks; each hunk carries a patch (the file's
    header and the hunk) that ``git apply`` takes alone."""
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None

    def close_hunk() -> None:
        nonlocal hunk
        if current is not None and hunk is not None:
            hunk["patch"] = "".join(current["header"]) + "".join(hunk.pop("raw"))
            current["hunks"].append(hunk)
        hunk = None

    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\n")
        if bare.startswith("diff --git "):
            close_hunk()
            current = _new_file(line)
            files.append(current)
        elif current is None:
            continue
        elif bare.startswith("@@ "):
            close_hunk()
            hunk = _new_hunk(line)
        elif hunk is None:
            current["header"].append(line)
            _header_line(current, bare)
        else:
            hunk["raw"].append(line)
            kind = {"+": "add", "-": "del", " ": "context", "\\": "meta"}.get(bare[:1], "context")
            hunk["lines"].append({"kind": kind, "text": bare[1:]})
            if kind in ("add", "del"):
                current["additions" if kind == "add" else "deletions"] += 1
    close_hunk()
    for f in files:
        f["header"] = "".join(f["header"])
        if f["status"] == "modified" and f["old_path"] not in (None, f["path"]):
            f["status"] = "renamed"
        if f["status"] in ("modified", "added"):
            f["old_path"] = None
    return files


def patch_path(patch: str) -> str:
    """The file a one-file patch changes (for the audit trail)."""
    files = parse_diff(patch)
    return files[0]["path"] if files and files[0]["path"] else "?"


def _clip(text: str) -> str:
    return text.encode()[:DIFF_LIMIT].decode(errors="ignore")


def review_payload(staged: str, unstaged: str, untracked: str) -> dict[str, Any]:
    """The review of a workspace: accepted (index) and pending (working tree, new files)."""
    truncated = any(len(t.encode()) > DIFF_LIMIT for t in (staged, unstaged, untracked))
    return {
        "accepted": parse_diff(_clip(staged)),
        "pending": parse_diff(_clip(unstaged)),
        "new_files": parse_diff(_clip(untracked)),
        "truncated": truncated,
    }


__all__ = [
    "DIFF_ARGS",
    "PATCH_OPS",
    "PATH_OPS",
    "REVIEW_OPS",
    "UNTRACKED_SCRIPT",
    "parse_diff",
    "patch_path",
    "review_payload",
    "validate_patch",
    "validate_path",
]
