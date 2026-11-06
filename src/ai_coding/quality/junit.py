"""Per-test results (ADR-0044): JUnit XML read safely, and the outputs of ``go test -json``
and ``cargo test`` turned into the same model, written back as JUnit.

JUnit is the common format: pytest (``--junitxml``, xunit1 for ``file``/``line``),
``node --test`` (``--test-reporter=junit``) and Vitest (``--reporter=junit``) write it;
the platform saves every report as a run artifact (``junit-*.xml``) and the API reads
them back. Paths are made relative to the project (the sandbox mounts it at ``/work``);
a failed test points at its failing line when the failure's text names one.

Reports come from project code run in the sandbox, so they are untrusted: the size is
capped, documents with a DOCTYPE or entity declarations are refused (no entity
expansion of any kind), and unknown elements and attributes are ignored.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from xml.parsers import expat

MAX_REPORT = 10 * 1024 * 1024
DETAILS_LIMIT = 4000
STATUSES = ("failed", "error", "passed", "skipped")
_LOCATION = re.compile(
    r"(?P<file>[\w./\\-]+\.(?:py|js|mjs|cjs|ts|tsx|jsx|go|rs|php|rb|java|kt|cs)):(?P<line>\d+)"
)
_SANDBOX_ROOT = "/work/"  # where the sandbox mounts the project
_FOREIGN = {"node_modules", "site-packages", "dist-packages", "deps", ".venv", "venv"}
_TEST_FILE = re.compile(r"^[\w./-]+\.(?:py|js|mjs|cjs|ts|tsx|jsx|mts|cts)$")


class JUnitError(ValueError):
    """The report is not a JUnit document the platform reads."""


@dataclass(frozen=True, slots=True)
class TestCase:
    __test__ = False  # not a pytest test class

    name: str
    classname: str = ""
    file: str | None = None
    line: int | None = None
    time: float = 0.0
    status: str = "passed"  # passed | failed | error | skipped
    message: str = ""
    details: str = ""

    @property
    def key(self) -> str:
        """Identity of a test across runs: ``classname::name``."""
        return f"{self.classname}::{self.name}" if self.classname else self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "classname": self.classname,
            "key": self.key,
            "file": self.file,
            "line": self.line,
            "time": round(self.time, 4),
            "status": self.status,
            "message": self.message[:1000],
            "details": _clip(self.details),
        }


@dataclass(frozen=True, slots=True)
class TestSuite:
    __test__ = False  # not a pytest test class

    name: str
    tool: str
    cases: tuple[TestCase, ...] = ()
    time: float = 0.0

    def count(self, status: str) -> int:
        return sum(1 for c in self.cases if c.status == status)

    def to_dict(self) -> dict[str, Any]:
        order = {s: i for i, s in enumerate(STATUSES)}
        cases = sorted(self.cases, key=lambda c: (order.get(c.status, 9), -c.time))
        return {
            "name": self.name,
            "tool": self.tool,
            "tests": len(self.cases),
            "failures": self.count("failed"),
            "errors": self.count("error"),
            "skipped": self.count("skipped"),
            "time": round(self.time or sum(c.time for c in self.cases), 3),
            "cases": [c.to_dict() for c in cases],
        }


def _clip(text: str, limit: int = DETAILS_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[:limit]}… [{len(text) - limit} more]"


def _float(value: str | None) -> float:
    try:
        return max(0.0, float((value or "0").replace(",", "")))
    except ValueError:
        return 0.0


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def project_path(path: str | None) -> str | None:
    """``path`` relative to the project root, or ``None`` when it is not a project file.

    The sandbox mounts the project at ``/work``; paths elsewhere (the interpreter's
    library, ``node_modules``, the installed dependencies under ``/deps``), escaping ones
    and ``node:`` internals are not the project's code.
    """
    if not path:
        return None
    rel = path.strip().replace("\\", "/").removeprefix("file://")
    rel = re.sub(r"^/+", "/", rel).removeprefix(_SANDBOX_ROOT)
    while rel.startswith("./"):
        rel = rel[2:]
    parts = rel.split("/")
    if (not rel or len(rel) > 500 or rel.startswith("/") or ".." in parts or ":" in rel
            or any(part in _FOREIGN for part in parts)):  # fmt: skip
        return None
    return rel


def _location(text: str, file: str | None = None) -> tuple[str | None, int | None]:
    """The first project location in a failure's text (in ``file``, when it is known)."""
    for match in _LOCATION.finditer(text):
        rel = project_path(match["file"])
        if rel and (file is None or rel == file):
            return rel, int(match["line"])
    return None, None


