"""Go project tools: ``go test`` (tests + coverage) and golangci-lint (ADR-0027).

Modules are downloaded into ``/deps`` (networked install, policy-gated) unless the
project vendors them; tests and lint then run with no network on a scratch copy
(``go`` may update ``go.sum``; the repository itself is never written).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ai_coding.domain.enums import Severity, Stack
from ai_coding.tools.base import Finding, ToolCategory, ToolSpec, fingerprint
from ai_coding.tools.project import Install, Invocation

GO_IMAGE = (  # golang 1.24 (Docker Hub via mirror.gcr.io)
    "mirror.gcr.io/library/golang@sha256:1a6d4452c65dea36aac2e2d606b01b4a029ec90cc1ae53890540ce6173ea77ac"
)
GOLANGCI_IMAGE = (  # golangci-lint v2.1.6
    "mirror.gcr.io/golangci/golangci-lint@sha256:568ee1c1c53493575fa9494e280e579ac9ca865787bafe4df3023ae59ecf299b"
)
GO_HOSTS = ("proxy.golang.org", "sum.golang.org")
_ENV = {"GOCACHE": "/deps/cache", "GOTMPDIR": "/deps", "GOPATH": "/deps/gopath",
        "GOMODCACHE": "/deps/mod", "GOTOOLCHAIN": "local",
        "GOLANGCI_LINT_CACHE": "/deps/lint"}  # fmt: skip
_COVERAGE = re.compile(r"coverage: (\d+(?:\.\d+)?)% of statements")
_REQUIRE = re.compile(r"^\s*require\b", re.M)


def _go_plan(root: Path) -> tuple[dict[str, str], Install | None] | str:
    gomod = root / "go.mod"
    if not gomod.is_file():
        return "no go.mod"
    if (root / "vendor" / "modules.txt").is_file():
        return {**_ENV, "GOFLAGS": "-mod=vendor -buildvcs=false"}, None
    env = {**_ENV, "GOFLAGS": "-mod=mod -buildvcs=false"}
    text = gomod.read_text(encoding="utf-8", errors="replace")
    if not _REQUIRE.search(text):
        return env, None  # no dependencies: nothing to download
    return env, Install(argv=("go", "mod", "download"), hosts=GO_HOSTS, writes_project=False)


class GoTestAdapter:
    """``go test -json -cover ./...``."""

    spec = ToolSpec(name="go-test", category=ToolCategory.TESTS, binary="go", image=GO_IMAGE,
                    stacks=frozenset({Stack.GO}), executes_project_code=True,
                    homepage="https://pkg.go.dev/cmd/go#hdr-Test_packages")  # fmt: skip

    def plan(self, root: Path) -> Invocation | str:
        planned = _go_plan(root)
        if isinstance(planned, str):
            return planned
        env, install = planned
        return Invocation(argv=("go", "test", "-json", "-count=1", "-cover", "./..."), env=env,
                          install=install, version_argv=("go", "version"),
                          writable=True)  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 1})  # 1: test or build failures (reported as findings)

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"go(\d+\.\d+(?:\.\d+)?)", output)
        return match.group(1) if match else None

    def coverage(self, stdout: str, stderr: str) -> float | None:
        values = [float(v) for v in _COVERAGE.findall(stdout)]
        return round(sum(values) / len(values), 1) if values else None

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        outputs: dict[tuple[str, str], list[str]] = {}
        build: dict[str, list[str]] = {}
        findings: list[Finding] = []
        for line in stdout.splitlines():
            if not line.startswith("{"):
                continue
            ev = json.loads(line)
            action = ev.get("Action")
            if action == "build-output":
                build.setdefault(str(ev.get("ImportPath")), []).append(str(ev.get("Output", "")))
            elif action == "output":
                key = (str(ev.get("Package")), str(ev.get("Test") or ""))
                outputs.setdefault(key, []).append(str(ev.get("Output", "")))
            elif action == "fail":
                pkg, test = str(ev.get("Package")), ev.get("Test")
                if test:
                    text = "".join(outputs.get((pkg, str(test)), []))[-1500:].strip()
                    findings.append(self._finding("go-test-failure", pkg, f"{test} failed", text))
                elif ev.get("FailedBuild"):
                    text = "".join(build.get(str(ev["FailedBuild"]), []))[-1500:].strip()
                    findings.append(self._finding("go-build-failure", pkg,
                                                  "package does not build", text))  # fmt: skip
        if exit_code != 0 and not findings:
            raise ValueError(f"go test failed: {(stderr or stdout)[-300:]}")
        return findings

    @staticmethod
    def _finding(rule: str, pkg: str, title: str, text: str) -> Finding:
        return Finding(
            tool="go-test",
            rule_id=rule,
            severity=Severity.HIGH,
            title=f"{pkg}: {title}"[:200],
            message=text or title,
            path=pkg,
            line=None,
            fingerprint=fingerprint("go-test", rule, pkg, title),
        )


class GolangciLintAdapter:
    """``golangci-lint run`` (default linters or the project's ``.golangci.yml``)."""

    spec = ToolSpec(name="golangci-lint", category=ToolCategory.LINT, binary="golangci-lint",
                    image=GOLANGCI_IMAGE, stacks=frozenset({Stack.GO}),
                    executes_project_code=True,
                    homepage="https://golangci-lint.run/")  # fmt: skip

    def plan(self, root: Path) -> Invocation | str:
        planned = _go_plan(root)
        if isinstance(planned, str):
            return planned
        env, install = planned
        return Invocation(argv=("golangci-lint", "run", "--output.json.path", "stdout",
                                "--output.text.path", "stderr", "--show-stats=false", "./..."),
                          env=env, install=install, writable=True,
                          version_argv=("golangci-lint", "--version"))  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 1})  # 1: issues found

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"version v?(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        data = json.loads(stdout or "{}")
        findings = []
        for issue in data.get("Issues") or []:
            pos = issue.get("Pos") or {}
            linter, text = str(issue.get("FromLinter", "golangci")), str(issue.get("Text", ""))
            path = str(pos.get("Filename") or "") or None
            level = str(issue.get("Severity") or "").lower()
            findings.append(Finding(
                tool="golangci-lint", rule_id=linter,
                severity=Severity.LOW if level in {"warning", "info"} else Severity.MEDIUM,
                title=f"{linter}: {text}"[:200], message=text, path=path,
                line=int(pos["Line"]) if pos.get("Line") else None,
                fingerprint=fingerprint("golangci-lint", linter, path, text),
            ))  # fmt: skip
        return findings
