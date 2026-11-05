"""Node.js project tools: ESLint and the TypeScript compiler from ``node_modules``
(ADR-0027). Read-only: no ``--fix``, ``tsc --noEmit`` without incremental output."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ai_coding.domain.enums import Severity, Stack
from ai_coding.tools.base import Finding, ToolCategory, ToolSpec, fingerprint, relative_path
from ai_coding.tools.project import WORK, Install, Invocation
from ai_coding.tools.testrun import NODE_IMAGE, NPM_HOSTS

_JS_STACKS = frozenset({Stack.JAVASCRIPT, Stack.TYPESCRIPT, Stack.REACT})
_ESLINT_CONFIGS = ("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
                   "eslint.config.ts", ".eslintrc", ".eslintrc.js", ".eslintrc.cjs",
                   ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml")  # fmt: skip
_TSC_LINE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\): (?P<level>error|warning) "
                       r"(?P<code>TS\d+): (?P<msg>.*)$")  # fmt: skip


def _manifest(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _node_plan(root: Path, package: str, entry: str) -> Install | str | None:
    if not (root / "package.json").is_file():
        return "no package.json"
    if (root / "node_modules" / entry).is_file():
        return None
    manifest = _manifest(root)
    if package not in {**(manifest.get("dependencies") or {}),
                       **(manifest.get("devDependencies") or {})}:  # fmt: skip
        return f"{package} is not a dependency of the project"
    verb = "ci" if (root / "package-lock.json").is_file() else "install"
    return Install(argv=("npm", verb, "--ignore-scripts", "--no-audit", "--no-fund"),
                   hosts=NPM_HOSTS)  # fmt: skip


class ESLintAdapter:
    """``eslint --format json .`` with the project's configuration."""

    spec = ToolSpec(name="eslint", category=ToolCategory.LINT, binary="node", image=NODE_IMAGE,
                    stacks=_JS_STACKS, executes_project_code=True,
                    homepage="https://eslint.org/")  # fmt: skip

    def plan(self, root: Path) -> Invocation | str:
        manifest = _manifest(root)
        if (
            not any((root / c).is_file() for c in _ESLINT_CONFIGS)
            and "eslintConfig" not in manifest
        ):
            return "no ESLint configuration"
        install = _node_plan(root, "eslint", "eslint/bin/eslint.js")
        if isinstance(install, str):
            return install
        return Invocation(argv=("node", "node_modules/eslint/bin/eslint.js", "--format", "json",
                                "."), install=install,
                          version_argv=("node", "node_modules/eslint/bin/eslint.js",
                                        "--version"))  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 1})  # 1: lint errors; 2: crash/config error

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        data = json.loads(stdout)
        if not isinstance(data, list):
            raise TypeError("eslint output is not a JSON array")
        findings = []
        for file in data:
            path = relative_path(str(file.get("filePath", "")), WORK)
            for m in file.get("messages") or []:
                rule = str(m.get("ruleId") or ("parse-error" if m.get("fatal") else "eslint"))
                message = str(m.get("message", ""))
                severity = Severity.HIGH if m.get("fatal") else (
                    Severity.MEDIUM if m.get("severity") == 2 else Severity.LOW)  # fmt: skip
                findings.append(Finding(
                    tool="eslint", rule_id=rule, severity=severity,
                    title=f"{rule}: {message}"[:200], message=message, path=path,
                    line=int(m["line"]) if m.get("line") else None,
                    fingerprint=fingerprint("eslint", rule, path, message),
                ))  # fmt: skip
        return findings


class TypeScriptAdapter:
    """``tsc --noEmit`` with the project's ``tsconfig.json``."""

    spec = ToolSpec(name="tsc", category=ToolCategory.TYPES, binary="node", image=NODE_IMAGE,
                    stacks=frozenset({Stack.TYPESCRIPT, Stack.REACT}), executes_project_code=True,
                    homepage="https://www.typescriptlang.org/")  # fmt: skip

    def plan(self, root: Path) -> Invocation | str:
        if not (root / "tsconfig.json").is_file():
            return "no tsconfig.json"
        install = _node_plan(root, "typescript", "typescript/bin/tsc")
        if isinstance(install, str):
            return install
        return Invocation(argv=("node", "node_modules/typescript/bin/tsc", "--noEmit",
                                "--pretty", "false", "--incremental", "false"),
                          install=install,
                          version_argv=("node", "node_modules/typescript/bin/tsc",
                                        "--version"))  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 1, 2})  # 1/2: diagnostics reported

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        findings = []
        for line in stdout.splitlines():
            m = _TSC_LINE.match(line.strip())
            if not m:
                continue
            path = relative_path(m["file"], WORK)
            message = m["msg"]
            findings.append(Finding(
                tool="tsc", rule_id=m["code"],
                severity=Severity.MEDIUM if m["level"] == "error" else Severity.LOW,
                title=f"{m['code']}: {message}"[:200], message=message, path=path,
                line=int(m["line"]), fingerprint=fingerprint("tsc", m["code"], path, message),
            ))  # fmt: skip
        if exit_code != 0 and not findings:
            raise ValueError(f"tsc failed without diagnostics: {(stdout or stderr)[-200:]}")
        return findings
