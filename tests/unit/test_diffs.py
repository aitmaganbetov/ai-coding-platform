"""Parsing of workspace diffs for the AI Changes Review (ADR-0040) and the checks of what
the browser sends back."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_coding.api.diffs import (
    DIFF_ARGS,
    PATCH_OPS,
    PATH_OPS,
    UNTRACKED_SCRIPT,
    parse_diff,
    patch_path,
    review_payload,
    validate_patch,
    validate_path,
)

SAMPLE = """diff --git a/app/shop.py b/app/shop.py
index 1111111..2222222 100644
--- a/app/shop.py
+++ b/app/shop.py
@@ -1,3 +1,4 @@ import os
 import subprocess
-x = 1
+x = 2
+y = 3
 z = 4
@@ -10 +11 @@ def f():
-a
+b
\\ No newline at end of file
diff --git a/new.txt b/new.txt
new file mode 100644
index 0000000..ce01362
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+hello
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index ce01362..0000000
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
diff --git a/old name.txt b/new name.txt
similarity index 90%
rename from old name.txt
rename to new name.txt
diff --git a/img.png b/img.png
index 1111111..2222222 100644
Binary files a/img.png and b/img.png differ
diff --git "a/\\320\\274\\320\\270\\321\\200.txt" "b/\\320\\274\\320\\270\\321\\200.txt"
index 1111111..2222222 100644
--- "a/\\320\\274\\320\\270\\321\\200.txt"
+++ "b/\\320\\274\\320\\270\\321\\200.txt"
@@ -1 +1 @@
-a
+b
"""


def test_parse_files_and_hunks() -> None:
    files = {f["path"]: f for f in parse_diff(SAMPLE)}
    shop = files["app/shop.py"]
    assert shop["status"] == "modified" and (shop["additions"], shop["deletions"]) == (3, 2)
    first, second = shop["hunks"]
    assert (first["old_start"], first["old_lines"], first["new_start"], first["new_lines"]) == (
        1,
        3,
        1,
        4,
    )
    assert first["section"] == "import os"
    assert [line["kind"] for line in first["lines"]] == ["context", "del", "add", "add", "context"]
    assert (second["old_lines"], second["new_lines"]) == (1, 1)  # "@@ -10 +11 @@": one line each
    assert second["lines"][-1] == {"kind": "meta", "text": " No newline at end of file"}
    # Each hunk applies alone: the file's header and that hunk only.
    assert second["patch"].startswith("diff --git a/app/shop.py b/app/shop.py\n")
    assert "+++ b/app/shop.py\n@@ -10 +11 @@" in second["patch"] and "x = 2" not in second["patch"]
    assert files["new.txt"]["status"] == "added" and files["new.txt"]["old_path"] is None
    assert files["gone.txt"]["status"] == "deleted"
    renamed = files["new name.txt"]
    assert renamed["status"] == "renamed" and renamed["old_path"] == "old name.txt"
    assert files["img.png"]["binary"] is True and files["img.png"]["hunks"] == []
    assert files["мир.txt"]["additions"] == 1  # quoted UTF-8 path


def test_payload_layers_and_limits() -> None:
    payload = review_payload(SAMPLE, "", "")
    assert len(payload["accepted"]) == 6 and payload["pending"] == [] and not payload["truncated"]
    assert review_payload("x" * (4 * 1024 * 1024 + 1), "", "")["truncated"] is True
    assert patch_path(parse_diff(SAMPLE)[0]["hunks"][0]["patch"]) == "app/shop.py"


def test_what_the_browser_sends_back_is_checked() -> None:
    hunk = parse_diff(SAMPLE)[0]["hunks"][0]["patch"]
    assert validate_patch(hunk) == hunk
    for bad in (None, "", "rm -rf /", "diff --git a/x b/x\n--- a/x\n+++ b/x\n",  # no hunk
                hunk + hunk, "x" * (1024 * 1024 + 1)):  # fmt: skip
        with pytest.raises(ValueError, match=r"patch|hunks"):
            validate_patch(bad)
    assert validate_path("docs/new file.md") == "docs/new file.md"
    for bad in (None, "", "/etc/passwd", "../x", "a/../../x", "-rf", ".git/config", "a//b", "a\nb"):
        with pytest.raises(ValueError, match="path"):
            validate_path(bad)


def test_the_review_commands_on_a_real_repository(tmp_path: Path, git_env: None) -> None:
    """What the broker runs in the container, here on a local repository: accept stages a
    hunk, reject reverts one in the working tree, new files come as 'new file' diffs."""

    def git(*args: str, data: str | None = None) -> str:
        done = subprocess.run(["git", *args], cwd=tmp_path, input=data, capture_output=True,
                              text=True, check=True)  # fmt: skip
        return done.stdout

    git("init", "-q", "-b", "main")
    lines = [f"line {i}\n" for i in range(1, 21)]
    (tmp_path / "a.txt").write_text("".join(lines), encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    lines[1], lines[17] = "LINE 2\n", "LINE 18\n"  # two hunks
    (tmp_path / "a.txt").write_text("".join(lines), encoding="utf-8")
    (tmp_path / "new.md").write_text("# new\n", encoding="utf-8")

    untracked = subprocess.run(["sh", "-c", UNTRACKED_SCRIPT], cwd=tmp_path, capture_output=True,
                               text=True, check=True).stdout  # fmt: skip
    payload = review_payload(git(*DIFF_ARGS, "--cached"), git(*DIFF_ARGS), untracked)
    (pending,) = payload["pending"]
    assert [f["path"] for f in payload["new_files"]] == ["new.md"]
    first, second = pending["hunks"]
    git(*PATCH_OPS["accept"], data=first["patch"])
    git(*PATCH_OPS["reject"], data=second["patch"])
    after = review_payload(git(*DIFF_ARGS, "--cached"), git(*DIFF_ARGS), "")
    assert [len(f["hunks"]) for f in after["accepted"]] == [1] and after["pending"] == []
    text = (tmp_path / "a.txt").read_text(encoding="utf-8")
    assert "LINE 2\n" in text and "line 18\n" in text  # accepted kept, rejected reverted
    git(*PATCH_OPS["unstage"], data=after["accepted"][0]["hunks"][0]["patch"])
    assert git(*DIFF_ARGS, "--cached") == "" and "LINE 2" in git(*DIFF_ARGS)

    # A binary file has no hunks: it is accepted, sent back and rejected as a whole.
    (tmp_path / "logo.bin").write_bytes(b"\x00\x01v1")
    git("add", "logo.bin")
    git("commit", "-q", "-m", "binary")
    (tmp_path / "logo.bin").write_bytes(b"\x00\x02v2")
    (binary,) = [f for f in parse_diff(git(*DIFF_ARGS)) if f["path"] == "logo.bin"]
    assert binary["binary"] is True and binary["hunks"] == []
    git(*PATH_OPS["accept_path"], "logo.bin")
    assert "logo.bin" in git(*DIFF_ARGS, "--cached")
    git(*PATH_OPS["unstage_path"], "logo.bin")
    assert "logo.bin" not in git(*DIFF_ARGS, "--cached") and "logo.bin" in git(*DIFF_ARGS)
    git(*PATH_OPS["reject_path"], "logo.bin")
    assert (tmp_path / "logo.bin").read_bytes() == b"\x00\x01v1"