# --------------------------------------------------------------------- read --
class _Reader:
    """Builds suites from expat events (a JUnit document is shallow and regular)."""

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.suites: list[TestSuite] = []
        self.names: list[str] = []  # open <testsuite> names (nested suites: joined)
        self.times: list[float] = []
        self.cases: list[list[TestCase]] = [[]]
        self.case: dict[str, Any] | None = None
        self.text: list[str] | None = None
        self.depth = 0

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        self.depth += 1
        if self.depth > 64:
            raise JUnitError("the report is nested too deeply")
        if tag == "testsuite":
            self.names.append(attrs.get("name") or "")
            self.times.append(_float(attrs.get("time")))
            self.cases.append([])
        elif tag == "testcase":
            classname = attrs.get("classname") or ""
            if classname == "test" and any(self.names):  # node --test: always "test"
                classname = " › ".join(n for n in self.names if n)
            file = project_path(attrs.get("file"))
            if file is None and _TEST_FILE.match(classname):  # Vitest: the classname is the file
                file = project_path(classname)
            line = _int(attrs.get("line"))
            if line is not None and "pytest" in self.names:  # pytest's lines are 0-based
                line += 1
            self.case = {
                "name": attrs.get("name") or "(без имени)",
                "classname": classname,
                "file": file,
                "line": line if file else None,
                "time": _float(attrs.get("time")),
                "status": "passed",
                "message": "",
                "details": [],
            }
        elif self.case is not None and tag in ("failure", "error", "skipped"):
            status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
            if self.case["status"] in ("passed", "skipped"):
                self.case["status"] = status
            message = attrs.get("message") or attrs.get("type") or ""
            self.case["message"] = self.case["message"] or message
            self.text = []
        elif self.case is not None and tag in ("system-out", "system-err"):
            self.text = []

    def data(self, chunk: str) -> None:
        if self.text is not None and sum(map(len, self.text)) < DETAILS_LIMIT * 4:
            self.text.append(chunk)

    def end(self, tag: str) -> None:
        self.depth -= 1
        if (
            tag in ("failure", "error", "skipped", "system-out", "system-err")
            and self.case is not None
        ):
            if self.text and "".join(self.text).strip():
                self.case["details"].append("".join(self.text))
            self.text = None
        elif tag == "testcase" and self.case is not None:
            details = "\n".join(self.case.pop("details"))
            case = TestCase(**self.case, details=details)
            if case.status in ("failed", "error"):  # point at the failing line
                file, line = _location(details, case.file)
                if file:
                    case = replace(case, file=file, line=line)
            self.cases[-1].append(case)
            self.case = None
        elif tag == "testsuite" and self.names:
            name = " › ".join(n for n in self.names if n) or "tests"
            cases = self.cases.pop()
            if cases:
                self.suites.append(
                    TestSuite(name=name, tool=self.tool, cases=tuple(cases), time=self.times[-1])
                )
            self.names.pop()
            self.times.pop()

    def finish(self) -> list[TestSuite]:
        loose = self.cases[0]  # <testcase> outside any <testsuite>
        if loose:
            self.suites.append(TestSuite(name="tests", tool=self.tool, cases=tuple(loose)))
        return self.suites


def parse_junit(data: bytes, *, tool: str = "junit") -> list[TestSuite]:
    """Suites of a JUnit report; :class:`JUnitError` for anything else (or unsafe)."""
    if len(data) > MAX_REPORT:
        raise JUnitError(f"the report is larger than {MAX_REPORT // 1024 // 1024} MB")
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise JUnitError("reports with a DOCTYPE or entity declarations are refused")
    reader = _Reader(tool)
    parser = expat.ParserCreate()

    def refuse(*_args: object) -> None:
        raise JUnitError("reports with a DOCTYPE or entity declarations are refused")

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = lambda *_a: 0  # never fetch anything
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler = reader.start
    parser.EndElementHandler = reader.end
    parser.CharacterDataHandler = reader.data
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise JUnitError(f"not a well-formed XML report: {exc}") from None
    return reader.finish()


# -------------------------------------------------------------------- write --
def to_junit_xml(suites: list[TestSuite]) -> bytes:
    """A JUnit document for ``suites`` (the artifact of tools that do not write one)."""
    root = ET.Element("testsuites")
    for suite in suites:
        node = ET.SubElement(
            root,
            "testsuite",
            name=suite.name,
            tests=str(len(suite.cases)),
            failures=str(suite.count("failed")),
            errors=str(suite.count("error")),
            skipped=str(suite.count("skipped")),
            time=f"{suite.time or sum(c.time for c in suite.cases):.3f}",
        )
        for case in suite.cases:
            attrs = {"name": case.name, "classname": case.classname, "time": f"{case.time:.4f}"}
            if case.file:
                attrs["file"] = case.file
            if case.line:
                attrs["line"] = str(case.line)
            item = ET.SubElement(node, "testcase", attrs)
            if case.status in ("failed", "error", "skipped"):
                tag = {"failed": "failure", "error": "error", "skipped": "skipped"}[case.status]
                child = ET.SubElement(item, tag, message=case.message[:1000])
                child.text = _clip(case.details) or None
    report: bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return report


# ------------------------------------------------------------- tool outputs --
def _go_module(root: Path | None) -> str | None:
    if root is None:
        return None
    try:
        text = (root / "go.mod").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^module\s+(\S+)", text, re.M)
    return match.group(1) if match else None


_GO_LOC = re.compile(r"(\w+_test\.go):(\d+):")
_GO_STATUS = {"pass": "passed", "fail": "failed", "skip": "skipped"}


def _go_events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("Package"):
                events.append(event)
    return events


