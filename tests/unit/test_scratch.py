"""Scratch directories: removal falls back to the sandbox user when the host cannot."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from ai_coding.executors.base import CommandSpec, ExecutorError
from ai_coding.tools import scratch as scratch_mod
from ai_coding.tools.scratch import SCRATCH, Scratch, clean_argv, remove_scratch, sandbox_scratch
from tests.unit.test_project_tools import FakeExecutor


def _denied_once(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The first host rmtree fails like on a CI runner (uid 1001 vs sandbox 1000)."""
    real, calls = shutil.rmtree, []

    def rmtree(path: Any, *args: Any, **kw: Any) -> None:
        calls.append(Path(path))
        if len(calls) == 1:
            raise PermissionError(13, "Permission denied")
        real(path, *args, **kw)

    monkeypatch.setattr(scratch_mod.shutil, "rmtree", rmtree)
    return calls


async def test_host_removal_needs_no_container(tmp_path: Path) -> None:
    ex = FakeExecutor()
    async with sandbox_scratch(ex, "t-") as s:
        (s.path / "f").write_text("x", encoding="utf-8")
        s.used("img")
    assert not s.path.exists() and ex.seen == []


async def test_fallback_cleans_as_the_sandbox_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _denied_once(monkeypatch)
    ex = FakeExecutor()
    s = Scratch(tmp_path / "s")
    s.path.mkdir()
    assert s.used("img-a") == "img-a" and s.used("img-a") == "img-a"
    assert await remove_scratch(ex, s) is True and not s.path.exists()
    (spec,) = ex.seen
    assert isinstance(spec, CommandSpec)
    assert spec.argv == clean_argv() and spec.image == "img-a" and not spec.network
    assert spec.mounts[0].source == s.path and spec.mounts[0].target == SCRATCH
    assert not spec.mounts[0].read_only and len(calls) == 2


async def test_cleanup_failure_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        scratch_mod.shutil,
        "rmtree",
        lambda p, *a, **k: (
            (_ for _ in ()).throw(PermissionError(13, "no")) if not k.get("ignore_errors") else None
        ),
    )
    s = Scratch(tmp_path / "s", ["img"])
    s.path.mkdir()
    assert await remove_scratch(FakeExecutor(raise_error=True), s) is False
    assert await remove_scratch(FakeExecutor(), Scratch(tmp_path / "missing")) is True


async def test_errors_inside_the_block_still_clean_up() -> None:
    with pytest.raises(ExecutorError):
        async with sandbox_scratch(FakeExecutor(), "t-") as s:
            raise ExecutorError("boom")
    assert not s.path.exists()
