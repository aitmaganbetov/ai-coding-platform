"""End-to-end and load tests of the user's project (S9, B-603): Playwright and k6.

Both run the project's own tests in the Docker sandbox (ADR-0027):

* **Playwright** — ``playwright test --reporter=json`` in the digest-pinned Playwright
  image, whose browsers match one Playwright version; a project on another version is
  SKIPPED with the reason (installing browsers would need the network). Dependencies
  come from ``node_modules`` or are installed through the egress allowlist. Tests
  start the application themselves (``webServer``) inside the sandbox; no network.
* **k6** — the project's k6 scripts against the system under test, which is reachable
  only through the egress allowlist proxy and only for
  ``quality_gates.tests.performance_hosts`` (also subject to ``security``). A crossed
  threshold is a finding; a script that fails to run is a tool failure (fail closed).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ai_coding.config.loader import find_project_config
from ai_coding.config.validator import validate_file
from ai_coding.domain.enums import Severity, Stack
from ai_coding.tools.base import Finding, ToolCategory, ToolSpec, fingerprint, relative_path
from ai_coding.tools.node import _manifest, _node_plan
from ai_coding.tools.project import WORK, Invocation

PLAYWRIGHT_VERSION = "1.56.1"
PLAYWRIGHT_IMAGE = (
    "mcr.microsoft.com/playwright"
    "@sha256:f1e7e01021efd65dd1a2c56064be399f3e4de00fd021ac561325f2bfbb2b837a"
)  # v1.56.1-noble: Node.js and the browsers of exactly this Playwright version
K6_IMAGE = (
    "mirror.gcr.io/grafana/k6"
    "@sha256:3ddc8b1a33a2c3d8edc6e99b6a762ae36cba08788463458f5e6a7703e14eb77d"
)  # v1.3.0
_PW_CONFIGS = ("playwright.config.ts", "playwright.config.js", "playwright.config.mjs",
               "playwright.config.cjs", "playwright.config.mts")  # fmt: skip
_PW_CLI = "node_modules/@playwright/test/cli.js"
_FAILED = frozenset({"failed", "timedOut", "interrupted"})
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SKIP_DIRS = frozenset({"node_modules", ".git", ".ai-coding", "vendor", "dist", "build"})


def _playwright_version(root: Path) -> str | None:
    """The installed or locked version of ``@playwright/test``."""
    installed = root / "node_modules" / "@playwright" / "test" / "package.json"
    try:
        return str(json.loads(installed.read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        pass
    try:
        lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
        return str(lock["packages"]["node_modules/@playwright/test"]["version"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    manifest = _manifest(root)
    declared = {**(manifest.get("dependencies") or {}),
                **(manifest.get("devDependencies") or {})}.get("@playwright/test")  # fmt: skip
    return str(declared) if declared and re.fullmatch(r"\d+\.\d+\.\d+", str(declared)) else None


class PlaywrightAdapter:
    """``playwright test --reporter=json`` with the project's configuration."""

    spec = ToolSpec(name="playwright", category=ToolCategory.TESTS, binary="node",
                    image=PLAYWRIGHT_IMAGE,
                    stacks=frozenset({Stack.JAVASCRIPT, Stack.TYPESCRIPT, Stack.REACT}),
                    executes_project_code=True, homepage="https://playwright.dev/",
                    env=(("CI", "1"), ("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"),
                         ("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")))  # fmt: skip

    def plan(self, root: Path) -> Invocation | str:
        if not any((root / c).is_file() for c in _PW_CONFIGS):
            return "no Playwright configuration"
        if not _enabled(root, "e2e"):
            return "disabled by quality_gates.tests.e2e"
        version = _playwright_version(root)
        if version is None:
            return "the @playwright/test version is not pinned (lock file or exact version)"
        if version != PLAYWRIGHT_VERSION:
            return (f"the project uses @playwright/test {version}; the sandbox image has the "
                    f"browsers of {PLAYWRIGHT_VERSION}")  # fmt: skip
        install = _node_plan(root, "@playwright/test", "@playwright/test/cli.js")
        if isinstance(install, str):
            return install
        return Invocation(argv=("node", _PW_CLI, "test", "--reporter=json"), install=install,
                          version_argv=("node", _PW_CLI, "--version"),
                          writable=True)  # test-results/ is written into the tree  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0, 1})  # 1: some tests failed

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        report = json.loads(stdout[stdout.find("{") :])
        findings: list[Finding] = []
        for suite in report.get("suites") or []:
            _collect(suite, [], findings)
        stats = report.get("stats") or {}
        if exit_code != 0 and not findings and not stats.get("unexpected"):
            errors = "; ".join(_clean(str(e.get("message", "")))[:200]
                               for e in report.get("errors") or [])  # fmt: skip
            raise ValueError(f"playwright failed without test results: {errors or stderr[-200:]}")
        return findings


def _collect(suite: dict[str, Any], titles: list[str], out: list[Finding]) -> None:
    here = [*titles, str(suite.get("title", ""))] if suite.get("title") else titles
    for spec in suite.get("specs") or []:
        for test in spec.get("tests") or []:
            results = test.get("results") or []
            if not results or results[-1].get("status") not in _FAILED:
                continue  # passed, skipped, or passed on retry (flaky)
            last = results[-1]
            error = last.get("error") or {}
            location = error.get("location") or {}
            path = relative_path(str(location.get("file") or ""), WORK) or str(
                spec.get("file") or suite.get("file") or ""
            )
            line = int(location.get("line") or spec.get("line") or 0) or None
            name = " › ".join([*here[1:], str(spec.get("title", ""))]) or "test"
            message = _clean(str(error.get("message") or last.get("status")))
            out.append(Finding(
                tool="playwright", rule_id=f"e2e-{last.get('status')}", severity=Severity.HIGH,
                title=f"E2E test failed: {name}"[:200], message=message[:2000], path=path,
                line=line, fingerprint=fingerprint("playwright", name, path, ""),
                evidence={"project": test.get("projectName"), "status": last.get("status"),
                          "retries": len(results) - 1},
            ))  # fmt: skip
    for child in suite.get("suites") or []:
        _collect(child, here, out)


