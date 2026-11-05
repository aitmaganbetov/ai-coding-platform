"""PHP project tools: PHPStan (types) and Laravel Pint (style), from the project's
``vendor/`` in the Composer image (ADR-0027). Read-only: Pint runs with ``--test``."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ai_coding.domain.enums import Severity, Stack
from ai_coding.tools.base import Finding, ToolCategory, ToolSpec, fingerprint, relative_path
from ai_coding.tools.project import WORK, Install, Invocation

COMPOSER_IMAGE = (  # composer 2.8 (PHP CLI + Composer; Docker Hub via mirror.gcr.io)
    "mirror.gcr.io/library/composer@sha256:5248900ab8b5f7f880c2d62180e40960cd87f60149ec9a1abfd62ac72a02577c"
)
COMPOSER_HOSTS = ("repo.packagist.org", "packagist.org", "api.github.com",
                  "codeload.github.com", "objects.githubusercontent.com")  # fmt: skip
_ENV = {"COMPOSER_HOME": "/deps/composer", "COMPOSER_CACHE_DIR": "/deps/composer-cache",
        "COMPOSER_NO_INTERACTION": "1"}  # fmt: skip
_PHP_STACKS = frozenset({Stack.PHP, Stack.LARAVEL})


def _dev_dependency(root: Path, package: str) -> bool:
    try:
        data: dict[str, Any] = json.loads((root / "composer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return package in (data.get("require-dev") or {}) or package in (data.get("require") or {})


def _install() -> Install:
    return Install(
        argv=("composer", "install", "--no-interaction", "--no-progress", "--no-scripts",
              "--no-plugins", "--prefer-dist"),
        hosts=COMPOSER_HOSTS,
    )  # fmt: skip


def _vendor_plan(root: Path, binary: str, packages: tuple[str, ...]) -> Install | str | None:
    """``None`` when vendor/ already has the tool; an install, or why it does not apply."""
    if not (root / "composer.json").is_file():
        return "no composer.json"
    if (root / "vendor" / "bin" / binary).is_file():
        return None
    if any(_dev_dependency(root, p) for p in packages):
        return _install()
    return f"{packages[0]} is not a dependency of the project"


class PHPStanAdapter:
    """``vendor/bin/phpstan analyse --error-format=json``."""

    spec = ToolSpec(name="phpstan", category=ToolCategory.TYPES, binary="php",
                    image=COMPOSER_IMAGE, stacks=_PHP_STACKS, executes_project_code=True,
                    homepage="https://phpstan.org/")  # fmt: skip

    def plan(self, root: Path) -> Invocation | str:
        install = _vendor_plan(root, "phpstan", ("phpstan/phpstan", "larastan/larastan",
                                                 "nunomaduro/larastan"))  # fmt: skip
        if isinstance(install, str):
            return install
        argv = ["php", "vendor/bin/phpstan", "analyse", "--error-format=json", "--no-progress",
                "--no-interaction", "--memory-limit=1G"]  # fmt: skip
        if not any((root / n).is_file() for n in ("phpstan.neon", "phpstan.neon.dist",
                                                  "phpstan.dist.neon")):  # fmt: skip
            paths = [d for d in ("app", "src") if (root / d).is_dir()]
            if not paths:
                return "no phpstan configuration and no app/ or src/ directory"
            argv += ["--level=5", *paths]
        return Invocation(argv=tuple(argv), env=_ENV, install=install,
                          version_argv=("php", "vendor/bin/phpstan", "--version"))  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 1})  # 1: errors found

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        data = json.loads(stdout)
        findings: list[Finding] = []
        for file, info in (data.get("files") or {}).items():
            path = relative_path(file, WORK)
            for m in info.get("messages") or []:
                rule = str(m.get("identifier") or "phpstan")
                message = str(m.get("message", ""))
                findings.append(Finding(
                    tool="phpstan", rule_id=rule, severity=Severity.MEDIUM,
                    title=f"{rule}: {message}"[:200], message=message, path=path,
                    line=int(m["line"]) if m.get("line") else None,
                    fingerprint=fingerprint("phpstan", rule, path, message),
                    evidence={"tip": m.get("tip")},
                ))  # fmt: skip
        findings.extend(  # configuration / internal errors
            Finding(
                tool="phpstan", rule_id="phpstan.error", severity=Severity.HIGH,
                title=str(error)[:200], message=str(error), path=None, line=None,
                fingerprint=fingerprint("phpstan", "phpstan.error", None, str(error)),
            )
            for error in data.get("errors") or []
        )  # fmt: skip
        return findings


class PintAdapter:
    """``vendor/bin/pint --test --format=json`` (never fixes)."""

    spec = ToolSpec(name="pint", category=ToolCategory.LINT, binary="php", image=COMPOSER_IMAGE,
                    stacks=_PHP_STACKS, executes_project_code=True,
                    homepage="https://laravel.com/docs/pint")  # fmt: skip

    def plan(self, root: Path) -> Invocation | str:
        install = _vendor_plan(root, "pint", ("laravel/pint",))
        if isinstance(install, str):
            return install
        return Invocation(argv=("php", "vendor/bin/pint", "--test", "--format=json"), env=_ENV,
                          install=install,
                          version_argv=("php", "vendor/bin/pint", "--version"))  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 1})  # 1: style violations

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        data = json.loads(stdout)
        findings = []
        for f in data.get("files") or []:
            path = relative_path(str(f.get("name", "")), WORK)
            fixers = [str(x) for x in f.get("appliedFixers") or []]
            message = "code style differs from the project's Pint rules" + (
                f" ({', '.join(fixers)})" if fixers else ""
            )
            findings.append(Finding(
                tool="pint", rule_id="pint.style", severity=Severity.LOW,
                title=f"style: {path}"[:200], message=message, path=path, line=None,
                fingerprint=fingerprint("pint", "pint.style", path, ",".join(sorted(fixers))),
                evidence={"fixers": fixers},
            ))  # fmt: skip
        return findings