def _go_case(pkg: str, rel: str, name: str, info: dict[str, Any], text: str) -> TestCase:
    file, line = None, None
    failed = info["status"] == "failed"
    match = _GO_LOC.search(text) if failed else None
    if match:
        file, line = project_path(f"{rel}/{match[1]}" if rel else match[1]), int(match[2])
    message = next((ln.strip() for ln in text.splitlines() if _GO_LOC.search(ln)), "")
    return TestCase(name=name, classname=pkg, file=file, line=line, time=info["time"],
                    status=info["status"], message=message,
                    details=text if failed else "")  # fmt: skip


def from_go_test_json(stdout: str, root: Path | None = None) -> list[TestSuite]:
    """``go test -json`` events → one suite per package (subtests are cases too)."""
    module = _go_module(root)
    cases: dict[str, dict[str, dict[str, Any]]] = {}  # insertion order: packages as they ran
    output: dict[tuple[str, str], list[str]] = {}
    elapsed: dict[str, float] = {}
    for ev in _go_events(stdout):
        pkg, test, action = str(ev["Package"]), ev.get("Test"), ev.get("Action")
        tests = cases.setdefault(pkg, {})
        if action == "output" and test:
            output.setdefault((pkg, str(test)), []).append(str(ev.get("Output", "")))
        elif action in _GO_STATUS:
            time = _float(str(ev.get("Elapsed", 0)))
            if test:
                tests[str(test)] = {"status": _GO_STATUS[str(action)], "time": time}
            else:
                elapsed[pkg] = time
    suites = []
    for pkg, tests in cases.items():
        rel = pkg.removeprefix(module).lstrip("/") if module and pkg.startswith(module) else ""
        built = [_go_case(pkg, rel, name, info, "".join(output.get((pkg, name), [])))
                 for name, info in tests.items()]  # fmt: skip
        if built:
            suites.append(TestSuite(name=pkg, tool="go-test", cases=tuple(built),
                                    time=elapsed.get(pkg, 0.0)))  # fmt: skip
    return suites


_CARGO_RUNNING = re.compile(r"^\s*Running (?:unittests |tests? )?(?P<src>\S+)", re.M)
_PANIC = re.compile(r"panicked at (?P<loc>[^\s:]+):(?P<line>\d+)(?::\d+)?:?\n?(?P<msg>[^\n]*)")
_CARGO_TEST = re.compile(r"^test (?P<name>\S+) \.\.\. (?P<result>ok|FAILED|ignored)", re.M)


def from_cargo_test(text: str) -> list[TestSuite]:
    """``cargo test`` (libtest's text output on stable Rust) → one suite per test target;
    a failure's message and location come from its panic. Durations are not printed by
    stable libtest, so they are 0."""
    marks = [(m.start(), m["src"]) for m in _CARGO_RUNNING.finditer(text)]
    suites: list[TestSuite] = []
    for index, (start, source) in enumerate(marks or [(0, "tests")]):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(text)
        block = text[start:end]
        built = []
        for m in _CARGO_TEST.finditer(block):
            name = m["name"]
            status = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}[m["result"]]
            message, details, file, line = "", "", None, None
            if status == "failed":
                section = re.search(
                    rf"---- {re.escape(name)} stdout ----\n(?P<body>.*?)(?=\n---- |\nfailures:|\Z)",
                    block,
                    re.S,
                )
                details = section["body"] if section else ""
                panic = _PANIC.search(details)
                if panic:
                    file, line = project_path(panic["loc"]), int(panic["line"])
                    message = panic["msg"].strip()
                    line = line if file else None
            classname, _, short = name.rpartition("::")
            built.append(
                TestCase(
                    name=short or name,
                    classname=classname,
                    file=file,
                    line=line,
                    status=status,
                    message=message,
                    details=details,
                )
            )
        if built:
            suites.append(TestSuite(name=source.strip("()"), tool="cargo-test", cases=tuple(built)))
    return suites


def from_tool_output(tool: str, output: str, root: Path | None = None) -> list[TestSuite]:
    """The per-test results of an audit test tool's output (empty when it has none)."""
    if tool == "go-test":
        return from_go_test_json(output, root)
    if tool == "cargo-test":
        return from_cargo_test(output)
    return []


# ------------------------------------------------------------------ summary --
def summarize(suites: list[TestSuite]) -> dict[str, Any]:
    """Totals of all suites (counted from the cases, not from the suites' attributes)."""
    cases = [c for s in suites for c in s.cases]
    return {
        "tests": len(cases),
        "passed": sum(c.status == "passed" for c in cases),
        "failed": sum(c.status == "failed" for c in cases),
        "errors": sum(c.status == "error" for c in cases),
        "skipped": sum(c.status == "skipped" for c in cases),
        "time": round(sum(s.time or sum(c.time for c in s.cases) for s in suites), 3),
    }


__all__ = [
    "JUnitError",
    "TestCase",
    "TestSuite",
    "from_cargo_test",
    "from_go_test_json",
    "from_tool_output",
    "parse_junit",
    "project_path",
    "summarize",
    "to_junit_xml",
]