def _clean(text: str) -> str:
    return _ANSI.sub("", text).strip()


def _tests_gate(root: Path) -> Any:
    """``quality_gates.tests`` of the project (defaults when absent or invalid)."""
    from ai_coding.config.models import TestsGate  # noqa: PLC0415

    path = find_project_config(root)
    report = validate_file(path) if path else None
    if report is None or report.config is None:
        return TestsGate()
    return report.config.quality_gates.tests


def _enabled(root: Path, field: str) -> bool:
    return bool(getattr(_tests_gate(root), field))


def find_k6_scripts(root: Path) -> list[str]:
    """``*.k6.js`` anywhere and ``k6/*.js``, project-relative (sorted)."""
    found: set[str] = set()
    for path in root.rglob("*.js"):
        rel = path.relative_to(root)
        if _SKIP_DIRS & set(rel.parts) or not path.is_file():
            continue
        if path.name.endswith(".k6.js") or (len(rel.parts) == 2 and rel.parts[0] == "k6"):
            found.add(rel.as_posix())
    return sorted(found)


_K6_LOOP = (
    'for s in "$@"; do k6 run --quiet --no-usage-report '
    '--summary-export /tmp/k6.json "$s" >/dev/null 2>/tmp/k6.err; code=$?; '
    'printf \'{"script":"%s","exit":%d,"summary":\' "$s" "$code"; '
    "if [ -s /tmp/k6.json ]; then cat /tmp/k6.json; else printf null; fi; "
    'printf \',"error":"%s"}\\n\' "$(tail -c 300 /tmp/k6.err | tr -d \'"\\\\\\n\\r\\t\')"; '
    "rm -f /tmp/k6.json /tmp/k6.err; done"
)


class K6Adapter:
    """The project's k6 scripts against the system under test, through the egress proxy."""

    spec = ToolSpec(name="k6", category=ToolCategory.PERFORMANCE, binary="k6", image=K6_IMAGE,
                    executes_project_code=True, homepage="https://k6.io/",
                    env=(("K6_NO_USAGE_REPORT", "true"),))  # fmt: skip

    def plan(self, root: Path) -> Invocation | str:
        gate = _tests_gate(root)
        scripts = list(gate.performance_scripts) or find_k6_scripts(root)
        if not scripts:
            return "no k6 scripts (*.k6.js, k6/*.js or quality_gates.tests.performance_scripts)"
        missing = [s for s in scripts if not (root / s).is_file()]
        if missing:
            return f"k6 script(s) not found: {', '.join(missing)}"
        hosts = tuple(gate.performance_hosts)
        if not hosts:
            return ("k6 needs the system under test: set "
                    "quality_gates.tests.performance_hosts (egress allowlist)")  # fmt: skip
        # The hosts are named by the operator: an internal staging system is allowed.
        return Invocation(argv=("sh", "-c", _K6_LOOP, "sh", *scripts),
                          version_argv=("k6", "version"), egress=hosts,
                          egress_private=True)  # fmt: skip

    def ok_exit_codes(self) -> frozenset[int]:
        return frozenset({0})

    def parse_version(self, output: str) -> str | None:
        match = re.search(r"v(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None

    def parse(self, stdout: str, stderr: str, exit_code: int) -> list[Finding]:
        findings = []
        entries = _json_objects(stdout)  # one object per script; summaries span lines
        if not entries:
            raise ValueError("k6 produced no results")
        for entry in entries:
            script, summary = str(entry["script"]), entry.get("summary")
            if not isinstance(summary, dict):  # the script did not run (syntax, import …)
                raise ValueError(  # noqa: TRY004 - the script did not run: a tool failure
                    f"k6 could not run {script} (exit {entry.get('exit')}): "
                    f"{entry.get('error', '')}"
                )
            for metric, data in sorted((summary.get("metrics") or {}).items()):
                for expression, crossed in sorted((data.get("thresholds") or {}).items()):
                    if not crossed:  # k6 marks a crossed threshold with true
                        continue
                    values = {k: v for k, v in data.items() if k != "thresholds"}
                    findings.append(Finding(
                        tool="k6", rule_id=f"threshold:{metric}", severity=Severity.MEDIUM,
                        title=f"k6 threshold crossed: {metric} {expression} ({script})"[:200],
                        message=f"{metric} {expression} not met: "
                                + ", ".join(f"{k}={v}" for k, v in values.items()),
                        path=script, line=None,
                        fingerprint=fingerprint("k6", f"{metric}:{expression}", script, ""),
                        evidence={"metric": metric, "threshold": expression, **values},
                    ))  # fmt: skip
        return findings


def _json_objects(text: str) -> list[dict[str, Any]]:
    """Consecutive JSON objects in ``text`` (whitespace between them)."""
    decoder, out, index = json.JSONDecoder(), [], 0
    while (start := text.find("{", index)) != -1:
        value, index = decoder.raw_decode(text, start)
        if isinstance(value, dict):
            out.append(value)
    return out
