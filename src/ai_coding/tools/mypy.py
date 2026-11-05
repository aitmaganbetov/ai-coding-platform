"""mypy adapter (Python types): installed into ``/deps`` (pinned version), run offline
against the project's own configuration (ADR-0027)."""

from __future__ import annotations

import re
from pathlib import Path

from ai_coding.domain.enums import Severity, Stack
from ai_coding.tools.base import Finding, ToolCategory, ToolSpec, fingerprint, relative_path
from ai_coding.tools.project import WORK, Install, Invocation
from ai_coding.tools.testrun import PYPI_HOSTS, PYTHON_IMAGE

MYPY_VERSION = "1.15.0"
_LINE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):(?:\d+:)? (?P<level>error|note|warning): "
                   r"(?P<msg>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$")  # fmt: skip


def _configured(root: Path) -> bool:
    if (root / "mypy.ini").is_file() or (root / ".mypy.ini").is_file():
        return True
    for name, marker in (("pyproject.toml", "[tool.mypy]"), ("setup.cfg", "[mypy]")):
        try:
            if marker in (root / name).read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


class MypyAdapter:
    """``python -m mypy`` with the project's configuration."""

    spec = ToolSpec(name="mypy", category=ToolCategory.TYPES, binary="python",
                    image=PYTHON_IMAGE, stacks=frozenset({Stack.PYTHON}),
                    executes_project_code=True, homepage="https://mypy-lang.org/")  # fmt: skip

    def plan(self, root: Path) -> Invocation | str:
        if not _configured(root):
            return "mypy is not configured for the project (mypy.ini / [tool.mypy])"
        install = ["python", "-m", "pip", "install", "--quiet", "--no-cache-dir",
                   "--disable-pip-version-check", "--target", "/deps",
                   f"mypy=={MYPY_VERSION}"]  # fmt: skip
        for req in sorted(root.glob("requirements*.txt")):
            install += ["-r", req.name]
        return Invocation(
            argv=("python", "-m", "mypy", "--show-error-codes", "--no-error-summary",
                  "--no-color-output", "--no-pretty", "--cache-dir", "/deps/.mypy_cache"),
            env={"PYTHONPATH": "/deps", "PYTHONDONTWRITEBYTECODE": "1"},
            install=Install(argv=tuple(install), hosts=PYPI_HOSTS, writes_project=False),
            version_argv=("python", "-m", "mypy", "--version"),
        )  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 1})  # 1: type errors; 2: usage/crash

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"mypy (\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        findings = []
        for line in stdout.splitlines():
            m = _LINE.match(line.strip())
            if not m or m["level"] == "note":
                continue
            path = relative_path(m["file"], WORK)
            code = m["code"] or "mypy"
            message = m["msg"]
            findings.append(Finding(
                tool="mypy", rule_id=code, severity=Severity.MEDIUM,
                title=f"{code}: {message}"[:200], message=message, path=path,
                line=int(m["line"]), fingerprint=fingerprint("mypy", code, path, message),
            ))  # fmt: skip
        if exit_code != 0 and not findings:
            raise ValueError(f"mypy failed without diagnostics: {(stdout or stderr)[-200:]}")
        return findings
